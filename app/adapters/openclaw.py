"""openclaw 适配器 —— NG 平台与 openclaw agent 系统之间的桥。

用途：
- 把 NG 的任务/消息派发给 openclaw 的 agent（如龙虾 lobster）
- 把 agent 的执行结果回收回 NG 的项目/任务
- 支持把项目/任务「转移」给指定 agent（transfer）

机制：
- 通过 `openclaw agent` CLI 或 gateway 触发 agent turn
- 通过 ~/.openclaw/shared/messages/ 文件消息与 agent 异步通信（本项目同套机制）
- 结果通过 ledger 记录，事件入库供审计回放

配置（环境变量）：
- OPENCLAW_BIN: openclaw 可执行路径（默认 openclaw）
- OPENCLAW_GATEWAY_URL: gateway WebSocket/HTTP 地址（默认走 CLI）
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.adapters.base import AgentExecutor, AgentTask

# 默认 openclaw 共享消息目录（与龙虾等 agent 同套通信）
DEFAULT_SHARED_DIR = Path(os.environ.get("OPENCLAW_SHARED_DIR", os.path.expanduser("~/.openclaw/shared/messages")))
# openclaw 可执行
OPENCLAW_BIN = os.environ.get("OPENCLAW_BIN", "openclaw")


@dataclass
class AgentResult:
    """一次 agent 派发的结果。"""
    agent_id: str
    task_id: str | None
    status: str            # dispatched | done | failed | timeout
    message: str
    output: str = ""
    error: str = ""
    transfer_id: str = ""
    at: float = field(default_factory=time.time)


def _cli_agent(message: str, agent_id: str, session_key: str | None = None) -> str:
    """通过 openclaw agent CLI 触发一次 agent turn（同步，拿回复）。

    openclaw agent CLI 默认走 gateway。Windows 兼容（2026-09-01）：
    Docker 里跑 openclaw gateway，配 OPENCLAW_GATEWAY_URL + OPENCLAW_GATEWAY_TOKEN
    指向它，本机无需装 openclaw 即可同步调用（否则走本机 gateway）。
    """
    cmd = [OPENCLAW_BIN, "agent", "--agent", agent_id, "-m", message, "--json"]
    if session_key:
        cmd += ["--session-key", session_key]
    # subprocess 默认继承父进程 env；OPENCLAW_GATEWAY_URL/TOKEN 由部署方注入即可
    # （Windows：Docker gateway + 这两个变量 → 本机无需装 openclaw CLI）
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"openclaw agent 调用失败 rc={proc.returncode}: {proc.stderr[:500]}")
    return proc.stdout


def dispatch_task(agent_id: str, task: dict[str, Any], *, via: str = "cli") -> AgentResult:
    """把 NG 任务派发给 openclaw agent 执行。

    via="cli"    同步调用 openclaw agent，拿回复（适合快速验证）
    via="message" 写文件消息异步投递（适合长任务，agent 完成后回写）
    """
    task_id = task.get("id") or task.get("task_id")
    prompt = (
        f"【NG 平台任务派发】\n"
        f"task_id: {task_id}\n"
        f"project: {task.get('project_id')}\n"
        f"目标: {task.get('goal') or task.get('title')}\n"
        f"说明: {task.get('description') or ''}\n"
        f"请执行并把结果按结构化返回（JSON）。"
    )
    if via == "message":
        return _dispatch_via_message(agent_id, task_id, prompt)
    # via cli
    try:
        out = _cli_agent(prompt, agent_id)
        return AgentResult(agent_id=agent_id, task_id=task_id, status="done",
                           message=prompt, output=out[:2000])
    except Exception as e:
        return AgentResult(agent_id=agent_id, task_id=task_id, status="failed",
                           message=prompt, error=str(e))


def transfer_agent(agent_id: str, target_project: str, target_task: str,
                   payload: dict[str, Any] | None = None) -> AgentResult:
    """把当前上下文/任务「转移」给指定 agent —— 生成交接消息。

    会在 shared/messages 写入一封给 agent 的交接信，含项目/任务/待办，
    agent 处理后回写结果到 ledger（由 NG 事件层采集）。
    """
    payload = payload or {}
    # 幂等键：agent+project+task+payload 哈希 → 同请求复用同 transfer_id
    idem = f"{agent_id}:{target_project}:{target_task}:{json.dumps(payload, sort_keys=True)}"
    transfer_id = f"ng-{__import__('hashlib').sha256(idem.encode()).hexdigest()[:12]}-{agent_id}"
    # 已存在同 id 文件 → 返回既存（不重复写，幂等）
    existing = next(DEFAULT_SHARED_DIR.glob(f"ng-platform-{agent_id}-transfer-{transfer_id.split('-')[1]}.md"), None) if DEFAULT_SHARED_DIR.exists() else None
    if existing:
        return AgentResult(agent_id=agent_id, task_id=target_task,
                           status="deduped", message=f"已存在: {existing.name}",
                           transfer_id=transfer_id)
    msg = {
        "from": "ng-platform",
        "to": agent_id,
        "status": "unread",
        "urgency": "high",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "transfer": {
            "transfer_id": transfer_id,
            "target_project": target_project,
            "target_task": target_task,
            "payload": payload,
        },
        "body": (
            f"【NG 平台 Agent 转移】\n"
            f"transfer_id: {transfer_id}\n"
            f"请接手项目 {target_project} 的任务 {target_task}。\n"
            f"完成后通过 NG 平台回报：POST /tasks/{target_task}/deliverables"
            f"（query 参数 file_ref=产出路径&summary=摘要&verdict=done|blocked）。"
            f"verdict=done 会自动交接给复核人。\n"
            f"payload: {json.dumps(payload, ensure_ascii=False)}"
        ),
    }
    body = msg["body"]
    DEFAULT_SHARED_DIR.mkdir(parents=True, exist_ok=True)
    fname = DEFAULT_SHARED_DIR / f"ng-platform-{agent_id}-transfer-{transfer_id.split(chr(45))[1]}.md"
    fname.write_text(_render_frontmatter(msg), encoding="utf-8")
    return AgentResult(agent_id=agent_id, task_id=target_task,
                       status="dispatched", message=body,
                       transfer_id=transfer_id)


def _render_frontmatter(msg: dict) -> str:
    body = msg.pop("body", "")
    lines = ["---"]
    for k, v in msg.items():
        if isinstance(v, str):
            lines.append(f"{k}: {v}")
        else:
            lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


def collect_results(ledger_dir: Path, agent_id: str | None = None) -> list[dict]:
    """从共享目录/ledger 收集 agent 回写的执行结果。"""
    results = []
    shared = DEFAULT_SHARED_DIR if shared_exists() else None
    for p in (ledger_dir, shared):
        if not p or not p.exists():
            continue
        for f in p.glob("*.jsonl"):
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if agent_id and r.get("agent") != agent_id:
                    continue
                results.append(r)
    return results


def shared_exists() -> bool:
    return DEFAULT_SHARED_DIR.exists()


class OpenClawExecutor(AgentExecutor):
    """openclaw 运行时实现（架构文档十二：Agent 执行复用 OpenClaw，NG 自研编排层在上）。

    实现 AgentExecutor 接口，供主编排层统一调用；后续可加 claude_sdk 等实现。
    """

    def dispatch(self, task: AgentTask, *, via: str = "message") -> AgentResult:
        if via == "cli":
            return dispatch_task(task.agent_id,
                                 {"id": task.task_id, "project_id": task.project_id,
                                  "goal": task.prompt}, via="cli")
        # message 模式 = transfer（写共享消息异步投递）
        return transfer_agent(task.agent_id, task.project_id, task.task_id,
                              {"prompt": task.prompt})

    def collect_results(self, agent_id: str | None = None) -> list[dict]:
        ledger = DEFAULT_SHARED_DIR if shared_exists() else Path("data/ledger")
        return collect_results(ledger, agent_id)
