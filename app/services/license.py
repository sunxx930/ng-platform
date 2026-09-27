"""离线授权：Ed25519 签名的许可证串 + 30 天试用期。

设计原则（与「本地化/自托管」一致——不依赖任何服务端）：
- 授权串由**发行方私钥**签名，App 里只内置**公钥** → 客户端无法伪造，也无法反推出私钥
- 试用期从**首次启动**起算，写入 NG_HOME 下的两处冗余记录，取最早的那个
  （删一处不能重置；但离线方案对"全删"本质无解，这里只防顺手改）

授权串格式（base64url，方便微信/邮件里复制粘贴）：
    <base64url(payload)>.<base64url(signature)>
payload:
    {"v":1, "tier":"tax", "exp":"2027-09-27", "mid":"<机器码，可空>", "name":"客户名"}
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import subprocess
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

# 发行方公钥：唯一真源在顶层 ng_crypto（代码补丁的启动器也要用它，
# 而启动器不能 import app.*，所以加密逻辑住在 app 外面）。此处保留别名，行为不变。
from ng_crypto import PUBLIC_KEY_HEX, verify_blob  # noqa: E402  （保持既有导入位置）

TRIAL_DAYS = 30
_LIC_FILENAME = "license.txt"


def _base() -> Path:
    return Path(os.environ.get("NG_HOME", Path.home() / ".ng-platform"))


def _lic_dir() -> Path:
    d = _base() / "license"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- 机器码 ----------

def _platform_uuid() -> str:
    """平台级稳定标识。刻意**不掺主机名/MAC**：

    - 主机名用户可改，改了不该让授权失效
    - uuid.getnode() 在拿不到真实网卡时返回**每次随机的假 MAC**（实测连续三次都不同），
      用它会让机器码每次都变、绑定彻底失效
    """
    sysname = platform.system()
    try:
        if sysname == "Darwin":
            out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                                 capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                if "IOPlatformUUID" in line:
                    return line.split('"')[-2]
        elif sysname == "Windows":
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "(Get-CimInstance Win32_ComputerSystemProduct).UUID"],
                                 capture_output=True, text=True, timeout=8).stdout
            if out.strip():
                return out.strip()
    except Exception:      # noqa: BLE001
        pass
    # 兜底：走到这里说明拿不到稳定标识，宁可报错也不要生成"会变的"机器码
    return ""


def machine_id() -> str:
    """稳定的机器码（换硬件才变）。"""
    seed = _platform_uuid() or f"{platform.machine()}-unknown"
    return hashlib.sha256(seed.encode()).hexdigest()[:16].upper()


# ---------- 授权串 ----------

def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify(token: str) -> dict | None:
    """验签并解析授权串。返回 payload dict；任何异常都返回 None（不抛）。"""
    token = (token or "").strip().replace("\n", "").replace(" ", "")
    if "." not in token:
        return None
    body, sig = token.split(".", 1)
    try:
        payload = _b64d(body)
    except Exception:      # noqa: BLE001
        return None
    # sig 解不开/验不过都由 verify_blob 兜住（fail-closed）
    if not verify_blob(payload, sig):
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except Exception:      # noqa: BLE001
        return None


def save_token(token: str) -> dict:
    """写入本地授权串（已脱敏：调用方负责不要把它记进日志）。"""
    info = verify(token)
    if not info:
        return {"ok": False, "reason": "授权串无效或签名不匹配"}
    _lic_dir().joinpath(_LIC_FILENAME).write_text(token.strip(), encoding="utf-8")
    return {"ok": True, "tier": info.get("tier"), "exp": info.get("exp")}


def load_token() -> str:
    f = _lic_dir() / _LIC_FILENAME
    return f.read_text(encoding="utf-8").strip() if f.is_file() else ""


# ---------- 试用期 ----------

def _stamps() -> list[Path]:
    """试用期起算时间的冗余记录（取最早，删一处不能重置）。"""
    d = _lic_dir()
    return [d / "first_run.json", _base() / "data" / ".first_run.json"]


def first_use() -> date | None:
    """税务库的首次使用日。**没用过就返回 None**（试用期还没起算）。

    只有税务数据库收费 → 试用期应当从"真的开始用它"才起算，
    而不是装上 App 就开始算（否则装完放一个月，试用期白烧）。
    """
    earliest = None
    for p in _stamps():
        if not p.is_file():
            continue
        try:
            dt = datetime.strptime(json.loads(p.read_text(encoding="utf-8"))["first_run"], "%Y-%m-%d").date()
        except Exception:      # noqa: BLE001
            continue
        if earliest is None or dt < earliest:
            earliest = dt
    return earliest


def note_use() -> None:
    """税务库被真正用到时调用（建索引 / 检索命中）。首次调用即起算试用期。"""
    start = first_use() or date.today()
    for p in _stamps():          # 两处冗余，删一处不能重置
        try:
            if not p.is_file():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps({"first_run": start.isoformat()}), encoding="utf-8")
        except Exception:        # noqa: BLE001
            pass


# ---------- 对外状态 ----------

def status() -> dict:
    """当前授权状态。tax 版据此决定知识库是否可读。

    state: licensed（有有效授权）/ trial（试用中）/ expired（试用到期）
    """
    today = date.today()
    info = verify(load_token())
    if info:
        exp = info.get("exp") or ""
        try:
            if exp and datetime.strptime(exp, "%Y-%m-%d").date() >= today:
                mid = info.get("mid") or ""
                if mid and mid != machine_id():
                    return {"state": "expired", "reason": "授权绑定的是另一台机器",
                            "machine_id": machine_id()}
                days = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
                return {"state": "licensed", "tier": info.get("tier"),
                        "exp": exp, "days_left": days, "machine_id": machine_id()}
        except Exception:        # noqa: BLE001
            pass
        return {"state": "expired", "reason": "授权已过期", "exp": exp,
                "machine_id": machine_id()}

    start = first_use()
    if start is None:
        # 还没用过税务库 → 试用期未起算（App 本身免费，不受影响）
        return {"state": "inactive", "days_total": TRIAL_DAYS, "machine_id": machine_id()}
    left = TRIAL_DAYS - (today - start).days
    if left > 0:
        return {"state": "trial", "trial_start": start.isoformat(),
                "days_left": left, "days_total": TRIAL_DAYS, "machine_id": machine_id()}
    return {"state": "expired", "reason": f"试用期 {TRIAL_DAYS} 天已满",
            "trial_start": start.isoformat(), "machine_id": machine_id()}


def _trial_key() -> bytes | None:
    """内置试用密钥（方案 A：试用即全量，随包下发）。

    无服务器时，知识包随安装包下发；试用要能用，密钥就必须在客户机器上。
    因此这是**软控制**——防顺手拷贝，不防破解（用户 2026-09-27 已知悉并选定）。
    密钥文件由打包脚本从 ~/.secrets 落进来，**不落进代码仓库**。
    """
    cands: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cands.append(Path(meipass) / "trial_pack.key")
    if getattr(sys, "frozen", False):
        cands.append(Path(sys.executable).resolve().parent / "trial_pack.key")
    cands.append(Path(__file__).resolve().parent.parent.parent / "data" / "trial_pack.key")
    for p in cands:
        try:
            if p.is_file():
                return p.read_bytes()[:32]
        except Exception:      # noqa: BLE001
            continue
    return None


def pack_key() -> bytes | None:
    """税务知识包的解密密钥。

    优先用**授权串里带的**密钥（付费授权）；没有授权串时回落到**内置试用密钥**，
    但受试用期约束：到期（expired）就不给密钥 → 解不开新包。
    """
    info = verify(load_token())
    if info:
        k = str(info.get("pk") or "")
        if k:
            return bytes.fromhex(k)
    if status().get("state") in ("licensed", "trial", "inactive"):
        return _trial_key()
    return None


def kb_unlocked() -> bool:
    """**税务知识库**是否可读（App 本身不受此限，免费）。

    licensed / trial 期内 → 可读；inactive（没用过）也放行，由首次检索触发起算。
    """
    return status().get("state") in ("licensed", "trial", "inactive")
