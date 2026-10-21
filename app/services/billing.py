"""收费模式：单档按天计费 + 30 天试用 + 离线授权串（**内置在 App 里**）。

产品口径（与「本地化/自托管」一致，用户 2026-10-21 定）：
- App 本身免费；**税务知识库**（法规库 + 案例库 + 后续更新）收费
- 试用 30 天，从**法规库真正启用**才起算（见 updater._install_blob → note_use）
- **按天收费**：1 元/天，客户买多少天就转多少（天数 × 1 元），授权串有效期 = 付费天数
  （`scripts/issue_license.py --days N`）；价格改这里或 NG_HOME/billing.json，不用重打包
- 收款走转账，客户把**机器码**发来，发行方用 scripts/issue_license.py 签发授权串，
  客户在 App 的「税务版」里粘贴激活 —— 零服务端、离线可用

对外只暴露 status()；界面据此显示：未启用 / 试用中 / 已授权 / 已到期。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# ---------- 可配置项（改这里，或放 NG_HOME/billing.json 覆盖） ----------

PLANS = [
    {
        "id": "tax-daily",
        "name": "税务知识库 · 按天计费",
        "price": "¥1/天",             # 按天收费；可被 NG_HOME/billing.json 覆盖
        "price_note": "购买天数 × 1 元，授权有效期 = 付费天数",
        "period": "天",
        "days": 1,
        "min_days": 1,
        "tier": "tax",
        "features": [
            "法规条文库：按「税种 × 规则 × 主题」三层检索",
            "案例库：真实案例与实务处理",
            "后续法规更新：随官网热更推送，无需重装",
            "全程离线：本地检索，数据不上传",
        ],
    },
]

CONTACT = {
    "label": "联系开通",
    "wechat": "",                     # 例如 "ng-tax"（留空则界面不显示）
    "email": "support@ng-platform.ai", # 授权/开通联系邮箱
    "qr": "pay_qr.png",               # 私人收款码图片（微信/支付宝），放在 data/ 下
    "note": "扫码转账后把「机器码」发给发行方，收到授权串后在下方粘贴激活。",
}

PURCHASE_STEPS = [
    "确定购买天数（1 元/天），复制下方机器码联系发行方",
    "扫私人收款码转账（天数 × 1 元），备注你的机器码",
    "收到授权串后，粘贴到底部输入框并点「激活」",
    "激活后点「启用税务库」，法规库与案例库随更新自动升级",
]


def _overrides() -> dict:
    """NG_HOME/billing.json 可覆盖价格/联系方式（不改代码、不重打包）。"""
    p = Path(os.environ.get("NG_HOME", Path.home() / ".ng-platform")) / "billing.json"
    try:
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:      # noqa: BLE001
        pass
    return {}


def plans() -> list[dict]:
    ov = _overrides()
    return ov.get("plans") or PLANS


def contact() -> dict:
    ov = _overrides()
    return {**CONTACT, **(ov.get("contact") or {})}


def purchase_steps() -> list[str]:
    ov = _overrides()
    return ov.get("purchase_steps") or PURCHASE_STEPS


def qr_path() -> Path | None:
    """私人收款码图片。默认 data/pay_qr.png；打包后也能从 _MEIPASS / 可执行文件旁找到。

    收款码是**发行方个人信息**，不随内容热更下发（不放进 ui/）：只随安装包/本地配置走。
    """
    name = str(contact().get("qr") or "pay_qr.png")
    cands: list[Path] = []
    if os.path.isabs(name):
        cands.append(Path(name))
    else:
        ng_home = Path(os.environ.get("NG_HOME", Path.home() / ".ng-platform"))
        cands += [ng_home / "data" / name,
                  Path(__file__).resolve().parent.parent.parent / "data" / name]
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            cands.append(Path(meipass) / name)
            cands.append(Path(meipass) / "data" / name)
        exe = Path(sys.executable).resolve().parent
        cands.append(exe / name)
        cands.append(exe / "data" / name)
    for p in cands:
        if p.is_file():
            return p
    return None


def status() -> dict:
    """收费 + 启用状态，界面一次拿全。"""
    from app.services.license import TRIAL_DAYS, status as lic_status, kb_unlocked

    kb = Path(os.environ.get("NG_HOME", Path.home() / ".ng-platform")) / "knowledge"
    # v2 按分片落盘，没有整包文件名可看 → 用 .installed_* 标记（v1 兼容旧整包）
    try:
        from app.services.kb_semantic import installed_versions
        installed = installed_versions()
    except Exception:      # noqa: BLE001
        installed = sorted(p.stem for p in (kb / "content").glob("*.enc")) if (kb / "content").is_dir() else []
    regs_installed = False
    try:
        from app.services.updater import regs_installed as _ri
        regs_installed = _ri(kb)
    except Exception:      # noqa: BLE001
        pass

    lic = lic_status()
    return {
        **lic,
        "trial_days_total": TRIAL_DAYS,
        "machine_id": lic.get("machine_id") or "",
        # 内容是否已装（法规库/案例库各为一个补丁）
        "installed": installed,
        "regs_installed": regs_installed,
        # 税务库当前能不能用：授权/试用有效，且装卸载了内容
        "tax_usable": bool(installed) and kb_unlocked(),
        "plans": plans(),
        "contact": contact(),
        "purchase_steps": purchase_steps(),
        "qr_available": qr_path() is not None,
    }
