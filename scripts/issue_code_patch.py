#!/usr/bin/env python3
"""给「代码补丁」签名，产出可并进 update.json 的 code 段。

签的是**规范化描述符**（ng_crypto.code_descriptor），不是裸哈希 ——
只签哈希的话，攻击者能拿旧包配新版本号做静默降级/回放；把版本、顺序号、
兼容性、哈希绑在一起签，改任一字段签名即失效。

私钥与授权同源：~/.secrets/ng-license/license-private.pem（**只在发行方本机**，
绝不进 CI）。这与出授权串、mac 公证的做法一致。

用法:
  python3 scripts/issue_code_patch.py --version 1.3.1 --zip dist/code-1.3.1.zip \
      --url https://github.com/sunxx930/ng-platform/releases/download/v1.3.1/code-1.3.1.zip

  # 不带 --url 时只打印，不发文件（本地测试用）
  python3 scripts/issue_code_patch.py --version 1.3.1 --zip dist/code-1.3.1.zip --out dist
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ng_boot     # noqa: E402
import ng_crypto   # noqa: E402

PRIVKEY = Path.home() / ".secrets" / "ng-license" / "license-private.pem"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--zip", required=True, help="build_code_patch.py 产出的 zip")
    ap.add_argument("--url", default="", help="补丁的公网地址（GitHub Release 资产）")
    ap.add_argument("--min-shell", default=ng_boot.MIN_SHELL_VERSION,
                    help="要求的最低外壳版本（默认 = 首个带加载器的版本）")
    ap.add_argument("--payload-api", type=int, default=ng_boot.PAYLOAD_API)
    ap.add_argument("--seq", type=int, default=0, help="顺序号（默认取当前时间戳）")
    ap.add_argument("--out", default="", help="把 code 段写到这里（如 dist/code-1.3.1.json）")
    a = ap.parse_args()

    if not PRIVKEY.is_file():
        print(f"✗ 找不到发行方私钥: {PRIVKEY}", file=sys.stderr)
        return 1
    zpath = Path(a.zip)
    if not zpath.is_file():
        print(f"✗ 找不到补丁: {zpath}", file=sys.stderr)
        return 1

    blob = zpath.read_bytes()
    sha = hashlib.sha256(blob).hexdigest()
    seq = a.seq or int(time.time())

    desc = ng_crypto.code_descriptor(
        version=a.version, min_shell=a.min_shell, payload_api=a.payload_api,
        seq=seq, sha256=sha, size=len(blob))
    sig = ng_crypto.sign_blob(desc, str(PRIVKEY))

    # 自检：签完立刻验一遍，避免发出一个自己都验不过的补丁
    if not ng_crypto.verify_blob(desc, sig):
        print("✗ 签名自检失败（不该发生）", file=sys.stderr)
        return 1

    block = {
        "version": a.version,
        "min_shell": a.min_shell,
        "payload_api": a.payload_api,
        "seq": seq,
        "url": a.url or f"<填公网地址>/code-{a.version}.zip",
        "sha256": sha,
        "size": len(blob),
        "sig": sig,
    }
    text = json.dumps(block, ensure_ascii=False, indent=2)
    print(text)
    if a.out:
        outp = Path(a.out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(json.dumps(block, ensure_ascii=False), encoding="utf-8")
        print(f"\n  ✓ 已写 {outp}（并进 update.json 的 code 段）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
