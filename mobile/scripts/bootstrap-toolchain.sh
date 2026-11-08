#!/usr/bin/env bash
# 把 Android 构建工具链装到 ~/.local（不用 sudo、不碰系统目录）。
#
# 为什么不用 Android Studio：这里只需要「命令行能出 APK」，SDK 的 GUI 部分
# 反而多占几个 G。装完之后 ~/.local 下的布局是：
#
#   ~/.local/jdk21/           JDK 21（AGP 8.13 要求 17+，Capacitor 8 要求 21）
#   ~/.local/android/         ANDROID_HOME
#     cmdline-tools/latest/   sdkmanager / avdmanager
#     platforms/android-36/   compileSdk
#     build-tools/36.0.0/     aapt2 / apksigner
#     platform-tools/         adb
#     emulator/ + system-images/   仅在 --with-emulator 时装
#   ~/.local/gradle/          Gradle 8.14.3 解压版
#
# 为什么要单独下 Gradle：`gradlew` 会去 services.gradle.org 拉发行包，本机
# 实测 SSL 握手失败（fake-ip）。腾讯镜像的 zip 可以用 curl 拉，于是干脆
# 用解压版，完全绕开 wrapper 的下载器。
set -euo pipefail

JDK_URL="https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.5%2B11/OpenJDK21U-jdk_x64_linux_hotspot_21.0.5_11.tar.gz"
CMDLINE_URL="https://dl.google.com/android/repository/commandlinetools-linux-13114758_latest.zip"
GRADLE_URL="https://mirrors.cloud.tencent.com/gradle/gradle-8.14.3-all.zip"

PREFIX="${PREFIX:-$HOME/.local}"
DL="$PREFIX/dl"
WITH_EMULATOR=0
[ "${1:-}" = "--with-emulator" ] && WITH_EMULATOR=1

mkdir -p "$DL" "$PREFIX"

need() { [ -e "$1" ] && echo "· 已有 $1" && return 1 || return 0; }

if need "$PREFIX/jdk21/bin/java"; then
  echo "→ JDK 21"
  curl -L --retry 3 -o "$DL/jdk21.tar.gz" "$JDK_URL"
  rm -rf "$PREFIX/jdk21"; mkdir -p "$PREFIX/jdk21"
  tar -xzf "$DL/jdk21.tar.gz" -C "$PREFIX/jdk21" --strip-components=1
fi

if need "$PREFIX/android/cmdline-tools/latest/bin/sdkmanager"; then
  echo "→ Android commandline-tools"
  curl -L --retry 3 -o "$DL/cmdline-tools.zip" "$CMDLINE_URL"
  rm -rf "$PREFIX/android/cmdline-tools"
  mkdir -p "$PREFIX/android/cmdline-tools"
  unzip -q "$DL/cmdline-tools.zip" -d "$PREFIX/android/cmdline-tools"
  mv "$PREFIX/android/cmdline-tools/cmdline-tools" "$PREFIX/android/cmdline-tools/latest"
fi

if need "$PREFIX/gradle/gradle-8.14.3/bin/gradle"; then
  echo "→ Gradle 8.14.3"
  curl -L --retry 3 -o "$DL/gradle.zip" "$GRADLE_URL"
  rm -rf "$PREFIX/gradle"; mkdir -p "$PREFIX/gradle"
  unzip -q "$DL/gradle.zip" -d "$PREFIX/gradle"
fi

export JAVA_HOME="$PREFIX/jdk21"
export ANDROID_HOME="$PREFIX/android"
SDKM="$PREFIX/android/cmdline-tools/latest/bin/sdkmanager"

echo "→ SDK 组件"
yes | "$SDKM" --licenses >/dev/null 2>&1 || true
PKGS=("platform-tools" "platforms;android-36" "build-tools;36.0.0")
[ "$WITH_EMULATOR" = "1" ] && PKGS+=("emulator" "system-images;android-35;google_apis;x86_64")
"$SDKM" "${PKGS[@]}"

cat <<EOF

✓ 工具链就绪
  JAVA_HOME=$JAVA_HOME
  ANDROID_HOME=$ANDROID_HOME
  gradle: $PREFIX/gradle/gradle-8.14.3/bin/gradle

提示：Gradle 拉 Maven 依赖时如果卡住（本机会卡），给它一个 HTTP 代理：
  export GRADLE_OPTS="-Dhttps.proxyHost=127.0.0.1 -Dhttps.proxyPort=7890"
EOF
