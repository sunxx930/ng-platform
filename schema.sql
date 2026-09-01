-- NG AI Platform —— PostgreSQL 数据模型（架构文档五 + 补强项 15.2/15.3/15.4）
-- 事件表：审计正源，DB 角色禁止 UPDATE/DELETE

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE projects (
  id            UUID PRIMARY KEY,
  title         TEXT NOT NULL,
  goal          TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'active',   -- active|paused|archived
  owner_id      UUID,                             -- 创建用户（FK 由迁移 003 补，避免建表顺序问题）
  projection_version BIGINT NOT NULL DEFAULT 0,   -- 补强项 15.2（投影物化）
  event_count   BIGINT NOT NULL DEFAULT 0,        -- 审计旁路
  user_id       UUID,                             -- 事件 user 维度对齐
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 多用户（2026-09-01）：注册用户账号，事件加 user 维度，项目按用户隔离
CREATE TABLE users (
  id            UUID PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,          -- scrypt 格式 salt$hash，stdlib 无新依赖
  level         INT  NOT NULL DEFAULT 1,  -- 1=L1 普通用户
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE auth_tokens (
  token_hash  TEXT PRIMARY KEY,          -- sha256(明文 token)
  user_id     UUID NOT NULL REFERENCES users(id),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  revoked     BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX idx_auth_tokens_user ON auth_tokens(user_id);

CREATE TABLE agents (
  id           UUID PRIMARY KEY,
  name         TEXT NOT NULL,
  capability   JSONB NOT NULL DEFAULT '{}',
  role         TEXT,
  status       TEXT NOT NULL DEFAULT 'available',
  permission   TEXT NOT NULL DEFAULT 'L1',        -- L0-L4
  executor     TEXT,                              -- builtin|openclaw（投影物化 2026-09-01）
  history      JSONB NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX idx_agents_name ON agents(name);

CREATE TABLE tasks (
  id             UUID PRIMARY KEY,
  project_id     UUID NOT NULL REFERENCES projects(id),
  title          TEXT NOT NULL,
  description    TEXT,
  owner_agent_id UUID REFERENCES agents(id),       -- 弃用（agent 身份=名字，见 owner_agent_name）
  reviewer_id    UUID REFERENCES agents(id),       -- 弃用
  owner_agent_name    TEXT,                        -- 投影物化（2026-09-01）：agent 名字
  reviewer_agent_name TEXT,
  has_deliverable     BOOLEAN NOT NULL DEFAULT false,
  deadline_ts         DOUBLE PRECISION,
  status         TEXT NOT NULL DEFAULT 'todo',    -- 冻结枚举 + 白名单（15.5）
  depends_on     UUID[],
  deadline       TIMESTAMPTZ,
  evidence       JSONB NOT NULL DEFAULT '{}',
  expected_version BIGINT NOT NULL DEFAULT 0,     -- 乐观锁（15.2）
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_tasks_project ON tasks(project_id);

CREATE TABLE sessions (
  id           UUID PRIMARY KEY,
  project_id   UUID NOT NULL REFERENCES projects(id),
  title        TEXT
);

CREATE TABLE messages (
  id            UUID PRIMARY KEY,
  session_id    UUID NOT NULL REFERENCES sessions(id),
  source        TEXT NOT NULL,                    -- user|agent|system
  body          TEXT,
  media         JSONB NOT NULL DEFAULT '[]',
  aggregate_id  UUID,
  idempotency_key TEXT UNIQUE,                    -- 幂等（15.3）
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE deliverables (
  id           UUID PRIMARY KEY,
  task_id      UUID NOT NULL REFERENCES tasks(id),
  file_ref     TEXT NOT NULL,
  version      INT  NOT NULL DEFAULT 1,
  hash         TEXT NOT NULL,
  source       TEXT,
  idempotency_key TEXT UNIQUE,                    -- 幂等（15.3）
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE reviews (
  id           UUID PRIMARY KEY,
  task_id      UUID NOT NULL REFERENCES tasks(id),
  reviewer_id  UUID NOT NULL,
  opinion      TEXT,
  verdict      TEXT NOT NULL,                     -- pass|reject|needs_changes|inconclusive
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE approvals (
  id           UUID PRIMARY KEY,
  scope        TEXT NOT NULL,
  action_ref   TEXT,
  approver_id  UUID,
  result       TEXT,                              -- approve|reject
  reason       TEXT,
  idempotency_key TEXT UNIQUE,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 审计正源（15.2：禁止 UPDATE/DELETE）
CREATE TABLE events (
  id             BIGSERIAL PRIMARY KEY,
  project_id     UUID,
  task_id        UUID,
  event_type     TEXT NOT NULL,
  actor          TEXT,
  user_id        UUID REFERENCES users(id),       -- 多用户：事件 user 维度（2026-09-01）
  payload        JSONB NOT NULL DEFAULT '{}',
  idempotency_key TEXT UNIQUE,                    -- 幂等（15.3）
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_events_project ON events(project_id, created_at);
CREATE INDEX idx_events_task    ON events(task_id);
CREATE INDEX idx_events_user    ON events(user_id);

-- 交接摘要
CREATE TABLE handovers (
  id           UUID PRIMARY KEY,
  task_id      UUID NOT NULL,
  summary      TEXT NOT NULL,
  pending      JSONB NOT NULL DEFAULT '[]',
  next_owner   UUID,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Worker 租约（15.4）
CREATE TABLE worker_runs (
  id            UUID PRIMARY KEY,
  task_id       UUID NOT NULL UNIQUE,
  lease_owner   TEXT NOT NULL,
  worker_type   TEXT NOT NULL DEFAULT 'default',
  heartbeat     TIMESTAMPTZ NOT NULL DEFAULT now(),
  attempt       INT  NOT NULL DEFAULT 1,
  retry_at      TIMESTAMPTZ,
  max_attempts  INT  NOT NULL DEFAULT 3,
  status        TEXT NOT NULL DEFAULT 'running',
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_worker_runs_heartbeat ON worker_runs(heartbeat);

-- 投递/通知（15.3）
CREATE TABLE deliveries (
  id             BIGSERIAL PRIMARY KEY,
  task_id        UUID,
  idempotency_key TEXT UNIQUE,
  attempt        INT  NOT NULL DEFAULT 0,
  status         TEXT NOT NULL DEFAULT 'pending', -- pending|sent|failed|dead
  failure_reason TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 事件不可篡改：DB 角色仅授权 INSERT（补强项 15.2 由迁移/权限脚本强制）

-- 投影读模型（2026-09-01，P1-1 投影物化）：feedback/usage 全量投影表
CREATE TABLE feedback_proj (
  id            BIGSERIAL PRIMARY KEY,
  content       TEXT NOT NULL,
  contact       TEXT NOT NULL DEFAULT '',
  rating        INT,
  actor         TEXT,
  created_at_ts DOUBLE PRECISION NOT NULL
);

CREATE TABLE usage_proj (
  id            BIGSERIAL PRIMARY KEY,
  project_id    UUID,
  task_id       UUID,
  label         TEXT,
  provider      TEXT,
  model         TEXT,
  input_tokens  BIGINT NOT NULL DEFAULT 0,
  output_tokens BIGINT NOT NULL DEFAULT 0,
  created_at_ts DOUBLE PRECISION NOT NULL
);
