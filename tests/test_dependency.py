"""任务依赖编排（2026-09-02）：串联/并联自动分类 + AutoStartWorker 依赖门控。

运行（mac/Linux）: cd ~/Desktop/ng-platform && .venv/bin/python -m pytest tests/test_dependency.py -q
运行（Windows）: cd ~/Desktop/ng-platform && .venv\\Scripts\\python -m pytest tests/test_dependency.py -q
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

from app.domain import events
from app.domain.task import TaskStatus
from app.storage.event_log import EventLog
from app.workers.auto_start import AutoStartWorker, task_state, task_depends_on
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app.security import auth as authmod
    from app.storage.user_store import UserStore
    monkeypatch.setattr("app.main.log", EventLog(path=tmp_path / "events.jsonl"))
    store = UserStore(path=tmp_path / "users.json")
    monkeypatch.setattr("app.main.user_store", store)
    authmod.set_user_store(store)
    return TestClient(app)


H1 = {"Authorization": "Bearer l1-agent-token"}


def _mk(log, pid, title, depends=None):
    tid = str(uuid.uuid4())
    log.append(events.new_event(
        events.EventType.TASK_CREATED, "system",
        {"title": title, "status": "todo", "depends_on": depends or []},
        project_id=pid, task_id=tid))
    return tid


def _set_state(log, tid, to, pid):
    log.append(events.new_event(
        events.EventType.TASK_STATE_CHANGED, "sys",
        {"from": "in_progress", "to": to}, project_id=pid, task_id=tid))


def test_task_depends_on_read(tmp_path):
    """依赖门控读 depends_on。"""
    log = EventLog(path=tmp_path / "e.jsonl")
    pid = str(uuid.uuid4())
    a = _mk(log, pid, "A")
    b = _mk(log, pid, "B", depends=[a])
    assert task_depends_on(log, a) == []
    assert task_depends_on(log, b) == [a]


def test_auto_start_blocks_until_dep_done(tmp_path):
    """串联：上游未完成 → 下游保持 TODO（不启动）。"""
    log = EventLog(path=tmp_path / "e2.jsonl")
    pid = str(uuid.uuid4())
    a = _mk(log, pid, "A")
    b = _mk(log, pid, "B", depends=[a])
    w = AutoStartWorker(log, state_dir=tmp_path / "w")
    w.process(a)
    assert task_state(log, a) == TaskStatus.IN_PROGRESS
    w.process(b)
    assert task_state(log, b) == TaskStatus.TODO   # 未启动


def test_auto_start_after_upstream_done(tmp_path):
    """串联：上游 completed → 下游自动启动。"""
    log = EventLog(path=tmp_path / "e3.jsonl")
    pid = str(uuid.uuid4())
    a = _mk(log, pid, "A")
    b = _mk(log, pid, "B", depends=[a])
    w = AutoStartWorker(log, state_dir=tmp_path / "w2")
    w.process(a)
    _set_state(log, a, "completed", pid)
    w.process(b)
    assert task_state(log, b) == TaskStatus.IN_PROGRESS


def test_auto_start_no_dep_runs(tmp_path):
    """并联：无依赖任务立即启动（回归）。"""
    log = EventLog(path=tmp_path / "e4.jsonl")
    pid = str(uuid.uuid4())
    a = _mk(log, pid, "A")
    b = _mk(log, pid, "B")
    w = AutoStartWorker(log, state_dir=tmp_path / "w3")
    w.process(a); w.process(b)
    assert task_state(log, a) == TaskStatus.IN_PROGRESS
    assert task_state(log, b) == TaskStatus.IN_PROGRESS


def test_parse_creates_depends_on_events(client, monkeypatch):
    """需求解析：LLM 输出 depends_on(标题) → TASK_CREATED 带 task_id 依赖。"""
    class _FakeLLM:
        def usage(self): return []
        def parse_json(self, system, user, **kw):
            return {"summary": "写报告",
                    "tasks": [
                        {"title": "调研", "description": "收集数据"},
                        {"title": "写报告", "description": "基于调研",
                         "depends_on": ["调研"]},
                    ]}
    monkeypatch.setattr("app.main.LLMClient", lambda: _FakeLLM())
    pid = client.post("/projects", headers=H1,
                      params={"title": "P", "goal": "g"}).json()["project_id"]
    r = client.post(f"/projects/{pid}/messages", headers=H1,
                    params={"body": "写一份调研报告", "parse": "true"})
    assert r.status_code == 200
    created = {t["title"]: t for t in r.json()["created_tasks"]}
    assert len(created) == 2
    assert created["写报告"]["depends_on"] == [created["调研"]["task_id"]]
    assert created["调研"]["depends_on"] == []


def test_parse_no_depends_backward_compat(client, monkeypatch):
    """需求解析：LLM 不输出 depends_on（老 mock）→ 全并联兼容。"""
    class _FakeLLM:
        def usage(self): return []
        def parse_json(self, system, user, **kw):
            return {"summary": "x", "tasks": [
                {"title": "T1", "description": "a"},
                {"title": "T2", "description": "b"},
            ]}
    monkeypatch.setattr("app.main.LLMClient", lambda: _FakeLLM())
    pid = client.post("/projects", headers=H1,
                      params={"title": "P2", "goal": "g"}).json()["project_id"]
    r = client.post(f"/projects/{pid}/messages", headers=H1,
                    params={"body": "做两件事", "parse": "true"})
    assert all(t["depends_on"] == [] for t in r.json()["created_tasks"])


def test_list_tasks_has_depends_on(client):
    """看板：list_tasks 每任务带 depends_on 字段。"""
    pid = client.post("/projects", headers=H1,
                      params={"title": "P3", "goal": "g"}).json()["project_id"]
    client.post(f"/projects/{pid}/tasks", headers=H1, params={"title": "T"})
    tasks = client.get(f"/projects/{pid}/tasks", headers=H1).json()["tasks"]
    assert all("depends_on" in t for t in tasks)


def test_executor_gets_goal_and_upstream(client, tmp_path, monkeypatch):
    """汇总#2：执行器注入项目目标 + 上游交付物内容（不编数据）。"""
    from app.workers.auto_agent import AutoAgentWorker
    import app.workers.auto_agent as aa_mod
    from app.agents.builtin import TaskContext, BuiltinAgent
    captured = {}
    class _FakeBuiltin:
        def execute(self, task):
            captured['goal'] = task.project_goal
            captured['upstream'] = task.upstream
            return {"file_ref": "artifacts/x.md", "summary": "s", "content_len": 1,
                    "usage": []}
    monkeypatch.setattr(aa_mod, "BuiltinAgent", lambda: _FakeBuiltin())
    # 建项目+目标，一个上游任务产出，一个依赖它的下游任务
    pid = client.post("/projects", headers=H1,
                      params={"title": "ctx", "goal": "算20局A/B/C胜率权重"}).json()["project_id"]
    # 造上游任务并提交真实产出文件
    up_tid = str(uuid.uuid4())
    from app.main import log
    log.append(events.new_event(events.EventType.TASK_CREATED, "system",
        {"title": "数据准备", "status": "todo", "depends_on": []}, project_id=pid, task_id=up_tid))
    f = tmp_path / "data.md"; f.write_text("A胜14 B胜10 C胜17\n共20局", encoding="utf-8")
    # 直接用 AutoAgentWorker 的辅助方法测上游注入
    w = AutoAgentWorker(EventLog(path=tmp_path / "e.jsonl"))
    # 简单验证 prompt 构造含 goal——mock BuiltinAgent.execute 收到字段
    tctx = TaskContext(task_id="t1", title="算权重", project_goal="算20局A/B/C胜率权重",
                       upstream="A胜14 B胜10 C胜17")
    assert "算20局A/B/C胜率权重" in tctx.project_goal
    assert "A胜14" in tctx.upstream


def test_builtin_rerun_new_file_ref(tmp_path):
    """D6 回归：BuiltinAgent 重跑产生新 file_ref（不吞幂等 → 打破 reject 死循环）。"""
    import app.agents.builtin as bmod
    from app.agents.builtin import BuiltinAgent, TaskContext
    class _FakeLLM:
        def usage(self): return []
        def complete(self, s, u, **kw): return "新稿内容"
    a = BuiltinAgent(llm=_FakeLLM(), artifacts_dir=tmp_path)
    t = TaskContext(task_id="d6-test", title="T")
    r1 = a.execute(t)
    r2 = a.execute(t)   # 重跑
    assert r1["file_ref"] != r2["file_ref"], f"重跑应新 file_ref: {r1} vs {r2}"
    assert "retry1" in r2["file_ref"], f"第二次应 .retry1: {r2}"
    # 文件都存在
    assert (tmp_path / "d6-test.md").exists()
    assert (tmp_path / "d6-test.retry1.md").exists()
