"""代码补丁的启动器：决定这次启动用**哪个版本**的代码，并处理切换/回滚。

⚠️ 硬性规则：**本模块绝不 import `app.*`**。
   一旦 `import app.anything`，打包进 PYZ 的 `app` 就进了 sys.modules，
   之后子模块会走 `app.__path__`（=_MEIPASS/app），sys.path 覆盖直接失效。
   所以这里只用标准库 + ng_crypto。

一次启动的流程（由 scripts/desktop_entry.py 调用）：
    resolve(bundle)          → 回滚检测 → 提升 pending → 返回该用的代码根（或 None=用打包版）
    ... 调用方把 code_root 插到 sys.path[0]，再 import app.main ...
    confirm()                → /health 通了之后确认这次启动是好的
    fallback_after_import_failure(bundle)
                             → import 就炸时当场隔离坏版本、回退上一版/打包版

为什么"只在下次启动切换"：桌面版是单进程，启动那一刻天然没有在跑的 agent 任务，
所以结构上不可能打断客户正在进行的项目（这是需求"覆盖会破坏进程"的正面解法）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# 启动尝试几次才判定"这个版本真的坏了"并回退（避免用户刚启动就退出被误判）
_MAX_BOOT_ATTEMPTS = 2

# 载荷接口版本：外壳（安装包）与载荷（代码补丁）约定的能力号。
# 只有改动了「载荷与外壳之间的契约」时才 +1 —— 那样老外壳会**明确拒绝**新载荷，
# 而不是半途应用出一个坏状态。装 shell.json 时写入的就是这个值。
PAYLOAD_API = 1

# 第一个带加载器的外壳版本：载荷的 min_shell 默认值。
# 比它老的外壳没有 ng_boot（1.2.x），无法加载载荷 —— 必须明确拒绝。
MIN_SHELL_VERSION = "1.3.0"


# ---------- 路径与原子读写 ----------

def code_dir() -> Path:
    base = Path(os.environ.get("NG_HOME") or (Path.home() / ".ng-platform"))
    return base / "code"


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:      # noqa: BLE001
        return {}


def _write_json(p: Path, obj: dict) -> None:
    """原子写：同目录临时文件 → os.replace（避免半截文件被下次启动读到）。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + p.name + ".", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:      # noqa: BLE001
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _version_root(code: Path, version: str) -> Path:
    return code / "versions" / str(version)


def _valid(root: Path) -> bool:
    return (root / "app" / "__init__.py").is_file()


# ---------- 兼容标记 ----------

def _cmp_version(a: str, b: str) -> int:
    """a>b → 1；a<b → -1；相等 → 0。只比 主.次.补丁（忽略 -xxx 后缀）。"""
    def nums(v: str) -> list[int]:
        try:
            return [int(x) for x in str(v).split("-")[0].split(".")]
        except Exception:      # noqa: BLE001
            return [0]
    aa, bb = nums(a), nums(b)
    for x, y in zip(aa, bb):
        if x != y:
            return (x > y) - (x < y)
    return (len(aa) > len(bb)) - (len(aa) < len(bb))


def shell_info(bundle_root: Path) -> dict:
    """读打包进安装包的 shell.json（外壳能力）。读不到就当最保守的空壳。"""
    return _read_json(Path(bundle_root) / "shell.json")


def payload_compatible(shell: dict, *, min_shell: str, payload_api: int) -> tuple[bool, str]:
    """载荷能不能在这个外壳上跑。返回 (是否兼容, 原因)。"""
    sv = str(shell.get("shell_version") or "")
    sa = int(shell.get("payload_api") or 0)
    if min_shell and sv and _cmp_version(sv, str(min_shell)) < 0:
        return False, f"此更新需要新版安装包（≥ {min_shell}），当前外壳 {sv}"
    if int(payload_api or 0) > sa:
        return False, f"此更新需要更新的安装包（载荷接口 {payload_api} > 外壳 {sa}）"
    return True, ""


# ---------- 启动决策 ----------

def _quarantine(code: Path, version: str, reason: str) -> None:
    """把启动失败的载荷挪进 quarantine/，留作诊断，不再参与启动。"""
    src = _version_root(code, version)
    if not src.is_dir():
        return
    dst = code / "quarantine" / str(version)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        os.replace(src, dst)
        _write_json(dst / "QUARANTINE_REASON.json", {"version": version, "reason": reason})
    except Exception:      # noqa: BLE001
        shutil.rmtree(src, ignore_errors=True)


def _prune(code: Path, keep: set[str]) -> None:
    """只留 current/previous/quarantine，删掉更老的版本目录，避免磁盘无限增长。"""
    vdir = code / "versions"
    if not vdir.is_dir():
        return
    for d in vdir.iterdir():
        if d.is_dir() and d.name not in keep:
            shutil.rmtree(d, ignore_errors=True)


