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
    project_goal: str = ""        # 项目目标（执行上下文，2026-09-03 汇总#2）
    upstream: str = ""            # 上游交付物/源数据内容（治数据编造）
    review_opinion: str = ""      # 复核打回修改指令（④，2026-09-03）


SYSTEM_PROMPT = (
    "你是 NG 平台的执行 agent。根据任务要求产出完整、结构化的交付内容（Markdown）。\n"
    "重要原则：只依据提供的【项目目标】【上游材料】和【本任务说明】作答；"
    "若任务需要数值/数据而你手上没有真实数据，明确标注'数据不可得，以下为方法说明'，"
    "绝不编造数字或声称算出了不存在的结果。\n"
    "数值可核验（2026-09-03 汇总v1.2 ①）：凡涉及计算/统计/权重/比率，必须在交付中"
    "【引用输入数据】并【给出算式或可复现的步骤/代码】，让复核人能用同样输入复算出结果；"
    "无法复现的计算要显式声明。宁可给方法不给数字，也不要编造看似精确的结果。"
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
        parts = [f"任务：{task.title}\n",
                 f"描述：{task.description or '（无补充说明）'}\n"]
        if task.project_goal:
            parts.append(f"项目目标：{task.project_goal}\n")
        if task.upstream:
            parts.append(f"上游材料/源数据：\n{task.upstream}\n")
        if task.review_opinion:
            parts.append(f"上次复核意见（本次需修正）:\n{task.review_opinion}\n")
        parts.append(f"要求交付：{', '.join(wants)}\n\n请产出交付文档。")
        prompt = "".join(parts)
        content = self._llm.complete(SYSTEM_PROMPT, prompt)
        # 打回重修死循环修复（2026-09-03 D6）：重跑若复用同 file_ref，deliverable 幂等键
        # 同内容会被吞 → 无新产出事件 → 状态不进 in_review → 每轮重跑烧 token。
        # 文件已存在（重跑/修稿）→ 用递增后缀，保证每次产出是新 deliverable。
        base = self._artifacts / f"{task.task_id}.md"
        n = 0
        target = base
        while target.exists():
            n += 1
            target = self._artifacts / f"{task.task_id}.retry{n}.md"
        target.write_text(content, encoding="utf-8")
        file_ref = target.name
        return {"file_ref": file_ref, "summary": f"已产出《{task.title}》交付文档"
                + (f"（第{n+1}稿）" if n else ""),
                "content_len": len(content), "usage": self._llm.usage()}
