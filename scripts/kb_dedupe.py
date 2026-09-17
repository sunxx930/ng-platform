#!/usr/bin/env python3
"""知识库去重：按正文指纹（去空白前 4000 字）判定重复，保留字符最多的一份。"""
import hashlib, os, re
from pathlib import Path
root = Path(os.environ.get('NG_HOME', Path.home()/'.ng-platform'))/'knowledge'/'tax-cases'
groups = {}
for f in sorted(root.glob('*.md')):
    t = f.read_text(encoding='utf-8')
    body = re.sub(r'^---.*?---', '', t, flags=re.S)
    fp = hashlib.sha256(re.sub(r'\s+', '', body)[:4000].encode()).hexdigest()[:12]
    groups.setdefault(fp, []).append((len(t), f))
removed = 0
# 1) 正文指纹
for fp, fs in groups.items():
    if len(fs) > 1:
        fs.sort(reverse=True)
        for _, f in fs[1:]:
            f.unlink(); removed += 1; print('去重删除(内容):', f.name)
# 2) 同标题（type+title），保留字符最多者
bytitle = {}
for f in sorted(root.glob('*.md')):
    t = f.read_text(encoding='utf-8')
    def fm(k):
        return next((l.split(':',1)[1].strip() for l in t.splitlines()[:10] if l.startswith(k+':')),'')
    bytitle.setdefault((fm('type'), fm('title')), []).append((len(t), f))
for key, fs in bytitle.items():
    if len(fs) > 1:
        fs.sort(reverse=True)
        for _, f in fs[1:]:
            f.unlink(); removed += 1; print('去重删除(同名):', f.name)
print(f'去重完成，删除 {removed} 份；剩余 {len(list(root.glob("*.md")))} 条')
