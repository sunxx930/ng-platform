#!/usr/bin/env python3
"""构建「知识包」——税务版的内容分发单元（走热更，不重装）。

两个独立补丁（用户 2026-09-27 定）：
    cases  案例库：tax-cases/*.md + index/ + models/embed|rerank
    regs   法规库：tax-regs/**/*.md + index/（不重复带模型）

包格式 v2（2026-10-21 起，用户「只更新受影响的部分」）：
    不再整包重发，而是**按源文件分片**。每个分片 = 一份 .md 的全部条目
    （index/shards/<id>.npz，由 kb_build_index.py 产出）。
    完整包带全部分片；增量包（--base）只带与基线相比变了的分片 + 删除清单。

用法:
  # 完整包（首次发布 / 客户错过多个版本时的兜底）
  python3 scripts/build_kbpack.py --kind regs --src /tmp/regs-stage \
      --version regs-2026-11-02 --out ~/Desktop/ng-pack --manifest ~/Desktop/ng-pack/update.json

  # 增量包（只含变动分片；基线包必须是已发布的完整包）
  python3 scripts/build_kbpack.py --kind regs --src /tmp/regs-stage \
      --version regs-2026-11-02 --base ~/Desktop/ng-pack/kb-regs-2026-10-21.pack \
      --out ~/Desktop/ng-pack --manifest ~/Desktop/ng-pack/update.json

输出：<out>/kb-<version>.pack（完整）/ <out>/kb-<version>-delta.pack（增量）+ sha256/大小。
manifest 中增量包会带 base（基线版本）与 full（完整包）两段，客户端据此选包。
"""
import argparse
import hashlib
import json
import os
import zipfile
from pathlib import Path

KB_SRC = Path.home() / ".ng-platform" / "knowledge"
_CONTENT_ROOT = {"regs": "tax-regs", "cases": "tax-cases"}


def _pack_key() -> bytes | None:
    pkey = KB_SRC.parent / "_pack.key"
    if not pkey.is_file():
        pkey = Path.home() / ".secrets" / "ng-license" / "pack.key"
    return pkey.read_bytes()[:32] if pkey.is_file() else None


