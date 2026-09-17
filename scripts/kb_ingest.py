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
    (re.compile(r'[A-Za-z0-9._%+-]+\s*@\s*[A-Za-z0-9-]{2,}(?:\s*\.\s*[A-Za-z0-9-]+)*'), '[邮箱]'),  # 域名点常被 OCR 吞掉
    (re.compile(r'\b1[3-9]\d{9}\b'), '[手机号]'),
    (re.compile(r'\b\d{15,18}[\dXx]?\b'), '[证件号]'),
    (re.compile(r'\b(?:\d[ -]?){15,19}\b'), '[账号]'),
    (re.compile(r'(地址|住所)[：:]\s*\S+'), r'\1：[地址]'),
    # 正文里的中文地址（地址是强标识，能定位到具体公司）
    (re.compile(r'[一-龥]{2,10}(?:路|大道|街|巷|弄)[一-龥]{0,6}?\s*\d{1,5}\s*号[一-龥\d]{0,20}'), '[地址]'),
    # 只掩「路名+门牌号」这种确凿地址；放宽会误伤(曾把「上市路径」「中级人民法院」当地址)
    (re.compile(r'[一-龥]{2,12}(?:路|大道|街|巷|弄)[一-龥]{0,6}?\s*\d{1,5}\s*号[一-龥\d]{0,20}'), '[地址]'),
    (re.compile(r'[一-龥]{2,12}(?:大厦|广场|大楼)\s*\d*\s*(?:[A-Z座栋]|[一二三四五六七八九十]+栋)?\s*\d*\s*[楼层室]?[^\s，。；、）)]{0,12}'), '[地址]'),
    # 中介/事务所名（非客户，但同样不该留）
    (re.compile(r'安永华明会计师事务所（特殊普通合伙）|安永华明会计师事务所|安永华明|安永（中国）企业咨询有限公司|安永（中国）|安永'), '[事务所]'),
    (re.compile(r'普华永道|PwC|PricewaterhouseCoopers'), '[事务所]'),
    (re.compile(r'德勤华永|德勤|Deloitte'), '[事务所]'),
    (re.compile(r'毕马威|KPMG'), '[事务所]'),
    (re.compile(r'Ernst\s*&\s*Young', re.I), '[事务所]'),
    # 本所自有品牌（用户 2026-09-18 要求一并掩掉，与外部事务所同等待遇）
    (re.compile(r'壹诺家办|壹诺|FinTaxLega\s*l(?:研究院)?'), '[事务所]'),
    (re.compile(r'Wolters\s*Kluwer|威科先行'), '[数据库]'),
    (re.compile(r'(?:\[事务所\]\s*)?企业咨询\s*[（(]\s*中国\s*[)）]\s*有限(?:责任)?公司'), '[事务所]'),
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


_SUF = ('股份有限公司', '有限责任公司', '有限公司', '集团有限公司', '集团', '公司')

_GENERIC = {'科技','技术','信息','网络','智能','电子','实业','投资','控股','集团','发展',
            '贸易','服务','咨询','管理','金融','银行','证券','基金','保险','地产','建设',
            '工程','传媒','文化','教育','医疗','健康','食品','农业','能源','环保','制造',
            '机械','汽车','物流','供应','销售','材料','生物','医药','化工','电力','水泥',
            '工业','新技术','国际','中国','中华','有限','股份','责任'}

_GEO_WORDS = ('北京','上海','深圳','广州','惠州','湖南','广西','重庆','昌江','天津','江苏','浙江',
              '福建','山东','河南','四川','湖北','广东','海南','横琴','香港','新加坡','云南','贵州',
              '东莞','佛山','珠海','中山','南京','南昌','杭州','苏州','成都','武汉','西安','青岛',
              '厦门','大连','宁波','无锡','合肥','长沙','郑州','济南','福州','泉州','温州','绍兴')
_GEO_RX = re.compile(r'^(?:' + '|'.join(sorted(_GEO_WORDS, key=len, reverse=True)) +
                     r')(?:市|省|自治区|特别行政区|地区|新区|经济特区|自治州|县|区)?$')


