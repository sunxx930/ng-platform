#!/usr/bin/env python3
"""全库二次脱敏：用本地映射表把所有客户名（含英文简称/交叉提及）统一替换为代号。

注意：本脚本只掩码、不还原（单向不可逆）。因此首次改写前会自动把整库快照到
knowledge/.snapshots/rescrub-<时间戳>/，只保留最近 10 份。加 --no-snapshot 可跳过。
"""
import json, sys, re, shutil, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kb_ingest import _variants, desensitize, apply_code
base = Path(__import__('os').environ.get('NG_HOME', Path.home()/'.ng-platform'))
m = json.loads((base/'_client_map.json').read_text(encoding='utf-8'))
root = base/'knowledge'/'tax-cases'
snapdir = base/'knowledge'/'.snapshots'
_snapped = False


def snapshot_once():
    """第一次真正改写前快照整库（单向脱敏一旦写坏只能靠它回滚）。"""
    global _snapped
    if _snapped or '--no-snapshot' in sys.argv:
        return
    snapdir.mkdir(parents=True, exist_ok=True)
    dst = snapdir/f"rescrub-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copytree(root, dst, dirs_exist_ok=True)
    for old in sorted(snapdir.glob('rescrub-*'))[:-10]:      # 只留最近 10 份
        shutil.rmtree(old, ignore_errors=True)
    print(f"已快照改前版本 -> {dst}")
    _snapped = True


n = 0
for f in sorted(root.glob('*.md')):
    t = f.read_text(encoding='utf-8'); o = t
    t = desensitize(t)                      # 中介/事务所/联系方式等固定规则
    for code, name in m.items():
        t = apply_code(t, code, _variants(name))
    # 标题/来源也脱敏；必要时重命名文件
    lines = t.splitlines()
    for i, ln in enumerate(lines[:10]):
        if ln.startswith(('title:', 'source:')):
            lines[i] = ln.split(':',1)[0] + ': ' + desensitize(ln.split(':',1)[1].strip())
    t = '\n'.join(lines) + ('\n' if t.endswith('\n') else '')
    newname = re.sub(r'安永|普华永道|德勤|毕马威|PwC|KPMG|Deloitte|Ernst\s*&\s*Young', '事务所', f.name)
    if t != o or newname != f.name:
        snapshot_once()
        if newname != f.name:
            f.rename(f.with_name(newname)); f = f.with_name(newname)
        f.write_text(t, encoding='utf-8'); n += 1
print(f"二次脱敏完成：改写 {n} 个文件，映射 {len(m)} 家")
