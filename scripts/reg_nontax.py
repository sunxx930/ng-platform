#!/usr/bin/env python3
"""非税件交付：把「全篇无税种」的法规从事件层抽成交付件（`_合并_语义/非税法规/`）。

背景：判定为 `taxes=[]` 的法规进不了「按税种」主题层，客户端只收 `按税种`，
这类文件默认**不会被交付**（用户 2026-10-30「我上传了全部文件就是要你整理发布」）。
`reg_export.py` 会把这个同级目录一并导出到 `tax-regs/非税/`。

放**同级**目录的原因：`reg_semantic_topic.py apply` 每次 rmtree 整棵 `按税种`，
交付件必须放在它旁边才不会被重跑删掉。

用法：
    # 点名几份（新入库、已确认全篇无税种）
    .venv/bin/python scripts/reg_nontax.py --docs "发改委令第37号 《…》" "发改委令第39号 《…》"
    # 全库补齐：事件层里有、桶层里没有、也还没交付的，全部落一份
    .venv/bin/python scripts/reg_nontax.py --all-missing [--apply]
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

APPLY_MARK = "〔仅适用："
_ONLY_APPLY = re.compile(r"〔仅适用：([^〕]+)〕")


def event_docs(regs: Path) -> dict[str, Path]:
    return {p.stem: p for p in sorted((regs / "_合并" / "按事件").glob("*.md"))}


def taxed_docs(regs: Path) -> set[str]:
    """桶层里出现过的文档名 = 已进「按税种」层，不能再当非税件交付。"""
    out: set[str] = set()
    for f in (regs / "_合并" / "按税种规则").rglob("*.md"):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.startswith("- "):
                out.update(m.strip() for m in _ONLY_APPLY.findall(line))
    return out


def clauses_of(path: Path) -> list[str]:
    return [l for l in path.read_text(encoding="utf-8").splitlines() if l.startswith("- ")]


def write_doc(out_dir: Path, name: str, bullets: list[str]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{name}.md"
    body = [f"# 非税 · {name}", "", f"## {name}", ""] + bullets
    p.write_text("\n".join(body) + "\n", encoding="utf-8")
    return p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--regs", default=str(Path.home() / "ng-regs"))
    ap.add_argument("--docs", nargs="*", default=[], help="文档名（= 事件层文件名，不含 .md）")
    ap.add_argument("--all-missing", action="store_true", help="补齐所有未交付的非税件")
    ap.add_argument("--apply", action="store_true", help="--all-missing 时真正落盘（缺省只统计）")
    a = ap.parse_args()

    regs = Path(a.regs).expanduser()
    docs = event_docs(regs)
    out_dir = regs / "_合并_语义" / "非税法规"

    if a.docs:
        miss = [d for d in a.docs if d not in docs]
        if miss:
            print("✗ 事件层里没有这些文档：")
            for d in miss:
                print("   ", d)
            return 1
        taxed = taxed_docs(regs)
        for name in a.docs:
            if name in taxed:
                print(f"  ! {name} 在按税种层里有落点，跳过（不是非税件）")
                continue
            p = write_doc(out_dir, name, clauses_of(docs[name]))
            print(f"  ✓ {p.name}")
        return 0

    if not a.all_missing:
        ap.print_help()
        return 2

    taxed = taxed_docs(regs)
    todo = [n for n in docs if n not in taxed and not (out_dir / f"{n}.md").is_file()]
    print(f"事件层 {len(docs)} 份｜按税种层 {len(taxed)} 份｜待补 {len(todo)} 份")
    for n in todo[:20]:
        print("   ", n[:70])
    if len(todo) > 20:
        print(f"    …另 {len(todo) - 20} 份")
    if not a.apply:
        print("（--apply 才会落盘）")
        return 0
    for n in todo:
        write_doc(out_dir, n, clauses_of(docs[n]))
    print(f"✓ 落盘 {len(todo)} 份 → {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
