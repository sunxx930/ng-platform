"""权限与安全（架构文档九 + 补强项）—— L0-L4 分级授权。

每个工具调用前检查：调用者、项目范围、资源范围、动作级别、审批状态。
"""
from __future__ import annotations

from enum import IntEnum


class Level(IntEnum):
    L0_READ = 0          # 读取已授权项目材料
    L1_INTERNAL = 1      # 写任务状态/草稿/内部日志
    L2_COLLAB = 2        # 向成员发送交接/进度
    L3_FLOW = 3          # 修改责任链/权限/审批节点
    L4_EXTERNAL = 4      # 资金/生产/公开发布/删除


# 需要用户审批的动作级别（补强项：审批门）
REQUIRES_APPROVAL: set[Level] = {Level.L3_FLOW, Level.L4_EXTERNAL}

# 默认权限映射：动作 → 最低级别
ACTION_REQUIRED_LEVEL: dict[str, Level] = {
    "read_project": Level.L0_READ,
    "read_audit": Level.L0_READ,
    "write_message": Level.L1_INTERNAL,
    "change_task_state": Level.L1_INTERNAL,
    "submit_deliverable": Level.L1_INTERNAL,
    "send_handover": Level.L2_COLLAB,
    "modify_chain": Level.L3_FLOW,
    "approve_action": Level.L3_FLOW,
    "pause_project": Level.L3_FLOW,
    "external_irreversible": Level.L4_EXTERNAL,
}


class PermissionDenied(Exception):
    pass


class ApprovalRequired(Exception):
    pass


def check(agent_level: Level | int, action: str) -> None:
    """校验 agent 的权限级别是否足以执行该动作。"""
    required = ACTION_REQUIRED_LEVEL.get(action, Level.L1_INTERNAL)
    if int(agent_level) < int(required):
        raise PermissionDenied(
            f"权限不足: {action} 需要 {required.name}，agent 只有 {Level(agent_level).name}")
    # 审批动作本身是审批门，不再要求二次审批（避免自指死锁）
    if action == "approve_action":
        return
    if required in REQUIRES_APPROVAL:
        raise ApprovalRequired(f"{action} 属于 {required.name}，需人工审批")
