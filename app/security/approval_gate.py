"""审批 gate 通用化（架构文档 security/approval_gate.py）——任何 L3/L4 动作的审批门。

用法：需审批的动作在放行前调 ensure_approved(project_id, scope, task_id=None)。
- 已有批准的审批 → 放行
- 无 → 自动建审批请求（幂等）并抛 PendingApproval(approval_id)，调用方返回 409 提示去审批
替代原先 pause_project 专用的「手工查已批准审批」逻辑。
"""
from __future__ import annotations

import uuid

from app.domain import events


class PendingApproval(Exception):
    """动作待审批。approval_id 供前端去 POST /approvals/{id}/decision。"""

    def __init__(self, approval_id: str, scope: str):
        self.approval_id = approval_id
        self.scope = scope
        super().__init__(f"动作需审批（scope={scope}），approval_id={approval_id}")


class ApprovalGate:
    """get_log 传 callable，动态读当前 log（log 会被 DB 模式替换 / 测试 monkeypatch）。"""

    def __init__(self, get_log):
        self._get_log = get_log

    @property
    def _log(self):
        return self._get_log()

    def ensure_approved(self, project_id: str, scope: str, *,
                        task_id: str | None = None):
        """已批准放行；否则建审批请求（幂等）并抛 PendingApproval。"""
        if self._is_approved(project_id, task_id, scope):
            return
        aid = self._ensure_requested(project_id, task_id, scope)
        raise PendingApproval(aid, scope)

    def _is_approved(self, project_id, task_id, scope) -> bool:
        for e in self._log.replay(project_id=project_id, task_id=task_id):
            if e["event_type"] == events.EventType.APPROVAL_DECIDED.value \
                    and e["payload"].get("result") == "approve" \
                    and scope in str(e["payload"].get("scope", "")):
                return True
        return False

    def _ensure_requested(self, project_id, task_id, scope) -> str:
        # 幂等：同 project+task+scope 已有请求 → 返回既有 approval_id（不新建）
        for e in self._log.replay(project_id=project_id, task_id=task_id):
            if e["event_type"] == events.EventType.APPROVAL_REQUESTED.value \
                    and e["payload"].get("scope") == scope:
                return e["payload"]["approval_id"]
        aid = str(uuid.uuid4())
        self._log.append(events.new_event(
            events.EventType.APPROVAL_REQUESTED, "system",
            {"approval_id": aid, "scope": scope},
            project_id=project_id, task_id=task_id,
            idempotency_key=f"approval:req:{task_id or project_id}:{scope}"))
        return aid
