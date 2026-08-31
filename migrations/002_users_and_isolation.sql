-- 多用户（2026-09-01）：注册用户账号 + 事件 user 维度 + 项目按用户隔离
-- 与 schema.sql 内容一致；已运行库走此迁移（IF NOT EXISTS 幂等，可重复应用）

CREATE TABLE IF NOT EXISTS users (
  id            UUID PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,          -- scrypt 格式 salt$hash，stdlib 无新依赖
  level         INT  NOT NULL DEFAULT 1,  -- 1=L1 普通用户
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS auth_tokens (
  token_hash  TEXT PRIMARY KEY,          -- sha256(明文 token)
  user_id     UUID NOT NULL REFERENCES users(id),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  revoked     BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_user ON auth_tokens(user_id);

-- 事件加 user 维度（审计正源）
ALTER TABLE events ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id);
CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id);

-- P0（2026-09-01 体检发现）：ng_app 应用角色对多用户新表必须有读写权限，
-- 否则 DB 模式下注册/登录/登出全部 500。业务表授权（非 events 审计表，不禁写）。
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE users, auth_tokens TO ng_app;
