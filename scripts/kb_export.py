#!/usr/bin/env python3
"""交付版导出：把工作版知识库泛化成「不可逆向」的客户可见版本。

用法:
  python3 scripts/kb_export.py --out <目录> [--scope all|general|client] [--audit]

与工作版(scripts/kb_ingest.py 产出)的区别——工作版只在本地用，交付版要能交出去：

  1) 剥指纹   : 删 source / source_sha256 / ingested / extract / sensitivity
                (source_sha256 是原文件的 sha256，拿到原件即可比对确认，必须去掉)
  2) 换文件名 : 用本次导出的随机盐生成匿名 ID，文件名不再携带任何哈希
  3) 重编代号 : 公司XX/个人X → 实体A/B/C…，本库内一致、**跨库随机打乱**，
                使两份交付库无法通过代号对齐
  4) 泛化事实 : 行业词→[行业]、非常见属地→[属地]、精确比例→档位、日期→年
  5) 英文实体 : 按模式匹配(专有名词 + Ltd/Limited/Holdings/Energy/Power…)→[实体]，
                不依赖名单，未见过的新名字也覆盖得住
  6) --audit  : 只做「反推测试」，列出每条残留的辨识线索，供人工判断

注意: 映射表(_client_map.json)永不出库；本脚本只读工作版，不改动它。
"""
import argparse, json, os, random, re, shutil, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# ---------- 泛化词表（按需增补）----------
INDUSTRY = {          # 行业/板块名 → [行业]
    '核燃料': 1, '核电': 1, '铀业': 1, '铀矿': 1, '风电': 1, '光伏': 1, '太阳能': 1,
    '水电': 1, '渔业': 1, '远洋': 1, '房地产': 1, '房屋开发': 1, '动力电池': 1, '电池': 1,
    '矿业': 1, '水泥': 1, '乳业': 1, '美妆': 1, '医疗器械': 1, '医疗美容': 1, '新能源': 1,
}
GEO_RARE = [          # 非常见属地 → [属地]（香港/开曼/BVI/百慕大等离岸地属通用，保留）
    '哈萨克斯坦', '乌兹别克斯坦', '加蓬', '利比里亚', '苏里南', '毛里求斯', '纳米比亚',
    '埃及', '孟加拉', '巴基斯坦', '塞内加尔', '肯尼亚', '南非', '爱尔兰', '丹麦',
    '德国', '荷兰', '卢森堡', '瑞士', '法国', '澳大利亚', '马来西亚',
]
# 保留的通用离岸地（出现频率极高、辨识度低，删掉会损伤税务知识本身）
GEO_KEEP = ('香港', '开曼', 'BVI', '英属维尔京', '百慕大', '根西', '新加坡', '英国')

# 英文实体模式：专有名词(1-5 个词) + 公司后缀 → [实体]
EN_SUFFIX = (r'Ltd\.?|Limited|Inc\.?|Incorporated|Corp\.?|Corporation|Company|Co\.?|'
             r'Holdings?|Group|Capital|Partners?|LLC|PLC|GmbH|B\.?V\.?|N\.?V\.?|'
             r'S\.?A\.?|Pte\.?\s*Ltd\.?|L\.?P\.?|LLP|Sdn\.?\s*Bhd\.?')
EN_ENTITY = re.compile(
    r'\b([A-Z][A-Za-z&.\'-]+(?:\s+[A-Z][A-Za-z&.\'-]+){0,4})'   # 专有名词序列
    r'(?:\s*\([A-Za-z]{1,12}\))?'                             # 可选的 (BVI)/(HK)/(M) 之类
    r'(?:[\s,]*\b(?:GP|LP|LLP|GmbH|B\.?V\.?|N\.?V\.?|S\.?A\.?|Pte\.?\s*Ltd\.?|Sdn\.?\s*Bhd\.?)\b)?'
    r'[\s,]*\b(' + EN_SUFFIX + r')\b\.?\)?')
# 纯中国公司名（工作版应已掩掉，交付版兜底）
CN_ENTITY = re.compile(r'[一-龥]{2,14}(?:有限公司|股份有限公司|有限责任公司|合伙企业|集团有限公司)')

CODE = re.compile(r'公司[A-Z]{1,2}|个人[A-Z]')
AMOUNT = re.compile(r'\[金额≈([^\]]*)\]')


def _label(n: int) -> str:
    out = ''
    while True:
        out = chr(65 + n % 26) + out
        n = n // 26 - 1
        if n < 0:
            return out


def build_relabel(texts, seed):
    """本次导出的 代号→中性标签 映射：本库内一致，跨库随机打乱。"""
    seen = []
    for t in texts:
        for c in CODE.findall(t):
            if c not in seen:
                seen.append(c)
    labels = list(range(len(seen)))
    random.Random(seed).shuffle(labels)
    return {c: f'实体{_label(i)}' for c, i in zip(seen, labels)}


