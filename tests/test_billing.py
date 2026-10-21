"""税务版收费模式：状态接口 / 收款码 / 未授权补丁不重复下载。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.storage.event_log import EventLog
from app.storage.user_store import UserStore


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("app.main.log", EventLog(path=tmp_path / "events.jsonl"))
    from app.security import auth as authmod
    store = UserStore(path=tmp_path / "users.json")
    monkeypatch.setattr("app.main.user_store", store)
    authmod.set_user_store(store)
    monkeypatch.setenv("NG_HOME", str(tmp_path))
    return tmp_path


def test_billing_status_shape(env):
    r = TestClient(app).get("/billing/status")
    assert r.status_code == 200
    d = r.json()
    assert d["state"] == "inactive"
    assert d["machine_id"]
    assert d["plans"][0]["price"]          # 价格可配（默认 1 元/天）
    assert d["plans"][0]["period"] == "天"
    assert d["purchase_steps"]
    assert d["tax_usable"] is False


def test_qr_missing_then_present(env, monkeypatch):
    # 仓库 data/pay_qr.png 已配置，找不到时是兜底路径之一；这里先把查找置空模拟「未配置」
    from app.services import billing
    real_lookup = billing.qr_path
    monkeypatch.setattr(billing, "qr_path", lambda: None)
    c = TestClient(app)
    assert c.get("/billing/qr").status_code == 404
    monkeypatch.setattr(billing, "qr_path", real_lookup)
    (env / "data").mkdir()
    (env / "data" / "pay_qr.png").write_bytes(b"\x89PNG fake")
    r = c.get("/billing/qr")
    assert r.status_code == 200 and r.content.startswith(b"\x89PNG")


def test_saved_patch_skips_download(env, monkeypatch):
    """未授权时补丁已存 patches/，下次直接装本地，不再重下几百 MB。"""
    from app.services import updater

    kd = env / "knowledge"
    (kd / "patches").mkdir(parents=True)
    (kd / "patches" / "regs-1.enc").write_bytes(b"enc")

    called = []
    monkeypatch.setattr(updater, "_download", lambda *a, **k: called.append(1))
    monkeypatch.setattr(updater, "install_patch_file",
                        lambda src, kd2: {"applied": False, "version": src.stem,
                                          "reason": "待激活授权后解密"})
    res = updater._apply_one_pack(
        kd, {"url": "https://example.invalid/kb-regs-1.pack", "version": "regs-1"}, "regs")
    assert called == []
    assert res["version"] == "regs-1"
