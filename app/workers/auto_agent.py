"""AutoAgentWorker —— NG 自研 agent 常驻执行：自动认领 builtin 任务并干活。

闭环自动化：用户给目标 → 平台拆任务分人 → builtin agent 自动执行 → 产出回报 → 自动交接复核。
不依赖人工跑 CLI；租约防并发；已产出/非 builtin 归属/非 in_progress 一律跳过。
"""
from __future__ import annotations

import hashlib
import time
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
        if ctx["status"] != TaskStatus.IN_PROGRESS:
            return
        # 汇总v1.1 ③ + D6（2026-09-03）：needs_changes/reject 打回的任务要能重做——
        # 有产出但被打回(有非pass review结论) → 允许重新执行；否则有产出则跳过。
        # 但打回次数 ≥3 → 不再自动重跑（防无限烧 token，留待人工介入/换 agent）。
        rework = False
        rework_count = 0
        for e in evs:
            if e["event_type"] == events.EventType.REVIEW_DECIDED.value \
                    and e["payload"].get("verdict") in ("needs_changes", "reject"):
                rework = True
                rework_count += 1
        if rework_count >= 3:
            return   # 反复打回超限，停自动重跑（人工介入）
        if ctx["has_deliverable"] and not rework:
            return
        if ctx["owner"] is None or not self._is_builtin(ctx["owner"]):
            # 只自动执行 builtin 任务；非 builtin（owner 失配/人工/外接）不进则发悬空告警，
            # 避免"owner_agent 显示名/注册名失配 → 静默挂起 30+ 分钟零告警"
            self._maybe_mark_stalled(tid, ctx)
            return
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

    _STALL_S = 1800   # 30 分钟无进展判悬空

    def _maybe_mark_stalled(self, tid: str, ctx: dict):
        """in_progress 且无人执行（owner 非 builtin/空）+ 30 分钟无心跳 → 发一次 task.stalled。

        外接 openclaw 任务（有 handover/transferred）或已 stalled 过的不重复告警。
        """
        evs = self._log.replay(task_id=tid)
        now = time.time()
        last_act = 0.0
        external = False
        already = False
        for e in evs:
            if e["event_type"] in (events.EventType.AGENT_HEARTBEAT.value,
                                   events.EventType.TASK_STATE_CHANGED.value):
                last_act = max(last_act, e.get("created_at_ts") or 0)
            if e["event_type"] in (events.EventType.HANDOVER_CREATED.value,
                                   events.EventType.AGENT_TRANSFERRED.value):
                external = True
            if e["event_type"] == events.EventType.TASK_STALLED.value:
                already = True
        if already or external or now - last_act < self._STALL_S:
            return
        try:
            self._log.append(events.new_event(
                events.EventType.TASK_STALLED, "auto_agent",
                {"owner": ctx.get("owner"), "reason": "归属无 builtin 执行者且长时间无进展"},
                project_id=ctx.get("project_id"), task_id=tid,
                idempotency_key=f"stalled:{tid}"))
            print(f"[auto_agent] 悬空任务告警: {tid} owner={ctx.get('owner')}", flush=True)
        except Exception:   # 幂等冲突等忽略，不打断 tick
            pass

    def _project_goal(self, pid) -> str:
        """项目目标（project.created 的 goal）。"""
        for e in self._log.replay(project_id=pid):
            if e["event_type"] == events.EventType.PROJECT_CREATED.value:
                return e["payload"].get("goal", "")
        return ""

    def _upstream_content(self, tid: str) -> str:
        """该任务 depends_on 上游已产出的交付物内容（供下游引用真实数据）。

        E1 修复（2026-09-03）：只取每个上游任务【最新一次】deliverable——
        旧错误版（被打回/重跑的历史稿）不拼进上下文，避免污染下游
        （algo 实锤：任务2 挑了第一版错统计）。
        """
        deps = []
        for e in self._log.replay(task_id=tid):
            if e["event_type"] == events.EventType.TASK_CREATED.value:
                deps = list(e["payload"].get("depends_on") or [])
                break
        chunks = []
        for dep in deps:
            latest_fr = None
            for e in self._log.replay(task_id=dep):
                if e["event_type"] == events.EventType.DELIVERABLE_SUBMITTED.value:
                    fr = e["payload"].get("file_ref", "")
                    if fr:
                        latest_fr = fr   # 覆盖式：循环到末尾即最新
            if latest_fr:
                try:
                    from app.storage.artifacts import resolve_artifact
                    p = resolve_artifact(latest_fr)   # P0-3：仅 artifacts 内相对路径
                    chunks.append(f"【上游任务最新产出 {latest_fr}】\n"
                                  + p.read_text(encoding="utf-8", errors="replace")[:4000])
                except Exception:
                    pass
        return "\n\n".join(chunks)

    def _deliverable_evidence(self, file_ref: str) -> dict:
        """产出证据（汇总 ⑤ + P0-3，2026-09-03/09-05）：与人工路径一致，
        仅读 artifacts 目录内相对路径，算内容长度/哈希/预览。"""
        try:
            from app.storage.artifacts import resolve_artifact
            p = resolve_artifact(file_ref)
            content = p.read_text(encoding="utf-8", errors="replace")
            return {"content_len": len(content),
                    "content_hash": hashlib.sha256(content.encode()).hexdigest()[:16],
                    "preview": content[:500], "file_missing": False}
        except Exception as e:
            return {"file_missing": True, "note": f"读取失败: {e}"}

    def _review_opinion(self, tid: str) -> str:
        """最近一次打回(needs_changes/reject)的复核修改意见（④），供重跑时修正。"""
        opinion = ""
        for e in self._log.replay(task_id=tid):
            if e["event_type"] == events.EventType.REVIEW_DECIDED.value \
                    and e["payload"].get("verdict") in ("needs_changes", "reject"):
                op = e["payload"].get("opinion", "")
                if op:
                    opinion = op   # 覆盖式：循环到末尾即最新打回意见
        return opinion

    def _execute(self, tid: str, ctx: dict):
        goal = self._project_goal(ctx["project_id"]) if ctx.get("project_id") else ""
        upstream = self._upstream_content(tid)
        opinion = self._review_opinion(tid)
        task = TaskContext(task_id=tid, title=ctx["title"], description=ctx["description"],
                           deliverables=ctx["deliverables"],
                           project_goal=goal, upstream=upstream,
                           review_opinion=opinion)
        try:
            result = BuiltinAgent().execute(task)
        except Exception as e:      # noqa: BLE001
            # 汇总v1.2 ⑤（2026-09-03）：执行失败写可审计事件，不静默（tick 仍会 print 兜底）
            self._log.append(events.new_event(
                events.EventType.AGENT_EXECUTION_FAILED, f"agent:{AGENT_NAME}",
                {"error": str(e)[:300], "title": ctx.get("title", "")},
                project_id=ctx.get("project_id"), task_id=tid,
                idempotency_key=f"execfail:{tid}:" + hashlib.sha256(
                    str(e).encode()).hexdigest()[:10]))
            raise
        idem = f"deliverable:{tid}:{result['file_ref']}:" + hashlib.sha256(
            f"done|{result['summary']}".encode()).hexdigest()[:10]
        # auto 路径交付证据字段（汇总 ⑤）：与人工路径一致，带 content_len/hash/preview
        evidence = self._deliverable_evidence(result["file_ref"])
        # 三.4 version 随返工递增（v1.1.2）：与人工路径口径一致
        prior = sum(1 for e in self._log.replay(task_id=tid)
                    if e["event_type"] == events.EventType.DELIVERABLE_SUBMITTED.value)
        payload = {"file_ref": result["file_ref"], "version": prior + 1,
                   "verdict": "done", "summary": result["summary"]}
        payload.update(evidence)
        self._log.append(events.new_event(
            events.EventType.DELIVERABLE_SUBMITTED, f"agent:{AGENT_NAME}",
            payload,
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
        """如任务无【未决】review 则创建（幂等），reviewer 取已指派。

        汇总v1.1 ②③（2026-09-03）：打回重修后重新交付应能二次复核——
        仅当存在未决 review 才跳过；已决(needs_changes/reject 打回过)则建新 review。
        """
        evs = self._log.replay(task_id=tid)
        req_ids = {e["payload"].get("review_id") for e in evs
                   if e["event_type"] == events.EventType.REVIEW_REQUESTED.value}
        dec_ids = {e["payload"].get("review_id") for e in evs
                   if e["event_type"] == events.EventType.REVIEW_DECIDED.value}
        if req_ids - dec_ids:
            return   # 有待决复核
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
            idempotency_key=f"review:req:{tid}:{uuid.uuid4().hex[:8]}"))
