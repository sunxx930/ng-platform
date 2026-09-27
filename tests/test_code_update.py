"""代码补丁：暂存/验签/兼容/防重放/穿越防护 的测试。

用**临时生成的密钥对**（monkeypatch ng_crypto.PUBLIC_KEY_HEX），不依赖发行方私钥。
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

import ng_boot
import ng_crypto
from app.services import code_update


@pytest.fixture
def keypair(monkeypatch, tmp_path_factory):
    """生成测试密钥对，把公钥换成测试公钥，返回**私钥文件路径**（sign_blob 收路径）。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    monkeypatch.setattr(ng_crypto, "PUBLIC_KEY_HEX", pub_hex)
    pem_path = tmp_path_factory.mktemp("keys") / "test-private.pem"
    pem_path.write_bytes(priv.private_bytes(serialization.Encoding.PEM,
                                            serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()))
    return pem_path


def _make_payload(tmp: Path, files: dict[str, bytes] | None = None) -> Path:
    """造一个结构合法的载荷 zip（含 payload.json 清单）。"""
    files = files or {
        "app/__init__.py": b"",
        "app/version.py": b'VERSION = "1.3.1"\n',
    }
    manifest = {rel: hashlib.sha256(raw).hexdigest() for rel, raw in files.items()}
    z = tmp / "code-1.3.1.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, raw in files.items():
            zf.writestr(rel, raw)
        zf.writestr("payload.json", json.dumps({"version": "1.3.1", "files": manifest}))
    return z


def _manifest_for(tmp: Path, z: Path, pem: Path, *, seq=100, min_shell="1.3.0",
                  payload_api=1, sig_override=None, **patch) -> Path:
    blob = z.read_bytes()
    sha = hashlib.sha256(blob).hexdigest()
    desc = ng_crypto.code_descriptor(version="1.3.1", min_shell=min_shell,
                                     payload_api=payload_api, seq=seq,
                                     sha256=sha, size=len(blob))
    sig = sig_override if sig_override is not None else ng_crypto.sign_blob(desc, str(pem))
    code = {"version": "1.3.1", "min_shell": min_shell, "payload_api": payload_api,
            "seq": seq, "url": z.as_uri(), "sha256": sha, "size": len(blob), "sig": sig}
    code.update(patch)
    mf = tmp / "update.json"
    mf.write_text(json.dumps({"version": "1.3.1", "files": [], "code": code}), encoding="utf-8")
    return mf


@pytest.fixture
def env(tmp_path, monkeypatch, keypair):
    """隔离的 NG_HOME + 测试外壳（shell.json）+ manifest 指向本地文件。"""
    monkeypatch.setenv("NG_HOME", str(tmp_path / "home"))
    shell = tmp_path / "shell"
    shell.mkdir()
    (shell / "shell.json").write_text(
        json.dumps({"shell_version": "1.3.0", "payload_api": 1}), encoding="utf-8")
    monkeypatch.setattr(code_update, "_bundle_root", lambda: shell)
    return tmp_path


def test_stage_happy_path(env, monkeypatch, keypair, tmp_path):
    z = _make_payload(tmp_path)
    mf = _manifest_for(tmp_path, z, keypair)
    monkeypatch.setenv("NG_UPDATE_MANIFEST", mf.as_uri())

    res = code_update.stage()
    assert res["staged"] is True, res
    assert res["restart_required"] is True
    # 只暂存：current 不动，pending 写入
    code = ng_boot.code_dir()
    assert json.loads((code / "pending.json").read_text())["version"] == "1.3.1"
    assert not (code / "current.json").exists()
    assert (code / "versions" / "1.3.1" / "app" / "__init__.py").is_file()


def test_rejects_bad_signature(env, monkeypatch, keypair, tmp_path):
    z = _make_payload(tmp_path)
    mf = _manifest_for(tmp_path, z, keypair, sig_override="AAAA" * 20)
    monkeypatch.setenv("NG_UPDATE_MANIFEST", mf.as_uri())
    res = code_update.stage()
    assert res["staged"] is False and "签名" in res["reason"]
    assert not (ng_boot.code_dir() / "pending.json").exists()


