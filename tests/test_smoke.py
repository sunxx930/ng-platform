"""MVP 冒烟测试 —— 覆盖复核门禁项（F1-F6 + 权限 + 审计 + 幂等）。

运行（mac/Linux）: cd ~/Desktop/ng-platform && .venv/bin/python -m pytest tests/ -q
运行（Windows）: cd ~/Desktop/ng-platform && .venv\Scripts\python -m pytest tests/ -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.domain.task import TaskStatus, can_transition
from app.storage.event_log import EventLog


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 隔离：每个测试用独立事件文件
    from app.main import log as real_log
    monkeypatch.setattr("app.main.log", EventLog(path=tmp_path / "events.jsonl"))
    c = TestClient(app)
    yield c


def _project(c):
    return c.post("/projects", headers=H1, params={"title": "t", "goal": "g"}).json()["project_id"]


H1 = {"Authorization": "Bearer l1-agent-token"}   # L1
H3 = {"Authorization": "Bearer l3-test-token"}   # L3


def _task(c, pid, **kw):
    return c.post(f"/projects/{pid}/tasks", headers=H1, params={"title": "T", **kw}).json()["task_id"]


def test_state_machine_whitelist(client):
    pid = _project(client)
    tid = _task(client, pid)
    for to in ["in_progress", "in_review", "pending_approval", "completed"]:
        r = client.patch(f"/tasks/{tid}/state", headers=H1, params={"to": to})
        assert r.status_code == 200, f"{to} 应合法"
    # 终态无出边：completed 不能再转
    r = client.patch(f"/tasks/{tid}/state", headers=H1, params={"to": "in_progress"})
    assert r.status_code == 400


def test_permission_denied_403(client):
    pid = _project(client)
    r = client.post(f"/projects/{pid}/pause", headers=H1)  # L1 无 L3 → 403
    assert r.status_code == 403
    assert "权限不足" in r.json()["detail"]


def test_audit_includes_state_changes(client):
    pid = _project(client)
    tid = _task(client, pid)
    client.patch(f"/tasks/{tid}/state", headers=H1, params={"to": "in_progress"})
    types = [e["event_type"] for e in client.get(f"/projects/{pid}/audit", headers=H1).json()["events"]]
    assert "task.state_changed" in types


def test_idempotency_dedup(client, tmp_path):
    pid = _project(client)
    tid = _task(client, pid)
    for _ in range(3):
        client.patch(f"/tasks/{tid}/state", headers=H1,
                     params={"to": "in_progress", "idempotency_key": "k1"})
    log = EventLog(path=tmp_path / "events.jsonl")
    n = sum(1 for e in log.replay(task_id=tid) if e.get("idempotency_key") == "k1")
    assert n == 1


def test_deliverables_endpoint(client):
    pid = _project(client)
    tid = _task(client, pid)
    r = client.post(f"/tasks/{tid}/deliverables", headers=H1, params={"file_ref": "out.txt"})
    assert r.status_code == 200


def test_heartbeat_in_audit(client):
    pid = _project(client)
    tid = _task(client, pid)
    client.post(f"/tasks/{tid}/heartbeat", headers=H1, params={"agent": "lobster"})
    types = [e["event_type"] for e in client.get(f"/projects/{pid}/audit", headers=H1).json()["events"]]
    assert "agent.heartbeat" in types


def test_transfer_idempotent(client):
    """同 transfer 请求 → 同 ID + 去重（唯一 ID ≠ 幂等，深修1）。"""
    pid = _project(client)
    tid = _task(client, pid)
    r1 = client.post("/agents/transfer", headers=H3,
                     json={"agent_id": "lobster", "project_id": pid, "task_id": tid,
                           "via": "message"}).json()
    r2 = client.post("/agents/transfer", headers=H3,
                     json={"agent_id": "lobster", "project_id": pid, "task_id": tid,
                           "via": "message"}).json()
    assert r1["transfer_id"] == r2["transfer_id"]          # 同请求同 ID
    # 不同 payload → 不同 ID
    r3 = client.post("/agents/transfer", headers=H3,
                     json={"agent_id": "lobster", "project_id": pid, "task_id": tid,
                           "payload": {"x": 1}, "via": "message"}).json()
    assert r1["transfer_id"] != r3["transfer_id"]


def test_transfer_permission_gate(client):
    pid = _project(client)
    tid = _task(client, pid)
    r = client.post("/agents/transfer", headers=H1,   # L1 < L2 → 403
                    json={"agent_id": "lobster", "project_id": pid, "task_id": tid,
                          "via": "message"})
    assert r.status_code == 403


def test_review_approval_binding(client):
    """复核/审批需先建对象，决策绑定任务，任意 ID 拒绝（深修2）。"""
    pid = _project(client)
    tid = _task(client, pid)
    # 任意 ID 决策 → 404（approval 用 L3 过权限后仍 404）
    assert client.post("/reviews/arbitrary/decision", headers=H1, params={"verdict": "pass"}).status_code == 404
    assert client.post("/approvals/arbitrary/decision", headers=H3,
                       params={"result": "approve"}).status_code == 404
    # 低权限审批 → 403
    assert client.post("/approvals/arbitrary/decision", headers=H1,
                       params={"result": "approve"}).status_code == 403
    # 建复核/审批对象 → 决策 → 事件入项目审计
    rid = client.post(f"/tasks/{tid}/reviews", headers=H1).json()["review_id"]
    aid = client.post(f"/tasks/{tid}/approvals", headers=H3, params={"scope": "flow"}).json()["approval_id"]
    client.post(f"/reviews/{rid}/decision", headers=H1, params={"verdict": "pass"})
    client.post(f"/approvals/{aid}/decision", headers=H3, params={"result": "approve"})
    types = [e["event_type"] for e in client.get(f"/projects/{pid}/audit", headers=H1).json()["events"]]
    assert "review.decided" in types and "approval.decided" in types


def test_approval_terminal_and_gate(client):
    """审批终态（二次决策 400）+ 审批后动作放行（阻塞3）。"""
    pid = _project(client)
    tid = _task(client, pid)
    # 建 pause 审批 → 批准 → pause 放行
    aid = client.post(f"/tasks/{tid}/approvals", headers=H3, params={"scope": "pause_project"}).json()["approval_id"]
    assert client.post(f"/approvals/{aid}/decision",
                       headers=H3, params={"result": "approve"}).status_code == 200
    # 已批准 → pause 200
    assert client.post(f"/projects/{pid}/pause", headers=H3).status_code == 200
    # 无审批的另一个项目 → pause 409 + 自动建审批请求（审批门通用化）
    pid2 = _project(client)
    r = client.post(f"/projects/{pid2}/pause", headers=H3)
    assert r.status_code == 409
    assert r.json().get("approval_id")
    # 同一审批二次决策 → 400（终态）
    assert client.post(f"/approvals/{aid}/decision",
                       headers=H3, params={"result": "reject"}).status_code == 400


def test_approval_gate_general_409(client):
    """审批门通用化：L3 动作未批准 → 409 + approval_id（而非专用 403）。"""
    pid = _project(client)
    r = client.post(f"/projects/{pid}/pause", headers=H3)
    assert r.status_code == 409
    aid = r.json()["approval_id"]
    # 批准后放行
    assert client.post(f"/approvals/{aid}/decision",
                       headers=H3, params={"result": "approve"}).status_code == 200
    assert client.post(f"/projects/{pid}/pause", headers=H3).status_code == 200


def test_request_approval_to_pending_approval(client):
    """审批门通用化：任务 IN_REVIEW 请求审批 → PENDING_APPROVAL。"""
    pid = _project(client)
    tid = _task(client, pid)
    client.patch(f"/tasks/{tid}/state", headers=H1, params={"to": "in_progress"})
    client.patch(f"/tasks/{tid}/state", headers=H1, params={"to": "in_review"})
    client.post(f"/tasks/{tid}/approvals", headers=H3, params={"scope": "flow_change"})
    tos = [e["payload"]["to"] for e in client.get(f"/projects/{pid}/audit", headers=H1).json()["events"]
           if e["event_type"] == "task.state_changed"]
    assert tos[-1] == "pending_approval"


def test_approval_decision_outcome(client):
    """审批门通用化：批准→COMPLETED；拒绝→退回 IN_PROGRESS。"""
    pid = _project(client)
    tid = _task(client, pid)
    client.patch(f"/tasks/{tid}/state", headers=H1, params={"to": "in_progress"})
    client.patch(f"/tasks/{tid}/state", headers=H1, params={"to": "in_review"})
    aid = client.post(f"/tasks/{tid}/approvals", headers=H3,
                      params={"scope": "flow_change"}).json()["approval_id"]
    assert client.post(f"/approvals/{aid}/decision",
                       headers=H3, params={"result": "approve"}).status_code == 200
    ctx = client.get(f"/tasks/{tid}/context", headers=H1).json()
    assert ctx["status"] == "completed"
    # 拒绝分支
    tid2 = _task(client, pid)
    client.patch(f"/tasks/{tid2}/state", headers=H1, params={"to": "in_progress"})
    client.patch(f"/tasks/{tid2}/state", headers=H1, params={"to": "in_review"})
    aid2 = client.post(f"/tasks/{tid2}/approvals", headers=H3,
                       params={"scope": "flow_change"}).json()["approval_id"]
    assert client.post(f"/approvals/{aid2}/decision",
                       headers=H3, params={"result": "reject"}).status_code == 200
    ctx2 = client.get(f"/tasks/{tid2}/context", headers=H1).json()
    assert ctx2["status"] == "in_progress"


def test_auth_required_401(client):
    """真实鉴权：无 token 或无效 token → 401（Fix 4）。"""
    pid = _project(client)
    tid = _task(client, pid)
    assert client.patch(f"/tasks/{tid}/state", params={"to": "in_progress"}).status_code == 401
    assert client.patch(f"/tasks/{tid}/state",
                        headers={"Authorization": "Bearer bad-token"},
                        params={"to": "in_progress"}).status_code == 401


def test_idempotent_retry(client):
    """复核/审批请求重试返回同一 ID；审批同 result 重试幂等 200（幂等重试）。"""
    pid = _project(client)
    tid = _task(client, pid)
    r1 = client.post(f"/tasks/{tid}/reviews", headers=H1).json()
    r2 = client.post(f"/tasks/{tid}/reviews", headers=H1).json()   # 重试
    assert r1["review_id"] == r2["review_id"]
    a1 = client.post(f"/tasks/{tid}/approvals", headers=H3, params={"scope": "pause_project"}).json()
    a2 = client.post(f"/tasks/{tid}/approvals", headers=H3, params={"scope": "pause_project"}).json()
    assert a1["approval_id"] == a2["approval_id"]
    # 同 result 重试 → 200 幂等
    assert client.post(f"/approvals/{a1['approval_id']}/decision",
                       headers=H3, params={"result": "approve"}).status_code == 200
    assert client.post(f"/approvals/{a1['approval_id']}/decision",
                       headers=H3, params={"result": "approve"}).status_code == 200


def test_state_transition_idempotent(client):
    """state 同状态重试 → 200 幂等（P1）。"""
    pid = _project(client)
    tid = _task(client, pid)
    assert client.patch(f"/tasks/{tid}/state", headers=H1, params={"to": "in_progress"}).status_code == 200
    assert client.patch(f"/tasks/{tid}/state", headers=H1, params={"to": "in_progress"}).status_code == 200


def test_idempotency_same_key_diff_to_409(client):
    """P1 反例：同幂等键 + 不同 to → 409（不再静默丢写返回假 200）。"""
    pid = _project(client)
    tid = _task(client, pid)
    assert client.patch(f"/tasks/{tid}/state", headers=H1,
                        params={"to": "in_progress", "idempotency_key": "pk1"}).status_code == 200
    r = client.patch(f"/tasks/{tid}/state", headers=H1,
                     params={"to": "blocked", "idempotency_key": "pk1"})
    assert r.status_code == 409
    assert "幂等键" in r.json()["detail"]


def test_idempotency_same_key_same_to_200(client):
    """P1 回归：同幂等键 + 同 to 重试 → 200 幂等。"""
    pid = _project(client)
    tid = _task(client, pid)
    r1 = client.patch(f"/tasks/{tid}/state", headers=H1,
                      params={"to": "in_progress", "idempotency_key": "pk2"})
    r2 = client.patch(f"/tasks/{tid}/state", headers=H1,
                      params={"to": "in_progress", "idempotency_key": "pk2"})
    assert r1.status_code == 200 and r2.status_code == 200


def test_idempotency_same_content_event_layer(client, tmp_path):
    """事件层幂等：同 key 同内容（deliverables 幂等键）→ 仍只落 1 条，不抛冲突。"""
    pid = _project(client)
    tid = _task(client, pid)
    for _ in range(2):
        assert client.post(f"/tasks/{tid}/deliverables", headers=H1,
                           params={"file_ref": "out.txt"}).status_code == 200
    log = EventLog(path=tmp_path / "events.jsonl")
    n = sum(1 for e in log.replay(task_id=tid)
            if e["event_type"] == "deliverable.submitted")
    assert n == 1


def test_list_projects(client):
    """看板数据：GET /projects 从事件溯源推导项目列表。"""
    pid = _project(client)
    r = client.get("/projects", headers=H1)
    assert r.status_code == 200
    projects = r.json()["projects"]
    assert any(p["project_id"] == pid for p in projects)
    assert any(p["status"] == "active" for p in projects)


def test_list_tasks(client):
    """看板数据：GET /projects/{id}/tasks 推导任务状态/owner/has_deliverable。"""
    pid = _project(client)
    tid = client.post(f"/projects/{pid}/tasks", headers=H1,
                      params={"title": "看板任务", "owner_agent": "ng-assistant"}).json()["task_id"]
    client.patch(f"/tasks/{tid}/state", headers=H1, params={"to": "in_progress"})
    client.post(f"/tasks/{tid}/deliverables", headers=H1, params={"file_ref": "d.md"})
    tasks = client.get(f"/projects/{pid}/tasks", headers=H1).json()["tasks"]
    t = next(x for x in tasks if x["task_id"] == tid)
    assert t["title"] == "看板任务"
    assert t["owner"] == "ng-assistant"
    assert t["status"] == "in_review"      # deliverable done → in_review
    assert t["has_deliverable"] is True


def test_deliverable_auto_advance_to_review(client):
    """闭环：Agent 提交产出(verdict=done) → 自动推进 in_progress→in_review（交接复核）。"""
    pid = _project(client)
    tid = _task(client, pid)
    client.patch(f"/tasks/{tid}/state", headers=H1, params={"to": "in_progress"})
    r = client.post(f"/tasks/{tid}/deliverables", headers=H1,
                    params={"file_ref": "a.md", "summary": "完成", "verdict": "done"})
    assert r.status_code == 200
    assert r.json()["status"] == "in_review"
    types = [e["event_type"] for e in client.get(f"/projects/{pid}/audit", headers=H1).json()["events"]]
    assert "task.state_changed" in types


def test_deliverable_verdict_blocked(client):
    """闭环：Agent 报告阻塞(verdict=blocked) → 自动推进 in_progress→blocked。"""
    pid = _project(client)
    tid = _task(client, pid)
    client.patch(f"/tasks/{tid}/state", headers=H1, params={"to": "in_progress"})
    r = client.post(f"/tasks/{tid}/deliverables", headers=H1,
                    params={"file_ref": "b.md", "verdict": "blocked"})
    assert r.json()["status"] == "blocked"


def test_deliverable_illegal_verdict(client):
    """非法 verdict → 400。"""
    pid = _project(client)
    tid = _task(client, pid)
    r = client.post(f"/tasks/{tid}/deliverables", headers=H1,
                    params={"file_ref": "c.md", "verdict": "wat"})
    assert r.status_code == 400


def test_transfer_escalation_stale(client, tmp_path):
    """主动推进：转移超时无产出回报 → task.blocked 升级（不依赖 Agent 记得回来干活）。"""
    from app.storage.event_log import EventLog
    from app.workers.transfer_escalation import TransferEscalationWorker
    pid = _project(client)
    tid = _task(client, pid)
    log = EventLog(path=tmp_path / "events.jsonl")   # 与 fixture 同文件
    tf_dir = tmp_path / "xfer"
    tf_dir.mkdir()
    (tf_dir / "ng-platform-lobster-transfer-test.md").write_text(
        f"---\nfrom: ng-platform\nto: lobster\nstatus: unread\n"
        f"created_at: 2026-08-31T00:00:00+0800\n"
        f'transfer: {{"transfer_id": "ng-test-lobster", "target_project": "{pid}", '
        f'"target_task": "{tid}", "payload": {{}}}}\n'
        f"---\n\nbody\n", encoding="utf-8")
    w = TransferEscalationWorker(log, transfer_dir=tf_dir, stale_timeout_s=0)
    w.scan()
    types = [e["event_type"] for e in log.replay(task_id=tid)]
    assert "task.blocked" in types, f"应升级，实际 {types}"


def test_transfer_escalation_skip_if_result(client, tmp_path):
    """已有产出回报的任务不升级。"""
    from app.storage.event_log import EventLog
    from app.workers.transfer_escalation import TransferEscalationWorker
    pid = _project(client)
    tid = _task(client, pid)
    client.patch(f"/tasks/{tid}/state", headers=H1, params={"to": "in_progress"})
    client.post(f"/tasks/{tid}/deliverables", headers=H1, params={"file_ref": "d.md"})
    log = EventLog(path=tmp_path / "events.jsonl")
    tf_dir = tmp_path / "xfer2"
    tf_dir.mkdir()
    (tf_dir / "ng-platform-lobster-transfer-test2.md").write_text(
        f"---\nfrom: ng-platform\nto: lobster\nstatus: unread\n"
        f"created_at: 2026-08-31T00:00:00+0800\n"
        f'transfer: {{"transfer_id": "ng-test2", "target_project": "{pid}", '
        f'"target_task": "{tid}", "payload": {{}}}}\n'
        f"---\n\nbody\n", encoding="utf-8")
    w = TransferEscalationWorker(log, transfer_dir=tf_dir, stale_timeout_s=0)
    w.scan()
    types = [e["event_type"] for e in log.replay(task_id=tid)]
    assert "task.blocked" not in types


def test_adapter_executor_interface():
    """执行层抽象：openclaw + claude_sdk 都实现 AgentExecutor（模型中立，执行层可替换）。"""
    from app.adapters.base import AgentExecutor
    from app.adapters.openclaw import OpenClawExecutor
    from app.adapters.claude_sdk import ClaudeSDKExecutor
    for ex_cls in (OpenClawExecutor, ClaudeSDKExecutor):
        assert issubclass(ex_cls, AgentExecutor)
        ex = ex_cls()
        assert callable(ex.dispatch) and callable(ex.collect_results)


def test_claude_sdk_executor_dispatches():
    """ClaudeSDKExecutor：mock client → dispatch 产出 done（model/thinking 正确）。"""
    from app.adapters.base import AgentTask
    from app.adapters.claude_sdk import ClaudeSDKExecutor
    class _FakeBlock:
        type = "text"
        text = "# 交付文档\n内容"
    class _FakeResp:
        content = [_FakeBlock()]
    class _FakeClient:
        class _Messages:
            def create(self, **kw):
                assert kw["model"] == "claude-opus-4-8"
                assert kw["thinking"] == {"type": "adaptive"}
                assert kw["output_config"]["effort"] == "medium"
                return _FakeResp()
        messages = _Messages()
    ex = ClaudeSDKExecutor(client=_FakeClient())
    r = ex.dispatch(AgentTask(agent_id="ng-assistant", task_id="t1",
                              project_id="p1", prompt="写文档"))
    assert r.status == "done"
    assert "交付文档" in r.output


def test_claude_sdk_executor_failure():
    """ClaudeSDKExecutor：client 异常 → dispatch 返回 failed（不抛出）。"""
    from app.adapters.base import AgentTask
    from app.adapters.claude_sdk import ClaudeSDKExecutor
    class _FakeClient:
        class _Messages:
            def create(self, **kw):
                raise RuntimeError("boom")
        messages = _Messages()
    ex = ClaudeSDKExecutor(client=_FakeClient())
    r = ex.dispatch(AgentTask(agent_id="a", task_id="t2", project_id="p", prompt="x"))
    assert r.status == "failed"
    assert "boom" in r.error


def test_llm_client_parse_json():
    """算力层：LLMClient parse_json 用注入的 http_post（mock，不烧 token）。"""
    from app.services.llm import LLMClient, LLMConfig
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append(json)
        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {"content": [{"type": "text", "text": '{"a": 1}'}]}
        return R()

    c = LLMClient(LLMConfig(provider="anthropic", api_key="k"), http_post=fake_post)
    assert c.parse_json("sys", "usr") == {"a": 1}
    assert c.usage() and c.usage()[0]["provider"] == "anthropic"
    assert calls[0]["model"] and calls[0]["messages"]


def test_llm_client_strict_no_key():
    """算力层：严格模式缺 key → LLMConfigError（P0 密钥契约延伸）。"""
    from app.services.llm import LLMClient, LLMConfig, LLMConfigError
    c = LLMClient(LLMConfig(provider="anthropic", api_key="", strict=True))
    with pytest.raises(LLMConfigError):
        c.complete("s", "u")


def test_agent_register_and_list(client):
    """Agent 注册中心：注册/更新（latest wins）+ 列表重建。"""
    client.post("/agents/register", headers=H1, params={"name": "lobster", "capability": "文档"})
    client.post("/agents/register", headers=H1, params={"name": "lobster", "capability": "分析,文档", "role": "ops"})
    agents = client.get("/agents", headers=H1).json()["agents"]
    assert sum(1 for a in agents if a["name"] == "lobster") == 1
    lobster = next(a for a in agents if a["name"] == "lobster")
    assert lobster["capability"] == "分析,文档" and lobster["role"] == "ops"


def test_message_parse_creates_tasks(client, monkeypatch):
    """前门：用户给目标 → 需求解析(LLM mock) → 团队匹配 → 自动建任务+责任链。"""
    class _FakeLLM:
        def usage(self): return []
        def parse_json(self, system, user, **kw):
            return {"summary": "写 BTC 周报",
                    "tasks": [{"title": "分析 BTC 周走势", "description": "看数据",
                               "deliverables": ["docs/btc-week.md"], "owner_hint": "lobster"}]}

    monkeypatch.setattr("app.main.LLMClient", lambda: _FakeLLM())
    pid = _project(client)
    client.post("/agents/register", headers=H1, params={"name": "lobster", "capability": "文档 分析"})
    r = client.post(f"/projects/{pid}/messages", headers=H1,
                    params={"body": "帮我分析 BTC 周走势并写文档", "parse": "true"})
    assert r.status_code == 200
    tasks = r.json()["created_tasks"]
    assert len(tasks) == 1
    assert tasks[0]["title"] == "分析 BTC 周走势"
    assert tasks[0]["owner"] == "lobster"          # 匹配到已注册 agent
    types = [e["event_type"] for e in client.get(f"/projects/{pid}/audit", headers=H1).json()["events"]]
    assert "goal.parsed" in types and "task.created" in types and "agent.assigned" in types


def test_message_parse_llm_unconfigured(client, monkeypatch):
    """算力未配置 → parse=true 返回 503 清晰报错（消息本身仍已记录）。"""
    from app.services.llm import LLMConfigError

    def _boom():
        raise LLMConfigError("算力未配置")
    monkeypatch.setattr("app.main.LLMClient", _boom)
    pid = _project(client)
    r = client.post(f"/projects/{pid}/messages", headers=H1,
                    params={"body": "x", "parse": "true"})
    assert r.status_code == 503
    assert "算力未配置" in r.json()["detail"]


def test_parse_retry_succeeds_on_second_attempt(client, monkeypatch):
    """龙虾汇总#2：LLM 解析第一次失败第二次成功 → 重试生效不抛错。"""
    calls = {"n": 0}
    class _FlakyLLM:
        def usage(self): return []
        def parse_json(self, system, user, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("LLM 临时故障")
            return {"summary": "ok", "tasks": [
                {"title": "T", "description": "d"}]}
    monkeypatch.setattr("app.main.LLMClient", lambda: _FlakyLLM())
    pid = _project(client)
    r = client.post(f"/projects/{pid}/messages", headers=H1,
                    params={"body": "做点事", "parse": "true"})
    assert r.status_code == 200, r.json()
    assert calls["n"] == 2   # 重试了一次
    assert len(r.json()["created_tasks"]) == 1


def test_message_parse_failed_event_recorded(client, monkeypatch):
    """龙虾汇总#2：解析失败写 goal.parse_failed 事件（审计可查，不静默）。"""
    from app.services.llm import LLMConfigError

    def _boom():
        raise LLMConfigError("算力未配置")
    monkeypatch.setattr("app.main.LLMClient", _boom)
    pid = _project(client)
    client.post(f"/projects/{pid}/messages", headers=H1,
                params={"body": "x", "parse": "true"})
    audit = client.get(f"/projects/{pid}/audit", headers=H1).json()["events"]
    assert any(e["event_type"] == "goal.parse_failed" for e in audit)


def test_builtin_agent_executes(tmp_path):
    """NG 自研 agent：用算力产出交付物并落盘（mock LLM）。"""
    from app.agents.builtin import BuiltinAgent, TaskContext
    class _FakeLLM:
        def usage(self): return []
        def complete(self, system, user, **kw):
            return "# 交付文档\n\n内容"
    agent = BuiltinAgent(llm=_FakeLLM(), artifacts_dir=tmp_path / "artifacts")
    r = agent.execute(TaskContext(task_id="t1", title="写周报",
                                  description="写份周报", deliverables=["docs/week.md"]))
    assert r["file_ref"] == "artifacts/t1.md"
    assert r["content_len"] > 0
    assert (tmp_path / "artifacts" / "t1.md").exists()
    assert "交付文档" in (tmp_path / "artifacts" / "t1.md").read_text(encoding="utf-8")


def test_task_context_endpoint(client):
    """任务上下文端点：agent 执行前读 title/desc/status。"""
    pid = _project(client)
    tid = _task(client, pid, description="分析 BTC 数据")
    client.patch(f"/tasks/{tid}/state", headers=H1, params={"to": "in_progress"})
    ctx = client.get(f"/tasks/{tid}/context", headers=H1).json()
    assert ctx["title"] == "T"
    assert ctx["description"] == "分析 BTC 数据"
    assert ctx["status"] == "in_progress"


def test_builtin_agent_closed_loop(client, tmp_path):
    """自研 agent 全链：产出 → 回报平台 → 自动交接复核（in_review）。"""
    from app.agents.builtin import BuiltinAgent, TaskContext
    class _FakeLLM:
        def usage(self): return []
        def complete(self, system, user, **kw):
            return "# 交付\n内容"
    pid = _project(client)
    tid = _task(client, pid)
    client.patch(f"/tasks/{tid}/state", headers=H1, params={"to": "in_progress"})
    result = BuiltinAgent(llm=_FakeLLM(), artifacts_dir=tmp_path / "artifacts").execute(
        TaskContext(task_id=tid, title="T"))
    r = client.post(f"/tasks/{tid}/deliverables", headers=H1,
                    params={"file_ref": result["file_ref"], "summary": result["summary"],
                            "verdict": "done", "agent": "ng-assistant"})
    assert r.status_code == 200
    assert r.json()["status"] == "in_review"


def test_auto_agent_worker_executes_builtin_task(client, tmp_path, monkeypatch):
    """常驻 agent worker：自动认领 builtin 任务 → 产出 → 自动 in_review。"""
    from app.storage.event_log import EventLog
    from app.workers.auto_agent import AutoAgentWorker
    client.post("/agents/register", headers=H1,
                params={"name": "ng-assistant", "capability": "通用", "executor": "builtin"})
    pid = _project(client)
    tid = client.post(f"/projects/{pid}/tasks", headers=H1,
                      params={"title": "T", "description": "写文档", "owner_agent": "ng-assistant"}).json()["task_id"]
    client.patch(f"/tasks/{tid}/state", headers=H1, params={"to": "in_progress"})

    class _FakeAgent:
        def __init__(self, *a, **kw): pass
        def execute(self, task):
            return {"file_ref": f"artifacts/{task.task_id}.md", "summary": "产出", "content_len": 10}
    monkeypatch.setattr("app.workers.auto_agent.BuiltinAgent", _FakeAgent)
    log = EventLog(path=tmp_path / "events.jsonl")
    AutoAgentWorker(log).tick([tid])
    types = [e["event_type"] for e in log.replay(task_id=tid)]
    assert "deliverable.submitted" in types
    assert "review.requested" in types          # P1：done 自动触发复核
    tos = [e["payload"]["to"] for e in log.replay(task_id=tid)
           if e["event_type"] == "task.state_changed"]
    assert tos[-1] == "in_review"


def test_list_templates(client):
    """preagent 模板库：返回预置专业 agent（含用户指定的税务/Excel/PPT/数据库）。"""
    templates = client.get("/agents/templates", headers=H1).json()["templates"]
    ids = {t["id"] for t in templates}
    for expected in ("tax-advisor", "excel-expert", "ppt-author", "database-expert"):
        assert expected in ids


def test_instantiate_template(client):
    """一键注册 preagent 模板 → 平台 agent（英文名，可匹配派活）。"""
    r = client.post("/agents/templates/excel-expert/instantiate", headers=H1)
    assert r.status_code == 200
    assert r.json()["name"] == "Excel专家"
    agents = client.get("/agents", headers=H1).json()["agents"]
    excel = next((a for a in agents if a["name"] == "Excel专家"), None)
    assert excel is not None
    assert excel["executor"] == "builtin"


def test_llm_config_save_openai(client, monkeypatch):
    """算力配置：选 openai + 输 key → 正确映射写 .env（monkeypatch 不污染真 .env）。"""
    captured = {}
    monkeypatch.setattr("app.main._write_env_updates", lambda u: captured.update(u))
    r = client.post("/agents/llm-config", headers=H1,
                    json={"provider": "openai", "api_key": "sk-test", "model": "gpt-4o"})
    assert r.status_code == 200
    assert captured.get("LLM_PROVIDER") == "openai"
    assert captured.get("OPENAI_API_KEY") == "sk-test"
    assert captured.get("LLM_MODEL") == "gpt-4o"


def test_llm_config_deepseek_mapping(client, monkeypatch):
    captured = {}
    monkeypatch.setattr("app.main._write_env_updates", lambda u: captured.update(u))
    r = client.post("/agents/llm-config", headers=H1,
                    json={"provider": "deepseek", "api_key": "sk-ds"})
    assert r.status_code == 200
    assert captured.get("LLM_PROVIDER") == "openai_compatible"
    assert captured.get("LLM_BASE_URL") == "https://api.deepseek.com/v1"
    assert captured.get("LLM_MODEL") == "deepseek-chat"


def test_llm_config_invalid_provider(client):
    r = client.post("/agents/llm-config", headers=H1, json={"provider": "wat"})
    assert r.status_code == 400


def test_llm_config_get(client):
    r = client.get("/agents/llm-config", headers=H1)
    assert r.status_code == 200
    assert "api_key_set" in r.json()


def test_feedback_submit_and_list(client):
    """反馈入口：提交 → feedback.submitted 事件 → owner 可查。"""
    r = client.post("/feedback", headers=H1, json={"content": "界面很漂亮", "contact": "wx"})
    assert r.status_code == 200
    items = client.get("/feedback", headers=H1).json()["feedback"]
    assert any(i["content"] == "界面很漂亮" and i["contact"] == "wx" for i in items)


def test_usage_get(client):
    """用量聚合：GET /usage 返回调用数/token/1M 上下文限制。"""
    r = client.get("/usage", headers=H1)
    assert r.status_code == 200
    d = r.json()
    assert d["context_limit"] == 1_000_000
    assert "calls" in d and "input_tokens" in d and "output_tokens" in d


def test_feedback_empty_rejected(client):
    r = client.post("/feedback", headers=H1, json={"content": "   "})
    assert r.status_code == 400


def test_deactivate_agent(client):
    """移除 agent：deactivate → latest-wins status=disabled（可再添加）。"""
    client.post("/agents/templates/tax-advisor/instantiate", headers=H1)
    r = client.post("/agents/税务顾问/deactivate", headers=H1)
    assert r.status_code == 200
    assert r.json()["status"] == "disabled"
    agents = client.get("/agents", headers=H1).json()["agents"]
    advisor = next((a for a in agents if a["name"] == "税务顾问"), None)
    assert advisor["status"] == "disabled"


def test_auto_agent_is_builtin_latest_wins(client, tmp_path, monkeypatch):
    """P0：_is_builtin 取最新注册（先 builtin 后 openclaw → 判 openclaw，不误执行）。"""
    from app.storage.event_log import EventLog
    from app.workers.auto_agent import AutoAgentWorker
    client.post("/agents/register", headers=H1,
                params={"name": "ng-assistant", "capability": "通用", "executor": "builtin"})
    client.post("/agents/register", headers=H1,
                params={"name": "ng-assistant", "capability": "通用", "executor": "openclaw"})
    pid = _project(client)
    tid = client.post(f"/projects/{pid}/tasks", headers=H1,
                      params={"title": "T", "owner_agent": "ng-assistant"}).json()["task_id"]
    client.patch(f"/tasks/{tid}/state", headers=H1, params={"to": "in_progress"})
    monkeypatch.setattr("app.workers.auto_agent.BuiltinAgent", lambda: None)
    log = EventLog(path=tmp_path / "events.jsonl")
    w = AutoAgentWorker(log)
    assert w._is_builtin("ng-assistant") is False    # latest = openclaw
    w.tick([tid])
    types = [e["event_type"] for e in log.replay(task_id=tid)]
    assert "deliverable.submitted" not in types      # openclaw 不自动执行


def test_transfer_escalation_blocks_task(client, tmp_path):
    """P1：升级同时推进状态到 blocked（_task_stuck 才停，避免跨转移重复升级）。"""
    from app.storage.event_log import EventLog
    from app.workers.transfer_escalation import TransferEscalationWorker
    pid = _project(client)
    tid = _task(client, pid)
    log = EventLog(path=tmp_path / "events.jsonl")
    tf_dir = tmp_path / "xferb"
    tf_dir.mkdir()
    (tf_dir / "ng-platform-lobster-transfer-b.md").write_text(
        f"---\nfrom: ng-platform\nto: lobster\nstatus: unread\n"
        f"created_at: 2026-08-31T00:00:00+0800\n"
        f'transfer: {{"transfer_id": "ng-b1", "target_project": "{pid}", '
        f'"target_task": "{tid}", "payload": {{}}}}\n'
        f"---\n\nbody\n", encoding="utf-8")
    TransferEscalationWorker(log, transfer_dir=tf_dir, stale_timeout_s=0).scan()
    tos = [e["payload"]["to"] for e in log.replay(task_id=tid)
           if e["event_type"] == "task.state_changed"]
    assert tos[-1] == "blocked", f"应推进 blocked，实际 {tos}"
    # 再扫一轮：已 blocked → _task_stuck False → 不再重复升级
    TransferEscalationWorker(log, transfer_dir=tf_dir, stale_timeout_s=0).scan()
    blocked_count = sum(1 for e in log.replay(task_id=tid)
                        if e["event_type"] == "task.blocked")
    assert blocked_count == 1


def test_deliverable_done_triggers_review(client):
    """P1：deliverable done 自动 in_review 时同时触发 review.requested。"""
    pid = _project(client)
    tid = _task(client, pid)
    client.patch(f"/tasks/{tid}/state", headers=H1, params={"to": "in_progress"})
    r = client.post(f"/tasks/{tid}/deliverables", headers=H1,
                    params={"file_ref": "x.md", "verdict": "done"})
    assert r.json()["status"] == "in_review"
    types = [e["event_type"] for e in client.get(f"/projects/{pid}/audit", headers=H1).json()["events"]]
    assert "review.requested" in types


def test_auto_agent_worker_skips_openclaw_owner(client, tmp_path, monkeypatch):
    """常驻 agent worker：openclaw 归属的任务不自动执行（留给外接执行方）。"""
    from app.storage.event_log import EventLog
    from app.workers.auto_agent import AutoAgentWorker
    client.post("/agents/register", headers=H1,
                params={"name": "lobster", "capability": "通用", "executor": "openclaw"})
    pid = _project(client)
    tid = client.post(f"/projects/{pid}/tasks", headers=H1,
                      params={"title": "T", "owner_agent": "lobster"}).json()["task_id"]
    client.patch(f"/tasks/{tid}/state", headers=H1, params={"to": "in_progress"})
    monkeypatch.setattr("app.workers.auto_agent.BuiltinAgent", lambda: None)
    log = EventLog(path=tmp_path / "events.jsonl")
    AutoAgentWorker(log).tick([tid])
    types = [e["event_type"] for e in log.replay(task_id=tid)]
    assert "deliverable.submitted" not in types


def test_auth_production_refuses_defaults():
    """P0 密钥契约：生产/严格模式拒绝不安全默认 token。"""
    from app.security.auth import resolve_tokens
    with pytest.raises(RuntimeError):
        resolve_tokens({"NG_ENV": "production"})   # 无 token → 拒绝启动
    with pytest.raises(RuntimeError):
        resolve_tokens({"NG_ENV": "production",
                        "NG_LEVEL3_TOKEN": "l3-test-token",   # 不安全默认
                        "NG_LEVEL1_TOKEN": "a" * 32})
    tok = resolve_tokens({"NG_ENV": "production",
                          "NG_LEVEL3_TOKEN": "a" * 32, "NG_LEVEL1_TOKEN": "b" * 32})
    assert ("admin", 3) in tok.values() and ("agent", 1) in tok.values()
    # 非严格 dev：无 token 也有本地 fallback
    assert "l3-test-token" in resolve_tokens({})


def test_review_needs_changes_returns_to_in_progress(client):
    """汇总#3：复核 needs_changes → 任务退回 in_progress（返工通道，不卡死）。"""
    pid = _project(client)
    tid = _task(client, pid)
    client.patch(f"/tasks/{tid}/state", headers=H1, params={"to": "in_progress"})
    client.post(f"/tasks/{tid}/deliverables", headers=H1,
                params={"file_ref": "docs/n.md", "verdict": "done"})  # → in_review
    rid = client.post(f"/tasks/{tid}/reviews", headers=H1).json()["review_id"]
    assert client.post(f"/reviews/{rid}/decision", headers=H3,
                       params={"verdict": "needs_changes"}).status_code == 200
    ctx = client.get(f"/tasks/{tid}/context", headers=H1).json()
    assert ctx["status"] == "in_progress", f"needs_changes 应退回 in_progress，实际 {ctx['status']}"


def test_review_reject_returns_to_in_progress(client):
    """汇总#3：复核 reject → 任务退回 in_progress（打回重做）。"""
    pid = _project(client)
    tid = _task(client, pid)
    client.patch(f"/tasks/{tid}/state", headers=H1, params={"to": "in_progress"})
    client.post(f"/tasks/{tid}/deliverables", headers=H1,
                params={"file_ref": "docs/rj.md", "verdict": "done"})
    rid = client.post(f"/tasks/{tid}/reviews", headers=H1).json()["review_id"]
    assert client.post(f"/reviews/{rid}/decision", headers=H3,
                       params={"verdict": "reject"}).status_code == 200
    ctx = client.get(f"/tasks/{tid}/context", headers=H1).json()
    assert ctx["status"] == "in_progress"


def test_rework_closed_loop_second_review(client):
    """汇总v1.1 ②③：needs_changes 打回→重交→二次复核→pass→completed 闭环。"""
    pid = _project(client)
    tid = _task(client, pid)
    client.patch(f"/tasks/{tid}/state", headers=H1, params={"to": "in_progress"})
    # 首交 → in_review → needs_changes 打回
    client.post(f"/tasks/{tid}/deliverables", headers=H1,
                params={"file_ref": "docs/v1.md", "verdict": "done"})
    rid1 = client.post(f"/tasks/{tid}/reviews", headers=H1).json()["review_id"]
    assert client.post(f"/reviews/{rid1}/decision", headers=H3,
                       params={"verdict": "needs_changes"}).status_code == 200
    # 打回 in_progress
    ctx = client.get(f"/tasks/{tid}/context", headers=H1).json()
    assert ctx["status"] == "in_progress", f"打回应回 in_progress，实际 {ctx['status']}"
    # 重交 v2 → 应能开新 review（二次复核）
    client.patch(f"/tasks/{tid}/state", headers=H1, params={"to": "in_progress"})  # 已在 in_progress
    r = client.post(f"/tasks/{tid}/deliverables", headers=H1,
                    params={"file_ref": "docs/v2.md", "verdict": "done"})
    assert r.status_code == 200
    ctx2 = client.get(f"/tasks/{tid}/context", headers=H1).json()
    assert ctx2["status"] == "in_review", f"重交应回 in_review，实际 {ctx2['status']}"
    # 二次复核 pass → completed
    rid2 = client.post(f"/tasks/{tid}/reviews", headers=H1).json()["review_id"]
    assert rid2 != rid1, "二次复核应新建 review_id"
    assert client.post(f"/reviews/{rid2}/decision", headers=H3,
                       params={"verdict": "pass"}).status_code == 200
    ctx3 = client.get(f"/tasks/{tid}/context", headers=H1).json()
    assert ctx3["status"] == "completed", f"pass 应 completed，实际 {ctx3['status']}"
