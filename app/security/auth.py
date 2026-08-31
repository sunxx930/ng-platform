"""真实身份鉴权（Fix 4 生产缺口）—— Bearer token → 身份/权限级别。

骨架用 token→(user,level) 注册表（环境变量可覆盖）；生产接真实 IdP/JWT。

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


def require_auth(authorization: str = Header(default="")):
    """FastAPI 依赖：校验 Bearer token，返回 {user, level}；失败 401。"""
    token = authorization.removeprefix("Bearer ").strip()
    if not token or token not in TOKENS:
        raise HTTPException(401, "未认证或 token 无效")
    user, level = TOKENS[token]
    return {"user": user, "level": level}
