"""NG AI Platform —— MVP 骨架 FastAPI 入口。

端点（架构文档十）：
  POST /projects                         创建项目
  POST /projects/{id}/messages           写入用户消息
  GET  /projects/{id}/context            读取项目上下文
  POST /projects/{id}/tasks              创建/确认任务
  PATCH /tasks/{id}/state                提交状态变化（状态机校验）
  POST /tasks/{id}/deliverables          提交产出
  POST /reviews/{id}/decision            提交复核结论
  POST /approvals/{id}/decision          提交用户审批
  GET  /projects/{id}/audit              查询审计事件（回放）
  POST /projects/{id}/pause              暂停项目

openclaw 接口（新增）：
  POST /agents/transfer                  把任务转移给 openclaw agent
"""

import hashlib
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.domain import events
from app.domain.task import (TaskStatus, ReviewVerdict, InvalidTransition,
                             transition)
from app.security import permission as perm
from app.security.approval_gate import ApprovalGate, PendingApproval
from app.security.auth import require_auth
from app.services.llm import LLMClient, LLMConfigError
from app.services.requirement_parser import RequirementParser
from app.services.team_matcher import match_team
from app.storage.event_log import EventLog, IdempotencyConflict

app = FastAPI(title="NG AI Platform", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok"}

# 事件正源：设置 DATABASE_URL 时用 PostgreSQL（含 append-only/幂等约束），否则 JSONL
import os
log = EventLog()
_dburl = os.environ.get("DATABASE_URL")
if _dburl:
    # DB 正源：失败必须显式报错，绝不静默回退 JSONL（阻塞 1）
    from sqlalchemy import create_engine
    engine = create_engine(_dburl, pool_pre_ping=True)
    with engine.connect() as _c:      # 启动即校验连接
        _c.execute(__import__('sqlalchemy').text("SELECT 1"))
    log = EventLog(engine=engine)
    print(f"[main] 事件正源=PostgreSQL ({_dburl.split('@')[-1]})", flush=True)

# 通用审批门（L3/L4 动作）：ensure_approved 已批准放行，否则建请求 + 抛 PendingApproval
# 传 callable 动态读当前 log（log 会在 DB 模式替换 / 测试 monkeypatch）
gate = ApprovalGate(lambda: log)

def require_level(action: str, actor_level: int | None = None):
    """权限守卫：actor_level 缺省按 L1（骨架简化，接真鉴权后替换）。"""
    level = actor_level if actor_level is not None else 1
    perm.check(level, action)


def _err(ev: events.EventType, **kw) -> dict:
    return events.new_event(ev, actor="api", payload=kw)


# ---------- 项目 ----------
@app.post("/projects")
def create_project(title: str, goal: str, auth: dict = Depends(require_auth)):
    pid = str(uuid.uuid4())
    log.append(events.new_event(
        events.EventType.PROJECT_CREATED, "user",
        {"title": title, "goal": goal}, project_id=pid))
    return {"project_id": pid, "status": "active"}


@app.get("/projects")
def list_projects(auth: dict = Depends(require_auth)):
    """项目列表（从事件溯源推导：PROJECT_CREATED + PROJECT_PAUSED）。供看板。"""
    require_level("read_project", auth["level"])
    projects: dict[str, dict] = {}
    for e in log.replay():
        if e["event_type"] == events.EventType.PROJECT_CREATED.value:
            projects[e["project_id"]] = {
                "project_id": e["project_id"],
                "title": e["payload"].get("title", ""),
                "goal": e["payload"].get("goal", ""),
                "status": "active",
            }
        elif e["event_type"] == events.EventType.PROJECT_PAUSED.value \
                and e.get("project_id") in projects:
            projects[e["project_id"]]["status"] = "paused"
    return {"projects": list(projects.values())}


@app.get("/projects/{pid}/tasks")
def list_tasks(pid: str, auth: dict = Depends(require_auth)):
    """项目任务看板数据（从事件推导：title/status/owner/reviewer/has_deliverable）。"""
    require_level("read_project", auth["level"])
    evs = log.replay(project_id=pid)
    if not evs:
        raise HTTPException(404, "project not found")
    tasks: dict[str, dict] = {}
    for e in evs:
        tid = e.get("task_id")
        if not tid:
            continue
        t = tasks.setdefault(tid, {"task_id": tid, "title": "",
                                   "status": TaskStatus.TODO.value,
                                   "owner": None, "reviewer": None,
                                   "has_deliverable": False})
        p = e["payload"]
        if e["event_type"] == events.EventType.TASK_CREATED.value:
            t["title"] = p.get("title", "")
        elif e["event_type"] == events.EventType.AGENT_ASSIGNED.value:
            t[p.get("role", "owner")] = p.get("agent")
        elif e["event_type"] == events.EventType.TASK_STATE_CHANGED.value:
            t["status"] = p["to"]
        elif e["event_type"] == events.EventType.DELIVERABLE_SUBMITTED.value:
            t["has_deliverable"] = True
    return {"tasks": list(tasks.values())}


@app.get("/projects/{pid}/context")
def project_context(pid: str, auth: dict = Depends(require_auth)):
    evs = log.replay(project_id=pid)
    if not evs:
        raise HTTPException(404, "project not found")
    goal = next((e["payload"].get("goal") for e in evs
                 if e["event_type"] == events.EventType.PROJECT_CREATED), None)
    return {"project_id": pid, "goal": goal, "events": len(evs)}


@app.post("/projects/{pid}/pause")
def pause_project(pid: str, auth: dict = Depends(require_auth)):
    # 权限：暂停需 L3
    if int(auth["level"]) < int(perm.Level.L3_FLOW):
        raise HTTPException(403, "权限不足: pause_project 需 L3_FLOW")
    # 通用审批门（阻塞3）：已批准放行，否则自动建审批请求并抛 PendingApproval(409)
    gate.ensure_approved(pid, "pause_project")
    log.append(events.new_event(
        events.EventType.PROJECT_PAUSED, "user", {}, project_id=pid))
    return {"project_id": pid, "status": "paused"}


@app.get("/projects/{pid}/audit")
def audit(pid: str, task_id: Optional[str] = Query(default=None), auth: dict = Depends(require_auth)):
    return {"events": log.replay(project_id=pid, task_id=task_id)}


# ---------- 消息 / 需求解析 ----------
def _agents_registry() -> list[dict]:
    """从 agent.registered 事件重建注册表（按 name 去重，latest wins）。"""
    reg: dict[str, dict] = {}
    for e in log.replay():
        if e["event_type"] == events.EventType.AGENT_REGISTERED.value:
            reg[e["payload"]["name"]] = e["payload"]
    return list(reg.values())


def _parse_and_create(pid: str, goal: str) -> list[dict]:
    """需求解析 + 团队匹配 + 建任务（闭环第 3 步前门）。返回 created tasks。"""
    agents = _agents_registry()
    # 只把 builtin（NG 自研，平台能自动执行）agent 名单给 LLM 参考，
    # 避免 LLM 凭名字推荐外部 openclaw agent → 任务卡等外部执行
    builtin_names = [a["name"] for a in agents
                     if a.get("executor", "builtin") == "builtin"]
    parsed = RequirementParser(LLMClient()).parse_goal(goal, builtin_names)
    log.append(events.new_event(
        events.EventType.GOAL_PARSED, "system",
        {"summary": parsed.summary, "tasks": [t.title for t in parsed.tasks]},
        project_id=pid,
        idempotency_key=f"goalparse:{pid}:" + hashlib.sha256(
            goal.encode()).hexdigest()[:10]))
    created = []
    for draft in parsed.tasks:
        tid = str(uuid.uuid4())
        log.append(events.new_event(
            events.EventType.TASK_CREATED, "system",
            {"title": draft.title, "description": draft.description,
             "deliverables": draft.deliverables, "status": TaskStatus.TODO.value},
            project_id=pid, task_id=tid))
        team = match_team(draft, agents)
        if team["owner"]:
            log.append(events.new_event(
                events.EventType.AGENT_ASSIGNED, "system",
                {"agent": team["owner"], "role": "owner"}, project_id=pid, task_id=tid))
        if team["reviewer"]:
            log.append(events.new_event(
                events.EventType.AGENT_ASSIGNED, "system",
                {"agent": team["reviewer"], "role": "reviewer"}, project_id=pid, task_id=tid))
        created.append({"task_id": tid, "title": draft.title,
                        "owner": team["owner"], "reviewer": team["reviewer"]})
    return created


@app.post("/projects/{pid}/messages")
def post_message(pid: str, body: str, parse: bool = False,
                 auth: dict = Depends(require_auth)):
    """写入用户消息。parse=true → 视为项目目标，走需求解析 + 团队匹配 + 自动建任务。"""
    mid = str(uuid.uuid4())
    log.append(events.new_event(
        events.EventType.MESSAGE_AGGREGATED, "user",
        {"message_id": mid, "body": body}, project_id=pid))
    created = []
    if parse:
        try:
            created = _parse_and_create(pid, body)
        except (LLMConfigError, ValueError, RuntimeError) as ex:
            raise HTTPException(503, f"需求解析失败（算力未配置或模型输出异常）: {ex}")
    return {"message_id": mid, "created_tasks": created}


# ---------- Agent 注册中心 ----------
@app.post("/agents/register")
def register_agent(name: str, capability: str = "", role: str = "",
                   status: str = "available", permission: str = "L1",
                   executor: str = "builtin",   # builtin=NG 自研(算力直跑) | openclaw=外接
                   auth: dict = Depends(require_auth)):
    """注册 agent（结构化目录，不自动建 Agent）。latest wins，可审计。"""
    require_level("write_message", auth["level"])
    # 幂等键内容寻址（P1）：同内容重试幂等；不同内容=更新追加（注册表 latest wins）
    log.append(events.new_event(
        events.EventType.AGENT_REGISTERED, f"user:{auth['user']}",
        {"name": name, "capability": capability, "role": role,
         "status": status, "permission": permission, "executor": executor},
        idempotency_key=f"agentreg:{name}:" + hashlib.sha256(
            f"{capability}|{role}|{status}|{permission}|{executor}".encode()).hexdigest()[:10]))
    return {"name": name, "status": status, "executor": executor}


@app.get("/agents")
def list_agents(auth: dict = Depends(require_auth)):
    """当前 agent 注册表（事件溯源重建）。"""
    require_level("read_project", auth["level"])
    return {"agents": _agents_registry()}


# ---------- LLM 算力配置（前端下拉选 provider + 输 API key） ----------
class LLMConfigIn(BaseModel):
    provider: str              # openai | anthropic | deepseek | openai_compatible
    api_key: str = ""
    model: str = ""
    base_url: str = ""         # openai_compatible 用


def _write_env_updates(updates: dict[str, str]):
    """更新项目根 .env（gitignore），保留其它行。仅 key 级覆盖，不碰 secrets 文件。"""
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    keys = set(updates)
    kept = [ln for ln in lines if ln.split("=", 1)[0].strip() not in keys]
    kept.append("")  # 分隔
    for k, v in updates.items():
        kept.append(f"{k}={v}")
    path.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")


@app.get("/agents/llm-config")
def get_llm_config(auth: dict = Depends(require_auth)):
    """当前算力配置（key 只报有无，不回显）。"""
    require_level("read_project", auth["level"])
    c = LLMClient()
    return {"provider": c.cfg.provider, "model": c.cfg.model,
            "api_key_set": bool(c.cfg.api_key)}


@app.post("/agents/llm-config")
def set_llm_config(body: LLMConfigIn, auth: dict = Depends(require_auth)):
    """选择算力提供商 + 输入 API key → 写 gitignore 的 .env（下次 LLM 调用生效）。"""
    require_level("write_message", auth["level"])
    p = body.provider.strip().lower()
    if p not in ("openai", "anthropic", "deepseek", "openai_compatible"):
        raise HTTPException(400, f"未知 provider: {p}")
    updates = {}
    if p == "openai":
        updates = {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": body.api_key.strip(),
                   "LLM_MODEL": body.model.strip() or "gpt-4o-mini"}
    elif p == "anthropic":
        updates = {"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": body.api_key.strip(),
                   "LLM_MODEL": body.model.strip() or "claude-opus-4-8"}
    elif p == "deepseek":
        updates = {"LLM_PROVIDER": "openai_compatible",
                   "LLM_BASE_URL": "https://api.deepseek.com/v1",
                   "LLM_API_KEY": body.api_key.strip(),
                   "LLM_MODEL": body.model.strip() or "deepseek-chat"}
    else:  # openai_compatible
        if not body.base_url:
            raise HTTPException(400, "openai_compatible 需 base_url")
        updates = {"LLM_PROVIDER": "openai_compatible",
                   "LLM_BASE_URL": body.base_url.strip(),
                   "LLM_API_KEY": body.api_key.strip(),
                   "LLM_MODEL": body.model.strip()}
    _write_env_updates(updates)
    return {"provider": p, "status": "saved",
            "note": "下次 LLM 调用生效（docker 模式需重建容器加载 secrets）"}


# ---------- preagent 模板库 ----------
def _load_templates() -> list[dict]:
    import json as _json
    from pathlib import Path
    p = Path(__file__).parent / "agents" / "templates.json"
    try:
        return _json.loads(p.read_text(encoding="utf-8")).get("templates", [])
    except Exception:
        return []


@app.get("/agents/templates")
def list_templates(auth: dict = Depends(require_auth)):
    """preagent 模板库：预置专业 agent，一键实例化。"""
    require_level("read_project", auth["level"])
    return {"templates": _load_templates()}


@app.post("/agents/templates/{tid}/instantiate")
def instantiate_template(tid: str, auth: dict = Depends(require_auth)):
    """一键注册 preagent 模板为平台 agent（executor=builtin，跑平台算力）。"""
    require_level("write_message", auth["level"])
    t = next((x for x in _load_templates() if x["id"] == tid), None)
    if t is None:
        raise HTTPException(404, f"模板 {tid} 不存在")
    log.append(events.new_event(
        events.EventType.AGENT_REGISTERED, f"user:{auth['user']}",
        {"name": t["name"], "capability": t["capability"], "role": t["role"],
         "status": "available", "permission": "L1",
         "executor": t.get("executor", "builtin"),
         "template_id": tid},
        idempotency_key=f"agentreg:{t['name']}:" + hashlib.sha256(
            f"{t['capability']}|{t['role']}|builtin".encode()).hexdigest()[:10]))
    return {"name": t["name"], "status": "available", "template_id": tid}


# ---------- 任务 ----------
@app.post("/projects/{pid}/tasks")
def create_task(pid: str, title: str, description: str = "",
                owner_agent: Optional[str] = None, deadline_ts: Optional[float] = None,
                auth: dict = Depends(require_auth)):
    tid = str(uuid.uuid4())
    log.append(events.new_event(
        events.EventType.TASK_CREATED, "system",
        {"title": title, "description": description,
         "status": TaskStatus.TODO.value, "deadline_ts": deadline_ts},
        project_id=pid, task_id=tid))
    if owner_agent:
        log.append(events.new_event(
            events.EventType.AGENT_ASSIGNED, "system",
            {"agent": owner_agent}, project_id=pid, task_id=tid))
    return {"task_id": tid, "status": TaskStatus.TODO.value}


@app.get("/tasks/{tid}/context")
def task_context(tid: str, auth: dict = Depends(require_auth)):
    """任务上下文（供 agent 执行前读取）：title/desc/deliverables/owner/reviewer/status/deadline。"""
    require_level("read_project", auth["level"])
    evs = log.replay(task_id=tid)
    if not evs:
        raise HTTPException(404, "task not found")
    ctx: dict = {"task_id": tid, "title": "", "description": "",
                 "deliverables": [], "owner": None, "reviewer": None,
                 "status": TaskStatus.TODO.value, "deadline_ts": None}
    for e in evs:
        p = e["payload"]
        if e["event_type"] == events.EventType.TASK_CREATED.value:
            ctx.update(title=p.get("title", ""), description=p.get("description", ""),
                       deliverables=list(p.get("deliverables") or []),
                       deadline_ts=p.get("deadline_ts"))
        elif e["event_type"] == events.EventType.AGENT_ASSIGNED.value:
            role = p.get("role", "owner")
            ctx[role] = p.get("agent") or ctx.get(role)
        elif e["event_type"] == events.EventType.TASK_STATE_CHANGED.value:
            ctx["status"] = p["to"]
    return ctx


@app.patch("/tasks/{tid}/state")
def change_state(tid: str, to: TaskStatus, actor: str = "system",
                 idempotency_key: Optional[str] = None, auth: dict = Depends(require_auth)):
    """状态转移（白名单校验）。当前骨架用事件序列推导当前状态。"""
    require_level("change_task_state", auth["level"])
    evs = log.replay(task_id=tid)
    state = TaskStatus.TODO
    project_id = None
    for e in evs:
        if e["event_type"] == events.EventType.TASK_CREATED.value:
            project_id = e.get("project_id")      # F3: 从 TASK_CREATED 解析项目
        if e["event_type"] == events.EventType.TASK_STATE_CHANGED.value:
            state = TaskStatus(e["payload"]["to"])
    # P1 幂等：同状态重试 → 200（不判非法转移）
    if state == to:
        return {"task_id": tid, "status": to.value, "idempotent": True}
    try:
        new = transition(state, to)
    except InvalidTransition as ex:
        raise HTTPException(400, str(ex))
    log.append(events.new_event(
        events.EventType.TASK_STATE_CHANGED, actor,
        {"from": state.value, "to": new.value},
        project_id=project_id, task_id=tid,       # F3: 状态事件带 project_id
        idempotency_key=idempotency_key))
    return {"task_id": tid, "status": new.value}


@app.exception_handler(perm.PermissionDenied)
async def perm_denied_handler(request: Request, exc: perm.PermissionDenied):
    return JSONResponse(status_code=403, content={"detail": str(exc)})

@app.exception_handler(perm.ApprovalRequired)
async def approval_required_handler(request: Request, exc: perm.ApprovalRequired):
    return JSONResponse(status_code=403, content={"detail": str(exc)})

@app.exception_handler(IdempotencyConflict)
async def idempotency_conflict_handler(request: Request, exc: IdempotencyConflict):
    """P1：同幂等键不同意图 → 409，不再静默丢写返回假 200。"""
    return JSONResponse(status_code=409, content={"detail": str(exc)})

@app.exception_handler(PendingApproval)
async def pending_approval_handler(request: Request, exc: PendingApproval):
    """审批门通用：动作需审批 → 409，带 approval_id 供去 POST /approvals/{id}/decision。"""
    return JSONResponse(status_code=409, content={
        "detail": str(exc), "approval_id": exc.approval_id, "scope": exc.scope})


@app.post("/tasks/{tid}/deliverables")
def submit_deliverable(tid: str, file_ref: str,
                       summary: str = "", verdict: str = "done",
                       agent: str = "agent",
                       auth: dict = Depends(require_auth)):
    """提交产出（架构文档十 + 闭环第 3 步）。

    verdict=done 且任务 in_progress → 自动推进 in_review（产出自动交接给复核人，状态机第七节）；
    verdict=blocked → 自动推进 blocked。幂等键绑定内容（P1 内容寻址）：同内容重试幂等。
    """
    require_level("submit_deliverable", auth["level"])
    evs = log.replay(task_id=tid)
    project_id = next((e.get("project_id") for e in evs
                       if e["event_type"] == events.EventType.TASK_CREATED.value), None)
    if project_id is None:
        raise HTTPException(404, "task not found")
    if verdict not in ("done", "blocked", "partial"):
        raise HTTPException(400, f"verdict 非法: {verdict}")
    state = TaskStatus.TODO
    for e in evs:
        if e["event_type"] == events.EventType.TASK_STATE_CHANGED.value:
            state = TaskStatus(e["payload"]["to"])
    idem = f"deliverable:{tid}:{file_ref}:" + hashlib.sha256(
        f"{verdict}|{summary}".encode()).hexdigest()[:10]
    log.append(events.new_event(
        events.EventType.DELIVERABLE_SUBMITTED, f"agent:{agent}",
        {"file_ref": file_ref, "version": 1, "verdict": verdict, "summary": summary},
        project_id=project_id, task_id=tid, idempotency_key=idem))
    # 闭环：Agent 提交产出 → 产出自动交接给复核人（状态机第七节）
    new_state = None
    if state == TaskStatus.IN_PROGRESS and verdict == "done":
        new_state = transition(state, TaskStatus.IN_REVIEW)
    elif state == TaskStatus.IN_PROGRESS and verdict == "blocked":
        new_state = transition(state, TaskStatus.BLOCKED)
    if new_state is not None:
        log.append(events.new_event(
            events.EventType.TASK_STATE_CHANGED, f"agent:{agent}",
            {"from": state.value, "to": new_state.value, "trigger": "deliverable.submitted"},
            project_id=project_id, task_id=tid,
            idempotency_key=f"deliverableadv:{tid}:" + hashlib.sha256(
                f"{verdict}|{file_ref}".encode()).hexdigest()[:10]))
    # P1 修复（2026-08-31）：done 自动交接复核 → 同时触发 review.requested（幂等），
    # reviewer 取 agent.assigned role=reviewer（团队匹配器已指派时）
    if new_state == TaskStatus.IN_REVIEW:
        reviewer = None
        for e in evs:
            if e["event_type"] == events.EventType.AGENT_ASSIGNED.value \
                    and e["payload"].get("role", "owner") == "reviewer":
                reviewer = e["payload"].get("agent")
        existing_review = any(e["event_type"] == events.EventType.REVIEW_REQUESTED.value
                              for e in evs)
        if not existing_review:
            rid = str(uuid.uuid4())
            log.append(events.new_event(
                events.EventType.REVIEW_REQUESTED, f"agent:{agent}",
                {"review_id": rid, "trigger": "deliverable.submitted",
                 "reviewer": reviewer},
                project_id=project_id, task_id=tid,
                idempotency_key=f"review:req:{tid}"))
    return {"task_id": tid, "deliverable": file_ref,
            "status": new_state.value if new_state else state.value}


@app.post("/tasks/{tid}/heartbeat")
def task_heartbeat(tid: str, agent: str = "agent", auth: dict = Depends(require_auth)):
    """Agent 心跳：更新任务活跃状态（F1：让 HeartbeatWorker 有据可依）。"""
    require_level("change_task_state", auth["level"])
    evs = log.replay(task_id=tid)
    project_id = next((e.get("project_id") for e in evs
                       if e["event_type"] == events.EventType.TASK_CREATED.value), None)
    log.append(events.new_event(
        events.EventType.AGENT_HEARTBEAT, agent, {},
        project_id=project_id, task_id=tid,
        idempotency_key=f"hb:{tid}:{agent}:{int(__import__('time').time()//60)}"))
    return {"task_id": tid, "beat": True}


# ---------- 复核 / 审批 ----------
@app.post("/tasks/{tid}/reviews")
def request_review(tid: str, auth: dict = Depends(require_auth)):
    """创建复核请求（绑定任务，返回 review_id）。"""
    require_level("change_task_state", auth["level"])
    evs = log.replay(task_id=tid)
    pid = next((e.get("project_id") for e in evs
                if e["event_type"] == events.EventType.TASK_CREATED.value), None)
    if pid is None:
        raise HTTPException(404, "task not found")   # 对象绑定
    # 幂等：同任务已有复核请求 → 返回同 review_id（重试不新建）
    existing = next((e for e in log.replay(task_id=tid)
                     if e["event_type"] == events.EventType.REVIEW_REQUESTED.value), None)
    if existing:
        return {"review_id": existing["payload"]["review_id"], "task_id": tid}
    rid = str(uuid.uuid4())
    log.append(events.new_event(
        events.EventType.REVIEW_REQUESTED, "system", {"review_id": rid},
        project_id=pid, task_id=tid, idempotency_key=f"review:req:{tid}"))
    return {"review_id": rid, "task_id": tid}


@app.post("/reviews/{rid}/decision")
def review_decision(rid: str, verdict: ReviewVerdict, auth: dict = Depends(require_auth)):
    """复核结论：校验 review_id 存在并绑定任务（对象绑定，缺权限 403）。"""
    require_level("change_task_state", auth["level"])
    evs = log.replay()
    req = next((e for e in evs if e["event_type"] == events.EventType.REVIEW_REQUESTED.value
                and e["payload"].get("review_id") == rid), None)
    if req is None:
        raise HTTPException(404, f"review {rid} 不存在")   # 拒绝任意 ID
    log.append(events.new_event(
        events.EventType.REVIEW_DECIDED, "reviewer",
        {"review_id": rid, "verdict": verdict.value},
        project_id=req.get("project_id"), task_id=req.get("task_id"),
        idempotency_key=f"review:dec:{rid}"))
    return {"review_id": rid, "verdict": verdict.value}


@app.post("/tasks/{tid}/approvals")
def request_approval(tid: str, scope: str = "flow_change", auth: dict = Depends(require_auth)):
    """创建审批请求（绑定任务，需 L3）。"""
    require_level("approve_action", auth["level"])
    evs = log.replay(task_id=tid)
    pid = next((e.get("project_id") for e in evs
                if e["event_type"] == events.EventType.TASK_CREATED.value), None)
    if pid is None:
        raise HTTPException(404, "task not found")
    # 幂等：同任务+scope 已有审批请求 → 返回同 approval_id（重试不新建）
    existing = next((e for e in log.replay(task_id=tid)
                     if e["event_type"] == events.EventType.APPROVAL_REQUESTED.value
                     and e["payload"].get("scope") == scope), None)
    if existing:
        return {"approval_id": existing["payload"]["approval_id"], "task_id": tid}
    aid = str(uuid.uuid4())
    log.append(events.new_event(
        events.EventType.APPROVAL_REQUESTED, "system",
        {"approval_id": aid, "scope": scope},
        project_id=pid, task_id=tid, idempotency_key=f"approval:req:{tid}:{scope}"))
    # 审批门通用化：请求审批时，任务 IN_REVIEW → PENDING_APPROVAL（状态机：待复核→待批准）
    _advance_to_pending_approval(tid, pid)
    return {"approval_id": aid, "task_id": tid}


def _advance_to_pending_approval(tid: str, pid: str | None):
    state = TaskStatus.TODO
    for e in log.replay(task_id=tid):
        if e["event_type"] == events.EventType.TASK_STATE_CHANGED.value:
            state = TaskStatus(e["payload"]["to"])
    if state == TaskStatus.IN_REVIEW:
        log.append(events.new_event(
            events.EventType.TASK_STATE_CHANGED, "system",
            {"from": TaskStatus.IN_REVIEW.value, "to": TaskStatus.PENDING_APPROVAL.value,
             "trigger": "approval.requested"},
            project_id=pid, task_id=tid,
            idempotency_key=f"to_pending_approval:{tid}"))


@app.post("/approvals/{aid}/decision")
def approval_decision(aid: str, result: str, auth: dict = Depends(require_auth)):
    """审批：校验 approval_id 存在并绑定任务，需 L3。"""
    require_level("approve_action", auth["level"])
    evs = log.replay()
    req = next((e for e in evs if e["event_type"] == events.EventType.APPROVAL_REQUESTED.value
                and e["payload"].get("approval_id") == aid), None)
    if req is None:
        raise HTTPException(404, f"approval {aid} 不存在")   # 拒绝任意 ID
    # 审批终态：已决策再决 → 同 result 幂等 200，不同 result 冲突 400（幂等重试）
    for e in evs:
        if e["event_type"] == events.EventType.APPROVAL_DECIDED.value                 and e["payload"].get("approval_id") == aid:
            if e["payload"].get("result") == result:
                return {"approval_id": aid, "result": result, "idempotent": True}
            raise HTTPException(400, f"approval {aid} 已决策为 {e['payload']['result']}，冲突")
    log.append(events.new_event(
        events.EventType.APPROVAL_DECIDED, "user",
        {"approval_id": aid, "result": result,
         "scope": req["payload"].get("scope", "")},
        project_id=req.get("project_id"), task_id=req.get("task_id"),
        idempotency_key=f"approval:dec:{aid}"))
    # 审批门通用化：批准 → 任务完成；拒绝 → 退回进行中（状态机：待批准→完成/退回）
    if req.get("task_id"):
        _apply_approval_outcome(req["task_id"], req.get("project_id"), result)
    return {"approval_id": aid, "result": result}


def _apply_approval_outcome(tid: str, pid: str | None, result: str):
    state = TaskStatus.TODO
    for e in log.replay(task_id=tid):
        if e["event_type"] == events.EventType.TASK_STATE_CHANGED.value:
            state = TaskStatus(e["payload"]["to"])
    if state != TaskStatus.PENDING_APPROVAL:
        return
    to = TaskStatus.COMPLETED if result == "approve" else TaskStatus.IN_PROGRESS
    log.append(events.new_event(
        events.EventType.TASK_STATE_CHANGED, "user",
        {"from": TaskStatus.PENDING_APPROVAL.value, "to": to.value,
         "trigger": "approval.decided"},
        project_id=pid, task_id=tid,
        idempotency_key=f"approval_outcome:{tid}:{aid_result_key(tid, result)}"))


def aid_result_key(tid: str, result: str) -> str:
    return hashlib.sha256(f"{tid}:{result}".encode()).hexdigest()[:10]


# ---------- openclaw 转移接口 ----------
class TransferIn(BaseModel):
    agent_id: str
    project_id: str
    task_id: str
    payload: Optional[dict] = None
    via: str = "message"


@app.post("/agents/transfer")
def agent_transfer(req: TransferIn, auth: dict = Depends(require_auth)):
    """把任务转移给外接 agent（openclaw 联系适配层）。需 L2 协作权限。

    NG 核心不依赖 openclaw：懒加载 + 优雅报错。openclaw 只是外部 agent 的联系通道，
    缺失时平台其余功能（解析/执行/复核/审计）照常运行。
    """
    require_level("send_handover", auth["level"])
    try:
        from app.adapters import openclaw
    except Exception as e:      # noqa: BLE001
        raise HTTPException(503, f"openclaw 联系适配层不可用: {e}")
    try:
        if req.via == "cli":
            result = openclaw.dispatch_task(
                req.agent_id, {"id": req.task_id, "project_id": req.project_id},
                via="cli")
        else:
            result = openclaw.transfer_agent(
                req.agent_id, req.project_id, req.task_id, req.payload)
    except Exception as e:      # noqa: BLE001
        raise HTTPException(503, f"openclaw 派发失败: {e}")
    log.append(events.new_event(
        events.EventType.AGENT_TRANSFERRED, "ng-platform",
        {"agent_id": req.agent_id, "task_id": req.task_id,
         "transfer_id": result.transfer_id, "status": result.status},
        project_id=req.project_id, task_id=req.task_id))
    return {
        "transfer_id": result.transfer_id,
        "status": result.status,
        "agent": req.agent_id,
        "detail": result.error or "ok",
    }
