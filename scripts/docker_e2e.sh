#!/usr/bin/env bash
# ng-platform Docker E2E —— 完整链路实测（任何有 docker 的机器可跑）。
#
# 覆盖三签阻塞项：
#   - P0 密钥契约：migrate 注入 NG_APP_PASSWORD → ALTER ROLE 后 ng_app 密码认证可连
#   - P1 幂等反例：同 key 不同 to → 409
#   - 全栈冒烟：建项目/建任务/状态流转/审计回放
#
# 用法:
#   POSTGRES_PASSWORD=<强> NG_APP_PASSWORD=<强> bash scripts/docker_e2e.sh
#
# 注意：本脚本不清空既有 pgdata 卷。首次跑或要干净环境时先 `docker compose down -v`。
set -euo pipefail
cd "$(dirname "$0")/.."

export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:?需注入}"
export NG_APP_PASSWORD="${NG_APP_PASSWORD:?需注入}"
export NG_LEVEL1_TOKEN="${NG_LEVEL1_TOKEN:-$(openssl rand -hex 24)}"
export NG_LEVEL3_TOKEN="${NG_LEVEL3_TOKEN:-$(openssl rand -hex 24)}"
echo "[e2e] NG_LEVEL1_TOKEN=${NG_LEVEL1_TOKEN:0:8}… NG_LEVEL3_TOKEN=${NG_LEVEL3_TOKEN:0:8}…（可外部传入固定值）"

# 安全（2026-08-31）：LLM key 走 compose secrets（.secrets/，gitignore）。
# CI/全新 checkout 无 .secrets/ → compose 会失败；E2E 不需要真实算力，生成占位即可。
mkdir -p .secrets
for s in OPENAI_API_KEY ANTHROPIC_API_KEY LLM_API_KEY; do
  [ -f ".secrets/$s" ] || : > ".secrets/$s"
done
chmod 600 .secrets/* 2>/dev/null || true
echo "[e2e] .secrets 占位就绪（E2E 不用真实算力 key）"

echo "[e2e] docker compose up -d --build"
docker compose up -d --build

echo "[e2e] 等待 api healthy"
for i in $(seq 1 90); do
  if curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
    echo "[e2e] api healthy（第 ${i} 次探测）"; break
  fi
  sleep 2
  if [ "$i" = "90" ]; then
    echo "[e2e] 超时，api 日志："; docker compose logs api --tail 50; exit 1
  fi
done

echo "[e2e] 健康检查: $(curl -fsS http://localhost:8000/health)"

H3="Authorization: Bearer $NG_LEVEL3_TOKEN"
H1="Authorization: Bearer $NG_LEVEL1_TOKEN"

echo "[e2e] 建项目"
PID=$(curl -fsS -X POST -H "$H3" \
  "http://localhost:8000/projects?title=e2e&goal=test" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["project_id"])')
echo "  project_id=$PID"

echo "[e2e] 建任务 → in_progress → 审计回放"
# 中文标题必须 percent-encode（query 参数），否则 HTTP 400
TID=$(curl -fsS -G -X POST -H "$H1" --data-urlencode "title=E2E任务" \
  "http://localhost:8000/projects/$PID/tasks" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["task_id"])')
curl -fsS -X PATCH -H "$H1" "http://localhost:8000/tasks/$TID/state?to=in_progress" >/dev/null
curl -fsS -H "$H1" "http://localhost:8000/projects/$PID/audit" \
  | python3 -c '
import sys, json
ev = json.load(sys.stdin)["events"]
assert any(e["event_type"] == "task.created" for e in ev), "缺 task.created"
assert any(e["event_type"] == "task.state_changed" for e in ev), "缺 task.state_changed"
print(f"  审计事件数={len(ev)} OK")'

echo "[e2e] P1 反例：同 key 不同 to → 第二次应 409"
# 幂等键全库唯一（非 per-task），每次运行用唯一 key 避免上次残留冲突
PK="e2e-pk-$(openssl rand -hex 4)"
C1=$(curl -s -o /dev/null -w '%{http_code}' -X PATCH -H "$H1" \
  "http://localhost:8000/tasks/$TID/state?to=blocked&idempotency_key=$PK")
[ "$C1" = "200" ] || { echo "  首次(key=$PK,to=blocked)应 200，实际 $C1 ❌"; exit 1; }
C2=$(curl -s -o /dev/null -w '%{http_code}' -X PATCH -H "$H1" \
  "http://localhost:8000/tasks/$TID/state?to=in_progress&idempotency_key=$PK")
[ "$C2" = "409" ] && echo "  → 409 ✅" || { echo "  二次(key=$PK,to=in_progress 不同意图)应 409，实际 $C2 ❌"; exit 1; }

echo "[e2e] P0 密钥契约：ng_app 用 NG_APP_PASSWORD 认证连接"
docker compose exec -T db bash -c "PGPASSWORD='$NG_APP_PASSWORD' psql -h db -U ng_app -d ng_platform -tAc 'SELECT 1'" >/dev/null \
  && echo "  → ng_app 密码认证 OK ✅" || { echo "  → ng_app 认证失败 ❌"; exit 1; }

echo "[e2e] ✅ 全部通过"
echo "[e2e] 清理: docker compose down"
docker compose down
