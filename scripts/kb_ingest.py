#!/usr/bin/env python3
"""知识库入库（税务案例/报告）：抽取文本 → 脱敏 → 写带标签的条目。
用法: python3 scripts/kb_ingest.py <文件> [--type 方法论|筹划案] [--topic 出海税务] [--keep-amount]
输出: ~/.ng-platform/knowledge/tax-cases/<slug>.md（含 front-matter 标签）
说明: 仅本地；原文件不动、不删除。图片/图表文字不在文本抽取范围内（需 OCR，二期）。
"""
import os, re, sys, hashlib, datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.services.materials import extract_text

RULES = [
    (re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+'), '[邮箱]'),
    (re.compile(r'\b1[3-9]\d{9}\b'), '[手机号]'),
    (re.compile(r'\b\d{15,18}[\dXx]?\b'), '[证件号]'),
    (re.compile(r'\b(?:\d[ -]?){15,19}\b'), '[账号]'),
    (re.compile(r'(地址|住所)[：:]\s*\S+'), r'\1：[地址]'),
]

# ---------- 客户案脱敏（--client-case） ----------
_AMT = re.compile(r'([0-9][0-9,]*(?:\.[0-9]+)?)\s*(亿元|百万元|万元|千元|元)')
_WHITELIST_CTX = re.compile(r'(税率|比例|占|％|%,|年度|年|第\s*\d+\s*号|公告|财税|国税发|文号)')


def _magnitude(v: float) -> str:
    for t, n in ((1e8, '亿级'), (1e7, '千万级'), (1e6, '百万级'), (1e5, '十万级'), (1e4, '万级')):
        if v >= t:
            return n
    return ''
def mask_amounts(t: str) -> str:
    def rep(m):
        raw, unit = m.group(1), m.group(2)
        try:
            v = float(raw.replace(',', '')) * {'元': 1, '千元': 1e3, '万元': 1e4, '百万元': 1e6, '亿元': 1e8}[unit]
        except Exception:
            return '[金额]'
        mag = _magnitude(v)
        return f'[金额≈{mag}]' if mag else '[金额]'
    return _AMT.sub(rep, t)


def _company_of(stem: str) -> str:
    """从文件名推断客户公司名：优先含"公司/集团/银行/事务所"的片段，否则第一段。"""
    parts = [x.strip('（）() ') for x in re.split(r'[_\-\s]', stem) if x.strip()]
    for x in parts:
        if '公司' in x or re.search(r'(集团|银行|事务所|中心)$', x):
            return x
    return parts[0] if parts else stem


def _variants(name: str):
    """只按公司名结构生成变体，避免误伤通用词（曾把"科技"当简称）。

    变体：全名 / 逐年去公司后缀 / 品牌前缀(2..len) / 括号全称(品牌+地区+后缀) / 英文简称。
    """
    vs = {name}
    SUF = ('股份有限公司', '有限责任公司', '有限公司', '集团有限公司', '集团', '公司')
    core = name
    prev = None
    while core and core != prev:
        prev = core
        for suf in SUF:
            if core.endswith(suf):
                core = core[:-len(suf)]
        if len(core) >= 3:
            vs.add(core)
    brand = re.sub(r'[（(].*?[)）]', '', name).strip()
    b2 = brand
    prev = None
    while b2 and b2 != prev:
        prev = b2
        for suf in SUF:
            if b2.endswith(suf):
                b2 = b2[:-len(suf)]
    GENERIC = {'科技','技术','信息','网络','智能','电子','实业','投资','控股','集团','发展',
               '贸易','服务','咨询','管理','金融','银行','证券','基金','保险','地产','建设',
               '工程','传媒','文化','教育','医疗','健康','食品','农业','能源','环保','制造',
               '机械','汽车','物流','供应','销售','材料','生物','医药','化工','电力','水泥',
               '工业','新技术','国际','中国','中华','实业','有限'}
    GEO = {'北京','上海','深圳','广州','惠州','湖南','广西','重庆','昌江','天津','江苏','浙江',
           '福建','山东','河南','四川','湖北','广东','海南','横琴','香港','新加坡','云南','贵州'}
    for k in range(2, len(b2) + 1):          # 品牌前缀/尾字，排除地名与行业通用词
        pre, tail = b2[:k], b2[-k:]
        if pre not in GENERIC and pre not in GEO:
            vs.add(pre)
        if tail not in GENERIC and tail not in GEO:
            vs.add(tail)
    for inner in re.findall(r'[（(]([^）)]{2,})[)）]', name):   # 括号地区 → 组装全称变体
        for suf in SUF:
            vs.add(f"{brand}（{inner}）{suf}"); vs.add(f"{b2}（{inner}）{suf}")
        vs.add(f"{brand}（{inner}）"); vs.add(f"{b2}（{inner}）")
    for tok in re.findall(r'[A-Za-z][A-Za-z0-9&]{1,}', name):   # 英文简称 TCL/OPPO
        vs.add(tok)
    return sorted((v for v in vs if len(v) >= 2), key=len, reverse=True)

def client_mask(text: str, stem: str, map_file: Path):
    """公司名→代号（映射留本地，不入库）；金额量级化。返回 (文本, 代号)。"""
    import json
    m = json.loads(map_file.read_text(encoding='utf-8')) if map_file.exists() else {}
    name = _company_of(stem)
    code = next((c for c, n in m.items() if n == name), None)
    if code is None:
        code = f"公司{chr(65 + len(m)) if len(m) < 26 else len(m)+1}"
        m[code] = name
        map_file.parent.mkdir(parents=True, exist_ok=True)
        map_file.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding='utf-8')
    for v in _variants(name):
        text = text.replace(v, code)
    return mask_amounts(text), code


