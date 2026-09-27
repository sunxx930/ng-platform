#!/usr/bin/env python3
"""建「交付版」语义索引——与 App 运行时同一套模型（ONNX bge-small-zh, 512 维）。

为什么单独有这个脚本：
  线上索引（NG_HOME/knowledge/index/vec.npy）此前是临时脚本产物，仓库里没有构建入口；
  每次加法规都要重建，必须可复现。分块逻辑与 scripts/kb_semantic.py 的 chunks_of 保持
  逐字一致，保证检索行为与当初实测（3820 块 / 三查询进 top-2）一致。

与 dev 版 scripts/kb_semantic.py 的区别：
  那个用 ollama bge-m3（1024 维、落 sqlite），是原型；本脚本用 App 内嵌的 ONNX 模型，
  产物是 vec.npy + meta.jsonl，直接给 app/services/kb_semantic.py 读。

meta.jsonl **不含 source/原件名**（交付版必须剥指纹，见 docs/CHANGELOG 与交付版约定）。

用法:
  python3 scripts/kb_build_index.py --kb-dir <知识库根>          # 建 <kb>/index/
  python3 scripts/kb_build_index.py --kb-dir <知识库根> --check   # 只校验现有索引
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def chunks_of(path: Path) -> list[tuple[str, str]]:
    """切块：法规按「条/章」，案例按段落（600 字上限）。返回 [(小标题, 文本)]。

    ⚠️ 与 scripts/kb_semantic.py::chunks_of 必须保持一致——改了这里，
       线上索引就得整库重建，否则新旧块对不上。
    """
    raw = path.read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n\n(.*)", raw, re.S)
    meta, body = (m.groups() if m else ("", raw))
    title = (re.search(r"^title:\s*(.+)$", meta, re.M) or [None, path.stem])[1]

    # 先按「第X条」切（税法的主要语义单元）
    parts = re.split(r"(?=第[一二三四五六七八九十百零\d]+条)", body)
    if len(parts) < 2:                       # 没有条文结构 → 按段落
        parts = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
    out, buf = [], ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(p) > 900:                     # 过长再硬切
            for i in range(0, len(p), 800):
                out.append((title, p[i:i + 800]))
            continue
        if len(buf) + len(p) < 600:
            buf = (buf + "\n" + p).strip()
        else:
            if buf:
                out.append((title, buf))
            buf = p
    if buf:
        out.append((title, buf))
    return out


def _fm(meta_text: str, key: str) -> str:
    for ln in meta_text.splitlines()[:12]:
        if ln.strip().startswith(key + ":"):
            return ln.split(":", 1)[1].strip()
    return ""


def build(kb_dir: Path) -> int:
    sys.path.insert(0, str(ROOT))
    from app.services.kb_semantic import embed   # ONNX，与运行时同一模型

    cases = kb_dir / "tax-cases"
    files = sorted(cases.glob("*.md"))
    if not files:
        print(f"✗ {cases} 下没有 .md", file=sys.stderr)
        return 1

    idx = kb_dir / "index"
    idx.mkdir(parents=True, exist_ok=True)

    vecs: list[list[float]] = []
    metas: list[dict] = []
    for f in files:
        raw = f.read_text(encoding="utf-8")
        m = re.match(r"---\n(.*?)\n---\n\n(.*)", raw, re.S)
        meta_text = m.group(1) if m else ""
        ch = chunks_of(f)
        if not ch:
            continue
        embs = embed([c[1] for c in ch])
        for (title, text), v in zip(ch, embs):
            vecs.append(v)
            # 交付版字段：**不含 source / 原件名 / 指纹**
            metas.append({
                "file": f.name,
                "title": title,
                "type": _fm(meta_text, "type"),
                "topic": _fm(meta_text, "topic"),
                "snippet": text,
            })
        print(f"  {f.name[:52]:54} {len(ch):3} 块")

    import numpy as np
    V = np.asarray(vecs, dtype=np.float32)
    np.save(idx / "vec.npy", V)
    (idx / "meta.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in metas),
        encoding="utf-8",
    )
    print(f"\n  ✓ 索引完成: {len(files)} 份 → {len(metas)} 块, 维度 {V.shape[1]}")
    print(f"    {idx/'vec.npy'}  {V.nbytes/1e6:.1f} MB")
    print(f"    {idx/'meta.jsonl'}")
    return 0


def check(kb_dir: Path) -> int:
    idx = kb_dir / "index"
    import numpy as np
    V = np.load(idx / "vec.npy")
    metas = [json.loads(l) for l in (idx / "meta.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"vec.npy    {V.shape}  dtype={V.dtype}")
    print(f"meta.jsonl {len(metas)} 条")
    # 交付版不该带这些字段
    bad = sorted({k for m in metas for k in m if k in ("source", "source_sha256", "ingested", "extract", "sensitivity")})
    print(f"泄漏字段   {bad or '无 ✓'}")
    # 代号残留（交付版应为 实体X，不该有 公司X）
    blob = "\n".join(m.get("snippet", "") for m in metas)
    codes = sorted(set(re.findall(r"公司[A-Z]{1,3}", blob)))
    print(f"公司X 残留 {codes if codes else '无 ✓'}")
    return 0 if not bad and not codes else 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb-dir", default=str(Path(os.environ.get("NG_HOME", Path.home() / ".ng-platform")) / "knowledge"))
    ap.add_argument("--check", action="store_true", help="只校验，不重建")
    a = ap.parse_args()
    kb = Path(a.kb_dir)
    return check(kb) if a.check else build(kb)


if __name__ == "__main__":
    sys.exit(main())
