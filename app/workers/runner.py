"""Worker 运行器（可复用）：CLI(`python -m app.workers`) 与桌面单机版共用。

桌面版入口 desktop_entry 在打包应用内起一个后台线程跑 run_forever(app.main.log)，
让单机安装包也具备自动开工/执行/复核（之前只在 docker 里独立进程）。"""
from __future__ import annotations

import time

from app.storage.event_log import EventLog
from app.workers.auto_start import AutoStartWorker
from app.workers.heartbeat import HeartbeatWorker
from app.workers.deadline import DeadlineWorker
from app.workers.blocker import BlockerWorker
from app.workers.report import ReportWorker
from app.workers.transfer_escalation import TransferEscalationWorker
from app.workers.auto_agent import AutoAgentWorker


def collect_task_ids(log: EventLog) -> list[str]:
    """所有任务 ID。DB 查 events 表；JSONL 全量回放。"""
    if log._engine is not None:
        from sqlalchemy import text
        with log._engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT DISTINCT task_id FROM events WHERE task_id IS NOT NULL")).fetchall()
        return [str(r[0]) for r in rows]
    ids = set()
    for e in log.replay():
        if e.get("task_id"):
            ids.add(e["task_id"])
    return list(ids)


def build_workers(log: EventLog) -> list:
    return [AutoStartWorker(log), HeartbeatWorker(log), DeadlineWorker(log),
            BlockerWorker(log), ReportWorker(log), TransferEscalationWorker(log),
            AutoAgentWorker(log)]


def run_forever(log: EventLog, once: bool = False):
    """跑 worker 循环（阻塞）。桌面版放后台线程，CLI 放主线程。"""
    workers = build_workers(log)
    print(f"[workers] 启动 {len(workers)} 个 Worker: {', '.join(w.name for w in workers)}",
          flush=True)
    while True:
        task_ids = collect_task_ids(log)
        for w in workers:
            try:
                w.tick(task_ids)
            except Exception as e:      # noqa: BLE001
                print(f"[workers] {w.name} 异常: {e}", flush=True)
        if once:
            print("[workers] --once 跑完，退出", flush=True)
            return
        time.sleep(min(w.interval_s for w in workers))
