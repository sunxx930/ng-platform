"""交付物 Office 自动生成（v1.2.1，纯 stdlib，无第三方依赖）。

BuiltinAgent 产出 Markdown 后，平台把它同源生成 .docx/.xlsx/.pptx 存进沙箱，
交付事件带 files 映射，前端可下载；PDF 由前端"导出 PDF"(浏览器打印) 承接。

- docx：按 md 行拆段落（标题/列表近似加粗）
- xlsx：若 md 含 Markdown 表格 → 每个表格一 sheet；否则内容行进一列
- pptx：标题行为 slide 标题，其余为要点

注意：本生成器产出结构合法（zip+xml 可解析）；最终是否被 Office/Keynote
完全接受请在真实机器上开一次验证（离线无法代验）。
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

from app.storage.artifacts import artifacts_base

MIME = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _split_md(text: str):
    lines = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        lines.append(s)
    return lines


# ---------------- docx ----------------
def build_docx(md: str, title: str = "") -> bytes:
    body = []
    if title:
        body.append(f"<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>{_esc(title)}</w:t></w:r></w:p>")
    for ln in _split_md(md):
        t = re.sub(r"[*_`#]", "", ln)
        if ln.startswith("#"):
            t = re.sub(r"^#+\s*", "", ln)
            body.append(f"<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>{_esc(t)}</w:t></w:r></w:p>")
        else:
            body.append(f"<w:p><w:r><w:t>{_esc(t)}</w:t></w:r></w:p>")
    xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           "<w:body>" + "".join(body) + "</w:body></w:document>")
    cts = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
           '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
           '<Default Extension="xml" ContentType="application/xml"/>'
           '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
           "</Types>")
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>")
    return _zip({"word/document.xml": xml, "[Content_Types].xml": cts,
                 "_rels/.rels": rels})


# ---------------- xlsx ----------------
def _md_tables(md: str):
    tbls, cur = [], None
    for ln in md.splitlines():
        s = ln.strip()
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if set(cells) <= {"---", ""} or all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells):
                continue
            if cur is None:
                cur = [cells]
            else:
                cur.append(cells)
        else:
            if cur:
                tbls.append(cur)
                cur = None
    if cur:
        tbls.append(cur)
    return tbls


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def build_xlsx(md: str, title: str = "") -> bytes:
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    tables = _md_tables(md)
    if not tables:
        tables = [[["行", "内容"]] + [[str(i), ln] for i, ln in enumerate(_split_md(md), 1)]]
    sheets_xml, names = [], []
    for i, t in enumerate(tables):
        names.append(f"Sheet{i + 1}")
        rws = []
        for k, row in enumerate(t[:200], 1):
            cells = "".join(
                f'<c r="{_col(j)}{k}" t="inlineStr"><is><t>{_esc(c or "")}</t></is></c>'
                for j, c in enumerate(row))
            rws.append(f'<row r="{k}">{cells}</row>')
        sheets_xml.append(
            f'<worksheet xmlns="{ns}"><sheetData>{"".join(rws)}</sheetData></worksheet>')
    sheets_tag = "".join(
        f'<sheet name="{_esc(n)}" sheetId="{i}" r:id="rId{i}"/>' for i, n in enumerate(names, 1))
    wb = (f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          f"<sheets>{sheets_tag}</sheets></workbook>")
    wbrels = "".join(
        f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, len(sheets_xml) + 1))
    cts = ('<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
           '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
           '<Default Extension="xml" ContentType="application/xml"/>'
           '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' +
           "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                   for i in range(1, len(sheets_xml) + 1)) + "</Types>")
    files = {"[Content_Types].xml": cts,
             "_rels/.rels": _rels_office("xl/workbook.xml"),
             "xl/workbook.xml": wb,
             "xl/_rels/workbook.xml.rels":
                 f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{wbrels}</Relationships>'}
    for i, s in enumerate(sheets_xml, 1):
        files[f"xl/worksheets/sheet{i}.xml"] = s
    return _zip(files)


def _col(j):
    s = ""
    j += 1
    while j:
        j, r = divmod(j - 1, 26)
        s = chr(65 + r) + s
    return s


# ---------------- pptx ----------------
def build_pptx(md: str, title: str = "") -> bytes:
    """极简 pptx：标题行 → 每页标题，其余 → 该页要点。"""
    slides = []
    cur_title, cur_bullets = (title or "NG AI Platform 交付"), []
    for ln in _split_md(md):
        if ln.startswith("#"):
            if cur_title and (cur_bullets or cur_title != title):
                slides.append((cur_title, cur_bullets))
            cur_title = re.sub(r"^#+\s*", "", ln)
            cur_bullets = []
        else:
            cur_bullets.append(re.sub(r"[*_`]", "", ln))
    if cur_title and (cur_bullets or cur_title != title):
        slides.append((cur_title, cur_bullets))
    if not slides:
        slides = [(title or "交付", [])]
    files = {}
    shapes_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    p_ns = "http://schemas.openxmlformats.org/presentationml/2006/main"
    r_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    for i, (t, bullets) in enumerate(slides, 1):
        body = [f'<p:sp><p:spPr/><p:txBody><a:bodyPr/><a:p><a:r><a:t>{_esc(t)}</a:t></a:r></a:p></p:txBody></p:sp>']
        for b in bullets[:8]:
            body.append(f'<p:sp><p:spPr/><p:txBody><a:bodyPr/><a:p><a:r><a:t>{_esc(b[:120])}</a:t></a:r></a:p></p:txBody></p:sp>')
        xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               f'<p:sld xmlns:a="{shapes_ns}" xmlns:r="{r_ns}" xmlns:p="{p_ns}">'
               f"<p:cSld><p:spTree>"
               f'<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
               + "".join(body) + "</p:spTree></p:cSld></p:sld>")
        files[f"ppt/slides/slide{i}.xml"] = xml
        files[f"ppt/slides/_rels/slide{i}.xml.rels"] = (
            f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>')
    pres_rels = "".join(
        f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{i}.xml"/>'
        for i in range(1, len(slides) + 1))
    pres = ('<p:presentation xmlns:a="' + shapes_ns + '" xmlns:r="' + r_ns + '" xmlns:p="' + p_ns + '">'
            + '<p:sldIdLst>'
            + ''.join('<p:sldId id="' + str(256 + i) + '" r:id="rId' + str(i) + '"/>'
                      for i in range(1, len(slides) + 1))
            + '</p:sldIdLst><p:sldSz cx="9144000" cy="6858000"/></p:presentation>')
    files["ppt/presentation.xml"] = pres
    files["ppt/_rels/presentation.xml.rels"] = (
        f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{pres_rels}</Relationships>')
    cts = ('<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
           '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
           '<Default Extension="xml" ContentType="application/xml"/>'
           '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>' +
           "".join(f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
                   for i in range(1, len(slides) + 1)) + "</Types>")
    files["[Content_Types].xml"] = cts
    files["_rels/.rels"] = _rels_office("ppt/presentation.xml")
    return _zip(files)


def _rels_office(target: str) -> str:
    return (f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="{target}"/>'
            "</Relationships>")


def _zip(files: dict) -> bytes:
    import io
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return bio.getvalue()


def generate_deliverable_files(project_id: str, task_id: str, md_ref: str, title: str = ""):
    """给定 md 交付 ref，生成 docx/xlsx/pptx 同源存沙箱，返回 {fmt: ref}（尽力而为）。"""
    from app.storage.artifacts import resolve_artifact
    try:
        md = resolve_artifact(md_ref).read_text(encoding="utf-8", errors="replace")
    except Exception:      # noqa: BLE001  无文本稿 → 不生成附件
        return {}
    out = {}
    base = artifacts_base()
    try:
        out["docx"] = _save_fmt(base, task_id, "docx", build_docx(md, title))
    except Exception:      # noqa: BLE001
        pass
    try:
        out["xlsx"] = _save_fmt(base, task_id, "xlsx", build_xlsx(md, title))
    except Exception:
        pass
    try:
        out["pptx"] = _save_fmt(base, task_id, "pptx", build_pptx(md, title))
    except Exception:
        pass
    return out


def _save_fmt(base: Path, task_id: str, fmt: str, data: bytes) -> str:
    f = base / f"{task_id}.deliverable.{fmt}"
    f.write_bytes(data)
    return f.name
