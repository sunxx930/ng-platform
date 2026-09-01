# NG-AI-Platform 国内云部署手册（2026-09-01）

> 目标：把 ng-platform 部署到国内云（阿里云/腾讯云），配 .com 域名 + HTTPS。
> 前置：项目已完整（多用户/投影物化/Windows 兼容/通知等全部就绪），本手册负责「上线」。

---

## 0. 概览

```
用户 → 域名(.com) → Caddy(HTTPS 反代) → api:8080 → PostgreSQL(db)
                                          └→ workers(调度)
```

- 后端 FastAPI 托管前端静态产物（`frontend/dist`），单容器即可
- 生产栈：`docker compose up --build` 一键起（db/migrate/api/workers）
- 可选：openclaw gateway（`docker compose --profile openclaw up`）

---

## 1. 服务器选型

| 规格 | 用途 | 参考价 |
|---|---|---|
| 2 vCPU / 2G / 40G SSD | 入门（不带 openclaw） | ~¥50-80/月 |
| 2 vCPU / 4G / 50G SSD | 推荐（带 openclaw gateway / 宽松） | ~¥80-150/月 |

- **系统**：Ubuntu 22.04 LTS（Docker 支持最好）
- **地域**：选离目标用户近的（如国内业务选华东/华北）
- **厂商**：阿里云 / 腾讯云 轻量应用服务器（备案一体化最省事）

## 2. 域名 + 备案

1. **买域名**：在**同一云厂商**买 .com（如 `阿里云万网` / `腾讯云 DNSPod`），备案联动最顺。
2. **备案**：云厂商控制台 → ICP 备案 → 按引导填主体信息（个人/企业）。
   - 需要：域名 + 服务器 + 实名认证
   - 周期：约 1-3 周（管局审核）。**备案期间可用 IP 直连测试，域名解析等备案通过后再做**。
3. **解析**：备案通过后，把域名 A 记录指向服务器公网 IP。

## 3. 服务器初始化（首次）

```bash
# 用 root 或 sudo 用户登录服务器
sudo apt update && sudo apt upgrade -y
sudo apt install -y docker.io docker-compose-v2 caddy
sudo systemctl enable --now docker caddy
```

> 或直接跑 `scripts/deploy.sh`（自动完成本步骤 + 后续全部）。

## 4. 部署代码

```bash
# 方案 A：git clone（推荐，便于更新）
git clone <你的仓库> ng-platform && cd ng-platform

# 方案 B：scp 上传
# 本地: scp -r ~/Desktop/ng-platform user@server:/opt/ng-platform
```

## 5. 前端构建（后端托管 dist 需要）

```bash
cd ng-platform/frontend && npm install && npm run build
cd ..   # 现在 frontend/dist 存在，docker 构建会打包进镜像
```

## 6. 生产密钥 + 启动

生成强随机密钥并启动：

```bash
cd ng-platform
POSTGRES_PASSWORD="$(openssl rand -hex 24)" \
NG_APP_PASSWORD="$(openssl rand -hex 24)" \
NG_LEVEL1_TOKEN="$(openssl rand -hex 32)" \
NG_LEVEL3_TOKEN="$(openssl rand -hex 32)" \
docker compose up -d --build
```

- migrate 容器自动建库 + 迁移 + 重建投影；api healthy 后访问 `http://<服务器IP>:8080/health` 应返回 `{"status":"ok"}`。
- LLM API key：走 `.secrets/`（compose secrets）或环境变量 `LLM_PROVIDER`/`OPENAI_API_KEY` 等，见 README「配置」。
- **首次用 IP 验证**：`http://<IP>:8080` 应出现注册/登录页。

## 7. HTTPS + 域名（备案通过后）

用 Caddy 自动签发 Let's Encrypt 证书：

```bash
sudo tee /etc/caddy/Caddyfile >/dev/null <<'EOF'
your-domain.com {
    reverse_proxy 127.0.0.1:8080
}
EOF
sudo systemctl reload caddy
```

- Caddy 自动申请并续期证书，`https://your-domain.com` 即生效。
- 若 API 端口收敛（compose 里 db 不暴露宿主端口，api 绑 8080），反代只需指 8080。

## 8. 运维

| 操作 | 命令 |
|---|---|
| 查看日志 | `docker compose logs -f api` |
| 重启 | `docker compose restart api` |
| 更新部署 | `git pull && cd frontend && npm run build && cd .. && docker compose up -d --build` |
| 备份 DB | `docker compose exec db pg_dump -U ng ng_platform > backup.sql` |
| 重建投影 | `docker compose exec api python -m app.projection_rebuild`（需超管连接） |

## 9. 安全要点

- **严格模式已内置**：`NG_ENV=production` 时缺 token/默认值 → 拒绝启动。
- Caddy 反代 8080，**不要**把 docker 端口直接暴露公网（api 已绑 8080，db 不发布宿主端口）。
- `.secrets/`（LLM key）与 `.env` 不入库（gitignore），服务器上单独配置。
- openclaw 可选：`docker compose --profile openclaw up -d` 起 gateway，配 `OPENCLAW_GATEWAY_URL`/`OPENCLAW_GATEWAY_TOKEN`。

## 10. 参考

- 完整功能：README.md（多用户 / 投影物化 / 通知 / Windows 兼容等）
- 迁移重建：README「迁移/重建」
- 跨平台 E2E：`scripts/docker_e2e.py`
