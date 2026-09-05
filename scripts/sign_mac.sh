#!/usr/bin/env bash
# NG-AI-Platform macOS 签名 + 公证 + DMG 打包脚本
#
# 前置（已完成 / 一次即可）：
#   1. Developer ID Application 证书已导入钥匙串（mingze sun，Team S2LRJPCP5W）
#   2. 公证凭据已存钥匙串档案（只需一次）:
#        xcrun notarytool store-credentials NG_PLATFORM \
#          --apple-id "<你的Apple ID邮箱>" \
#          --team-id S2LRJPCP5W
#
# 用法（必须在【你自己的终端、普通用户】下跑，不能 sudo/root —— root 拿不到
#      keychain 私钥 ACL，会报 errSecInternalComponent）：
#        bash scripts/sign_mac.sh
#
# 可选环境变量覆盖默认值：
#   TEAM_ID / CERT / STAGE_BASE / FORCE_REPACK=1（强制重打包）
#   旧式公证传参：APPLE_ID + APP_PW
#
# 产物: dist/NG-AI-Platform.dmg（已 Developer ID 签名 + 公证 + 钉票据）
#
# 为什么用暂存目录：若项目在 iCloud「桌面与文稿」同步的路径下（如 ~/Desktop/ng-platform），
# 文件提供者守护进程会把 com.apple.FinderInfo / com.apple.fileprovider.* 写回 .app 文件夹，
# codesign 报 "resource fork, Finder information, or similar detritus not allowed"。
# 解决：复制到同步范围外的暂存目录（默认 ~/.ng-sign-staging）再签名/打 DMG，最后把 DMG 拷回。

set -euo pipefail
cd "$(dirname "$0")/.."

# ---------- 硬性前置检查 ----------
if [ "$(id -u)" -eq 0 ]; then
  echo "[sign] ✗ 正在以 root 运行。root 无法用你的 keychain 私钥签名（errSecInternalComponent）。"
  echo "[sign]   请打开【普通终端】（不要 sudo），cd 到本目录后再跑。"
  exit 1
fi

# ---------- 默认凭据（可用环境变量覆盖） ----------
TEAM_ID="${TEAM_ID:-S2LRJPCP5W}"
CERT="${CERT:-Developer ID Application: mingze sun (S2LRJPCP5W)}"
PROFILE="${PROFILE:-NG_PLATFORM}"          # notarytool keychain 档案名
APPLE_ID="${APPLE_ID:-}"                   # 旧式 env 传参（可选）
APP_PW="${APP_PW:-}"
STAGE_BASE="${STAGE_BASE:-$HOME/.ng-sign-staging}"   # 须在 iCloud 同步范围之外

APP_NAME="NG-AI-Platform"
APP_PATH="dist/${APP_NAME}.app"
DMG_NAME="${APP_NAME}.dmg"

# 证书必须真的在钥匙串里（cert+私钥配对）
if ! security find-identity -p codesigning -v 2>/dev/null | grep -q "$CERT"; then
  echo "[sign] ✗ 钥匙串里找不到 Developer ID 证书: $CERT"
  echo "[sign]   请先在 developer.apple.com 下载 Developer ID Application 证书并双击导入钥匙串。"
  exit 1
fi

# ---------- 0. 打包（bundle 不存在或 FORCE_REPACK=1 时） ----------
if [ ! -d "$APP_PATH" ] || [ "${FORCE_REPACK:-0}" = "1" ]; then
  echo "[sign] 0/7 重新打包（PyInstaller）…"
  .venv/bin/pyinstaller ng-platform.spec --noconfirm
fi

# ---------- 1. 建暂存目录（iCloud 同步范围外），复制 bundle 过去 ----------
STAGE="$STAGE_BASE/$APP_NAME-build"
echo "[sign] 1/7 暂存到同步范围外: $STAGE"
rm -rf "$STAGE"
mkdir -p "$STAGE"
# 普通 cp 不带 xattr/FinderInfo；再补一次 xattr -cr 兜底
cp -R "$APP_PATH" "$STAGE/$APP_NAME.app"
xattr -cr "$STAGE/$APP_NAME.app" 2>/dev/null || true
STAGED_APP="$STAGE/$APP_NAME.app"
STAGED_DMG="$STAGE/$DMG_NAME"
if xattr -r -l "$STAGED_APP" 2>/dev/null | grep -qiE 'FinderInfo|ResourceFork|fileprovider'; then
  echo "[sign] ✗ 暂存副本仍带 detritus 属性，中止。请手动检查: xattr -r -l \"$STAGED_APP\""
  exit 1
