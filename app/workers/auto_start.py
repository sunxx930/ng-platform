"""AutoStartWorker —— 任务创建且依赖满足 → 自动派发进进行中。

任务依赖编排（2026-09-02，用户"平台按任务性质分串联/并联"）：
- TASK_CREATED 事件的 payload.depends_on 存被依赖任务 id。
- 本 worker 只启动「无依赖 或 上游全部 completed/archived」的任务（并联任务立即开工、
  串联任务等上游完成才开工）；依赖未满足保持 TODO 等待（勿置 blocked，避免 BlockerWorker
  把"等依赖"误当真阻塞升级）。
"""
from __future__ import annotations

from app.domain import events
from app.domain.task import TaskStatus, transition
from app.workers.base import Worker

# 上游满足依赖的结束态
_DONE = {TaskStatus.COMPLETED.value, TaskStatus.ARCHIVED.value}


def task_state(log, task_id: str) -> TaskStatus:
    """从事件流推导任务最新状态（默认 TODO）。"""
    state = TaskStatus.TODO
    for e in log.replay(task_id=task_id):
        if e["event_type"] == events.EventType.TASK_STATE_CHANGED.value:
            state = TaskStatus(e["payload"]["to"])
    return state


def task_depends_on(log, task_id: str) -> list[str]:
    """从 TASK_CREATED 事件读该任务依赖的上游 task_id 列表。"""
    for e in log.replay(task_id=task_id):
        if e["event_type"] == events.EventType.TASK_CREATED.value:
            return list(e["payload"].get("depends_on") or [])
    return []


class AutoStartWorker(Worker):
    name = "auto_start"
    interval_s = 15.0

    def process(self, task_id: str):
        state = task_state(self._log, task_id)
        # 待办且依赖满足 → 派发进进行中
        if state == TaskStatus.TODO:
            deps = task_depends_on(self._log, task_id)
            if deps:
                # 串联：所有上游须 completed/archived 才启动；否则保持 TODO 等
                for dep in deps:
                    if task_state(self._log, dep) not in _DONE:
                        return   # 上游未完成，等待
            new = transition(state, TaskStatus.IN_PROGRESS)
            self.emit(task_id, events.EventType.TASK_STATE_CHANGED,
                      {"from": state.value, "to": new.value,
                       "trigger": "AutoStartWorker"})
