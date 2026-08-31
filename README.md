# NG AI Platform

面向长期项目的 **AI Agent 团队操作系统**：用户给目标，平台自动组队、推进、记忆、复核、审计，用户只管关键决策。

> NG = New Generation / Next Generation

## 架构

```
用户/渠道 → 消息接入与会话聚合 → 项目上下文与权限 → 需求解析与 Agent 匹配
        → 任务编排器与状态机 → Agent 执行适配层 → 工具/文件/外部服务
```

**核心设计**：
- **事件溯源**：`events` 是审计正源（append-only，禁止 UPDATE/DELETE），状态字段是可重建投影
- **任务状态机**：8 状态冻结枚举 + 转移白名单（`todo → in_progress → in_review → pending_approval → completed → archived`）
- **主动推进**：5 个常驻 Worker（AutoStart/Heartbeat/Deadline/Blocker/Report），带租约 + 幂等 + 恢复
- **权限 L0-L4**：动作分级授权，L3/L4 强制人工审批
- **openclaw 适配器**：把任务转移给 openclaw agent（如龙虾），写共享消息

## 目录结构

```
ng-platform/
├── app/
│   ├── main.py            # FastAPI 入口 + 端点
│   ├── config.py          # 配置（环境变量）
│   ├── domain/            # 领域模型：事件类型 / 任务状态机
│   ├── adapters/openclaw.py  # openclaw 转移接口
│   ├── storage/event_log.py  # 事件日志（JSONL 落盘 / PostgreSQL）
│   ├── security/permission.py # L0-L4 权限
│   └── workers/           # 5 个调度 Worker
├── migrations/            # DB 迁移（append-only 强制等）
├── tests/                 # pytest 冒烟测试
├── schema.sql             # PostgreSQL 数据模型
├── docker-compose.yml
└── Dockerfile
```

## 快速开始

### 本地运行（JSONL 落盘，无 DB 依赖）

```bash
# Python 3.12+（代码使用 PEP 604 语法）
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt          # 网络受限可用清华源

# 启动 API
.venv/bin/uvicorn app.main:app --reload            # http://127.0.0.1:8000

# 启动调度 Worker（另开终端）
.venv/bin/python -m app.workers
```

### Docker + PostgreSQL（生产模式）

```bash
POSTGRES_PASSWORD=<强> NG_APP_PASSWORD=<强> \
NG_LEVEL1_TOKEN=<强随机> NG_LEVEL3_TOKEN=<强随机> \
docker compose up --build        # 自动建库 + 迁移 + API/Worker
```

- 服务显式 `NG_ENV=production`：token/密码缺值或默认值 → 拒绝启动（严格模式）
- 端口收敛：db 不向宿主发布（仅内部网络）；api 仅绑定 `127.0.0.1:8000`
- 全链路实测：`POSTGRES_PASSWORD=<强> NG_APP_PASSWORD=<强> bash scripts/docker_e2e.sh`
- CI 复跑：`.github/workflows/e2e.yml`（待 git 仓库初始化后启用）

### 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

## API 端点

| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| POST | `/projects` | 创建项目 | - |
| GET | `/projects/{id}/context` | 项目上下文 | L0 |
| POST | `/projects/{id}/messages` | 写入用户消息 | L1 |
| POST | `/projects/{id}/tasks` | 创建任务（可带 deadline）| - |
| PATCH | `/tasks/{id}/state` | 状态流转（白名单校验）| L1 |
| POST | `/tasks/{id}/deliverables` | 提交产出 | L1 |
| POST | `/tasks/{id}/heartbeat` | Agent 心跳 | L1 |
| POST | `/reviews/{id}/decision` | 复核结论 | - |
| POST | `/approvals/{id}/decision` | 审批 | L3 |
| GET | `/projects/{id}/audit` | 审计回放 | L0 |
| POST | `/projects/{id}/pause` | 暂停项目 | L3（需审批）|
| POST | `/agents/transfer` | 转移任务给 openclaw agent | L2 |

## openclaw 转移

`POST /agents/transfer` 把任务转给 openclaw agent：

```json
{
  "agent_id": "lobster",
  "project_id": "...",
  "task_id": "...",
  "payload": {"note": "转给龙虾"},
  "via": "message"     // message=写共享消息，cli=同步调用
}
```

`message` 模式写入 `~/.openclaw/shared/messages/ng-platform-<agent>-transfer-<uuid>.md`。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | 空（本地走 JSONL） | 接 PG 必填；不再烘焙默认凭据 |
| `NG_APP_PASSWORD` | `ng`（仅 dev） | `run_migrations.py` 设置 ng_app 角色密码（ALTER ROLE 同步）；生产必填 |
| `NG_ENV` | `development` | `production` 时强制要求强 token / 密码，缺失或不安全默认值则**拒绝启动** |
| `NG_LEVEL1_TOKEN` / `NG_LEVEL3_TOKEN` | dev 兜底 `l1-agent-token`/`l3-test-token` | 生产必填强随机 token（`NG_ENV=production` 或 `NG_STRICT_TOKENS=1` 时缺/默认值拒启） |
| `OPENCLAW_BIN` | `openclaw` | openclaw 可执行 |
| `OPENCLAW_SHARED_DIR` | `~/.openclaw/shared/messages` | 共享消息目录 |
| `HEARTBEAT_TIMEOUT_S` / `BLOCKER_TIMEOUT_S` | `300` / `600` | Worker 超时 |

