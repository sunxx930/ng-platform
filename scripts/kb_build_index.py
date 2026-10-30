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
  python3 scripts/kb_build_index.py --kb-dir <知识库根>              # 案例库（tax-cases/）
  python3 scripts/kb_build_index.py --kb-dir <知识库根> --kind regs  # 法规库（tax-regs/）
  python3 scripts/kb_build_index.py --kb-dir <知识库根> --check      # 只校验现有索引
  python3 scripts/kb_build_index.py --kb-dir <知识库根> --no-cache   # 换模型后全量重算

默认复用已有索引里「同一句正文」的向量，只给新条文跑模型（加一批法规几秒完成）；
同时按源文件产出 index/shards/（增量更新用）。
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


def _is_en(raw: str) -> bool:
    """按正文语言分流：中日韩字符占比 <2% 且有拉丁字母 → 英文条目（走 embed-en）。

    2026-10-31 加：EY 全球指南这类英文大部头用中文模型嵌入等于随机向量
    （"VAT rate in Germany" 目标块排到 1,700 名外），必须换成英文模型。
    口径宽松即可：中文法规即使夹英文条款（税率表、英文缩写）也远高于 2%。
    """
    body = re.sub(r"^---\n.*?\n---\n", "", raw, flags=re.S)
    cjk = len(re.findall(r"[\u4e00-\u9fff]", body))
    latin = len(re.findall(r"[A-Za-z]", body))
    return latin > 0 and cjk / max(cjk + latin, 1) < 0.02


def _fm(meta_text: str, key: str) -> str:
    for ln in meta_text.splitlines()[:12]:
        if ln.strip().startswith(key + ":"):
            return ln.split(":", 1)[1].strip()
    return ""


def _chunks_regs(path: Path) -> list[tuple[str, str]]:
    """法规条文切块：按 `## 文档组` 分段，组内把连续条文并到 ~600 字。

    法规索引按「一条条文一行 `- `」的导出格式切块（见 scripts/reg_export.py）；
    每个块保留所属文档组标题（含文号），检索命中后能直接看出出处。
    """
    raw = path.read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n\n(.*)", raw, re.S)
    meta, body = (m.groups() if m else ("", raw))
    topic = _fm(meta, "title") or path.stem

    out: list[tuple[str, str]] = []
    group, buf = topic, ""
    for ln in body.splitlines():
        if ln.startswith("## "):
            if buf:
                out.append((group, buf)); buf = ""
            group = ln[3:].strip() or topic
            continue
        if not ln.startswith("- "):
            continue
        p = ln[2:].strip()
        if not p:
            continue
        if len(p) > 900:                       # 超长表格/清单，硬切
            if buf:
                out.append((group, buf)); buf = ""
            for i in range(0, len(p), 800):
                out.append((group, p[i:i + 800]))
            continue
        if len(buf) + len(p) < 600:
            buf = (buf + "\n" + p).strip()
        else:
            if buf:
                out.append((group, buf))
            buf = p
    if buf:
        out.append((group, buf))
    return out