def _case_slug(stem: str, code: str, hid: str) -> str:
    date = re.search(r'(20\d{6})', stem)
    tail = (date.group(1) if date else '')
    return f"客户案-{code}-税务复核备忘录-{tail}-{hid}".strip('-')
def desensitize(t: str) -> str:
    for rx, rep in RULES:
        t = rx.sub(rep, t)
    return t

def slug(s: str) -> str:
    s = re.sub(r'[^\w一-龥]+', '-', s).strip('-')
    return s[:60] or 'entry'


def ocr_embedded(src: Path, cap: int = 20000) -> str:
    """对 Office 内嵌图片 / 独立图片做 Vision OCR（macOS，scripts/ocr）。失败静默。"""
    import shutil, subprocess, tempfile, zipfile
    ocr = Path(__file__).resolve().parent / 'ocr'
    if not ocr.exists():
        return ''
    ext = src.suffix.lower()
    imgs, tmp = [], None
    if ext in ('.pptx', '.docx', '.xlsx'):
        tmp = Path(tempfile.mkdtemp())
        inner = {'pptx': 'ppt/media/', 'docx': 'word/media/', 'xlsx': 'xl/media/'}[ext.lstrip('.')]
        with zipfile.ZipFile(src) as z:
            for n in z.namelist():
                if n.startswith(inner) and n.lower().endswith(('.png', '.jpg', '.jpeg')):
                    p = tmp / Path(n).name
                    p.write_bytes(z.read(n)); imgs.append(p)
    elif ext in ('.png', '.jpg', '.jpeg'):
        imgs = [src]
    if not imgs:
        return ''
    try:
        r = subprocess.run([str(ocr)] + [str(p) for p in imgs[:60]],
                           capture_output=True, text=True, timeout=180)
        txt = r.stdout.strip()
    except Exception:
        txt = ''
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    return txt[:cap]


def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    src = Path(sys.argv[1]); args = sys.argv[2:]
    def opt(k, d): return args[args.index(k)+1] if k in args else d
    typ, topic = opt('--type', '方法论'), opt('--topic', '出海税务')
    if src.is_dir():                      # 批量：吃整个文件夹的 Office/文本
        exts = ('.pptx', '.docx', '.xlsx', '.pdf', '.txt', '.md')
        files = sorted(f for f in src.rglob('*') if f.suffix.lower() in exts and not f.name.startswith('~$'))
        print(f"批量入库 {len(files)} 个文件 …")
        for f in files:
            import subprocess
            subprocess.run([sys.executable, __file__, str(f), '--type', typ, '--topic', topic]
                           + (['--no-ocr'] if '--no-ocr' in args else []))
        return
    if src.suffix.lower() == '.pdf':      # PDF → mac PDFKit；扫描件自动转 OCR
        import subprocess
        d = Path(__file__).resolve().parent
        try:
            text = subprocess.run([str(d / 'pdftext'), str(src)], capture_output=True, text=True, timeout=180).stdout
        except Exception:
            text = ''
        note = 'ok(pdf)'
        if len(text.strip()) < 50 and (d / 'pdfocr').exists():   # 无文字层 → 渲染 OCR
            try:
                text = subprocess.run([str(d / 'pdfocr'), str(src)], capture_output=True, text=True, timeout=600).stdout
                note = f'ok(pdf-ocr {len(text)}字)'
            except Exception:
                note = 'pdf: 抽取失败'
        elif not text.strip():
            note = 'pdf: 无文字(可能是扫描件，需 OCR)'
    else:
        text, note = extract_text(src)
    ocr_txt = '' if '--no-ocr' in args else ocr_embedded(src)
    if ocr_txt:
        text = text + '\n[图片OCR]\n' + ocr_txt
        note = f"{note}+ocr({len(ocr_txt)}字)"
    body = desensitize(text)
    hid = hashlib.sha256(src.read_bytes()).hexdigest()[:8]
    base = Path(os.environ.get('NG_HOME', Path.home()/'.ng-platform'))
    outdir = base/'knowledge'/'tax-cases'; outdir.mkdir(parents=True, exist_ok=True)
    code = None
    if '--client-case' in args:
        body, code = client_mask(body, src.stem, base/'_client_map.json')
        name_slug, title, sens = _case_slug(src.stem, code, hid), f"{code} 税务复核备忘录", \
            "high(客户案·公司名代号化/金额量级化·待人工复核；人名未系统处理)"
        source = f"{code}（原件名已隐）"
    else:
        name_slug, title, sens, source = f"{slug(src.stem)}-{hid}", src.stem, \
            "low(已脱敏·待人工复核)", src.name
    out = outdir/f"{name_slug}.md"
    fm = (f"---\ntitle: {title}\ntype: {typ}\ntopic: {topic}\n"
          f"source: {source}\nsource_sha256: {hid}\ningested: {datetime.date.today()}\n"
          f"extract: {note}\nsensitivity: {sens}\n---\n\n")
    out.write_text(fm + body, encoding='utf-8')
    print(f"入库: {out}\n  字符 {len(body)} | 抽取 {note}")

if __name__ == '__main__':
    main()
