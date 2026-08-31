"""任务状态机 —— 状态枚举冻结 + 转移白名单（架构补强项 15.5）。"""
from __future__ import annotations

from enum import Enum


class TaskStatus(str, Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    IN_REVIEW = "in_review"
    PENDING_APPROVAL = "pending_approval"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"


# 合法转移白名单（补强项要求：每条转移建立白名单）
TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.TODO: {TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED},
    TaskStatus.IN_PROGRESS: {TaskStatus.IN_REVIEW, TaskStatus.BLOCKED,
                             TaskStatus.CANCELLED},
    TaskStatus.BLOCKED: {TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED},
    TaskStatus.IN_REVIEW: {TaskStatus.IN_PROGRESS, TaskStatus.PENDING_APPROVAL,
                           TaskStatus.COMPLETED},
    TaskStatus.PENDING_APPROVAL: {TaskStatus.COMPLETED, TaskStatus.IN_PROGRESS,
                                  TaskStatus.CANCELLED},
    TaskStatus.COMPLETED: {TaskStatus.ARCHIVED},
    TaskStatus.CANCELLED: set(),
    TaskStatus.ARCHIVED: set(),
}

# 复核补充状态（补强项 15.5）
class ReviewVerdict(str, Enum):
    PASS = "pass"
    REJECT = "reject"
    NEEDS_CHANGES = "needs_changes"
    INCONCLUSIVE = "inconclusive"


class InvalidTransition(Exception):
    pass


def can_transition(frm: TaskStatus, to: TaskStatus) -> bool:
    return to in TRANSITIONS.get(frm, set())


def transition(frm: TaskStatus, to: TaskStatus) -> TaskStatus:
    """执行状态转移（非法则抛异常，供 API 层 400）。"""
    if not can_transition(frm, to):
        raise InvalidTransition(f"非法状态转移: {frm.value} -> {to.value}")
    return to
