"""项目材料库 v1.2.1：上传(沙箱+文本抽取)/列表/护栏/越权。

运行: .venv/bin/python -m pytest tests/test_materials.py -q
"""
import base64
import io
import sys
import uuid
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.storage.event_log import EventLog
from app.storage.user_store import UserStore


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app.security import auth as authmod
    monkeypatch.setattr("app.main.log", EventLog(path=tmp_path / "events.jsonl"))
    store = UserStore(path=tmp_path / "users.json")
    monkeypatch.setattr("app.main.user_store", store)
    authmod.set_user_store(store)
    monkeypatch.setenv("NG_HOME", str(tmp_path))   # artifacts 落临时目录
    c = TestClient(app)
    yield c


def _reg(c, name):
    uname = f"{name}-{uuid.uuid4().hex[:6]}"
    d = c.post("/auth/register", json={"username": uname, "password": "secret123"}).json()
    return d["token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


def _docx(text):
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w") as z:
        z.writestr("word/document.xml",
                   '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   "<w:body><w:p><w:r><w:t>" + text + "</w:t></w:r></w:p></w:body></w:document>")
    return bio.getvalue()


def _xlsx():
    bio = io.BytesIO()
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    with zipfile.ZipFile(bio, "w") as z:
        z.writestr("xl/sharedStrings.xml",
                   f'<sst xmlns="{ns}" count="2" uniqueCount="2"><si><t>苹果</t></si><si><t>3.5</t></si></sst>')
        z.writestr("xl/worksheets/sheet1.xml",
                   f'<worksheet xmlns="{ns}"><sheetData><row r="1">'
                   '<c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row></sheetData></worksheet>')
    return bio.getvalue()


def _b64(b):
    return base64.b64encode(b).decode()


def test_materials_upload_txt_docx_xlsx(client):
    t = _reg(client, "a")
    pid = client.post("/projects", headers=_h(t), params={"title": "t", "goal": "g"}).json()["project_id"]
    # txt
    r = client.post(f"/projects/{pid}/materials", headers=_h(t),
                    json={"name": "data.txt", "data_b64": _b64(b"apple,3.5")})
    assert r.status_code == 200, r.text
    assert r.json()["text_ref"]
    # docx
    r = client.post(f"/projects/{pid}/materials", headers=_h(t),
                    json={"name": "报告.docx", "data_b64": _b64(_docx("标题 收入100 成本60"))})
    assert r.status_code == 200, r.text
    assert "收入100" in _txt(client, r)
    # xlsx
    r = client.post(f"/projects/{pid}/materials", headers=_h(t),
                    json={"name": "表.xlsx", "data_b64": _b64(_xlsx())})
    assert r.status_code == 200, r.text
    assert r.json()["text_ref"], "xlsx 应抽取文本"
    # list
    lst = client.get(f"/projects/{pid}/materials", headers=_h(t)).json()["materials"]
    assert len(lst) == 3
    assert any(m["name"] == "表.xlsx" for m in lst)


def _txt(client, resp):
    """从 text_ref 路径读回内容：模拟 agent 用 resolve 读抽取文件。"""
    import os
    from pathlib import Path
    ref = resp.json()["text_ref"]
    return (Path(os.environ["NG_HOME"]) / "artifacts" / ref).read_text(encoding="utf-8")


def test_materials_guards(client):
    t = _reg(client, "a")
    tb = _reg(client, "b")
    pid = client.post("/projects", headers=_h(t), params={"title": "t", "goal": "g"}).json()["project_id"]
    # 越权(B) → 403
    assert client.post(f"/projects/{pid}/materials", headers=_h(tb),
                       json={"name": "x.txt", "data_b64": _b64(b"hi")}).status_code == 403
    # 类型不支持
    assert client.post(f"/projects/{pid}/materials", headers=_h(t),
                       json={"name": "a.sh", "data_b64": _b64(b"#!/bin/sh")}).status_code == 400
    # 超大小 (21MB)
    big = _b64(b"x" * (21 * 1024 * 1024))
    assert client.post(f"/projects/{pid}/materials", headers=_h(t),
                       json={"name": "big.txt", "data_b64": big}).status_code == 400
