"""内容热更（升级模式 A）：UI 资产 + 知识包，客户端自动拉取替换。

安全边界（保证客户项目不受影响）：
- UI 包只写 NG_HOME/ui；知识包只写 NG_HOME/knowledge → **绝不碰** data/events/artifacts
- 只允许白名单扩展名 & 无目录穿越；下载→sha256 校验→原子 rename 覆盖
- manifest 默认 https://ng-platform.ai/update.json（可 env NG_UPDATE_MANIFEST 覆盖/测试）
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.request
from pathlib import Path

_ALLOW_EXT = {".html", ".js", ".css", ".svg", ".png", ".ico", ".json", ".woff", ".woff2", ".map"}
_MAX_FILES = 200
_MAX_TOTAL_MB = 80


def manifest_url() -> str:
    return os.environ.get("NG_UPDATE_MANIFEST", "https://ng-platform.ai/update.json")


def _manifest() -> dict:
    data = urllib.request.urlopen(manifest_url(), timeout=10).read()
    return json.loads(data.decode("utf-8"))


def _download(url: str, dest: Path, *, resume: bool = False, timeout: int = 60,
              chunk: int = 1 << 20) -> None:
    """下载到 dest。resume=True 时用 Range 头续传已有部分（知识包可能几百 MB）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    have = dest.stat().st_size if (resume and dest.exists()) else 0
    req = urllib.request.Request(url)
    if have:
        req.add_header("Range", f"bytes={have}-")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        # 服务器不支持断点续传（返回 200 而非 206）→ 从头来，避免拼出错文件
        if have and getattr(resp, "status", 200) != 206:
            have = 0
        mode = "ab" if have else "wb"
        with open(dest, mode) as fh:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                fh.write(buf)


def patch_files(knowledge_dir: Path) -> list[Path]:
    """已下载/已保存的补丁原件（客户从官网下的 *.enc）。"""
    d = Path(knowledge_dir) / "patches"
    return sorted(d.glob("*.enc")) if d.is_dir() else []


# ---------- 案例库闸门（用户 2026-09-27 定）----------
# 「法规库上线后，必须下载法规库才能继续用案例库」。
# 真正的目的是堵试用期漏洞：试用期从**法规库启用**才起算（规则「甲」），
# 只装案例库的客户会永远不烧试用期——强制装法规库 = 强制起算。
_REGS_REQUIRED = ".regs_required"
_REGS_INSTALLED = ".regs_installed"


def regs_required(knowledge_dir: Path) -> bool:
    """法规库是否已上线（manifest 里出现过 knowledge_regs）。落了标记就一直要求。"""
    return (Path(knowledge_dir) / _REGS_REQUIRED).is_file()


def regs_installed(knowledge_dir: Path) -> bool:
    return (Path(knowledge_dir) / _REGS_INSTALLED).is_file()


def refresh_regs_requirement(knowledge_dir: Path) -> bool:
    """查一次 manifest 更新「法规库是否已上线」。取不到就用上次的标记（不误放行）。"""
    knowledge_dir = Path(knowledge_dir)
    try:
        required = bool(_manifest().get("knowledge_regs"))
    except Exception:      # noqa: BLE001
        return regs_required(knowledge_dir)
    if required:
        try:
            knowledge_dir.mkdir(parents=True, exist_ok=True)
            (knowledge_dir / _REGS_REQUIRED).write_text("1", encoding="utf-8")
        except Exception:      # noqa: BLE001
            pass
    return required


def case_gate_reason(knowledge_dir: Path) -> str | None:
    """案例库能不能用。返回 None = 可用；否则返回给用户看的原因。"""
    if regs_required(knowledge_dir) and not regs_installed(knowledge_dir):
        return "法规库已上线，需先下载法规库后才能继续使用案例库"
    return None


# 补丁 zip 里的顶层目录：内容（受保护，永不落明文）与模型（公开，落盘）
_CONTENT_DIRS = ("tax-cases", "index", "tax-regs")


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:64] or "pack"


def _extract_models(zf, names: list[str], models_dir: Path) -> int:
    """模型 → 落盘（公开模型，ONNX 需要文件路径）。"""
    n = 0
    for name in names:
        rel = name.replace("\\", "/").lstrip("/")
        if not rel.startswith("models/") or name.endswith("/"):
            continue
        if ".." in rel.split("/") or ":" in rel:
            continue
        dst = models_dir / rel[len("models/"):]
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(zf.read(name))
        n += 1
    return n


