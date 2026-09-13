"""AutoReviewWorker —— 自动复核决策（v1.2.5）：让单机版闭环"跑到底"，不停在 in_review。

策略（保守、可关）：
- 用 NG_AUTO_REVIEW=0 可关闭（默认开）。
- 只处理【待复核且无结论】的任务，且复核人**不是真人**（被指派为注册用户名的 reviewer 留给人工）。
- 数值任务：先跑自动复算(autocheck)，PASS → 通过；非 PASS/不可用 → **留给人工**（不瞎判）。
- 非数值任务：交付证据存在且非 file_missing → pass；file_missing → needs_changes（走既有返工+≥3次保险丝）。
- 决策落 review.decided(actor=auto-reviewer) + 状态联动（与人工路径同幂等键），审计可查、人工仍可在决策前介入。
"""
from __future__ import annotations

import os

from app.domain import events
from app.domain.task import TaskStatus
from app.workers.base import Worker

_NUM_WORDS = ['计算', '统计', '权重', '比率', '胜率', '均值', '检验', '概率', '金额', '算', '汇总', '指标']


def _auto_review_on() -> bool:
    return str(os.environ.get("NG_AUTO_REVIEW", "1")).strip().lower() not in {"0", "false", "no", "off"}


class AutoReviewWorker(Worker):
    name = "auto_review"
    interval_s = 20.0

    def tick(self, task_ids: list[str]):
        if not _auto_review_on():
            return
        for tid in task_ids:
            try:
                self._maybe_decide(tid)
            except Exception as e:      # noqa: BLE001
                print(f"[auto_review] {tid} 异常: {e}", flush=True)

    def _builtin_names(self) -> set:
        names = set()
        for e in self._log.replay():
            if e["event_type"] == events.EventType.AGENT_REGISTERED.value:
                names.add(e["payload"].get("name"))
        return names

    def _maybe_decide(self, tid: str):
        evs = self._log.replay(task_id=tid)
        state = TaskStatus.TODO
        title = ""
        reviewer = None
        latest = None
        pid = None
        for e in evs:
            et = e["event_type"]
            p = e["payload"]
            if e.get("project_id"):
                pid = e["project_id"]
            if et == events.EventType.TASK_CREATED.value:
                title = p.get("title", "")
            elif et == events.EventType.AGENT_ASSIGNED.value and p.get("role", "owner") == "reviewer":
                reviewer = p.get("agent")
            elif et == events.EventType.TASK_STATE_CHANGED.value:
                state = TaskStatus(p["to"])
            elif et == events.EventType.DELIVERABLE_SUBMITTED.value and p.get("file_ref"):
                latest = p
        if state != TaskStatus.IN_REVIEW:
            return
        req = None
        dec = set()
        for e in evs:
            if e["event_type"] == events.EventType.REVIEW_REQUESTED.value:
                req = e["payload"].get("review_id") or req
            elif e["event_type"] == events.EventType.REVIEW_DECIDED.value:
                dec.add(e["payload"].get("review_id"))
        if not req or req in dec:
            return
        # 真人复核人 → 不抢人工
        if reviewer and reviewer not in self._builtin_names():
            return
        # 决定 verdict
        verdict, opinion = None, ""
        if any(w in title for w in _NUM_WORDS):
            try:
                from app.services.autocheck import run_autocheck
                r = run_autocheck(pid, tid, self._log)
                if r.get("verdict") == "PASS":
                    verdict, opinion = "pass", f"自动复核：{r.get('note', '自动复算通过')}"
                else:
                    return      # 数值任务非 PASS/不可用 → 交人工
            except Exception:      # noqa: BLE001
                return
        else:
            if latest and not latest.get("file_missing"):
                verdict, opinion = "pass", "自动复核：交付证据完整（规则判定）"
            else:
                verdict, opinion = "needs_changes", "自动复核：未找到交付文件，请补齐后再交"
        self._log.append(events.new_event(
            events.EventType.REVIEW_DECIDED, "auto-reviewer",
            {"review_id": req, "verdict": verdict, "opinion": opinion},
            project_id=pid, task_id=tid,
            idempotency_key=f"review:dec:{req}"))
        if state == TaskStatus.IN_REVIEW:
            to = TaskStatus.COMPLETED if verdict == "pass" else TaskStatus.IN_PROGRESS
            self._log.append(events.new_event(
                events.EventType.TASK_STATE_CHANGED, "auto-reviewer",
                {"from": TaskStatus.IN_REVIEW.value, "to": to.value,
                 "trigger": f"auto_review:{verdict}", "verdict": verdict, "opinion": opinion},
                project_id=pid, task_id=tid,
                idempotency_key=f"review_outcome:{tid}:{verdict}:{req}"))
