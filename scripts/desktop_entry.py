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
    """用户数据目录（事件/用户/日志落这里，可写）。不可写回退临时目录，避免首次注册 500。"""
    if _is_frozen():
        base = Path(os.environ.get("NG_HOME", Path.home() / ".ng-platform"))
    else:
        base = Path(__file__).resolve().parent.parent / "data"
    try:
        base.mkdir(parents=True, exist_ok=True)
        probe = base / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        # 成功路径也必须导出 NG_HOME —— main.py 靠这个变量判定内容热更目录（NG_HOME/ui）。
        # 此前只在失败回退里设置，导致正常运行时 NG_HOME 为空、热更目录永远不被读取。
        os.environ.setdefault("NG_HOME", str(base))
        return base
    except Exception as e:      # noqa: BLE001
        import tempfile
        fallback = Path(tempfile.gettempdir()) / "ng-platform-data"
        fallback.mkdir(parents=True, exist_ok=True)
        print(f"[desktop] 数据目录 {base} 不可写（{e}），回退到 {fallback}", flush=True)
        os.environ["NG_HOME"] = str(fallback)
        return fallback


def _ask_yes_no(title: str, text: str) -> bool:
    """原生确认框。拿不到 GUI（无头/异常）就返回 False —— 宁可不启用，不要误启用。"""
    try:
        if sys.platform == "darwin":
            import json
            import subprocess
            script = (f'display dialog {json.dumps(text)} with title {json.dumps(title)} '
                      'buttons {"暂不启用", "启用"} default button "启用"')
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=300)
            return "button returned:启用" in r.stdout
        if sys.platform == "win32":
            import ctypes
            MB_YESNO, MB_ICONQUESTION = 0x04, 0x20
            return ctypes.windll.user32.MessageBoxW(
                0, text, title, MB_YESNO | MB_ICONQUESTION) == 6
    except Exception:      # noqa: BLE001
        pass
    return False


def _maybe_enable_tax(wd: Path) -> None:
    """方案B：安装包里带了税务小包 → 首次启动问一句要不要启用。

    只问一次（无论答什么都记 .tax_prompted），之后可由界面里的入口再启用。
    答「启用」→ 就地展开加密包（无需联网）；试用期不在此起算，从**法规库启用**才起算。
    """
    try:
        from app.services.updater import bundled_pack_path, install_bundled_pack
    except Exception as e:      # noqa: BLE001
        print(f"[desktop] 税务版模块不可用: {e}", flush=True)
        return

    kb = wd / "knowledge"
    if (kb / "index" / "vec.npy").is_file():
        return                                   # 已启用
    flag = kb / ".tax_prompted"
    if flag.exists():
        return                                   # 问过了，不重复打扰
    try:
        src = bundled_pack_path()
    except Exception:      # noqa: BLE001
        return
    if not src:
        return                                   # 本包不带税务库

    ok = _ask_yes_no(
        "NG AI Platform 税务版",
        "这个安装包里带了「税务知识库」（案例库 + 语义检索）。\n\n"
        "要现在启用吗？启用后可在联机助手中检索税务案例。\n"
        "试用 30 天；试用期从**法规库启用**时才开始计算。",
    )
    try:
        kb.mkdir(parents=True, exist_ok=True)
        flag.write_text("1", encoding="utf-8")
    except Exception:      # noqa: BLE001
        pass
    if not ok:
        print("[desktop] 用户暂未启用税务版（可稍后在界面里启用）", flush=True)
        return

    res = install_bundled_pack(kb)
    print(f"[desktop] 税务版启用: {res}", flush=True)
    if res.get("applied"):
        _ask_yes_no("NG AI Platform 税务版",
                    "税务知识库已启用 ✓\n\n打开界面即可在助手里检索税务案例。")
    else:
        _ask_yes_no("NG AI Platform 税务版",
                    f"启用没成功：{res.get('reason')}\n\n可稍后在界面里重试。")


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
    import app.main as M                       # 先导入：拿到共享的 log 与 app
    from app.workers.runner import run_forever
    port = int(os.environ.get("NG_PORT", "8001"))

    # 单机版关键修复（2026-09-12）：打包应用内**同进程后台线程**跑 worker，
    # 否则只起 API+前端 → 任务永远停 todo（auto_start/auto_agent/复核不执行）。
    threading.Thread(target=run_forever, args=(M.log,), daemon=True).start()

    def _serve():
        uvicorn.run(M.app, host="127.0.0.1", port=port, log_level="warning")

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

    # 首次运行：安装包里带了税务小包 → 先问一句要不要启用（方案B）
    _maybe_enable_tax(wd)

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
