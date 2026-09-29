#!/usr/bin/env bash
# 内测版（源码运行形态）：把服务拉起来 —— **幂等**，已经在跑就什么都不做。
#
# 谁在用：Windows 桌面那个「QuizForge 内测版.vbs」（它在 WSL 里调本脚本），
# 也可以自己跑：
#
#     bash tools/beta-serve.sh
#
# 注意这是**源码运行形态**（仓库 + WSL 里的服务端进程），与打包产物
# （`make package` / `dist-release` 出来的单文件）无关 —— 那边自带一份完整运行时，
# 不需要 WSL，也不该由这个脚本去起。
#
# ## 只允许一个 quizforge
#
# 端口 8100 一次只能被一个进程绑住，这是硬约束；这里额外做的是**友好地检测**，
# 把"已经有别人在服务"和"端口被无关进程占了"两种情况分开说清楚，而不是让
# 第二个人撞上 `address already in use`。检查顺序：
#
#   1. 8100 上有没有 quizforge 在应答（不管是本脚本、`make api-dev`，
#      还是**桌面那个打包版 exe** 起的）—— 有就不动它；
#   2. 有没有同命令的进程活着（比如正在启动、端口还没起来）—— 有也不动；
#   3. 端口被别的进程占着 —— 报清楚，别硬起。
#
# 日志默认写到 /tmp/quizforge-beta.log；端口用 QF_BETA_PORT 覆盖。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${QF_BETA_PORT:-8100}"
LOG="${QF_BETA_LOG:-/tmp/quizforge-beta.log}"
PATTERN='uvicorn app.main:app'
HEALTH="http://127.0.0.1:${PORT}/api/health"

# ---- 说一句话：**同时**上屏与进日志 -----------------------------------------
#
# 为什么要有这个：Windows 桌面那个「QuizForge 内测版.vbs」是**隐藏窗口**跑的
# （`sh.Run cmd, 0, False`），所以本脚本的话从前只落在那个看不见的 stdout 上，
# 写进日志的**只有 uvicorn 的输出**。于是"起不来"的时候，用户会等满 60 秒、
# 看到一个让他去看日志的弹窗，而日志是**空的** —— 2026-09-28 实测那次：
# `/tmp/quizforge-beta.log` 创建于 22:42 却是 0 字节，什么都没留下。
# 让每句话都进日志，那个指向日志的弹窗才有意义。
#
# `tee -a`：照旧上屏（手动 `bash tools/beta-serve.sh` 时看得见），同时追加进日志。
say() {
  printf '%s %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$LOG"
}

cd "$ROOT"

# ---- 1. 已经在服务了？（谁起的都算）---------------------------------------
if curl -fsS --max-time 2 "$HEALTH" >/dev/null 2>&1; then
  say "8100 上已经有一个 quizforge 在服务（可能是打包版，或 make api-dev）—— 不动它。"
  exit 0
fi

# ---- 2. 同命令的进程还在（正在启动 / 端口未就绪）--------------------------
if pgrep -f "$PATTERN" >/dev/null 2>&1; then
  say "已有一个同命令的进程在跑 —— 不动它。日志：$LOG"
  exit 0
fi

# ---- 3. 端口被无关进程占着 -------------------------------------------------
if (exec 3<>"/dev/tcp/127.0.0.1/${PORT}") 2>/dev/null; then
  exec 3>&- 2>/dev/null || true
  say "端口 ${PORT} 被别的进程占着（不是 quizforge）—— 先释放它，或用 QF_BETA_PORT 换一个。"
  exit 1
fi

# ---- 起 -------------------------------------------------------------------
# 前端是静态产物：**不构建就会看到上一次的样子**（Makefile 里记着这个坑）
make -s web
say "前端已构建（api/web）。"

cd "$ROOT/api"
setsid nohup .venv/bin/uvicorn app.main:app \
  --host 0.0.0.0 --port "$PORT" >>"$LOG" 2>&1 </dev/null &
say "已拉起服务（端口 $PORT）。日志：$LOG"
