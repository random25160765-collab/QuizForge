#!/usr/bin/env bash
# 一键出 APK：前端 → Python 源 → Gradle。
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

# 安装包里不放数据：题库走 Turso 整包（app/bank_cloud.py），新设备首启拉一次。
# 快照不管哪天打，第二天就过期；跟着包发出去只会让每台设备多背 62MB 旧数据。
echo "→ 题库：不随包（首启从云端整包拉）"
if [ -e "$ANDROID/app/src/main/assets/bank.db" ]; then
  echo "assets/bank.db 不该存在：安装包只放代码，题库走云端同步" >&2
  exit 1
fi

echo "→ Python 源目录"
bash "$ROOT/mobile/scripts/assemble-python.sh"
find "$ANDROID/app/src/main/python" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

echo "→ 前端产物 → assets/web"
mkdir -p "$ANDROID/app/src/main/assets/web"
rsync -a --delete "$ROOT/api/web/" "$ANDROID/app/src/main/assets/web/"

# Pyodide 的校验清单（3KB）必须随包：手机没有 vendor/ 副本，只能按清单下载，
# 而 heavy_deps 见到空清单就不敢下（校验不了）。缺了它 = "跑 Python"永远不可用，
# 所以这里宁可让构建失败，也不要出一个功能残缺还看不出来的包。
echo "→ Python 运行时清单 → assets/runtime"
if [ ! -f "$ROOT/build/pyodide-manifest.json" ]; then
  echo "缺 build/pyodide-manifest.json —— 先跑 python3 build/package.py manifest" >&2
  exit 1
fi
mkdir -p "$ANDROID/app/src/main/assets/runtime"
cp "$ROOT/build/pyodide-manifest.json" "$ANDROID/app/src/main/assets/runtime/pyodide-manifest.json"

# TikZJax 的载荷必须是 `.gz`（引擎里写死 `fetch('core.dump.gz')`），但 AGP 在
# mergeAssets 时会把 assets 里的 `.gz` **解压并去掉后缀** —— 实测 core.dump.gz
# 变成 160MB 的 core.dump、633 个 tex_files/*.gz 全失去后缀，引擎一个都取不到，
# 手机上所有 TikZ 图都会变成 tikzjax-broken。进包前改成 `.gzz` 骗过它，
# 启动时再改回来（android_boot.restore_tikzjax_gz）。
echo "→ TeX 引擎载荷改名（避开 AGP 对 .gz 的解压）"
TIKZ="$ANDROID/app/src/main/assets/web/assets/tikzjax"
if [ -d "$TIKZ" ]; then
  renamed=0
  while IFS= read -r -d '' f; do
    mv "$f" "$f"z
    renamed=$((renamed + 1))
  done < <(find "$TIKZ" -name '*.gz' -print0)
  left=$(find "$TIKZ" -name '*.gz' | wc -l | tr -d ' ')
  echo "  .gz → .gzz：$renamed 个（剩余 .gz：$left）"
  if [ "$left" -ne 0 ]; then
    echo "还有 .gz 没改名，会被 AGP 解压" >&2
    exit 1
  fi
fi

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
