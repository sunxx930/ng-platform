"""BlockerWorker —— 阻塞超过阈值沿责任链升级，无责任人请求用户指定。"""
from __future__ import annotations

import time

from app.domain import events
from app.domain.task import TaskStatus
from app.workers.base import Worker


class BlockerWorker(Worker):
    name = "blocker"
    interval_s = 60.0
    ESCALATE_TIMEOUT_S = 600  # 阻塞 10 分钟升级

    def process(self, task_id: str):
        evs = self._log.replay(task_id=task_id)
        blocked_since = None
        state = TaskStatus.TODO
        owner = None
        for e in evs:
            if e["event_type"] == events.EventType.TASK_STATE_CHANGED.value:
                payload = e["payload"]
                state = TaskStatus(payload["to"])   # F2: 更新 state
                if payload.get("to") == TaskStatus.BLOCKED.value and blocked_since is None:
                    blocked_since = e.get("created_at_ts")
            if e["event_type"] == events.EventType.AGENT_ASSIGNED.value:
                owner = e["payload"].get("agent")
        if state != TaskStatus.BLOCKED or blocked_since is None:
            return
        now = time.time()
        if blocked_since and (now - blocked_since) > self.ESCALATE_TIMEOUT_S:
            self.emit(task_id, events.EventType.TASK_BLOCKED,
                      {"escalate": True, "owner": owner or "unassigned"},
                      idempotency_key=f"blocker:esc:{task_id}")
