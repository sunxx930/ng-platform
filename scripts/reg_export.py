#!/usr/bin/env python3
"""法规库 → 交付版内容（tax-regs/）：把封版的工作版知识库导出成可打包的 Markdown。

用法:
  python3 scripts/reg_export.py --src ~/ng-regs/_合并_语义/按税种 --out <目录>

产物:
  <out>/tax-regs/<税种>/<主题>.md   （front-matter: title/type/tax/topic）

口径:
  - 只读源库，**不改**封版内容；导出可随时重跑
  - 交付版 strip 内部标注〔税种依文件标题〕〔适用主体未识别〕；
    〔文号 条号〕引用与〔仅适用：…〕保留（客户需要引用出处）
  - 一条条文 = 一行 `- `，打包索引按行切块
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

_INTERNAL_MARKS = re.compile(r"〔(?:税种依文件标题|适用主体未识别)〕")
_BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _clean_name(s: str, maxlen: int = 90) -> str:
    s = _BAD_NAME.sub("_", s).strip().strip(".")
    return (s or "未命名")[:maxlen]


def export(src: Path, out: Path) -> int:
    src = Path(src)
    if not src.is_dir():
        print(f"✗ 源目录不存在: {src}", file=sys.stderr)
        return 1
    dst_root = Path(out) / "tax-regs"
    dst_root.mkdir(parents=True, exist_ok=True)

    files = sorted(src.glob("*/*.md"))
    if not files:
        print(f"✗ {src} 下没有 <税种>/<主题>.md", file=sys.stderr)
        return 1

    n_files = n_bullets_in = n_bullets_out = 0
    used: dict[str, int] = {}
    for f in files:
        tax = f.parent.name
        raw = f.read_text(encoding="utf-8")
        if not raw.strip():
            continue
        topic = f.stem
        # 源文件第一行是「# 税种 · 主题」，正文从第二行起（保留分组标题与条文行）
        lines = raw.splitlines()
        body_lines = []
        for i, ln in enumerate(lines):
            if i == 0 and ln.startswith("# "):
                continue
            if ln.startswith("- "):
                n_bullets_in += 1
                ln = _INTERNAL_MARKS.sub("", ln)
                n_bullets_out += 1
            body_lines.append(ln)
        body = "\n".join(body_lines).strip("\n")

        # 文件名去重（同一税种下若有两个主题清洗后同名，追加序号）
        base = _clean_name(topic)
        key = f"{tax}/{base}"
        used[key] = used.get(key, 0) + 1
        if used[key] > 1:
            base = f"{base}（{used[key]}）"

        d = dst_root / _clean_name(tax)
        d.mkdir(parents=True, exist_ok=True)
        fm = (
            "---\n"
            f"title: {topic}\n"
            "type: 法规\n"
            f"tax: {tax}\n"
            f"topic: {topic}\n"
            "---\n\n"
        )
        (d / f"{base}.md").write_text(fm + body + "\n", encoding="utf-8")
        n_files += 1

    # 非税法规模（2026-10-30 加）：全篇无税种落点的法规（如《对外投资管理办法》）
    # 进不了「按税种」主题层，此前会被无声地挡在交付之外。用户在 `_合并_语义/非税法规/`
    # 放一份 `<主题>.md` 即可随库发布；这里同样导出到 `tax-regs/非税/`。
    # ⚠ 该目录是**交付层**（不是 reg_semantic_topic 的产物）——后者每次 apply 会 rmtree
    #   整棵「按税种」，所以非税件必须放在它的**同级**目录，才能跨重跑留存。
    extra_dir = src.parent / "非税法规"
    if extra_dir.is_dir():
        e_files = sorted(extra_dir.glob("*.md"))
        e_used: set[str] = set()   # _clean_name 截断到 90 字，超长标题可能撞名 → 追加序号
        for f in e_files:
            raw = f.read_text(encoding="utf-8")
            if not raw.strip():
                continue
            topic = f.stem
            lines = raw.splitlines()
            body_lines = []
            for i, ln in enumerate(lines):
                if i == 0 and ln.startswith("# "):
                    continue
                if ln.startswith("- "):
                    n_bullets_in += 1
                    ln = _INTERNAL_MARKS.sub("", ln)
                    n_bullets_out += 1
                body_lines.append(ln)
            body = "\n".join(body_lines).strip("\n")
            d = dst_root / _clean_name("非税")
            d.mkdir(parents=True, exist_ok=True)
            fm = ("---\n"
                  f"title: {topic}\n"
                  "type: 法规\n"
                  "tax: 非税\n"
                  f"topic: {topic}\n"
                  "---\n\n")
            base = _clean_name(topic)
            name, k = base, 2
            while name in e_used:
                name = f"{base}-{k}"
                k += 1
            e_used.add(name)
            (d / f"{name}.md").write_text(fm + body + "\n", encoding="utf-8")
            n_files += 1
        print(f"  ✓ 非税法规 {len(e_files)} 份")

    print(f"  ✓ 导出 {n_files} 份主题文件")
    print(f"    条文行 {n_bullets_in} → {n_bullets_out}")
    print(f"    {dst_root}")
    if n_bullets_in != n_bullets_out:
        print("  ⚠ 条文行数在清洗后发生变化（内部标注不应增减行数）", file=sys.stderr)
        return 2
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(Path.home() / "ng-regs" / "_合并_语义" / "按税种"),
                    help="工作版主题层目录（按税种/<主题>.md）")
    ap.add_argument("--out", required=True, help="输出根目录（会生成 tax-regs/ 子目录）")
    a = ap.parse_args()
    return export(Path(os.path.expanduser(a.src)), Path(os.path.expanduser(a.out)))


if __name__ == "__main__":
    sys.exit(main())
