#!/usr/bin/env python3
"""把增量归组结果（<桶名>.result.json）套用到旧语义产物上，并校验。

result.json 形如：
  {"insert": [{"group": 2, "lines": ["- 条文…"]}, {"new_group": "小标题", "lines": [...]}],
   "remove": ["- 条文…"]}
group 是 task.json 里 groups 数组的下标（0 起）；插入到该组**最后一条之后**。

用法: python3 scripts/reg_semantic_apply.py
"""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

REGS = Path.home() / "ng-regs"
TASKS = REGS / ".semantic"


def bullets(lines: list[str]) -> list[str]:
    return [l for l in lines if l.startswith("- ")]


def apply_one(task: dict, result: dict) -> tuple[bool, str]:
    dst = Path(task["dst"])
    src = Path(task["src"])
    lines = dst.read_text(encoding="utf-8").split("\n")

    # 1) 删除
    for dead in result.get("remove", []):
        if dead in lines:
            lines.remove(dead)

    # 2) 插入：按组插到该组最后一条之后
    ins = result.get("insert", [])
    headings = [i for i, l in enumerate(lines) if l.startswith("## ")]

    def group_end(gidx: int) -> int:
        """第 gidx 组的插入位置（该组末尾、下个 ## 之前）。"""
        start = headings[gidx]
        end = headings[gidx + 1] if gidx + 1 < len(headings) else len(lines)
        while end > start + 1 and lines[end - 1] == "":
            end -= 1
        return end

    # 从后往前插，避免下标错位
    for e in sorted([x for x in ins if "group" in x], key=lambda x: -x["group"]):
        pos = group_end(e["group"])
        lines[pos:pos] = list(e["lines"])

    for e in [x for x in ins if "new_group" in x]:
        lines += ["", f"## {e['new_group']}"] + list(e["lines"])

    out = "\n".join(lines)
    if not out.endswith("\n"):
        out += "\n"
    dst.write_text(out, encoding="utf-8")

    # 3) 校验：产物条文多重集 == 源
    a = collections.Counter(l.rstrip("\n") for l in
                            src.read_text(encoding="utf-8", errors="replace").splitlines()
                            if l.startswith("- "))
    b = collections.Counter(bullets(dst.read_text(encoding="utf-8").split("\n")))
    return a == b, (f"{dst.name}: 源{sum(a.values())} 产物{sum(b.values())}")


def main() -> int:
    results = sorted(TASKS.glob("*.result.json"))
    if not results:
        print("没有 *.result.json")
        return 0
    ok = bad = 0
    for rf in results:
        task = json.loads((TASKS / rf.name.replace(".result.json", ".task.json"))
                          .read_text(encoding="utf-8"))
        res = json.loads(rf.read_text(encoding="utf-8"))
        good, msg = apply_one(task, res)
        print(("✓ " if good else "✗ ") + msg)
        ok += good
        bad += (not good)
        if good:
            rf.unlink()
    print(f"套用完成：成功 {ok} / 失败 {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
