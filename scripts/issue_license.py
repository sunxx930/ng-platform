#!/usr/bin/env python3
"""签发 NG 税务版授权串（发行方专用）。

私钥在 ~/.secrets/ng-license/license-private.pem —— **绝不入库、绝不随 App 分发**。
App 里只内置公钥，所以签出来的串无法被伪造。

用法:
  # 发给客户：绑定他的机器，有效期一年
  python3 scripts/issue_license.py --mid 6EDA4DDB058DC50D --days 365 --name "某某公司"

  # 不绑机器（客户换电脑不用重发；但一份串可多处用——按需选择）
  python3 scripts/issue_license.py --days 365 --name "某某公司"

  # 查看某个授权串的信息
  python3 scripts/issue_license.py --inspect "<授权串>"

  # 查本机机器码（让客户跑这个，把结果发你）
  python3 scripts/issue_license.py --machine-id
"""
import argparse
import base64
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.services.license import verify, machine_id  # noqa: E402

KEY = Path.home() / ".secrets" / "ng-license" / "license-private.pem"


def _b64(x: bytes) -> str:
    return base64.urlsafe_b64encode(x).decode().rstrip("=")


def sign(payload: dict) -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    if not KEY.is_file():
        sys.exit(f"✗ 找不到私钥：{KEY}\n  私钥只在发行方手里，丢了就再也签不出授权。")
    k = serialization.load_pem_private_key(KEY.read_bytes(), password=None)
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return _b64(raw) + "." + _b64(k.sign(raw))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mid", default="", help="绑定机器码（客户用 --machine-id 查）")
    ap.add_argument("--days", type=int, default=365, help="有效期天数（默认 365）")
    ap.add_argument("--exp", default="", help="直接指定到期日 YYYY-MM-DD（优先于 --days）")
    ap.add_argument("--tier", default="tax", help="版本档位（默认 tax）")
    ap.add_argument("--name", default="", help="客户名（仅记录用，可空）")
    ap.add_argument("--inspect", default="", help="查看已有授权串")
    ap.add_argument("--machine-id", action="store_true", help="打印本机机器码")
    a = ap.parse_args()

    if a.machine_id:
        print(machine_id()); return
    if a.inspect:
        info = verify(a.inspect)
        print("  有效 ✓" if info else "  无效 ✗（签名不匹配或格式错）")
        if info:
            print("  " + json.dumps(info, ensure_ascii=False))
            print(f"  本机机器码: {machine_id()}")
            if info.get("mid"):
                print("  机器绑定: " + ("匹配 ✓" if info["mid"] == machine_id() else "不匹配 ✗"))
        return

    exp = a.exp or (date.today() + timedelta(days=a.days)).isoformat()
    # 税务知识包的解密密钥随授权下发：包可公开托管，没有它解不开
    pkey = KEY.parent / "pack.key"
    payload = {"v": 1, "tier": a.tier, "exp": exp, "mid": a.mid, "name": a.name}
    if pkey.is_file():
        payload["pk"] = pkey.read_bytes().hex()
    tok = sign(payload)
    print()
    print("  " + tok)
    print()
    print(f"  档位: {a.tier}   到期: {exp}   绑定机器: {a.mid or '（不限）'}   客户: {a.name or '（未填）'}")
    print("  ↑ 把这一整串发给客户，让他在 App 里「激活」处粘贴")


if __name__ == "__main__":
    main()
