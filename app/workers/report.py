"""ReportWorker —— 状态/结论变化主动推送责任人/复核人/用户。

通知 P1-2（2026-09-01）：前端通知改为事件派生（GET /notifications，按项目 owner 隔离），
本 worker 的 JSONL 队列保留为历史兼容（不破坏 worker 启动），docstring 更新说明。
"""
from __future__ import annotations

from app.workers.base import Worker


class ReportWorker(Worker):
    name = "report"
    interval_s = 30.0

    def process(self, task_id: str) -> None:
        """**已停写**（2026-09-27）。

        原实现每次 tick 把「最新事件是状态变更/复核/审批」的任务重新追加到
        notifications.jsonl；但任务完成后它的"最新事件"永远停在那类事件上，
        于是**每 30 秒重复写同一条**、且没有任何去重。

        实测代价：dev 实例连跑 19 天 → 该文件 6,288,387 行 / **1.53 GB**，
        抽样去重后仅 796 条唯一内容；而**没有任何代码读它**（通知早已事件派生，
        见 GET /notifications → _derive_notifications(log.replay())）。
        除了白占磁盘，它当时还落在 iCloud 同步目录里，拖垮同步进程。

        保留本 Worker 类（不删）是为了不动 workers 启动顺序与既有测试。
        """
        return