def build(kb_dir: Path, kind: str = "cases", no_cache: bool = False) -> int:
    sys.path.insert(0, str(ROOT))
    from app.services.kb_semantic import embed   # ONNX，与运行时同一模型

    if kind == "regs":
        root = kb_dir / "tax-regs"
        files = sorted(root.rglob("*.md"))
        if not files:
            print(f"✗ {root} 下没有 .md", file=sys.stderr)
            return 1
    else:
        root = kb_dir / "tax-cases"
        files = sorted(root.glob("*.md"))
        if not files:
            print(f"✗ {root} 下没有 .md", file=sys.stderr)
            return 1

    idx = kb_dir / "index"
    idx.mkdir(parents=True, exist_ok=True)
    # 每份源文件一个「分片」：增量更新只重打变了的文件（见 scripts/build_kbpack.py --base）。
    # 分片内**不做跨文件去重**（否则另一个文件变化时，本文件里被去重掉的那条会凭空消失）；
    # 跨文件重复由运行时 load_index 合并时按正文去重。
    import hashlib
    shards_dir = idx / "shards"
    shards_dir.mkdir(parents=True, exist_ok=True)
    _prefix = "r-" if kind == "regs" else "c-"

    import numpy as np
    vec_blocks: list = []                   # 中文：bge-small-zh（512 维）
    metas: list[dict] = []
    vec_blocks_en: list = []                # 英文：bge-small-en（384 维），与中文分开存
    metas_en: list[dict] = []
    seen: set[str] = set()                  # 法规库跨主题重复条文去重（同一句在一份文档的多个主题里出现）
    seen_en: set[str] = set()
    dtype = np.float16 if kind == "regs" else np.float32

    # 向量缓存：同一句正文的向量与文件无关，已有索引里的向量可直接复用。
    # 加一批新法规只欠新条文那几百条推理，不必把 3 万多条全部重算（--no-cache 可强制全算；
    # 换模型时必须 --no-cache）。
    # 缓存分中文/英文两套（两个模型维度不同，绝不能混用）
    cache: dict[str, "np.ndarray"] = {}
    cache_en: dict[str, "np.ndarray"] = {}
    for _vec, _meta, _c in ((idx / "vec.npy", idx / "meta.jsonl", cache),
                            (idx / "vec-en.npy", idx / "meta-en.jsonl", cache_en)):
        if no_cache or not (_vec.is_file() and _meta.is_file()):
            continue
        try:
            V0 = np.load(_vec)
            for i, ln in enumerate(_meta.read_text(encoding="utf-8").splitlines()):
                if ln.strip():
                    _c.setdefault(json.loads(ln)["snippet"], V0[i])
        except Exception as e:      # noqa: BLE001
            print(f"  ⚠ 已有索引读不了（{e}），全量重算")
            _c.clear()
    print(f"  复用已有向量 中文 {len(cache)} 条 / 英文 {len(cache_en)} 条（新条文才走模型）")

    written: set[str] = set()
    for f in files:
        raw = f.read_text(encoding="utf-8")
        m = re.match(r"---\n(.*?)\n---\n\n(.*)", raw, re.S)
        meta_text = m.group(1) if m else ""
        rel = f.relative_to(root).as_posix() if kind == "regs" else f.name
        ch = _chunks_regs(f) if kind == "regs" else chunks_of(f)
        if not ch:
            continue
        # 只算缓存里没有的条文；分批推理（单个主题文件可能含上万条，如协定 HS 表，
        # 一次喂给 ONNX 会爆内存）
        en = _is_en(raw)
        _cache = cache_en if en else cache
        lang = "en" if en else "zh"
        todo = list(dict.fromkeys(c[1] for c in ch if c[1] not in _cache))
        for i in range(0, len(todo), 64):
            batch = todo[i:i + 64]
            for t, v in zip(batch, embed(batch, lang=lang)):
                _cache[t] = np.asarray(v, dtype=dtype)
        embs = [_cache[c[1]] for c in ch]

        def _meta(title: str, text: str) -> dict:
            # 交付版字段：**不含 source / 原件名 / 指纹**
            meta = {"file": rel, "title": title, "type": _fm(meta_text, "type"),
                    "topic": _fm(meta_text, "topic"), "snippet": text}
            if kind == "regs" and _fm(meta_text, "tax"):
                meta["tax"] = _fm(meta_text, "tax")
            if en:
                meta["lang"] = "en"     # 运行时按它选英文模型算分
            return meta

        # 1) 分片（文件全量，供增量更新）：id 由路径定，hash 由内容定
        shard_metas = [_meta(t, x) for (t, x) in ch]
        V = np.asarray(embs, dtype=dtype)
        mj = "\n".join(json.dumps(x, ensure_ascii=False) for x in shard_metas)
        h = hashlib.sha256(V.tobytes() + mj.encode("utf-8")).hexdigest()
        sid = _prefix + hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]
        np.savez_compressed(shards_dir / f"{sid}.npz", vec=V,
                            meta=np.frombuffer(mj.encode("utf-8"), dtype=np.uint8),
                            path=np.array(rel), hash=np.array(h))
        written.add(f"{sid}.npz")

        # 2) 全局索引（跨文件按正文去重）。同一句条文常同时挂在同一份文档的多个主题下
        #    （组标题不同），只按 (标题, 正文) 去重会漏，检索 top-3 出现重复句。
        rows, kept = [], 0
        for (title, text), v in zip(ch, embs):
            if kind == "regs":
                if text in (seen_en if en else seen):
                    continue
                (seen_en if en else seen).add(text)
            (metas_en if en else metas).append(_meta(title, text))
            rows.append(v)
            kept += 1
        if rows:
            (vec_blocks_en if en else vec_blocks).append(np.asarray(rows, dtype=dtype))
        print(f"  {rel[:52]:54} {len(ch):3} 块" + (f" → {kept}" if kept != len(ch) else "") + ("  [en]" if en else ""))

    # 源文件被删/重命名 → 清掉它的旧分片，否则增量包还会一直带着它
    for p in shards_dir.glob("*.npz"):
        if p.name not in written:
            p.unlink()

    # 法规库块数大（~3.5 万），float16 存储把 vec.npy 减半；运行时 load_index 会转 float32
    V = np.vstack(vec_blocks) if vec_blocks else np.zeros((0, 512), dtype)
    np.save(idx / "vec.npy", V)
    (idx / "meta.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in metas),
        encoding="utf-8",
    )
    Ven = np.vstack(vec_blocks_en) if vec_blocks_en else None
    if Ven is not None:
        np.save(idx / "vec-en.npy", Ven)
        (idx / "meta-en.jsonl").write_text(
            "\n".join(json.dumps(x, ensure_ascii=False) for x in metas_en),
            encoding="utf-8",
        )
    else:                                   # 没有英文条目 → 清掉旧产物，免得留着上一版
        for f in (idx / "vec-en.npy", idx / "meta-en.jsonl"):
            if f.is_file():
                f.unlink()
    print(f"\n  ✓ 索引完成: {len(files)} 份 → 中文 {len(metas)} 块（{V.shape[1]} 维）"
          + (f" + 英文 {len(metas_en)} 块（{Ven.shape[1]} 维）" if Ven is not None else ""))
    print(f"    {idx/'vec.npy'}  {V.nbytes/1e6:.1f} MB")
    print(f"    {idx/'meta.jsonl'}")
    print(f"    {shards_dir}  {len(list(shards_dir.glob('*.npz')))} 个分片（增量更新用）")
    return 0


