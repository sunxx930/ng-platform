"""Worker 基类 —— 幂等/重试/租约恢复（补强项 15.3/15.4）。

调度器硬性要求：
- 幂等键：每任务每动作唯一
- 重试 + 超时 + 断点恢复
- 重复消息去重
- 不依赖 Agent 自己「记得回来干活」
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from app.domain import events
from app.domain.task import TaskStatus


class Worker:
    """Worker 基类。子类实现 `process(task_id)`，由 `tick()` 周期性驱动。"""

    name: str = "base"
    interval_s: float = 30.0
    LEASE_TIMEOUT_S = 300
    MAX_ATTEMPTS = 3

    def __init__(self, log: Any, state_dir: Path | None = None):
        self._log = log
        self._engine = getattr(log, "_engine", None)   # DB 正源租约
        self._state_dir = state_dir or Path("data/workers")
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._leases: dict[str, dict] = {}
        self._load_leases()

    # ---- 租约（15.4） ----
    def _lease_file(self) -> Path:
        return self._state_dir / f"{self.name}_leases.json"

    def _load_leases(self):
        if self._lease_file().exists():
            self._leases = json.loads(self._lease_file().read_text(encoding="utf-8"))

    def _save_leases(self):
        self._lease_file().write_text(
            json.dumps(self._leases, ensure_ascii=False), encoding="utf-8")

    def acquire_lease(self, task_id: str) -> bool:
        # DB 正源：worker_runs 表原子租约（跨进程互斥，Fix2）
        if self._engine is not None:
            return self._acquire_db_lease(task_id)
        lease = self._leases.get(task_id)
        now = time.time()
        if lease and now - lease.get("heartbeat", 0) < self.LEASE_TIMEOUT_S:
            return False  # 租约仍被持有
        self._leases[task_id] = {
            "lease_owner": self.name, "heartbeat": now,
            "attempt": lease.get("attempt", 0) + 1 if lease else 1,
            "max_attempts": self.MAX_ATTEMPTS,
        }
        self._save_leases()
        return True

    def _acquire_db_lease(self, task_id: str) -> bool:
        from sqlalchemy import text
        now = int(time.time())
        with self._engine.begin() as conn:
            # 原子更新：租约过期/完成才可被本类型接管（按类型隔离）
            r = conn.execute(text(
                "UPDATE worker_runs SET lease_owner=:owner, worker_type=:wtype, "
                "heartbeat=now(), attempt=attempt+1, status='running' "
                "WHERE task_id=:tid AND (EXTRACT(EPOCH FROM heartbeat) < :hb - :timeout OR status='done')"),
                {"owner": self.name, "wtype": self.name, "hb": now, "tid": task_id,
                 "timeout": self.LEASE_TIMEOUT_S})
            if r.rowcount == 1:
                return True
            ins = conn.execute(text(
                "INSERT INTO worker_runs (id, task_id, lease_owner, worker_type, heartbeat) "
                "VALUES (gen_random_uuid(), :tid, :owner, :wtype, now()) "
                "ON CONFLICT (task_id) DO NOTHING"),
                {"tid": task_id, "owner": self.name, "wtype": self.name})
            return ins.rowcount == 1

    def renew_lease(self, task_id: str) -> bool:
        """续租：仅当前持有者可刷新心跳（Fix 3）。"""
        if self._engine is None:
            if task_id in self._leases:
                self._leases[task_id]["heartbeat"] = time.time(); self._save_leases()
                return True
            return False
        from sqlalchemy import text
        with self._engine.begin() as c:
            r = c.execute(text(
                "UPDATE worker_runs SET heartbeat=now() "
                "WHERE task_id=:tid AND lease_owner=:owner AND status='running'"),
                {"tid": task_id, "owner": self.name})
            return r.rowcount == 1

    def release(self, task_id: str):
        """释放租约（标记 done，其他可接管）。"""
        if self._engine is None:
            self._leases.pop(task_id, None); self._save_leases()
            return
        from sqlalchemy import text
        with self._engine.begin() as c:
            c.execute(text(
                "UPDATE worker_runs SET status='done' WHERE task_id=:tid AND lease_owner=:owner"),
                {"tid": task_id, "owner": self.name})

    def heartbeat(self, task_id: str):
        if task_id in self._leases:
            self._leases[task_id]["heartbeat"] = time.time()
            self._save_leases()

    # ---- 幂等（15.3） ----
    def _resolve_project(self, task_id: str) -> str | None:
        """从 TASK_CREATED 事件解析任务所属项目（Worker 事件带项目归属）。"""
        for e in self._log.replay(task_id=task_id):
            if e["event_type"] == events.EventType.TASK_CREATED.value:
                return e.get("project_id")
        return None

    def emit(self, task_id: str, event_type: events.EventType, payload: dict,
             project_id: str | None = None, idempotency_key: str | None = None):
        """写事件（幂等：重复 key 会被事件层忽略）。项目归属自动解析。"""
        key = idempotency_key or f"{self.name}:{task_id}:{event_type.value}"
        project_id = project_id or self._resolve_project(task_id)   # 深修3: 项目归属
        self._log.append(events.new_event(
            event_type, f"worker:{self.name}", payload,
            project_id=project_id, task_id=task_id, idempotency_key=key))

    # ---- 主循环 ----
    def tick(self, task_ids: list[str]):
        for tid in task_ids:
            if not self.acquire_lease(tid):
                continue  # 租约未释放，跳过（避免重复处理）
            try:
                self.process(tid)
                self.renew_lease(tid)   # Fix 3: 处理中续租（长任务心跳）
            except Exception as e:
                if self._engine is None and tid in self._leases:
                    self._leases[tid]["error"] = str(e)
                    self._save_leases()
            finally:
                self.release(tid)       # Fix 3: 处理完真正释放（done 供其他接管）

    def process(self, task_id: str):
        raise NotImplementedError
