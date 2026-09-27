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


def _notify(title: str, text: str) -> None:
    """单按钮提示框（回退通知用）。拿不到 GUI 就退化为打印。"""
    try:
        if sys.platform == "darwin":
            import json
            import subprocess
            subprocess.run(["osascript", "-e",
                            f'display dialog {json.dumps(text)} with title {json.dumps(title)} '
                            'buttons {"好"} default button "好"'],
                           capture_output=True, timeout=300)
            return
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)   # MB_ICONINFORMATION
            return
    except Exception:      # noqa: BLE001
        pass
    print(f"[desktop] {title}: {text}", flush=True)


def _maybe_enable_tax(wd: Path) -> None:
    """首次启动问一句要不要启用税务版（案例库补丁）。

    补丁与安装包并列挂在官网，点「要」就地下载安装（带断点续传 + sha256 校验）。
    只问一次（无论答什么都记 .tax_prompted），之后可由界面里的入口再启用。
    试用期不在此起算——从**法规库启用**才起算（见 updater._install_blob）。
    """
    try:
        from app.services.kb_semantic import content_packs
        from app.services.updater import apply_knowledge_pack
    except Exception as e:      # noqa: BLE001
        print(f"[desktop] 税务版模块不可用: {e}", flush=True)
        return

    kb = wd / "knowledge"
    if content_packs():
        return                                   # 已有内容补丁
    flag = kb / ".tax_prompted"
    if flag.exists():
        return                                   # 问过了，不重复打扰

    ok = _ask_yes_no(
        "NG AI Platform 税务版",
        "要启用「税务知识库」吗？\n\n"
        "启用后可在助手里检索税务案例（案例库 + 语义检索，离线可用）。\n"
        "需要联网下载一次（约 300MB，支持断点续传）。",
    )
    try:
        kb.mkdir(parents=True, exist_ok=True)
        flag.write_text("1", encoding="utf-8")
    except Exception:      # noqa: BLE001
        pass
    if not ok:
        print("[desktop] 用户暂未启用税务版（可稍后在界面里启用）", flush=True)
        return

    # 补丁约 300MB，**放后台线程下**——否则界面要等下载完才打开（首次体验很差）。
    def _worker() -> None:
        try:
            res = apply_knowledge_pack(kb)
        except Exception as e:      # noqa: BLE001
            res = {"applied": False, "reason": str(e)}
        print(f"[desktop] 税务版启用: {res}", flush=True)
        if res.get("applied"):
            _ask_yes_no("NG AI Platform 税务版",
                        "税务知识库已启用 ✓\n\n现在就能在助手里检索税务案例了。")
        else:
            _ask_yes_no("NG AI Platform 税务版",
                        f"下载/启用没成功：{res.get('reason')}\n\n"
                        "可稍后在界面里重试。")

    threading.Thread(target=_worker, daemon=True).start()
    print("[desktop] 税务知识库后台下载中，界面照常可用…", flush=True)


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

    # 代码补丁：决定这次启动用**哪个版本**的代码（提升待生效版本 / 检测回滚），
    # 再把载荷根插到 sys.path 最前——它会先于 _MEIPASS 被命中，从而覆盖打包的 app.*。
    # 必须在 import app.main 之前做（一旦 app 进了 sys.modules，覆盖就失效）。
    import ng_boot
    code_root = ng_boot.resolve(bundle)
    if code_root:
        sys.path.insert(0, str(code_root))
        print(f"[desktop] 使用代码载荷: {code_root}", flush=True)

    import uvicorn
    try:
        import app.main as M                   # 先导入：拿到共享的 log 与 app
    except Exception as e:                     # noqa: BLE001
        # 载荷在 import 阶段就炸（坏补丁最常见的形态）→ 当场隔离并回退，不等下次启动
        print(f"[desktop] 载荷 import 失败，回退：{type(e).__name__}: {e}", flush=True)
        code_root = ng_boot.fallback_after_import_failure(bundle)
        if code_root:
            sys.path.insert(0, str(code_root))
        import app.main as M
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

    # API 活了 → 钉住这次启动是好的（否则下次启动会把它当"启动失败"回滚）
    if code_root:
        ng_boot.confirm()
    if os.environ.get("NG_BOOT_ROLLED_BACK"):
        _notify("NG AI Platform",
                f"上次的更新（{os.environ['NG_BOOT_ROLLED_BACK']}）启动失败，已自动回退到上一个可用版本。\n"
                "你的项目和资料没有受影响。")

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
