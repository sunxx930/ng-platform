"""NG 自研 agent —— 跑在平台自己的算力上（LLMClient），不依赖 openclaw。

闭环：用户给目标 → 需求解析建任务 → NG agent 用算力产出交付物 → 落盘 → 回报平台 → 自动交接复核。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.services.llm import LLMClient

AGENT_NAME = "NG助理"


@dataclass
class TaskContext:
    task_id: str
    project_id: str = ""
    title: str = ""
    description: str = ""
    deliverables: list[str] = field(default_factory=list)


SYSTEM_PROMPT = (
    "你是 NG 平台的执行 agent。根据任务要求产出完整、结构化的交付内容（Markdown），"
    "包括标题、小节、正文。直接输出内容，不要多余说明。"
)


class BuiltinAgent:
    """NG 自研执行 agent：用平台算力把任务做掉，交付物落盘。"""

    def __init__(self, llm: LLMClient | None = None, artifacts_dir: Path | None = None):
        self._llm = llm or LLMClient()
        self._artifacts = Path(artifacts_dir or "artifacts")

    def execute(self, task: TaskContext) -> dict:
        """产出交付物 → 落盘 artifacts/<task_id>.md。返回 {file_ref, summary, content_len}。"""
        self._artifacts.mkdir(parents=True, exist_ok=True)
        wants = task.deliverables or ["交付内容"]
        prompt = (
            f"任务：{task.title}\n"
            f"描述：{task.description or '（无补充说明）'}\n"
            f"要求交付：{', '.join(wants)}\n\n"
            f"请产出完整的交付文档。"
        )
        content = self._llm.complete(SYSTEM_PROMPT, prompt)
        file_ref = f"artifacts/{task.task_id}.md"
        (self._artifacts / f"{task.task_id}.md").write_text(content, encoding="utf-8")
        return {"file_ref": file_ref, "summary": f"已产出《{task.title}》交付文档",
                "content_len": len(content), "usage": self._llm.usage()}
