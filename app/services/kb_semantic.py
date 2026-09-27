"""语义检索（本地小模型，零外部服务）。

随「小包」（方案B：无服务器 → 知识包内嵌进安装包）下发的内容：
    NG_HOME/knowledge/models/embed/    bge-small-zh-v1.5 (ONNX int8, 23MB)  ← 必需
    NG_HOME/knowledge/index/           vec.npy + meta.jsonl                 ← 必需
    NG_HOME/knowledge/models/rerank/   bge-reranker-base  (ONNX int8, 283MB) ← 可选

两阶段：纯语义召回 top-K →（有重排模型时）cross-encoder 精排 top-N。
实测（3820 块 / 163 份）：带重排三查询全部进 top-2，单次约 1 秒。
**重排是可选增强**：小包不带 283MB 重排模型，此时退化为纯语义召回。
不建议长期缺重排——实测纯语义会被"表面相似"骗（讲境外派遣的会压过正确答案）。

模型不存在或依赖缺失时，调用方应回退到 kb.py 的关键词检索（见 kb.search）。
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


def index_dir() -> Path:
    return kb_dir() / "index"


def _has_model(kind: str) -> bool:
    d = models_dir() / kind
    return (d / "model.onnx").is_file() and (d / "tokenizer.json").is_file()


def rerank_available() -> bool:
    """重排模型在不在（283MB，小包不带 → 可选增强）。"""
    return _has_model("rerank")


def available() -> bool:
    """语义检索是否可用。**只要求 embed + 索引 + 运行时**——重排是可选增强，
    缺它照样能纯语义召回（见 rerank_available）。"""
    try:
        import onnxruntime  # noqa: F401
        from tokenizers import Tokenizer  # noqa: F401
    except Exception:      # noqa: BLE001
        return False
    return (_has_model("embed")
            and (index_dir() / "vec.npy").is_file())


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


def load_index():
    """返回 (向量矩阵, 元信息列表)。"""
    import numpy as np
    d = index_dir()
    V = np.load(d / "vec.npy")
    meta = [json.loads(l) for l in (d / "meta.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    return V, meta


def search(query: str, topn: int = 3, recall: int = 20) -> list[dict]:
    """语义检索：召回 recall 条 →（有重排模型时）精排取 topn。返回体与 kb.search 一致。

    没有重排模型（小包形态）时退化为纯语义 top-n，score 即余弦相似度。
    """
    import numpy as np
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