def _strip_suffixes(s):
    """逐级去掉公司后缀，返回所有长度 ≥3 的中间形式（从长到短）。"""
    out, cur, prev = [], s, None
    while cur and cur != prev:
        prev = cur
        for suf in _SUF:
            if cur.endswith(suf):
                cur = cur[:-len(suf)]
        if len(cur) >= 3:
            out.append(cur)
    return out


def _core_variants(vs, s):
    """全名 / 去后缀核心 / 括号全称 / 英文整体（≥3，不切子串）。"""
    vs.add(s)
    brand = re.sub(r'[（(].*?[)）]', '', s).strip()
    for base in (s, brand):
        for c in _strip_suffixes(base):
            vs.add(c)
    core = _strip_suffixes(brand)
    core = core[-1] if core else brand
    for inner in re.findall(r'[（(]([^）)]{2,})[)）]', s):     # 括号地区 → 组装全称变体
        for suf in _SUF:
            vs.add(f"{brand}（{inner}）{suf}")
            vs.add(f"{core}（{inner}）{suf}")
        vs.add(f"{brand}（{inner}）")
    if re.search(r'[\u4e00-\u9fa5]', s):                   # 只有含中文的名字才抽内嵌英文品牌
        for tok in re.findall(r'[A-Za-z][A-Za-z0-9&]{2,}', s):
            vs.add(tok)
    # 纯英文名（如 CGN Mining Company Limited）整串已加入，绝不按词拆
    # —— 历史事故：拆词后 Company/Global/Energy 等普通英文词成了替换目标


def _prefix_variants(vs, s):
    """仅主名生成「品牌前缀」简称（≥3 字）。

    不生成词尾碎片：历史版本取 tail 曾把「车销售」「融服务」「信服」当简称，
    配合 str.replace 会把「机动车销售」「金融服务」「电信服务」等普通词改坏。
    """
    b = re.sub(r'[（(].*?[)）]', '', s).strip()
    st = _strip_suffixes(b)
    b = st[-1] if st else b
    if not re.fullmatch(r'[\u4e00-\u9fa5]+', b):
        return                                                # 非纯中文（英文名）不做前缀
    for k in range(3, len(b)):
        pre = b[:k]
        if pre in _GENERIC or _GEO_RX.match(pre):
            continue                                          # 通用词 / 省市地名 → 跳过
        if len(pre) <= 3 and pre[:2] in _GEO_WORDS:
            continue                                          # 地名+1字：如"深圳香"会误伤"深圳香港"
        vs.add(pre)


def _variants(name: str):
    parts = [p.strip() for p in name.split('/') if p.strip()]
    vs = set()
    for p in parts:
        _core_variants(vs, p)                                  # 每个别名只取整体，绝不切前缀/词尾
        # 历史教训：任何"自动生成简称"都会撞普通词（可持续→公司AU、车销售→公司A、深圳市→公司AF）。
        # 简称一律改为在映射表里显式写别名，例如 公司A=比亚迪汽车销售有限公司/比亚迪
    return sorted((v for v in vs if len(v) >= 2), key=len, reverse=True)




def mask_all(text: str, m: dict) -> str:
    """全局脱敏：所有代号的变体按长度从长到短统一替换。

    长名优先，避免短品牌名先吞掉长实体名（如 "中广核" 先把 "中广核华盛投资有限公司" 吃掉）。
    """
    pairs = [(v, c) for c, n in m.items() for v in _variants(n)]
    ascii_pairs = [(v, c) for v, c in pairs if re.fullmatch(r"[A-Za-z0-9&.'\- ]+", v)]
    cjk_pairs = [(v, c) for v, c in pairs if not re.fullmatch(r"[A-Za-z0-9&.'\- ]+", v)]
    for group in (ascii_pairs, cjk_pairs):          # 先英文后中文：中文换代号后紧跟的英文会被词边界挡住
        for v, code in sorted(group, key=lambda x: len(x[0]), reverse=True):
            if re.fullmatch(r"[A-Za-z0-9&.'\- ]+", v):
                # 空白容错：OCR 常把 "Meiya Shanghai" 排成 "Meiya  Shanghai"
                body = r'\s+'.join(re.escape(w) for w in v.split())
                text = re.sub(r'(?<![A-Za-z])' + body + r'(?![A-Za-z])', code, text)
            else:
                # 中文变体同样容忍 OCR 在字间插空格（如「高尔夫球 俱乐部」）
                text = re.sub(r'\s*'.join(re.escape(ch) for ch in v), code, text)
    return text

