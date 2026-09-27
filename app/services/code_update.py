"""代码补丁：下载 + 验签 + 暂存（**只暂存，绝不切换**）。

切换在下次启动由 ng_boot 做（见 ng_boot.resolve）。为什么不在这里切：
桌面版是单进程，运行中换代码会打断客户正在跑的任务——"下次启动生效"从结构上
杜绝了这件事。

校验顺序（fail-closed，任何一步失败都不动运行中的版本）：
    ① 验签（Ed25519，签的是规范化描述符）② 兼容性（min_shell / payload_api）
    ③ 下载（复用 updater._download，带断点续传）④ sha256
    ⑤ 防重放（seq 必须递增）⑥ 解压（目录穿越防护）
    ⑦ 逐文件 sha256（payload.json）⑧ 原子写 pending.json
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import ng_boot
import ng_crypto
from app.services import updater

_MAX_FILES = 2000          # 载荷是源码，比 UI 资产多，但仍要有上限


def _bundle_root() -> Path:
    """外壳资源根：冻结时是 _MEIPASS，开发时是仓库根。"""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent.parent


def _highest_seq(code_dir: Path) -> int:
    try:
        return int((code_dir / ".highest_seq").read_text(encoding="utf-8").strip())
    except Exception:      # noqa: BLE001
        return 0


def _set_highest_seq(code_dir: Path, seq: int) -> None:
    try:
        (code_dir / ".highest_seq").write_text(str(int(seq)), encoding="utf-8")
    except Exception:      # noqa: BLE001
        pass


def status() -> dict:
    """补丁状态（给界面看）。"""
    code = ng_boot.code_dir()
    cur = ng_boot._read_json(code / "current.json")   # noqa: SLF001 —— 同包内复用
    prev = ng_boot._read_json(code / "previous.json")  # noqa: SLF001
    pend = ng_boot._read_json(code / "pending.json")   # noqa: SLF001
    sh = ng_boot.shell_info(_bundle_root())
    return {
        "shell_version": sh.get("shell_version"),
        "payload_api": sh.get("payload_api"),
        "active": cur.get("version"),
        "previous": prev.get("version"),
        "pending": pend.get("version"),          # 非空 = 有更新等下次启动生效
        "highest_seq": _highest_seq(code),
        "restart_required": bool(pend.get("version")),
    }


def stage() -> dict:
    """检查并暂存新补丁。**不重启、不切换**。返回 {staged, version, reason, restart_required}。"""
    code = ng_boot.code_dir()
    code.mkdir(parents=True, exist_ok=True)

    try:
        manifest = updater._manifest()          # noqa: SLF001 —— 复用既有取 manifest
    except Exception as e:      # noqa: BLE001
        return {"staged": False, "reason": f"取 manifest 失败: {type(e).__name__}"}

    c = manifest.get("code") or {}
    version = str(c.get("version") or "")
    if not version:
        return {"staged": False, "reason": "manifest 无代码补丁"}

    cur = str(ng_boot._read_json(code / "current.json").get("version") or "")   # noqa: SLF001
    if version == cur:
        return {"staged": False, "version": version, "reason": "已是最新"}
    if version == str(ng_boot._read_json(code / "pending.json").get("version") or ""):  # noqa: SLF001
        return {"staged": True, "version": version, "reason": "已暂存，等下次启动生效",
                "restart_required": True}

    # ① 验签：签的是**规范化描述符**，改任一字 段签名即失效
    desc = ng_crypto.code_descriptor(
        version=version, min_shell=str(c.get("min_shell") or ""),
        payload_api=int(c.get("payload_api") or 0), seq=int(c.get("seq") or 0),
        sha256=str(c.get("sha256") or ""), size=int(c.get("size") or 0))
    if not ng_crypto.verify_blob(desc, str(c.get("sig") or "")):
        return {"staged": False, "version": version, "reason": "签名验证失败——补丁已丢弃"}

    # ② 兼容性：需要更新的外壳就明确拒绝，绝不半途应用
    ok, why = ng_boot.payload_compatible(
        ng_boot.shell_info(_bundle_root()),
        min_shell=str(c.get("min_shell") or ""), payload_api=int(c.get("payload_api") or 0))
    if not ok:
        return {"staged": False, "version": version, "reason": why}

    # ⑤ 防重放/降级：seq 必须比历史高
    seq = int(c.get("seq") or 0)
    if seq <= _highest_seq(code):
        return {"staged": False, "version": version, "reason": "补丁顺序号不递增，拒绝（防回放/降级）"}

    url = str(c.get("url") or "")
    want = str(c.get("sha256") or "").lower()
    size = int(c.get("size") or 0)
    if not url or not want:
        return {"staged": False, "version": version, "reason": "manifest 字段不全"}

    # ③ 下载（断点续传）
    part = code / "downloads" / f"code-{version}.zip.part"
    try:
        updater._download(url, part, resume=True)   # noqa: SLF001
        blob = part.read_bytes()
    except Exception as e:      # noqa: BLE001
        return {"staged": False, "version": version, "reason": f"下载失败: {e}"}

    # ④ sha256
    if hashlib.sha256(blob).hexdigest() != want:
        return {"staged": False, "version": version, "reason": "sha256 校验失败（已保留 .part 供续传）"}
    if size and len(blob) != size:
        return {"staged": False, "version": version, "reason": "大小与 manifest 不符"}

    # ⑥⑦ 解压到 versions/<v>，逐文件校验
    try:
        _extract_verified(blob, code / "versions" / version)
    except Exception as e:      # noqa: BLE001
        shutil.rmtree(code / "versions" / version, ignore_errors=True)
        return {"staged": False, "version": version, "reason": f"解压/校验失败: {e}"}

    # ⑧ 原子写 pending.json（下次启动由 ng_boot 提升）
    ng_boot._write_json(code / "pending.json",      # noqa: SLF001
                        {"version": version, "seq": seq})
    _set_highest_seq(code, seq)
    part.unlink(missing_ok=True)
    try:
        from app.version import VERSION
        cap = f"（当前 v{VERSION}）"
    except Exception:      # noqa: BLE001
        cap = ""
    return {"staged": True, "version": version, "restart_required": True,
            "reason": f"已就绪，下次启动生效{cap}"}


def _extract_verified(blob: bytes, dest: Path) -> None:
    """把载荷 zip 解开到 dest，按 payload.json 逐文件校 sha256。失败抛异常。"""
    import io
    import os as _os

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)      # versions/ 可能还不存在
    tmp = Path(tempfile.mkdtemp(prefix=".code-", dir=str(dest.parent)))
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = zf.namelist()
            if len(names) > _MAX_FILES:
                raise ValueError(f"载荷文件数超限 > {_MAX_FILES}")
            if "payload.json" not in names:
                raise ValueError("载荷缺少 payload.json（无法校验）")
            pj = json.loads(zf.read("payload.json").decode("utf-8"))
            files = pj.get("files") or {}
            if not files:
                raise ValueError("payload.json 没有 files 清单")
            total = 0
            for rel, want in files.items():
                rel = str(rel).replace("\\", "/").lstrip("/")
                if not rel or ".." in rel.split("/") or ":" in rel:
                    raise ValueError(f"载荷含非法路径: {rel}")
                if rel not in names:
                    raise ValueError(f"载荷缺文件: {rel}")
                raw = zf.read(rel)
                total += len(raw)
                if total > updater._MAX_TOTAL_MB * 1024 * 1024:   # noqa: SLF001
                    raise ValueError(f"载荷解压后超 {updater._MAX_TOTAL_MB}MB 上限")
                if hashlib.sha256(raw).hexdigest() != str(want).lower():
                    raise ValueError(f"文件校验失败: {rel}")
                dst = (tmp / rel).resolve()
                if tmp.resolve() not in dst.parents and dst != tmp.resolve():
                    raise ValueError(f"载荷路径越界: {rel}")
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(raw)
        if not (tmp / "app" / "__init__.py").is_file():
            raise ValueError("载荷缺少 app/__init__.py（不是有效载荷）")
        # 原子换入
        shutil.rmtree(dest, ignore_errors=True)
        _os.replace(tmp, dest)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
