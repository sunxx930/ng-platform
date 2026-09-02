"""产品免注册测试入口（2026-09-02，Task 38 收尾）：demo token 鉴权。

运行（mac/Linux）: cd ~/Desktop/ng-platform && .venv/bin/python -m pytest tests/test_demo.py -q
运行（Windows）: cd ~/Desktop/ng-platform && .venv\\Scripts\\python -m pytest tests/test_demo.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app.security import auth as authmod
    from app.storage.user_store import UserStore
    import app.storage.event_log as el
    monkeypatch.setattr("app.main.log", el.EventLog(path=tmp_path / "events.jsonl"))
    store = UserStore(path=tmp_path / "users.json")
    monkeypatch.setattr("app.main.user_store", store)
    authmod.set_user_store(store)
    return TestClient(app)


@pytest.fixture()
def demo_token(monkeypatch):
    """模拟 NG_DEMO_TOKEN 注入：把 demo token 加进静态 token 表（免注册直通身份）。"""
    from app.security import auth as a
    monkeypatch.setattr(a, "TOKENS", dict(a.TOKENS))
    a.TOKENS["demo-e2e-token-xyz"] = ("demo-admin", 3)
    return "demo-e2e-token-xyz"


def test_demo_token_auth(client, demo_token):
    """demo token 免注册直通鉴权：返回 demo-admin L3。"""
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {demo_token}"})
    assert r.status_code == 200
    assert r.json()["user"] == "demo-admin"
    assert r.json()["level"] == 3


def test_demo_can_create_project_and_task(client, demo_token):
    """demo token（L3）能建项目 + 任务 + 推进状态（免注册核心用途）。"""
    h = {"Authorization": f"Bearer {demo_token}"}
    pid = client.post("/projects", headers=h,
                      params={"title": "demo-proj", "goal": "g"}).json()["project_id"]
    assert pid
    tid = client.post(f"/projects/{pid}/tasks", headers=h,
                      params={"title": "T"}).json()["task_id"]
    assert tid
    assert client.patch(f"/tasks/{tid}/state", headers=h,
                        params={"to": "in_progress"}).status_code == 200


def test_demo_token_not_available_without_injection(client):
    """未注入 demo token 时该 token 无效（401）——不泄漏默认 demo 凭据。"""
    r = client.get("/auth/me", headers={"Authorization": "Bearer demo-e2e-token-xyz"})
    assert r.status_code == 401


def test_review_pass_auto_completes(client, monkeypatch):
    """QA 试用（2026-09-03）：复核 pass 后任务自动推进 completed（不卡 in_review）。"""
    h = {"Authorization": "Bearer l1-agent-token"}   # L1 静态 token
    pid = client.post("/projects", headers=h,
                      params={"title": "rev-auto", "goal": "g"}).json()["project_id"]
    tid = client.post(f"/projects/{pid}/tasks", headers=h,
                      params={"title": "R"}).json()["task_id"]
    client.patch(f"/tasks/{tid}/state", headers=h, params={"to": "in_progress"})
    client.post(f"/tasks/{tid}/deliverables", headers=h,
                params={"file_ref": "docs/r.md", "verdict": "done"})   # → in_review
    rid = client.post(f"/tasks/{tid}/reviews", headers=h).json()["review_id"]
    assert client.post(f"/reviews/{rid}/decision", headers=h,
                       params={"verdict": "pass"}).status_code == 200
    ctx = client.get(f"/tasks/{tid}/context", headers=h).json()
    assert ctx["status"] == "completed", f"复核 pass 应自动 completed，实际 {ctx['status']}"
