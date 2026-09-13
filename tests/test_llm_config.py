"""算力配置持久化修复 v1.2.4：界面保存 key → 立即生效 + 落到数据目录 .env。"""
import os
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
def client(tmp_path, monkeypatch):
    from app.security import auth as authmod
    monkeypatch.setattr("app.main.log", EventLog(path=tmp_path / "events.jsonl"))
    store = UserStore(path=tmp_path / "users.json")
    monkeypatch.setattr("app.main.user_store", store)
    authmod.set_user_store(store)
    monkeypatch.setenv("NG_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    c = TestClient(app)
    yield c


def test_llm_config_saved_and_effective(client, tmp_path):
    t = client.post("/auth/register",
                    json={"username": "llm-" + uuid.uuid4().hex[:6], "password": "secret123"}).json()["token"]
    h = {"Authorization": f"Bearer {t}"}
    keys = ["LLM_PROVIDER", "LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL"]
    saved = {k: os.environ.get(k) for k in keys}
    try:
        r = client.post("/agents/llm-config", headers=h,
                        json={"provider": "qwen", "api_key": "sk-test-123", "model": "qwen-max"})
        assert r.status_code == 200, r.text
        # 立即生效：运行中进程环境变量已更新
        assert os.environ.get("LLM_API_KEY") == "sk-test-123"
        assert os.environ.get("LLM_MODEL") == "qwen-max"
        # 持久化：写到数据目录（cwd/NG_HOME）的 .env
        envf = tmp_path / ".env"
        assert envf.exists() and "LLM_API_KEY=sk-test-123" in envf.read_text(encoding="utf-8")
        # GET 报已配置
        assert client.get("/agents/llm-config", headers=h).json()["api_key_set"] is True
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
