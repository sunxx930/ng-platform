#!/usr/bin/env python3
"""两阶段检索：纯语义召回 top-K → 本地小模型(LLM)精排 → top-N。

为什么需要：
  纯语义对"表述不同但同义"很强，但会被表面相似的文档骗
  （问"境外公司提供服务要不要代扣代缴"，它会先给讲"境外派遣"的）。
  混合权重调不出一个对所有问题都最优的配比。
  两阶段是标准解法：召回保不漏，重排保准确。

用法: python3 kb_rerank.py "问题" [--recall 20] [--top 5]
"""
import json, re, sqlite3, sys, urllib.request

DB = '/tmp/kb_semantic.db'
OLLAMA = 'http://127.0.0.1:11434'
EMB = 'bge-m3'
RERANK = 'qwen3:1.7b'


def _post(path, payload, timeout=180):
    req = urllib.request.Request(f'{OLLAMA}{path}',
                                 data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def embed(texts):
    return _post('/api/embed', {'model': EMB, 'input': texts})['embeddings']


def cos(a, b):
    s = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** .5
    nb = sum(y * y for y in b) ** .5
    return s / (na * nb) if na and nb else 0


def recall(con, q, k=20):
    rows = con.execute('SELECT path,title,text,v FROM vec').fetchall()
    qv = embed([q])[0]
    sc = sorted(((cos(qv, json.loads(r[3])), r) for r in rows), key=lambda x: -x[0])
    return sc[:k]


def llm_rerank(q, cands, topn=5):
    """一次调用给所有候选打分（不是候选数那么多次）。"""
    listing = '\n'.join(f'[{i}] {c[1][2][:110].replace(chr(10), " ")}' for i, c in enumerate(cands))
    prompt = (
        '你是税务检索助手。下面是【问题】和若干【候选资料片段】。\n'
        '请给每个片段打 0-10 分：10=直接回答了问题；7-9=高度相关；4-6=沾边；0-3=无关。\n'
        '只输出 JSON 数组，形如 [{"i":0,"s":8},{"i":1,"s":2}]，不要任何解释或多余文字。\n\n'
        f'【问题】{q}\n\n【候选】\n{listing}\n/no_think')
    out = _post('/api/generate', {
        'model': RERANK, 'prompt': prompt, 'stream': False,
        'options': {'temperature': 0, 'num_predict': 1200}}).get('response', '')
    scores = {}
    m = re.search(r'\[.*?\]', out, re.S)                      # 格式1：JSON
    if m:
        try:
            for it in json.loads(m.group(0)):
                scores[int(it['i'])] = float(it.get('s', 0))
        except Exception:
            pass
    if not scores:                                        # 格式2：Markdown 列表「**[3]** 8」
        for i, sc in re.findall(r'\[?(\d+)\]?\D{0,6}?(\d{1,2})(?:\s|$)', out):
            if i not in scores:
                scores[int(i)] = float(sc)
    if not scores:                      # 模型没按格式输出 → 退回原序
        return [(i, c, None) for i, c in enumerate(cands[:topn])]
    order = sorted(scores.items(), key=lambda kv: -kv[1])[:topn]
    return [(i, cands[i], s) for i, s in order if i < len(cands)]


def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    q = sys.argv[1]
    recall_n = int(sys.argv[sys.argv.index('--recall') + 1]) if '--recall' in sys.argv else 12
    top = int(sys.argv[sys.argv.index('--top') + 1]) if '--top' in sys.argv else 5

    con = sqlite3.connect(DB)
    cands = recall(con, q, recall_n)
    print(f'\n查询: {q}')
    print(f'召回 {len(cands)} 块 → 重排取前 {top}\n' + '─' * 74)
    print('【重排后】')
    for i, (sc, r), s in llm_rerank(q, cands, top):
        mark = f'LLM={s:.0f}' if s is not None else 'LLM=?'
        print(f'  {mark:7} 语义{sc:.3f}  {r[1][:30]:32}| {r[2][:56].replace(chr(10), " ")}')
    print('\n【重排前的原顺序（纯语义）】')
    for sc, r in cands[:top]:
        print(f'           语义{sc:.3f}  {r[1][:30]:32}| {r[2][:56].replace(chr(10), " ")}')


if __name__ == '__main__':
    main()
