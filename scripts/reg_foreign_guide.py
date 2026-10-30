#!/usr/bin/env python3
"""EY 全球税务指南（英文大部头）→ 可检索交付件。

背景：这类指南一份上千页、按国家分章，既不是中国法规（没有「第X条」「文号」），
也不能整份塞进一个分片（几千页拼一块，检索只能命中零星段落）。用户 2026-10-31
上传 7 份 EY Global Tax Guides，要求入库发布。

做法（只做交付层，不动合并层/桶层/主题层）：
  1. 按国家切段：每页第一条「字号 ≥ 11.5 且在国家词典里」的行 = 国家标题；
     词典见 `scripts/data/ey_countries.txt`（从 VAT 指南的 13pt 标题抽出）。
  2. 页眉页脚（字号 < 8.3）、页码、联系方式（含 @/+电话）丢弃；
     正文按行重排成段落（上一行没有句末标点就接着拼）。
  3. 每段一行：`- <指南简称>｜<国家> · <段落>` —— **标签必须逐行带**，
     `kb_build_index` 只用条目正文做嵌入与重排，`## 组名` 不入向量
     （2026-10-30 在产业目录上踩过：裸正文的重排分 0.8，带标签 9.0）。
  4. 写 `_合并_语义/非税法规/<标题>.md`，随 `reg_export.py` 导出到 `tax-regs/非税/`。

用法：
    .venv/bin/python scripts/reg_foreign_guide.py --pdf x.pdf --title "EY 增值税与商品服务税指南（2026）" [--apply]
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

DEFAULT_LEX = Path(__file__).parent / "data" / "ey_countries.txt"

BULLET = re.compile(r"^[•▪●◦\-–—]\s*")
PAGE_NO = re.compile(r"^[\divxlIVXLCDM]{1,6}$")
CONTACT = re.compile(r"@|\+\d|\bTel\b|\bPhone\b|\bMobile\b|\bFax\b")
TERM = re.compile(r"[.!?:;”\"')\]]$")


def load_lex(path: Path) -> set[str]:
    return {l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()}


def norm(s: str) -> str:
    return re.sub(r"[\s   ]+", " ", s).strip()


def page_lines(page) -> list[tuple[float, str]]:
    out = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            spans = l.get("spans", [])
            if not spans:
                continue
            t = norm("".join(s["text"] for s in spans))
            if not t:
                continue
            size = max(s["size"] for s in spans)
            out.append((round(size, 1), t))
    return out


def collect(pdf: Path, lex: set[str]) -> list[tuple[str, list[str]]]:
    """→ [(国家, 段落文本…)]，按文件顺序。"""
    import pymupdf

    doc = pymupdf.open(str(pdf))
    # 页眉页脚判别：同一行文字在十几页上重复出现、且字号 ≤ 11 —— 这些是跑页眉
    # （"Worldwide Estate and Inheritance Tax Guide 2026" / "EY Worldwide … Guide 2026"），
    # 会挡住页首的国名标题，靠文本模式难免漏（各指南写法不统一），按出现频次判最稳。
    from collections import Counter
    freq: Counter = Counter()
    for pno in range(doc.page_count):
        for sz, t in page_lines(doc[pno]):
            if sz <= 11.0:
                freq[t] += 1

    sections: list[list] = []          # [[country, [paras]]]
    country = ""
    started = False                    # 第一个国家标题之前是封面/目录/概览矩阵，没有归属国，整段丢掉
    buf = ""                           # 正在拼的段落
    paras: list[str] = []

    def flush_para():
        nonlocal buf
        if buf:
            for piece in split_long(buf):
                # 表格残片（「✓」「Tax credits」「EY contacts:」+ 人名）会拼成短段，
                # 60 字以下一律丢：正文段落都远超这个长度。
                if len(piece) >= 60:
                    paras.append(piece)
            buf = ""

    def flush_section():
        nonlocal paras
        flush_para()
        if country and paras:
            sections.append([country, paras])
        paras = []

    # 各国指南的章标题字号不统一（VAT/公司税 13pt、遗产/TP/固定资产/研发 14pt + 40pt 章扉页），
    # 但同一份文件内是固定的。取「页首第一处 ≥12pt 且命中词典」出现最多的字号作为标题字号，
    # 正文里同为国名、字号不同的行（如研发指南 15pt 汇总表里的国名）就不会假成新章节。
    from collections import Counter as _C
    _c: _C = _C()
    for pno in range(doc.page_count):
        for sz, t in page_lines(doc[pno]):
            if PAGE_NO.match(t) or sz < 12:
                continue
            if t in lex:
                _c[sz] += 1
            break
    head_sz = _c.most_common(1)[0][0] if _c else 13.0

    for pno in range(doc.page_count):
        lines = page_lines(doc[pno])
        # 国家标题 = 本页**第一处**「字号≥11.5 且在国家词典里」的短行。
        # 不能只看「本页第一条≥11.5 的行」：遗产税指南正文页先出现页码(12pt)，
        # 40pt 的国名排在页眉之后，那样会一个国也切不出来（实测只剩 2 段）。
        def _noise(sz: float, t: str) -> bool:
            return (sz < 8.3 or bool(PAGE_NO.match(t)) or bool(CONTACT.search(t))
                    or (t in lex and sz < 11.5)
                    or (sz <= 11.0 and freq.get(t, 0) >= 10))

        head_i = None
        for i, (sz, t) in enumerate(lines):
            if _noise(sz, t):
                continue
            # 正文页的国名标题永远在本页最前面（页码/页眉之后）；不是则本页无标题。
            # 只看「第一处命中」（旧写法）在转让定价指南上会把正文里 14pt 的国名提及
            # 当成新章节，切出 804 段（实际 ~50 国）。
            # 章扉页的国名是 40pt（≥30 一律认），正文页标题必须等于本文件标题字号。
            if t in lex and len(t) <= 60 and (sz == head_sz or sz >= 30):
                head_i = i
            break
        if head_i is not None:
            flush_section()
            country = lines[head_i][1]
            started = True
            lines = lines[head_i + 1:]
        for sz, t in (lines if started else []):
            if sz < 8.3 or PAGE_NO.match(t) or CONTACT.search(t):
                continue
            if t in lex:                        # 页眉里的国名（10pt）等：不算正文
                continue
            if "Worldwide" in t and t.endswith("Guide") and sz <= 10.2:
                continue                        # 页眉指南名
            if BULLET.match(t):
                flush_para()
                buf = BULLET.sub("", t)
            elif buf and not TERM.search(buf):
                buf += " " + t
            else:
                flush_para()
                buf = t
    flush_section()

    # 相邻同名合并：转让定价指南每页页首都印一次国名（14pt），不合并会切出
    # 一页一段的 784 段。再丢掉 400 字以内的小段：目录/联系方式页会假成一段。
    merged: list[list] = []
    for c, ps in sections:
        if merged and merged[-1][0] == c:
            merged[-1][1].extend(ps)
        else:
            merged.append([c, list(ps)])
    return [(c, ps) for c, ps in merged if sum(len(p) for p in ps) >= 400]


def split_long(s: str, n: int = 700) -> list[str]:
    out = []
    while len(s) > n:
        cut = max(s.rfind(". ", 0, n), s.rfind("; ", 0, n))
        cut = cut + 1 if cut > n // 2 else n
        out.append(s[:cut].strip())
        s = s[cut:].strip()
    if s:
        out.append(s)
    return out


def render(sections: list[tuple[str, list[str]]], title: str, short: str,
           zh: dict[str, str], zh_topic: str = "", topic_all: bool = False) -> str:
    """标签设计（2026-10-31 实测定稿，别随手改）：

    每行都带的标签会直接进向量，所以两头都得照顾：
      - 只用中文指南名（「EY 转让定价指南」）→ 几万条英文条目把中文法规挤出召回，
        「转让定价 独立交易原则 关联交易」top-50 全是 EY 块；
      - 只有英文名 + 中文国名 → 中文国别查询能命中（阿根廷 增值税 排第 2），
        但中文**主题**词对不上（「美国 遗产税 税率」目标块余弦 0.52、排 560 名）；
      - 每行都挂「（国名 · 主题）」→ 国别主题查询解决了，但**又把中文主题查询带坏**：
        「转让定价 独立交易原则」去重后第 2/3 名变成 Cyprus/South Korea（EY 块）。
    折中：**中文主题只挂在每个国家段的头一条**（每份指南约 100 行，全库 ~800 行），
    其余行只带「英文指南名｜国名（中文国名）」。头部段落通常就是该节的概览
    （税率/规则），中文国别+主题查询能命中它，而几万条正文不会挤占中文主题查询。"""
    out = [f"# 非税 · {title}", ""]
    for country, paras in sections:
        out += [f"## {country}", ""]
        base = f"{short}｜{country}"
        zc = zh.get(country, "")
        head_note = " · ".join(x for x in (zc, zh_topic) if x)
        head_tag = base + (f"（{head_note}）" if head_note else "")
        body_tag = base + (f"（{zc}）" if zc else "")
        for i, p in enumerate(paras):
            use_head = topic_all or i == 0
            out.append(f"- {(head_tag if use_head else body_tag)} · {p}")
        out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--title", required=True, help="交付件标题（= 文件名）")
    ap.add_argument("--short", default="", help="逐行标签用的简称，缺省=标题前半段")
    ap.add_argument("--lexicon", default=str(DEFAULT_LEX))
    ap.add_argument("--zh-map", default=str(DEFAULT_LEX.parent / "ey_countries_zh.json"))
    ap.add_argument("--zh-topic", default="", help="标签里的中文主题词（如「增值税」）")
    ap.add_argument("--topic-all", action="store_true",
                    help="中文主题挂到每一行（只用于中文语料几乎不涉的主题，如遗产税）")
    ap.add_argument("--dest", default=str(Path.home() / "ng-regs" / "_合并_语义" / "非税法规"))
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    lex = load_lex(Path(a.lexicon))
    import json as _json
    zh = _json.loads(Path(a.zh_map).read_text(encoding="utf-8")) if Path(a.zh_map).is_file() else {}
    short = a.short or re.split(r"[（(]", a.title)[0].strip()
    sections = collect(Path(a.pdf).expanduser(), lex)
    n_para = sum(len(ps) for _, ps in sections)
    n_char = sum(len(p) for _, ps in sections for p in ps)
    print(f"国家段 {len(sections)}｜段落 {n_para}｜正文 {n_char:,} 字｜标签「{short}」｜主题「{a.zh_topic}」")
    print("样例：")
    for c, ps in sections[:2]:
        print(f"   {c}: {ps[0][:80] if ps else '(空)'}")
    if not a.apply:
        print("（--apply 才落盘）")
        return 0
    dest = Path(a.dest).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    p = dest / f"{a.title}.md"
    p.write_text(render(sections, a.title, short, zh, a.zh_topic, a.topic_all), encoding="utf-8")
    print(f"✓ {p}  {p.stat().st_size/1024/1024:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
