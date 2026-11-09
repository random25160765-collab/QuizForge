#!/usr/bin/env bash
# 把仓库里的后端装配成 Chaquopy 能吃的 Python 源目录。
#
# 为什么是"拷"而不是把 srcDirs 指到仓库里：Chaquopy 会把整个 srcDir 编成字节码
# 塞进 APK，仓库根（带着 reference/ 软链、data/ 250M 的库、vendor/ 146M 的 Pyodide）
# 显然不能整个进去。这里只挑运行期真正需要的部分。
#
# 清单依据（实测过，不是猜的）：
#   * api/app/**  —— 后端本体，排除 __pycache__ 与 cli/（只有命令行入口，routers 不 import）
#   * tools/{check,question_parser,topics,dataset}.py —— toolkit.py 与 bank_import.py
#     通过 `from check import …` 直接用，四个文件的级联关系已核过
#   * yaml/      —— PyYAML 没有 Android wheel，拷纯 Python 部分（去掉 _yaml*.so 后
#     PyYAML 自动退回纯 Python 实现）
#   * android_boot.py —— 手写启动器，从 mobile/python_src 拷
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEST="$ROOT/mobile/android/app/src/main/python"
VENV="$ROOT/api/.venv"

if [ ! -d "$VENV" ]; then
  echo "缺少 $VENV —— 先跑 make api-venv" >&2
  exit 1
fi

echo "→ 清理 $DEST"
rm -rf "$DEST"
mkdir -p "$DEST"

echo "→ api/app"
# 末尾斜杠不能少：`rsync src dst`（目标不存在时）在有的版本上会再套一层目录，
# 结果是 APK 里变成 app/app/db.pyc —— 症状是运行时 `No module named 'app.db'`。
mkdir -p "$DEST/app"
rsync -a --exclude '__pycache__' --exclude '*.pyc' --exclude 'cli' "$ROOT/api/app/" "$DEST/app/"

echo "→ tools（4 个）"
mkdir -p "$DEST/tools"
for name in check question_parser topics dataset; do
  cp "$ROOT/tools/$name.py" "$DEST/tools/$name.py"
done

echo "→ 纯 Python yaml"
YAML_SRC="$(ls -d "$VENV"/lib/python*/site-packages/yaml 2>/dev/null | head -1)"
if [ -z "${YAML_SRC:-}" ]; then
  echo "找不到 PyYAML 的 yaml 包（$VENV）" >&2
  exit 1
fi
mkdir -p "$DEST/yaml"
cp "$YAML_SRC"/*.py "$DEST/yaml/"
# 纯 Python 版只需要 py 文件；__init__ 会自己找 _yaml，找不到就用 Python 实现。

echo "→ android_boot.py"
cp "$ROOT/mobile/python_src/android_boot.py" "$DEST/android_boot.py"

# 冒烟：装配结果必须能被桌面 Python import（不启动服务），否则 Android 上只会更糟。
PYTHON="${PYTHON:-$VENV/bin/python}"
"$PYTHON" - "$DEST" <<'PY'
import sys
from pathlib import Path

dest = Path(sys.argv[1])
sys.path.insert(0, str(dest))
sys.path.insert(0, str(dest / "tools"))

import yaml  # noqa: F401  —— 纯 Python 版能 import
import check  # noqa: F401  —— tools 目录挂进来后 `from check import …` 成立
import question_parser  # noqa: F401
import topics  # noqa: F401
import dataset  # noqa: F401

# 只 import 包本身，不触发 Settings（那要求 QF_DATA_DIR 存在）。
import app  # noqa: F401

print("smoke ok:", yaml.__file__)
PY

echo "✓ 装配完成：$(find "$DEST" -name '*.py' | wc -l) 个 .py"
