#!/usr/bin/env bash
# 内测版（源码运行形态）：把服务拉起来 —— **幂等**，已经在跑就什么都不做。
#
# 谁在用：Windows 桌面那个「QuizForge 内测版.vbs」（它在 WSL 里调本脚本），
# 也可以自己跑：
#
#     bash tools/beta-serve.sh
#
# 注意这是**源码运行形态**（仓库 + WSL 里的 uvicorn），与打包产物
# （`make package` / `dist-release` 出来的单文件）无关 —— 那边自带一份完整运行时，
# 不需要 WSL，也不该由这个脚本去起。
#
# 日志默认写到 /tmp/quizforge-beta.log；端口用 QF_BETA_PORT 覆盖。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${QF_BETA_PORT:-8100}"
LOG="${QF_BETA_LOG:-/tmp/quizforge-beta.log}"
PATTERN='uvicorn app.main:app'

cd "$ROOT"

if pgrep -f "$PATTERN" >/dev/null 2>&1; then
  echo "已在运行（$PATTERN）—— 不动它。日志：$LOG"
  exit 0
fi

# 前端是静态产物：**不构建就会看到上一次的样子**（Makefile 里记着这个坑）
make -s web
echo "前端已构建（api/web）。"

cd "$ROOT/api"
setsid nohup .venv/bin/uvicorn app.main:app \
  --host 0.0.0.0 --port "$PORT" >>"$LOG" 2>&1 </dev/null &
echo "已拉起 uvicorn（端口 $PORT）。日志：$LOG"
