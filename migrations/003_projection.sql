-- 投影物化 + 乐观锁（2026-09-01，P1-1）：启用 schema 预留的 projection_version/expected_version
-- 事件仍是唯一正源；projects/tasks/agents/feedback_proj/usage_proj 是从事件折叠的读模型（可 rebuild 重建）。
-- 与 schema.sql 内容一致；IF NOT EXISTS 幂等，可重复应用。

-- projects：owner_id 作为创建用户（存量全 NULL，与现状一致）
ALTER TABLE projects ADD CONSTRAINT fk_projects_owner FOREIGN KEY (owner_id) REFERENCES users(id);
ALTER TABLE projects ADD COLUMN IF NOT EXISTS event_count BIGINT NOT NULL DEFAULT 0;  -- 审计旁路
ALTER TABLE projects ADD COLUMN IF NOT EXISTS user_id UUID;                          -- 事件 user 维度对齐

-- tasks：agent 身份是名字字符串（events.payload.agent），owner_agent_id/reviewer_id(UUID) 错配弃用，
-- 新增 TEXT 名字列；看板/上下文派生列
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS owner_agent_name    TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS reviewer_agent_name TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS has_deliverable     BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS deadline_ts         DOUBLE PRECISION;
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);

-- agents：名字身份（latest-wins）。name 已有列，仅补 executor + 唯一索引
ALTER TABLE agents ADD COLUMN IF NOT EXISTS executor TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_name ON agents(name);

-- feedback / usage 投影表（全量投影，2026-09-01）
CREATE TABLE IF NOT EXISTS feedback_proj (
  id            BIGSERIAL PRIMARY KEY,
  content       TEXT NOT NULL,
  contact       TEXT NOT NULL DEFAULT '',
  rating        INT,
  actor         TEXT,
  created_at_ts DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_proj (
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

-- 应用角色权限（001 已给 projects/tasks/agents UPDATE；补投影表 + 序列）
GRANT SELECT, INSERT, UPDATE ON TABLE projects, tasks, agents, feedback_proj, usage_proj TO ng_app;
GRANT USAGE, SELECT ON SEQUENCE feedback_proj_id_seq, usage_proj_id_seq TO ng_app;