def _decrypt(blob: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    k = _pack_key()
    if not k:
        raise SystemExit("✗ 基线包是加密的，但找不到 pack.key")
    return AESGCM(k).decrypt(blob[:12], blob[12:], None)


def _read_pack_info(path: Path) -> dict:
    """读基线包的 pack.json（明文或加密都行）。"""
    blob = path.read_bytes()
    try:
        inner = blob
        with zipfile.ZipFile(_io(inner)) as zf:
            return json.loads(zf.read("pack.json"))
    except Exception:
        with zipfile.ZipFile(_io(_decrypt(blob))) as zf:
            return json.loads(zf.read("pack.json"))


def _io(b: bytes):
    import io
    return io.BytesIO(b)


def _shards(src: Path) -> list[dict]:
    import numpy as np
    d = src / "index" / "shards"
    if not d.is_dir():
        raise SystemExit(f"✗ {d} 不存在（先跑 kb_build_index.py 生成分片）")
    out = []
    for p in sorted(d.glob("*.npz")):
        z = np.load(p)
        out.append({"id": p.stem, "hash": str(z["hash"]), "path": str(z["path"]),
                    "chunks": int(z["vec"].shape[0])})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True, help="包版本号（变了才会触发客户端更新）")
    ap.add_argument("--kind", choices=("cases", "regs"), default="cases",
                    help="cases=案例库补丁；regs=法规库补丁")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--src", default=str(KB_SRC), help="知识库源目录（默认 ~/.ng-platform/knowledge）")
    ap.add_argument("--base", default="", help="基线完整包：给了就只打变动分片（增量包）")
    ap.add_argument("--manifest", default="", help="顺带更新的 update.json 路径")
    ap.add_argument("--url-base", default="https://ng-platform.ai/downloads",
                    help="包的公网地址前缀")
    a = ap.parse_args()

    src, out = Path(a.src), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    content_root = _CONTENT_ROOT[a.kind]
    all_shards = _shards(src)
    if not all_shards:
        raise SystemExit(f"✗ {src}/index/shards 下没有分片")

    base_info: dict = {}
    if a.base:
        bp = Path(a.base)
        if not bp.is_file():
            raise SystemExit(f"✗ 基线包不存在: {bp}")
        base_info = _read_pack_info(bp)
        if base_info.get("format") != 2:
            raise SystemExit("✗ 基线包是旧格式（format 1），没有分片信息；先发一版完整 v2 包")
        if base_info.get("kind") != a.kind:
            raise SystemExit(f"✗ 基线包是 {base_info.get('kind')}，与 --kind {a.kind} 不符")

    base_hash = {s["id"]: s["hash"] for s in (base_info.get("shards") or [])}
    new_ids = {s["id"] for s in all_shards}
    if base_info:
        selected = [s for s in all_shards if base_hash.get(s["id"]) != s["hash"]]
        removed = sorted(set(base_hash) - new_ids)
    else:
        selected = all_shards
        removed = []

    pack = out / (f"kb-{a.version}-delta.pack" if base_info else f"kb-{a.version}.pack")
    with zipfile.ZipFile(pack, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr("pack.json", json.dumps({
            "format": 2,
            "kind": a.kind,
            "version": a.version,
            "base": base_info.get("version", "") if base_info else "",
            "shards": selected,
            "removed": removed,
            "total_after": len(all_shards),      # 装完后客户端应有这么多分片（防内容目录残缺）
            "models": a.kind == "cases" and not base_info,
        }, ensure_ascii=False))
        for s in selected:
            zf.write(src / "index" / "shards" / f"{s['id']}.npz",
                     f"shards/{s['id']}/index.npz")
            f = src / content_root / s["path"]
            if f.is_file():                      # 分片在索引里、原文不在 → 只靠索引也能检索
                zf.write(f, f"shards/{s['id']}/content/{content_root}/{s['path']}")
        if a.kind == "cases" and not base_info:
            d = src / "models"
            if d.is_dir():
                for f in sorted(d.rglob("*")):
                    if f.is_file():
                        zf.write(f, f"models/{f.relative_to(d).as_posix()}")

    # 加密：包可以公开托管，没有授权里的密钥就是一堆乱码
    k = _pack_key()
    if k:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = os.urandom(12)
        pack.write_bytes(nonce + AESGCM(k).encrypt(nonce, pack.read_bytes(), None))
        print("  ✓ 已加密")
    else:
        print("  ⚠ 未加密——找不到 pack.key，包将是明文")

    raw = pack.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    size = len(raw)
    url = f"{a.url_base}/{pack.name}"

    print(f"\n  ✓ {'增量包' if base_info else '完整包'}: {pack}")
    print(f"      分片 {len(selected)}/{len(all_shards)}"
          + (f"，删除 {len(removed)}" if removed else "")
          + f"，条目 {sum(s['chunks'] for s in selected)}")
    print(f"  size    {size/1e6:.1f} MB")
    print(f"  sha256  {sha}")
    print(f"  url     {url}")

    if a.manifest:
        mf = Path(a.manifest)
        m = json.loads(mf.read_text(encoding="utf-8")) if mf.is_file() else {}
        if a.kind != "regs":        # regs 包版本 ≠ App 版本，别把它写进顶层 version
            m.setdefault("version", a.version)
        key = "knowledge_regs" if a.kind == "regs" else "knowledge"
        if base_info:
            # 增量包：顶层指向增量；full 留完整包（首次安装/错过版本/内容目录残缺时用）
            full = m.get(key) or {}
            if full.get("version") != a.version or not full.get("url"):
                print(f"  ⚠ manifest 里没有 {a.version} 的完整包记录——先打完整包再打增量包")
                full = {"url": f"{a.url_base}/kb-{a.version}.pack"}
            full = {k2: full[k2] for k2 in ("url", "sha256", "size") if k2 in full}
            m[key] = {"version": a.version, "url": url, "sha256": sha, "size": size,
                      "base": base_info["version"], "full": full}
        else:
            m[key] = {"version": a.version, "url": url, "sha256": sha, "size": size}
        mf.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  ✓ 已更新 manifest 的 {key} 段: {mf}")

    print("\n  下一步：把 .pack 传到 url 指向的位置，再把 update.json 发到官网。")


if __name__ == "__main__":
    main()