## 边界与下一步

**已清零（三方会签阻塞项，2026-08-31）**：
- **P0 密钥契约**：`auth.py` 生产拒默认 token；`config.py` 不再烘焙凭据；`001_events_append_only.sql` 去密码字面量；`run_migrations.py` 用 `NG_APP_PASSWORD` + `ALTER ROLE` 统一同步（角色已存在也改密码）
- **P1 幂等反例**：事件层内容寻址幂等——同 key 同意图幂等返回，同 key 不同意图抛 `IdempotencyConflict` → 409（不再静默丢写假 200）
- **Docker E2E**：`scripts/docker_e2e.sh` 本机实测通过（P0 密码契约 + P1 反例 + 全栈冒烟）
- **闭环（实施路径第 3 步，2026-08-31）**：`submit_deliverable` 加 `verdict/summary`——Agent 提交产出(verdict=done) 自动推进 `in_progress→in_review`（产出自动交接复核，状态机第七节），verdict=blocked → blocked；`adapters/base.py` `AgentExecutor` 抽象（执行层模型中立，openclaw 为 `OpenClawExecutor` 实现）；`TransferEscalationWorker` 转移超时(30min)无回报 → `task.blocked` 升级（第 6 个 Worker，不依赖 Agent 记得回来干活）。测试 21→27 全绿，狗粮实机复验闭环通过。
- **底层算力接入 + 前门（2026-08-31）**：`app/services/llm.py` `LLMClient`——**只要用户有 API 就接得进**（anthropic / openai / 任意 openai_compatible 端点，`LLM_PROVIDER`+`LLM_API_KEY`/`ANTHROPIC_API_KEY`/`OPENAI_API_KEY`+`LLM_BASE_URL`+`LLM_MODEL`，P0 不烘焙、严格模式缺 key 拒用，`load_dotenv` 读 `.env`）；`requirement_parser.py` 用算力把"用户给目标"解析成任务草案；`team_matcher.py` 按注册表匹配责任/复核人（owner 优先 builtin）；`POST /projects/{id}/messages?parse=true` 自动建任务+责任链；`POST /agents/register`（executor=builtin/openclaw）/`GET /agents` 注册中心（事件溯源，可审计）。测试 27→32 全绿（LLM mock 不烧 token）。
- **NG 自研 agent + 全自动（2026-08-31）**：`app/agents/builtin.py` `ng-assistant`——跑平台自己的算力（DeepSeek 已接通），不依赖 openclaw：读 `GET /tasks/{id}/context` → 产出落盘 `artifacts/` → `app/agents/run.py` CLI 或 **AutoAgentWorker（第 7 个 Worker，自动认领 builtin 任务）**执行 → 自动 in_review。**全自动 dogfood 5/5 通**（零人工）：用户一句目标 → 解析任务 → 派 ng-assistant(owner)+lobster(reviewer) → 自动产出 → 全部交接复核。测试 32→37 全绿。

- **审批 gate 通用化（2026-08-31）**：`app/security/approval_gate.py` `ApprovalGate.ensure_approved`——任何 L3/L4 动作自动建审批请求（幂等）+ `PendingApproval`→409+approval_id；`pause_project` 已重构走通用 gate；任务状态联动（请求审批→PENDING_APPROVAL，批准→COMPLETED，拒绝→退回）。测试 40→**43 全绿**。完整修复清单见 `docs/fix-report-2026-08-31.md`。
- **第二执行层 claude_sdk + openclaw 去耦（2026-08-31）**：`app/adapters/claude_sdk.py` `ClaudeSDKExecutor`（官方 Anthropic SDK，`claude-opus-4-8` + adaptive thinking + effort；`app/agents/run.py --executor claude_sdk`）；**openclaw 去耦**——main.py 移除模块级依赖，`/agents/transfer` 懒加载 + 503 优雅报错，openclaw 仅作外接 agent 联系层，核心不依赖它。执行层三实现：builtin(自研算力) / claude_sdk(Anthropic) / openclaw(仅联系)。测试 43→**45 全绿**。真实 Claude 需设 `ANTHROPIC_API_KEY`。

- **算力已接通 OpenAI（2026-08-31）**：`.env` 激活 OpenAI（`gpt-4o-mini`），DeepSeek 备选注释保留；claude_sdk 为休眠可选实现。**OpenAI 全路径 dogfood 通过**（解析 4 任务 → ng-assistant 自动产出 → in_review）。

**待做（后续阶段）**：
- **投影物化/乐观锁**：projection_version / expected_version 启用
- **前端看板**：React 项目/任务/责任链/审批视图（frontend/ 待建）

本地 MVP 已放行；正式生产待上述后续项 + 真实 IdP 接入后再评估。
