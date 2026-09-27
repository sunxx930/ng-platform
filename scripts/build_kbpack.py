#!/usr/bin/env python3
"""构建「知识包」——税务版的内容分发单元（走热更，不重装）。

包内容（解压后正好落到 NG_HOME/knowledge/）：
    tax-cases/*.md      条目
    index/              向量 + 元数据
    models/embed|rerank 本地小模型（嵌入 23MB + 重排 279MB）

用法:
  # 只打案例（第一批）
  python3 scripts/build_kbpack.py --version 2026-09-cases --out ~/Desktop/ng-pack

  # 带法规（第二批，届时把法规条目放进 tax-cases/ 后重跑即可）
  python3 scripts/build_kbpack.py --version 2026-10-regs --out ~/Desktop/ng-pack

  # 顺带更新 update.json 的 knowledge 段（供 /update/apply 读取）
  python3 scripts/build_kbpack.py --version ... --out ... --manifest ~/Desktop/ng-pack/update.json

输出：<out>/kb-<version>.pack （zip）+ 打印 sha256/大小，供填入 manifest。
"""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path

KB_SRC = Path.home() / ".ng-platform" / "knowledge"


def _add(zf: zipfile.ZipFile, src: Path, arc_prefix: str) -> int:
    n = 0
    for f in sorted(src.rglob("*")):
        if not f.is_file() or f.name.startswith("."):
            continue
        if any(part.endswith(".bak") for part in f.parts):
            continue
        arc = f"{arc_prefix}/{f.relative_to(src).as_posix()}"
        zf.write(f, arc)
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True, help="包版本号（变了才会触发客户端更新）")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--src", default=str(KB_SRC), help="知识库源目录（默认 ~/.ng-platform/knowledge）")
    ap.add_argument("--manifest", default="", help="顺带更新的 update.json 路径")
    ap.add_argument("--url-base", default="https://ng-platform.ai/downloads",
                    help="包的公网地址前缀")
    a = ap.parse_args()

    src, out = Path(a.src), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    pack = out / f"kb-{a.version}.pack"

    counts = {}
    with zipfile.ZipFile(pack, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for sub in ("tax-cases", "index", "models"):
            d = src / sub
            if d.is_dir():
                counts[sub] = _add(zf, d, sub)

    # 加密：包可以公开托管，没有授权里的密钥就是一堆乱码
    pkey = KB_SRC.parent / "_pack.key"
    if not pkey.is_file():
        pkey = Path.home() / ".secrets" / "ng-license" / "pack.key"
    if pkey.is_file():
        import os as _os
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        k = pkey.read_bytes()
        nonce = _os.urandom(12)
        enc = AESGCM(k).encrypt(nonce, pack.read_bytes(), None)
        pack.write_bytes(nonce + enc)
        print(f"  ✓ 已加密（密钥 {pkey}）")
    else:
        print("  ⚠ 未加密——找不到 pack.key，包将是明文")

    raw = pack.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    size = len(raw)

    print(f"\n  ✓ 知识包: {pack}")
    for k, v in counts.items():
        print(f"      {k:12} {v} 个文件")
    print(f"  size    {size/1e6:.1f} MB")
    print(f"  sha256  {sha}")
    url = f"{a.url_base}/{pack.name}"
    print(f"  url     {url}")

    if a.manifest:
        mf = Path(a.manifest)
        m = json.loads(mf.read_text(encoding="utf-8")) if mf.is_file() else {}
        m.setdefault("version", a.version)
        m["knowledge"] = {"version": a.version, "url": url, "sha256": sha, "size": size}
        mf.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  ✓ 已更新 manifest: {mf}")

    print("\n  下一步：把 .pack 传到 url 指向的位置，再把 update.json 发到官网。")


if __name__ == "__main__":
    main()
