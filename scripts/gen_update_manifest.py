#!/usr/bin/env python3
"""内容热更发布：把 frontend/dist 复制到官网可下载的 website/ui/ 并生成 update.json。

用法（每次改了前端，发内容升级包时跑）:
  python3 scripts/gen_update_manifest.py
产物:
  website/update.json   —— 客户端 POST /update/apply 拉这个 manifest
  website/ui/…          —— 官网静态托管（GitHub Pages 自动发布），url 指向 https://ng-platform.ai/ui/…
  changelog 同步见 CHANGELOG.md / version.json
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "frontend" / "dist"
OUT = ROOT / "website" / "ui"
BASE = "https://ng-platform.ai/ui"
_EXT = {".html", ".js", ".css", ".svg", ".png", ".ico", ".json", ".map"}


def main() -> int:
    if not (DIST / "index.html").exists():
        print("缺少 frontend/dist（先 npm run build）", file=sys.stderr)
        return 1
    sys.path.insert(0, str(ROOT))
    from app.version import VERSION
    OUT.mkdir(parents=True, exist_ok=True)
    files = []
    for p in sorted(DIST.rglob("*")):
        if p.is_file() and p.suffix.lower() in _EXT:
            rel = p.relative_to(DIST).as_posix()
            target = OUT / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(p.read_bytes())
            files.append({
                "path": f"ui/{rel}",
                "url": f"{BASE}/{rel}",
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            })
    manifest = {"version": VERSION, "date": __import__("time").strftime("%Y-%m-%d"),
                "files": files}
    (ROOT / "website" / "update.json").write_text(json.dumps(manifest, ensure_ascii=False),
                                                  encoding="utf-8")
    print(f"update.json version={VERSION}, files={len(files)} → website/ui")
    return 0


if __name__ == "__main__":
    sys.exit(main())