fi

# ---------- 2. 签名（hardened runtime + 时间戳，公证必需） ----------
echo "[sign] 2/7 Developer ID 签名…"
codesign --force --deep --verbose \
  --timestamp --options runtime \
  --sign "$CERT" "$STAGED_APP"
codesign --verify --deep --strict --verbose=2 "$STAGED_APP"
echo "[sign] 签名验证 OK"
codesign -dv "$STAGED_APP" 2>&1 | grep -iE '^TeamIdentifier|^Signature' || true

# ---------- 3. 打 DMG ----------
echo "[sign] 3/7 打 DMG…"
rm -f "$STAGED_DMG"
hdiutil create -volname "$APP_NAME" -srcfolder "$STAGED_APP" \
  -ov -format UDZO "$STAGED_DMG"

# ---------- 4. 公证（notarytool，通常几分钟） ----------
# 不探测钥匙串条目（root 读不到用户 genp，且 notarytool 存法会变），
# 直接交给 notarytool 用自己的档案名解析；失败再给存储提示。
if [ -n "$APPLE_ID" ] && [ -n "$APP_PW" ]; then
  echo "[sign] 4/7 提交公证（env 传凭据）…"
  xcrun notarytool submit "$STAGED_DMG" \
    --apple-id "$APPLE_ID" --team-id "$TEAM_ID" --password "$APP_PW" \
    --wait
else
  echo "[sign] 4/7 提交公证（keychain 档案: ${PROFILE}，可等几分钟）…"
  set +e
  xcrun notarytool submit "$STAGED_DMG" \
    --keychain-profile "$PROFILE" \
    --wait
  rc=$?
  set -e
  if [ $rc -ne 0 ]; then
    echo "[sign] 公证提交失败（rc=${rc}）。"
    echo "[sign]   若提示 'No Keychain password item found for profile: ${PROFILE}'，"
    echo "[sign]   请先在你自己的终端存一次档案（密码不会外泄）："
    echo "     xcrun notarytool store-credentials $PROFILE \\"
    echo "       --apple-id \"<你的Apple ID邮箱>\" \\"
    echo "       --team-id $TEAM_ID"
    exit $rc
  fi
fi

# ---------- 5. 钉入公证票据 ----------
echo "[sign] 5/7 钉入公证票据…"
xcrun stapler staple "$STAGED_DMG"

# ---------- 6. 最终验证（挂载 DMG，验里面的 .app） ----------
# 说明: 直接在 DMG 文件上跑 spctl --type open 常误报 rejected（DMG 不是代码签名对象）。
# 权威做法: 挂载后对 .app 做 Gatekeeper 在线评估（应显示 source=Notarized Developer ID）。
hdiutil detach "/Volumes/$APP_NAME" >/dev/null 2>&1 || true
echo "[sign] 6/7 最终验证（挂载 DMG 验 app，在线查 Apple）…"
if hdiutil attach -nobrowse -readonly "$STAGED_DMG" >/dev/null 2>&1; then
  APP_IN_DMG="/Volumes/$APP_NAME/$APP_NAME.app"
  if [ -d "$APP_IN_DMG" ]; then
    if codesign --verify --deep --strict "$APP_IN_DMG" >/dev/null 2>&1; then
      echo "[sign] 代码签名结构验证 OK"
    else
      echo "[sign] ⚠ 代码签名结构验证失败！"
    fi
    set +e
    spctl --assess --type execute --verbose=4 "$APP_IN_DMG"
    s=$?
    set -e
    if [ $s -eq 0 ]; then
      echo "[sign] Gatekeeper 验证通过 ✅"
    else
      echo "[sign] ⚠ Gatekeeper 在线评估未通过（离线会误报）。公证已 Accepted 且票据已钉，建议有网环境下双击人工确认。"
    fi
  else
    echo "[sign] ⚠ 挂载成功但找不到 app，跳过自动评估"
  fi
  hdiutil detach "/Volumes/$APP_NAME" >/dev/null 2>&1 || true
else
  echo "[sign] ⚠ 挂载 DMG 失败，跳过自动评估（公证已 Accepted + 票据已钉）"
fi

# ---------- 7. 拷回仓库 dist ----------
echo "[sign] 7/7 拷回: dist/$DMG_NAME"
rm -f "dist/$DMG_NAME"
cp "$STAGED_DMG" "dist/$DMG_NAME"

echo "[sign] ✅ 完成！可分发包: dist/$DMG_NAME"
