#!/usr/bin/env python3
"""从 CHANGELOG.md 生成官网「更新日志」静态页（changelog.html）。

用法（发版后跑一次，提交产物即可）:
  python3 scripts/gen_changelog_html.py
产出: website/changelog.html 与 ./changelog.html（官网根）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
try:
    import markdown
except Exception:
    print("需 pip install markdown", file=sys.stderr); sys.exit(1)

CSS = """
:root{--bg:#0b0f1a;--panel:#151b2b;--text:#eef0f6;--muted:#98a1b5;--accent:#6d7cff;--accent2:#4facfe}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'PingFang SC','Songti SC',system-ui,sans-serif;background:var(--bg);color:var(--text);line-height:1.7;padding:34px 20px}
.wrap{max-width:760px;margin:0 auto}
a{color:var(--accent2);text-decoration:none}
.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px;font-size:14px;color:var(--muted)}
h1{font-size:24px;margin-bottom:6px}
h2{font-size:18px;margin:26px 0 6px;padding-bottom:6px;border-bottom:1px solid #262f45;color:#fff}
h3{font-size:15px;margin:14px 0 4px;color:var(--accent2)}
p{margin:4px 0}
ul{margin:4px 0 4px 22px}
li{margin:2px 0}
strong{color:#fff}
code{background:#202a44;padding:1px 5px;border-radius:5px;font-size:.9em}
"""
MD = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
body = markdown.markdown(MD, extensions=["tables", "fenced_code"])
html = ("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>更新日志 · NG AI Platform™</title><style>" + CSS + "</style></head><body>"
        "<div class='wrap'><div class='top'><a href='index.html'>← 返回首页</a>"
        "<span>NG AI Platform™</span></div>" + body + "</div></body></html>")
for out in (ROOT / "website" / "changelog.html", ROOT / "changelog.html"):
    out.write_text(html, encoding="utf-8")
    print(f"生成 {out}")
