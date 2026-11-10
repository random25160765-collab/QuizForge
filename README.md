# QFStudio

本机自用的学习台。**对话是前台，题与图是后台。**

单用户、local-first：后端是 FastAPI，前端是手写静态页，数据是一个 SQLite 文件。
同一份后端跑在四种形态里 —— 源码、单文件程序、Windows 桌面外壳、Android APK。

## 跑起来

```bash
make api-venv     # 后端环境（venv + 依赖）
make api-dev      # 构建前端并起服务（前台阻塞）
```

打开 <http://127.0.0.1:8100/chat.html>。顶层 `/` 也跳到这里。

干净克隆是空库：表在首启自动建好，题 0 道。对话与批改要一份模型配置 ——
把 `config/settings.example.json` 复制成 `config/settings.local.json`，填 `ai` 那块
（`baseUrl` / `apiKey` / `model`），或者在设置页「AI」里填。文件是权威，设置页写的就是它。

要它跑在后台：

```bash
nohup make api-dev >/tmp/quizforge.log 2>&1 &
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8100/api/health
```

改了 `theme/` 之后用 `make dev`：起服务，并监听改动自动重建前端。

## 四种形态

| 形态 | 命令 | 产物 / 入口 |
|---|---|---|
| 源码 | `make api-dev` | `127.0.0.1:8100` |
| 本机单文件 | `make package`（或 `make dist-linux`） | `build/dist/quizforge` |
| Windows 桌面 | `make dist` | 桌面上的 `QFStudio Setup <版本>.exe` 与 `quizforge.exe` |
| Android | `make apk` | `QFStudio-<版本>-<日期>.apk`，构建后拷到 Windows 桌面 |

桌面与 Android 跑的是同一份 `api/app`。数据目录：开发时是仓库 `data/`，打包后是
`~/quizforge`（桌面）或应用私有目录（Android）。

## 八个页面

| 页面 | 干什么 |
|---|---|
| **对话** | 主入口。会话树 + 流式回答；题卡、引用、凭条长在消息里 |
| **主页** | 今天的状态：精力带与打卡 |
| **刷题** | 工作台 / 练习 / 组卷 / 复习四个视图 |
| **错题本** | 错题、掌握度、SM2 间隔重复 |
| **知识图谱** | 力导向图：概念、类型化关系、根因诊断 |
| **资料** | 只读来源库：材料、切片、条目与引用 |
| **日程** | 左边计划清单，右边日历；计划拖进日历才排期 |
| **设置** | AI / 记忆 / 联网搜索 / Obsidian / 外观 / 同步 / 日历 / 作息与健康 / 运行环境 / 数据 |

## 数据与配置

| 路径 | 是什么 |
|---|---|
| `data/quizforge.db` | 工作库（SQLite，WAL）：题库、知识图谱、对话、进度、设置镜像 |
| `config/settings.local.json` | 跨机器有意义的设置：AI、搜索、Obsidian、Turso；快照口令 `syncPassphrase` |
| `db/quizforge.db.gz.enc` | 冷备份快照：`make db-snapshot` 写回，`make db-restore` 恢复 |

仓库里是代码与文档；库、快照与本机设置住在上面这几个位置，已被 gitignore。

**跨机器**：设置页「同步」填 Turso 地址与令牌，后台自动对齐
（`api/app/autosync.py`），也可以 `make sync` 手动推拉一次。行级表按 `(表, 主键)`
增量双向同步；对话、分组、消息、附件端到端加密后上传，其余用户表明文。
题库交换另有 `make bank-export` → `bank.json` 与 `make bank-import`。

## 内容与出题

- **资料流水线**（`pipeline/`）：归一 → 切片 → 向量化 → 检索。`make add` 收入新文件，
  `make ops` 看仪表盘，`make images` 把材料里的图也送进检索。
- **知识空间**：材料 → 知识点 → 概念 → 关系；题挂在概念上，`make coverage` 看哪些点
  还缺题，`make coverage-gaps` 列出具体缺口。
