"""投影物化（P1-1，2026-09-01）—— 事件溯源读模型。

事件仍是唯一正源（append-only）；projects/tasks/agents/feedback_proj/usage_proj
是从事件流折叠的派生读模型，读取 O(事件数) → O(行数)。可整体 rebuild 重建。

一致性与容错：
- 同步折叠：EventLog.append 同事务内调 apply（inserted 才调，幂等重试不双计）。
- 乐观锁：task.state_changed 走守卫 UPDATE（expected_version 匹配才 +1），
  冲突抛 OptimisticLockConflict → 事件事务整体回滚。
- 防御：非乐观锁投影错误吞掉打印告警（不阻断事件提交，漂移靠 rebuild 收敛）；
  只有 OptimisticLockConflict 向上抛（有意回滚）。
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from app.domain import events as evmod


class OptimisticLockConflict(Exception):
    """任务状态并发冲突：事件插入与版本自增同事务，冲突回滚（事件也不落库）。"""


class Projector:
    """投影器：把事件折叠到读模型表。纯 SQL，风格对齐 event_log。"""

    def __init__(self, engine):
        self._engine = engine

    # ---------- 折叠入口（EventLog.append 同事务调用） ----------
    def apply(self, conn, event: dict, expected_version: int | None = None):
        """折叠单条事件。expected_version 仅 task.state_changed 的乐观锁用。"""
        etype = event["event_type"]
        try:
            if etype == evmod.EventType.PROJECT_CREATED.value:
                self._project_created(conn, event)
            elif etype in (evmod.EventType.PROJECT_PAUSED.value,
                           evmod.EventType.PROJECT_ARCHIVED.value):
                self._project_status(conn, event)
            elif etype == evmod.EventType.TASK_CREATED.value:
                self._task_created(conn, event)
            elif etype == evmod.EventType.TASK_STATE_CHANGED.value:
                self._task_state_changed(conn, event, expected_version)
            elif etype == evmod.EventType.AGENT_ASSIGNED.value:
                self._agent_assigned(conn, event)
            elif etype == evmod.EventType.DELIVERABLE_SUBMITTED.value:
                self._deliverable_submitted(conn, event)
            elif etype == evmod.EventType.AGENT_REGISTERED.value:
                self._agent_registered(conn, event)
            elif etype == evmod.EventType.FEEDBACK_SUBMITTED.value:
                self._feedback_submitted(conn, event)
            elif etype == evmod.EventType.USAGE_RECORDED.value:
                self._usage_recorded(conn, event)
        except OptimisticLockConflict:
            raise   # 有意回滚（事件也不落库）
        except Exception as e:      # noqa: BLE001
            # 防御：投影 bug 不阻断事件正源，漂移靠 rebuild 收敛
            print(f"[projector] 折叠 {etype} 失败（忽略，事件已落库）: {e}", flush=True)

    # ---------- 折叠规则 ----------
    def _project_created(self, conn, event):
        conn.execute(text(
            """INSERT INTO projects (id, title, goal, status, owner_id, event_count, user_id)
               VALUES (:id, :title, :goal, 'active', :owner, 1, :uid)
               ON CONFLICT (id) DO UPDATE SET title=:title, goal=:goal,
                 owner_id=COALESCE(projects.owner_id, :owner),
                 user_id=COALESCE(projects.user_id, :uid),
                 event_count=projects.event_count+1"""),
            {"id": event["project_id"], "title": event["payload"].get("title", ""),
             "goal": event["payload"].get("goal", ""), "owner": event.get("user_id"),
             "uid": event.get("user_id")})

    def _project_status(self, conn, event):
        conn.execute(text(
            "UPDATE projects SET status=:st, event_count=event_count+1 WHERE id=:id"),
            {"st": "paused" if event["event_type"] == evmod.EventType.PROJECT_PAUSED.value
                    else "archived", "id": event["project_id"]})

    def _task_created(self, conn, event):
        pid = event.get("project_id")
        if pid is None:
            return   # 孤儿任务：无项目归属不投影（FK 不允许 NULL，与看板语义一致）
        deadline_ts = event["payload"].get("deadline_ts")
        deliverables = json.dumps(
            {"deliverables": list(event["payload"].get("deliverables") or [])},
            ensure_ascii=False)
        conn.execute(text(
            """INSERT INTO tasks (id, project_id, title, description, status, deadline_ts, evidence)
               VALUES (:id, :pid, :title, :desc, 'todo', :dl, :ev)
               ON CONFLICT (id) DO UPDATE SET title=:title, description=:desc,
                 deadline_ts=COALESCE(:dl, tasks.deadline_ts)"""),
            {"id": event["task_id"], "pid": pid,
             "title": event["payload"].get("title", ""),
             "desc": event["payload"].get("description"),
             "dl": deadline_ts, "ev": deliverables})

    def _task_state_changed(self, conn, event, expected_version):
        tid = event.get("task_id")
        to = event["payload"].get("to")
        # 乐观锁守卫：版本匹配才 +1；无 expected_version（worker/兼容路径）放行
        # CAST 显式类型，避免 None 时 AmbiguousParameter（text() 里 :: 会被当绑定符）
        r = conn.execute(text(
            """UPDATE tasks SET status=:to, expected_version=expected_version+1
               WHERE id=:tid
                 AND (CAST(:ev AS bigint) IS NULL OR expected_version=CAST(:ev AS bigint))"""),
            {"tid": tid, "to": to, "ev": expected_version})
        if r.rowcount == 0 and expected_version is not None:
            raise OptimisticLockConflict(
                f"任务 {tid} 状态并发冲突：expected_version={expected_version}，请刷新后重试")

    def _agent_assigned(self, conn, event):
        tid = event.get("task_id")
        role = event["payload"].get("role", "owner")
        agent = event["payload"].get("agent")
        col = "owner_agent_name" if role == "owner" else "reviewer_agent_name"
        conn.execute(text(f"UPDATE tasks SET {col}=:a WHERE id=:tid"),
                     {"a": agent, "tid": tid})

    def _deliverable_submitted(self, conn, event):
        tid = event.get("task_id")
        conn.execute(text(
            "UPDATE tasks SET has_deliverable=TRUE WHERE id=:tid"), {"tid": tid})

    def _agent_registered(self, conn, event):
        p = event["payload"]
        name = p.get("name")
        if not name:
            return
        # latest-wins：同 name 覆盖。history 存完整 payload，get_agents 读它保证与 replay 逐字一致
        conn.execute(text(
            """INSERT INTO agents (id, name, capability, role, status, permission, executor, history)
               VALUES (:id, :name, :cap, :role, :st, :perm, :exec, :hist)
               ON CONFLICT (name) DO UPDATE SET
                 capability=:cap, role=:role, status=:st, permission=:perm,
                 executor=:exec, history=:hist"""),
            {"id": str(uuid.uuid4()), "name": name,
             "cap": json.dumps(p.get("capability", ""), ensure_ascii=False),
             "role": p.get("role"), "st": p.get("status", "available"),
             "perm": p.get("permission", "L1"), "exec": p.get("executor", "builtin"),
             "hist": json.dumps(p, ensure_ascii=False)})

    def _feedback_submitted(self, conn, event):
        p = event["payload"]
        conn.execute(text(
            """INSERT INTO feedback_proj (content, contact, rating, actor, created_at_ts)
               VALUES (:content, :contact, :rating, :actor, :ts)"""),
            {"content": p.get("content", ""), "contact": p.get("contact", ""),
             "rating": p.get("rating"), "actor": event.get("actor"),
             "ts": event.get("created_at_ts", 0)})

    def _usage_recorded(self, conn, event):
        p = event["payload"]
        conn.execute(text(
            """INSERT INTO usage_proj
               (project_id, task_id, label, provider, model, input_tokens, output_tokens, created_at_ts)
               VALUES (:pid, :tid, :label, :provider, :model, :it, :ot, :ts)"""),
            {"pid": event.get("project_id"), "tid": event.get("task_id"),
             "label": p.get("label"), "provider": p.get("provider"),
             "model": p.get("model"), "it": p.get("input_tokens", 0),
             "ot": p.get("output_tokens", 0), "ts": event.get("created_at_ts", 0)})

    # ---------- 读取（返回与 _derive_* replay 纯函数完全一致的形状） ----------
    def get_projects(self, viewer: str | None = None) -> list[dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(text(
                """SELECT id, title, goal, status FROM projects
                   WHERE status <> 'archived'
                     AND (CAST(:viewer AS uuid) IS NULL
                          OR owner_id = CAST(:viewer AS uuid))
                   ORDER BY created_at"""),
                {"viewer": viewer}).mappings().all()
        return [{"project_id": str(r["id"]), "title": r["title"],
                 "goal": r["goal"], "status": r["status"]} for r in rows]

    def get_tasks(self, project_id: str) -> list[dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(text(
                """SELECT id, title, status, owner_agent_name, reviewer_agent_name,
                          has_deliverable
                   FROM tasks WHERE project_id=:pid ORDER BY created_at"""),
                {"pid": project_id}).mappings().all()
        return [{"task_id": str(r["id"]), "title": r["title"],
                 "status": r["status"], "owner": r["owner_agent_name"],
                 "reviewer": r["reviewer_agent_name"],
                 "has_deliverable": r["has_deliverable"]} for r in rows]

    def get_task_context(self, task_id: str) -> dict | None:
        with self._engine.connect() as conn:
            r = conn.execute(text(
                """SELECT id, title, description, status, owner_agent_name,
                          reviewer_agent_name, deadline_ts, evidence
                   FROM tasks WHERE id=:tid"""),
                {"tid": task_id}).mappings().first()
        if r is None:
            return None
        try:
            ev = json.loads(r["evidence"]) if isinstance(r["evidence"], str) \
                else (r["evidence"] or {})
        except Exception:
            ev = {}
        return {"task_id": str(r["id"]), "title": r["title"],
                "description": r["description"],
                "deliverables": ev.get("deliverables", []),
                "owner": r["owner_agent_name"], "reviewer": r["reviewer_agent_name"],
                "status": r["status"], "deadline_ts": r["deadline_ts"]}

    def get_task_row(self, task_id: str) -> dict | None:
        """PATCH 乐观锁读：status/project_id/expected_version。"""
        with self._engine.connect() as conn:
            r = conn.execute(text(
                """SELECT id, project_id, status, expected_version
                   FROM tasks WHERE id=:tid"""),
                {"tid": task_id}).mappings().first()
        if r is None:
            return None
        return {"task_id": str(r["id"]),
                "project_id": str(r["project_id"]) if r["project_id"] else None,
                "status": r["status"], "expected_version": r["expected_version"]}

    def get_project_id(self, task_id: str) -> str | None:
        with self._engine.connect() as conn:
            r = conn.execute(text(
                "SELECT project_id FROM tasks WHERE id=:tid"),
                {"tid": task_id}).mappings().first()
        return str(r["project_id"]) if r and r["project_id"] else None

    def project_exists(self, project_id: str) -> bool:
        with self._engine.connect() as conn:
            r = conn.execute(text(
                "SELECT 1 FROM projects WHERE id=:pid"), {"pid": project_id}).first()
        return r is not None

    def get_agents(self) -> list[dict]:
        """latest-wins 注册表：读 history JSONB（完整 payload）→ 与 _agents_registry 逐字一致。"""
        with self._engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT history FROM agents ORDER BY name")).mappings().all()
        out = []
        for r in rows:
            h = r["history"]
            if isinstance(h, str):
                try:
                    out.append(json.loads(h))
                except Exception:
                    continue
            else:
                out.append(h)   # psycopg 已把 JSONB 解析成 dict
        return out

    def get_feedback(self) -> list[dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(text(
                """SELECT content, contact, rating, actor, created_at_ts
                   FROM feedback_proj ORDER BY id DESC""")).mappings().all()
        return [{"actor": r["actor"], "content": r["content"],
                 "contact": r["contact"] or "", "rating": r["rating"],
                 "ts": r["created_at_ts"]} for r in rows]

    def get_usage(self) -> dict:
        with self._engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT label, provider, input_tokens, output_tokens FROM usage_proj")
                ).mappings().all()
        tin = sum(r["input_tokens"] for r in rows)
        tout = sum(r["output_tokens"] for r in rows)
        by_label: dict[str, dict] = {}
        for r in rows:
            b = by_label.setdefault(r["label"] or "?",
                                    {"calls": 0, "input_tokens": 0, "output_tokens": 0})
            b["calls"] += 1
            b["input_tokens"] += r["input_tokens"]
            b["output_tokens"] += r["output_tokens"]
        return {"calls": len(rows), "input_tokens": tin, "output_tokens": tout,
                "context_limit": 1_000_000, "by_label": by_label}

    # ---------- 重建（事件唯一正源，投影可整体推倒重建） ----------
    def rebuild(self):
        """TRUNCATE 全部投影表 + 重放所有事件。需要超管连接（ng_app 无 TRUNCATE）。"""
        with self._engine.begin() as conn:
            conn.execute(text(
                "TRUNCATE projects, tasks, agents, feedback_proj, usage_proj CASCADE"))
            rows = conn.execute(text(
                "SELECT * FROM events ORDER BY id")).mappings().all()
            for r in rows:
                event = dict(r)
                p = event.get("payload")
                if isinstance(p, str):
                    try:
                        event["payload"] = json.loads(p)
                    except Exception:
                        event["payload"] = {}
                # psycopg 已把 JSONB 解析成 dict，直接可用；None → 空
                elif p is None:
                    event["payload"] = {}
                if event.get("project_id") is not None:
                    event["project_id"] = str(event["project_id"])
                if event.get("task_id") is not None:
                    event["task_id"] = str(event["task_id"])
                if event.get("user_id") is not None:
                    event["user_id"] = str(event["user_id"])
                self.apply(conn, event)
        return True
