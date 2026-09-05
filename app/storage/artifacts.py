"""交付物 file_ref 沙箱解析（P0-3）。

把可读路径收口到程序自身的 artifacts 目录，杜绝 /etc/hosts、.env、../ 等任意
服务端文件读取。基准目录 = NG_HOME（桌面版 desktop_entry 会 chdir 到该目录）或
进程 CWD 下的 artifacts/；dev/docker 亦一致（docker 挂 ./artifacts:/app/artifacts）。

用法：由 _deliverable_evidence 这类读方调用；不合法路径抛 PermissionError，
文件不存在抛 FileNotFoundError，调用方自行转 HTTP。
"""
from __future__ import annotations

import os
from pathlib import Path

_BAD = ("\\", ":", "\x00")


def artifacts_base() -> Path:
    """artifacts 根目录（绝对路径）。"""
    root = Path(os.environ.get("NG_HOME") or Path.cwd()).resolve()
    return root / "artifacts"


def resolve_artifact(file_ref: str) -> Path:
    """校验并解析 file_ref → artifacts 目录内的真实文件 Path。

    兼容三种历史/现行写法（统一收口到 artifacts 根内，沙箱不破）：
      ① 裸名 `63c….md`（新契约，2c312ad 沙箱后 builtin 产方）
      ② 旧前缀 `artifacts/63c….md`（builtin 旧产出 + 历史事件 + 上游注入引用）→ 自动剥前缀
      ③ 绝对路径且落在 artifacts 根内（历史手动提交记录）→ 放行；根外绝对路径仍拒绝

    - 拒绝：空值、含 .. 穿越、反斜杠/盘符/空字节；符号链接解析后必须仍落在根内
    - 不存在 → FileNotFoundError；不合法 → PermissionError
    """
    if not file_ref:
        raise PermissionError("file_ref 为空")
    if any(b in file_ref for b in _BAD):
        raise PermissionError(f"file_ref 含非法字符: {file_ref!r}")
    if ".." in file_ref.split("/"):
        raise PermissionError(f"file_ref 不允许目录穿越: {file_ref!r}")
    base = artifacts_base()
    p = Path(file_ref)
    if p.is_absolute():
        # 历史绝对路径兼容：解析后必须仍在 artifacts 根内（沙箱不破）
        p = p.resolve()
        try:
            p.relative_to(base)
        except ValueError:
            raise PermissionError(f"file_ref 越界（不在 artifacts 目录内）: {file_ref!r}")
        if not p.is_file():
            raise PermissionError(f"file_ref 不是普通文件: {file_ref!r}")
        return p
    # 相对路径兼容：幂等剥掉旧 `artifacts/` 前缀（builtin 旧产出/历史事件/上游引用多带）
    rel = p.as_posix()
    while rel.startswith("artifacts/"):
        rel = rel[len("artifacts/"):]
    p = (base / rel).resolve()
    try:
        p.relative_to(base)
    except ValueError:
        raise PermissionError(f"file_ref 越界（不在 artifacts 目录内）: {file_ref!r}")
    if not p.exists():
        raise FileNotFoundError(file_ref)
    if not p.is_file():
        raise PermissionError(f"file_ref 不是普通文件: {file_ref!r}")
    return p
