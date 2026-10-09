#!/usr/bin/env python3
"""清理语义层里「只有 ## 标题、没有一条 '- ' 条文」的空组。

病根：早前「计税依据主税」收敛等操作把条文挪走，apply 只删条文行、不删 `## 标题`
（reg_semantic_apply.py 的 remove 分支），于是留下无内容的孤儿组标题；
主题层再按 sid 拼装时又生成「整份只有空组」的主题文件。

本脚本自包含（不依赖改其它脚本）：
  1) 备份 _合并_语义 → .backups/
  2) 剪掉两层里的空组：
     - 工作层 _合并_语义/按税种规则 ：只剪组，同步刷新 `> 语义归组 · 共 N 条 / M 组`
     - 主题层 _合并_语义/按税种     ：剪组，并删掉剪完后无任何条文的整文件
  3) 重建 _合并_语义/_索引.md（逻辑与 reg_semantic_topic.py cmd_apply 一致）
  4) 校验：两层条文（'- ' 行）总数必须不变

用法:
  python3 scripts/reg_prune_empty.py            # 预览（dry-run，不写任何文件）
  python3 scripts/reg_prune_empty.py --apply    # 实际执行
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

REGS = Path.home() / "ng-regs"
SEM = REGS / "_合并_语义"
WORK = SEM / "按税种规则"     # 工作层（桶）
TOPIC = SEM / "按税种"        # 主题层
BACKUPS = REGS / ".backups"

COUNT_RE = re.compile(r"共 \d+ 条 / \d+ 组")


def count_bullets(lines: list[str]) -> int:
    return sum(1 for l in lines if l.startswith("- "))


def prune_lines(lines: list[str]) -> tuple[list[str], int]:
    """删掉「## 标题」后全为空白（无任何 '- ' 条文）的空组。

    只剪 body 全为空白的组；若组内含非空白非条文行则保守跳过（不误删）。
    返回 (新行列表, 删除组数)。
    """
    heads = [i for i, l in enumerate(lines) if l.startswith("## ")]
    if not heads:
        return lines, 0
    drop: set[int] = set()
    removed = 0
    for k, h in enumerate(heads):
        n = heads[k + 1] if k + 1 < len(heads) else len(lines)
        body = lines[h + 1:n]
        if all(b.strip() == "" for b in body):      # body 全空 → 空组
            drop.update(range(h, n))
            removed += 1
    if not removed:
        return lines, 0
    out = [l for i, l in enumerate(lines) if i not in drop]
    txt = "\n".join(out)
    txt = re.sub(r"\n{3,}", "\n\n", txt)             # 折叠多余空行（防边界残留）
    lines = txt.rstrip("\n").split("\n") if txt.strip() else []
    return lines, removed


def prune_file(p: Path, *, drop_if_bulletless: bool) -> tuple[int, bool]:
    """剪一个文件。返回 (删掉的空组数, 是否删了整文件)。"""
    txt = p.read_text(encoding="utf-8")
    lines = txt.split("\n")
    nb0 = count_bullets(lines)
    new_lines, removed = prune_lines(lines)
    if removed:
        nb = count_bullets(new_lines)
        assert nb == nb0, f"{p}: 剪组后条文数变了 {nb0}->{nb}"
        new_lines = [COUNT_RE.sub(f"共 {nb} 条 / {sum(1 for l in new_lines if l.startswith('## '))} 组", l)
                     if l.startswith("> 语义归组") else l for l in new_lines]
        p.write_text("\n".join(new_lines).rstrip("\n") + "\n", encoding="utf-8")
    if drop_if_bulletless and count_bullets(new_lines) == 0:
        p.unlink()
        return removed, True
    return removed, False


def rebuild_index() -> tuple[int, int, int]:
    """按 reg_semantic_topic.py cmd_apply 的口径重建 _合并_语义/_索引.md。"""
    tax_rows: dict[str, list] = {}
    for tax in sorted(os.listdir(TOPIC)):
        td = TOPIC / tax
        if not td.is_dir():
            continue
        rows = []
        for f in sorted(td.glob("*.md")):
            ls = f.read_text(encoding="utf-8").splitlines()
            rows.append((f.stem,
                         sum(1 for l in ls if l.startswith("## ")),
                         sum(1 for l in ls if l.startswith("- "))))
        tax_rows[tax] = rows
    flat = [r for rs in tax_rows.values() for r in rs]
    nfile, nline, ngrp = len(flat), sum(r[2] for r in flat), sum(r[1] for r in flat)
    out = ["# 语义归组库索引", "",
           f"共 **{nfile}** 个主题文件 / **{nline}** 条 / **{ngrp}** 个语义组", ""]
    for tax in sorted(tax_rows, key=lambda t: -sum(r[2] for r in tax_rows[t])):
        out.append(f"## {tax}")
        for name, g, n in sorted(tax_rows[tax], key=lambda x: -x[2]):
            out.append(f"- {name} —— {g} 组 / {n} 条")
        out.append("")
    (SEM / "_索引.md").write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    return nfile, nline, ngrp


def total_bullets(root: Path) -> int:
    n = 0
    for dp, _, fs in os.walk(root):
        for f in fs:
            if f.endswith(".md"):
                n += count_bullets((Path(dp) / f).read_text(encoding="utf-8").splitlines())
    return n


def empty_group_count(root: Path) -> int:
    n = 0
    for dp, _, fs in os.walk(root):
        for f in fs:
            if not f.endswith(".md"):
                continue
            _, r = prune_lines((Path(dp) / f).read_text(encoding="utf-8").split("\n"))
            n += r
    return n


def main() -> int:
    apply = "--apply" in sys.argv

    w0, t0 = total_bullets(WORK), total_bullets(TOPIC)
    we, te = empty_group_count(WORK), empty_group_count(TOPIC)
    print(f"工作层 {WORK.name}: 条文 {w0} / 空组 {we}")
    print(f"主题层 {TOPIC.name}: 条文 {t0} / 空组 {te}")
    if not (we or te):
        print("没有空组，无事可做。")
        return 0
    if not apply:
        print("\n(dry-run) 加 --apply 实际执行。")
        return 0

    BACKUPS.mkdir(parents=True, exist_ok=True)
    import time
    bk = BACKUPS / f"语义层-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    subprocess_tar = ["tar", "-czf", str(bk), "-C", str(REGS), "_合并_语义"]
    import subprocess
    subprocess.run(subprocess_tar, check=True)
    print(f"备份 → {bk}")

    wr = tr = wf = tf = 0
    for dp, _, fs in os.walk(WORK):
        for f in fs:
            if f.endswith(".md"):
                r, d = prune_file(Path(dp) / f, drop_if_bulletless=False)
                wr += r
                wf += d
    for dp, _, fs in os.walk(TOPIC):
        for f in fs:
            if f.endswith(".md"):
                r, d = prune_file(Path(dp) / f, drop_if_bulletless=True)
                tr += r
                tf += d
    nfile, nline, ngrp = rebuild_index()

    w1, t1 = total_bullets(WORK), total_bullets(TOPIC)
    print(f"\n工作层剪空组 {wr} 个；主题层剪空组 {tr} 个、删空文件 {tf} 个")
    print(f"索引重建：{nfile} 文件 / {nline} 条 / {ngrp} 组")
    ok = (w0 == w1 == t1 == t0)
    print(f"校验：工作层 {w0}→{w1}，主题层 {t0}→{t1}  " + ("✓ 条文数不变" if ok else "✗ 条文数变了！"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
