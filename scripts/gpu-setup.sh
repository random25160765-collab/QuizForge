#!/usr/bin/env bash
#
# quizforge · GPU 那条路的依赖准备与环境设置
#
# ## 为什么单独一个脚本，而不是塞进 requirements.txt
#
# CUDA 那几个 wheel 加起来 1.4G，**只对装了 N 卡的机器有意义** —— 把它写进所有机器的
# 依赖清单里，等于让每台 CPU 机器都背 1.4G 的无用下载。但这条路又必须"可重复执行、
# 可体检、有据可查"：换台机器、重装环境、或者半年后回来看"当时到底装了什么"，
# 都得有地方查。所以装什么、怎么验、写进哪个配置，全在这一个地方。
#
# ## 用法
#
#     scripts/gpu-setup.sh --check      只体检，不动任何东西
#     scripts/gpu-setup.sh              装依赖 + 写本机配置（config/embed.local.json）
#     scripts/gpu-setup.sh --with-fp16  顺带取 fp16 模型（1.1G，清单里有 sha256 校验）
#     scripts/gpu-setup.sh --cpu        反过来：本机配置切回 CPU（int8）
#
# ## 它按顺序做什么
#
#   1. 看得见卡吗（`nvidia-smi` / `/dev/dxg` / `libcuda.so` —— WSL 里这三样缺一不可）
#   2. 装 `onnxruntime-gpu` 与 CUDA 运行库（pip；先卸掉 CPU 版的 `onnxruntime`）
#   3. 验 provider：ORT 真能列出 `CUDAExecutionProvider` 才算成
#   4. 把"这台机器该用什么"写进 `config/embed.local.json`（变体 + provider）
#   5. 可选：按清单取 fp16 模型
#
# 为什么第 4 步必须落到文件：这次切 fp16 全靠命令行上的环境变量，于是**换个终端再跑一次
# `pipeline.embed`，它就会拿 int8 把整库重算回去**（记账名一换，所有片都判成 stale_model）。
# 机器相关的东西不该取决于"你今天怎么敲的命令"。
set -euo pipefail

cd "$(dirname "$0")/.."
VENV="${VENV:-api/.venv}"
PY="$VENV/bin/python"
PIP="$VENV/bin/pip"
WANT_FP16=0
MODE="setup"

for arg in "$@"; do
  case "$arg" in
    --check) MODE="check" ;;
    --cpu) MODE="cpu" ;;
    --with-fp16) WANT_FP16=1 ;;
    -h|--help) sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "不认识的参数：$arg（--check / --cpu / --with-fp16）" >&2; exit 2 ;;
  esac
done

[ -x "$PY" ] || { echo "找不到 $PY —— 先建虚拟环境：python3 -m venv api/.venv && api/.venv/bin/pip install -r api/requirements.txt" >&2; exit 1; }

echo "== 1. 看得见卡吗 =="
GPU_VISIBLE=0
if command -v nvidia-smi >/dev/null 2>&1; then
  NAME="$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | head -1 || true)"
  if [ -n "$NAME" ]; then echo "   卡：$NAME"; GPU_VISIBLE=1; else echo "   nvidia-smi 在，但报不出卡" >&2; fi
else
  echo "   没有 nvidia-smi（没装驱动，或这台机器没有 N 卡）" >&2
fi
if [ -e /dev/dxg ]; then echo "   /dev/dxg 在（WSL 的 GPU 通道）"; else echo "   /dev/dxg 不在 —— WSL 里没透传 GPU，先看 Windows 侧的显卡驱动" >&2; fi
if ls /usr/lib/wsl/lib/libcuda.so* >/dev/null 2>&1; then echo "   /usr/lib/wsl/lib/libcuda.so 在"; fi

echo "== 2. 依赖 =="
if [ "$MODE" = "check" ]; then
  echo "   （--check：不动依赖）"
else
  "$PIP" show onnxruntime >/dev/null 2>&1 && { echo "   卸掉 CPU 版的 onnxruntime（它和 GPU 版同名，留着会互相盖）"; "$PIP" uninstall -y onnxruntime >/dev/null; }
  echo "   装 onnxruntime-gpu 与 CUDA 运行库（约 1.4G，第一次会慢）…"
  "$PIP" install -q --upgrade onnxruntime-gpu nvidia-cuda-runtime-cu12 nvidia-cublas-cu12 nvidia-cudnn-cu12
fi
if [ "$MODE" = "cpu" ]; then
  echo "   装回 CPU 版 onnxruntime…"
  "$PIP" uninstall -y onnxruntime-gpu >/dev/null 2>&1 || true
  "$PIP" install -q --upgrade onnxruntime
fi

echo "== 3. 验 provider =="
PROVIDERS="$("$PY" -c 'import onnxruntime as o; print(" ".join(o.get_available_providers()))' 2>/dev/null || echo "")"
echo "   可用：$PROVIDERS"
case " $PROVIDERS " in
  *" CUDAExecutionProvider "*)
    if [ "$MODE" = "check" ]; then :; else
      echo "   写本机配置：config/embed.local.json（变体 model_fp16.onnx + CUDA）"
      "$PY" - <<'PYCODE'
import json, sys
from pathlib import Path
sys.path.insert(0, "api")
from app.local_embed import local_config_path
path = local_config_path()
path.parent.mkdir(parents=True, exist_ok=True)
current = {}
if path.is_file():
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        current = {}
current.update({"onnx": "model_fp16.onnx", "provider": "CUDAExecutionProvider",
                "note": "scripts/gpu-setup.sh 写的：本机有卡，用 fp16 走 GPU"})
path.write_text(json.dumps(current, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
print("   →", path)
PYCODE
    fi
    ;;
  *) echo "   ⚠ 没有 CUDAExecutionProvider。要么这台机器没卡，要么依赖没装全。" >&2 ;;
esac
if [ "$MODE" = "cpu" ]; then
  "$PY" - <<'PYCODE'
import json, sys
from pathlib import Path
sys.path.insert(0, "api")
from app.local_embed import local_config_path
path = local_config_path()
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps({"onnx": "model.onnx", "provider": "CPUExecutionProvider",
                            "note": "scripts/gpu-setup.sh --cpu 写的：这台机器走 CPU"},
                           ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
print("   →", path)
PYCODE
fi

echo "== 4. 模型 =="
if [ "$WANT_FP16" = "1" ]; then
  echo "   按清单取模型（已有就跳过，逐个校验 sha256）…"
  "$PY" -c 'import sys; sys.path.insert(0, "api"); from app import local_embed; ok, detail = local_embed.ensure(log=lambda m: print("     " + m)); print("     ", detail); raise SystemExit(0 if ok else 1)'
else
  echo "   （要取 fp16 模型就加 --with-fp16）"
fi

echo
echo "== 现在这台机器会怎么跑 =="
"$PY" - <<'PYCODE'
import sys
sys.path.insert(0, "api")
from app import local_embed
print("   变体：", local_embed.onnx_file())
print("   记账名：", local_embed.model_label())
print("   本机配置：", local_embed.local_config_path())
PYCODE
echo
echo "接着可以：api/.venv/bin/python -m pipeline.ops bench    # 量一遍速度与 provider"
echo "           api/.venv/bin/python -m pipeline.ops plan    # 看现在该开几条车道"
