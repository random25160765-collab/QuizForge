"""quizforge 出题流水线（自建执行器，不用第三方 agent 框架）。

设计见 `.codebuddy/skills/quizforge-author/references/pipeline.md`：
任务表 + 租约 + 幂等 + 账本；编排是「出题单元内串联、单元间并联」。

运行方式（复用后端虚拟环境，不新增依赖）：

    api/.venv/bin/python -m pipeline.dispatch --map maps/tt-metal/METALIUM_GUIDE
    api/.venv/bin/python -m pipeline.worker --concurrency 4
    api/.venv/bin/python -m pipeline.status
"""
