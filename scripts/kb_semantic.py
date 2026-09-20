#!/usr/bin/env python3
"""语义检索原型：用本地 ollama(bge-m3) 给知识库做向量 + 语义搜索。

用途：在接入 NG 的 pgvector 之前，先验证"语义检索效果到底行不行"。
     索引落在本地 sqlite，不动 NG 的库。

用法:
  python3 kb_semantic.py index            # 建索引（首次/内容变了重跑）
  python3 kb_semantic.py search "问题"     # 语义检索
  python3 kb_semantic.py compare "问题"    # 语义 vs 关键词 对比
"""
import json, os, re, sqlite3, sys, urllib.request
from pathlib import Path

KB = Path.home() / '.ng-platform' / 'knowledge' / 'tax-cases'
IDX = Path('/tmp/kb_semantic.db')
OLLAMA = 'http://127.0.0.1:11434'
MODEL = 'bge-m3'


def embed(texts: list[str]) -> list[list[float]]:
    """批量取向量（ollama /api/embed 支持批量）。"""
    req = urllib.request.Request(
        f'{OLLAMA}/api/embed',
        data=json.dumps({'model': MODEL, 'input': texts}).encode(),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())['embeddings']


def chunks_of(path: Path) -> list[tuple[str, str]]:
    """切块：法规按「条/章」，案例按段落（600 字上限）。返回 [(小标题, 文本)]。"""
    raw = path.read_text(encoding='utf-8')
    m = re.match(r'---\n(.*?)\n---\n\n(.*)', raw, re.S)
    meta, body = (m.groups() if m else ('', raw))
    title = (re.search(r'^title:\s*(.+)$', meta, re.M) or [None, path.stem])[1]

    # 先按「第X条」切（税法的主要语义单元）
    parts = re.split(r'(?=第[一二三四五六七八九十百零\d]+条)', body)
    if len(parts) < 2:                       # 没有条文结构 → 按段落
        parts = [p for p in re.split(r'\n\s*\n', body) if p.strip()]
    out, buf = [], ''
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(p) > 900:                     # 过长再硬切
            for i in range(0, len(p), 800):
                out.append((title, p[i:i + 800]))
            continue
        if len(buf) + len(p) < 600:
            buf = (buf + '\n' + p).strip()
        else:
            if buf:
                out.append((title, buf))
            buf = p
    if buf:
        out.append((title, buf))
    return out


def cmd_index():
    con = sqlite3.connect(IDX)
    con.execute('CREATE TABLE IF NOT EXISTS vec (path TEXT, title TEXT, seq INT, text TEXT, v TEXT)')
    con.execute('DELETE FROM vec')
    files = sorted(KB.glob('*.md'))
    total = 0
    for f in files:
        ch = chunks_of(f)
        if not ch:
            continue
        embs = embed([c[1] for c in ch])
        for i, ((title, text), v) in enumerate(zip(ch, embs)):
            con.execute('INSERT INTO vec VALUES (?,?,?,?,?)',
                        (f.name, title, i, text, json.dumps(v)))
        total += len(ch)
        print(f'  {f.name[:50]:52} {len(ch):3} 块')
    con.commit()
    dim = len(json.loads(con.execute('SELECT v FROM vec LIMIT 1').fetchone()[0]))
    print(f'\n  索引完成: {len(files)} 份文档 → {total} 块, 维度 {dim}')
    con.close()


def cos(a, b):
    s = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** .5
    nb = sum(y * y for y in b) ** .5
    return s / (na * nb) if na and nb else 0


def cmd_search(q, topn=8, quiet=False):
    con = sqlite3.connect(IDX)
    rows = con.execute('SELECT path, title, text, v FROM vec').fetchall()
    qv = embed([q])[0]
    scored = sorted(((cos(qv, json.loads(r[3])), r) for r in rows), key=lambda x: -x[0])
    if not quiet:
        print(f'\n查询: {q}\n' + '─' * 70)
    for sc, (path, title, text, _) in scored[:topn]:
        if not quiet:
            print(f'[{sc:.3f}] {title[:40]}')
            print(f'        {text[:140].replace(chr(10), " ")}...\n')
    return scored[:topn]


def cmd_compare(q):
    """语义 vs 关键词：看语义是否找到了关键词找不到的东西。"""
    con = sqlite3.connect(IDX)
    rows = con.execute('SELECT path, title, text, v FROM vec').fetchall()
    kw = [r for r in rows if any(t in r[2] for t in q.split())]
    print(f'\n查询: {q}')
    print(f'\n【关键词】命中 {len(kw)} 块' + ('' if kw else '  ← 一个都没有！'))
    for r in kw[:3]:
        print(f'   {r[1][:36]} | {r[2][:90]}...')
    sem = cmd_search(q, topn=5, quiet=True)
    print(f'\n【语义】top 5')
    for sc, (path, title, text, _) in sem:
        print(f'   [{sc:.3f}] {title[:36]} | {text[:90]}...')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    if sys.argv[1] == 'index':
        cmd_index()
    elif sys.argv[1] == 'search':
        cmd_search(' '.join(sys.argv[2:]))
    elif sys.argv[1] == 'compare':
        cmd_compare(' '.join(sys.argv[2:]))
    else:
        print(__doc__)
