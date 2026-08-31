"""运行全部调度 Worker。

用法: python -m app.workers            # 常驻
      python -m app.workers --once     # 跑一轮就退出
"""
from __future__ import annotations

import sys
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
    """从事件日志收集所有任务 ID（骨架从 JSONL 推导；接 DB 后改查询）。"""
    task_ids = set()
    for e in log.replay():
        if e.get("task_id"):
            task_ids.add(e["task_id"])
    return list(task_ids)


def main():
    once = "--once" in sys.argv
    log = EventLog()
    dburl = __import__("os").environ.get("DATABASE_URL")
    if dburl:   # Worker 也接 DB 正源（阻塞 1）
        from sqlalchemy import create_engine
        log = EventLog(engine=create_engine(dburl))
        print(f"[workers] 事件正源=PostgreSQL", flush=True)
    workers = [
        AutoStartWorker(log),
        HeartbeatWorker(log),
        DeadlineWorker(log),
        BlockerWorker(log),
        ReportWorker(log),
        TransferEscalationWorker(log),
        AutoAgentWorker(log),
    ]
    print(f"[workers] 启动 {len(workers)} 个 Worker: "
          f"{', '.join(w.name for w in workers)}", flush=True)
    while True:
        task_ids = collect_task_ids(log)
        for w in workers:
            try:
                w.tick(task_ids)
            except Exception as e:
                print(f"[workers] {w.name} 异常: {e}", flush=True)
        if once:
            print("[workers] --once 跑完，退出", flush=True)
            break
        time.sleep(min(w.interval_s for w in workers))


if __name__ == "__main__":
    main()
