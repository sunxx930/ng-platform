"""本地知识库检索（v1.2.7，零依赖）。

目录：NG_HOME/knowledge/<分类>/*.md（front-matter: title/source…）。
检索：关键词计分（中文按 2+ 字词/英文数字词），返回 TopN 摘要片段供 Agent 注入上下文。
纯本地、无 embedding；规模大或需语义再考虑向量（改造清单）。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_TERM = re.compile(r"[一-龥]{2,}|[A-Za-z0-9]{2,}")
_STOP = {"我们", "可以", "进行", "以及", "通过", "相关", "情况", "问题", "需要", "是否", "如何"}


def kb_root() -> Path:
    return Path(os.environ.get("NG_HOME", Path.home() / ".ng-platform")) / "knowledge"


def _entries():
    root = kb_root()
    if not root.is_dir():
        return
    for f in sorted(root.rglob("*.md")):
        try:
            yield f, f.read_text(encoding="utf-8")
        except Exception:      # noqa: BLE001
            continue


def _front(text: str, key: str) -> str:
    for ln in text.splitlines()[:12]:
        if ln.strip().startswith(f"{key}:"):
            return ln.split(":", 1)[1].strip()
    return ""


def _note_if_regulations(hits: list[dict]) -> None:
    """试用期口径：只有用到**法规**才开始计时（第一批案例不触发收费倒计时）。"""
    if any(h.get("type") == "法规实务" for h in hits):
        try:
            from app.services.license import note_use
            note_use()
        except Exception:      # noqa: BLE001
            pass


def search(query: str, topn: int = 3, min_score: int = 2) -> list[dict]:
    """优先语义检索（模型随知识包下发）；模型不在或依赖缺失 → 回退关键词。"""
    try:
        from app.services import kb_semantic
        if kb_semantic.available():
            hits = kb_semantic.search(query, topn=topn)
            _note_if_regulations(hits)
            return hits
    except Exception:          # noqa: BLE001
        pass                   # 语义不可用不该拖垮检索，回退关键词
    return _search_keyword(query, topn=topn, min_score=min_score)


def _search_keyword(query: str, topn: int = 3, min_score: int = 2) -> list[dict]:
    terms = [t for t in _TERM.findall(query or "") if t not in _STOP]
    if not terms:
        return []
    hits = []
    for f, text in _entries():
        score = sum(text.count(t) for t in terms)
        if score < min_score:
            continue
        pos = -1
        for t in terms:
            i = text.find(t)
            if i >= 0 and (pos < 0 or i < pos):
                pos = i
        snippet = re.sub(r"\s+", " ", text[max(0, pos - 200): pos + 600]).strip()
        hits.append({"file": f.name, "title": _front(text, "title") or f.stem,
                     "source": _front(text, "source"), "type": _front(text, "type"),
                     "score": score, "snippet": snippet})
    hits.sort(key=lambda h: h["score"], reverse=True)
    out = hits[:topn]
    _note_if_regulations(out)
    return out


def context_block(query: str, topn: int = 3) -> str:
    """给 Agent 用：把 TopN 命中拼成带来源的参考块（无命中返回空串）。"""
    hs = search(query, topn=topn)
    if not hs:
        return ""
    out = ["【知识库参考】（仅作参考、以官方现行规定为准；引用请标注来源）"]
    for h in hs:
        out.append(f"- 来源：{h['title']}（{h['source'] or h['file']}）\n  {h['snippet']}")
    return "\n".join(out)
