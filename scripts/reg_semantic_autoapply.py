#!/usr/bin/env python3
"""机械生成增量归组结果（<桶名>.result.json），替代子代理。

适用：新条文能被 **(事件/文号)** 唯一确定归属组的批次（如"捐赠"这类 dst 已按文号成组的桶）。
规则：每条 new 行按其行尾文号，找 dst 里标题含该文号的组 → 落入；
      找不到则新建组，标题优先复用**全库其它桶**里含该文号的既有组标题，
      否则用 f"{仅适用事件}（{文号}）"。remove 原样回填。

不适用（会误判）：同一文号在一桶内被语义拆成多组的桶 —— 那类仍走子代理。
生成后跑 semantic_build.sh regroup-apply（有源/产物多重集校验）。
"""
from __future__ import annotations

import collections
import glob
import json
import re
from pathlib import Path

REGS = Path.home() / "ng-regs"
SEM = REGS / "_合并_语义" / "按税种规则"
TASKS = REGS / ".semantic"

_TAG = re.compile(r"〔([^〕]*?)〕")
_EVENT = re.compile(r"〔仅适用：([^〕]+)〕")
# 行尾条号：第X条 / 一、(子号) / 1． / 前言 / 附录 / 附件 / 正文
_CLAUSE = re.compile(
    r"^(?:第[0-9一二三四五六七八九十百零]+条(?:[-－(].*)?"
    r"|[一二三四五六七八九十百零]+(?:[-－][^ ]+)?"
    r"|\d+(?:\.\d+)*(?:[-－][^ ]+)?"
    r"|前言|附录|附件|正文|附表|目录)$")


def doc_of(line: str) -> str | None:
    """取行尾〔文号 条号〕里的文号（剥掉条号与"仅适用"等附加标签）。"""
    for t in reversed(_TAG.findall(line)):
        if t.startswith("仅适用：") or t in ("税种依文件标题", ""):
            continue
        # 网页页脚型坏标签：`〔国家税务总局公告2012年第29号 前言】****… 【来源〕`
        # （PDF 里括号用了形近的 】，贪婪吃到整段解读页脚）→ 先截断再取文号
        t = re.split(r"[】【]|\*{2,}", t)[0]
        toks = t.split()
        if len(toks) > 1 and _CLAUSE.match(toks[-1]):
            toks = toks[:-1]
        return " ".join(toks).strip()
    return None


def event_of(line: str) -> str | None:
    m = _EVENT.search(line)
    return m.group(1).strip() if m else None


def global_title_by_doc() -> dict[str, str]:
    """全库扫描：文号 → 既有组标题（含该文号），保证新建组命名一致。"""
    m: dict[str, str] = {}
    for p in glob.glob(str(SEM / "**" / "*.md"), recursive=True):
        for l in Path(p).read_text(encoding="utf-8").splitlines():
            if l.startswith("## "):
                title = l[3:].strip()
                mm = re.search(r"（([^）]+)）\s*$", title)
                if mm:
                    m.setdefault(mm.group(1).strip(), title)
    return m


def main() -> int:
    gtitle = global_title_by_doc()
    made = 0
    for f in sorted(TASKS.glob("*.task.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        groups = d["groups"]
        inserts: dict[int, list[str]] = collections.defaultdict(list)
        new_groups: dict[str, list[str]] = collections.OrderedDict()
        for line in d["new"]:
            doc = doc_of(line)
            hits = [k for k, g in enumerate(groups) if doc and doc in g]
            if len(hits) == 1:
                inserts[hits[0]].append(line)
            else:
                title = (gtitle.get(doc) if doc else None) \
                    or (f"{event_of(line)}（{doc}）" if doc and event_of(line)
                        else (f"{event_of(line)}" if event_of(line) else (doc or "其他")))
                new_groups.setdefault(title, []).append(line)
        out = {"insert": [{"group": k, "lines": v} for k, v in sorted(inserts.items())]
               + [{"new_group": t, "lines": v} for t, v in new_groups.items()],
               "remove": d.get("removed", [])}
        res = TASKS / f.name.replace(".task.json", ".result.json")
        res.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        made += 1
        print(f"  {f.name.replace('.task.json',''):26s} "
              f"插入 {len(d['new'])} 条 / {len(inserts)} 现有组 + {len(new_groups)} 新组"
              f"；删除 {len(d.get('removed', []))}")
    print(f"生成 {made} 个 result.json → {TASKS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
