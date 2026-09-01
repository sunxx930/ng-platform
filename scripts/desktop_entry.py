"""NG-AI-Platform 桌面版入口（PyInstaller 打包用）。

双击即用：启动时拉起 uvicorn API + 自动打开浏览器；退出时关掉。
零依赖：默认 JSONL 存储（不装 PostgreSQL），功能完整。
"""
from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
from pathlib import Path


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _bundle_root() -> Path:
    """PyInstaller 打包后的资源根目录。"""
    if _is_frozen():
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def _work_dir() -> Path:
    """用户数据目录（事件/用户/日志落这里，可写）。"""
    if _is_frozen():
        base = Path(os.environ.get("NG_HOME", Path.home() / ".ng-platform"))
    else:
        base = Path(__file__).resolve().parent.parent / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base


def main():
    # 工作目录切到用户数据目录，确保 JSONL/日志可写
    wd = _work_dir()
    os.chdir(wd)

    # 用绝对路径定位打包内的数据文件（templates.json / dist）
    bundle = _bundle_root()
    # 资源可能打包在 _MEIPASS/app/... 或 bundle 下
    for cand in (bundle / "app" / "agents" / "templates.json",
                 bundle / "agents" / "templates.json"):
        if cand.exists():
            os.environ.setdefault("NG_TEMPLATES_PATH", str(cand.parent))
            break

    import uvicorn
    port = int(os.environ.get("NG_PORT", "8001"))

    # 后台启动 uvicorn
    def _serve():
        uvicorn.run("app.main:app", host="127.0.0.1", port=port, log_level="warning")

    t = threading.Thread(target=_serve, daemon=True)
    t.start()

    # 等 API ready 后开浏览器
    url = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            import urllib.request
            urllib.request.urlopen(f"{url}/health", timeout=1)
            break
        except Exception:
            time.sleep(0.5)

    webbrowser.open(url)
    print(f"NG-AI-Platform 运行中: {url}  (Ctrl+C 退出)", flush=True)

    # 主线程阻塞，Ctrl+C 退出
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nNG-AI-Platform 已退出", flush=True)


if __name__ == "__main__":
    main()
