"""Agent 执行适配层基类 —— 模型/运行时中立（架构文档十一/十二：执行层可替换）。

openclaw.py / claude_sdk.py 各自实现 AgentExecutor。NG 自研编排层不绑定单一运行时，
派发/收集结果走统一接口，流程、消息、存储、模型均可替换（设计原则 4）。
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class AgentTask:
    """一次要派发给执行方的任务。"""
    agent_id: str
    task_id: str
    project_id: str
    prompt: str


@dataclass
class AgentResult:
    """一次派发/执行的结果。"""
    agent_id: str
    task_id: str | None
    status: str            # dispatched | done | failed | timeout | deduped
    message: str
    output: str = ""
    error: str = ""
    transfer_id: str = ""
    at: float = field(default_factory=time.time)


class AgentExecutor(ABC):
    """Agent 执行接口。派发任务 + 收集回写结果。"""

    @abstractmethod
    def dispatch(self, task: AgentTask, *, via: str = "message") -> AgentResult:
        """把 NG 任务派发给执行方。

        via=message   异步投递（写共享消息，适合长任务）
        via=cli       同步调用执行方拿回复（适合快速验证）
        """
        raise NotImplementedError

    @abstractmethod
    def collect_results(self, agent_id: str | None = None) -> list[dict]:
        """收集执行方回写的结果（供审计/入库采集）。"""
        raise NotImplementedError
