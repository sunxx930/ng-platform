"""事件日志 —— append-only 审计正源。

补强项 15.2：events 禁止 UPDATE/DELETE（DB 角色层面约束）；幂等去重。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine


class IdempotencyConflict(Exception):
    """幂等键复用但内容（意图）不一致 → API 层映射 409（P1 反例修复）。"""


def _content(e: dict) -> tuple:
    """事件内容指纹（不含 idempotency_key/created_at_ts）——用于判定幂等是否同意图。

    归一化：DB 里 UUID 列读回是 uuid.UUID 对象、payload 可能是字符串，统一转 str/dict 后再比。
    """
    payload = e.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            pass
    return (str(e.get("event_type")), str(e.get("actor")),
            json.dumps(payload, sort_keys=True, ensure_ascii=False),
            str(e.get("project_id")), str(e.get("task_id")),
            str(e.get("user_id")))


class EventLog:
    """事件日志存储。默认落本地 JSONL（无 DB 时可运行），配 DB 后走 PostgreSQL。"""

    def __init__(self, engine: Engine | None = None, path: Path | None = None):
        self._engine = engine
        self._path = path or Path("data/events.jsonl")

    def append(self, event: dict[str, Any]) -> dict:
        event.setdefault("idempotency_key", None)
        if self._engine is not None:
            return self._append_db(event)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        ik = event.get("idempotency_key")
        if ik:
            existing = self._find_key(ik)
            if existing is not None:
                # 内容一致=幂等返回；内容不同=冲突（P1：同 key 不同意图不再静默丢写）
                if _content(existing) == _content(event):
                    return existing
                raise IdempotencyConflict(
                    f"幂等键 {ik} 已被不同意图占用（内容不一致），拒绝覆盖")
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def _find_key(self, ik: str) -> dict | None:
        if not self._path.exists():
            return None
        for line in open(self._path, encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("idempotency_key") == ik:
                return r
        return None

    def _append_db(self, event: dict) -> dict:
        ik = event.get("idempotency_key")
        with self._engine.begin() as conn:
            # DB 幂等：ON CONFLICT 原子去重（F4 修复，消除 SELECT-then-INSERT 竞态）
            r = conn.execute(text(
                """INSERT INTO events
                   (event_type, actor, payload, project_id, task_id, user_id, idempotency_key)
                   VALUES (:event_type, :actor, :payload, :project_id, :task_id, :user_id, :idempotency_key)
                   ON CONFLICT (idempotency_key) DO NOTHING
                   RETURNING id
                """), {
                "event_type": event["event_type"],
                "actor": event["actor"],
                "payload": json.dumps(event["payload"], ensure_ascii=False),
                "project_id": event.get("project_id"),
                "task_id": event.get("task_id"),
                "user_id": event.get("user_id"),
                "idempotency_key": ik,
            }).mappings().first()
            # 无返回 = key 冲突：内容一致=幂等；内容不同=抛冲突（P1 反例修复）
            if r is None and ik is not None:
                existing = conn.execute(text(
                    "SELECT event_type, actor, payload, project_id, task_id, user_id "
                    "FROM events WHERE idempotency_key=:ik"), {"ik": ik}).mappings().first()
                if existing is not None:
                    stored = {
                        "event_type": existing["event_type"],
                        "actor": existing["actor"],
                        "payload": existing["payload"],
                        "project_id": existing["project_id"],
                        "task_id": existing["task_id"],
                        "user_id": existing["user_id"],
                    }
                    if _content(stored) != _content(event):
                        raise IdempotencyConflict(
                            f"幂等键 {ik} 已被不同意图占用（内容不一致），拒绝覆盖")
        return event

    def replay(self, project_id: str | None = None,
               task_id: str | None = None) -> list[dict]:
        if self._engine is not None:
            return self._replay_db(project_id, task_id)
        rows = []
        if not self._path.exists():
            return rows
        for line in open(self._path, encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if project_id and r.get("project_id") != project_id:
                continue
            if task_id and r.get("task_id") != task_id:
                continue
            rows.append(r)
        return rows

    def _replay_db(self, project_id, task_id) -> list[dict]:
        q = "SELECT * FROM events WHERE 1=1"
        params = {}
        if project_id:
            q += " AND project_id=:project_id"; params["project_id"] = project_id
        if task_id:
            q += " AND task_id=:task_id"; params["task_id"] = task_id
        q += " ORDER BY id"
        with self._engine.connect() as conn:
            rows = conn.execute(text(q), params).mappings().all()
        out = []
        for r in rows:
            d = dict(r)
            # 与 JSONL 路径一致：UUID 列转 str，补 created_at_ts（heartbeat/deadline 依赖）
            if d.get("project_id") is not None:
                d["project_id"] = str(d["project_id"])
            if d.get("task_id") is not None:
                d["task_id"] = str(d["task_id"])
            if d.get("user_id") is not None:
                d["user_id"] = str(d["user_id"])
            if d.get("created_at"):
                d["created_at_ts"] = d["created_at"].timestamp()
            out.append(d)
        return out
