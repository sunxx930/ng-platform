#!/usr/bin/env python3
"""目录/清单类法规的**表格条目** → 可检索的交付件。

背景：产业目录、禁限目录这类文件，正文只有寥寥几条，真内容是一张几千行的
条目表。`reg_build` 把大表当附件单独留存、不拆条文（否则一张表会虚增几千"条
文"、污染桶层），结果**表里的产业一个都搜不到**。用户 2026-10-30 要求按
HS 减让表的先例处理：表行保留、可检索，但不进税种层。

做法（只做交付层，不动合并层/桶层）：
  1. 直接读 PDF 文字层，按「部分 → 省级 → 大类 一、→ 小类 （一）→ 编号条目」归位；
  2. 每种一行 `- <部分>｜<层级…>｜<N>.<正文>`，行内自带上下文（检索命中时不用回查章节）；
  3. 写成 `_合并_语义/非税法规/<标题>.md`，由 `reg_export.py` 随库导出。

用法：
    .venv/bin/python scripts/reg_catalog.py --pdf <x.pdf> --out-title "<交付件标题>"
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

PART = re.compile(r"^(?:.*?)(全国鼓励外商投资产业目录|中西部等地区外商投资优势产业目录|中西部地区外商投资优势产业目录)$")
PROV = re.compile(r"^[一-鿿]{2,8}(?:省|自治区|市)(?:（[^）]{0,20}）)?$")
MAJOR = re.compile(r"^([一二三四五六七八九十]+)、([^\d。；]{2,30})$")
MINOR = re.compile(r"^（([一二三四五六七八九十]+)）([^\d。；]{2,30})$")
ENTRY = re.compile(r"^(\d+)[.．](.*)$")


def lines_of(pdf: Path, skip_first: int = 1) -> list[str]:
    import pymupdf
    d = pymupdf.open(str(pdf))
    out = []
    for i in range(skip_first, d.page_count):
        for l in d[i].get_text().splitlines():
            s = l.strip()
            if s:
                out.append(s)
    return out


def parse(lines: list[str], prov_fallback: str = "") -> list[dict]:
    part = major = minor = prov = ""
    rows: list[dict] = []
    cur = None
    last_num = None        # 本节上一条的编号：条目编号是连续递增的，用它挡掉「0.3 毫米…」这类续行

    def flush():
        nonlocal cur
        if cur:
            rows.append(cur)
            cur = None

    for s in lines:
        m = PART.match(s)
        if m and len(s) <= 30:                    # 分部标题
            flush(); part = m.group(1); major = minor = prov = ""; last_num = None
            continue
        if PROV.match(s):
            flush(); prov = s; major = minor = ""; last_num = None; continue
        m = MAJOR.match(s)
        if m:
            flush(); major = s; minor = ""; continue
        m = MINOR.match(s)
        if m:
            flush(); minor = s; continue
        m = ENTRY.match(s)
        if m:
            n = int(m.group(1))
            # 编号必须在本节里递增（容许个位数跳号）：否则就是「厚度0.3 毫米…」这种
            # 以数字起头的**续行**被误当成新条目（实测把第 146 条劈成「0.」+「3 毫米…」）
            if last_num is None:
                ok = n == 1
            else:
                ok = last_num < n <= last_num + 5
            if not ok:
                if cur is not None:
                    cur["text"] += s
                continue
            flush(); last_num = n
            cur = {"num": m.group(1), "text": m.group(2),
                   "part": part, "prov": prov, "major": major, "minor": minor}
            continue
        if cur is not None and not s.startswith(("附件", "注：")):
            cur["text"] += s                  # 版面换行：条目正文被劈成两行
        # 其余（页眉页脚/说明行）丢弃
    flush()
    return rows


def render(rows: list[dict], title: str) -> str:
    """每个「分部 / 省份」一个 `##` 组（索引按组切块，组名进检索结果），
    组内条目自带大类/小类前缀 —— 一个 600 字块常从半截开始，前缀保证单条可读。"""
    out = [f"# 非税 · {title}", ""]
    last = None
    for r in rows:
        grp = (r["part"], r["prov"])
        if grp != last:
            if r["prov"]:
                head = f"{r['prov']}（{r['part']}）"
            else:
                head = r["part"] or "目录"
            out += [f"## {head}", ""]
            last = grp
        ctx = [x for x in (r["major"], r["minor"]) if x]
        if r["prov"]:
            ctx = []                      # 省份已在组名里
        body = f"{r['num']}.{r['text']}"
        out.append(f"- {'｜'.join(ctx + [body]) if ctx else body}")
    out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--out-title", required=True, help="交付件标题（= 文件名，不含 .md）")
    ap.add_argument("--dest", default=str(Path.home() / "ng-regs" / "_合并_语义" / "非税法规"))
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    rows = parse(lines_of(Path(a.pdf).expanduser()))
    parts: dict[str, int] = {}
    for r in rows:
        parts[r["part"] or "?"] = parts.get(r["part"] or "?", 0) + 1
    print(f"条目 {len(rows)} 行｜分部：{parts}")
    print("样例：")
    for r in rows[:2] + rows[-2:]:
        print("   ", f"{r['part']}｜{r['prov']}｜{r['major']}｜{r['minor']}｜{r['num']}.{r['text'][:40]}")
    if not a.apply:
        print("（--apply 才落盘）")
        return 0
    dest = Path(a.dest).expanduser()
    p = dest / f"{a.out_title}.md"
    p.write_text(render(rows, a.out_title), encoding="utf-8")
    print(f"✓ {p}  {p.stat().st_size/1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
