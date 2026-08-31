"""AutoStartWorker —— 任务创建且依赖满足 → 自动派发进进行中。"""
from __future__ import annotations

from app.domain import events
from app.domain.task import TaskStatus, transition
from app.workers.base import Worker


class AutoStartWorker(Worker):
    name = "auto_start"
    interval_s = 15.0

    def process(self, task_id: str):
        evs = self._log.replay(task_id=task_id)
        state = TaskStatus.TODO
        for e in evs:
            if e["event_type"] == events.EventType.TASK_STATE_CHANGED.value:
                state = TaskStatus(e["payload"]["to"])
        # 待办且依赖满足（骨架先忽略依赖，简化）→ 派发
        if state == TaskStatus.TODO:
            new = transition(state, TaskStatus.IN_PROGRESS)
            self.emit(task_id, events.EventType.TASK_STATE_CHANGED,
                      {"from": state.value, "to": new.value,
                       "trigger": "AutoStartWorker"})