def resolve(bundle_root: Path) -> Path | None:
    """决定本次启动用哪个代码根。返回 None = 用打包内的代码。

    顺序：① 上次启动没确认成功 → 判定回退 ② 提升 pending ③ 返回 current。
    """
    code = code_dir()
    try:
        code.mkdir(parents=True, exist_ok=True)
    except Exception:      # noqa: BLE001
        return None                      # 目录不可写 → 老实跑打包版

    # ① 回滚检测：上次提升了版本但没能 confirm
    boot = _read_json(code / ".boot.json")
    if boot and not boot.get("confirmed"):
        badv = str(boot.get("version") or "")
        attempts = int(boot.get("attempts") or 1)
        if badv and attempts >= _MAX_BOOT_ATTEMPTS:
            _quarantine(code, badv, f"启动 {attempts} 次未确认成功")
            prev = _read_json(code / "previous.json")
            pv = str(prev.get("version") or "")
            if pv and _valid(_version_root(code, pv)):
                _write_json(code / "current.json", {"version": pv})
            else:
                (code / "current.json").unlink(missing_ok=True)
            (code / ".boot.json").unlink(missing_ok=True)
            (code / "pending.json").unlink(missing_ok=True)
            os.environ["NG_BOOT_ROLLED_BACK"] = badv
        else:
            # 只崩过一次 → 再给它一次机会（用户刚开就退出也会走到这里）
            boot["attempts"] = attempts + 1
            _write_json(code / ".boot.json", boot)

    # ② 提升待生效的版本（这就是"下次启动才切换"那一步）
    pend = _read_json(code / "pending.json")
    pv = str(pend.get("version") or "")
    if pv and _valid(_version_root(code, pv)):
        cur = _read_json(code / "current.json")
        cv = str(cur.get("version") or "")
        if cv and cv != pv:
            _write_json(code / "previous.json", {"version": cv})
        _write_json(code / "current.json", {"version": pv})
        same = str(boot.get("version") or "") == pv
        _write_json(code / ".boot.json",
                    {"version": pv, "confirmed": False,
                     "attempts": (int(boot.get("attempts") or 0) + 1) if same else 1})
        (code / "pending.json").unlink(missing_ok=True)
        _prune(code, {pv, str(_read_json(code / "previous.json").get("version") or "")})

    # ③ 返回要用的代码根
    cur = _read_json(code / "current.json")
    cv = str(cur.get("version") or "")
    if cv:
        root = _version_root(code, cv)
        if _valid(root):
            ui = root / "frontend" / "dist"
            if ui.is_dir():
                # 让 app.main 的 _ui_roots 优先用载荷里的界面（盖过遗留的 NG_HOME/ui）
                os.environ["NG_UI_ROOT"] = str(ui)
            return root
    return None


def confirm() -> None:
    """/health 通了 → 这次启动是好的，钉住，下次不再走回滚判断。"""
    code = code_dir()
    boot = _read_json(code / ".boot.json")
    if boot and not boot.get("confirmed"):
        boot["confirmed"] = True
        try:
            _write_json(code / ".boot.json", boot)
        except Exception:      # noqa: BLE001
            pass


def fallback_after_import_failure(bundle_root: Path) -> Path | None:
    """`import app.main` 抛异常时调用：当场隔离坏版本，返回可用的代码根（或 None=打包版）。

    与 resolve 里的回滚不同，这是**同一次启动内**的补救——载荷在 import 阶段就炸，
    是最常见的坏补丁形态，不该等到下次启动才处理。
    """
    code = code_dir()
    cur = _read_json(code / "current.json")
    cv = str(cur.get("version") or "")
    if cv:
        _quarantine(code, cv, "import app.main 失败")
        (code / "current.json").unlink(missing_ok=True)
        (code / ".boot.json").unlink(missing_ok=True)
        (code / "pending.json").unlink(missing_ok=True)
        # 清掉半途 import 的 app 包，否则下次 import 仍会命中坏模块
        for k in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
            sys.modules.pop(k, None)
        bad_entry = str(_version_root(code, cv))
        sys.path[:] = [p for p in sys.path if str(p) != bad_entry]
        os.environ["NG_BOOT_ROLLED_BACK"] = cv

        prev = _read_json(code / "previous.json")
        pv = str(prev.get("version") or "")
        if pv and _valid(_version_root(code, pv)):
            _write_json(code / "current.json", {"version": pv})
            root = _version_root(code, pv)
            ui = root / "frontend" / "dist"
            if ui.is_dir():
                os.environ["NG_UI_ROOT"] = str(ui)
            return root
    return None
