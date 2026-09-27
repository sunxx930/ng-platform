#!/usr/bin/env python3
"""打「代码补丁」——日常迭代的分发单元（业务代码 + 界面，不含原生依赖）。

包内容（解压后镜像仓库结构，正好能被 ng_boot 的 sys.path 覆盖用）：
    app/**              业务代码（只 .py + agents/templates.json）
    frontend/dist/**    界面资产
    payload.json        逐文件 sha256 清单（客户端据此校验）

确定性：文件按名排序、时间戳固定为 1980-01-01 —— 同样的源码永远得到同样的 zip，
便于比对与复现。

用法:
  python3 scripts/build_code_patch.py --version 1.3.1
  python3 scripts/build_code_patch.py --version 1.3.1 --out dist

产物: <out>/code-<version>.zip （打印 sha256 / 大小，交给 issue_code_patch.py 签名）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_FIXED_DT = (1980, 1, 1, 0, 0, 0)      # 固定时间戳 → 可复现

# 打进补丁的顶层目录（与 ng_boot 的约定一致）
_INCLUDE = ("app", "frontend/dist")
# 不打的：字节码、以及不是代码/界面的东西
_SKIP_DIRS = {"__pycache__", ".pytest_cache"}
_SKIP_SUFFIX = {".pyc", ".pyo"}
_SKIP_NAMES = {"schema.sql"}


def _iter_files() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for top in _INCLUDE:
        base = ROOT / top
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            if any(part in _SKIP_DIRS for part in p.parts):
                continue
            if p.suffix in _SKIP_SUFFIX or p.name in _SKIP_NAMES:
                continue
            out.append((p.relative_to(ROOT).as_posix(), p))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True, help="补丁版本号（= app/version.py 的 VERSION）")
    ap.add_argument("--out", default="dist")
    a = ap.parse_args()

    files = _iter_files()
    if not files:
        print("✗ 没找到要打包的文件（app/ 或 frontend/dist/ 不存在？）")
        return 1

    # 逐文件 sha256 → payload.json
    manifest: dict[str, str] = {}
    for rel, p in files:
        manifest[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    payload_json = json.dumps(
        {"version": a.version, "files": manifest}, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")

    out_dir = ROOT / a.out if not Path(a.out).is_absolute() else Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"code-{a.version}.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for rel, p in files:
            zi = zipfile.ZipInfo(rel, date_time=_FIXED_DT)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            zf.writestr(zi, p.read_bytes())
        zi = zipfile.ZipInfo("payload.json", date_time=_FIXED_DT)
        zi.compress_type = zipfile.ZIP_DEFLATED
        zi.external_attr = 0o644 << 16
        zf.writestr(zi, payload_json)

    blob = zip_path.read_bytes()
    print(f"  ✓ 代码补丁: {zip_path}")
    print(f"      文件数  {len(files)}")
    print(f"      size    {len(blob)}")
    print(f"      sha256  {hashlib.sha256(blob).hexdigest()}")
    print(f"\n  下一步: python3 scripts/issue_code_patch.py --version {a.version} "
          f"--zip {zip_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
