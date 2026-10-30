#!/usr/bin/env python3
"""废止清理：新法规宣布「《某文件》废止」时，把被废止的文件从交付层清出去。

背景：库里同时存在新旧版本时，客户端检索会同时命中旧文件（例如已被
《市场主体登记管理条例》取代的旧条例）。用户 2026-10-30 要求「宣布废止的
文件要自动清理」。

做法（**只认高置信度，宁可不删**）：
1. 扫合并层事件层的每条条文，找出「整份文件废止」的句子；
   - 排除部分废止：句中出现 第X条/款/项/章、附件、附表、附注、表单、
     填报说明、所附、部分 → 跳过（只废了一张表，不是废了整份文件）。
2. 从句子中取出被废止文件的**文号**（书名号后括号里的，或书名号前的
   「…令第153号公布的《X》」）；文号是唯一可靠的身份。
3. 文号规范化后与库内文档的条文注释文号求交集；命中即高置信度。
4. 没有文号时退回「标题完全相等 + 全库唯一」，否则只记进报告不动手。

产物：
- `_合并_语义/_已废止.json`  机器可读清单（reg_export.py 读取并跳过）
- `_合并_语义/_废止清单.md`  人读报告（含依据句子、置信度）

用法：
    .venv/bin/python scripts/reg_repeal.py --src ~/ng-regs/_合并 --dry-run
    .venv/bin/python scripts/reg_repeal.py --src ~/ng-regs/_合并 --apply
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ANN = re.compile(r"〔([^〕]+)〕")
APPLY_MARK = "〔仅适用："

# 废止句只有「整份文件」才算数：判据是**分句窗口**——
# 取「废止」所在分句（上一个 。； 之后到 废止 之间），窗口内出现下列信号即放弃：
#   第X条/款/项/章、附件、附表、附注、表单、填报说明、所附、部分、清单 → 只废了局部
#   根据/依据/按照/为贯彻 → 书名号里的文号是「引用的依据」，不是被废的那份
#   中…规定/中与…不符 → 「X 中有关…的规定废止」也是局部废止
CUE = re.compile(r"废止|停止执行|停止施行")
LOCAL_SCOPE = re.compile(
    r"[条款项章]|附件|附表|附注|表单|填报说明|所附|部分|清单"
    r"|根据|依据|按照|为贯彻|贯彻落实|中[^。；]{0,30}?的?(?:规定|内容|条款)|中与"
    r"|除[^。；]{0,60}?外"          # 「除《X》《Y》…外，其他规定停止执行」→ X/Y 是保留项
)
TITLE = re.compile(r"《([^》]{3,80})》")

# 文号形态：海关总署令第153号 / 国税发[2000]195号 / 国家税务总局公告2018年第31号
NUM_PATTERNS = [
    re.compile(r"(?P<org>[一-龥]{2,14})令第(?P<n>\d+)号"),
    re.compile(r"(?P<org>[一-龥]{2,20}?)令\s*(?P<y>\d{4})\s*年第\s*(?P<n>\d+)\s*号"),
    re.compile(r"(?P<org>[一-龥]{2,20}?)公告\s*(?P<y>\d{4})\s*年第\s*(?P<n>\d+)\s*号"),
    re.compile(r"(?P<y>\d{4})\s*年第\s*(?P<n>\d+)\s*号公告"),
    re.compile(r"(?P<org>[一-龥]{2,12}?)\s*[\[［〔(（]\s*(?P<y>\d{2,4})\s*[\]］〕)）]\s*第?\s*(?P<n>\d+)\s*号"),
]
PAREN = re.compile(r"[(（]([^）)]{2,60})[)）]")


ORG_ALIAS = {
    "国家发展和改革委员会": "发改委", "发展和改革委员会": "发改委", "国家发改委": "发改委",
    "国家税务总局": "税务总局",
    "中国证券监督管理委员会": "证监会", "中国证监会": "证监会",
    "中国银行保险监督管理委员会": "银保监会", "中国银监会": "银监会",
    "中国保监会": "保监会", "中国保险监督管理委员会": "保监会",
}


def norm_org(s: str) -> str:
    s = re.sub(r"[\s　]", "", s).replace("中华人民共和国", "").lstrip("日以")
    for pre in ("国家", "中国"):
        if s.startswith(pre) and len(s) > len(pre) + 2:
            s = s[len(pre):]
    return ORG_ALIAS.get(s, s)


def numbers(text: str) -> set[str]:
    """文本里所有文号，规范成可比较的键。

    返回两级键：`<机关>|令|N` / `<机关>|年号|Y|N`（严格）以及
    `~|令|N` / `~|年号|Y|N`（忽略机关，仅在同号唯一时才用）——
    机关名写法不一致（「国家发改委」vs「国家发展和改革委员会」）是常态。
    """
    out: set[str] = set()
    for rx in NUM_PATTERNS:
        for m in rx.finditer(text):
            g = m.groupdict()
            if g.get("y"):
                y = g["y"]
                y = ("20" + y) if len(y) == 2 else y
                out.add(f"{norm_org(g.get('org') or '')}|年号|{y}|{g['n']}")
                out.add(f"~|年号|{y}|{g['n']}")
            else:
                out.add(f"{norm_org(g['org'])}|令|{g['n']}")
                out.add(f"~|令|{g['n']}")
    # 「2016 年第 34 号令」：令在号后，上面几个模式都不认，而这是部委令最常见的引用写法。
    # 它与库内文档注释里的「…令第34号」是同一份东西 → 归到同一个 `|令|N` 键。
    for m in re.finditer(r"(?P<org>[一-龥]{2,20}?)\s*(?P<y>\d{4})\s*年第\s*(?P<n>\d+)\s*号令",
                         text):
        out.add(f"{norm_org(m['org'])}|令|{m['n']}")
    return out


def read_docs(src: Path) -> dict[str, Path]:
    d = src / "按事件"
    return {p.stem: p for p in sorted(d.glob("*.md"))}


def doc_numbers(path: Path) -> set[str]:
    """文档自身的文号集合：取每条条文注释里「文号 + 条号」那一段。"""
    nums: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("- "):
            continue
        parts = ANN.findall(line)
        if not parts:
            continue
        head = parts[-1].rsplit(" ", 1)[0]      # 〔<文号> <条号>〕
        nums |= numbers(head)
    return nums


def sentence_of(text: str, pos: int, width: int = 60) -> str:
    lo = max(0, text.rfind("。", 0, pos) + 1)
    hi = text.find("。", pos)
    hi = len(text) if hi < 0 else hi + 1
    seg = text[lo:hi].strip()
    return seg if len(seg) <= width * 2 else seg[: width * 2] + "…"


def _inside_title(body: str, pos: int) -> bool:
    """位置是否落在书名号内部（《…废止…》这种标题里的‘废止’不算废止句）。"""
    lo = body.rfind("《", 0, pos)
    return lo >= 0 and body.find("》", lo, pos) < 0


def find_repeals(name: str, path: Path) -> list[dict]:
    """一份文档里所有「整份废止」句子 → [{title, nums, evidence}]。"""
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("- "):
            continue
        body = line[2:].split(APPLY_MARK)[0]    # 去掉 仅适用/文号注释
        # 行头【文档 第X条】与本条自己的「第X条」都不是条文内容：不剥掉的话，
        # 「废止」若出现在本条**第一句**，窗口里必带「条」字 → LOCAL_SCOPE 误判成局部废止。
        # 实测 39 号令第三十二条「本办法自…起施行,《国家企业技术中心认定管理办法》…同时废止」
        # 因此漏检（旧办法里还有「本办法自…施行,第X条…」同型句式）。
        body = re.sub(r"^【[^】]*】", "", body)
        body = re.sub(r"^\s*第[一二三四五六七八九十百零\d]+条\s*", "", body)
        for cue in CUE.finditer(body):
            if _inside_title(body, cue.start()):
                continue
            lo = max(body.rfind("。", 0, cue.start()),
                     body.rfind("；", 0, cue.start())) + 1
            window = body[lo:cue.start()]
            # 书名号内部（标题本身）不参与「局部废止」判定：标题里常有「条」「项」
            if LOCAL_SCOPE.search(re.sub(r"《[^》]*》", "《》", window)):
                continue
            # 窗口里的每个《标题》都是被废止对象；文号取紧随其后的括号。
            # 例外：标题后面跟着“公布/施行/发布”的是**发布该文件的令**，不是被废的。
            for tm in TITLE.finditer(window):
                title = tm.group(1).strip()
                if not title or title == name:
                    continue
                nxt = TITLE.search(window, tm.end())
                tail = window[tm.end(): nxt.start() if nxt else len(window)]
                if re.search(r"公布|施行|发布|印发|审议通过", tail):
                    continue
                nums = numbers(title)
                k = lo + tm.end()
                pm = PAREN.match(body, k)
                if pm:
                    nums |= numbers(pm.group(1))
                if not nums:                    # …海关总署令第153号公布的《X》
                    pre = body[max(0, lo + tm.start() - 30): lo + tm.start()]
                    if re.search(r"(?:公布|发布|印发)的?$", pre):
                        nums |= numbers(pre)
                out.append({
                    "title": title,
                    "nums": sorted(nums),
                    "evidence": sentence_of(body, lo + tm.start()),
                    "line": body[:400],
                })
    return out


def norm_title(s: str) -> str:
    s = re.sub(r"[\s　·《》〈〉]", "", s).replace("中华人民共和国", "")
    # 版本标记（年版/年本/年修订…）要保留，否则 2011 年本会被算成 2019 年本
    s = re.sub(r"[（(]([^）)]*?(?:年版|年本|年修订|年修正|修订版|修正版)[^）)]*)[）)]",
               r"\1", s)
    s = re.sub(r"[（(][^）)]*[）)]", "", s)
    s = re.sub(r"[一-龥]{2,12}\[\d{4}\]\d+号", "", s)
    return s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="合并层目录（含「按事件」）")
    ap.add_argument("--out", default="", help="默认写 <src>/_合并_语义/")
    ap.add_argument("--apply", action="store_true", help="写入清单；缺省只打印")
    args = ap.parse_args()

    src = Path(args.src).expanduser()
    docs = read_docs(src)
    dn = {name: doc_numbers(p) for name, p in docs.items()}
    titled: dict[str, list[str]] = {}
    for name in docs:
        titled.setdefault(norm_title(name), []).append(name)

    confirmed: list[dict] = []
    review: list[dict] = []
    for name, path in docs.items():
        for rep in find_repeals(name, path):
            t_norm, t_nums = norm_title(rep["title"]), set(rep["nums"])
            target = ""
            why = ""
            strict = {k for k in t_nums if not k.startswith("~|")}
            if strict:                       # ① 文号交集（机关名必须一致）
                cand = [d for d, ns in dn.items() if d != name and ns & strict]
                if len(cand) == 1:
                    target, why = cand[0], "文号命中"
                elif cand:
                    why = f"文号命中多份({len(cand)})"
            if not target:                   # ② 标题完全相等且全库唯一
                same = [d for d in titled.get(t_norm, []) if d != name]
                if len(same) == 1:
                    other = dn[same[0]]
                    if strict and other and not (other & strict):
                        why = "标题同名但文号不符"      # 同名不同版本 → 交人工
                    else:
                        target, why = same[0], "标题唯一"
                elif same:
                    why = f"标题同义多份({len(same)})"
            row = {"from": name, "to": rep["title"], "nums": rep["nums"],
                   "evidence": rep["evidence"], "why": why}
            (confirmed if target else review).append({**row, "target": target})

    # 同一目标被多份文件宣布废止时只记一次（保留最新来源）
    uniq: dict[str, dict] = {}
    for r in confirmed:
        uniq.setdefault(r["target"], r)
    print(f"文档 {len(docs)} 份｜确认废止 {len(uniq)} 份｜待人工复核 {len(review)} 条")
    for t, r in sorted(uniq.items()):
        print(f"  ✓ {t[:52]:54s} ← {r['from'][:40]}（{r['why']}）")
    for r in review[:40]:
        print(f"  ? {r['to'][:40]:42s} ← {r['from'][:36]}  [{r['why']}]")

    out_dir = Path(args.out).expanduser() if args.out else src.parent / "_合并_语义"
    if args.apply:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "_已废止.json").write_text(
            json.dumps([{"doc": t, **r} for t, r in sorted(uniq.items())],
                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        lines = ["# 已废止清单（自动生成，勿手改正文）", "",
                 f"> 依据：`scripts/reg_repeal.py`；共 {len(uniq)} 份。",
                 "> 交付层导出时跳过这些文件。", ""]
        for t, r in sorted(uniq.items()):
            lines += [f"## {t}", "",
                      f"- 依据：{r['from']}（{r['why']}）",
                      f"- 原文：{r['evidence']}", ""]
        if review:
            lines += ["## 待人工复核（未清理）", ""]
            lines += [f"- {r['to']} ← {r['from']}：{r['evidence']}"
                      for r in review]
            lines.append("")
        (out_dir / "_废止清单.md").write_text("\n".join(lines), encoding="utf-8")
        print(f"\n已写入 {out_dir/'_已废止.json'}、{out_dir/'_废止清单.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
