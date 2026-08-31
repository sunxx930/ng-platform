"""需求解析器 —— 用 LLM 把用户目标解析成结构化任务草案（架构文档：需求解析与 Agent 匹配层）。

基于底层算力接入（LLMClient），LLM 拆解目标 → TaskDraft[]，可审计（goal.parsed 事件）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.services.llm import LLMClient


@dataclass
class TaskDraft:
    title: str
    description: str = ""
    deliverables: list[str] = field(default_factory=list)
    owner_hint: str = ""       # 建议责任 agent
    reviewer_hint: str = ""    # 建议复核 agent


@dataclass
class GoalParse:
    summary: str = ""
    tasks: list[TaskDraft] = field(default_factory=list)


SYSTEM_PROMPT = (
    "你是 NG AI Platform 的需求解析器。把用户的项目目标拆成可执行的任务清单。\n"
    '严格输出 JSON：{"summary": "一句话概括", '
    '"tasks": [{"title": "任务标题", "description": "做什么/怎么做", '
    '"deliverables": ["交付物"], "owner_hint": "建议责任人(未知给空)", '
    '"reviewer_hint": "建议复核人(未知给空)"}]}\n'
    "任务数 1-5 个，拆到可执行粒度，不要重叠。"
)


class RequirementParser:
    def __init__(self, llm: LLMClient):
        self._llm = llm

    def parse_goal(self, goal: str, agent_names: list[str] | None = None) -> GoalParse:
        """解析目标 → 任务草案。agent_names 供 LLM 参考（能力匹配提示）。"""
        user = f"项目目标: {goal}"
        if agent_names:
            user += f"\n可用 agent 名单: {', '.join(agent_names)}"
        data = self._llm.parse_json(SYSTEM_PROMPT, user)
        tasks = [TaskDraft(
            title=t.get("title", ""),
            description=t.get("description", ""),
            deliverables=list(t.get("deliverables") or []),
            owner_hint=t.get("owner_hint", "") or "",
            reviewer_hint=t.get("reviewer_hint", "") or "",
        ) for t in data.get("tasks", []) if t.get("title")]
        return GoalParse(summary=data.get("summary", "") or goal[:60], tasks=tasks)
