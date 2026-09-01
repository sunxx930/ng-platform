"""启动时自动执行 pending 迁移（Fix 2 生产缺口）。
用法: python run_migrations.py   （读 DATABASE_URL，逐条应用 migrations/*.sql）
"""
import os, sys, glob
from sqlalchemy import create_engine, text

def main():
    # 迁移用独立超管连接（建表/触发器/角色需超级用户；应用角色只读业务）
    dburl = os.environ.get("MIGRATION_DATABASE_URL", os.environ.get("DATABASE_URL", ""))
    if not dburl:
        print("无 DATABASE_URL，跳过迁移"); return
    # P0 密钥契约（2026-08-31）：ng_app 密码统一走 NG_APP_PASSWORD，
    # 迁移文件不含密码字面量；角色已存在也会被 ALTER 同步（堵死残留）
    ng_pw = os.environ.get("NG_APP_PASSWORD")
    strict = os.environ.get("NG_ENV", "").lower() == "production"
    if ng_pw is None:
        if strict:
            print("[migrate] 生产环境必须注入 NG_APP_PASSWORD"); sys.exit(1)
        ng_pw = "ng"   # 仅本地 dev 兜底
    eng = create_engine(dburl)
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS _migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())"))
        applied = {r[0] for r in conn.execute(text("SELECT name FROM _migrations")).fetchall()}
    for f in sorted(glob.glob("migrations/*.sql")):
        name = os.path.basename(f)
        if name in applied:
            continue
        sql = open(f).read()
        with eng.begin() as conn:
            conn.execute(text("INSERT INTO _migrations (name) VALUES (:n) ON CONFLICT DO NOTHING"), {"n": name})
            try:
                conn.execute(text(sql))
                print(f"[migrate] 应用 {name}")
            except Exception as e:
                conn.execute(text("DELETE FROM _migrations WHERE name=:n"), {"n": name})
                print(f"[migrate] {name} 失败: {e}"); raise
    # 密钥契约落地：应用角色密码统一 ALTER（角色已存在也同步，堵死残留）
    # PG 的 ALTER ROLE 是 DDL 不接受绑定参数：密码单引号转义后内联（天然防注入）
    safe_pw = ng_pw.replace("'", "''")
    with eng.begin() as conn:
        conn.execute(text(f"ALTER ROLE ng_app WITH PASSWORD '{safe_pw}'"))
    # P1-1 投影物化：003 应用后若存量事件非空且投影空 → 自动重建（一次性迁移灌存量）
    _maybe_rebuild_projection(eng, dburl)
    print("[migrate] 完成")


def _maybe_rebuild_projection(eng, dburl):
    """events>0 且 projects=0 → 全量重建投影表（TRUNCATE+重放，需超管连接）。"""
    from sqlalchemy import text as _text
    with eng.connect() as conn:
        n_events = conn.execute(_text("SELECT count(*) FROM events")).scalar()
        n_proj = conn.execute(_text("SELECT count(*) FROM projects")).scalar()
    if not n_events or n_proj:
        return   # 空库无需重建；投影已非空则不动（避免重复清理业务行）
    print(f"[migrate] 存量 {n_events} 条事件 → 重建投影表…", flush=True)
    from app.storage.projection import Projector
    Projector(eng).rebuild()
    print("[migrate] 投影重建完成", flush=True)

if __name__ == "__main__":
    main()
