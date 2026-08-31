"""DB 模式回归 —— 事件正源用 PostgreSQL（本机 Postgres.app :5433）。

运行: cd ~/Desktop/ng-platform && .venv/bin/python -m pytest tests/test_db.py -q
需要本地 PG（ng_platform 库已建，含 schema + append-only 迁移）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from sqlalchemy import create_engine
from sqlalchemy import text

from app.storage.event_log import EventLog

DB_URL = "postgresql+psycopg://postgres@localhost:5433/ng_platform"
DB_APP_URL = "postgresql+psycopg://ng_app:ng@localhost:5433/ng_platform"


@pytest.fixture()
def db_log():
    eng = create_engine(DB_URL)          # 超级用户（验证/超管操作）
    app_eng = create_engine(DB_APP_URL)  # ng_app：仅 INSERT/SELECT（append-only）
    # 不 DELETE 清理：append-only 触发器连超管也拦（正确行为）；测试用唯一 ID+幂等键天然幂等
    return EventLog(engine=app_eng), EventLog(engine=eng)


def test_db_event_append_and_idempotency(db_log):
    app_log, _ = db_log
    from app.domain import events
    e1 = events.new_event(events.EventType.PROJECT_CREATED, "user",
                          {"title": "db"}, project_id="11111111-1111-1111-1111-111111111111",
                          idempotency_key="db-test-1")
    app_log.append(e1)
    app_log.append(dict(e1))   # 同 key 再写 → 幂等
    rows = app_log.replay(project_id="11111111-1111-1111-1111-111111111111")
    assert len(rows) == 1, f"幂等应 1 条，实际 {len(rows)}"


def test_db_append_only_trigger(db_log):
    app_log, admin = db_log
    from app.domain import events
    from sqlalchemy import text
    ev = events.new_event(events.EventType.TASK_CREATED, "t",
                          {"title": "x"}, task_id="22222222-2222-2222-2222-222222222222",
                          idempotency_key="db-test-2")
    app_log.append(ev)
    # 直接 UPDATE events 应被触发器拒绝（超管也拦）
    with admin._engine.begin() as c:
        with pytest.raises(Exception):
            c.execute(text("UPDATE events SET actor='hacked' WHERE idempotency_key='db-test-2'"))


def test_db_idempotency_conflict(db_log):
    """P1 反例（DB 路径）：同幂等键 + 不同内容 → IdempotencyConflict，不静默丢写。"""
    app_log, _ = db_log
    from app.domain import events
    from app.storage.event_log import IdempotencyConflict
    ev1 = events.new_event(events.EventType.TASK_CREATED, "t", {"title": "x"},
                           task_id="33333333-3333-3333-3333-333333333333",
                           idempotency_key="db-conflict-1")
    app_log.append(ev1)
    ev2 = events.new_event(events.EventType.TASK_CREATED, "t", {"title": "Y"},
                           task_id="33333333-3333-3333-3333-333333333333",
                           idempotency_key="db-conflict-1")
    with pytest.raises(IdempotencyConflict):
        app_log.append(ev2)
    # 同 key 同内容 → 幂等不抛（重跑安全）
    app_log.append(dict(ev1))


def test_db_worker_lease_mutex(db_log):
    """跨进程租约互斥：两个 Worker 抢同一任务，只有一个拿到。"""
    import uuid
    app_log, _ = db_log
    from app.workers.auto_start import AutoStartWorker
    w1 = AutoStartWorker(app_log)
    w2 = AutoStartWorker(app_log)
    tid = str(uuid.uuid4())   # 每次唯一，避免旧租约污染
    # 先造一个 TASK_CREATED（供状态机推导）
    from app.domain import events
    app_log.append(events.new_event(events.EventType.TASK_CREATED, "sys",
                                   {"title": "w", "deadline_ts": None}, task_id=tid))
    a1 = w1.acquire_lease(tid)
    a2 = w2.acquire_lease(tid)
    assert a1 and not a2, f"互斥应一个拿到一个拿不到: {a1},{a2}"
