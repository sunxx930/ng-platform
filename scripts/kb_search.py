#!/usr/bin/env python3
"""本地知识库检索（关键词，零依赖）：python3 scripts/kb_search.py "关键词1 关键词2" """
import os, sys
from pathlib import Path
q = sys.argv[1].split() if len(sys.argv) > 1 else []
root = Path(os.environ.get('NG_HOME', Path.home()/'.ng-platform'))/'knowledge'/'tax-cases'
ent = []
for f in sorted(root.glob('*.md')):
    t = f.read_text(encoding='utf-8')
    score = sum(t.count(k) for k in q)
    if score: ent.append((score, f.name, t))
for s, n, t in sorted(ent, reverse=True)[:5]:
    title = next((l for l in t.splitlines() if l.startswith('title:')), '')
    hit = next((l for l in t.splitlines() if any(k in l for k in q)), '')
    print(f"[{s}] {n} {title}\n    {hit[:120]}")
if not ent: print('无匹配')
