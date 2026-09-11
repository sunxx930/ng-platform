"""运行全部调度 Worker。

用法: python -m app.workers            # 常驻
      python -m app.workers --once     # 跑一轮就退出

逻辑已抽到 app/workers/runner.py：桌面单机版（scripts/desktop_entry.py）
在打包应用内用后台线程调用 run_forever，让单机安装包也具备自动开工/执行/复核。
"""
from __future__ import annotations

import sys

from app.storage.event_log import EventLog
from app.workers.runner import collect_task_ids, run_forever  # noqa: F401 (兼容旧引用)


def main():
    once = "--once" in sys.argv
    log = EventLog()
    dburl = __import__("os").environ.get("DATABASE_URL")
    if dburl:   # Worker 也接 DB 正源 + 投影物化
        from sqlalchemy import create_engine
        from app.storage.projection import Projector
        engine = create_engine(dburl)
        log = EventLog(engine=engine, projector=Projector(engine))
        print("[workers] 事件正源=PostgreSQL + 投影物化", flush=True)
    run_forever(log, once=once)


if __name__ == "__main__":
    main()