@pytest.mark.parametrize("field,value", [
    ("version", "9.9.9"), ("seq", 999), ("sha256", "b" * 64), ("min_shell", "0.0.1"),
])
def test_tampering_any_signed_field_breaks_signature(env, monkeypatch, keypair, tmp_path,
                                                     field, value):
    """改任一字 段（版本/顺序/哈希/兼容）→ 签名失效 → 拒绝。"""
    z = _make_payload(tmp_path)
    honest = _manifest_for(tmp_path, z, keypair)
    honest_code = json.loads(honest.read_text())["code"]
    tampered = dict(honest_code)
    tampered[field] = value
    mf = tmp_path / "tampered.json"
    mf.write_text(json.dumps({"version": "1.3.1", "files": [], "code": tampered}),
                  encoding="utf-8")
    monkeypatch.setenv("NG_UPDATE_MANIFEST", mf.as_uri())
    res = code_update.stage()
    assert res["staged"] is False, f"{field} 被篡改却没被拒"


def test_rejects_min_shell_too_high(env, monkeypatch, keypair, tmp_path):
    """载荷要求更高的外壳 → 明确拒绝（不半途应用）。"""
    z = _make_payload(tmp_path)
    mf = _manifest_for(tmp_path, z, keypair, min_shell="9.9.9")
    monkeypatch.setenv("NG_UPDATE_MANIFEST", mf.as_uri())
    res = code_update.stage()
    assert res["staged"] is False and "安装包" in res["reason"]


def test_rejects_payload_api_too_high(env, monkeypatch, keypair, tmp_path):
    z = _make_payload(tmp_path)
    mf = _manifest_for(tmp_path, z, keypair, payload_api=99)
    monkeypatch.setenv("NG_UPDATE_MANIFEST", mf.as_uri())
    assert code_update.stage()["staged"] is False


def test_rejects_seq_replay(env, monkeypatch, keypair, tmp_path):
    """顺序号不递增 → 拒绝（防回放/降级）。"""
    z = _make_payload(tmp_path)
    ng_boot.code_dir().mkdir(parents=True, exist_ok=True)
    (ng_boot.code_dir() / ".highest_seq").write_text("500", encoding="utf-8")
    mf = _manifest_for(tmp_path, z, keypair, seq=100)
    monkeypatch.setenv("NG_UPDATE_MANIFEST", mf.as_uri())
    res = code_update.stage()
    assert res["staged"] is False and "顺序号" in res["reason"]


def test_rejects_traversal_in_payload(env, monkeypatch, keypair, tmp_path):
    """payload.json 里塞 ../ → 拒绝。"""
    files = {"app/__init__.py": b"", "../escape.py": b"bad"}
    z = _make_payload(tmp_path, files)
    mf = _manifest_for(tmp_path, z, keypair)
    monkeypatch.setenv("NG_UPDATE_MANIFEST", mf.as_uri())
    res = code_update.stage()
    assert res["staged"] is False
    assert not (tmp_path / "escape.py").exists()
    assert not (ng_boot.code_dir().parent / "escape.py").exists()


def test_rejects_file_hash_mismatch(env, monkeypatch, keypair, tmp_path):
    """zip 里文件与 payload.json 清单对不上 → 拒绝。"""
    files = {"app/__init__.py": b"", "app/x.py": b"original"}
    manifest = {rel: hashlib.sha256(raw).hexdigest() for rel, raw in files.items()}
    z = tmp_path / "code-1.3.1.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("app/__init__.py", b"")
        zf.writestr("app/x.py", b"TAMPERED")          # 与清单不符
        zf.writestr("payload.json", json.dumps({"version": "1.3.1", "files": manifest}))
    mf = _manifest_for(tmp_path, z, keypair)
    monkeypatch.setenv("NG_UPDATE_MANIFEST", mf.as_uri())
    res = code_update.stage()
    assert res["staged"] is False and "校验失败" in res["reason"]
    assert not (ng_boot.code_dir() / "pending.json").exists()
