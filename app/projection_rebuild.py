"""投影重建命令 —— 事件唯一正源，投影表可整体推倒重建（P1-1）。

用法:
  DATABASE_URL=postgresql+psycopg://postgres@localhost:5433/ng_platform \
    python -m app.projection_rebuild

TRUNCATE + 重放全部事件。需超管连接（ng_app 无 TRUNCATE 权限）。
幂等、原子：失败整体回滚，可反复跑。
"""
from __future__ import annotations

import os

from sqlalchemy import create_engine

from app.storage.projection import Projector


def main():
    dburl = os.environ.get("MIGRATION_DATABASE_URL",
                           os.environ.get("DATABASE_URL", ""))
    if not dburl:
        print("无 DATABASE_URL/MIGRATION_DATABASE_URL，跳过重建"); return
    proj = Projector(create_engine(dburl))
    print("[rebuild] 清空投影表 + 重放全部事件…", flush=True)
    proj.rebuild()
    print("[rebuild] 完成", flush=True)


if __name__ == "__main__":
    main()
