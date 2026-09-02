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
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request, Depends, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.domain import events
from app.domain.task import (TaskStatus, ReviewVerdict, InvalidTransition,
                             transition)
from app.security import permission as perm
from app.security.approval_gate import ApprovalGate, PendingApproval
from app.security import auth as authmod
from app.security.auth import require_auth
from app.services.llm import LLMClient, LLMConfigError
from app.services.requirement_parser import RequirementParser
from app.services.team_matcher import match_team
from app.storage.event_log import EventLog, IdempotencyConflict
from app.storage.projection import Projector, OptimisticLockConflict
from app.storage.user_store import UserStore, UsernameConflict, InvalidCredentials

app = FastAPI(title="NG AI Platform", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/auth/me")
def auth_me(auth: dict = Depends(require_auth)):
    """当前身份与权限级别（前端权限感知：隐藏/禁用 L3 动作）。"""
    return {"user": auth["user"], "level": auth["level"],
            "user_id": auth.get("user_id"), "username": auth["user"]}


# ---------- 多用户注册 / 登录（2026-09-01，用户客户端入口） ----------
class RegisterIn(BaseModel):
    username: str
    password: str


@app.post("/auth/register")
def auth_register(body: RegisterIn):
    """注册用户：用户名唯一 + 密码≥6 → 建用户（L1）+ 发会话 token。

    多用户：注册用户是普通用户（level 1），数据按 user_id 隔离。
    管理员在服务器端走静态 NG_LEVEL3_TOKEN，不经此端。
    """
    username = body.username.strip()
    if not username:
        raise HTTPException(400, "用户名不能为空")
    if len(body.password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    try:
        user = user_store.create_user(username, body.password, level=1)
    except UsernameConflict:
        raise HTTPException(409, f"用户名已存在: {username}")
    token = user_store.issue_token(user["id"])
    log.append(events.new_event(
        events.EventType.USER_REGISTERED, f"user:{username}",
        {"user_id": user["id"], "username": username}, user_id=user["id"]))
    return {"token": token, "user_id": user["id"],
            "username": username, "level": user["level"]}


@app.post("/auth/login")
def auth_login(body: RegisterIn):
    """登录：校验用户名/密码 → 发新会话 token。失败 401。"""
    username = body.username.strip()
    user = user_store.get_user_by_username(username)
    if user is None or not user_store.verify_password(body.password,
                                                      user["password_hash"]):
        raise HTTPException(401, "用户名或密码错误")
    token = user_store.issue_token(user["id"])
    log.append(events.new_event(
        events.EventType.USER_LOGGED_IN, f"user:{username}",
        {"user_id": user["id"], "username": username}, user_id=user["id"]))
    return {"token": token, "user_id": user["id"],
            "username": username, "level": user["level"]}


@app.post("/auth/logout")
def auth_logout(authorization: str = Header(default="")):
    """登出：吊销当前会话 token（静态 token 登出无操作，返回 ok）。"""
    token = authorization.removeprefix("Bearer ").strip()
    if token and user_store is not None:
        user_store.revoke_token(token)
    return {"status": "ok"}

# 事件正源：设置 DATABASE_URL 时用 PostgreSQL（含 append-only/幂等约束），否则 JSONL
import os
log = EventLog()
user_store = UserStore()
_dburl = os.environ.get("DATABASE_URL")
if _dburl:
    # DB 正源：失败必须显式报错，绝不静默回退 JSONL（阻塞 1）
    from sqlalchemy import create_engine
    engine = create_engine(_dburl, pool_pre_ping=True)
    with engine.connect() as _c:      # 启动即校验连接
        _c.execute(__import__('sqlalchemy').text("SELECT 1"))
    projector = Projector(engine)      # P1-1 投影物化：DB 模式同步折叠读模型
    log = EventLog(engine=engine, projector=projector)
    user_store = UserStore(engine=engine)
    authmod.set_user_store(user_store)   # 多用户：注册用户会话 token 走 DB
    print(f"[main] 事件正源=PostgreSQL ({_dburl.split('@')[-1]}) + 投影物化", flush=True)
else:
    authmod.set_user_store(user_store)   # dev JSONL 兜底同样注入

# 通用审批门（L3/L4 动作）：ensure_approved 已批准放行，否则建请求 + 抛 PendingApproval
# 传 callable 动态读当前 log（log 会在 DB 模式替换 / 测试 monkeypatch）
gate = ApprovalGate(lambda: log)

def require_level(action: str, actor_level: int | None = None):
    """权限守卫：actor_level 缺省按 L1（骨架简化，接真鉴权后替换）。"""
    level = actor_level if actor_level is not None else 1
    perm.check(level, action)


def _err(ev: events.EventType, **kw) -> dict:
    return events.new_event(ev, actor="api", payload=kw)


def _db_mode() -> bool:
    """DB 投影模式：engine + projector 齐备才走投影表读取（否则 JSONL replay 推导）。"""
    return log._engine is not None and getattr(log, "projector", None) is not None


def _derive_project_list(events_iter, viewer) -> list[dict]:
    """replay 纯函数：项目列表（多用户隔离），形状与 Projector.get_projects 一致。"""
    projects: dict[str, dict] = {}
    for e in events_iter:
        if e["event_type"] == events.EventType.PROJECT_CREATED.value:
            if viewer is not None and e.get("user_id") != viewer:
                continue
            projects[e["project_id"]] = {
                "project_id": e["project_id"],
                "title": e["payload"].get("title", ""),
                "goal": e["payload"].get("goal", ""),
                "status": "active",
            }
        elif e["event_type"] in (events.EventType.PROJECT_PAUSED.value,
                                 events.EventType.PROJECT_ARCHIVED.value) \
                and e.get("project_id") in projects:
            projects[e["project_id"]]["status"] = \
                "paused" if e["event_type"] == events.EventType.PROJECT_PAUSED.value else "archived"
    return [p for p in projects.values() if p["status"] != "archived"]


def _derive_task_list(events_iter, project_id) -> list[dict]:
    """replay 纯函数：项目任务看板，形状与 Projector.get_tasks 一致。"""
    tasks: dict[str, dict] = {}
    for e in events_iter:
        tid = e.get("task_id")
        if not tid:
            continue
        t = tasks.setdefault(tid, {"task_id": tid, "title": "",
                                   "status": TaskStatus.TODO.value,
                                   "owner": None, "reviewer": None,
                                   "has_deliverable": False,
                                   "depends_on": []})
        p = e["payload"]
        if e["event_type"] == events.EventType.TASK_CREATED.value:
            t["title"] = p.get("title", "")
            t["depends_on"] = list(p.get("depends_on") or [])
        elif e["event_type"] == events.EventType.AGENT_ASSIGNED.value:
            t[p.get("role", "owner")] = p.get("agent")
        elif e["event_type"] == events.EventType.TASK_STATE_CHANGED.value:
            t["status"] = p["to"]
        elif e["event_type"] == events.EventType.DELIVERABLE_SUBMITTED.value:
            t["has_deliverable"] = True
    return list(tasks.values())


def _derive_task_project(events_iter) -> str | None:
    """replay 纯函数：从事件序列解析任务所属项目（F3）。"""
    for e in events_iter:
        if e["event_type"] == events.EventType.TASK_CREATED.value:
            return e.get("project_id")
    return None


def _task_project_id(tid: str) -> str | None:
    """任务所属项目：DB 投影模式查 tasks 行（缺失回退 replay），JSONL 走 replay。"""
    if _db_mode():
        pid = log.projector.get_project_id(tid)
        if pid is not None:
            return pid
    return _derive_task_project(log.replay(task_id=tid))


def _derive_task_context(events_iter, task_id) -> dict:
    """replay 纯函数：任务上下文，形状与 Projector.get_task_context 一致。"""
    ctx: dict = {"task_id": task_id, "title": "", "description": "",
                 "deliverables": [], "owner": None, "reviewer": None,
                 "status": TaskStatus.TODO.value, "deadline_ts": None,
                 "depends_on": []}
    for e in events_iter:
        p = e["payload"]
        if e["event_type"] == events.EventType.TASK_CREATED.value:
            ctx.update(title=p.get("title", ""), description=p.get("description", ""),
                       deliverables=list(p.get("deliverables") or []),
                       deadline_ts=p.get("deadline_ts"),
                       depends_on=list(p.get("depends_on") or []))
        elif e["event_type"] == events.EventType.AGENT_ASSIGNED.value:
            role = p.get("role", "owner")
            ctx[role] = p.get("agent") or ctx.get(role)
        elif e["event_type"] == events.EventType.TASK_STATE_CHANGED.value:
            ctx["status"] = p["to"]
    return ctx


# ---------- 通知（P1-2 事件派生，按用户项目隔离） ----------
_NOTIFY_EVENTS = {
    events.EventType.TASK_STATE_CHANGED.value,
    events.EventType.REVIEW_DECIDED.value,
    events.EventType.APPROVAL_DECIDED.value,
}


def _notify_summary(event: dict) -> str:
    """事件 → 中文摘要（复用前端 EVENT_LABEL 语义）。"""
    p = event["payload"]
    et = event["event_type"]
    if et == events.EventType.TASK_STATE_CHANGED.value:
        return f"任务状态 → {p.get('to', '?')}"
    if et == events.EventType.REVIEW_DECIDED.value:
        return f"复核结论: {p.get('verdict', '?')}"
    if et == events.EventType.APPROVAL_DECIDED.value:
        return f"审批: {'已批准' if p.get('result') == 'approve' else '已拒绝'}"
    return ""


def _derive_notifications(events_iter, viewer_user_id) -> list[dict]:
    """从事件派生当前用户的通知（按项目 owner 隔离）。

    项目归属 = project.created 事件的 user_id；只收 viewer 拥有的项目里
    的状态变化/复核/审批事件。返回按 ts 倒序、最近 20 条。
    """
    owner_of: dict[str, str] = {}     # project_id → owner user_id
    for e in events_iter:
        if e["event_type"] == events.EventType.PROJECT_CREATED.value \
                and e.get("user_id"):
            owner_of[e["project_id"]] = e["user_id"]
    out = []
    for e in events_iter:
        if e["event_type"] not in _NOTIFY_EVENTS:
            continue
        pid = e.get("project_id")
        if viewer_user_id is not None and owner_of.get(pid) != viewer_user_id:
            continue
        out.append({
            "event_type": e["event_type"],
            "project_id": pid,
            "task_id": e.get("task_id"),
            "summary": _notify_summary(e),
            "ts": e.get("created_at_ts", 0),
        })
    out.sort(key=lambda n: n["ts"], reverse=True)
    return out[:20]


@app.get("/notifications")
def get_notifications(auth: dict = Depends(require_auth)):
    """当前用户的通知（事件派生，按项目 owner 隔离）。"""
    require_level("read_project", auth["level"])
    return {"notifications": _derive_notifications(log.replay(), auth.get("user_id"))}


# ---------- 项目 ----------
@app.post("/projects")
def create_project(title: str, goal: str, auth: dict = Depends(require_auth)):
    pid = str(uuid.uuid4())
    log.append(events.new_event(
        events.EventType.PROJECT_CREATED, "user",
        {"title": title, "goal": goal}, project_id=pid,
        user_id=auth.get("user_id")))
    return {"project_id": pid, "status": "active"}


@app.get("/projects")
def list_projects(auth: dict = Depends(require_auth)):
    """项目列表（事件溯源推导），已归档的排除。供看板。

    多用户隔离：注册用户（有 user_id）只见自己的项目；
    服务器端静态 token（user_id=None，管理员/agent 通道）见全部。
    """
    require_level("read_project", auth["level"])
    viewer = auth.get("user_id")
    if _db_mode():
        projects = log.projector.get_projects(viewer)
    else:
        projects = _derive_project_list(log.replay(), viewer)
    return {"projects": projects}


@app.post("/projects/{pid}/archive")
def archive_project(pid: str, auth: dict = Depends(require_auth)):
    """终止/删除项目：使用者（owner，L1）随时有权。从看板移除，事件留审计（append-only）。"""
    require_level("read_project", auth["level"])   # L1：owner 随时可终止/删除
    if _db_mode():
        if not log.projector.project_exists(pid):
            raise HTTPException(404, "project not found")
    else:
        evs = log.replay(project_id=pid)
        if not any(e["event_type"] == events.EventType.PROJECT_CREATED.value for e in evs):
            raise HTTPException(404, "project not found")
    log.append(events.new_event(events.EventType.PROJECT_ARCHIVED, f"user:{auth['user']}",
                                {"reason": "owner_terminated"}, project_id=pid,
                                idempotency_key=f"archive:{pid}"))
    return {"project_id": pid, "status": "archived"}


@app.get("/projects/{pid}/tasks")
def list_tasks(pid: str, auth: dict = Depends(require_auth)):
    """项目任务看板数据（从事件推导：title/status/owner/reviewer/has_deliverable）。"""
    require_level("read_project", auth["level"])
    if _db_mode():
        tasks = log.projector.get_tasks(pid)
        if not log.projector.project_exists(pid):
            raise HTTPException(404, "project not found")
    else:
        evs = log.replay(project_id=pid)
        if not evs:
            raise HTTPException(404, "project not found")
        tasks = _derive_task_list(evs, pid)
    return {"tasks": tasks}


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
    """审计回放。项目不存在 → 404（与 list_tasks 一致，复核 2026-09-02 顺手项）。"""
    require_level("read_project", auth["level"])
    if _db_mode():
        if not log.projector.project_exists(pid):
            raise HTTPException(404, "project not found")
    else:
        evs = log.replay(project_id=pid)
        if not any(e["event_type"] == events.EventType.PROJECT_CREATED.value
                   for e in evs):
            raise HTTPException(404, "project not found")
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
    _llm = LLMClient()
    parsed = RequirementParser(_llm).parse_goal(goal, builtin_names)
    _record_usage(pid, None, _llm.usage(), "requirement_parse")
    log.append(events.new_event(
        events.EventType.GOAL_PARSED, "system",
        {"summary": parsed.summary, "tasks": [t.title for t in parsed.tasks]},
        project_id=pid,
        idempotency_key=f"goalparse:{pid}:" + hashlib.sha256(
            goal.encode()).hexdigest()[:10]))
    # 先为全部任务生成 task_id + title→tid 映射（依赖引用标题，需先有 tid）
    drafts = list(parsed.tasks)
    tid_of_title = {draft.title: str(uuid.uuid4()) for draft in drafts}
    created = []
    for draft in drafts:
        tid = tid_of_title[draft.title]
        # 依赖解析：LLM 输出的 depends_on 是标题 → 映射成本任务 tid；找不到的忽略（容错）
        depends = [tid_of_title[d] for d in draft.depends_on
                   if d in tid_of_title and tid_of_title[d] != tid]
        log.append(events.new_event(
            events.EventType.TASK_CREATED, "system",
            {"title": draft.title, "description": draft.description,
             "deliverables": draft.deliverables, "status": TaskStatus.TODO.value,
             "depends_on": depends},
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
                        "owner": team["owner"], "reviewer": team["reviewer"],
                        "depends_on": depends})
    return created


@app.post("/projects/{pid}/messages")
def post_message(pid: str, body: str, parse: bool = False,
                 auth: dict = Depends(require_auth)):
    """写入用户消息。parse=true → 视为项目目标，走需求解析 + 团队匹配 + 自动建任务。"""
    mid = str(uuid.uuid4())
    log.append(events.new_event(
        events.EventType.MESSAGE_AGGREGATED, "user",
        {"message_id": mid, "body": body}, project_id=pid,
        user_id=auth.get("user_id")))
    created = []
    if parse:
        try:
            created = _parse_and_create(pid, body)
        except (LLMConfigError, ValueError, RuntimeError) as ex:
            # 龙虾汇总#2（2026-09-03）：解析失败写 parse_failed 事件（审计可查，不静默）
            log.append(events.new_event(
                events.EventType.GOAL_PARSE_FAILED, "system",
                {"error": str(ex)[:300], "body_len": len(body)},
                project_id=pid, idempotency_key=f"parsefail:{pid}:" + hashlib.sha256(
                    body.encode()).hexdigest()[:10]))
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
    """当前 agent 注册表（latest-wins，DB 投影 / JSONL replay）。"""
    require_level("read_project", auth["level"])
    if _db_mode():
        return {"agents": log.projector.get_agents()}
    return {"agents": _agents_registry()}


@app.post("/agents/{name}/deactivate")
def deactivate_agent(name: str, auth: dict = Depends(require_auth)):
    """从平台移除 agent（latest-wins 注册 status=disabled，事件留审计）。"""
    require_level("write_message", auth["level"])
    registry = {a["name"]: a for a in _agents_registry()}
    if name not in registry:
        raise HTTPException(404, f"agent {name} 未注册")
    log.append(events.new_event(
        events.EventType.AGENT_REGISTERED, f"user:{auth['user']}",
        {"name": name, "capability": registry[name].get("capability", ""),
         "role": registry[name].get("role", ""), "status": "disabled",
         "permission": registry[name].get("permission", "L1"),
         "executor": registry[name].get("executor", "builtin")},
        idempotency_key=f"agentreg:{name}:disabled"))
    return {"name": name, "status": "disabled"}


# ---------- LLM 算力配置（前端下拉选 provider + 输 API key） ----------
class LLMConfigIn(BaseModel):
    provider: str
    api_key: str = ""
    model: str = ""
    base_url: str = ""


# 算力提供商目录（一线/二线/本地），前端下拉数据源 + 配置映射
LLM_PROVIDERS: dict[str, dict] = {
    # 一线
    "openai":      {"name": "OpenAI",          "tier": "一线", "type": "openai",
                    "default_model": "gpt-4o-mini", "base_url": ""},
    "anthropic":   {"name": "Anthropic Claude", "tier": "一线", "type": "anthropic",
                    "default_model": "claude-opus-4-8", "base_url": ""},
    "google":      {"name": "Google Gemini",    "tier": "一线", "type": "openai_compatible",
                    "default_model": "gemini-2.0-flash",
                    "base_url": "https://generativelanguage.googleapis.com/v1beta/openai"},
    "deepseek":    {"name": "DeepSeek",         "tier": "一线", "type": "openai_compatible",
                    "default_model": "deepseek-chat",
                    "base_url": "https://api.deepseek.com/v1"},
    "xai":         {"name": "xAI Grok",         "tier": "一线", "type": "openai_compatible",
                    "default_model": "grok-2", "base_url": "https://api.x.ai/v1"},
    # 二线
    "qwen":        {"name": "阿里通义 Qwen",     "tier": "二线", "type": "openai_compatible",
                    "default_model": "qwen-max",
                    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    "glm":         {"name": "智谱 GLM",          "tier": "二线", "type": "openai_compatible",
                    "default_model": "glm-4-flash",
                    "base_url": "https://open.bigmodel.cn/api/paas/v4"},
    "kimi":        {"name": "Moonshot Kimi",    "tier": "二线", "type": "openai_compatible",
                    "default_model": "moonshot-v1-8k",
                    "base_url": "https://api.moonshot.cn/v1"},
    "minimax":     {"name": "MiniMax",          "tier": "二线", "type": "openai_compatible",
                    "default_model": "MiniMax-Text-01",
                    "base_url": "https://api.minimax.chat/v1"},
    "mistral":     {"name": "Mistral",          "tier": "二线", "type": "openai_compatible",
                    "default_model": "mistral-small",
                    "base_url": "https://api.mistral.ai/v1"},
    "siliconflow": {"name": "硅基流动 SiliconFlow", "tier": "二线", "type": "openai_compatible",
                    "default_model": "Qwen/Qwen2.5-7B-Instruct",
                    "base_url": "https://api.siliconflow.cn/v1"},
    "wenxin":      {"name": "百度文心",          "tier": "二线", "type": "openai_compatible",
                    "default_model": "ernie-4.0-8k",
                    "base_url": "https://qianfan.baidubce.com/v2"},
    # 本地
    "ollama":      {"name": "Ollama 本地",       "tier": "本地", "type": "openai_compatible",
                    "default_model": "qwen2.5:7b",
                    "base_url": "http://localhost:11434/v1"},
}


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


@app.get("/agents/providers")
def list_providers(auth: dict = Depends(require_auth)):
    """算力提供商目录（一线/二线/本地），前端下拉数据源。"""
    require_level("read_project", auth["level"])
    return {"providers": [
        {"id": k, "name": v["name"], "tier": v["tier"],
         "default_model": v["default_model"], "base_url": v["base_url"]}
        for k, v in LLM_PROVIDERS.items()]}


@app.post("/agents/llm-config")
def set_llm_config(body: LLMConfigIn, auth: dict = Depends(require_auth)):
    """选择算力提供商 + 输入 API key → 写 gitignore 的 .env（下次 LLM 调用生效）。"""
    require_level("write_message", auth["level"])
    p = body.provider.strip().lower()
    prov = LLM_PROVIDERS.get(p)
    if not prov:
        raise HTTPException(400, f"未知 provider: {p}")
    updates = {"LLM_PROVIDER": prov["type"]}
    if prov["type"] == "openai":
        updates["OPENAI_API_KEY"] = body.api_key.strip()
    elif prov["type"] == "anthropic":
        updates["ANTHROPIC_API_KEY"] = body.api_key.strip()
    else:  # openai_compatible
        updates["LLM_BASE_URL"] = (body.base_url.strip() or prov["base_url"])
        updates["LLM_API_KEY"] = body.api_key.strip()
    updates["LLM_MODEL"] = body.model.strip() or prov["default_model"]
    _write_env_updates(updates)
    return {"provider": p, "status": "saved",
            "note": "下次 LLM 调用生效（docker 模式需重建容器加载 secrets）"}


# ---------- 试用者反馈 ----------
class FeedbackIn(BaseModel):
    content: str
    contact: str = ""       # 联系方式（可选）
    rating: int | None = None  # 1-5（可选）


@app.post("/feedback")
def submit_feedback(body: FeedbackIn, auth: dict = Depends(require_auth)):
    """试用者提意见：写 feedback.submitted 事件（审计），owner 可查。"""
    if not body.content.strip():
        raise HTTPException(400, "反馈内容不能为空")
    log.append(events.new_event(
        events.EventType.FEEDBACK_SUBMITTED, f"user:{auth['user']}",
        {"content": body.content.strip(), "contact": body.contact.strip(),
         "rating": body.rating},
        idempotency_key=f"feedback:" + hashlib.sha256(
            body.content.strip().encode()).hexdigest()[:12]))
    return {"status": "submitted"}


@app.get("/feedback")
def list_feedback(auth: dict = Depends(require_auth)):
    """反馈列表（owner 查看试用者意见）。"""
    require_level("read_audit", auth["level"])
    if _db_mode():
        return {"feedback": log.projector.get_feedback()}
    items = []
    for e in log.replay():
        if e["event_type"] == events.EventType.FEEDBACK_SUBMITTED.value:
            p = e["payload"]
            items.append({"actor": e.get("actor"), "content": p.get("content"),
                          "contact": p.get("contact", ""), "rating": p.get("rating"),
                          "ts": e.get("created_at_ts")})
    return {"feedback": list(reversed(items))}


# ---------- LLM 用量（1M 上下文限制展示） ----------
def _record_usage(project_id: str | None, task_id: str | None,
                  usage_list: list[dict], label: str):
    """把 LLMClient 的每次调用用量持久化为 usage.recorded 事件（审计）。"""
    for u in usage_list:
        log.append(events.new_event(
            events.EventType.USAGE_RECORDED, "llm",
            {"provider": u.get("provider"), "model": u.get("model"),
             "input_tokens": u.get("input_tokens", 0),
             "output_tokens": u.get("output_tokens", 0), "label": label},
            project_id=project_id, task_id=task_id,
            idempotency_key=f"usage:{label}:{int(u.get('ts', 0))}"))


@app.get("/usage")
def get_usage(auth: dict = Depends(require_auth)):
    """LLM 用量聚合（1M 上下文限制：展示已用 vs 上限，应对=接近时预警）。"""
    require_level("read_audit", auth["level"])
    if _db_mode():
        return log.projector.get_usage()
    rows = [e for e in log.replay()
            if e["event_type"] == events.EventType.USAGE_RECORDED.value]
    tin = sum(e["payload"].get("input_tokens", 0) for e in rows)
    tout = sum(e["payload"].get("output_tokens", 0) for e in rows)
    by_label: dict[str, dict] = {}
    for e in rows:
        p = e["payload"]
        b = by_label.setdefault(p.get("label", "?"),
                                {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        b["calls"] += 1
        b["input_tokens"] += p.get("input_tokens", 0)
        b["output_tokens"] += p.get("output_tokens", 0)
    return {"calls": len(rows), "input_tokens": tin, "output_tokens": tout,
            "context_limit": 1_000_000,   # 1M token 上下文窗口
            "by_label": by_label}


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
                depends_on: Optional[str] = None,   # 依赖的任务id/title（逗号分隔，可选）
                auth: dict = Depends(require_auth)):
    tid = str(uuid.uuid4())
    deps = []
    if depends_on:
        # 同项目内按 task_id 或 title 解析依赖
        for dep in depends_on.split(","):
            dep = dep.strip()
            if not dep:
                continue
            if dep in ("", tid):
                continue
            deps.append(dep)   # 任务id直接存；title 由调用方确保同批已建
    log.append(events.new_event(
        events.EventType.TASK_CREATED, "system",
        {"title": title, "description": description,
         "status": TaskStatus.TODO.value, "deadline_ts": deadline_ts,
         "depends_on": deps},
        project_id=pid, task_id=tid))
    if owner_agent:
        log.append(events.new_event(
            events.EventType.AGENT_ASSIGNED, "system",
            {"agent": owner_agent}, project_id=pid, task_id=tid))
    return {"task_id": tid, "status": TaskStatus.TODO.value}


@app.get("/tasks/{tid}/context")
def task_context(tid: str, auth: dict = Depends(require_auth)):
    """任务上下文（供 agent 执行前读取）：title/desc/deliverables/owner/reviewer/status/deadline。

    DB 投影模式走 tasks 投影行（含 expected_version），行缺失回退 replay（孤儿任务）。
    """
    require_level("read_project", auth["level"])
    if _db_mode():
        ctx = log.projector.get_task_context(tid)
        if ctx is None:
            evs = log.replay(task_id=tid)
            if not evs:
                raise HTTPException(404, "task not found")
            return _derive_task_context(evs, tid)
        row = log.projector.get_task_row(tid)
        ctx["expected_version"] = row["expected_version"] if row else None
        return ctx
    evs = log.replay(task_id=tid)
    if not evs:
        raise HTTPException(404, "task not found")
    ctx = _derive_task_context(evs, tid)
    ctx["expected_version"] = None    # JSONL 无锁，前端按可选字段处理
    return ctx


@app.patch("/tasks/{tid}/state")
def change_state(tid: str, to: TaskStatus, actor: str = "system",
                 idempotency_key: Optional[str] = None,
                 expected_version: Optional[int] = None,
                 auth: dict = Depends(require_auth)):
    """状态转移（白名单校验 + 乐观锁）。

    DB 投影模式：tasks.expected_version 乐观锁——客户端带预期版本，不匹配 409；
    无 expected_version（worker/兼容路径）放行但正常自增。JSONL 模式无锁（replay 推导）。
    幂等语义不变：同状态重试 200；同幂等键不同意图 409。
    """
    require_level("change_task_state", auth["level"])
    if _db_mode():
        row = log.projector.get_task_row(tid)
        if row is None:
            evs = log.replay(task_id=tid)
            if not evs:
                raise HTTPException(404, "task not found")
            state = _derive_task_context(evs, tid)["status"]
            state = TaskStatus(state)
            project_id = _derive_task_project(evs)
            cur_v = None
        else:
            state = TaskStatus(row["status"])
            project_id = row["project_id"]
            cur_v = row["expected_version"]
        # 幂等优先：同状态重试 → 200（不判非法转移）
        if state == to:
            return {"task_id": tid, "status": to.value, "idempotent": True}
        # 状态机校验：非法转移 400（先 400 后 409，非法不报成并发冲突）
        try:
            new = transition(state, to)
        except InvalidTransition as ex:
            raise HTTPException(400, str(ex))
        # 乐观锁预检：带过期版本直接 409（挡掉大多数已过期请求，不落任何事件）
        if expected_version is not None and cur_v is not None \
                and expected_version != cur_v:
            raise HTTPException(
                409, f"任务状态已被修改（版本 {cur_v} ≠ 预期 {expected_version}），请刷新后重试")
        log.append(events.new_event(
            events.EventType.TASK_STATE_CHANGED, actor,
            {"from": state.value, "to": new.value},
            project_id=project_id, task_id=tid,
            idempotency_key=idempotency_key),
            expected_version=cur_v)
        # 龙虾反馈（2026-09-02）：手动推进到 pending_approval 时自动建审批请求
        # （否则 pending_approval 状态无对应 approval.requested → 审批队列空、卡死）
        if new == TaskStatus.PENDING_APPROVAL:
            _ensure_approval_requested(tid, project_id)
        return {"task_id": tid, "status": new.value,
                "expected_version": (cur_v or 0) + 1}
    # JSONL 路径：replay 推导，无锁
    evs = log.replay(task_id=tid)
    state = TaskStatus.TODO
    project_id = None
    for e in evs:
        if e["event_type"] == events.EventType.TASK_CREATED.value:
            project_id = e.get("project_id")      # F3: 从 TASK_CREATED 解析项目
        if e["event_type"] == events.EventType.TASK_STATE_CHANGED.value:
            state = TaskStatus(e["payload"]["to"])
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
    # 龙虾反馈（2026-09-02）：手动推进到 pending_approval 自动建审批请求
    if new == TaskStatus.PENDING_APPROVAL:
        _ensure_approval_requested(tid, project_id)
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

@app.exception_handler(OptimisticLockConflict)
async def optimistic_lock_conflict_handler(request: Request, exc: OptimisticLockConflict):
    """乐观锁：并发状态修改（事件事务已回滚，无半写状态）→ 409。"""
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
    project_id = _task_project_id(tid)
    if project_id is None:
        raise HTTPException(404, "task not found")
    if verdict not in ("done", "blocked", "partial"):
        raise HTTPException(400, f"verdict 非法: {verdict}")
    state = TaskStatus.TODO
    if _db_mode():
        row = log.projector.get_task_row(tid)
        if row is not None:
            state = TaskStatus(row["status"])
    # reviewer / 已有复核判断仍需事件序列（review.requested 不投影）
    evs = log.replay(task_id=tid)
    if state == TaskStatus.TODO:
        for e in evs:
            if e["event_type"] == events.EventType.TASK_STATE_CHANGED.value:
                state = TaskStatus(e["payload"]["to"])
    idem = f"deliverable:{tid}:{file_ref}:" + hashlib.sha256(
        f"{verdict}|{summary}".encode()).hexdigest()[:10]
    # 产出证据（龙虾反馈阻塞#4）：验证文件 + 存内容长度/哈希/预览
    evidence = _deliverable_evidence(file_ref)
    log.append(events.new_event(
        events.EventType.DELIVERABLE_SUBMITTED, f"agent:{agent}",
        {"file_ref": file_ref, "version": 1, "verdict": verdict, "summary": summary,
         **evidence},
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
        # 汇总v1.1 ②③（2026-09-03）：打回重修后重新提交应能二次复核——
        # 仅当存在【未决】review 才跳过；旧 review 已决(needs_changes/reject 打回过)则建新 review
        pending_review = False
        requested_ids = {e["payload"].get("review_id") for e in evs
                         if e["event_type"] == events.EventType.REVIEW_REQUESTED.value}
        decided_ids = {e["payload"].get("review_id") for e in evs
                       if e["event_type"] == events.EventType.REVIEW_DECIDED.value}
        if requested_ids - decided_ids:
            pending_review = True
        if not pending_review:
            rid = str(uuid.uuid4())
            log.append(events.new_event(
                events.EventType.REVIEW_REQUESTED, f"agent:{agent}",
                {"review_id": rid, "trigger": "deliverable.submitted",
                 "reviewer": reviewer},
                project_id=project_id, task_id=tid,
                idempotency_key=f"review:req:{tid}:{rid[:8]}"))
    return {"task_id": tid, "deliverable": file_ref,
            "status": new_state.value if new_state else state.value}


@app.post("/tasks/{tid}/heartbeat")
def task_heartbeat(tid: str, agent: str = "agent", auth: dict = Depends(require_auth)):
    """Agent 心跳：更新任务活跃状态（F1：让 HeartbeatWorker 有据可依）。"""
    require_level("change_task_state", auth["level"])
    project_id = _task_project_id(tid)
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
    pid = _task_project_id(tid)
    if pid is None:
        raise HTTPException(404, "task not found")   # 对象绑定
    # 幂等：同任务有【未决】复核请求 → 返回同 review_id（重试不新建）
    # 汇总v1.1 ②④（2026-09-03）：旧 review 已决(needs_changes/reject 打回过) → 建新 review，可二次复核/改判
    evs = log.replay(task_id=tid)
    req_ids = {e["payload"].get("review_id") for e in evs
               if e["event_type"] == events.EventType.REVIEW_REQUESTED.value}
    dec_ids = {e["payload"].get("review_id") for e in evs
               if e["event_type"] == events.EventType.REVIEW_DECIDED.value}
    if req_ids - dec_ids:
        pending = next((e["payload"]["review_id"] for e in evs
                        if e["event_type"] == events.EventType.REVIEW_REQUESTED.value
                        and e["payload"].get("review_id") in (req_ids - dec_ids)), None)
        if pending:
            return {"review_id": pending, "task_id": tid}
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
    # 试用汇总#4（2026-09-03）：reviewer 身份绑定——执行者须是任务指派的 reviewer
    # 或 L3 管理员（L3 可代审，demo/静态 L3 token 放行）；普通注册用户非 reviewer 不可审。
    tid = req.get("task_id")
    if tid and int(auth.get("level") or 0) < 3:
        assigned_reviewer = None
        for e in evs:
            if e["event_type"] == events.EventType.AGENT_ASSIGNED.value \
                    and e.get("task_id") == tid \
                    and e["payload"].get("role", "owner") == "reviewer":
                assigned_reviewer = e["payload"].get("agent")
        if assigned_reviewer and auth["user"] != assigned_reviewer:
            raise HTTPException(403,
                f"复核权限不足：任务指派复核人为 {assigned_reviewer}，你不是该任务的 reviewer（或需 L3）")
    log.append(events.new_event(
        events.EventType.REVIEW_DECIDED, "reviewer",
        {"review_id": rid, "verdict": verdict.value},
        project_id=req.get("project_id"), task_id=req.get("task_id"),
        idempotency_key=f"review:dec:{rid}"))
    # 试用汇总#1#3（2026-09-03）：复核决策联动任务状态，返工不卡死。
    #   pass          → 自动 completed（交付完成）
    #   needs_changes → 退回 in_progress（返工可修改重交）
    #   reject        → 退回 in_progress（打回重做）
    if req.get("task_id"):
        _apply_review_outcome(req["task_id"], req.get("project_id"), verdict)
    return {"review_id": rid, "verdict": verdict.value}


def _apply_review_outcome(tid: str, pid: str | None, verdict: ReviewVerdict):
    """复核结论 → 任务状态联动（仅当 in_review，幂等）。"""
    state = TaskStatus.TODO
    for e in log.replay(task_id=tid):
        if e["event_type"] == events.EventType.TASK_STATE_CHANGED.value:
            state = TaskStatus(e["payload"]["to"])
    if state != TaskStatus.IN_REVIEW:
        return
    if verdict == ReviewVerdict.PASS:
        to = TaskStatus.COMPLETED
        trigger = "review.decided:pass"
    else:  # needs_changes / reject → 退回进行中返工
        to = TaskStatus.IN_PROGRESS
        trigger = "review.decided:rework"
    log.append(events.new_event(
        events.EventType.TASK_STATE_CHANGED, "system",
        {"from": TaskStatus.IN_REVIEW.value, "to": to.value,
         "trigger": trigger, "verdict": verdict.value},
        project_id=pid, task_id=tid,
        idempotency_key=f"review_outcome:{tid}:{verdict.value}"))


@app.post("/tasks/{tid}/approvals")
def request_approval(tid: str, scope: str = "flow_change", auth: dict = Depends(require_auth)):
    """创建审批请求（绑定任务，需 L3）。"""
    require_level("approve_action", auth["level"])
    pid = _task_project_id(tid)
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


def _ensure_approval_requested(tid: str, pid: str | None):
    """如任务尚无审批请求则自动建一个（scope=flow_change）。龙虾反馈 2026-09-02：
    手动推进到 pending_approval 后若无 approval.requested，审批队列空、任务卡死。"""
    existing = any(e["event_type"] == events.EventType.APPROVAL_REQUESTED.value
                   for e in log.replay(task_id=tid))
    if existing:
        return
    aid = str(uuid.uuid4())
    log.append(events.new_event(
        events.EventType.APPROVAL_REQUESTED, "system",
        {"approval_id": aid, "scope": "flow_change"},
        project_id=pid, task_id=tid,
        idempotency_key=f"approval:req:{tid}:auto"))


def _deliverable_evidence(file_ref: str) -> dict:
    """产出证据（龙虾反馈阻塞#4，2026-09-02）：验证 file_ref 指向的文件是否存在，
    存在则存内容长度/哈希/预览；不存在则标记 file_missing（不卡死流程但能看出没真文件）。"""
    from pathlib import Path
    p = Path(file_ref)
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.exists():
        return {"file_missing": True, "note": f"file_ref={file_ref} 未找到对应文件"}
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"file_missing": True, "note": f"读取失败: {e}"}
    return {
        "content_len": len(content),
        "content_hash": hashlib.sha256(content.encode()).hexdigest()[:16],
        "preview": content[:500],
        "file_missing": False,
    }


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


# ---------- 远程访问：/api 前缀剥离 + 前端静态托管（生产） ----------
@app.middleware("http")
async def strip_api_prefix(request: Request, call_next):
    """前端用 /api/* 调后端；剥离前缀路由到真实端点（dev vite 代理同语义）。"""
    p = request.scope["path"]
    if p == "/api":
        request.scope["path"] = "/"
    elif p.startswith("/api/"):
        request.scope["path"] = p[len("/api"):]
    return await call_next(request)


from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _DIST.exists() and (_DIST / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    async def spa():
        return FileResponse(_DIST / "index.html")
