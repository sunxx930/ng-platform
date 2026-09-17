#!/usr/bin/env python3
"""知识库总清单：列出所有条目（标题/类型/主题/字符/敏感度/来源）。"""
import os
from pathlib import Path
root = Path(os.environ.get('NG_HOME', Path.home()/'.ng-platform'))/'knowledge'
def fm(t, k):
    for ln in t.splitlines()[:12]:
        if ln.strip().startswith(k+':'): return ln.split(':',1)[1].strip()
    return ''
rows=[]
for f in sorted(root.rglob('*.md')):
    t=f.read_text(encoding='utf-8')
    rows.append((fm(t,'title') or f.stem, fm(t,'type'), fm(t,'topic'),
                 len(t), fm(t,'sensitivity'), fm(t,'source')))
print(f"共 {len(rows)} 条\n")
for i,(ti,ty,tp,n,se,so) in enumerate(rows,1):
    print(f"{i:>2}. [{ty or '-'}] {ti}  | 主题:{tp or '-'} | {n}字 | {se or '-'}")
