# 当前状态

> **唯一易变的状态落点。** 稳定的约定在 `.codebuddy/skills/`，这里只放会变的东西。
> 开工先读它，收工更新它。
>
> 最后更新：2026-09-16

## 一、题库

- **18 道题 / 11 个学科**，`make check` 基线：`[OK] 18 个文件全部通过（0 个告警）`
- 题型：`single` 7 · `blank` 5 · `short` 3 · `multi` 2 · `problem` 1
- 学科：cpp 3 · cuda 3 · cutlass 3 · rust 2，其余 7 个学科各 1

**已知缺口**

- `layer` / `wing`：18 道里只有 3 道填了（`format.md` 标必填、`check.py` 不校验）
  → **四层覆盖目前不可追踪**，这是题库最大的结构缺口
- `problem` 全库仅 1 道：`questions/cuda/cuda-0003-gemm-roofline-problem.md`（6 小问）
- 同一材料另有 25 问草稿在 `draft/re.md`，可作为它扩写小问的素材（长链条材料就是一个点，出成一道 `problem`，
  见 `from-source.md` §1.5）

## 二、运行环境（最后核实 2026-09-16）

| 组件 | 状态 |
|---|---|
| 后端 uvicorn `127.0.0.1:8100`（宿主 venv，热重载） | 在跑，`/api/health` → 200 |
| PostgreSQL 容器 `quizforge-db-1` | Up（healthy），`127.0.0.1:5432` |
| 产物 | `dist/`（离线单文件）· `api/web/`（在线前端）· `dist-local/`（本机自用版） |

重启：`make db-up` → `make api-dev`。只改了 `theme/` 里的前端时，`make web` 后刷新即可，不用重启。

## 三、材料来源（只读，均在 `.gitignore` 里）

| 路径 | 指向 | 内容 |
|---|---|---|
| `reference/` | `/mnt/f/Documents` | 15 个学科目录，以 PDF 为主：`cuda/`（CUDA 指南、PTX ISA 9.3、SASS 微架构、CuTe layout 论文）· `swe/`（15 篇软件演化论文）· `linux/`（Linux 命令行与 shell 脚本编程大全 第 3 版）· `other/`（AI-Infra-Book）· 其余 11 个目录**尚未盘点** |
| `Codebase/` | `/home/rd/Source/` | QEMU 书稿、CUTLASS、Tenstorrent ISA、Rust 训练等 |

## 四、未决事项（等用户拍板）

1. `layer` / `wing` 要不要在 `check.py` 加校验（现在标着必填却零校验，涉及 15 道存量）
2. `chapter` / `tags` 三处口径不一（`_TEMPLATE.md` 说不需要 · `format.md` 说建议 · `check.py` 缺 `chapter` 会告警）
3. `wing` 的「综合」定义散在四个层 skill 里、措辞各异 → 建议收进 `format.md` 成一张表
4. 五个 skill 里写死的本机路径 `/home/rd/Desktop/quizforge`（仓库已公开）
5. HIG Foundations 逐条体检（UI 收尾，用户原话「明天再说」）

## 五、待建

**先后顺序是硬的**：① 知识空间（清洗切分 → 标注 → 分类）→ ② 检索层（三路召回 + RRF）→
③ 学习算法（含回流：正确率→难度、错答共现→关系强度、IRT/BKT→掌握度，**硬编码实时算法、不用 LLM**）→
④ RAG / 问答。前一步不扎实、后一步就没有东西可做，所以现在的重点只在前两步。

| 东西 | 说明 |
|---|---|
| `tools/relations.py` | 从题目字段解析出五类关系边（易混 / 前置 / 迁移 / 锚定 / 强度），产出 `relations.yaml` |
| 切片脚本 | 按材料形态分档切分，产出 `maps/<材料>/{slices,coverage,figures}.yaml`。图文混排的材料要按原位保留图占位符、把每张图单独记账（见 `references/pipeline.md` §2 ①） |
| `pipeline/` | **自建执行器**（不用第三方 agent 框架）：SQLite 任务表 + 租约 + 三种进程（dispatcher / worker / verifier），幂等靠 `input_hash`，账本记每任务 tokens/耗时（见 `references/pipeline.md` §6） |
| `Retriever` 接口 | 三路召回（向量 / 图 / 规则）+ **RRF 融合** + 评测集。先用 numpy + SQLite 实现，接口定死，之后可换 pgvector / 图数据库（见 §5） |
| `maps/` | 目录尚未创建（机器生成的索引与矩阵；`draft/` 放人写的母本与草稿） |

## 六、已知小问题

- `theme/app.css` 的 `.browsecard:first-of-type` 选择器**失效**：题卡都是 `<div>`，而面板里第一个
  `<div>` 是 `.panel__head`，所以首卡上方的分隔线其实还在。想按原意去掉，改成 `.panel__head + .browsecard`。

