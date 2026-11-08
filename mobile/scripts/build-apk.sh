#!/usr/bin/env bash
# 一键出 APK：前端 → 题库快照 → Python 源 → Gradle。
#
# 用法：
#   bash mobile/scripts/build-apk.sh                 # release（自签，可长期升级）
#   bash mobile/scripts/build-apk.sh --debug         # debug（debug 签名，最快）
#   bash mobile/scripts/build-apk.sh --with-sync     # 顺带把 Turso 配置打进包（含密钥！）
#   bash mobile/scripts/build-apk.sh --skip-web      # 前端没改时省一次构建
#
# 每一步为什么这么做写在对应脚本的头部；这里只负责顺序与失败即停。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ANDROID="$ROOT/mobile/android"

BUILD_TYPE="release"
WITH_SYNC=0
SKIP_WEB=0
for arg in "$@"; do
  case "$arg" in
    --debug) BUILD_TYPE="debug" ;;
    --release) BUILD_TYPE="release" ;;
    --with-sync) WITH_SYNC=1 ;;
    --skip-web) SKIP_WEB=1 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

export JAVA_HOME="${JAVA_HOME:-$HOME/.local/jdk21}"
export ANDROID_HOME="${ANDROID_HOME:-$HOME/.local/android}"
GRADLE="${GRADLE:-$HOME/.local/gradle/gradle-8.14.3/bin/gradle}"

[ -x "$JAVA_HOME/bin/java" ] || { echo "缺 JDK：先跑 mobile/scripts/bootstrap-toolchain.sh" >&2; exit 1; }
[ -x "$GRADLE" ] || { echo "缺 Gradle：先跑 mobile/scripts/bootstrap-toolchain.sh" >&2; exit 1; }

if [ "$SKIP_WEB" = "0" ]; then
  echo "→ 前端（make web）"
  ( cd "$ROOT" && make web )
fi

echo "→ 题库快照"
"$ROOT/api/.venv/bin/python" "$ROOT/mobile/scripts/export-bank-db.py"

echo "→ Python 源目录"
bash "$ROOT/mobile/scripts/assemble-python.sh"
find "$ANDROID/app/src/main/python" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

echo "→ 前端产物 → assets/web"
mkdir -p "$ANDROID/app/src/main/assets/web"
rsync -a --delete "$ROOT/api/web/" "$ANDROID/app/src/main/assets/web/"

if [ "$WITH_SYNC" = "1" ]; then
  echo "→ 同步配置（含密钥，仅自用包）"
  "$ROOT/api/.venv/bin/python" "$ROOT/mobile/scripts/inject-sync-assets.py"
fi

echo "→ Gradle assemble${BUILD_TYPE^}"
( cd "$ANDROID"
  printf 'sdk.dir=%s\n' "$ANDROID_HOME" > local.properties
  "$GRADLE" "assemble${BUILD_TYPE^}" --no-daemon --max-workers=4 )

APK="$ANDROID/app/build/outputs/apk/$BUILD_TYPE/app-$BUILD_TYPE.apk"
echo
ls -lh "$APK"
if [ "$BUILD_TYPE" = "release" ]; then
  echo "签名："
  "$ANDROID_HOME/build-tools/36.0.0/apksigner" verify --print-certs "$APK" | head -4
fi
echo
echo "安装：adb install -r $APK"