def _shards_dir(knowledge_dir: Path) -> Path:
    return Path(knowledge_dir) / "content" / "shards"


def _drop_legacy_pack(knowledge_dir: Path, kind: str, key: bytes) -> str | None:
    """删掉旧格式（整包）的同类型 content/*.enc —— 否则新旧内容会在检索里叠一遍。

    旧包文件名不带类型，只能解开看内容里有没有 tax-regs/ 或 tax-cases/。只读一次。
    """
    import io as _io
    import zipfile as _zip
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    want = "tax-regs/" if kind == "regs" else "tax-cases/"
    for p in (Path(knowledge_dir) / "content").glob("*.enc"):
        try:
            blob = p.read_bytes()
            plain = AESGCM(key).decrypt(blob[:12], blob[12:], None)
            with _zip.ZipFile(_io.BytesIO(plain)) as zf:
                if any(n.startswith(want) for n in zf.namelist()):
                    p.unlink()
                    return p.name
        except Exception:      # noqa: BLE001
            continue
    return None


def _install_sharded(zf, info: dict, knowledge_dir: Path, key: bytes) -> dict:
    """格式 v2：按分片落盘（content/shards/<id>.enc），只写本次包里带的分片。

    增量包（pack.json 有 base）只带变动分片 + removed 清单；完整包带全部，
    顺带清掉不属于本版的同类分片（内容目录残缺/回滚时能自愈）。
    """
    import io as _io
    import os as _os
    import re as _re
    import zipfile as _zip
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    kind = info.get("kind") or "cases"
    prefix = "r-" if kind == "regs" else "c-"
    sid_re = _re.compile(r"^[rc]-[0-9a-f]{16}$")
    names = zf.namelist()
    shards_dir = _shards_dir(knowledge_dir)
    shards_dir.mkdir(parents=True, exist_ok=True)

    listed: set[str] = set()
    n_shards = n_files = 0
    for s in info.get("shards") or []:
        sid = str(s.get("id") or "")
        if not sid_re.match(sid):
            continue
        listed.add(sid)
        head = f"shards/{sid}/"
        members = [n for n in names if n.startswith(head) and not n.endswith("/")]
        if not members:
            continue
        buf = _io.BytesIO()
        with _zip.ZipFile(buf, "w", _zip.ZIP_DEFLATED, compresslevel=6) as cz:
            for n in members:
                rel = n[len(head):]
                if ".." in rel.split("/") or ":" in rel:
                    continue
                cz.writestr(rel, zf.read(n))
                n_files += 1
        nonce = _os.urandom(12)
        tmp = shards_dir / f".{sid}.tmp"
        tmp.write_bytes(nonce + AESGCM(key).encrypt(nonce, buf.getvalue(), None))
        _os.replace(tmp, shards_dir / f"{sid}.enc")
        n_shards += 1

    for rid in info.get("removed") or []:
        rid = str(rid)
        if sid_re.match(rid):
            try:
                (shards_dir / f"{rid}.enc").unlink(missing_ok=True)
            except Exception:      # noqa: BLE001
                pass

    if not info.get("base"):        # 完整包：清掉不在清单里的同类分片
        for p in shards_dir.glob(f"{prefix}*.enc"):
            if p.stem not in listed:
                try:
                    p.unlink()
                except Exception:      # noqa: BLE001
                    pass

    models_dir = knowledge_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    n_models = _extract_models(zf, names, models_dir)
    legacy = _drop_legacy_pack(knowledge_dir, kind, key)
    return {"shards": n_shards, "files": n_files, "models": n_models,
            "installed_total": len(list(shards_dir.glob(f"{prefix}*.enc"))),
            "total_after": int(info.get("total_after") or 0), "legacy_dropped": legacy}


