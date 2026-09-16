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
    text, note = extract_text(src)
    ocr_txt = '' if '--no-ocr' in args else ocr_embedded(src)
    if ocr_txt:
        text = text + '\n[图片OCR]\n' + ocr_txt
        note = f"{note}+ocr({len(ocr_txt)}字)"
    body = desensitize(text)
    hid = hashlib.sha256(src.read_bytes()).hexdigest()[:8]
    outdir = Path(os.environ.get('NG_HOME', Path.home()/'.ng-platform'))/'knowledge'/'tax-cases'
    outdir.mkdir(parents=True, exist_ok=True)
    out = outdir/f"{slug(src.stem)}-{hid}.md"
    fm = (f"---\ntitle: {src.stem}\ntype: {typ}\ntopic: {topic}\n"
          f"source: {src.name}\nsource_sha256: {hid}\ningested: {datetime.date.today()}\n"
          f"extract: {note}\nsensitivity: low(已脱敏·待人工复核)\n---\n\n")
    out.write_text(fm + body, encoding='utf-8')
    print(f"入库: {out}\n  字符 {len(body)} | 抽取 {note}")

if __name__ == '__main__':
    main()
