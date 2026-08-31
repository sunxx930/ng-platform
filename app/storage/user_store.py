"""用户账号存储 —— 多用户（2026-09-01）。

镜像 EventLog 双模式：有 DATABASE_URL 走 PostgreSQL（users/auth_tokens 表），
否则落本地 JSON 文件兜底（dev 无 DB 可跑）。密码 scrypt（stdlib），token 存 sha256 哈希。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine


class UsernameConflict(Exception):
    """用户名已存在 → API 层映射 409。"""


class InvalidCredentials(Exception):
    """用户名或密码错误 → API 层映射 401。"""


def _scrypt_hash(password: str, salt: bytes | None = None) -> str:
    """scrypt 哈希：返回 "scrypt$salt_hex$hash_hex"（salt 随机 16B）。"""
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${dk.hex()}"


def _verify_scrypt(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(),
                            salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
        return secrets.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class UserStore:
    """用户 + 会话 token 存储。DB 引擎可用则用表，否则 JSON 文件兜底。"""

    def __init__(self, engine: Engine | None = None,
                 path: Path | None = None):
        self._engine = engine
        self._path = path or Path("data/users_store.json")

    # ---------- 密码 ----------
    @staticmethod
    def hash_password(password: str) -> str:
        return _scrypt_hash(password)

    @staticmethod
    def verify_password(password: str, password_hash: str) -> bool:
        return _verify_scrypt(password, password_hash)

    # ---------- 用户 ----------
    def create_user(self, username: str, password: str, level: int = 1) -> dict:
        uid = str(uuid.uuid4())
        user = {
            "id": uid, "username": username, "level": level,
            "password_hash": self.hash_password(password),
            "created_at": _now_iso(),
        }
        if self._engine is not None:
            self._create_user_db(user)
        else:
            self._create_user_json(user)
        return {"id": uid, "username": username, "level": level}

    def get_user_by_username(self, username: str) -> dict | None:
        if self._engine is not None:
            return self._get_user_db(username=username)
        return self._get_user_json(username=username)

    def get_user(self, user_id: str) -> dict | None:
        if self._engine is not None:
            return self._get_user_db(user_id=user_id)
        return self._get_user_json(user_id=user_id)

    # ---------- token ----------
    def issue_token(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        thash = _hash_token(token)
        if self._engine is not None:
            with self._engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO auth_tokens (token_hash, user_id) VALUES (:h, :uid)"),
                    {"h": thash, "uid": user_id})
        else:
            self._save_tokens_json(lambda toks: toks.update({thash: {
                "user_id": user_id, "revoked": False,
                "created_at": _now_iso()}}))
        return token

    def resolve_token(self, token: str) -> dict | None:
        """token → {user_id, username, level}；无效/吊销/无用户 → None。"""
        thash = _hash_token(token)
        if self._engine is not None:
            row = None
            with self._engine.connect() as conn:
                row = conn.execute(text(
                    """SELECT t.user_id, u.username, u.level
                       FROM auth_tokens t JOIN users u ON u.id=t.user_id
                       WHERE t.token_hash=:h AND t.revoked=FALSE"""),
                    {"h": thash}).mappings().first()
            if row is None:
                return None
            return {"user_id": str(row["user_id"]),
                    "username": row["username"], "level": int(row["level"])}
        toks = self._load_tokens_json()
        rec = toks.get(thash)
        if rec is None or rec.get("revoked"):
            return None
        user = self.get_user(rec["user_id"])
        if user is None:
            return None
        return {"user_id": user["id"], "username": user["username"],
                "level": int(user["level"])}

    def revoke_token(self, token: str) -> None:
        thash = _hash_token(token)
        if self._engine is not None:
            with self._engine.begin() as conn:
                conn.execute(text(
                    "UPDATE auth_tokens SET revoked=TRUE WHERE token_hash=:h"),
                    {"h": thash})
        else:
            self._save_tokens_json(lambda toks: toks.update({thash: {
                "user_id": "", "revoked": True, "created_at": _now_iso()}}))

    # ---------- DB 路径 ----------
    def _create_user_db(self, user: dict) -> None:
        try:
            with self._engine.begin() as conn:
                conn.execute(text(
                    """INSERT INTO users (id, username, password_hash, level)
                       VALUES (:id, :username, :password_hash, :level)"""), user)
        except Exception as e:
            if self._is_duplicate(e):
                raise UsernameConflict(f"用户名已存在: {user['username']}") from e
            raise

    def _get_user_db(self, username: str | None = None,
                     user_id: str | None = None) -> dict | None:
        q = "SELECT id, username, password_hash, level FROM users WHERE 1=1"
        params = {}
        if username is not None:
            q += " AND username=:username"; params["username"] = username
        if user_id is not None:
            q += " AND id=:id"; params["id"] = user_id
        with self._engine.connect() as conn:
            row = conn.execute(text(q), params).mappings().first()
        if row is None:
            return None
        return {"id": str(row["id"]), "username": row["username"],
                "password_hash": row["password_hash"], "level": int(row["level"])}

    @staticmethod
    def _is_duplicate(e: Exception) -> bool:
        code = getattr(getattr(e, "orig", None), "sqlstate", None)
        return code == "23505" or "UniqueViolation" in str(e)

    # ---------- JSON 兜底路径 ----------
    def _load_users_json(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_users_json(self, users: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(users, ensure_ascii=False, indent=2),
                              encoding="utf-8")

    def _create_user_json(self, user: dict) -> None:
        users = self._load_users_json()
        if any(u.get("username") == user["username"] for u in users.values()):
            raise UsernameConflict(f"用户名已存在: {user['username']}")
        users[user["id"]] = user
        self._save_users_json(users)

    def _get_user_json(self, username: str | None = None,
                       user_id: str | None = None) -> dict | None:
        for u in self._load_users_json().values():
            if username is not None and u.get("username") == username:
                return u
            if user_id is not None and u.get("id") == user_id:
                return u
        return None

    def _tokens_path(self) -> Path:
        return self._path.with_name(self._path.stem + "_tokens.json")

    def _load_tokens_json(self) -> dict:
        p = self._tokens_path()
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_tokens_json(self, mutate) -> None:
        p = self._tokens_path()
        toks = self._load_tokens_json()
        mutate(toks)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(toks, ensure_ascii=False), encoding="utf-8")


def _now_iso() -> str:
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