def _install_blob(blob: bytes, knowledge_dir: Path, version: str) -> dict:
    """装补丁：内存解密 → 模型落盘（公开）→ 内容**重新加密**后落盘。

    ⚠️ 关键：**tax-cases / index / tax-regs 一律不写明文盘**（9/20 定案：内容不可提取）。
    盘上只留密文，检索时在内存解密（见 kb_semantic.load_index）。

    两种格式：
      v1（旧）    content/<补丁>.enc —— 整包
      v2（2026-10-21 起）content/shards/<分片>.enc —— 按源文件分片，增量包只带变动分片

    没有授权/试用密钥时不能解 → 补丁原件存到 patches/ 等激活后再装。
    """
    import io as _io
    import zipfile as _zip

    knowledge_dir = Path(knowledge_dir)
    patches_dir = knowledge_dir / "patches"
    patches_dir.mkdir(parents=True, exist_ok=True)
    patch_path = patches_dir / f"{_safe_name(version)}.enc"
    patch_path.write_bytes(blob)

    from app.services.license import pack_key, note_use
    key = pack_key()
    if not key:
        return {"applied": False, "version": version,
                "reason": "已保存补丁，待激活授权后解密", "patch": str(patch_path)}

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    plain = AESGCM(key).decrypt(blob[:12], blob[12:], None)

    models_dir = knowledge_dir / "models"
    content_dir = knowledge_dir / "content"
    content_dir.mkdir(parents=True, exist_ok=True)
    models_dir.mkdir(parents=True, exist_ok=True)

    with _zip.ZipFile(_io.BytesIO(plain)) as zf:
        names = zf.namelist()
        info = None
        if "pack.json" in names:
            try:
                info = json.loads(zf.read("pack.json"))
            except Exception:      # noqa: BLE001
                info = None
        if info and info.get("format") == 2:
            # 包内版本号才是权威（客户手动下载的文件名可能是任意名）
            version = str(info.get("version") or version)
            res = _install_sharded(zf, info, knowledge_dir, key)
            regs_installed = (info.get("kind") == "regs")
        else:
            res = {"shards": 0, "total_after": 0}
            # v1：内容 → 重新打包再加密成 content/<补丁>.enc（**不落明文**）
            n_models = _extract_models(zf, names, models_dir)
            buf = _io.BytesIO()
            n_content = 0
            with _zip.ZipFile(buf, "w", _zip.ZIP_DEFLATED, compresslevel=6) as cz:
                for name in names:
                    rel = name.replace("\\", "/").lstrip("/")
                    if not rel.startswith(_CONTENT_DIRS) or name.endswith("/"):
                        continue
                    if ".." in rel.split("/") or ":" in rel:
                        continue
                    cz.writestr(rel, zf.read(name))
                    n_content += 1
            import os as _os
            nonce = _os.urandom(12)
            enc = AESGCM(key).encrypt(nonce, buf.getvalue(), None)
            (content_dir / f"{_safe_name(version)}.enc").write_bytes(nonce + enc)
            res["content"] = n_content
            res["models"] = n_models
            regs_installed = any(n.replace("\\", "/").startswith("tax-regs/") for n in names)

    # 试用期起算（用户 2026-09-27 定，甲）：**法规库启用**这一刻才起算。
    # 案例补丁不含 tax-regs/ → 装案例不烧试用期。
    if regs_installed:
        try:
            (knowledge_dir / _REGS_INSTALLED).write_text("1", encoding="utf-8")
        except Exception:      # noqa: BLE001
            pass
        try:
            note_use()
        except Exception:      # noqa: BLE001
            pass

    # 原始补丁是「模型 + 内容」的大包；模型已落盘、内容已转成小密文，原件留着白占 ~250M。
    # 需要重装时从官网重下即可（有 sha256 校验）。
    try:
        patch_path.unlink(missing_ok=True)
    except Exception:      # noqa: BLE001
        pass

    # 已装版本标记：增量包选包与界面「已启用」判断都看它（v2 没有整包文件名可看）。
    # 手动安装（install_patch_file）也走这里，别再依赖下载路径单独写。
    tag = (info or {}).get("kind")
    if tag in ("cases", "regs"):
        try:
            (knowledge_dir / f".installed_{tag}").write_text(version, encoding="utf-8")
        except Exception:      # noqa: BLE001
            pass

    (knowledge_dir / "version.json").write_text(
        json.dumps({"version": version, "regs": regs_installed, "format": 2 if info else 1},
                   ensure_ascii=False),
        encoding="utf-8")
    # 内容变了 → 让检索重建内存索引
    try:
        from app.services import kb_semantic as _kb
        _kb.reset_index_cache()
    except Exception:      # noqa: BLE001
        pass
    return {"applied": True, "version": version, "reason": "ok",
            "regs": regs_installed, **res}


