"""通知回归（P1-2，2026-09-01）：事件派生 + 按项目 owner 隔离。

运行（mac/Linux）: cd ~/Desktop/ng-platform && .venv/bin/python -m pytest tests/test_notifications.py -q
运行（Windows）: cd ~/Desktop/ng-platform && .venv\\Scripts\\python -m pytest tests/test_notifications.py -q
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.storage.event_log import EventLog


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 隔离：每个测试用独立事件文件 + 独立用户存储
    from app.main import log as real_log
    from app.security import auth as authmod
    from app.storage.user_store import UserStore
    monkeypatch.setattr("app.main.log", EventLog(path=tmp_path / "events.jsonl"))
    store = UserStore(path=tmp_path / "users.json")
    monkeypatch.setattr("app.main.user_store", store)
    authmod.set_user_store(store)
    c = TestClient(app)
    yield c


H1 = {"Authorization": "Bearer l1-agent-token"}   # L1（静态 token，viewer=None 见全部）
H3 = {"Authorization": "Bearer l3-test-token"}   # L3 静态 token


def _project(c, title="p"):
    return c.post("/projects", headers=H1, params={"title": title, "goal": "g"}).json()["project_id"]


def _task(c, pid):
    return c.post(f"/projects/{pid}/tasks", headers=H1, params={"title": "T"}).json()["task_id"]


def test_notifications_empty_on_no_activity(client):
    """无相关事件 → 空通知。"""
    r = client.get("/notifications", headers=H1)
    assert r.status_code == 200
    assert r.json()["notifications"] == []


def test_notifications_derive_state_changes(client):
    """任务状态变化 → 出现在通知（摘要 + 顺序）。"""
    pid = _project(client)
    tid = _task(client, pid)
    client.patch(f"/tasks/{tid}/state", headers=H1, params={"to": "in_progress"})
    notifs = client.get("/notifications", headers=H1).json()["notifications"]
    assert len(notifs) >= 1
    first = notifs[0]
    assert first["event_type"] == "task.state_changed"
    assert first["project_id"] == pid and first["task_id"] == tid
    assert "in_progress" in first["summary"]


def test_notifications_capped_at_20(client):
    """超过 20 条 → 只返回最近 20 条（倒序）。"""
    pid = _project(client)
    tid = _task(client, pid)
    for to in ["in_progress", "blocked", "in_progress", "blocked"] * 10:
        client.patch(f"/tasks/{tid}/state", headers=H1, params={"to": to})
    notifs = client.get("/notifications", headers=H1).json()["notifications"]
    assert len(notifs) <= 20
    # 倒序：ts 降序
    ts = [n["ts"] for n in notifs]
    assert ts == sorted(ts, reverse=True)


def test_notifications_review_approval_decided(client):
    """复核/审批结论 → 通知。"""
    pid = _project(client)
    tid = _task(client, pid)
    client.patch(f"/tasks/{tid}/state", headers=H1, params={"to": "in_progress"})
    client.patch(f"/tasks/{tid}/state", headers=H1, params={"to": "in_review"})
    rid = client.post(f"/tasks/{tid}/reviews", headers=H1).json()["review_id"]
    client.post(f"/reviews/{rid}/decision", headers=H1, params={"verdict": "pass"})
    aid = client.post(f"/tasks/{tid}/approvals", headers=H3,
                      params={"scope": "flow"}).json()["approval_id"]
    client.post(f"/approvals/{aid}/decision", headers=H3, params={"result": "approve"})
    types = {n["event_type"] for n in client.get("/notifications", headers=H1).json()["notifications"]}
    assert "review.decided" in types and "approval.decided" in types


def test_notifications_isolated_by_user(client):
    """多用户隔离：A 建的项目，B 看不到通知。"""
    # 注册 A/B（注册用户有真实 user_id）
    a = client.post("/auth/register", json={"username": "na", "password": "secret123"}).json()
    b = client.post("/auth/register", json={"username": "nb", "password": "secret123"}).json()
    ha = {"Authorization": f"Bearer {a['token']}"}
    hb = {"Authorization": f"Bearer {b['token']}"}
    pid = client.post("/projects", headers=ha, params={"title": "A项目", "goal": "g"}).json()["project_id"]
    tid = client.post(f"/projects/{pid}/tasks", headers=ha, params={"title": "T"}).json()["task_id"]
    client.patch(f"/tasks/{tid}/state", headers=ha, params={"to": "in_progress"})
    # A 看得到
    a_notifs = client.get("/notifications", headers=ha).json()["notifications"]
    assert any(n["project_id"] == pid for n in a_notifs)
    # B 看不到
    b_notifs = client.get("/notifications", headers=hb).json()["notifications"]
    assert all(n["project_id"] != pid for n in b_notifs)
