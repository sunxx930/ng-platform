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


def apply_knowledge_pack(knowledge_dir: Path, *, keep_encrypted: bool = True) -> dict:
    """拉「知识包」并按需落地到 knowledge_dir。

    与 UI 热更的区别：
    - 单个大文件（加密归档），不是逐文件 → 扩展名白名单不适用，改为整包 sha256 校验
    - 体积大 → 支持断点续传（下载到 .part，续传，校验通过才原子改名）
    - 解密不在这里做：本函数只保证「正确、完整地把包放到该在的位置」

    manifest 里对应字段：
        "knowledge": {"version": "2026-09", "url": "...", "sha256": "...", "size": 123}
    返回 {applied, version, reason}。
    """
    knowledge_dir = Path(knowledge_dir)
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    m = _manifest()
    pack = m.get("knowledge") or {}
    url = str(pack.get("url", "") or "")
    version = str(pack.get("version", "") or "")
    want = str(pack.get("sha256", "") or "").lower()
    if not url:
        return {"applied": False, "version": version, "reason": "manifest 无知识包"}

    cur = knowledge_dir / "version.json"
    if cur.exists() and version:
        try:
            if json.loads(cur.read_text(encoding="utf-8")).get("version") == version:
                return {"applied": False, "version": version, "reason": "已是最新"}
        except Exception:  # noqa: BLE001
            pass

    part = knowledge_dir / "kb.pack.part"
    try:
        _download(url, part, resume=True)
        blob = part.read_bytes()
        if want and hashlib.sha256(blob).hexdigest() != want:
            return {"applied": False, "version": version,
                    "reason": "sha256 校验失败（已保留 .part 供续传）"}
        enc_path = knowledge_dir / "kb.pack.enc"
        enc_path.write_bytes(blob)

        # 解密需要授权里的包密钥；没有授权则只落地密文，等激活后再解
        import re as _re
        from app.services.license import pack_key
        key = pack_key()
        if not key:
            return {"applied": True, "version": version, "reason": "已下载，待激活授权后解密"}
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        plain = AESGCM(key).decrypt(blob[:12], blob[12:], None)
        import io as _io
        import zipfile as _zip
        # 解压到临时目录再原子搬入，避免半截状态
        tmp = Path(tempfile.mkdtemp(prefix=".kb-", dir=str(knowledge_dir)))
        with _zip.ZipFile(_io.BytesIO(plain)) as zf:
            for name in zf.namelist():
                rel = name.replace("\\", "/").lstrip("/")
                if not rel or ".." in rel.split("/") or ":" in rel:
                    continue
                dst = (tmp / rel).resolve()
                try:
                    dst.relative_to(tmp.resolve())
                except ValueError:
                    continue
                if name.endswith("/"):
                    dst.mkdir(parents=True, exist_ok=True)
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(zf.read(name))
        for sub in ("tax-cases", "index", "models"):
            src = tmp / sub
            if not src.is_dir():
                continue
            tgt = knowledge_dir / sub
            old = knowledge_dir / (sub + ".old")
            if tgt.exists():
                os.replace(tgt, old)
            os.replace(src, tgt)
            import shutil as _sh
            _sh.rmtree(old, ignore_errors=True)
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)
        part.unlink(missing_ok=True)
        cur.write_text(json.dumps({"version": version}, ensure_ascii=False), encoding="utf-8")
        return {"applied": True, "version": version, "reason": "ok"}
    except Exception as e:  # noqa: BLE001
        return {"applied": False, "version": version, "reason": f"失败: {e}"}


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
