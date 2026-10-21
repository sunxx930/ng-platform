"""知识包 v2 增量更新：分片落盘 / 只装变动分片 / 删除清单 / 基线不对回退完整包。

用合成分片（不跑 ONNX）：只验证安装与合并逻辑。
"""
import hashlib
import io
import json
import os
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.services import kb_semantic, updater
from app.services.license import pack_key


def _npz(path: str, texts: list[str]) -> bytes:
    V = np.asarray([[float(i + 1), 0.0, 0.0, 1.0] for i, _ in enumerate(texts)], dtype=np.float16)
    meta = [{"file": path, "title": t, "type": "法规", "topic": "", "snippet": t} for t in texts]
    mj = "\n".join(json.dumps(x, ensure_ascii=False) for x in meta)
    buf = io.BytesIO()
    np.savez_compressed(buf, vec=V, meta=np.frombuffer(mj.encode("utf-8"), dtype=np.uint8),
                        path=np.array(path),
                        hash=np.array(hashlib.sha256(V.tobytes() + mj.encode()).hexdigest()))
    return buf.getvalue()


def _pack(path: Path, version: str, shards: dict[str, list[str]], *, removed=(),
          base="", kind="regs", total_after=None) -> Path:
    """写一个加密的 v2 包：shards = {分片id: [正文…]}。"""
    ids = sorted(shards)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("pack.json", json.dumps({
            "format": 2, "kind": kind, "version": version, "base": base,
            "shards": [{"id": s, "hash": s, "path": f"{s}.md", "chunks": len(shards[s])} for s in ids],
            "removed": list(removed),
            "total_after": len(ids) if total_after is None else total_after, "models": False,
        }))
        for s in ids:
            zf.writestr(f"shards/{s}/index.npz", _npz(f"{s}.md", shards[s]))
    k = pack_key()
    nonce = os.urandom(12)
    path.write_bytes(nonce + AESGCM(k).encrypt(nonce, path.read_bytes(), None))
    return path


def _entry(p: Path, version: str, **extra) -> dict:
    return {"version": version, "url": str(p),
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(), **extra}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NG_HOME", str(tmp_path))
    kb_semantic.reset_index_cache()
    return tmp_path


def test_full_install_then_delta_touches_one_shard(env):
    kb = env / "knowledge"
    a = _pack(env / "v1.pack", "v1", {"r-0000000000000001": ["第一条 甲"],
                                      "r-0000000000000002": ["第二条 乙"]})
    res = updater._install_blob(a.read_bytes(), kb, "v1")
    assert res["applied"] and res["installed_total"] == 2
    sd = kb / "content" / "shards"
    before = {p.name: p.read_bytes() for p in sd.glob("*.enc")}

    delta = _pack(env / "v2.pack", "v2", {"r-0000000000000002": ["第二条 乙改"]},
                  base="v1")
    res = updater._install_blob(delta.read_bytes(), kb, "v2")
    assert res["shards"] == 1 and res["installed_total"] == 2
    after = {p.name: p.read_bytes() for p in sd.glob("*.enc")}
    assert after["r-0000000000000001.enc"] == before["r-0000000000000001.enc"]
    assert after["r-0000000000000002.enc"] != before["r-0000000000000002.enc"]

    V, metas = kb_semantic.load_index()
    assert V.shape[0] == 2
    assert {m["snippet"] for m in metas} == {"第一条 甲", "第二条 乙改"}


def test_delta_removes_deleted_shard(env):
    kb = env / "knowledge"
    a = _pack(env / "v1.pack", "v1", {"r-0000000000000001": ["第一条 甲"],
                                      "r-0000000000000002": ["第二条 乙"]})
    updater._install_blob(a.read_bytes(), kb, "v1")
    d = _pack(env / "v2.pack", "v2", {"r-0000000000000001": ["第一条 甲"]},
              removed=["r-0000000000000002"], base="v1")
    res = updater._install_blob(d.read_bytes(), kb, "v2")
    assert res["installed_total"] == 1
    assert not (kb / "content/shards/r-0000000000000002.enc").exists()
    _, metas = kb_semantic.load_index()
    assert [m["snippet"] for m in metas] == ["第一条 甲"]


def test_merge_dedupes_same_clause_across_shards(env):
    kb = env / "knowledge"
    a = _pack(env / "v1.pack", "v1", {"r-0000000000000001": ["同一句条文"],
                                      "r-0000000000000002": ["同一句条文"]})
    updater._install_blob(a.read_bytes(), kb, "v1")
    _, metas = kb_semantic.load_index()
    assert len(metas) == 1


def test_legacy_v1_pack_dropped_on_sharded_install(env):
    kb = env / "knowledge"
    # 旧格式：content/<版本>.enc，内含 tax-regs/
    legacy = io.BytesIO()
    with zipfile.ZipFile(legacy, "w") as zf:
        zf.writestr("tax-regs/旧/条文.md", "第一条 旧条款")
        zf.writestr("index/meta.jsonl", json.dumps({"snippet": "旧条款"}, ensure_ascii=False))
    k = pack_key()
    (kb / "content").mkdir(parents=True, exist_ok=True)
    nonce = os.urandom(12)
    (kb / "content" / "old.enc").write_bytes(nonce + AESGCM(k).encrypt(nonce, legacy.getvalue(), None))

    a = _pack(env / "v1.pack", "v1", {"r-0000000000000001": ["第一条 甲"]})
    res = updater._install_blob(a.read_bytes(), kb, "v1")
    assert res["legacy_dropped"] == "old.enc"
    assert not (kb / "content" / "old.enc").exists()


def test_apply_picks_full_when_not_on_base(env, monkeypatch):
    kb = env / "knowledge"
    full = _pack(env / "full2.pack", "v2", {"r-0000000000000001": ["第一条 甲"]})
    delta = _pack(env / "delta2.pack", "v2", {"r-0000000000000001": ["第一条 甲"]}, base="v1")
    entry = _entry(delta, "v2", base="v1",
                   full={"url": str(full), "sha256": hashlib.sha256(full.read_bytes()).hexdigest()})

    monkeypatch.setattr(updater, "_download",
                        lambda url, dest, **kw: dest.write_bytes(Path(url).read_bytes()))
    res = updater._apply_one_pack(kb, entry, "regs")      # 全新机器（不在 v1 基线）
    assert res["applied"] and res["version"] == "v2"
    assert res["installed_total"] == 1
    assert (kb / ".installed_regs").read_text() == "v2"


def test_apply_repairs_when_shard_missing(env, monkeypatch):
    kb = env / "knowledge"
    v1 = _pack(env / "v1.pack", "v1", {"r-0000000000000001": ["第一条 甲"],
                                       "r-0000000000000002": ["第二条 乙"]})
    updater._install_blob(v1.read_bytes(), kb, "v1")
    (kb / ".installed_regs").write_text("v1")
    (kb / "content/shards/r-0000000000000001.enc").unlink()   # 内容目录残缺

    full2 = _pack(env / "full2.pack", "v2", {"r-0000000000000001": ["第一条 甲"],
                                             "r-0000000000000002": ["第二条 乙"]})
    delta2 = _pack(env / "delta2.pack", "v2", {}, base="v1", total_after=2)
    entry = _entry(delta2, "v2", base="v1",
                   full={"url": str(full2), "sha256": hashlib.sha256(full2.read_bytes()).hexdigest()})
    monkeypatch.setattr(updater, "_download",
                        lambda url, dest, **kw: dest.write_bytes(Path(url).read_bytes()))
    res = updater._apply_one_pack(kb, entry, "regs")
    assert res.get("repaired") is True
    assert res["installed_total"] == 2
