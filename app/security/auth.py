"""真实身份鉴权（Fix 4 生产缺口）—— Bearer token → 身份/权限级别。

多用户（2026-09-01）：
- 注册用户会话 token → UserStore 解析（main.py 注入），返回 user_id/username/level
- 静态 token→(user,level) 注册表保留，作服务器端管理员/agent 通道（NG_LEVEL3/NG_LEVEL1）

P0 密钥契约（2026-08-31）：生产/严格模式拒绝不安全默认 token，杜绝漏配静默上线。
"""
from __future__ import annotations

import os

from fastapi import HTTPException, Header

# 本地 dev 兜底默认（生产不得使用）
_INSECURE_DEFAULTS = {"l3-test-token", "l1-agent-token", ""}


def resolve_tokens(env: dict) -> dict[str, tuple[str, int]]:
    """解析 token → (user, level)。

    - NG_ENV=production 或 NG_STRICT_TOKENS=1 时：token 必须注入且非不安全默认，否则 RuntimeError（拒绝启动）
    - 非严格：env 有就用，无才 fallback 本地 dev 默认
    """
    strict = str(env.get("NG_ENV", "")).lower() == "production" \
        or str(env.get("NG_STRICT_TOKENS", "")).lower() in {"1", "true", "yes"}
    tokens: dict[str, tuple[str, int]] = {}
    for name, level in (("NG_LEVEL3_TOKEN", 3), ("NG_LEVEL1_TOKEN", 1)):
        val = (env.get(name) or "").strip()
        if strict and (not val or val in _INSECURE_DEFAULTS):
            raise RuntimeError(
                f"{name} 未注入或不安全默认值，严格模式拒绝启动"
                f"（NG_ENV=production / NG_STRICT_TOKENS=1 时强制注入强随机 token）")
        if not val:
            val = "l3-test-token" if level == 3 else "l1-agent-token"
        tokens[val] = ("admin" if level == 3 else "agent", level)
    return tokens


TOKENS = resolve_tokens(dict(os.environ))


# 测试模式（2026-09-02）：NG_DEMO_TOKEN 注入后加入静态 token 表，供测试 agent 免注册直通。
# 2026-09-03 升级：NG_DEMO_MODE=1 时，任意 `demo-` 前缀 token 动态放行并派生独立 user_id，
# 每个试用者用不同 token → 各自隔离（复用多用户隔离逻辑），不再共享全见。
_DEMO_TOKEN = os.environ.get("NG_DEMO_TOKEN", "").strip()
_DEMO_MODE = os.environ.get("NG_DEMO_MODE", "").strip().lower() in {"1", "true", "yes"}
if _DEMO_TOKEN and _DEMO_TOKEN not in TOKENS:
    TOKENS[_DEMO_TOKEN] = ("demo-admin", 3)

# 多用户（2026-09-01）：注册用户会话 token 走 UserStore（main.py 启动时注入）。
# 静态 token 契约不变——先查会话 token，未命中再查静态注册表（服务器端管理员/agent 通道）。
user_store = None


def set_user_store(store) -> None:
    global user_store
    user_store = store


def _demo_identity(token: str) -> tuple[str, int, str]:
    """demo 模式动态 token → (user, level, user_id)。user_id 由 token 派生（确定性）。"""
    import hashlib
    h = hashlib.sha256(token.encode()).hexdigest()
    uid = f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
    return "demo-" + token[:8], 3, uid


def require_auth(authorization: str = Header(default="")):
    """FastAPI 依赖：校验 Bearer token，返回 {user, level, user_id}；失败 401。

    - 注册用户会话 token → UserStore 解析出 {user_id, username, level}
    - 静态 token（NG_LEVEL3/NG_LEVEL1）→ 服务器端通道，user_id=None
    - demo 模式（NG_DEMO_MODE=1）：`demo-` 前缀 token 动态放行，派生独立 user_id
      → 每个试用者一个 token 即一个隔离用户（各看各的），非共享全见。
    """
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "未认证或 token 无效")
    if user_store is not None:
        sess = user_store.resolve_token(token)
        if sess is not None:
            return {"user": sess["username"], "level": sess["level"],
                    "user_id": sess["user_id"]}
    if _DEMO_MODE and token.startswith("demo-"):
        user, level, uid = _demo_identity(token)
        return {"user": user, "level": level, "user_id": uid}
    if token in TOKENS:
        user, level = TOKENS[token]
        return {"user": user, "level": level, "user_id": None}
    raise HTTPException(401, "未认证或 token 无效")
