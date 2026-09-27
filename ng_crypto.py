"""通用 Ed25519 签名/验签（**顶层模块，绝不 import app.***）。

为什么单独一个顶层文件：
    代码补丁的启动器（ng_boot）要在 `import app.main` **之前**验签，那时不能
    碰 app 包——一旦 `import app.anything`，打包的 app 就进了 sys.modules，
    后续子模块会走 app.__path__（=_MEIPASS/app），**sys.path 覆盖直接失效**。
    所以加密逻辑必须住在 app 外面。

密钥与授权同源（发行方私钥 ~/.secrets/ng-license/license-private.pem，只在发行方手里），
但本模块的 verify_blob 是**通用的**——签什么由调用方决定（授权串、代码补丁描述符…）。
"""
from __future__ import annotations

import base64

# 发行方公钥（与 app/services/license.py 原本的那份是同一个，此处为唯一真源）
PUBLIC_KEY_HEX = "d98e798c7b9b80520f3f5ace492b83e95a05b725dcff79e08a858e7a11a3758a"


def _b64d(s: str) -> bytes:
    """无填充 base64url → bytes（容忍粘贴时丢掉的 '='）。"""
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64e(x: bytes) -> str:
    return base64.urlsafe_b64encode(x).decode().rstrip("=")


def verify_blob(data: bytes, sig_b64url: str, pubkey_hex: str | None = None) -> bool:
    """验签。任何异常一律返回 False（不抛）——调用方按 fail-closed 处理。"""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex or PUBLIC_KEY_HEX))
        pk.verify(_b64d(sig_b64url), data)
        return True
    except Exception:      # noqa: BLE001
        return False


def sign_blob(data: bytes, privkey_path: str) -> str:
    """签名，返回 base64url（无填充）。**只在发行方脚本里用**，客户端不调用。"""
    from cryptography.hazmat.primitives import serialization
    with open(privkey_path, "rb") as fh:
        key = serialization.load_pem_private_key(fh.read(), password=None)
    return _b64e(key.sign(data))


def code_descriptor(*, version: str, min_shell: str, payload_api: int,
                    seq: int, sha256: str, size: int) -> bytes:
    """代码补丁的**规范化描述符**——这是被签名的字节串。

    为什么签描述符而不是只签 sha256：
      只签哈希的话，攻击者能拿一个**旧的合法包**配上**新的版本号/顺序号**，
      实现静默降级或回放。把版本、顺序、兼容性、哈希绑在一起签，
      改任何一个字段签名都失效。

    格式固定为 `NGCODEv1` 开头 + 每行 k=v，行序固定（跨平台可复现）。
    """
    return (
        "NGCODEv1\n"
        f"version={version}\n"
        f"min_shell={min_shell}\n"
        f"payload_api={int(payload_api)}\n"
        f"seq={int(seq)}\n"
        f"sha256={sha256.lower()}\n"
        f"size={int(size)}\n"
    ).encode("utf-8")
