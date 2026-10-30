"""语义检索（本地小模型，零外部服务）。

盘上布局（**内容永不落明文**，见 9/20 定案）：
    NG_HOME/knowledge/content/shards/<分片>.enc  每份源文件一个密文（v2，2026-10-21 起）
    NG_HOME/knowledge/content/<补丁>.enc         旧整包（v1，兼容读）
    NG_HOME/knowledge/models/embed/        bge-small-zh-v1.5 (ONNX int8, 24MB)  ← 中文/默认
    NG_HOME/knowledge/models/embed-en/     bge-small-en-v1.5 (ONNX int8, 32MB)  ← 英文条目（2026-10-31 起）
    NG_HOME/knowledge/models/rerank/       bge-reranker-base  (ONNX, 283MB)     ← 公开模型，落盘

**双语索引**：中文法规用 embed（512 维），英文条目（EY 指南等）用 embed-en（384 维），
建索引时按文件语言分流，检索时按**查询语言**选模型（见 search）。中文查询只走 embed，
行为与加英文模型前逐字节一致；纯英文查询走 embed-en，中文模型对英文无能为力
（"VAT rate in Germany" 目标块排到 1,700 名外，2026-10-31 实测）。

补丁（案例库/法规库）各自独立，检索时**在内存解密**并合并打分；
客户机器上不出现 tax-cases/*.md、index/meta.jsonl 这类明文正文。
增量更新只替换变动的分片（见 app/services/updater.py::_install_sharded）。

两阶段：纯语义召回 top-K →（有重排模型时）cross-encoder 精排 top-N。
实测（3820 块 / 163 份）：带重排三查询全部进 top-2，单次约 1 秒。

模型或补丁缺失时，调用方应回退到 kb.py 的关键词检索（见 kb.search）。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

# bge 检索任务的标准查询指令（文档侧不加）；中英文各一句，不能混用
_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
_QUERY_PREFIX_EN = "Represent this sentence for searching relevant passages:"

_sess_cache: dict[str, tuple] = {}


def kb_dir() -> Path:
    return Path(os.environ.get("NG_HOME", Path.home() / ".ng-platform")) / "knowledge"


def models_dir() -> Path:
    return kb_dir() / "models"


def content_dir() -> Path:
    """内容密文目录：每个补丁一个 *.enc（tax-cases + index 打包加密）。

    **这是刻意的**：交付版内容永不落明文盘（定案 9/20）。
    盘上只有密文，检索时在内存解密。
    """
    return kb_dir() / "content"


def shards_dir() -> Path:
    """分片密文目录（v2 格式）：每份源文件一个 .enc，增量更新只替换变动的分片。"""
    return content_dir() / "shards"


def content_packs() -> list[Path]:
    """全部内容密文：旧整包（content/*.enc）+ 新分片（content/shards/*.enc）。"""
    d = content_dir()
    out = sorted(d.glob("*.enc")) if d.is_dir() else []
    s = shards_dir()
    if s.is_dir():
        out += sorted(s.glob("*.enc"))
    return out


def installed_versions() -> list[str]:
    """已安装内容的版本号。

    v1 整包：`content/<版本>.enc` 的文件名就是版本；
    v2 分片：`content/shards/` 下没有单一大包，版本记在 `.installed_cases/.installed_regs` 标记里。
    """
    kb = kb_dir()
    out = [p.stem for p in content_dir().glob("*.enc")] if content_dir().is_dir() else []
    for tag in ("cases", "regs"):
        try:
            v = (kb / f".installed_{tag}").read_text(encoding="utf-8").strip()
        except Exception:      # noqa: BLE001
            v = ""
        if v:
            out.append(v)
    return sorted(set(out))


def _has_model(kind: str) -> bool:
    d = models_dir() / kind
    return (d / "model.onnx").is_file() and (d / "tokenizer.json").is_file()


def rerank_available() -> bool:
    """重排模型在不在（可选增强；缺它退化为纯语义召回）。"""
    return _has_model("rerank")


def available() -> bool:
    """语义检索是否可用：embed 模型 + 至少一个内容补丁 + 运行时。"""
    try:
        import onnxruntime  # noqa: F401
        from tokenizers import Tokenizer  # noqa: F401
    except Exception:      # noqa: BLE001
        return False
    return _has_model("embed") and bool(content_packs())


def _load(kind: str):
    import onnxruntime as ort
    from tokenizers import Tokenizer
    d = models_dir() / kind
    sess = ort.InferenceSession(str(d / "model.onnx"), providers=["CPUExecutionProvider"])
    tok = Tokenizer.from_file(str(d / "tokenizer.json"))
    tok.enable_truncation(max_length=512)
    tok.enable_padding(length=None, pad_id=0, pad_token="[PAD]")
    return sess, tok


def _sess(kind: str):
    if kind not in _sess_cache:
        _sess_cache[kind] = _load(kind)
    return _sess_cache[kind]


def en_available() -> bool:
    """英文嵌入模型在不在（bge-small-en-v1.5）。缺它时英文条目只能靠中文模型 + 双语标签命中。"""
    return _has_model("embed-en")


def _run(sess, tok, texts, pairs=False):
    """跑一次推理，返回 (输出, attention_mask)。"""
    import numpy as np
    encs = tok.encode_batch([(a, b) for a, b in texts] if pairs else list(texts))
    ids = np.array([e.ids for e in encs], dtype=np.int64)
    mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
    feeds = {"input_ids": ids, "attention_mask": mask}
    if "token_type_ids" in {i.name for i in sess.get_inputs()}:
        feeds["token_type_ids"] = np.zeros_like(ids)
    return sess.run(None, feeds)[0], mask


def embed(texts: list[str], is_query: bool = False, lang: str = "zh") -> list[list[float]]:
    """均值池化 + L2 归一化（bge 的标准用法）。lang="en" 走英文模型（384 维）。"""
    import numpy as np
    kind = "embed-en" if lang == "en" else "embed"
    sess, tok = _sess(kind)
    if is_query:
        pre = _QUERY_PREFIX_EN if lang == "en" else _QUERY_PREFIX
        texts = [pre + t for t in texts]
    out, mask = _run(sess, tok, texts)
    m = mask[..., None].astype(np.float32)
    v = (out * m).sum(1) / np.clip(m.sum(1), 1e-9, None)
    v = v / np.clip(np.linalg.norm(v, axis=1, keepdims=True), 1e-9, None)
    return v.tolist()


def rerank(query: str, docs: list[str]) -> list[float]:
    sess, tok = _sess("rerank")
    out, _ = _run(sess, tok, [(query, d) for d in docs], pairs=True)
    return [float(x) for x in out.reshape(-1)]


# 内容索引的内存缓存（每个补丁解一次，进程内复用）
_index_cache: tuple | None = None


def reset_index_cache() -> None:
    """装了/卸了新补丁后调用，让下次检索重新解密建索引。"""
    global _index_cache
    _index_cache = None


def load_index():
    """返回 (中文矩阵, 中文元信息, 英文矩阵, 英文元信息)——**在内存解密**，不落明文盘。

    每个补丁（案例/法规）各是一个密文 zip，内含 index/vec.npy + index/meta.jsonl。
    多个补丁的向量按行拼接，检索时一起算分。

    英文条目（meta 里 `lang == "en"`，由 kb_build_index 按文件语言分流）用另一个模型
    嵌入（384 维），单独成矩阵返回；中英文维度不同，**绝不能拼一起**。
    """
    global _index_cache
    if _index_cache is not None:
        return _index_cache

    import io as _io
    import zipfile as _zip
    import numpy as _np
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from app.services.license import pack_key

    packs = content_packs()
    if not packs:
        raise RuntimeError("没有内容补丁（content/*.enc）")
    key = pack_key()
    if not key:
        raise RuntimeError("无授权/试用密钥，无法解开内容补丁")

    Vs, metas, Ven, metas_en, bad = [], [], [], [], []
    sd = shards_dir()
    for p in packs:
        try:
            blob = p.read_bytes()
            plain = AESGCM(key).decrypt(blob[:12], blob[12:], None)
            if p.parent == sd:              # v2 分片：index.npz（vec + meta 字节）
                with _zip.ZipFile(_io.BytesIO(plain)) as zf:
                    z = _np.load(_io.BytesIO(zf.read("index.npz")))
                    V = z["vec"]
                    raw = bytes(z["meta"]).decode("utf-8")
                    ms = [json.loads(l) for l in raw.splitlines() if l.strip()]
            else:                           # v1 整包：index/vec.npy + index/meta.jsonl
                with _zip.ZipFile(_io.BytesIO(plain)) as zf:
                    V = _np.load(_io.BytesIO(zf.read("index/vec.npy")))
                    ms = [json.loads(l) for l in
                          zf.read("index/meta.jsonl").decode("utf-8").splitlines() if l.strip()]
            # 分片按文件切，一个分片内语言一致；老分片没有 lang 字段 → 中文
            if ms and ms[0].get("lang") == "en":
                Ven.append(V)
                metas_en += ms
            else:
                Vs.append(V)
                metas += ms
        except Exception as e:      # noqa: BLE001
            # 单个补丁坏了不该把整个检索拖垮；但要**响亮地**报出来（免得内容静默缺失）
            bad.append(f"{p.name}: {e}")
            print(f"[kb] ⚠ 补丁加载失败，已跳过：{p.name} —— {e}", flush=True)
    if not Vs and not Ven:
        raise RuntimeError(f"全部内容补丁都加载失败: {bad}")

    def _merge(vecs, ms):
        if not vecs:
            return None, []
        M = _np.vstack(vecs).astype(_np.float32) if len(vecs) > 1 else vecs[0].astype(_np.float32)
        # 分片按源文件切，同一句条文可能同时挂在多个主题（跨文件重复）→ 合并时按正文去重。
        # 放在这里而不是建索引时：否则另一份文件变化时，本文件里被去重掉的那条会凭空消失。
        seen: set[str] = set()
        keep: list[int] = []
        for i, m in enumerate(ms):
            s = m.get("snippet")
            if s in seen:
                continue
            seen.add(s)
            keep.append(i)
        if len(keep) < len(ms):
            M = M[_np.asarray(keep)]
            ms = [ms[i] for i in keep]
        return M, ms

    Mzh, mzh = _merge(Vs, metas)
    Men, men = _merge(Ven, metas_en)
    _index_cache = (Mzh, mzh, Men, men)
    return _index_cache


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text or "")


def gate_reason() -> str | None:
    """案例库闸门原因（None = 可用）。

    用户 2026-09-27 定：**法规库上线后，必须先下载法规库才能继续用案例库**。
    """
    try:
        from app.services.updater import case_gate_reason
        return case_gate_reason(kb_dir())
    except Exception:      # noqa: BLE001
        return None


def search(query: str, topn: int = 3, recall: int = 20) -> list[dict]:
    """语义检索：召回 recall 条 →（有重排模型时）精排取 topn。返回体与 kb.search 一致。

    没有重排模型（小包形态）时退化为纯语义 top-n，score 即余弦相似度。
    纯英文查询有英文模型时只召回英文条目（没有重排也照样可用，见下方 en_ready）。
    """
    import numpy as np
    # 授权闸门：试用到期/未激活 → 税务库整体不可用（内容密钥不下发）
    try:
        from app.services.license import kb_unlocked
        if not kb_unlocked():
            print("[kb] 税务知识库授权已到期：请在「税务版」面板续费并激活授权串", flush=True)
            return []
    except Exception:      # noqa: BLE001
        pass
    # 闸门：法规库上线而未装 → 案例库整体不可用（堵试用期漏洞）
    reason = gate_reason()
    if reason:
        print(f"[kb] 案例库不可用：{reason}", flush=True)
        return []
    V, meta, Ven, meta_en = load_index()
    # 中文查询**一律只走中文模型**：英文模型把中文查询映射成随机向量，混进来只会干扰
    # （中文查到英文条目的路是条目自带的「英文名｜国名（中文国名）」标签，见 reg_foreign_guide）。
    en_ready = (not _has_cjk(query)) and Ven is not None and bool(meta_en) and en_available()
    have_rerank = rerank_available()
    cand: list[dict] = []
    scores: list[float] = []
    if en_ready:
        qe = np.array(embed([query], is_query=True, lang="en")[0], dtype=np.float32)
        se = Ven @ qe
        oe = np.argsort(-se)[:recall]
        cand += [meta_en[i] for i in oe]
        scores += list(map(float, se[oe]))
    # 有重排时要两组候选一起交给重排（中英余弦分不可比，只有重排能统一打分）；
    # 没重排时中英各排各的，英文查询就不掺中文候选——两组余弦差一个量级，混排没有意义。
    if V is not None and meta and (not en_ready or have_rerank):
        qv = np.array(embed([query], is_query=True)[0], dtype=np.float32)
        sims = V @ qv
        order = np.argsort(-sims)[:recall]
        cand += [meta[i] for i in order]
        if not have_rerank:
            scores += list(map(float, sims[order]))
    if not cand:
        return []
    if have_rerank:
        scores = rerank(query, [c["snippet"] for c in cand])
    ranked = sorted(zip(scores, cand), key=lambda x: -x[0])[:topn]
    out = []
    for sc, c in ranked:
        out.append({**c, "score": round(sc, 4)})
    # 试用期口径（2026-09-27 用户定：甲）——**检索不触发起算**。
    # 起算绑定「法规库（第二批法条）启用」，见 updater._install_blob 里的 note_use()。
    # 此前按「命中 type == 法规实务」触发是错的：案例包里就有 538 块贴该标签的实务文章，
    # 客户搜案例会把试用期烧掉。
    return out
