"""v1.1 安全回归（走查 P0-1/P0-2/P0-3 + L1 真人复核）。

运行: cd ~/Desktop/ng-platform && .venv/bin/python -m pytest tests/test_v1_1_security.py -q
"""
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
    # 隔离：独立事件文件 + 独立用户存储（dev JSONL 兜底）。
    # 必须同时注入 auth 内部 store——先前其它测试文件会把全局 auth._store
    # 指到各自的临时 store 且不还原，导致本文件按字母序最后运行时 require_auth
    # 查错 store → 401。
    from app.main import log as real_log   # noqa: F401
    from app.security import auth as authmod
    monkeypatch.setattr("app.main.log", EventLog(path=tmp_path / "events.jsonl"))
    store = UserStore(path=tmp_path / "users_store.json")
    monkeypatch.setattr("app.main.user_store", store)
    authmod.set_user_store(store)
    c = TestClient(app)
    yield c


def _reg(c, name):
    """注册并返回 token/身份。"""
    uname = f"{name}-{uuid.uuid4().hex[:6]}"
    r = c.post("/auth/register", json={"username": uname, "password": "secret123"})
    assert r.status_code == 200, r.text
    d = r.json()
    return d["token"], uname, d["user_id"]


def _proj(c, tok):
    return c.post("/projects", headers={"Authorization": f"Bearer {tok}"},
                  params={"title": "t", "goal": "g"}).json()["project_id"]


def _task(c, tok, pid):
    return c.post(f"/projects/{pid}/tasks", headers={"Authorization": f"Bearer {tok}"},
                  params={"title": "T"}).json()["task_id"]


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


# ---------- P0-1：PATCH 禁直达 completed ----------
def test_patch_cannot_complete(client):
    ta, _, _ = _reg(client, "a")
    pid = _proj(client, ta)
    tid = _task(client, ta, pid)
    r = client.patch(f"/tasks/{tid}/state", headers=_h(ta), params={"to": "completed"})
    assert r.status_code == 400
    assert "completed" in r.json()["detail"]


# ---------- P0-2：对象级隔离 ----------
def test_cross_user_object_isolation(client):
    ta, _, _ = _reg(client, "a")
    tb, _, _ = _reg(client, "b")
    pid = _proj(client, ta)
    tid = _task(client, ta, pid)
    # B 读/写 A 的项目与任务 → 403
    for path in [f"/projects/{pid}/tasks", f"/projects/{pid}/audit",
                 f"/projects/{pid}/context", f"/tasks/{tid}/context"]:
        assert client.get(path, headers=_h(tb)).status_code == 403, path
    assert client.patch(f"/tasks/{tid}/state", headers=_h(tb),
                        params={"to": "in_progress"}).status_code == 403
    assert client.post(f"/tasks/{tid}/deliverables", headers=_h(tb),
                       params={"file_ref": "artifacts/x.md", "verdict": "done"}).status_code == 403
    # A 自己仍正常
    assert client.get(f"/projects/{pid}/tasks", headers=_h(ta)).status_code == 200


# ---------- P0-3：file_ref 沙箱 ----------
def test_deliverable_file_ref_sandbox(client):
    ta, _, _ = _reg(client, "a")
    pid = _proj(client, ta)
    tid = _task(client, ta, pid)
    # 绝对路径 / 穿越 → 403
    for bad in ["/etc/hosts", "../.env", "artifacts/../.env"]:
        r = client.post(f"/tasks/{tid}/deliverables", headers=_h(ta),
                        params={"file_ref": bad, "verdict": "done"})
        assert r.status_code == 403, f"{bad} 应 403"
    # artifacts 内不存在的文件 → 200 + file_missing（不泄露，流程不卡）
    r = client.post(f"/tasks/{tid}/deliverables", headers=_h(ta),
                    params={"file_ref": "artifacts/nonexist-v11.md", "verdict": "done"})
    assert r.status_code == 200
    assert r.json().get("status")


# ---------- review 授权 + opinion 契约（L1(b)） ----------
def test_review_owner_and_opinion(client, tmp_path):
    ta, _, _ = _reg(client, "a")
    tb, _, _ = _reg(client, "b")
    pid = _proj(client, ta)
    tid = _task(client, ta, pid)
    client.patch(f"/tasks/{tid}/state", headers=_h(ta), params={"to": "in_progress"})
    client.post(f"/tasks/{tid}/deliverables", headers=_h(ta),
                params={"file_ref": "artifacts/v11.md", "verdict": "done"})
    rid = client.post(f"/tasks/{tid}/reviews", headers=_h(ta)).json()["review_id"]
    # 打回无 opinion → 400
    assert client.post(f"/reviews/{rid}/decision", headers=_h(ta),
                       params={"verdict": "needs_changes"}).status_code == 400
    # B（非 owner/非指派/非 L3）审 A 的任务 → 403
    assert client.post(f"/reviews/{rid}/decision", headers=_h(tb),
                       params={"verdict": "pass"}).status_code == 403
    # 项目 owner 可审（打回需意见）→ in_progress
    r = client.post(f"/reviews/{rid}/decision", headers=_h(ta),
                    params={"verdict": "needs_changes", "opinion": "改口径"})
    assert r.status_code == 200, r.text
    assert client.get(f"/tasks/{tid}/context", headers=_h(ta)).json()["status"] == "in_progress"
    # 二次交 → owner pass → completed（闭环）
    client.patch(f"/tasks/{tid}/state", headers=_h(ta), params={"to": "in_progress"})
    client.post(f"/tasks/{tid}/deliverables", headers=_h(ta),
                params={"file_ref": "artifacts/v11b.md", "verdict": "done"})
    rid2 = client.post(f"/tasks/{tid}/reviews", headers=_h(ta)).json()["review_id"]
    assert client.post(f"/reviews/{rid2}/decision", headers=_h(ta),
                       params={"verdict": "pass"}).status_code == 200
    assert client.get(f"/tasks/{tid}/context", headers=_h(ta)).json()["status"] == "completed"


# ---------- 真人账号可被指派 reviewer（方案 b） ----------
def test_real_user_assigned_reviewer_can_decide(client):
    to, uname, _ = _reg(client, "owner")
    tr, rname, _ = _reg(client, "reviewer")
    pid = _proj(client, to)
    tid = client.post(f"/projects/{pid}/tasks", headers=_h(to),
                      params={"title": "R"}).json()["task_id"]
    # 指派 reviewer = 真人账号用户名（rname），并 assign owner=to
    from app.main import log
    from app.domain import events
    log.append(events.new_event(events.EventType.AGENT_ASSIGNED, "system",
                                {"agent": rname, "role": "reviewer"},
                                project_id=pid, task_id=tid))
    client.patch(f"/tasks/{tid}/state", headers=_h(to), params={"to": "in_progress"})
    client.post(f"/tasks/{tid}/deliverables", headers=_h(to),
                params={"file_ref": "artifacts/r.md", "verdict": "done"})
    rid = client.post(f"/tasks/{tid}/reviews", headers=_h(to)).json()["review_id"]
    # 被指派真人账号（非 owner）可决策 pass → completed
    assert client.post(f"/reviews/{rid}/decision", headers=_h(tr),
                       params={"verdict": "pass"}).status_code == 200
    assert client.get(f"/tasks/{tid}/context", headers=_h(to)).json()["status"] == "completed"
