#!/usr/bin/env python3
"""按主题从已建好的法规库里抽条文（跨批次、去重、带文号）。

用途：写论文/出专题时，把某个主题相关的条文一次性捞全 —— 比如「股权转让」「重组」
「代持」。法规库是分批建的（~/ng-regs/<批次>/），本工具跨所有批次汇总。

用法:
  python3 scripts/reg_topic.py 股权转让 重组 代持
  python3 scripts/reg_topic.py --tax 印花税            # 只看某税种
  python3 scripts/reg_topic.py --event 横琴            # 只看某事件层文件
  python3 scripts/reg_topic.py 股权转让 --out /tmp/x.md

输出：按「批次 → 来源文件」分组，每条保留 【文号 条号】[…状态]…正文 原样。
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

REGS = Path.home() / "ng-regs"
_CLAUSE = re.compile(r"^- 【")


def iter_lines(root: Path, *, tax: str = "", event: str = ""):
    """产出 (批次, 相对来源, 行)。"""
    for batch in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        dirs = []
        if tax:
            dirs.append(batch / "按税种规则" / tax)
        elif event:
            dirs.append(batch / "按事件")
        else:
            dirs += [batch / "按税种规则", batch / "按事件"]
        for base in dirs:
            if not base.is_dir():
                continue
            for f in sorted(base.rglob("*.md")):
                if event and f.stem != event:
                    continue
                for ln in f.read_text(encoding="utf-8").splitlines():
                    if _CLAUSE.match(ln):
                        yield batch.name, f.relative_to(batch).as_posix(), ln


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("kw", nargs="*", help="主题关键词（任一命中即收）")
    ap.add_argument("--tax", default="", help="限定税种")
    ap.add_argument("--event", default="", help="限定事件层文件")
    ap.add_argument("--root", default=str(REGS))
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    root = Path(a.root)
    if not root.is_dir():
        print(f"✗ 找不到法规库 {root}")
        return 1

    seen: set[str] = set()
    hits: list[tuple[str, str, str]] = []
    for batch, rel, ln in iter_lines(root, tax=a.tax, event=a.event):
        if a.kw and not any(k in ln for k in a.kw):
            continue
        # 去重：同一条文会在多层/多税种出现，按「文号+条号+正文」指纹去重
        fp = ln[:120]
        if fp in seen:
            continue
        seen.add(fp)
        hits.append((batch, rel, ln))

    out = [f"# 主题命中：{'、'.join(a.kw) or '(全部)'}"
           + (f" · 税种={a.tax}" if a.tax else "")
           + (f" · 事件={a.event}" if a.event else ""),
           f"\n共 **{len(hits)}** 条（已去重）\n"]
    cur = None
    for batch, rel, ln in hits:
        key = (batch, rel)
        if key != cur:
            out.append(f"\n## {batch} / {rel}\n")
            cur = key
        out.append(ln + "\n")

    text = "\n".join(out)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"✓ {len(hits)} 条 → {a.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