def apply_code(text: str, code: str, variants):
    """把客户名变体替换为代号。纯 ASCII 变体加词边界，避免 NLABB→NL公司AC 这类切词。"""
    for v in variants:                                        # variants 已按长度降序
        if re.fullmatch(r'[A-Za-z0-9&.\'\- ]+', v):
            text = re.sub(r'(?<![A-Za-z])' + re.escape(v) + r'(?![A-Za-z])', code, text)
        else:
            text = text.replace(v, code)
    return text

def client_mask(text: str, stem: str, map_file: Path, ctype: str = ''):
    """公司名→代号（映射留本地，不入库）；金额量级化。返回 (文本, 代号)。"""
    import json
    m = json.loads(map_file.read_text(encoding='utf-8')) if map_file.exists() else {}
    raw = stem.strip()
    name = raw
    code = next((c for c, n in m.items() if n == raw), None)   # 显式 --client-name 优先精确匹配
    if code is None:
        name = _company_of(raw)                                # 否则回退：按文件名推断客户名
        code = next((c for c, n in m.items() if n == name), None)
    if code is None:
        if ctype in ('person','company'):
            prefix = '个人' if ctype=='person' else '公司'
        else:
            prefix = '个人' if (len(name)==3 and re.fullmatch(r'[\u4e00-\u9fa5]+', name) and name[0] in _SURNAME) else '公司'
        same = sum(1 for k in m if k.startswith(prefix))
        def _suf(n):
            out=''
            while True:
                out = chr(65+n%26) + out; n = n//26 - 1
                if n < 0: return out
        code = f"{prefix}{_suf(same)}"
        m[code] = name
        map_file.parent.mkdir(parents=True, exist_ok=True)
        map_file.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding='utf-8')
    text = mask_all(text, m)                                   # 全局按长度优先替换
    return mask_amounts(text), code


def _case_slug(stem: str, code: str, hid: str) -> str:
    date = re.search(r'(20\d{6})', stem)
    tail = (date.group(1) if date else '')
    return f"客户案-{code}-税务复核备忘录-{tail}-{hid}".strip('-')
_SURNAME = set("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻潘葛范彭鲁韦马苗凤花方俞任袁柳唐罗薛伍余米贝姚孟顾尹江钟卢汪石戴崔贾龚程陆裴宋庞熊纪舒屈项祝梁杜阮蓝季贾路娄危童颜郭梅盛林钟徐邱骆高夏蔡田樊胡凌霍虞万支柯管卢莫房缪干解应宗丁宣邓郁单杭洪包诸左石崔吉龚邢滑裴陸荣翁荀羊惠甄曲家封芮储靳汲邴糜松井段富巫乌焦巴弓牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴胥能苍双闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍卻璩桑桂濮牛寿通边扈燕冀郏浦尚农温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文寇广禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公")
_JOB = r'(先生|女士|小姐|经理|总监|董事|监事|主管|法人代表|法定代表人|财务负责人|项目负责人|负责人|联系人|经办人|编制人|复核人|审核人|签字人|授权代表|股东)'
_FIELD = r'(姓名|联系人|法定代表人|负责人|经办人|编制人|复核人|审核人|签字|授权代表)'
_BAD = ("公司", "单位", "该", "本", "贵", "甲方", "乙方", "事务所", "有限", "集团", "中心", "银行", "部门")