def install_patch_file(src: Path, knowledge_dir: Path) -> dict:
    """装一个**本地补丁文件**（客户从官网下载的 *.enc，案例库/法规库各一个）。

    内容永不落明文盘；模型落盘（公开）。没有授权/试用密钥时先存着，激活后再装。
    """
    knowledge_dir = Path(knowledge_dir)
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    src = Path(src)
    if not src.is_file():
        return {"applied": False, "reason": f"补丁文件不存在: {src}"}
    version = src.stem or "patch"
    try:
        return _install_blob(src.read_bytes(), knowledge_dir, version)
    except Exception as e:      # noqa: BLE001
        return {"applied": False, "version": version, "reason": f"失败: {e}"}


def install_saved_patches(knowledge_dir: Path) -> dict:
    """把 patches/ 下已保存的补丁全部装一遍（激活授权后把之前存下的补丁解出来）。"""
    knowledge_dir = Path(knowledge_dir)
    out = []
    for p in patch_files(knowledge_dir):
        out.append({"patch": p.name, **install_patch_file(p, knowledge_dir)})
    if not out:
        return {"applied": False, "reason": "没有待安装的补丁", "installed": []}
    return {"applied": any(x.get("applied") for x in out), "installed": out}


def apply_knowledge_pack(knowledge_dir: Path, *, keep_encrypted: bool = True) -> dict:
    """拉「知识包」并按需落地到 knowledge_dir。

    与 UI 热更的区别：
    - 单个大文件（加密归档），不是逐文件 → 扩展名白名单不适用，改为整包 sha256 校验
    - 体积大 → 支持断点续传（下载到 .part，续传，校验通过才原子改名）
    - 下载完交给 _install_blob：内存解密 → 模型落盘、内容转密文（不落明文）

    manifest 里对应字段：
        "knowledge": {"version": "2026-09", "url": "...", "sha256": "...", "size": 123}
        # v2 增量段还会带 base（基线版本）与 full（完整包 url/sha256/size）：
        "knowledge_regs": {"version": "regs-…", "url": "<增量包>", "sha256": "…",
                           "size": 123, "base": "regs-…", "full": {"url": "…", …}}
    返回 {applied, version, reason}。
    """
    knowledge_dir = Path(knowledge_dir)
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    m = _manifest()
    # 法规库是否已上线：顺带刷新闸门标记（离线时保留上次判断）
    if m.get("knowledge_regs"):
        try:
            (knowledge_dir / _REGS_REQUIRED).write_text("1", encoding="utf-8")
        except Exception:      # noqa: BLE001
            pass

    out = {"knowledge": _apply_one_pack(knowledge_dir, m.get("knowledge") or {}, "cases")}
    # 法规库上线后必须装它（否则案例库被闸门挡）——所以有就一起拉
    if m.get("knowledge_regs"):
        out["knowledge_regs"] = _apply_one_pack(knowledge_dir, m.get("knowledge_regs") or {}, "regs")
    out["applied"] = any(v.get("applied") for v in out.values() if isinstance(v, dict))
    return out


def _fetch_install(knowledge_dir: Path, entry: dict, tag: str, version: str) -> dict:
    """下载 entry 指向的包并安装（断点续传 + sha256 校验 + .part 清理）。"""
    url = str(entry.get("url", "") or "")
    want = str(entry.get("sha256", "") or "").lower()
    if not url:
        return {"applied": False, "version": version, "reason": "manifest 无该补丁地址"}
    knowledge_dir = Path(knowledge_dir)
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    # .part 按包 sha 命名：增量包和完整包互不干扰，中断后还能续传
    part = knowledge_dir / f"kb-{tag}-{(want or 'x')[:12]}.pack.part"
    try:
        _download(url, part, resume=True)
        blob = part.read_bytes()
        if want and hashlib.sha256(blob).hexdigest() != want:
            part.unlink(missing_ok=True)
            return {"applied": False, "version": version, "reason": "sha256 校验失败"}
        res = _install_blob(blob, knowledge_dir, version)
        if res.get("applied"):
            try:
                (knowledge_dir / f".installed_{tag}").write_text(version, encoding="utf-8")
            except Exception:      # noqa: BLE001
                pass
        # 安装/落盘后 .part 已无用（未授权时内容已在 patches/<version>.enc）
        try:
            part.unlink(missing_ok=True)
        except Exception:      # noqa: BLE001
            pass
        return res
    except Exception as e:  # noqa: BLE001
        return {"applied": False, "version": version, "reason": f"失败: {e}"}


