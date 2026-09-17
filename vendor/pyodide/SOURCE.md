# vendored Pyodide

- version: 0.26.4
- source: vendor/pyodide
- 运行时: 5 个文件
- 预置包: 3 个（62MB） —— numpy, scipy
- synced_at: 2026-09-17T21:35:33

由 `tools/vendor.py` 生成，请勿手工修改。
用途：对话里的 run_python 工具在沙箱 iframe 里真跑 Python；
包必须与 pyodide.js 同目录 —— Pyodide 按 indexURL 找 .whl。
