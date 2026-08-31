"""TransferEscalationWorker —— 转移出去的任务超时无结果回报 → 升级。

架构文档八（主动推进调度器）：调度器硬性要求「不依赖 Agent 自己记得回来干活」。
转移（agent.transferred）后默认 30 分钟仍无产出回报、任务还停在待办/进行中 → 发 task.blocked
升级事件（幂等，按任务+时间桶去重），提醒用户接管或换人。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from app.domain import events
from app.domain.task import TaskStatus
from app.workers.base import Worker


class TransferEscalationWorker(Worker):
    name = "transfer_escalation"
    interval_s = 60.0
    STALE_TIMEOUT_S = 1800  # 转移后 30 分钟无结果视为滞留

    def __init__(self, log, transfer_dir: Path | None = None, stale_timeout_s: int | None = None):
        super().__init__(log)
        self._transfer_dir = Path(
            transfer_dir or os.environ.get(
                "OPENCLAW_SHARED_DIR",
                os.path.expanduser("~/.openclaw/shared/messages"))).expanduser()
        self._stale_timeout = stale_timeout_s or self.STALE_TIMEOUT_S

    # 目录扫描型 Worker：不走 per-task 租约，tick 直接扫
    def tick(self, task_ids: list[str]):
        self.scan()

    def scan(self):
        if not self._transfer_dir.exists():
            return
        now = time.time()
        for f in sorted(self._transfer_dir.glob("ng-platform-*-transfer-*.md")):
            meta = self._parse_transfer(f)
            if not meta:
                continue
            task_id = meta["target_task"]
            transferred_at = meta["created_at_ts"]
            if self._task_stuck(task_id) and (now - transferred_at) > self._stale_timeout:
                # 每次转移只升级一次：key 用 task+transfer_id（稳定），payload 不放易变字段
                # （stale_s 会随扫描变化 → 同 key 不同内容触发 P1 幂等冲突）
                self.emit(task_id, events.EventType.TASK_BLOCKED,
                          {"escalate": True, "reason": "transfer_stale",
                           "transfer_id": meta["transfer_id"]},
                          idempotency_key=f"transfer_esc:{task_id}:{meta['transfer_id']}")
                # P1 修复（2026-08-31）：升级同时推进状态到 blocked，
                # 否则 _task_stuck 恒 True → 跨转移重复升级
                self._block_task(task_id, meta["transfer_id"])

    def _block_task(self, task_id: str, transfer_id: str):
        """按状态机合法跳推进到 blocked（TODO→IN_PROGRESS→BLOCKED / IN_PROGRESS→BLOCKED）。"""
        state, has_result = self._task_state(task_id)
        if has_result or state in (TaskStatus.BLOCKED, TaskStatus.CANCELLED,
                                   TaskStatus.COMPLETED, TaskStatus.ARCHIVED):
            return
        base = f"transfer_block:{task_id}:{transfer_id}"
        if state == TaskStatus.TODO:
            self.emit(task_id, events.EventType.TASK_STATE_CHANGED,
                      {"from": TaskStatus.TODO.value, "to": TaskStatus.IN_PROGRESS.value,
                       "trigger": "transfer_escalation"},
                      idempotency_key=base + ":start")
            self.emit(task_id, events.EventType.TASK_STATE_CHANGED,
                      {"from": TaskStatus.IN_PROGRESS.value, "to": TaskStatus.BLOCKED.value,
                       "trigger": "transfer_escalation"},
                      idempotency_key=base + ":block")
        elif state == TaskStatus.IN_PROGRESS:
            self.emit(task_id, events.EventType.TASK_STATE_CHANGED,
                      {"from": TaskStatus.IN_PROGRESS.value, "to": TaskStatus.BLOCKED.value,
                       "trigger": "transfer_escalation"},
                      idempotency_key=base + ":block")

    def _task_state(self, task_id: str) -> tuple[TaskStatus, bool]:
        has_result = False
        state = TaskStatus.TODO
        for e in self._log.replay(task_id=task_id):
            if e["event_type"] == events.EventType.DELIVERABLE_SUBMITTED.value:
                has_result = True
            if e["event_type"] == events.EventType.TASK_STATE_CHANGED.value:
                state = TaskStatus(e["payload"]["to"])
        return state, has_result

    def _task_stuck(self, task_id: str) -> bool:
        """True = 任务仍停在待办/进行中且没有任何产出回报。"""
        state, has_result = self._task_state(task_id)
        if has_result:
            return False
        return state in (TaskStatus.TODO, TaskStatus.IN_PROGRESS)

    @staticmethod
    def _parse_transfer(f: Path) -> dict | None:
        """解析转移文件 frontmatter → {transfer_id, target_task, created_at_ts}。"""
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            return None
        transfer_json = None
        created_at = None
        for line in text.splitlines():
            if line.startswith("transfer: "):
                try:
                    transfer_json = json.loads(line[len("transfer: "):])
                except Exception:
                    transfer_json = None
            if line.startswith("created_at: "):
                created_at = line[len("created_at: "):].strip()
        if not transfer_json:
            return None
        task_id = str(transfer_json.get("target_task") or "").strip()
        # 只认合法 UUID：历史转移文件里混有 'task-xxxx' 前缀的非法 id，replay 会炸 DB
        try:
            __import__("uuid").UUID(task_id)
        except ValueError:
            return None
        if not task_id:
            return None
        # 优先用 frontmatter 时间（mtime 会被消息系统改 status 时刷新，不可靠）
        created_ts = f.stat().st_mtime
        if created_at:
            try:
                created_ts = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%S%z").timestamp()
            except ValueError:
                pass
        return {"transfer_id": transfer_json.get("transfer_id", ""),
                "target_task": task_id, "created_at_ts": created_ts}
