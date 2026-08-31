"""ReportWorker —— 状态/结论变化主动推送责任人/复核人/用户（骨架记事件+写通知队列）。"""
from __future__ import annotations

import json
from pathlib import Path

from app.domain import events
from app.domain.task import TaskStatus
from app.workers.base import Worker


class ReportWorker(Worker):
    name = "report"
    interval_s = 30.0

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._notify_file = self._state_dir / "notifications.jsonl"

    def process(self, task_id: str):
        evs = self._log.replay(task_id=task_id)
        if not evs:
            return
        latest = evs[-1]
        if latest["event_type"] not in (
            events.EventType.TASK_STATE_CHANGED.value,
            events.EventType.REVIEW_DECIDED.value,
            events.EventType.APPROVAL_DECIDED.value,
        ):
            return
        # 推送到待发送队列（通知层/渠道对接时消费）
        self._notify_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._notify_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "task_id": task_id,
                "event": latest["event_type"],
                "payload": latest["payload"],
                "to": ["责任人", "复核人", "用户"],   # 骨架简化
            }, ensure_ascii=False) + "\n")
