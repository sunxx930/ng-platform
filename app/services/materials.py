"""项目材料库（v1.2.1）：客户上传原始数据（Word/Excel/PPT/图片/文本）→ 沙箱存储 + 文本抽取。

- 上传文件存到 artifacts 沙箱内（project_inputs/），延续 P0-3 不破坏
- Word(.docx)/Excel(.xlsx)/PPT(.pptx) 用**纯 stdlib**（zipfile+xml）抽文字为 .txt
   ——避免第三方库/联网依赖；图片/无法抽取 → text_ref 为空 + note
- agent/复核只读抽取后的 text_ref（文本模型可读）；原件保留备查
"""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from app.storage.artifacts import artifacts_base

MAX_MB = 20
_ALLOWED = {".docx", ".xlsx", ".pptx", ".txt", ".md", ".json", ".csv", ".png", ".jpg", ".jpeg"}
# 文本模型可读/可抽取的
_TEXTISH = {".txt", ".md", ".json", ".csv", ".docx", ".xlsx", ".pptx"}


class MaterialError(Exception):
    pass


def _read_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    root = ET.fromstring(xml)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paras = ["".join(t.text or "" for t in p.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"))
             for p in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p")]
    return "\n".join(p for p in paras if p.strip())


def _read_pptx(path: Path) -> str:
    out = []
    with zipfile.ZipFile(path) as z:
        names = sorted(n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n))
        for n in names:
            xml = z.read(n).decode("utf-8", "replace")
            try:
                root = ET.fromstring(xml)
            except Exception:
                continue
            texts = [t.text or "" for t in root.iter(
                "{http://schemas.openxmlformats.org/drawingml/2006/main}t")]
            line = " ".join(x for x in texts if x.strip())
            if line.strip():
                out.append(line)
    return "\n".join(out)


def _read_xlsx(path: Path) -> str:
    """纯 stdlib 抽取：sharedStrings + 每个 sheet 的文字（不展开公式求值）。"""
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml").decode("utf-8", "replace"))
            ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
            for si in root.iter(ns + "si"):
                shared.append("".join(t.text or "" for t in si.iter(ns + "t")))
        rows = []
        for n in sorted(x for x in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", x)):
            root = ET.fromstring(z.read(n).decode("utf-8", "replace"))
            ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
            for row in root.iter(ns + "row"):
                cells = []
                for c in row.iter(ns + "c"):
                    v = None
                    for node in c.iter(ns + "t"):
                        v = node.text or ""
                    if v is None:
                        for node in c.iter(ns + "v"):
                            v = node.text or ""
                    t = c.get("t")
                    if t == "s" and v is not None and v.isdigit() and int(v) < len(shared):
                        v = shared[int(v)]
                    if v is not None:
                        cells.append(str(v))
                if any(x.strip() for x in cells):
                    rows.append(",".join(cells))
    return "\n".join(rows)


def extract_text(path: Path) -> tuple[str, str]:
    """返回 (抽取文本, note)。图片/失败 → ("" , 说明)。"""
    ext = path.suffix.lower()
    try:
        if ext == ".txt" or ext == ".md" or ext == ".json" or ext == ".csv":
            return path.read_text(encoding="utf-8", errors="replace"), "ok"
        if ext == ".docx":
            return _read_docx(path), "ok(docx)"
        if ext == ".pptx":
            return _read_pptx(path), "ok(pptx)"
        if ext == ".xlsx":
            return _read_xlsx(path), "ok(xlsx)"
        if ext in {".png", ".jpg", ".jpeg"}:
            return "", "image: 已存原件，图表需视觉/OCR（二期）"
    except Exception as e:   # noqa: BLE001
        return "", f"抽取失败: {type(e).__name__}: {e}"
    return "", "unsupported"


def store_material(project_id: str, filename: str, data: bytes):
    """校验并落盘。返回 (material_id, orig_ref, text_ref, text_len, note)。"""
    if len(data) > MAX_MB * 1024 * 1024:
        raise MaterialError(f"文件超过 {MAX_MB}MB")
    ext = Path(filename).suffix.lower()
    if ext not in _ALLOWED:
        raise MaterialError(f"不支持的文件类型: {ext or '(无扩展名)'}")
    if Path(filename).name != filename or "/" in filename or "\\" in filename or filename.startswith("."):
        raise MaterialError("文件名不合法")
    base = artifacts_base() / "project_inputs"
    base.mkdir(parents=True, exist_ok=True)
    mid = hashlib.sha256(f"{project_id}:{filename}:{len(data)}".encode()).hexdigest()[:12]
    orig = base / f"{project_id}_{mid}_{Path(filename).name}"
    orig.write_bytes(data)
    text, note = extract_text(orig)
    text_ref = None
    if text.strip():
        tf = base / f"{project_id}_{mid}.txt"
        tf.write_text(text[:200000], encoding="utf-8")
        text_ref = f"project_inputs/{tf.name}"
    return {"material_id": mid, "name": filename, "ext": ext,
            "size": len(data), "orig_ref": f"project_inputs/{orig.name}",
            "text_ref": text_ref, "text_len": len(text), "note": note}
