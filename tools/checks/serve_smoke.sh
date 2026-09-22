#!/usr/bin/env bash
# 服务起来之后，验它是不是**真的能用**。
#
# ## 三件事，都是"用户拿到手能不能用"的判据
#
#   1. `/api/health` 200 —— 在监听；
#   2. 题库不是空壳，且**接口给的与库里对得上** —— 快照真的恢复出来了；
#   3. 入口一致：顶层 `/` 跳对话页，而对话页能开。
#
# ## 为什么是这三件
#
# 它们各自都抓到过真问题，而且都是**只有新机器才暴露**的那一类：
#
#   * **空壳库** —— 拉下来能起服务、页面也开得出来，但题库是 0，用户一脸茫然；
#   * **快照恢复失败** —— `data/` 目录在新机器上不存在，恢复那一步直接崩；
#   * **三个入口各指一页** —— `/` 跳刷题、`make api-dev` 提示笔记、
#     README 说对话是主入口；单独看都对，合起来"打开首页看到的不是首页"。
#
# `make test` 跑在"已经跑起来的机器"上，这几类一条都抓不到 ——
# 所以这条脚本是给**干净环境**用的，CI 里就挂在"照 README 跑完"的后面。
#
# ## 用法
#
#     bash tools/checks/serve_smoke.sh [base_url]     # 默认 http://127.0.0.1:8100
#
# 退出码非零 = 有问题（CI 直接当闸门用）。它会**等服务就绪**（最多两分钟），
# 所以可以紧接着起服务就调它。

set -euo pipefail

BASE="${1:-http://127.0.0.1:8100}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DB="$ROOT/data/quizforge.db"

fail() { printf '✗ %s\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------- 1) 在监听
#
# 最多等 `SMOKE_WAIT` 秒（默认 120 —— CI 里冷启动要时间，起服务与调它是两步）。
# 手工验的时候把它调小，别拿一个根本不会有人监听的地址去试、
# 然后陪着它等满两分钟：`SMOKE_WAIT=4 bash tools/checks/serve_smoke.sh http://127.0.0.1:9999`
WAIT="${SMOKE_WAIT:-120}"
code=""
for _ in $(seq 1 $((WAIT / 2))); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/health" || true)
  [ "$code" = "200" ] && break
  sleep 2
done
[ "$code" = "200" ] || fail "/api/health 返回 ${code:-（连不上）} —— 等了 ${WAIT} 秒仍未就绪"
echo "✓ /api/health 200"

# ------------------------------------------------- 2) 题库不是空壳，且与库一致
[ -f "$DB" ] || fail "没有库：$DB —— 先跑 make db-restore"
python3 - "$DB" "$BASE" <<'PY'
import json
import sqlite3
import sys
import urllib.request

db_path, base = sys.argv[1], sys.argv[2]
conn = sqlite3.connect(db_path)
live = conn.execute(
    "SELECT COUNT(*) FROM questions"
    " WHERE retired_at IS NULL AND status IN ('verified','published')"
).fetchone()[0]
topics = conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
served = json.load(urllib.request.urlopen(base + "/api/bank"))["questions"]

print(f"  库：现行题 {live} · 主题 {topics}；接口：{len(served)} 题")
if live <= 0:
    raise SystemExit("✗ 库里一道题都没有 —— 快照没恢复出来")
if topics <= 0:
    raise SystemExit("✗ 库里没有考纲 —— 快照不完整")
if len(served) != live:
    raise SystemExit(f"✗ 接口与库对不上：接口 {len(served)} ≠ 库 {live}")
print("✓ 题库与库一致")
PY

# ---------------------------------------------------------------- 3) 入口一致
index=$(curl -s "$BASE/")
printf '%s' "$index" | grep -q 'url=\./chat\.html' \
  || fail "顶层 / 没有跳向对话页 —— 它是「对话是前台」的落点（见 README 与 tools/build_web.py）"
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/chat.html")
[ "$code" = "200" ] || fail "/chat.html 返回 $code"
echo "✓ 首页 → chat.html → 200"

echo "全部通过"
