"""Claude SDK 执行适配层 —— 官方 Anthropic SDK 实现（AgentExecutor 第二实现）。

模型中立执行层：openclaw（外接运行时）/ claude_sdk（Anthropic 官方）/ BuiltinAgent（自研 LLMClient）并列。
配置：ANTHROPIC_API_KEY 或 `ant auth login` profile；模型默认 claude-opus-4-8，可用 CLAUDE_MODEL 覆盖。
"""
from __future__ import annotations

import os

from app.adapters.base import AgentExecutor, AgentTask, AgentResult

DEFAULT_MODEL = "claude-opus-4-8"

SYSTEM_PROMPT = (
    "你是 NG 平台的执行 agent。根据任务要求产出完整、结构化的交付内容（Markdown），"
    "包括标题、小节、正文。直接输出内容，不要多余说明。"
)


class ClaudeSDKExecutor(AgentExecutor):
    """用官方 Anthropic SDK 同步执行 NG 任务（via=cli 语义）。"""

    def __init__(self, client=None, model: str | None = None,
                 max_tokens: int = 16000, effort: str = "medium"):
        import anthropic
        self._client = client or anthropic.Anthropic()
        self._model = model or os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL)
        self._max_tokens = max_tokens
        self._effort = effort

    def dispatch(self, task: AgentTask, *, via: str = "message") -> AgentResult:
        try:
            prompt = (
                f"项目：{task.project_id}\n"
                f"任务：{task.prompt}\n\n"
                f"请执行并产出完整的交付内容。"
            )
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                thinking={"type": "adaptive"},
                output_config={"effort": self._effort},
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in response.content if b.type == "text")
            return AgentResult(agent_id=task.agent_id, task_id=task.task_id,
                               status="done", message=task.prompt,
                               output=text[:2000])
        except Exception as e:      # noqa: BLE001 —— 返回 failed，不抛出
            return AgentResult(agent_id=task.agent_id, task_id=task.task_id,
                               status="failed", message=task.prompt,
                               error=f"{type(e).__name__}: {e}")

    def collect_results(self, agent_id: str | None = None) -> list[dict]:
        """同步执行无文件回写，结果在 dispatch 返回里。"""
        return []
