#!/usr/bin/env python3
"""语义归组的前处理：小文件直接按"规则名"出一组，大文件打包成批交子代理。

配合 reg_merge.py（跨批次合并产物）用：
  ~/ng-regs/_合并/按税种规则/<税种>/<规则>.md  →  ~/ng-regs/_合并_语义/按税种规则/...

用法:
  python3 scripts/reg_semantic_prep.py            # 小文件落盘 + 打印大文件批次(JSON)
  python3 scripts/reg_semantic_prep.py --threshold 10
"""
from __future__ import annotations

import argparse
import collections
import json
import os
from pathlib import Path

REGS = Path.home() / "ng-regs"
SRC = REGS / "_合并" / "按税种规则"
DST = REGS / "_合并_语义" / "按税种规则"


def clauses(p: Path) -> list[str]:
    return [l.rstrip("\n") for l in p.read_text(encoding="utf-8").splitlines() if l.startswith("- ")]


def rule_name(p: Path) -> str:
    return p.relative_to(SRC).as_posix()[:-3].replace("/", " · ")


def write_single(src: Path, dst: Path, rows: list[str]) -> None:
    """小文件：整份一组，标题用规则名。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    out = [f"# {rule_name(src)}", f"> 语义归组 · 共 {len(rows)} 条 / 1 组",
           f"## {rule_name(src).split(' · ')[-1]}", *rows]
    dst.write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=int, default=10, help="超过此条数才交子代理")
    ap.add_argument("--cap", type=int, default=150, help="每批最大条数")
    a = ap.parse_args()

    small = big = 0
    pending = []                       # [(n, src, dst, rows)]
    for dp, _, fs in os.walk(SRC):
        for f in sorted(fs):
            if not f.endswith(".md"):
                continue
            src = Path(dp) / f
            rel = src.relative_to(SRC)
            dst = DST / rel
            rows = clauses(src)
            if dst.exists():
                # 已有产物：内容没变 → 跳过；变了 → **留给 reg_semantic_diff 走增量**
                # （只把新增条文插进旧分组，不整桶重读重写）。这里一律不重做。
                continue
            if len(rows) <= a.threshold:
                write_single(src, dst, rows)
                small += 1
            else:
                pending.append((len(rows), str(src), str(dst)))
                big += 1

    # 打包：大文件降序，贪心塞进 cap 大小的批（单个超 cap 的独占一批）
    pending.sort(reverse=True)
    batches, cur, cur_n = [], [], 0
    for n, src, dst in pending:
        if n >= a.cap or cur_n + n > a.cap:
            if cur:
                batches.append(cur)
            if n >= a.cap:
                batches.append([{"in": src, "out": dst, "n": n}])
                cur, cur_n = [], 0
                continue
            cur, cur_n = [{"in": src, "out": dst, "n": n}], n
        else:
            cur.append({"in": src, "out": dst, "n": n})
            cur_n += n
    if cur:
        batches.append(cur)

    # 清掉「源已不存在」的孤儿产物（合并重算后旧分类的桶会消失）
    pruned = 0
    if DST.exists():
        for dp, _, fs in os.walk(DST):
            for f in fs:
                if f.endswith(".md") and not (SRC / os.path.relpath(os.path.join(dp, f), DST)).exists():
                    os.remove(os.path.join(dp, f))
                    pruned += 1
    if pruned:
        print(f"清掉孤儿产物 {pruned} 个", flush=True)

    print(f"小文件(≤{a.threshold}条)已落盘: {small} 个", flush=True)
    print(f"大文件待处理: {big} 个 → {len(batches)} 批", flush=True)
    print(json.dumps(batches, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
