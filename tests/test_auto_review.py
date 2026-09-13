"""AutoReviewWorker v1.2.5：自动复核闭环 / 真人复核不抢 / 开关。"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

import app.main as M
from app.storage.event_log import EventLog
from app.storage.user_store import UserStore
from app.domain import events
from app.workers.auto_review import AutoReviewWorker


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app.security import auth as authmod
    monkeypatch.setattr("app.main.log", EventLog(path=tmp_path / "events.jsonl"))
    store = UserStore(path=tmp_path / "users.json")
    monkeypatch.setattr("app.main.user_store", store)
    authmod.set_user_store(store)
    monkeypatch.setenv("NG_HOME", str(tmp_path))
    (tmp_path / "artifacts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "artifacts" / "d.md").write_text("内容", encoding="utf-8")
    return TestClient(M.app)


def _h(c):
    t = c.post("/auth/register",
               json={"username": "ar-" + uuid.uuid4().hex[:6], "password": "secret123"}).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def _mk_review_task(c, h, title="写周报"):
    pid = c.post("/projects", headers=h, params={"title": "t", "goal": "g"}).json()["project_id"]
    tid = c.post(f"/projects/{pid}/tasks", headers=h, params={"title": title}).json()["task_id"]
    c.patch(f"/tasks/{tid}/state", headers=h, params={"to": "in_progress"})
    c.post(f"/tasks/{tid}/deliverables", headers=h, params={"file_ref": "d.md", "verdict": "done"})
    return pid, tid


def _status(c, h, pid, tid):
    return c.get(f"/tasks/{tid}/context", headers=h).json()["status"]


def test_auto_review_passes_and_completes(client):
    h = _h(client)
    pid, tid = _mk_review_task(client, h)
    AutoReviewWorker(M.log).tick([tid])
    assert _status(client, h, pid, tid) == "completed"
    evs = client.get(f"/projects/{pid}/audit", headers=h).json()["events"]
    dec = [e for e in evs if e["event_type"] == "review.decided"]
    assert dec and dec[-1]["actor"] == "auto-reviewer"


def test_human_reviewer_not_overridden(client):
    h = _h(client)
    pid, tid = _mk_review_task(client, h)
    # 指派一个"真人"复核人（不在 builtin 注册表里）
    M.log.append(events.new_event(events.EventType.AGENT_ASSIGNED, "system",
                                  {"agent": "真人张三", "role": "reviewer"},
                                  project_id=pid, task_id=tid))
    AutoReviewWorker(M.log).tick([tid])
    assert _status(client, h, pid, tid) == "in_review"   # 留给人工


def test_auto_review_can_be_disabled(client, monkeypatch):
    monkeypatch.setenv("NG_AUTO_REVIEW", "0")
    h = _h(client)
    pid, tid = _mk_review_task(client, h)
    AutoReviewWorker(M.log).tick([tid])
    assert _status(client, h, pid, tid) == "in_review"
