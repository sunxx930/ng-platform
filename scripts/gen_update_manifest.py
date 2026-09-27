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
OUT = ROOT / "ui"
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
    mf = ROOT / "update.json"
    prev = {}
    if mf.is_file():
        try:
            prev = json.loads(mf.read_text(encoding="utf-8")) or {}
        except Exception:      # noqa: BLE001
            prev = {}
    manifest = {"version": VERSION, "date": __import__("time").strftime("%Y-%m-%d"),
                "files": files}
    # 保留各「内容域」——它们由各自的上线流程维护，本脚本只管 files/version/date。
    # 此前只写三个键，会把 knowledge 段整个抹掉（每次都得手工合回去），这里修根。
    #   knowledge      案例库补丁
    #   knowledge_regs 法规库补丁（出现即视为"法规库已上线"，触发案例库闸门）
    #   code           签名后的代码补丁
    # --code <file> 可把 issue_code_patch.py 产出的 code 段直接灌进来。
    for k in ("knowledge", "knowledge_regs", "code"):
        if prev.get(k):
            manifest[k] = prev[k]
    if len(sys.argv) > 2 and sys.argv[1] == "--code":
        manifest["code"] = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    mf.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    extra = [k for k in ("knowledge", "knowledge_regs", "code") if manifest.get(k)]
    print(f"update.json version={VERSION}, files={len(files)}"
          f"{', 保留内容域: ' + ','.join(extra) if extra else ''} → ui/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