def check(kb_dir: Path, kind: str = "cases") -> int:
    idx = kb_dir / "index"
    import numpy as np
    V = np.load(idx / "vec.npy")
    metas = [json.loads(l) for l in (idx / "meta.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"vec.npy    {V.shape}  dtype={V.dtype}")
    print(f"meta.jsonl {len(metas)} 条")
    bad: list[str] = []
    # 英文条目单独一套（384 维，见双语索引说明）；没有英文条目时这两个文件不存在
    if (idx / "vec-en.npy").is_file():
        Ven = np.load(idx / "vec-en.npy")
        men = [json.loads(l) for l in (idx / "meta-en.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        print(f"vec-en.npy {Ven.shape}  dtype={Ven.dtype}  meta-en {len(men)} 条")
        if Ven.shape[0] != len(men):
            print("⚠ vec-en 行数与 meta-en 条数不一致")
            bad.append("vec-en.rows")
        if men and men[0].get("lang") != "en":
            print("⚠ meta-en 里没有 lang=en 标记，客户端会把它当中文块")
            bad.append("meta-en.lang")
    # 交付版不该带这些字段
    bad += sorted({k for m in metas for k in m if k in ("source", "source_sha256", "ingested", "extract", "sensitivity")})
    print(f"泄漏字段   {bad or '无 ✓'}")
    # 代号残留（只对案例库有意义；法规原文里的「A公司」不是脱敏泄漏）
    codes: list[str] = []
    if kind == "cases":
        blob = "\n".join(m.get("snippet", "") for m in metas)
        codes = sorted(set(re.findall(r"公司[A-Z]{1,3}", blob)))
        print(f"公司X 残留 {codes if codes else '无 ✓'}")
    return 0 if not bad and not codes else 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb-dir", default=str(Path(os.environ.get("NG_HOME", Path.home() / ".ng-platform")) / "knowledge"))
    ap.add_argument("--kind", choices=("cases", "regs"), default="cases",
                    help="cases=案例库；regs=法规库（tax-regs/ 下按条切块）")
    ap.add_argument("--check", action="store_true", help="只校验，不重建")
    ap.add_argument("--no-cache", action="store_true",
                    help="不复用已有向量（换了嵌入模型时必须加）")
    a = ap.parse_args()
    kb = Path(a.kb_dir)
    return check(kb, a.kind) if a.check else build(kb, a.kind, no_cache=a.no_cache)


if __name__ == "__main__":
    sys.exit(main())
