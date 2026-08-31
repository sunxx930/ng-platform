"""多用户回归（2026-09-01）：注册/登录门 + 认证 user_id + 项目按用户隔离 + 事件 user 维度。

运行: cd ~/Desktop/ng-platform && .venv/bin/python -m pytest tests/ -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.storage.event_log import EventLog
from app.storage.user_store import UserStore


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 隔离：每个测试用独立事件文件 + 独立用户存储（dev JSONL 兜底）
    from app.main import log as real_log
    from app.security import auth as authmod
    monkeypatch.setattr("app.main.log", EventLog(path=tmp_path / "events.jsonl"))
    store = UserStore(path=tmp_path / "users_store.json")
    monkeypatch.setattr("app.main.user_store", store)
    authmod.set_user_store(store)   # 注入到 require_auth
    c = TestClient(app)
    yield c


def _register(c, username="alice", password="secret123"):
    return c.post("/auth/register",
                  json={"username": username, "password": password})


def _login(c, username="alice", password="secret123"):
    return c.post("/auth/login",
                  json={"username": username, "password": password})


def _h(token):
    return {"Authorization": f"Bearer {token}"}


def test_register_returns_token_and_me(client):
    """注册成功 → token 可用；/auth/me 返回 user_id/username/level。"""
    r = _register(client)
    assert r.status_code == 200
    d = r.json()
    assert d["token"] and d["user_id"] and d["username"] == "alice"
    assert d["level"] == 1
    me = client.get("/auth/me", headers=_h(d["token"])).json()
    assert me["user_id"] == d["user_id"]
    assert me["username"] == "alice"
    assert me["level"] == 1


def test_register_duplicate_409(client):
    """重复用户名注册 → 409。"""
    assert _register(client).status_code == 200
    assert _register(client, username="alice").status_code == 409


def test_register_validation(client):
    """用户名空 / 密码过短 → 400。"""
    assert _register(client, username="  ").status_code == 400
    assert _register(client, password="short").status_code == 400


def test_login_wrong_password_401(client):
    """登录错密码 → 401。"""
    _register(client)
    r = _login(client, password="wrong-pass")
    assert r.status_code == 401


def test_login_unknown_user_401(client):
    assert _login(client, username="nobody").status_code == 401


def test_login_ok(client):
    """登录成功 → 新 token 可用。"""
    _register(client)
    r = _login(client)
    assert r.status_code == 200
    assert r.json()["username"] == "alice"
    assert client.get("/auth/me", headers=_h(r.json()["token"])).status_code == 200


def test_no_token_401(client):
    """无/坏 token → 401（回归）。"""
    assert client.get("/projects").status_code == 401
    assert client.get("/projects", headers=_h("bad-token")).status_code == 401


def test_project_isolation_by_user(client):
    """项目隔离：A 建的项目 B 看不到；L3 静态 token 见全部。"""
    a = _register(client, username="alice").json()
    b = _register(client, username="bob").json()
    ha, hb = _h(a["token"]), _h(b["token"])
    pid = client.post("/projects", headers=ha,
                      params={"title": "A 的项目", "goal": "g"}).json()["project_id"]
    # B 看不到 A 的项目
    b_projects = client.get("/projects", headers=hb).json()["projects"]
    assert all(p["project_id"] != pid for p in b_projects)
    # A 看得到自己的
    a_projects = client.get("/projects", headers=ha).json()["projects"]
    assert any(p["project_id"] == pid for p in a_projects)
    # L3 静态 token（服务器端管理员通道，user_id=None）见全部
    h3 = {"Authorization": "Bearer l3-test-token"}
    all_projects = client.get("/projects", headers=h3).json()["projects"]
    assert any(p["project_id"] == pid for p in all_projects)


def test_project_created_event_has_user_id(client):
    """事件 user 维度：project.created 带 user_id = 注册用户。"""
    d = _register(client).json()
    pid = client.post("/projects", headers=_h(d["token"]),
                      params={"title": "T", "goal": "g"}).json()["project_id"]
    audit = client.get(f"/projects/{pid}/audit", headers=_h(d["token"])).json()["events"]
    created = next(e for e in audit if e["event_type"] == "project.created")
    assert created.get("user_id") == d["user_id"]


def test_logout_revokes_token(client):
    """登出 → 旧 token 失效 401。"""
    d = _register(client).json()
    assert client.get("/projects", headers=_h(d["token"])).status_code == 200
    assert client.post("/auth/logout", headers=_h(d["token"])).status_code == 200
    assert client.get("/projects", headers=_h(d["token"])).status_code == 401
