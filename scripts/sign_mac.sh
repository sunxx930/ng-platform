#!/usr/bin/env bash
# NG-AI-Platform macOS 签名 + 公证 + DMG 打包脚本（2026-09-02）
#
# 前提（已付 Apple Developer Program $99/年）：
#   1. 在 developer.apple.com 生成 Developer ID Application 证书，导入钥匙串
#   2. 在 appleid.apple.com 生成 App 专用密码（需开双重认证）
#   3. 在 developer.apple.com → Membership 拿 Team ID
#
# 用法（填好下方变量后）:
#   bash scripts/sign_mac.sh
#   或
#   APPLE_ID=you@email.com TEAM_ID=XXXX TEAM_ID=XXXXXXXXXX \
#     CERT="Developer ID Application: Your Name (TEAMID)" \
#     APP_PW=xxxx-xxxx-xxxx-xxxx bash scripts/sign_mac.sh
#
# 产物: dist/NG-AI-Platform.dmg（已签名 + 公证 + 钉票据，可直接分发）

set -euo pipefail
cd "$(dirname "$0")/.."

# ---------- 凭据（也可用环境变量传入，避免写死进脚本） ----------
APPLE_ID="${APPLE_ID:?需设 Apple ID 邮箱}"
TEAM_ID="${TEAM_ID:?需设 Team ID（10 位）}"
CERT="${CERT:?需设 Developer ID Application 证书名，如 'Developer ID Application: 公司 (TEAMID)'}"
APP_PW="${APP_PW:?需设 App 专用密码（appleid.apple.com 生成）}"

APP_NAME="NG-AI-Platform"
APP_PATH="dist/${APP_NAME}.app"
DMG_PATH="dist/${APP_NAME}.dmg"

# ---------- 0. 先打包（若 .app 不存在或强制重打） ----------
if [ ! -d "$APP_PATH" ]; then
  echo "[sign] .app 不存在，先打包…"
  .venv/bin/pyinstaller ng-platform.spec --noconfirm
fi

# ---------- 1. 签名（hardened runtime + 时间戳，公证必需） ----------
echo "[sign] 1/4 签名…"
codesign --deep --force --verify --verbose \
  --timestamp --options runtime \
  --sign "$CERT" "$APP_PATH"
codesign --verify --deep --strict "$APP_PATH"
echo "[sign] 签名验证 OK"

# ---------- 2. 打 DMG ----------
echo "[sign] 2/4 打 DMG…"
rm -f "$DMG_PATH"
hdiutil create -volname "$APP_NAME" -srcfolder "$APP_PATH" \
  -ov -format UDZO "$DMG_PATH"

# ---------- 3. 公证（notarytool，5-15 分钟） ----------
echo "[sign] 3/4 提交公证（可能等几分钟）…"
xcrun notarytool submit "$DMG_PATH" \
  --apple-id "$APPLE_ID" \
  --team-id "$TEAM_ID" \
  --password "$APP_PW" \
  --wait

# ---------- 4. 钉入公证票据 ----------
echo "[sign] 4/4 钉入公证票据…"
xcrun stapler staple "$DMG_PATH"
spctl --assess --type open --context context:primary-signature "$DMG_PATH" \
  && echo "[sign] Gatekeeper 验证通过 ✅" || echo "[sign] 提示: spctl 可能需 --ignore-cache"

echo "[sign] ✅ 完成！可分发包: $DMG_PATH"
