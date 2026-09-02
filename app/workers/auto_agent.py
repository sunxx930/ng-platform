"""AutoAgentWorker —— NG 自研 agent 常驻执行：自动认领 builtin 任务并干活。

闭环自动化：用户给目标 → 平台拆任务分人 → builtin agent 自动执行 → 产出回报 → 自动交接复核。
不依赖人工跑 CLI；租约防并发；已产出/非 builtin 归属/非 in_progress 一律跳过。
"""
from __future__ import annotations

import hashlib
import uuid

from app.agents.builtin import AGENT_NAME, BuiltinAgent, TaskContext
from app.domain import events
from app.domain.task import TaskStatus
from app.workers.base import Worker


class AutoAgentWorker(Worker):
    name = "auto_agent"
    interval_s = 20.0

    def tick(self, task_ids: list[str]):
        for tid in task_ids:
            try:
                self._maybe_execute(tid)
            except Exception as e:
                print(f"[auto_agent] {tid} 执行异常: {e}", flush=True)

    def _maybe_execute(self, tid: str):
        evs = self._log.replay(task_id=tid)
        ctx = {"owner": None, "status": TaskStatus.TODO, "has_deliverable": False,
               "title": "", "description": "", "deliverables": [], "project_id": None}
        for e in evs:
            p = e["payload"]
            if e["event_type"] == events.EventType.AGENT_ASSIGNED.value \
                    and p.get("role", "owner") == "owner":
                ctx["owner"] = p.get("agent")
            elif e["event_type"] == events.EventType.TASK_CREATED.value:
                ctx.update(title=p.get("title", ""), description=p.get("description", ""),
                           deliverables=list(p.get("deliverables") or []),
                           project_id=e.get("project_id"))
            elif e["event_type"] == events.EventType.TASK_STATE_CHANGED.value:
                ctx["status"] = TaskStatus(p["to"])
            elif e["event_type"] == events.EventType.DELIVERABLE_SUBMITTED.value:
                ctx["has_deliverable"] = True
        if ctx["status"] != TaskStatus.IN_PROGRESS or ctx["has_deliverable"]:
            return
        if ctx["owner"] is None or not self._is_builtin(ctx["owner"]):
            return   # 只自动执行 NG 自研 builtin agent 的任务
        if not self.acquire_lease(tid):
            return
        try:
            self._execute(tid, ctx)
        finally:
            self.release(tid)

    def _is_builtin(self, owner: str) -> bool:
        """owner 是否注册为 builtin（NG 自研算力直跑）。

        P0 修复（2026-08-31）：与 main._agents_registry 一致用 **latest wins**，
        取该 agent 最近一次注册的 executor（先 builtin 后 openclaw → 判 openclaw，不误执行）。
        P2-4（2026-09-01）：DB 投影模式走 agents 表 latest-wins（O(1) 查询，替代全量回放）；
        JSONL 模式保留全量回放（无投影表）。
        """
        if self._log._engine is not None and getattr(self._log, "projector", None) is not None:
            # 投影 agents 表已 latest-wins（每 name 一行），直接查 executor
            for a in self._log.projector.get_agents():
                if a.get("name") == owner:
                    return a.get("executor", "builtin") == "builtin"
            return False
        found = None
        for e in self._log.replay():
            if e["event_type"] == events.EventType.AGENT_REGISTERED.value \
                    and e["payload"].get("name") == owner:
                found = e["payload"].get("executor", "builtin")
        return found == "builtin" if found is not None else False

    def _project_goal(self, pid) -> str:
        """项目目标（project.created 的 goal）。"""
        for e in self._log.replay(project_id=pid):
            if e["event_type"] == events.EventType.PROJECT_CREATED.value:
                return e["payload"].get("goal", "")
        return ""

    def _upstream_content(self, tid: str) -> str:
        """该任务 depends_on 上游已产出的交付物内容（供下游引用真实数据）。"""
        deps = []
        for e in self._log.replay(task_id=tid):
            if e["event_type"] == events.EventType.TASK_CREATED.value:
                deps = list(e["payload"].get("depends_on") or [])
                break
        chunks = []
        for dep in deps:
            # 上游产出 file_ref 从 deliverable.submitted 事件读，读文件内容
            for e in self._log.replay(task_id=dep):
                if e["event_type"] == events.EventType.DELIVERABLE_SUBMITTED.value:
                    fr = e["payload"].get("file_ref", "")
                    if fr:
                        try:
                            from pathlib import Path
                            p = Path(fr)
                            if not p.is_absolute():
                                p = Path.cwd() / p
                            if p.exists():
                                chunks.append(f"【上游任务产出 {fr}】\n"
                                              + p.read_text(encoding="utf-8", errors="replace")[:4000])
                        except Exception:
                            pass
        return "\n\n".join(chunks)

    def _execute(self, tid: str, ctx: dict):
        goal = self._project_goal(ctx["project_id"]) if ctx.get("project_id") else ""
        upstream = self._upstream_content(tid)
        task = TaskContext(task_id=tid, title=ctx["title"], description=ctx["description"],
                           deliverables=ctx["deliverables"],
                           project_goal=goal, upstream=upstream)
        result = BuiltinAgent().execute(task)
        idem = f"deliverable:{tid}:{result['file_ref']}:" + hashlib.sha256(
            f"done|{result['summary']}".encode()).hexdigest()[:10]
        self._log.append(events.new_event(
            events.EventType.DELIVERABLE_SUBMITTED, f"agent:{AGENT_NAME}",
            {"file_ref": result["file_ref"], "version": 1, "verdict": "done",
             "summary": result["summary"]},
            project_id=ctx["project_id"], task_id=tid, idempotency_key=idem))
        self._log.append(events.new_event(
            events.EventType.TASK_STATE_CHANGED, f"agent:{AGENT_NAME}",
            {"from": TaskStatus.IN_PROGRESS.value, "to": TaskStatus.IN_REVIEW.value,
             "trigger": "deliverable.submitted"},
            project_id=ctx["project_id"], task_id=tid,
            idempotency_key=f"deliverableadv:{tid}:" + hashlib.sha256(
                f"done|{result['file_ref']}".encode()).hexdigest()[:10]))
        # 记录 LLM 用量（1M 上下文限制展示）
        for u in (result.get("usage") or []):
            self._log.append(events.new_event(
                events.EventType.USAGE_RECORDED, "llm",
                {"provider": u.get("provider"), "model": u.get("model"),
                 "input_tokens": u.get("input_tokens", 0),
                 "output_tokens": u.get("output_tokens", 0), "label": "agent_execute"},
                project_id=ctx["project_id"], task_id=tid,
                idempotency_key=f"usage:agent_execute:{int(u.get('ts', 0))}"))
        # P1 修复（2026-08-31）：与 API submit_deliverable 一致——done 交接复核时触发 review.requested
        self._ensure_review_requested(tid, ctx["project_id"])
        print(f"[auto_agent] {tid} 自动完成产出 → in_review"
              f"（{result['file_ref']}，{result['content_len']} 字符）", flush=True)

    def _ensure_review_requested(self, tid: str, project_id: str | None):
        """如任务尚无 review.requested 则创建（幂等），reviewer 取已指派。"""
        evs = self._log.replay(task_id=tid)
        if any(e["event_type"] == events.EventType.REVIEW_REQUESTED.value for e in evs):
            return
        reviewer = None
        for e in evs:
            if e["event_type"] == events.EventType.AGENT_ASSIGNED.value \
                    and e["payload"].get("role", "owner") == "reviewer":
                reviewer = e["payload"].get("agent")
        self._log.append(events.new_event(
            events.EventType.REVIEW_REQUESTED, f"agent:{AGENT_NAME}",
            {"review_id": str(uuid.uuid4()), "trigger": "deliverable.submitted",
             "reviewer": reviewer},
            project_id=project_id, task_id=tid,
            idempotency_key=f"review:req:{tid}"))
