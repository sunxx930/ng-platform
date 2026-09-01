"""运行 NG agent 执行一个任务（模型中立执行层）。

用法（mac/Linux）: .venv/bin/python -m app.agents.run <task_id> [--executor builtin|claude_sdk] [--base ...] [--token ...]
用法（Windows）: .venv\\Scripts\\python -m app.agents.run <task_id> [--executor ...]
流程: 读任务上下文 → 执行器产出 → 落盘 artifacts/ → POST deliverables(verdict=done) → 自动 in_review
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import httpx

from app.adapters.base import AgentTask
from app.agents.builtin import AGENT_NAME, BuiltinAgent, TaskContext


def _produce(executor: str, task: TaskContext) -> dict:
    """用指定执行器产出交付物并落盘 artifacts/<task_id>.md。"""
    artifacts = Path("artifacts")
    artifacts.mkdir(parents=True, exist_ok=True)
    if executor == "claude_sdk":
        from app.adapters.claude_sdk import ClaudeSDKExecutor
        agent_task = AgentTask(agent_id=AGENT_NAME, task_id=task.task_id,
                               project_id=task.project_id,
                               prompt=f"{task.title}\n{task.description}\n交付：{', '.join(task.deliverables) or '交付文档'}")
        r = ClaudeSDKExecutor().dispatch(agent_task)
        if r.status != "done":
            raise RuntimeError(f"claude_sdk 执行失败: {r.error}")
        content = r.output
        file_ref = f"artifacts/{task.task_id}.md"
        (artifacts / f"{task.task_id}.md").write_text(content, encoding="utf-8")
        return {"file_ref": file_ref, "summary": f"已产出《{task.title}》交付文档", "content_len": len(content)}
    return BuiltinAgent().execute(task)


def main():
    ap = argparse.ArgumentParser(description="NG agent 执行任务（模型中立执行层）")
    ap.add_argument("task_id", help="要执行的任务 ID")
    ap.add_argument("--executor", default="builtin", choices=("builtin", "claude_sdk"))
    ap.add_argument("--base", default=os.environ.get("NG_API_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--token", default=os.environ.get("NG_AGENT_TOKEN", "l1-agent-token"))
    args = ap.parse_args()

    base = args.base.rstrip("/")
    headers = {"Authorization": f"Bearer {args.token}"}
    with httpx.Client(timeout=300) as c:
        # 1) 读任务上下文
        r = c.get(f"{base}/tasks/{args.task_id}/context", headers=headers)
        r.raise_for_status()
        data = r.json()
        task = TaskContext(task_id=args.task_id, project_id=data.get("project_id", ""),
                           title=data.get("title", ""),
                           description=data.get("description", ""),
                           deliverables=data.get("deliverables", []))
        # 2) 执行器产出（builtin=自研算力 / claude_sdk=Anthropic SDK）
        print(f"[ng-agent:{AGENT_NAME}] 执行任务: {task.title}（executor={args.executor}）", flush=True)
        result = _produce(args.executor, task)
        print(f"[ng-agent] 产出 {result['file_ref']}（{result['content_len']} 字符）", flush=True)
        # 3) 回报平台 → 自动交接复核
        r = c.post(f"{base}/tasks/{args.task_id}/deliverables",
                   params={"file_ref": result["file_ref"],
                           "summary": result["summary"],
                           "verdict": "done", "agent": AGENT_NAME},
                   headers=headers)
        r.raise_for_status()
        resp = r.json()
        print(f"[ng-agent] 回报完成，任务状态 → {resp['status']}", flush=True)


if __name__ == "__main__":
    main()
