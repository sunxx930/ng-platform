#!/usr/bin/env python3
"""ng-platform Docker E2E —— 跨平台版（Windows/macOS/Linux 均可跑）。

与 scripts/docker_e2e.sh 语义一致：P0 密钥契约 / P1 幂等反例 / 全栈冒烟。
用 httpx（HTTP 断言）+ subprocess（docker compose）+ secrets（强随机 token），
替代 bash 依赖的 openssl/curl/seq。

用法:
  POSTGRES_PASSWORD=<强> NG_APP_PASSWORD=<强> python3 scripts/docker_e2e.py

注意：本脚本不清空既有 pgdata 卷。首次跑或要干净环境时先 `docker compose down -v`。
"""
from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://localhost:8080"


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if check and proc.returncode != 0:
        print("[e2e] 命令失败:", " ".join(cmd), file=sys.stderr)
        print(proc.stderr[-2000:], file=sys.stderr)
        sys.exit(1)
    return proc


def _compose(*args: str) -> subprocess.CompletedProcess:
    return _run(["docker", "compose", *args])


def _require(name: str) -> str:
    val = os.environ.get(name, "")
    if not val:
        print(f"[e2e] 缺少环境变量 {name}"); sys.exit(1)
    return val


def _random_token() -> str:
    return secrets.token_hex(24)


def main():
    pw = _require("POSTGRES_PASSWORD")
    ng_pw = _require("NG_APP_PASSWORD")
    os.environ["NG_LEVEL1_TOKEN"] = os.environ.get("NG_LEVEL1_TOKEN") or _random_token()
    os.environ["NG_LEVEL3_TOKEN"] = os.environ.get("NG_LEVEL3_TOKEN") or _random_token()
    print(f"[e2e] NG_LEVEL1_TOKEN={os.environ['NG_LEVEL1_TOKEN'][:8]}… "
          f"NG_LEVEL3_TOKEN={os.environ['NG_LEVEL3_TOKEN'][:8]}…（可外部传入固定值）")

    # 安全（2026-08-31）：LLM key 走 compose secrets（.secrets/，gitignore）。
    # CI/全新 checkout 无 .secrets/ → compose 会失败；E2E 不需要真实算力，生成占位即可。
    secrets_dir = ROOT / ".secrets"
    secrets_dir.mkdir(exist_ok=True)
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LLM_API_KEY"):
        (secrets_dir / name).touch(exist_ok=True)
    print("[e2e] .secrets 占位就绪（E2E 不用真实算力 key）")

    print("[e2e] docker compose up -d --build")
    _compose("up", "-d", "--build")

    # 等待 api healthy
    print("[e2e] 等待 api healthy")
    healthy = False
    with httpx.Client(timeout=10) as client:
        for i in range(1, 91):
            try:
                if client.get(f"{BASE}/health").status_code == 200:
                    print(f"[e2e] api healthy（第 {i} 次探测）"); healthy = True; break
            except httpx.HTTPError:
                pass
            if i < 90:
                import time
                time.sleep(2)
        if not healthy:
            logs = _compose("logs", "api", "--tail", "50")
            print("[e2e] 超时，api 日志：\n" + logs.stdout)
            _compose("down"); sys.exit(1)

    h3 = {"Authorization": f"Bearer {os.environ['NG_LEVEL3_TOKEN']}"}
    h1 = {"Authorization": f"Bearer {os.environ['NG_LEVEL1_TOKEN']}"}
    with httpx.Client(timeout=15) as client:
        # 建项目
        print("[e2e] 建项目")
        r = client.post(f"{BASE}/projects", params={"title": "e2e", "goal": "test"}, headers=h3)
        r.raise_for_status()
        pid = r.json()["project_id"]
        print(f"  project_id={pid}")

        # 建任务（中文标题需 URL 编码，httpx params 自动处理）
        print("[e2e] 建任务 → in_progress → 审计回放")
        r = client.post(f"{BASE}/projects/{pid}/tasks",
                        params={"title": "E2E任务"}, headers=h1)
        r.raise_for_status()
        tid = r.json()["task_id"]
        client.patch(f"{BASE}/tasks/{tid}/state",
                     params={"to": "in_progress"}, headers=h1).raise_for_status()
        audit = client.get(f"{BASE}/projects/{pid}/audit", headers=h1).raise_for_status().json()["events"]
        types = {e["event_type"] for e in audit}
        assert "task.created" in types, "缺 task.created"
        assert "task.state_changed" in types, "缺 task.state_changed"
        print(f"  审计事件数={len(audit)} OK")

        # P1 反例：同 key 不同 to → 第二次应 409
        print("[e2e] P1 反例：同 key 不同 to → 第二次应 409")
        pk = f"e2e-pk-{secrets.token_hex(4)}"
        c1 = client.patch(f"{BASE}/tasks/{tid}/state",
                          params={"to": "blocked", "idempotency_key": pk}, headers=h1)
        assert c1.status_code == 200, f"首次(key={pk},to=blocked)应 200，实际 {c1.status_code}"
        c2 = client.patch(f"{BASE}/tasks/{tid}/state",
                          params={"to": "in_progress", "idempotency_key": pk}, headers=h1)
        assert c2.status_code == 409, f"二次(key={pk},to=in_progress)应 409，实际 {c2.status_code}"
        print("  → 409 ✅")

    # P0 密钥契约：ng_app 用 NG_APP_PASSWORD 认证连接
    print("[e2e] P0 密钥契约：ng_app 用 NG_APP_PASSWORD 认证连接")
    psql_cmd = (
        f"PGPASSWORD='{ng_pw}' psql -h db -U ng_app -d ng_platform -tAc 'SELECT 1'"
    )
    r = _compose("exec", "-T", "db", "bash", "-c", psql_cmd)
    assert r.returncode == 0, f"ng_app 认证失败: {r.stderr[:300]}"
    print("  → ng_app 密码认证 OK ✅")

    print("[e2e] ✅ 全部通过")
    _compose("down")


if __name__ == "__main__":
    main()