def _mask_names(t: str) -> str:
    """仅姓名字段上下文脱敏：签字/联系人/法定代表人…：张三 → [姓名]。

    保守：要求首字为常见姓氏、长度 2–4、且不含公司/机构类停用字，避免误伤"分公司/公司"等。
    """
    STOP = set("公司事务所银行集团中心部门有限经办负责管理处科研院校会局厂店区市县省大中")
    FIELDS = r'(姓名|联系人|人员|参会人员|参会人|参与人|出席人|与会人员|法定代表人|法人代表|负责人|经办人|编制人|复核人|审核人|签字|授权代表|股东|董事|监事|执行董事|总经理|副总经理|财务总监|董秘|合伙人)'

    def ok(n: str) -> bool:
        return 2 <= len(n) <= 4 and n[0] in _SURNAME and not any(c in STOP for c in n[1:])

    def rep(m):
        nm = m.group(2)
        return f'{m.group(1)}：[姓名]' if ok(nm) else m.group(0)
    return re.sub(rf'{FIELDS}\s*[：:]?\s*([\u4e00-\u9fa5]{{2,4}})', rep, t)


def _watch_names() -> list:
    import json
    f = Path(os.environ.get('NG_HOME', Path.home()/'.ng-platform'))/'_person_watch.json'
    try:
        return json.loads(f.read_text(encoding='utf-8')) if f.exists() else []
    except Exception:
        return []


# 客户名册：带标签的长顿号枚举（研讨会课件常整段罗列客户名 = 公开客户清单）
_ROSTER = re.compile(r'((?:企业)?客户|金融机构|合作伙伴|服务的客户)\s*[:：]\s*([^。；\n]{20,})')


def _roster_sub(m):
    return f'{m.group(1)}：[客户名单]' if m.group(2).count('、') >= 4 else m.group(0)


def desensitize(t: str) -> str:
    for rx, rep in RULES:
        t = rx.sub(rep, t)
    t = _ROSTER.sub(_roster_sub, t)          # 客户名册整段替换
    for n in sorted(_watch_names(), key=len, reverse=True):   # 长名优先
        if ' ' in n.strip():
            t = re.sub(r'\s+'.join(re.escape(w) for w in n.split()), '[姓名]', t)  # 空格容错
        else:
            t = t.replace(n, '[姓名]')
    t = _mask_names(t)
    return re.sub(r'(\[(?:事务所|数据库|姓名)\])+', r'\1', t)   # 合并重复占位

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
    cname = opt('--client-name', '')
    ctype = opt('--client-type', '')   # person|company 可强制
    if src.is_dir():                      # 批量：吃整个文件夹的 Office/文本
        exts = ('.pptx', '.docx', '.doc', '.xlsx', '.pdf', '.txt', '.md')
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
    elif src.suffix.lower() == '.doc':    # 老版 Word → mac textutil
        import subprocess
        try:
            text = subprocess.run(['textutil', '-convert', 'txt', '-stdout', str(src)],
                                  capture_output=True, text=True, timeout=120).stdout
        except Exception:
            text = ''
        note = 'ok(doc)' if text.strip() else 'doc: 抽取失败'
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
        body, code = client_mask(body, cname or src.stem, base/'_client_map.json', ctype)
        kind = slug(topic) or '资料'
        name_slug = f"客户案-{code}-{kind}-{hid}"
        title = f"{code} {topic or '客户资料'}"
        sens = "high(客户案·公司名代号化/金额量级化·待人工复核；人名未系统处理)"
        source = f"{code}（原件名已隐）"
    else:
        title_ = opt('--title', '') or src.stem          # 文件名笼统时用 --title 指定
        name_slug, title, sens, source = f"{slug(title_)}-{hid}", title_, \
            "low(已脱敏·待人工复核)", src.name
    out = outdir/f"{name_slug}.md"
    fm = (f"---\ntitle: {title}\ntype: {typ}\ntopic: {topic}\n"
          f"source: {source}\nsource_sha256: {hid}\ningested: {datetime.date.today()}\n"
          f"extract: {note}\nsensitivity: {sens}\n---\n\n")
    out.write_text(fm + body, encoding='utf-8')
    print(f"入库: {out}\n  字符 {len(body)} | 抽取 {note}")

if __name__ == '__main__':
    main()
