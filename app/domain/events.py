"""事件类型定义 —— 审计正源（append-only，不可 UPDATE/DELETE）。"""
from __future__ import annotations

from enum import Enum


class EventType(str, Enum):
    PROJECT_CREATED = "project.created"
    PROJECT_PAUSED = "project.paused"
    PROJECT_ARCHIVED = "project.archived"
    TASK_CREATED = "task.created"
    TASK_STATE_CHANGED = "task.state_changed"
    TASK_BLOCKED = "task.blocked"
    TASK_DEADLINE_MET = "task.deadline_met"
    MESSAGE_AGGREGATED = "message.aggregated"
    DELIVERABLE_SUBMITTED = "deliverable.submitted"
    REVIEW_REQUESTED = "review.requested"
    REVIEW_DECIDED = "review.decided"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    HANDOVER_CREATED = "handover.created"
    AGENT_ASSIGNED = "agent.assigned"
    AGENT_HEARTBEAT = "agent.heartbeat"
    AGENT_TRANSFERRED = "agent.transferred"   # openclaw 转移
    AGENT_REGISTERED = "agent.registered"     # agent 注册中心（事件溯源，可审计）
    GOAL_PARSED = "goal.parsed"               # 需求解析：用户目标 → 任务草案
    FEEDBACK_SUBMITTED = "feedback.submitted" # 试用者反馈
    USAGE_RECORDED = "usage.recorded"         # LLM 用量（token）记录


import time

def new_event(event_type: EventType | str, actor: str, payload: dict,
              project_id: str | None = None, task_id: str | None = None,
              idempotency_key: str | None = None) -> dict:
    """构造一条事件（由事件日志层负责入库，idempotency_key 去重）。"""
    return {
        "event_type": event_type.value if isinstance(event_type, EventType) else str(event_type),
        "actor": actor,
        "payload": payload,
        "project_id": project_id,
        "task_id": task_id,
        "idempotency_key": idempotency_key,
        "created_at_ts": time.time(),
    }
