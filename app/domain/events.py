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
    GOAL_PARSE_FAILED = "goal.parse_failed"   # 需求解析失败（龙虾汇总#2：失败要可审计）
    FEEDBACK_SUBMITTED = "feedback.submitted" # 试用者反馈
    USAGE_RECORDED = "usage.recorded"         # LLM 用量（token）记录
    USER_REGISTERED = "user.registered"       # 用户注册（多用户，2026-09-01）
    USER_LOGGED_IN = "user.logged_in"         # 用户登录（多用户，2026-09-01）


import time

def new_event(event_type: EventType | str, actor: str, payload: dict,
              project_id: str | None = None, task_id: str | None = None,
              idempotency_key: str | None = None,
              user_id: str | None = None) -> dict:
    """构造一条事件（由事件日志层负责入库，idempotency_key 去重）。

    user_id（多用户 2026-09-01）：事件的操作者（注册用户），审计/项目隔离维度。
    """
    return {
        "event_type": event_type.value if isinstance(event_type, EventType) else str(event_type),
        "actor": actor,
        "payload": payload,
        "project_id": project_id,
        "task_id": task_id,
        "idempotency_key": idempotency_key,
        "user_id": user_id,
        "created_at_ts": time.time(),
    }
