#!/usr/bin/env python3
"""全库二次脱敏：用本地映射表把所有客户名（含英文简称/交叉提及）统一替换为代号。"""
import json, sys, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kb_ingest import _variants, desensitize
base = Path(__import__('os').environ.get('NG_HOME', Path.home()/'.ng-platform'))
m = json.loads((base/'_client_map.json').read_text(encoding='utf-8'))
root = base/'knowledge'/'tax-cases'
n = 0
for f in sorted(root.glob('*.md')):
    t = f.read_text(encoding='utf-8'); o = t
    t = desensitize(t)                      # 中介/事务所/联系方式等固定规则
    for code, name in m.items():
        for v in _variants(name):
            t = t.replace(v, code)
    # 标题/来源也脱敏；必要时重命名文件
    lines = t.splitlines()
    for i, ln in enumerate(lines[:10]):
        if ln.startswith(('title:', 'source:')):
            lines[i] = ln.split(':',1)[0] + ': ' + desensitize(ln.split(':',1)[1].strip())
    t = '\n'.join(lines) + ('\n' if t.endswith('\n') else '')
    newname = re.sub(r'安永|普华永道|德勤|毕马威|PwC|KPMG|Deloitte|Ernst\s*&\s*Young', '事务所', f.name)
    if t != o or newname != f.name:
        if newname != f.name:
            f.rename(f.with_name(newname)); f = f.with_name(newname)
        f.write_text(t, encoding='utf-8'); n += 1
print(f"二次脱敏完成：改写 {n} 个文件，映射 {len(m)} 家")