## 七、最近的决策

- **2026-09-16** —— **技术栈定案**：所有 LLM 能力走 API（chat / vision / embedding），**不引任何推理框架**
  （把 torch / transformers 那一整片挡在门外）；依赖判据是「重量」——体积、是否要起外部服务、
  传递依赖数，轻则引、重则手搓；三个接口（`LLMClient` / `VisionClient` / `Embedder`）都走 OpenAI 兼容、
  provider 只是配置；embedding 必须本地缓存（索引频繁重建）；账本按能力分别记。落地在 `pipeline.md` §7。
- **2026-09-16** —— 新增 skill **`quizforge-handmade`**（人工手写单题的接入）：agent 不写题面，
  只做格式合规、元数据补全、把关系边写成可解析的句式（前置/易混/迁移/锚定）、校验与入库——
  保证手写题（尤其法式大题）**不进图谱盲区**。已登记进 `skills/README.md` 与 `quizforge-init` 路由表。
- **2026-09-16** —— **编排定案**：出题 + 校验是一个**串联单元**（两个独立会话），**N 个单元并联**；
  L1–L4 一视同仁地并行（不给 L3/L4 单开串行轨）；**只有法式大题一题一议、不进流水线**（人做，
  但产物照常入库并参与关系解析）。「串联」指流程不指上下文——校验员不看 `## 解析`。落地在 §6.5。
- **2026-09-16** —— 定下**先后顺序**：① 知识空间 → ② 检索层 → ③ 学习算法（含回流）→ ④ RAG / 问答。
  回流通道**不涉及语义理解**，硬编码一个实时算法即可（正确率 → 难度标定、错答共现 → 关系强度、
  IRT/BKT → 掌握度），且**当前不设计**——先把知识空间做扎实，后续学习算法与 RAG 才有东西可做。
- **2026-09-16** —— 确立**宏观框架**：自动出题 ≙ 知识库构建流水线（清洗切分 → 标注 → 分类 → 学习），
  终点是一个「知识空间」（点 / 边 / 样本 / 坐标），为学习算法（选题策略）提供基础；同时记下三处
  类比会破的地方（合成样本而非贴标签、闭环回流、验收标准是学习效果）。落地在 `references/pipeline.md` §0.5。
- **2026-09-16** —— 检索层与执行器定案：三路召回（向量语义 / 图逻辑关联 / 规则）+ **RRF 融合**；
  **出题即标注**（题库同时是标注数据集，喂给智适应与知识图谱）；并行调度**自建**（SQLite 任务表 +
  租约 + dispatcher/worker/verifier 三进程），不依赖第三方 agent 框架。落地在 `references/pipeline.md` §5 / §6。
- **2026-09-16** —— 补上**图文混排**的处理约定（图片是一等公民、不许丢）：切片原位保留图占位符、
  `figures.yaml` 单独记账、视觉理解先筛后做（唯一需要多模态的环节）、`kind: figure` 可作知识点、
  用到的图拷进 `assets/figures/` 并由离线构建内联 base64。落地位置：`references/pipeline.md` §2 ① 与 §3。
- **2026-09-16** —— **业务立论落盘** `docs/THESIS.md`：推导被外包、判断被涨薪；
  识别先于推导、审读先于产出。四层分法、A/B 批次、大题分支点、关系靠解析、不引向量库
  都是它的推论（文末有「立论 → 仓库实现」对照表）。
- **2026-09-16** —— 新增开局入口：skill `quizforge-init` + `docs/STATUS.md`（稳定约定进 skill、
  易变状态进 STATUS，新会话两步上手）；`draft/` 纳入版本控制。
- **2026-09-16** —— 出题方针与批量流水线落盘（commit `77dc2da`）：依据优先级（原材料 + 诉求 > 考纲）、
  一条流水线四层共用（派工 → 出题 → 独立校验 → 统合入库）、覆盖矩阵五步、切片分档与四条纪律、
  存储约定、关系靠解析而非标注、暂不引入向量库与图数据库。详见 `references/pipeline.md`。
- **2026-09-15** —— UI 五条规则（间距与相邻一致 / 中心严格对齐 / 同源阴影 / 克制 / 一内容一开关）；
  阴影改用主题 token，`--shadow-2` 成为**唯一**的离地高度来源。深色档待专门设计。
- **2026-09-15** —— 仓库初始化并推送到 GitHub（`78cb998`）。

## 八、跨会话记忆（agent 侧）

三条：`quizforge UI 风格偏好（苹果 HIG 取向）` · `quizforge 出题方针` · `quizforge 出题流水线`。
开新 session 时用户会一并带上；仓库里对应的落盘位置见上面第七节。
