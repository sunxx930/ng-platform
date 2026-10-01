#!/usr/bin/env python3
"""跨批次合并：把各批次的「按税种规则」「按事件」并成一份**全局库**。

用户 2026-09-30：「最后要以条款内容为单位整理，打破文件界限」→
  · 单位 = **条文内容**：每行以正文起头，文号/条号挪到行末作标注〔文号 条号〕；
  · 打破文件界限：**不再按来源文件分块**（去掉原 `## 文件名` 小标题），
    同一「税种×处理规则」下的条文跨文件平铺；
  · 排序按**内容**（正文），内容相近的条文自然聚到一起。

用法:
  python3 scripts/reg_merge.py                 # 输出到 ~/ng-regs/_合并/
  python3 scripts/reg_merge.py --out /tmp/m
"""
from __future__ import annotations

import argparse
import re
from collections import OrderedDict, defaultdict
from pathlib import Path

REGS = Path.home() / "ng-regs"
_CLAUSE = re.compile(r"^- 【(?P<doc>.+?) (?P<unit>\S+)】(?P<rest>.*)$")


def parse_line(ln: str):
    """`- 【文号 单元】正文…` → (文号, 单元, 正文+标注)。不是条文行返回 None。"""
    m = _CLAUSE.match(ln)
    return (m.group("doc"), m.group("unit"), m.group("rest").strip()) if m else None


def collect(root: Path, section: str):
    """section = 按税种规则 | 按事件。返回 {相对子路径: [(doc,unit,rest), ...]}。"""
    buckets: dict[str, list] = defaultdict(list)
    for batch in sorted(p for p in root.iterdir()
                        if p.is_dir() and not p.name.startswith((".", "_"))):
        base = batch / section
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*.md")):
            rel = f.relative_to(base).as_posix()          # 税种/规则.md  或  事件.md
            for ln in f.read_text(encoding="utf-8").splitlines():
                row = parse_line(ln)
                if row:
                    buckets[rel].append(row)
    return buckets


def dedup(rows):
    """同一条文可能在多层出现 —— 按「文号+单元+正文」指纹去重。"""
    seen, out = set(), []
    for doc, unit, rest in rows:
        fp = (doc, unit, rest[:120])
        if fp in seen:
            continue
        seen.add(fp)
        out.append((doc, unit, rest))
    return out


# 排序时**剥掉行首条号标记**（第一条 / 一、 / （一） / 1.）——否则全按"第一条"排，
# 内容聚不到一起。剥掉后按正文首字排，"对在横琴…""对在前海…"这种自然相邻。
_LEAD_MARK = re.compile(r"^(?:第[一二三四五六七八九十百零\d]+条|[一二三四五六七八九十百]+、"
                        r"|[（(][一二三四五六七八九十]+[)）]|\d+[．.])\s*")


def render(title: str, rows) -> str:
    rows = sorted(dedup(rows), key=lambda r: _LEAD_MARK.sub("", r[2]))   # 按**内容**排
    docs = {r[0] for r in rows}
    out = [f"# {title}", f"> 全库 {len(docs)} 份法规 / {len(rows)} 条条文",
           "> （以条文内容为单位，打破文件界限；文号作行末标注）", ""]
    for doc, unit, rest in rows:
        out.append(f"- {rest}  〔{doc} {unit}〕")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(REGS))
    ap.add_argument("--out", default=str(REGS / "_合并"))
    a = ap.parse_args()
    root, out_root = Path(a.root), Path(a.out)
    if not root.is_dir():
        print(f"✗ 找不到法规库 {root}")
        return 1
    # ⚠ 合并是**整体重算**：先清空输出目录，否则上轮的产物会残留成脏数据
    # （实测：重新分类后「关税/收入.md」这种旧分类产物还留着，指向已不存在的桶）。
    import shutil
    if out_root.exists():
        shutil.rmtree(out_root)

    index = ["# 全局库索引（跨批次合并）", ""]
    total_files = total_clauses = 0
    for section in ("按税种规则", "按事件"):
        buckets = collect(root, section)
        index.append(f"## {section}")
        for rel, rows in sorted(buckets.items()):
            rows = dedup(rows)
            dst = out_root / section / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            title = rel[:-3].replace("/", " · ")
            dst.write_text(render(title, rows), encoding="utf-8")
            total_files += 1
            total_clauses += len(rows)
            index.append(f"- {rel[:-3]}：{len(rows)} 条")
        index.append("")
    index.insert(2, f"共 {total_files} 个规则/事件文件 / {total_clauses} 条（去重后）\n")
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "_索引.md").write_text("\n".join(index), encoding="utf-8")
    print(f"✓ 合并完成 → {out_root}")
    print(f"  {total_files} 个文件 / {total_clauses} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
