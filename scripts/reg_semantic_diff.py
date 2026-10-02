#!/usr/bin/env python3
"""增量语义归组：为「内容变了的桶」生成**小任务**（只含新增/删除的条文）。

背景：新批次入库会改到若干「税种×规则」桶。整桶重做归组要 LLM 重读重写整份文件
（几千字）；其实只是加了几条 —— 增量做法：
  · 给 LLM 的输入 = 现有**分组标题列表** + 新增条文 + 删除条文（不含整份正文）
  · 让它只输出"哪几条插进哪个组"的小 JSON
  · 由 reg_semantic_apply.py 套用到旧文件上
省的是输入+输出两侧的大头。

用法: python3 scripts/reg_semantic_diff.py
输出: ~/ng-regs/.semantic/<桶名>.task.json（交子代理）→ 结果写 <桶名>.result.json → 跑 apply
"""
from __future__ import annotations

import collections
import json
import os
import re
from pathlib import Path

_TAG = re.compile(r"\s*「(?:已作废|部分作废)」")   # 含前导空格（line_of 里是" 「已作废」"）


def reconcile_tags(src_lines: list[str], dst_path: Path) -> bool:
    """语义产物里与某条源条文**只差「已作废」标签**的行，机械改成源行（不动分组）。

    作废状态是机械后缀，没必要惊动 LLM。返回是否改动过。
    """
    dst_lines = dst_path.read_text(encoding="utf-8").split("\n")
    pool: dict[str, list[int]] = collections.defaultdict(list)
    for i, l in enumerate(dst_lines):
        if l.startswith("- "):
            pool[_TAG.sub("", l)].append(i)
    used, changed = set(), False
    for s in src_lines:
        for i in pool.get(_TAG.sub("", s), []):
            if i not in used and dst_lines[i] != s:
                dst_lines[i] = s
                used.add(i)
                changed = True
                break
            if i not in used:
                used.add(i)
                break
    if changed:
        dst_path.write_text("\n".join(dst_lines), encoding="utf-8")
    return changed

REGS = Path.home() / "ng-regs"
SRC = REGS / "_合并" / "按税种规则"
DST = REGS / "_合并_语义" / "按税种规则"
TASKS = REGS / ".semantic"


def clauses(p: Path) -> list[str]:
    return [l.rstrip("\n") for l in p.read_text(encoding="utf-8", errors="replace").splitlines()
            if l.startswith("- ")]


def main() -> int:
    TASKS.mkdir(parents=True, exist_ok=True)
    made = prune = 0
    for dp, _, fs in os.walk(SRC):
        for f in sorted(fs):
            if not f.endswith(".md"):
                continue
            src = Path(dp) / f
            rel = src.relative_to(SRC)
            dst = DST / rel
            if not dst.exists():
                continue                       # 新桶：没旧产物，走原来的"整份归组"（prep 会挑出来）
            sl = clauses(src)
            reconcile_tags(sl, dst)            # 先机械同步「已作废」类标签，剩下的才算真变动
            a, b = collections.Counter(sl), collections.Counter(clauses(dst))
            name = rel.as_posix().replace("/", "__")[:-3]
            if a == b:
                # 没变：**清掉可能遗留的陈旧任务**。否则上一轮生成的 task.json 会滞留，
                # 被误当成"本次任务"生成 result 再套用（实测把已归好的条文又插一遍、重复）。
                (TASKS / f"{name}.task.json").unlink(missing_ok=True)
                (TASKS / f"{name}.result.json").unlink(missing_ok=True)
                continue
            new = list((a - b).elements())     # 源里有、产物里没有 → 要插入
            removed = list((b - a).elements())  # 产物里有、源里没有 → 要删除
            langs = [l[2:].strip() for l in dst.read_text(encoding="utf-8").splitlines()
                     if l.startswith("## ")]
            (TASKS / f"{name}.task.json").write_text(json.dumps(
                {"src": str(src), "dst": str(dst), "groups": langs,
                 "new": new, "removed": removed}, ensure_ascii=False, indent=1), encoding="utf-8")
            made += 1
            print(f"  增量任务 {rel}: +{len(new)} / -{len(removed)}", flush=True)

    # 清理：源/产物都已不存在的任务文件
    for t in TASKS.glob("*.task.json"):
        if not Path(json.loads(t.read_text(encoding="utf-8"))["src"]).exists():
            t.unlink(missing_ok=True)
            (TASKS / t.name.replace(".task.json", ".result.json")).unlink(missing_ok=True)

    print(f"增量任务 {made} 个 → {TASKS}（孤儿清理 {prune}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
