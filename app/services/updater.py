"""内容热更（升级模式 A，v1.2.1）：只备"UI/内容升级包"，客户端自动拉取替换。

安全边界（保证客户项目不受影响）：
- 只写 NG_HOME/ui（前端资产），**绝不碰** data/events/artifacts → 项目/任务零影响
- 只允许白名单扩展名 & 无目录穿越；下载→sha256 校验→原子 rename 覆盖
- manifest 默认 https://ng-platform.ai/update.json（可 env NG_UPDATE_MANIFEST 覆盖/测试）
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import urllib.request
from pathlib import Path

_ALLOW_EXT = {".html", ".js", ".css", ".svg", ".png", ".ico", ".json", ".woff", ".woff2", ".map"}
_MAX_FILES = 200
_MAX_TOTAL_MB = 80


def manifest_url() -> str:
    return os.environ.get("NG_UPDATE_MANIFEST", "https://ng-platform.ai/update.json")


def apply_ui_update(ui_dir: Path) -> dict:
    """拉 manifest 并按需落地到 ui_dir。返回 {applied, version, failed}。"""
    ui_dir = Path(ui_dir)
    ui_dir.mkdir(parents=True, exist_ok=True)
    data = urllib.request.urlopen(manifest_url(), timeout=10).read()
    m = json.loads(data.decode("utf-8"))
    files = m.get("files") or []
    version = str(m.get("version", "") or "")
    applied = failed = 0
    if len(files) > _MAX_FILES:
        raise ValueError(f"升级包文件数超限 > {_MAX_FILES}")
    for f in files:
        path = str(f.get("path", "") or "").replace("\\", "/")
        url = str(f.get("url", "") or "")
        want = str(f.get("sha256", "") or "").lower()
        if not path or not url:
            continue
        if path.startswith("/") or ".." in path.split("/") or ":" in path:
            failed += 1
            continue
        if Path(path).suffix.lower() not in _ALLOW_EXT:
            failed += 1
            continue
        target = (ui_dir / path).resolve()
        try:
            target.relative_to(ui_dir.resolve())
        except ValueError:
            failed += 1
            continue
        raw = urllib.request.urlopen(url, timeout=15).read()
        if want and hashlib.sha256(raw).hexdigest() != want:
            failed += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        # 原子写：同目录临时文件 → os.replace
        fd, tmp = tempfile.mkstemp(prefix=".ui-", dir=str(target.parent))
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
            os.replace(tmp, target)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        applied += 1
    if version:
        (ui_dir / "version.json").write_text(json.dumps({"version": version},
                                                        ensure_ascii=False), encoding="utf-8")
    return {"applied": applied, "failed": failed, "version": version}
