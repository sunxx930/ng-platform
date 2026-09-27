"""语义检索（本地小模型，零外部服务）。

盘上布局（**内容永不落明文**，见 9/20 定案）：
    NG_HOME/knowledge/content/<补丁>.enc   tax-cases + index 打包加密 ← 只在内存解
    NG_HOME/knowledge/models/embed/        bge-small-zh-v1.5 (ONNX, 23MB)   ← 公开模型，落盘
    NG_HOME/knowledge/models/rerank/       bge-reranker-base  (ONNX, 283MB) ← 公开模型，落盘

补丁（案例库/法规库）各自独立，检索时**在内存解密**并合并打分；
客户机器上不出现 tax-cases/*.md、index/meta.jsonl 这类明文正文。

两阶段：纯语义召回 top-K →（有重排模型时）cross-encoder 精排 top-N。
实测（3820 块 / 163 份）：带重排三查询全部进 top-2，单次约 1 秒。

模型或补丁缺失时，调用方应回退到 kb.py 的关键词检索（见 kb.search）。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

# bge 检索任务的标准查询指令（文档侧不加）
_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："

_embed_sess = _embed_tok = None
_rerank_sess = _rerank_tok = None


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


def content_packs() -> list[Path]:
    d = content_dir()
    return sorted(d.glob("*.enc")) if d.is_dir() else []


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
    global _embed_sess, _embed_tok, _rerank_sess, _rerank_tok
    if kind == "embed":
        if _embed_sess is None:
            _embed_sess, _embed_tok = _load("embed")
        return _embed_sess, _embed_tok
    if _rerank_sess is None:
        _rerank_sess, _rerank_tok = _load("rerank")
    return _rerank_sess, _rerank_tok


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


def embed(texts: list[str], is_query: bool = False) -> list[list[float]]:
    """均值池化 + L2 归一化（bge 的标准用法）。"""
    import numpy as np
    sess, tok = _sess("embed")
    if is_query:
        texts = [_QUERY_PREFIX + t for t in texts]
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
    """返回 (向量矩阵, 元信息列表)——**在内存解密**，不落明文盘。

    每个补丁（案例/法规）各是一个密文 zip，内含 index/vec.npy + index/meta.jsonl。
    多个补丁的向量按行拼接，检索时一起算分。
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

    Vs, metas, bad = [], [], []
    for p in packs:
        try:
            blob = p.read_bytes()
            plain = AESGCM(key).decrypt(blob[:12], blob[12:], None)
            with _zip.ZipFile(_io.BytesIO(plain)) as zf:
                V = _np.load(_io.BytesIO(zf.read("index/vec.npy")))
                metas += [json.loads(l) for l in
                          zf.read("index/meta.jsonl").decode("utf-8").splitlines() if l.strip()]
            Vs.append(V)
        except Exception as e:      # noqa: BLE001
            # 单个补丁坏了不该把整个检索拖垮；但要**响亮地**报出来（免得内容静默缺失）
            bad.append(f"{p.name}: {e}")
            print(f"[kb] ⚠ 补丁加载失败，已跳过：{p.name} —— {e}", flush=True)
    if not Vs:
        raise RuntimeError(f"全部内容补丁都加载失败: {bad}")
    M = _np.vstack(Vs).astype(_np.float32) if len(Vs) > 1 else Vs[0].astype(_np.float32)
    _index_cache = (M, metas)
    return _index_cache


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
    """
    import numpy as np
    # 闸门：法规库上线而未装 → 案例库整体不可用（堵试用期漏洞）
    reason = gate_reason()
    if reason:
        print(f"[kb] 案例库不可用：{reason}", flush=True)
        return []
    V, meta = load_index()
    qv = np.array(embed([query], is_query=True)[0], dtype=np.float32)
    sims = V @ qv
    order = np.argsort(-sims)[:recall]
    cand = [meta[i] for i in order]
    if rerank_available():
        scores = rerank(query, [c["snippet"] for c in cand])
    else:
        scores = [float(sims[i]) for i in order]
    ranked = sorted(zip(scores, cand), key=lambda x: -x[0])[:topn]
    out = []
    for sc, c in ranked:
        out.append({**c, "score": round(sc, 4)})
    # 试用期口径（2026-09-27 用户定：甲）——**检索不触发起算**。
    # 起算绑定「法规库（第二批法条）启用」，见 updater._install_blob 里的 note_use()。
    # 此前按「命中 type == 法规实务」触发是错的：案例包里就有 538 块贴该标签的实务文章，
    # 客户搜案例会把试用期烧掉。
    return out
