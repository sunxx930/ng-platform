"""DeadlineWorker —— 临近截止触发自检预警，到期自动汇报/升级。"""
from __future__ import annotations

import time

from app.domain import events
from app.domain.task import TaskStatus
from app.workers.base import Worker


class DeadlineWorker(Worker):
    name = "deadline"
    interval_s = 60.0
    WARN_LEAD_S = 600  # 提前 10 分钟预警

    def process(self, task_id: str):
        evs = self._log.replay(task_id=task_id)
        deadline = None
        state = TaskStatus.TODO
        for e in evs:
            if e["event_type"] == events.EventType.TASK_CREATED.value:
                deadline = e["payload"].get("deadline_ts")
            if e["event_type"] == events.EventType.TASK_STATE_CHANGED.value:
                state = TaskStatus(e["payload"]["to"])
        if not deadline or state in (TaskStatus.COMPLETED, TaskStatus.CANCELLED,
                                     TaskStatus.ARCHIVED):
            return
        remaining = deadline - time.time()
        if 0 < remaining <= self.WARN_LEAD_S:
            self.emit(task_id, events.EventType.TASK_DEADLINE_MET,
                      {"remaining_s": int(remaining), "warning": True},
                      idempotency_key=f"deadline:warn:{task_id}:{int(remaining//300)}")
        elif remaining <= 0:
            self.emit(task_id, events.EventType.TASK_DEADLINE_MET,
                      {"remaining_s": int(remaining), "overdue": True},
                      idempotency_key=f"deadline:over:{task_id}")
