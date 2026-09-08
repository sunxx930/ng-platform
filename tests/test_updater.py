"""内容热更（方案A）v1.2.2：UI override 服务 + apply（file:// manifest，离线可测）。"""
import hashlib
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.storage.event_log import EventLog
from app.storage.user_store import UserStore


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("app.main.log", EventLog(path=tmp_path / "events.jsonl"))
    from app.security import auth as authmod
    store = UserStore(path=tmp_path / "users.json")
    monkeypatch.setattr("app.main.user_store", store)
    authmod.set_user_store(store)
    monkeypatch.setenv("NG_HOME", str(tmp_path))
    return tmp_path


def _h(c):
    t = c.post("/auth/register",
               json={"username": "up-" + uuid.uuid4().hex[:6], "password": "secret123"}).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def test_apply_content_update(env, monkeypatch):
    src = env / "src.js"
    src.write_text("console.log('v2')", encoding="utf-8")
    manifest = {
        "version": "2.0",
        "files": [{"path": "assets/app.js", "url": (src.as_uri()),
                  "sha256": _sha(src.read_bytes())}],
    }
    mf = env / "manifest.json"
    mf.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("NG_UPDATE_MANIFEST", mf.as_uri())
    c = TestClient(app)
    h = _h(c)
    r = c.post("/update/apply", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["applied"] == 1
    assert (env / "ui" / "assets" / "app.js").read_text(encoding="utf-8") == "console.log('v2')"
    assert (env / "ui" / "version.json").read_text(encoding="utf-8").find("2.0") >= 0
    # 状态
    st = c.get("/update/status", headers=h).json()
    assert st["enabled"] is True and st["ui_version"] == "2.0"
    # override 服务生效
    body = c.get("/assets/app.js").content
    assert b"console.log('v2')" in body


def test_apply_rejects_traversal(env, monkeypatch):
    src = env / "x.js"
    src.write_text("bad", encoding="utf-8")
    mf = env / "manifest.json"
    mf.write_text(json.dumps({"version": "1", "files": [
        {"path": "../../escape.js", "url": src.as_uri(), "sha256": _sha(src.read_bytes())},
    ]}), encoding="utf-8")
    monkeypatch.setenv("NG_UPDATE_MANIFEST", mf.as_uri())
    c = TestClient(app)
    h = _h(c)
    r = c.post("/update/apply", headers=h).json()
    assert r["applied"] == 0 and r["failed"] == 1
    assert not (env / "escape.js").exists()
    assert not (env.parent / "escape.js").exists()