- **出题流水线**：`make drive` 串起派工 → 出题 → 独立校验 → 发布 → 打回；
  `make graph-check` 是知识图谱的硬不变量体检。
- 材料目录按只读使用，原文按坐标（行区间 / 页码）回读。

## 它怎么想事情

agent 把推导的边际成本压到近零，验证与审读成了最稀缺的环节，所以这里考的是
「审得出」：四层（识记 / 理解 / 应用 / 迁移）里 L1+L2 出满，L3+L4 少而精、由人精审。
知识空间与出题流水线服务的是同一个问题：下一步该学什么、该出什么题。

前台与后台是同一个上下文：讲到一个点就能推题，答完点「发送」回到对话里，由本页模型
批改、接着讲；资料与笔记反过来给对话当上下文。

## 能力挂载

输入区那颗药丸决定这一版对话能调哪些工具：

| 模式 | 能干什么 |
|---|---|
| **极简** | 纯聊天 |
| **查询** | 只读：笔记、资料、知识图谱、对话树 |
| **学习** | 全部工具（推题、跑 Python、派子代理） |

粒度是「组 × 权限档」（`read` / `propose` / `write` / `exec`），默认学习模式。
会改状态的动作（收藏、标掌握）以提案形式出现，由人按确认。
提问风格由 `lens/` 里的「滤镜」切换（大白话、苏格拉底、费曼等），也可以自己写。

## 常用命令

```bash
make dev          # 起服务 + 监听 theme/ 自动重建前端
make web          # 只重建前端 → api/web/
make api-test     # 后端测试（临时 SQLite 文件）
make test         # 语法检查 + 题库校验 + 前端断言
make check        # 密钥扫描 + 题库校验
make graph-check  # 知识图谱体检
make vendor       # 取回第三方前端库（katex / mermaid / tikzjax / pyodide …）
make share CID=…  # 把一条会话导出成自包含单页
make help         # 全部目标
```

## 目录

```
api/               FastAPI 后端（routers、agent_loop、tools、sync、heavy_deps）
theme/             前端源码（runtime/*.js、pages/*.html、*.css）→ make web → api/web/
tools/             构建（build_web、package）、取源、分享、代码量
pipeline/          资料流水线：归一、切片、向量、图谱、出题调度
build/             PyInstaller 与 Windows 分发脚本
mobile/            Android（Capacitor + Chaquopy），见 mobile/README.md
desktop/           Electron 外壳（Windows 安装包）
obsidian-bridge/   Obsidian 插件：读写本机 vault
obsidian-sync/     Obsidian 插件：vault 云同步
vendor/            第三方前端库与运行时，版本钉死在 tools/vendor.py
lens/              预置提问滤镜
docs/              设计与协议文档
config/            本机设置与示例
db/                冷备份快照
```

## 文档

| 文档 | 定位 |
|---|---|
| [`docs/STATUS.md`](docs/STATUS.md) | 当前状态：形态、题库与图谱规模、未决事项 |
| [`docs/THESIS.md`](docs/THESIS.md) | 业务立论：设计取舍的根据 |
| [`docs/DESIGN.md`](docs/DESIGN.md) | 工程决策 |
| [`docs/TUTORIAL.md`](docs/TUTORIAL.md) | 使用与运维 |
| [`docs/检索与向量化.md`](docs/检索与向量化.md) | 两个书架与 RRF 融合 |
| [`docs/对话树.md`](docs/对话树.md) | 会话树的分叉与回放 |
| [`docs/日程调度.md`](docs/日程调度.md) | 计划、日历与门禁 |
| [`docs/Android打包.md`](docs/Android打包.md) · [`docs/分发工作流.md`](docs/分发工作流.md) | 出包与分发 |
| [`AGENTS.md`](AGENTS.md) | 仓库约定（提交规范、材料只读、题目是投影） |

状态以 `docs/STATUS.md` 为准；那份文档随工作更新，其余文档解释设计与协议。
