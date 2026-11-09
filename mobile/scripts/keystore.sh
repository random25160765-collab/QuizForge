#!/usr/bin/env bash
# 生成自签 release keystore 与口令文件 —— 都在仓库外。
#
#   ~/.config/quizforge-android/release.jks
#   ~/.config/quizforge-android/keystore.properties
#
# `app/build.gradle` 通过 QF_KEYSTORE_PROPERTIES（或默认路径）读后者；
# 两个文件都不进 git 的原因见 .gitignore 的注释。
#
# 重新生成 = 换签名 = 老版本无法覆盖安装（Android 只允许同签名的升级）。
# 所以这个脚本在文件已存在时拒绝执行，除非显式 --force。
set -euo pipefail

DEST="${DEST:-$HOME/.config/quizforge-android}"
KEYTOOL="${KEYTOOL:-$HOME/.local/jdk21/bin/keytool}"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

if [ -f "$DEST/release.jks" ] && [ "$FORCE" = "0" ]; then
  echo "· keystore 已存在：$DEST/release.jks（要重建加 --force，会让老包无法升级）"
  exit 0
fi

if [ ! -x "$KEYTOOL" ]; then
  echo "找不到 keytool：$KEYTOOL —— 先跑 scripts/bootstrap-toolchain.sh" >&2
  exit 1
fi

mkdir -p "$DEST"
PASS="$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 24)"
cat > "$DEST/keystore.properties" <<EOF
storeFile=$DEST/release.jks
storePassword=$PASS
keyAlias=quizforge
keyPassword=$PASS
EOF
chmod 600 "$DEST/keystore.properties"

"$KEYTOOL" -genkeypair -v \
  -keystore "$DEST/release.jks" -alias quizforge \
  -keyalg RSA -keysize 2048 -validity 10950 \
  -storepass "$PASS" -keypass "$PASS" \
  -dname "CN=QFStudio, OU=Personal, O=QFStudio, L=Unknown, ST=Unknown, C=CN"

echo "✓ $DEST/release.jks"
echo "  口令在同目录的 keystore.properties（600）；丢了就只能换签名重装。"
