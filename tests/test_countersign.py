"""审批会签（2026-10-30）——多人共同审批：全部同意才通过，任一驳回退回。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

from app import main as mainmod
from app.main import app
from app.security import auth as authmod
from app.storage.event_log import EventLog
from app.storage.user_store import UserStore

H1 = {"Authorization": "Bearer l1-agent-token"}   # username=agent（L1）
H3 = {"Authorization": "Bearer l3-test-token"}    # username=admin（L3）


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("app.main.log", EventLog(path=tmp_path / "events.jsonl"))
    store = UserStore(path=tmp_path / "users_store.json")
    monkeypatch.setattr(mainmod, "user_store", store)
    monkeypatch.setattr(authmod, "user_store", store)
    return TestClient(app)


def _task_in_review(c):
    pid = c.post("/projects", headers=H1,
                 params={"title": "t", "goal": "g"}).json()["project_id"]
    tid = c.post(f"/projects/{pid}/tasks", headers=H1,
                 params={"title": "会签测试任务"}).json()["task_id"]
    for to in ("in_progress", "in_review"):
        assert c.patch(f"/tasks/{tid}/state", headers=H1,
                       params={"to": to}).status_code == 200
    return pid, tid


def _status(c, pid, tid):
    tasks = c.get(f"/projects/{pid}/tasks", headers=H1).json()["tasks"]
    return next(t["status"] for t in tasks if t["task_id"] == tid)


def _audit(c, pid):
    return c.get(f"/projects/{pid}/audit", headers=H1).json()["events"]


def test_countersign_all_approve(client):
    pid, tid = _task_in_review(client)
    r = client.post(f"/tasks/{tid}/approvals", headers=H3,
                    params={"scope": "flow_change", "signers": "admin,agent",
                            "rule": "all"})
    assert r.status_code == 200, r.text
    aid = r.json()["approval_id"]
    assert r.json()["signers"] == ["admin", "agent"]
    assert _status(client, pid, tid) == "pending_approval"

    # 会签审批不允许 L3 一步代签
    assert client.post(f"/approvals/{aid}/decision", headers=H3,
                       params={"result": "approve"}).status_code == 400

    r = client.post(f"/approvals/{aid}/sign", headers=H3, params={"result": "approve"})
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert r.json()["progress"]["signed"] == 1
    assert _status(client, pid, tid) == "pending_approval"

    r = client.post(f"/approvals/{aid}/sign", headers=H1, params={"result": "approve"})
    assert r.status_code == 200 and r.json()["status"] == "approve"
    assert _status(client, pid, tid) == "completed"

    types = [e["event_type"] for e in _audit(client, pid)]
    assert types.count("approval.signed") == 2
    assert "approval.decided" in types


def test_countersign_reject_veto(client):
    pid, tid = _task_in_review(client)
    aid = client.post(f"/tasks/{tid}/approvals", headers=H3,
                      params={"scope": "flow", "signers": "admin,agent"}).json()["approval_id"]
    # 驳回必须带意见
    assert client.post(f"/approvals/{aid}/sign", headers=H1,
                       params={"result": "reject"}).status_code == 400
    r = client.post(f"/approvals/{aid}/sign", headers=H1,
                    params={"result": "reject", "opinion": "材料不完整"})
    assert r.status_code == 200 and r.json()["status"] == "reject"
    assert _status(client, pid, tid) == "in_progress"
    # 终态后其他人不能再签
    assert client.post(f"/approvals/{aid}/sign", headers=H3,
                       params={"result": "approve"}).status_code == 400


def test_sign_requires_designated_signer(client):
    pid, tid = _task_in_review(client)
    aid = client.post(f"/tasks/{tid}/approvals", headers=H3,
                      params={"scope": "flow", "signers": "admin"}).json()["approval_id"]
    assert client.post(f"/approvals/{aid}/sign", headers=H1,
                       params={"result": "approve"}).status_code == 403
    # 未知会签人拒绝发起
    r = client.post(f"/tasks/{tid}/approvals", headers=H3,
                    params={"scope": "flow2", "signers": "nobody"})
    assert r.status_code == 400


def test_countersign_sign_idempotent(client):
    pid, tid = _task_in_review(client)
    aid = client.post(f"/tasks/{tid}/approvals", headers=H3,
                      params={"scope": "flow", "signers": "admin,agent"}).json()["approval_id"]
    r1 = client.post(f"/approvals/{aid}/sign", headers=H3, params={"result": "approve"})
    r2 = client.post(f"/approvals/{aid}/sign", headers=H3, params={"result": "approve"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json().get("idempotent") is True
    signed = [e for e in _audit(client, pid) if e["event_type"] == "approval.signed"]
    assert len(signed) == 1
    # 同人改签 → 400
    assert client.post(f"/approvals/{aid}/sign", headers=H3,
                       params={"result": "reject", "opinion": "改主意"}).status_code == 400


def test_sign_retry_after_terminal_is_idempotent(client):
    """终态后同一会签人重放同一签署 → 200 幂等（返回实际终态），不改签仍 400。"""
    pid, tid = _task_in_review(client)
    aid = client.post(f"/tasks/{tid}/approvals", headers=H3,
                      params={"scope": "flow", "signers": "admin,agent"}).json()["approval_id"]
    assert client.post(f"/approvals/{aid}/sign", headers=H3,
                       params={"result": "approve"}).status_code == 200
    assert client.post(f"/approvals/{aid}/sign", headers=H1,
                       params={"result": "approve"}).status_code == 200
    retry = client.post(f"/approvals/{aid}/sign", headers=H3,
                        params={"result": "approve"})
    assert retry.status_code == 200, retry.text
    assert retry.json()["idempotent"] is True
    assert retry.json()["status"] == "approve"
    assert client.post(f"/approvals/{aid}/sign", headers=H3,
                       params={"result": "reject", "opinion": "改主意"}).status_code == 400


def test_set_signers_on_existing_approval(client):
    pid, tid = _task_in_review(client)
    aid = client.post(f"/tasks/{tid}/approvals", headers=H3,
                      params={"scope": "flow"}).json()["approval_id"]
    r = client.post(f"/approvals/{aid}/signers", headers=H3,
                    params={"signers": "admin,agent", "rule": "all"})
    assert r.status_code == 200
    assert client.post(f"/approvals/{aid}/decision", headers=H3,
                       params={"result": "approve"}).status_code == 400
    client.post(f"/approvals/{aid}/sign", headers=H3, params={"result": "approve"})
    r = client.post(f"/approvals/{aid}/sign", headers=H1, params={"result": "approve"})
    assert r.json()["status"] == "approve"
    assert _status(client, pid, tid) == "completed"


def test_legacy_single_approval_still_works(client):
    pid, tid = _task_in_review(client)
    aid = client.post(f"/tasks/{tid}/approvals", headers=H3,
                      params={"scope": "flow"}).json()["approval_id"]
    r = client.post(f"/approvals/{aid}/decision", headers=H3, params={"result": "approve"})
    assert r.status_code == 200 and r.json()["result"] == "approve"
    assert _status(client, pid, tid) == "completed"


def test_any_rule(client):
    pid, tid = _task_in_review(client)
    aid = client.post(f"/tasks/{tid}/approvals", headers=H3,
                      params={"scope": "flow", "signers": "admin,agent",
                              "rule": "any"}).json()["approval_id"]
    r = client.post(f"/approvals/{aid}/sign", headers=H3, params={"result": "approve"})
    assert r.json()["status"] == "approve"
    assert _status(client, pid, tid) == "completed"


def test_pending_countersigns_visible_to_signers(client):
    pid, tid = _task_in_review(client)
    aid = client.post(f"/tasks/{tid}/approvals", headers=H3,
                      params={"scope": "flow", "signers": "admin,agent"}).json()["approval_id"]

    items = client.get("/approvals/pending", headers=H1).json()["approvals"]
    assert [x["approval_id"] for x in items] == [aid]
    assert items[0]["task_title"] == "会签测试任务"
    assert items[0]["my_signature"] is None

    # 签署后仍在列表（等待其他会签人），带 my_signature
    client.post(f"/approvals/{aid}/sign", headers=H1, params={"result": "approve"})
    items = client.get("/approvals/pending", headers=H1).json()["approvals"]
    assert items[0]["my_signature"] == "approve"
    assert items[0]["progress"]["signed"] == 1

    # 非会签人看不到
    reg = client.post("/auth/register", json={"username": "outsider", "password": "secret1"})
    assert reg.status_code == 200
    oh = {"Authorization": f"Bearer {reg.json()['token']}"}
    assert client.get("/approvals/pending", headers=oh).json()["approvals"] == []

    # 全部签完 → 从待办消失
    client.post(f"/approvals/{aid}/sign", headers=H3, params={"result": "approve"})
    assert client.get("/approvals/pending", headers=H3).json()["approvals"] == []


def test_signer_notification_even_not_project_owner(client):
    pid, tid = _task_in_review(client)
    reg = client.post("/auth/register", json={"username": "signer1", "password": "secret1"})
    assert reg.status_code == 200
    tok = {"Authorization": f"Bearer {reg.json()['token']}"}
    aid = client.post(f"/tasks/{tid}/approvals", headers=H3,
                      params={"scope": "flow", "signers": "signer1"}).json()["approval_id"]
    notifs = client.get("/notifications", headers=tok).json()["notifications"]
    assert any(n.get("approval_id") == aid and n["event_type"] == "approval.requested"
               for n in notifs)