def _apply_one_pack(knowledge_dir: Path, pack: dict, tag: str) -> dict:
    """下载并安装一个补丁（cases / regs 共用）。

    v2 包支持增量：manifest 段里带 base（基线版本）+ full（完整包）时，
    客户端正好在基线上就下增量包（只含变动分片），否则下完整包。
    """
    version = str(pack.get("version", "") or "")
    url = str(pack.get("url", "") or "")
    if not url:
        return {"applied": False, "version": version, "reason": "manifest 无该补丁"}

    mark = knowledge_dir / f".installed_{tag}"
    installed = mark.read_text(encoding="utf-8").strip() if mark.is_file() else ""
    if installed == version:
        return {"applied": False, "version": version, "reason": "已是最新"}

    # 之前已下载但因未授权没装成（存在 patches/<version>.enc）→ 直接装本地，不再重下
    saved = knowledge_dir / "patches" / f"{_safe_name(version)}.enc"
    if saved.is_file():
        res = install_patch_file(saved, knowledge_dir)
        if res.get("applied"):
            try:
                mark.write_text(str(res.get("version") or version), encoding="utf-8")
            except Exception:      # noqa: BLE001
                pass
        return res

    base = str(pack.get("base") or "")
    full = pack.get("full") or {}
    if base and installed != base:
        # 不在增量基线上（首次安装 / 错过版本 / 内容目录被清）→ 用完整包
        if not full.get("url"):
            return {"applied": False, "version": version,
                    "reason": f"需要先安装完整包（当前 {installed or '无'}，增量基线 {base}）"}
        return _fetch_install(knowledge_dir, full, tag, version)

    res = _fetch_install(knowledge_dir, pack, tag, version)
    # 增量装完分片数对不上（内容目录残缺）→ 用完整包重建
    total = res.get("total_after") or 0
    if (res.get("applied") and total and res.get("installed_total") != total
            and full.get("url")):
        res = _fetch_install(knowledge_dir, full, tag, version)
        res["repaired"] = True
    return res


def apply_ui_update(ui_dir: Path) -> dict:
    """拉 manifest 并按需落地到 ui_dir。返回 {applied, version, failed}。"""
    ui_dir = Path(ui_dir)
    ui_dir.mkdir(parents=True, exist_ok=True)
    data = urllib.request.urlopen(manifest_url(), timeout=10).read()
    m = json.loads(data.decode("utf-8"))
    files = m.get("files") or []
    version = str(m.get("version", "") or "")
    applied = failed = 0
    if len(files) > _MAX_FILES:
        raise ValueError(f"升级包文件数超限 > {_MAX_FILES}")
    total_bytes = 0                       # 总体积上限（此前 _MAX_TOTAL_MB 声明了却没用上）
    for f in files:
        path = str(f.get("path", "") or "").replace("\\", "/")
        url = str(f.get("url", "") or "")
        want = str(f.get("sha256", "") or "").lower()
        if not path or not url:
            continue
        if path.startswith("/") or ".." in path.split("/") or ":" in path:
            failed += 1
            continue
        if Path(path).suffix.lower() not in _ALLOW_EXT:
            failed += 1
            continue
        target = (ui_dir / path).resolve()
        try:
            target.relative_to(ui_dir.resolve())
        except ValueError:
            failed += 1
            continue
        raw = urllib.request.urlopen(url, timeout=15).read()
        total_bytes += len(raw)
        if total_bytes > _MAX_TOTAL_MB * 1024 * 1024:
            raise ValueError(f"升级包总体积超限 > {_MAX_TOTAL_MB}MB")
        if want and hashlib.sha256(raw).hexdigest() != want:
            failed += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        # 原子写：同目录临时文件 → os.replace
        fd, tmp = tempfile.mkstemp(prefix=".ui-", dir=str(target.parent))
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
            os.replace(tmp, target)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        applied += 1
    if version:
        (ui_dir / "version.json").write_text(json.dumps({"version": version},
                                                        ensure_ascii=False), encoding="utf-8")
    return {"applied": applied, "failed": failed, "version": version}
