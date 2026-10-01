#!/usr/bin/env python3
"""语义层改按「政策/主题」成组 —— 程序规定与实体规定**不拆分**。

用户 2026-10-01：「程序和实体不分离」「程序规定和实体规定不能拆分」。

旧结构：按税种规则/<税种>/<处理规则>.md（程序、税收优惠、收入、定义…各一个文件）
        —— 同一政策（如「企业重组」）的程序条落在 程序.md、实体条落在 税收优惠.md，被拆开。
新结构：按税种/<税种>/<主题>.md —— 同一主题下，实体组与程序组**放在一起**
       （组标题沿用原来的语义小标题，组内条文原样）。

做法（省钱、不动条文）：把现有 919 个语义组按主题**归并**，不重新切条文。
  1) collect：导出每个税种的「组清单」（组标题 + 首条样本 + 来源规则）交 LLM
  2) LLM 只回「哪些组属于哪个主题」的小 JSON
  3) apply：按主题把各组（标题 + 条文原样）拼成新文件

用法:
  python3 scripts/reg_semantic_topic.py collect
  python3 scripts/reg_semantic_topic.py apply
"""
from __future__ import annotations

import collections
import json
import os
import re
import shutil
import sys
from pathlib import Path

REGS = Path.home() / "ng-regs"
SRC = REGS / "_合并_语义" / "按税种规则"          # 旧层
DST = REGS / "_合并_语义" / "按税种"               # 新层
TASKS = REGS / ".topics"


def collect_groups() -> list[dict]:
    """每个 ## 组一条：gid / tax / rule / title / n / sample / lines(该组条文行)。"""
    out = []
    for tax in sorted(os.listdir(SRC)):
        td = SRC / tax
        if not td.is_dir():
            continue
        for f in sorted(td.glob("*.md")):
            cur = None
            for l in f.read_text(encoding="utf-8", errors="replace").splitlines():
                if l.startswith("## "):
                    if cur:
                        out.append(cur)
                    cur = {"tax": tax, "rule": f.stem, "title": l[3:].strip(),
                           "n": 0, "sample": "", "lines": []}
                elif l.startswith("- ") and cur is not None:
                    cur["n"] += 1
                    cur["lines"].append(l)
                    if not cur["sample"]:
                        cur["sample"] = l[2:][:80]
            if cur:
                out.append(cur)
    for g in out:
        # **稳定 id**：不能按位置编 —— 新批次加入组后位置会错位，旧映射就废了
        g["sid"] = f"{g['tax']}|{g['rule']}|{g['title']}"
    return out


def cmd_collect() -> int:
    TASKS.mkdir(parents=True, exist_ok=True)
    groups = collect_groups()
    by_tax: dict[str, list] = collections.defaultdict(list)
    for g in groups:
        by_tax[g["tax"]].append({k: g[k] for k in ("sid", "rule", "title", "n", "sample")})
    big = {t: v for t, v in by_tax.items() if len(v) >= 25}
    small = {t: v for t, v in by_tax.items() if len(v) < 25}
    for t, v in big.items():
        (TASKS / f"task-{t}.json").write_text(
            json.dumps({"tax": t, "groups": v}, ensure_ascii=False, indent=1), encoding="utf-8")
    if small:
        (TASKS / "task-小税种.json").write_text(
            json.dumps({"tax": "*", "taxes": small}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"组 {len(groups)} 个 → 任务 {len(big) + (1 if small else 0)} 个")
    for t, v in sorted(big.items(), key=lambda x: -len(x[1])):
        print(f"   task-{t}.json: {len(v)} 组")
    if small:
        print(f"   task-小税种.json: {len(small)} 税种 / {sum(len(v) for v in small.values())} 组")
    return 0


def cmd_apply() -> int:
    groups = collect_groups()
    gid2g = {g["sid"]: g for g in groups}
    mapping: dict[str, str] = {}                  # sid → "税种|主题"
    for rf in sorted(TASKS.glob("result-*.json")):
        data = json.loads(rf.read_text(encoding="utf-8"))
        for tax, topics in data.items():
            for t in topics:
                for gid in t["groups"]:
                    if gid in gid2g:
                        mapping[gid] = f"{tax}|{t['主题']}"
    for gid in gid2g:                             # LLM 漏掉的 → 归「其他」，不丢
        mapping.setdefault(gid, f"{gid2g[gid]['tax']}|其他")

    if DST.exists():
        shutil.rmtree(DST)
    buckets: dict[str, list[int]] = collections.defaultdict(list)
    for gid, key in mapping.items():
        buckets[key].append(gid)

    made = total = 0
    for key in sorted(buckets):
        tax, topic = key.split("|", 1)
        topic = re.sub(r"[/\\:*?\"<>|]", "·", topic).strip() or "其他"   # 主题名里可能有 / 等，净化成合法文件名
        lines = [f"# {tax} · {topic}", ""]
        for gid in sorted(buckets[key]):           # 保持原顺序（组内条文原样）
            g = gid2g[gid]
            lines.append(f"## {g['title']}")
            lines += g["lines"]
            lines.append("")
            total += len(g["lines"])
        d = DST / tax
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{topic.strip() or '其他'}.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        made += 1
    print(f"✓ 生成 {made} 个主题文件 / {total} 条 → {DST}")
    # 校验：条文总数应与旧层一致
    old = sum(g["n"] for g in groups)
    print(f"  旧层条文 {old}，新层条文 {total}" + ("（一致）" if old == total else "  ✗ 不一致！"))
    return 0 if old == total else 1


if __name__ == "__main__":
    c = sys.argv[1] if len(sys.argv) > 1 else ""
    fn = {"collect": cmd_collect, "apply": cmd_apply}.get(c)
    if not fn:
        print("用法: reg_semantic_topic.py collect|apply")
        raise SystemExit(2)
    raise SystemExit(fn())
