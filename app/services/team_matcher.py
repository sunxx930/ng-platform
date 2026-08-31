"""团队匹配器 —— 按 agent 注册表匹配责任/复核人（架构文档：团队匹配器 + Agent 注册中心）。

输入 agent 注册表（list[dict]: name/capability/status/permission）+ 任务草案 owner_hint/reviewer_hint。
规则：优先用 hint（可用时）；否则按 capability 关键词匹配可用 agent；复核人避免与责任人同人。
"""
from __future__ import annotations

from app.services.requirement_parser import TaskDraft


def _available(agents: list[dict]) -> list[dict]:
    return [a for a in agents if a.get("status", "available") != "disabled"]


def _match_by_hint_or_capability(agents: list[dict], hint: str, capability_hint: str = "") -> dict | None:
    for a in agents:
        if hint and a.get("name", "").lower() == hint.strip().lower():
            return a
    if capability_hint:
        for a in agents:
            cap = str(a.get("capability") or "").lower()
            if any(k in cap for k in capability_hint.lower().split() if len(k) > 1):
                return a
    return None


def match_team(task: TaskDraft, agents: list[dict]) -> dict:
    """返回 {owner, reviewer}（可为 None）。

    owner 优先 builtin（NG 自研，平台能自动执行）；hint/能力作 tiebreaker；
    只有无 builtin 时才落到 openclaw 外部执行方。reviewer 从剩余人里挑（≠ owner）。
    """
    pool = _available(agents)
    builtin = [a for a in pool if a.get("executor", "builtin") == "builtin"]
    external = [a for a in pool if a.get("executor", "builtin") != "builtin"]
    owner = _match_by_hint_or_capability(builtin, task.owner_hint, task.title)
    if owner is None:
        owner = _match_by_hint_or_capability(external, task.owner_hint, task.title)
    if owner is None and builtin:
        owner = builtin[0]   # 兜底：有 builtin 就优先派给它（平台自己干活）
    others = [a for a in pool if a.get("name") != (owner or {}).get("name")]
    reviewer = _match_by_hint_or_capability(others, task.reviewer_hint, "")
    if reviewer is None and others:
        reviewer = others[0]
    return {"owner": owner["name"] if owner else None,
            "reviewer": reviewer["name"] if reviewer else None}
