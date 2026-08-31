"""HeartbeatWorker —— 周期检查任务心跳，静默超阈值自动提醒/转阻塞。"""
from __future__ import annotations

import time

from app.domain import events
from app.domain.task import TaskStatus, transition
from app.workers.base import Worker


class HeartbeatWorker(Worker):
    name = "heartbeat"
    interval_s = 60.0
    SILENT_TIMEOUT_S = 300  # 5 分钟无心跳视为静默

    def process(self, task_id: str):
        evs = self._log.replay(task_id=task_id)
        state = TaskStatus.TODO
        in_progress_since = None
        last_heartbeat = None
        for e in evs:
            if e["event_type"] == events.EventType.TASK_STATE_CHANGED.value:
                payload = e["payload"]
                if payload.get("to") == TaskStatus.IN_PROGRESS.value and in_progress_since is None:
                    in_progress_since = e.get("created_at_ts")
                state = TaskStatus(payload["to"])
            if e["event_type"] == events.EventType.AGENT_HEARTBEAT.value:
                last_heartbeat = e.get("created_at_ts")
        if state != TaskStatus.IN_PROGRESS:
            return
        now = time.time()
        # 宽限：刚进 IN_PROGRESS 未满宽限不误判；心跳存在则按心跳判定
        if in_progress_since is None:
            return
        if now - in_progress_since < self.SILENT_TIMEOUT_S:
            return  # F1: 宽限期，不立即转 BLOCKED
        last_active = last_heartbeat or in_progress_since
        if now - last_active > self.SILENT_TIMEOUT_S:
            self.emit(task_id, events.EventType.TASK_STATE_CHANGED,
                      {"from": state.value, "to": TaskStatus.BLOCKED.value,
                       "trigger": "HeartbeatWorker:silent"})
