"""投影物化 + 乐观锁回归（P1-1，2026-09-01）。

DB 模式：事件插入同事务折叠投影读模型；tasks.expected_version 乐观锁。
运行（mac/Linux）: cd ~/Desktop/ng-platform && .venv/bin/python -m pytest tests/test_projection.py -q
运行（Windows）: cd ~/Desktop/ng-platform && .venv\Scripts\python -m pytest tests/test_projection.py -q
需要本地 PG（ng_platform 库，含 003 迁移 + 投影表）。
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.domain import events
from app.storage.event_log import EventLog
from app.storage.projection import Projector, OptimisticLockConflict
from app.storage.user_store import UserStore

DB_URL = "postgresql+psycopg://postgres@localhost:5433/ng_platform"
DB_APP_URL = "postgresql+psycopg://ng_app:ng@localhost:5433/ng_platform"


@pytest.fixture()
def db_log():
    """DB 投影模式：应用角色 EventLog+Projector + 超管连接（清理用）。"""
    app_eng = create_engine(DB_APP_URL)
    admin = create_engine(DB_URL)
    projector = Projector(app_eng)
    log = EventLog(engine=app_eng, projector=projector)
    yield log, admin, projector
    log._engine.dispose()
    admin.dispose()


def _cleanup(admin, ids: list[str], project_ids: list[str]):
    """清理测试投影行（events 是 append-only，靠唯一 id/幂等键隔离不删）。"""
    with admin.begin() as c:
        if ids:
            c.execute(text(
                "DELETE FROM tasks WHERE id = ANY(:ids)"), {"ids": ids})
            c.execute(text(
                "DELETE FROM feedback_proj WHERE id IN (SELECT id FROM feedback_proj WHERE content LIKE 'ptest-%')"))
            c.execute(text(
                "DELETE FROM usage_proj WHERE label LIKE 'ptest-%'"))
            c.execute(text(
                "DELETE FROM agents WHERE name LIKE 'ptest-%'"))
        if project_ids:
            c.execute(text(
                "DELETE FROM projects WHERE id = ANY(:pids)"), {"pids": project_ids})


def _new_project(db_log):
    log, admin, _ = db_log
    pid = str(uuid.uuid4())
    log.append(events.new_event(events.EventType.PROJECT_CREATED, "user",
                                {"title": "t", "goal": "g"}, project_id=pid))
    return pid, admin


def _new_task(db_log, pid):
    log, admin, _ = db_log
    tid = str(uuid.uuid4())
    log.append(events.new_event(events.EventType.TASK_CREATED, "system",
                                {"title": "T", "description": "d",
                                 "deliverables": ["docs/x.md"]},
                                project_id=pid, task_id=tid))
    return tid, admin


# ---------- 1. 投影-回放对拍（核心正确性） ----------
def test_projection_matches_replay(db_log):
    """同事件序列：Projector 读取 == replay 推导（逐字节一致）。"""
    log, admin, proj = db_log
    from app.main import _derive_project_list, _derive_task_list, _derive_task_context
    import tempfile
    jlog = EventLog(path=Path(tempfile.gettempdir()) / f"ptest-{uuid.uuid4()}.jsonl")

    pid = str(uuid.uuid4())
    tid = str(uuid.uuid4())
    for lg in (jlog, log):
        lg.append(events.new_event(events.EventType.PROJECT_CREATED, "user",
                                   {"title": "对拍项目", "goal": "g"}, project_id=pid))
        lg.append(events.new_event(events.EventType.TASK_CREATED, "system",
                                   {"title": "T", "deliverables": ["a.md"]},
                                   project_id=pid, task_id=tid))
        lg.append(events.new_event(events.EventType.AGENT_ASSIGNED, "system",
                                   {"agent": "lobster", "role": "owner"},
                                   project_id=pid, task_id=tid))
        lg.append(events.new_event(events.EventType.TASK_STATE_CHANGED, "sys",
                                   {"from": "todo", "to": "in_progress"},
                                   project_id=pid, task_id=tid))
        lg.append(events.new_event(events.EventType.DELIVERABLE_SUBMITTED, "agent",
                                   {"file_ref": "a.md"}, project_id=pid, task_id=tid))

    # projects：共享 DB 里可能残留其它测试项目，按本测试 pid 过滤对比
    db_p = [p for p in proj.get_projects(None) if p["project_id"] == pid]
    js_p = [p for p in _derive_project_list(jlog.replay(), None)
            if p["project_id"] == pid]
    assert db_p == js_p, f"projects 对拍不一致: {db_p} != {js_p}"
    # tasks 看板
    db_t = proj.get_tasks(pid)
    js_t = _derive_task_list(jlog.replay(project_id=pid), pid)
    assert db_t == js_t, f"tasks 对拍不一致: {db_t} != {js_t}"
    assert db_t[0]["owner"] == "lobster"
    assert db_t[0]["status"] == "in_progress"
    assert db_t[0]["has_deliverable"] is True
    # context
    db_c = proj.get_task_context(tid)
    js_c = _derive_task_context(jlog.replay(task_id=tid), tid)
    assert db_c["title"] == js_c["title"] and db_c["status"] == js_c["status"]
    assert db_c["owner"] == js_c["owner"]
    assert db_c["deliverables"] == js_c["deliverables"] == ["a.md"]

    _cleanup(admin, [tid], [pid])


# ---------- 2. 乐观锁 ----------
def test_optimistic_lock_conflict_rolls_back(db_log):
    """写-写冲突：expected_version 不匹配 → 409 且事件不落库（事务回滚）。"""
    log, admin, proj = db_log
    pid, _ = _new_project(db_log)
    tid, _ = _new_task(db_log, pid)
    # 第一次 PATCH（无版本）→ 成功，expected_version 0→1
    log.append(events.new_event(events.EventType.TASK_STATE_CHANGED, "sys",
                                {"from": "todo", "to": "in_progress"},
                                project_id=pid, task_id=tid), expected_version=0)
    row = proj.get_task_row(tid)
    assert row["status"] == "in_progress" and row["expected_version"] == 1
    # 再用过期版本 0 → OptimisticLockConflict，事件也不落库
    before = len([e for e in log.replay(task_id=tid)
                  if e["event_type"] == "task.state_changed"])
    with pytest.raises(OptimisticLockConflict):
        log.append(events.new_event(
            events.EventType.TASK_STATE_CHANGED, "sys",
            {"from": "in_progress", "to": "blocked"},
            project_id=pid, task_id=tid), expected_version=0)
    after = len([e for e in log.replay(task_id=tid)
                 if e["event_type"] == "task.state_changed"])
    assert after == before, f"冲突时事件不应落库: {before}->{after}"
    row = proj.get_task_row(tid)
    assert row["status"] == "in_progress" and row["expected_version"] == 1

    _cleanup(admin, [tid], [pid])


def test_optimistic_lock_precheck_409(db_log):
    """带过期版本直接 PATCH → API 409（预检，不落事件）。"""
    import app.main as main_mod
    log, admin, proj = db_log
    pid, _ = _new_project(db_log)
    tid, _ = _new_task(db_log, pid)
    log.append(events.new_event(events.EventType.TASK_STATE_CHANGED, "sys",
                                {"from": "todo", "to": "in_progress"},
                                project_id=pid, task_id=tid), expected_version=0)
    # 模拟 API 层（main 用全局 log，这里直接验证 change_state 的 409 逻辑）
    monkeypatch_app(main_mod, log)
    c = TestClient(main_mod.app)
    r = c.patch(f"/tasks/{tid}/state", params={"to": "blocked", "expected_version": 0},
                headers={"Authorization": "Bearer l1-agent-token"})
    assert r.status_code == 409
    assert "刷新" in r.json()["detail"]
    # 事件未多落
    n = sum(1 for e in log.replay(task_id=tid) if e["event_type"] == "task.state_changed")
    assert n == 1
    monkeypatch_restore(main_mod)
    _cleanup(admin, [tid], [pid])


def test_optimistic_lock_no_version_compat(db_log):
    """不带 expected_version → 200（worker/兼容路径，正常自增）。"""
    log, admin, proj = db_log
    pid, _ = _new_project(db_log)
    tid, _ = _new_task(db_log, pid)
    log.append(events.new_event(events.EventType.TASK_STATE_CHANGED, "sys",
                                {"from": "todo", "to": "in_progress"},
                                project_id=pid, task_id=tid), expected_version=0)
    # worker 无版本写 → 放行，版本自增
    log.append(events.new_event(events.EventType.TASK_STATE_CHANGED, "sys",
                                {"from": "in_progress", "to": "blocked"},
                                project_id=pid, task_id=tid))
    row = proj.get_task_row(tid)
    assert row["status"] == "blocked" and row["expected_version"] == 2
    _cleanup(admin, [tid], [pid])


# ---------- 3. 幂等共存 ----------
def test_idempotent_retry_does_not_bump_version(db_log):
    """同幂等键重试 → 幂等，expected_version 不自增。"""
    log, admin, proj = db_log
    pid, _ = _new_project(db_log)
    tid, _ = _new_task(db_log, pid)
    ev = events.new_event(events.EventType.TASK_STATE_CHANGED, "sys",
                          {"from": "todo", "to": "in_progress"},
                          project_id=pid, task_id=tid,
                          idempotency_key=f"ptest-k-{uuid.uuid4().hex[:8]}")
    log.append(ev, expected_version=0)
    log.append(dict(ev), expected_version=0)   # 重试（events append-only，唯一 key）
    row = proj.get_task_row(tid)
    assert row["expected_version"] == 1, f"幂等重试不应自增版本: {row}"
    _cleanup(admin, [tid], [pid])


# ---------- 4. 多用户隔离走投影 ----------
def test_multi_user_isolation_projection(db_log):
    """A 建项目 → owner_id=A；B 看不到、L3 静态 token 见全部。"""
    log, admin, proj = db_log
    # events.user_id 有 FK 到 users，建两个真实用户
    from app.storage.user_store import UserStore
    store = UserStore(engine=log._engine)
    ua = store.create_user(f"ptest-a-{uuid.uuid4().hex[:6]}", "secret123")
    ub = store.create_user(f"ptest-b-{uuid.uuid4().hex[:6]}", "secret123")
    pid = str(uuid.uuid4())
    log.append(events.new_event(events.EventType.PROJECT_CREATED, "user",
                                {"title": "A 的项目", "goal": "g"},
                                project_id=pid, user_id=ua["id"]))
    # B 看不见
    assert proj.get_projects(ub["id"]) == []
    # A 看得见
    a_projects = proj.get_projects(ua["id"])
    assert any(p["project_id"] == pid for p in a_projects)
    # L3 静态 token（viewer=None）见全部
    assert any(p["project_id"] == pid for p in proj.get_projects(None))
    # 用户不删：events.user_id FK 引用，append-only 不可清；用户名唯一无残留
    _cleanup(admin, [], [pid])


# ---------- 5. agents/feedback/usage 全投影 ----------
def test_agents_feedback_usage_projection(db_log):
    """agent.registered latest-wins、feedback 落表、usage 聚合。"""
    log, admin, proj = db_log
    tag = uuid.uuid4().hex[:8]
    aname = f"ptest-agent-{tag}"
    log.append(events.new_event(events.EventType.AGENT_REGISTERED, "user",
                                {"name": aname, "capability": "文档",
                                 "executor": "builtin"},
                                idempotency_key=f"ptest-ar-{tag}-1"))
    log.append(events.new_event(events.EventType.AGENT_REGISTERED, "user",
                                {"name": aname, "capability": "分析,文档",
                                 "executor": "builtin", "role": "ops"},
                                idempotency_key=f"ptest-ar-{tag}-2"))
    agents = proj.get_agents()
    a = next(x for x in agents if x["name"] == aname)
    assert a["capability"] == "分析,文档" and a["role"] == "ops"   # latest wins
    log.append(events.new_event(events.EventType.FEEDBACK_SUBMITTED, "user:u",
                                {"content": f"ptest-反馈-{tag}", "rating": 5}))
    fb = proj.get_feedback()
    assert any(x["content"] == f"ptest-反馈-{tag}" and x["rating"] == 5 for x in fb)
    log.append(events.new_event(events.EventType.USAGE_RECORDED, "llm",
                                {"provider": "openai", "model": "m",
                                 "label": f"ptest-{tag}",
                                 "input_tokens": 10, "output_tokens": 5}))
    u = proj.get_usage()
    assert u["input_tokens"] >= 10 and f"ptest-{tag}" in u["by_label"]
    _cleanup(admin, [], [])


# ---------- 6. 重建幂等 ----------
def test_rebuild_idempotent(db_log):
    """rebuild 幂等：重建两次结果一致。TRUNCATE 需超管连接。"""
    log, admin, proj = db_log
    # 用真实存量事件重建（events 不可删，投影表可 TRUNCATE）
    admin_proj = Projector(create_engine(DB_URL))
    admin_proj.rebuild()
    n1 = proj.get_projects(None)
    admin_proj.rebuild()
    n2 = proj.get_projects(None)
    assert n1 == n2
    with admin.connect() as c:
        total = c.execute(text("SELECT count(*) FROM projects")).scalar()
    assert total >= 30   # 存量 30 个项目（含 archived，get_projects 过滤了）


# ---------- 7. 孤儿任务不投影 ----------
def test_orphan_task_not_projected(db_log):
    """无 project_id 的 task.state_changed → 不建行；collect_task_ids 仍含该 id。"""
    log, admin, proj = db_log
    tid = str(uuid.uuid4())
    log.append(events.new_event(events.EventType.TASK_STATE_CHANGED, "sys",
                                {"from": "todo", "to": "in_progress"}, task_id=tid))
    assert proj.get_task_row(tid) is None
    # collect_task_ids 查 events 表 → 仍含孤儿
    from app.workers.__main__ import collect_task_ids
    ids = collect_task_ids(log)
    assert tid in ids
    _cleanup(admin, [], [])


# ---------- 辅助：monkeypatch app.main.log 到 DB 模式 ----------
def monkeypatch_app(main_mod, log):
    main_mod._orig_log = main_mod.log
    main_mod.log = log
    # auth 依赖 user_store，用 JSONL 兜底即可（本用例走静态 l1 token）

def monkeypatch_restore(main_mod):
    main_mod.log = main_mod._orig_log
