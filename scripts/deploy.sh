#!/usr/bin/env bash
# ng-platform 国内云一键部署（2026-09-01）
#
# 作用（幂等，可重复跑）：
#   1. 服务器初始化：装 docker/caddy（已装则跳过）
#   2. 生成生产强随机密钥（.env.deploy，gitignore）
#   3. 前端构建（后端托管 dist 需要）
#   4. docker compose up -d --build（db/migrate/api/workers）
#   5. 健康检查
#   6. 可选：Caddy HTTPS 反代（传 DOMAIN 环境变量才配）
#
# 用法（在 ng-platform 项目根目录）:
#   bash scripts/deploy.sh                    # 仅部署（IP 访问）
#   DOMAIN=your-domain.com bash scripts/deploy.sh   # 部署 + HTTPS 反代
#
# 安全：NG_ENV=production 严格模式，token 缺/默认值 → 拒绝启动。
set -euo pipefail
cd "$(dirname "$0")/.."

# ---------- 1. 服务器初始化 ----------
if ! command -v docker >/dev/null 2>&1; then
  echo "[deploy] 安装 docker…"
  sudo apt-get update -y && sudo apt-get install -y docker.io docker-compose-v2
  sudo systemctl enable --now docker
fi
if ! command -v caddy >/dev/null 2>&1; then
  echo "[deploy] 安装 caddy…"
  sudo apt-get install -y caddy || true
  sudo systemctl enable --now caddy 2>/dev/null || true
fi
docker --version | head -1

# ---------- 2. 生产密钥（.env.deploy，gitignore） ----------
ENV_FILE=".env.deploy"
if [ ! -f "$ENV_FILE" ]; then
  echo "[deploy] 生成生产密钥 → $ENV_FILE"
  cat > "$ENV_FILE" <<EOF
POSTGRES_PASSWORD=$(openssl rand -hex 24)
NG_APP_PASSWORD=$(openssl rand -hex 24)
NG_LEVEL1_TOKEN=$(openssl rand -hex 32)
NG_LEVEL3_TOKEN=$(openssl rand -hex 32)
EOF
  chmod 600 "$ENV_FILE"
else
  echo "[deploy] 复用已有密钥 $ENV_FILE"
fi
set -a; source "$ENV_FILE"; set +a

# ---------- 3. 前端构建（后端托管 dist 需要） ----------
echo "[deploy] 前端构建…"
if [ ! -d frontend/node_modules ]; then
  (cd frontend && npm install)
fi
(cd frontend && npm run build)

# ---------- 4. docker compose up ----------
echo "[deploy] docker compose up -d --build…"
docker compose up -d --build

# ---------- 5. 健康检查 ----------
echo "[deploy] 等待 api healthy…"
for i in $(seq 1 45); do
  if curl -fsS http://localhost:8080/health >/dev/null 2>&1; then
    echo "[deploy] api healthy（第 ${i} 次探测）"; break
  fi
  sleep 2
  if [ "$i" = "45" ]; then
    echo "[deploy] 超时，api 日志："; docker compose logs api --tail 50; exit 1
  fi
done
echo "[deploy] 健康检查: $(curl -fsS http://localhost:8080/health)"

# ---------- 6. Caddy HTTPS 反代（可选，DOMAIN 传入才配） ----------
if [ -n "${DOMAIN:-}" ]; then
  echo "[deploy] 配置 Caddy 反代 https://${DOMAIN} → 127.0.0.1:8080"
  sudo tee /etc/caddy/Caddyfile >/dev/null <<EOF
${DOMAIN} {
    reverse_proxy 127.0.0.1:8080
}
EOF
  sudo systemctl reload caddy
  echo "[deploy] HTTPS 已配置：https://${DOMAIN}（首次访问自动签发证书）"
else
  echo "[deploy] 未传 DOMAIN，跳过 HTTPS（临时用 http://<服务器IP>:8080）"
fi

echo "[deploy] ✅ 部署完成"
