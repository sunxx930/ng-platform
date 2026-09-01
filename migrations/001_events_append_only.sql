-- 事件表 append-only 强制（补强项 15.2：禁止 UPDATE/DELETE）
-- 应用通过 ng_app 角色连接；该角色对 events 只有 INSERT/SELECT。

-- 1) 应用角色（仅 INSERT/SELECT，无 UPDATE/DELETE）
-- P0 密钥契约（2026-08-31）：此处不写密码字面量；密码由 run_migrations.py
--    用 NG_APP_PASSWORD 环境变量 + 绑定参数统一 ALTER ROLE（角色已存在也同步）。
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ng_app') THEN
    CREATE ROLE ng_app LOGIN;
  ELSE
    ALTER ROLE ng_app WITH LOGIN;
  END IF;
END$$;

-- 2) 撤销任何既有写权限，只留 INSERT/SELECT
REVOKE ALL ON TABLE events FROM ng_app;
GRANT SELECT, INSERT ON TABLE events TO ng_app;
GRANT USAGE, SELECT ON SEQUENCE events_id_seq TO ng_app;

-- 3) 其他表给应用角色常规权限（events 之外的业务表；死表已移除 2026-09-01，见 004）
GRANT SELECT, INSERT, UPDATE ON TABLE projects, tasks, agents, worker_runs
  TO ng_app;

-- 4) 数据库层拦截：事件表拒改（防御性触发器）
CREATE OR REPLACE FUNCTION fn_events_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'events 表是 append-only，禁止 %', TG_OP;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_events_no_update ON events;
CREATE TRIGGER trg_events_no_update
  BEFORE UPDATE OR DELETE ON events
  FOR EACH ROW EXECUTE FUNCTION fn_events_append_only();

-- 说明：应用连接串用 ng_app 角色（DATABASE_URL=postgresql+psycopg://ng_app:ng@localhost:5432/ng_platform）
