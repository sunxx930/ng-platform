"""Office 交付生成 v1.2.1：docx/xlsx/pptx 结构性 + 下载端点。

注意：这里验证"文件结构合法(zip+xml 可解析)"；真实 Word/Excel/PowerPoint
打开请在真机开一次确认（离线无法代验）。
"""
import sys
import uuid
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import export
from app.storage.event_log import EventLog
from app.storage.user_store import UserStore

MD = "# 周报\n\n收入 100，成本 60。\n\n| 项 | 值 |\n|---|---|\n| 收入 | 100 |\n| 成本 | 60 |\n"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app.security import auth as authmod
    monkeypatch.setattr("app.main.log", EventLog(path=tmp_path / "events.jsonl"))
    store = UserStore(path=tmp_path / "users.json")
    monkeypatch.setattr("app.main.user_store", store)
    authmod.set_user_store(store)
    monkeypatch.setenv("NG_HOME", str(tmp_path))
    c = TestClient(app)
    yield c


def _parts(data: bytes):
    return zipfile.ZipFile(bytearray(data) and __import__("io").BytesIO(data)).namelist()


def test_builders_structure():
    d = export.build_docx(MD, "标题")
    assert "word/document.xml" in _parts(d)
    x = export.build_xlsx(MD)
    assert "xl/workbook.xml" in _parts(x)
    assert any(n.startswith("xl/worksheets/sheet") for n in _parts(x))
    p = export.build_pptx(MD, "标题")
    parts = _parts(p)
    assert "ppt/presentation.xml" in parts
    assert any(n.startswith("ppt/slides/slide") for n in parts)


def test_generate_and_download(client, tmp_path):
    ta = client.post("/auth/register",
                     json={"username": "exp-" + uuid.uuid4().hex[:6], "password": "secret123"}).json()["token"]
    h = {"Authorization": f"Bearer {ta}"}
    pid = client.post("/projects", headers=h, params={"title": "t", "goal": "g"}).json()["project_id"]
    tid = client.post(f"/projects/{pid}/tasks", headers=h, params={"title": "写周报"}).json()["task_id"]
    client.patch(f"/tasks/{tid}/state", headers=h, params={"to": "in_progress"})
    # 放一个真实 md 到沙箱，作为交付源
    (tmp_path / "artifacts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "artifacts" / "r.md").write_text(MD, encoding="utf-8")
    r = client.post(f"/tasks/{tid}/deliverables", headers=h,
                    params={"file_ref": "r.md", "verdict": "done"})
    assert r.status_code == 200
    for fmt in ("docx", "xlsx", "pptx"):
        dr = client.get(f"/tasks/{tid}/deliverable_file", headers=h, params={"fmt": fmt})
        assert dr.status_code == 200, fmt
        assert dr.content[:2] == b"PK", f"{fmt} 应为 zip 文件"
