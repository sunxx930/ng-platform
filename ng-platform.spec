# -*- mode: python ; coding: utf-8 -*-
# NG-AI-Platform 桌面版打包配置（PyInstaller，mac + Windows 通用）
#
# 用法:
#   mac:     .venv/bin/pyinstaller ng-platform.spec --noconfirm
#   Windows: .venv\Scripts\pyinstaller ng-platform.spec --noconfirm
#
# 产物:
#   mac:     dist/NG-AI-Platform.app
#   Windows: dist/NG-AI-Platform.exe
#
# 注意: 打包必须在目标平台本身进行（PyInstaller 不支持跨平台）。

import platform
import sys
from pathlib import Path

# ---- 平台判断 ----
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

ROOT = Path(SPECPATH)

# 资源根（相对 __file__ 解析）：入口在 _MEIPASS 下，templates.json 等在 app/agents/
# 打包时按项目原结构打进去，运行时代码用 Path(__file__).parent 解析到 _MEIPASS 内。

a = Analysis(
    [str(ROOT / "scripts" / "desktop_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        # 前端 dist（后端托管，含 index.html + assets）
        (str(ROOT / "frontend" / "dist"), "frontend/dist"),
        # 后端数据文件
        (str(ROOT / "app" / "agents" / "templates.json"), "app/agents"),
        # 迁移与 schema（JSONL 模式不跑，但保留完整性）
        (str(ROOT / "migrations"), "migrations"),
        (str(ROOT / "schema.sql"), "."),
    ],
    hiddenimports=[
        # FastAPI/uvicorn 全家（字符串 import 抓不到的）
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
        # app 包（uvicorn.run("app.main:app") 字符串导入，PyInstaller 抓不到）
        "app.main",
        "app.domain.events",
        "app.domain.task",
        "app.security.auth",
        "app.security.permission",
        "app.security.approval_gate",
        "app.storage.event_log",
        "app.storage.projection",
        "app.storage.user_store",
        "app.services.llm",
        "app.services.requirement_parser",
        "app.services.team_matcher",
        "app.agents.builtin",
        "app.adapters.openclaw",
        "app.adapters.claude_sdk",
        "app.workers.auto_start",
        "app.workers.heartbeat",
        "app.workers.deadline",
        "app.workers.blocker",
        "app.workers.report",
        "app.workers.transfer_escalation",
        "app.workers.auto_agent",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 打包版零依赖：不需要 PostgreSQL 相关（JSONL 模式全功能）
    excludes=["psycopg", "pgvector", "sqlalchemy.dialects.postgresql"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# Windows 用 onefile 单文件（分发最简：一个 .exe，直接压缩或上传）
# mac 保持 on-dir 的 .app（图标/签名链依赖此结构）
if IS_WIN:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="NG-AI-Platform",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        # Windows 无黑窗启动
        console=False,
        disable_windowed_traceback=False,
        icon="app/static/icon.ico" if (ROOT / "app" / "static" / "icon.ico").exists() else None,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="NG-AI-Platform",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,
        disable_windowed_traceback=False,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="NG-AI-Platform",
    )
    app = BUNDLE(
        coll,
        name="NG-AI-Platform.app",
        icon="app/static/icon.icns" if (ROOT / "app" / "static" / "icon.icns").exists() else None,
        bundle_identifier="com.ngplatform.app",
    )