def generalize(text, relabel):
    # 1) 代号重编
    for c, lab in relabel.items():
        text = text.replace(c, lab)
    # 2) 中国公司名兜底（工作版漏掩的）
    text = CN_ENTITY.sub('[实体]', text)
    # 3) 英文实体模式
    text = EN_ENTITY.sub('[实体]', text)
    # 3b) 收尾：清掉 [实体] 后面残留的公司后缀碎片
    text = re.sub(r'(\[实体\])\s*,?\s*(?:Limited|Ltd\.?|Company|Corporation|Holdings?)\b\.?',
                  r'\1', text)
    # 4) 行业
    for k in sorted(INDUSTRY, key=len, reverse=True):
        text = text.replace(k, '[行业]')
    # 5) 非常见属地（避开保留词内部）
    for g in sorted(GEO_RARE, key=len, reverse=True):
        text = text.replace(g, '[属地]')
    # 6) 精确比例 → 档位
    def ratio(m):
        v = float(m.group(1))
        return '[全资]' if v >= 99.5 else ('[控股]' if v >= 50 else '[参股]')
    text = re.sub(r'(\d{1,3}(?:\.\d+)?)\s*%', ratio, text)
    # 7) 日期粗化到年
    text = re.sub(r'(20\d\d)\s*年\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?', r'\1年', text)
    return text


def parse(raw):
    m = re.match(r'---\n(.*?)\n---\n\n(.*)', raw, re.S)
    if not m:
        return {}, raw
    fm, body = m.groups()
    meta = dict(re.findall(r'^(\w+):\s*(.*)$', fm, re.M))
    return meta, body


def audit(text):
    """反推测试：列出正文里仍可辨识的线索。"""
    clues = []
    for pat, tag in ((r'[一-龥]{2,12}(?:有限公司|股份有限公司|合伙企业)', '中文公司名'),
                     (r'\b[A-Z][A-Za-z&.\'-]*(?:\s+[A-Z][A-Za-z&.\'-]+){0,3}\b', '英文大写词组'),
                     (r'\d{1,3}(?:\.\d+)?\s*%', '精确比例'),
                     (r'20\d\d\s*年\s*\d{1,2}\s*月', '精确月份')):
        for m in list(re.finditer(pat, text))[:40]:
            s = m.group().strip()
            if len(s) > 3 and s not in GEO_KEEP:
                clues.append((tag, s))
    return clues


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', help='交付版输出目录（--audit 时不需要）')
    ap.add_argument('--scope', default='all', choices=['all', 'general', 'client'])
    ap.add_argument('--audit', action='store_true', help='只做反推测试，不写文件')
    ap.add_argument('--seed', type=int, default=0, help='代号打乱种子(默认按时间)')
    a = ap.parse_args()

    base = Path(os.environ.get('NG_HOME', Path.home() / '.ng-platform'))
    root = base / 'knowledge' / 'tax-cases'
    entries = []
    for f in sorted(root.glob('*.md')):
        meta, body = parse(f.read_text(encoding='utf-8'))
        is_client = meta.get('type') == '筹划案' and f.name.startswith('客户案-')
        if a.scope == 'client' and not is_client:
            continue
        if a.scope == 'general' and is_client:
            continue
        entries.append((f, meta, body))

    seed = a.seed or int(time.time())
    relabel = build_relabel([b for _, _, b in entries], seed)

    if a.audit:
        print(f"反推测试（{len(entries)} 条，seed={seed}）")
        for f, meta, body in entries:
            g = generalize(body, relabel)
            clues = audit(g)
            if clues:
                print(f"\n--- {meta.get('topic','')}  [{f.name}]")
                seen = set()
                for tag, s in clues:
                    if s in seen:
                        continue
                    seen.add(s)
                    print(f'    {tag}: {s}')
        return

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    ids = {}
    for f, meta, body in entries:
        g = generalize(body, relabel)
        # 匿名 ID：本次导出的随机盐，不复用工作版文件名里的哈希
        i = len(ids)
        fid = f'{seed % 100000:05d}-{i:04d}'
        ids[f.name] = fid
        head = (f"---\ntitle: {meta.get('topic','资料')}\n"
                f"type: {meta.get('type','')}\ntopic: {meta.get('topic','')}\n---\n\n")
        (out / f'{fid}.md').write_text(head + g, encoding='utf-8')
    # 索引（不含任何来源/时间/哈希）
    idx = ['# 知识库索引\n']
    for f, meta, body in entries:
        idx.append(f"- `{ids[f.name]}.md` — [{meta.get('type','')}] {meta.get('topic','')}")
    (out / '_index.md').write_text('\n'.join(idx) + '\n', encoding='utf-8')
    print(f"已导出 {len(entries)} 条 -> {out}")
    print(f"  代号重编: {len(relabel)} 个 -> 实体A…; seed={seed}（不随库交付）")
    print(f"  已剥离: source / source_sha256 / ingested / extract / sensitivity")
    print(f"  映射表未随库输出 ✓")


if __name__ == '__main__':
    main()
