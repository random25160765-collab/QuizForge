---
name: quizforge-pipeline
description: quizforge 出题流水线的运行手册——权威在哪（数据库）、状态机怎么转（派工→出题→校验→发布→打回重出）、一条命令 `make drive` 怎么跑、失败题为什么必须有归宿、覆盖率怎么看、新机器怎么上手，以及常见的"卡住"该怎么判断与处理。当需要跑流水线、补材料缺口、查为什么没进度、理解某道题现在处于什么状态、或接手一个已经有数据的 quizforge 时使用。
---

# quizforge 流水线手册

## 一句话前提

**题库、考纲、切片、图清单、知识点、点候选的权威全在 Postgres。**
仓库里**没有**题目文件、没有 `maps/`、没有 `meta/topics.yaml` —— 那些都已经入库。
所以：**不要去找文件，去查库**（`make coverage` / `python -m pipeline.status`）。

数据要进版本库的唯一形态是快照：`db/quizforge.sql.gz`（`make db-dump` / `make db-restore`）。

## 状态机（一张图）

```
材料(只读) ──ingest──▶ 切片表 ──dispatch extract──▶ 抽取 ▶ point_candidates
                                                      ▼
                                   resolve ▶ knowledge_points + point_edges + point_sources
                                                      ▼
              dispatch author ──▶ 出题员 ▶ questions(status=draft) ──▶ 校验员
                                                                        │
                     pass ──▶ status=verified ──▶ promote ──▶ published │
                     fail ──▶ 留在 draft ──▶ rework：重出（带缺陷清单）或退役 ⏹
```

- **状态**：`draft`（草稿）→ `verified`（过校验）→ `published`（对外）；`retired_at` 是终态标记 ✗
- **任务表**（`pipeline/state.sqlite3`）只是**索引**：删了能重建，不进版本库 ✗
- **题↔点的边**（`question_points`）在**校验通过时**才写 ✗ —— 覆盖率与防重复都靠它 ✔

## 一条命令跑完全流程

```bash
make drive                                   # 出题 → 校验 → 发布 → 打回重出，收敛即停
make drive ARGS="--material ViT-TTNN-vit_bh --iterations 2"
make drive ARGS="--iterations 40 --per-round-materials 5"   # 大批量推进（可中断可续）
make drive ARGS="--dry-run"                  # 只看它要做什么
```

一轮 = 派工（缺口最大的几份）→ worker → promote → rework。**收敛判据**：队列空 **且** 本轮没有新发布/新退役。

## 常用命令

| 目的 | 命令 |
|---|---|
| 看哪些材料还有缺口 | `make coverage` · 单份 `make coverage-gaps MATERIAL=xxx` |
| 补缺口派工 | `python -m pipeline.dispatch --material <slug> --kind all` |
| 执行（出题/校验） | `python -m pipeline.worker --kind all --concurrency 3 --max 12` |
| 发布（过闸门） | `python -m pipeline.promote --apply` |
| 失败题归宿 | `python -m pipeline.rework --apply`（重出 or 退役） |
| 救回"只差一点"的题 | `python -m pipeline.fix --limit 8 --apply --recheck` |
| 队列/账本 | `python -m pipeline.status` |
| 材切片入库（新材料第一步） | `python -m pipeline.ingest <file-or-dir> --subject tt-metal` |
| 知识空间同步（历史 maps 用） | `python -m pipeline.dbsync` |
| 考纲增删改（库为正） | `python -m app.cli.topics tree / add / rename / move / retire / export`（在 `api/` 下） |
| 校验构建 | `make check` · `make test` · `make api-test` |
| 数据库快照 | `make db-dump` / `make db-restore` |

## 不可违反的六条

1. **权威在库** ✗：任何"顺手写个题目文件"的做法都是在造第二个真相 ✔
2. **校验独立** ✗：换会话、重读原文、**不许看题目的 `## 解析`** ✔
3. **失败题必须有归宿** ✗：要么重出（带缺陷清单）✗、要么退役 ✔ —— 不许烂在 `draft` 里 ✔
   （跑完一轮就 `make drive` 或 `python -m pipeline.rework --apply`，别让草稿越积越多 ✗）
4. **编号在库内分配** ✗（咨询锁 + 最大值 ✗），不许扫目录取号 ✔
5. **退役而不是删除** ✗：题目 id 是稳定资产，删了就解释不了"这道题去哪了" ✔
6. **数据库必须推送** ✔：`db/quizforge.sql.gz` 是唯一的数据形态 ✔

## 新机器上手

```bash
git clone … && make db-up && make db-restore   # 起库 + 灌数据
make check && make test                        # 题库与前端自检
make api-dev                                   # 后端 → http://127.0.0.1:8100
```

## 常见"卡住"的判据

| 现象 | 先看什么 | 大概率原因与处理 |
|---|---|---|
| 队列不动 | `python -m pipeline.status` | 没有 worker 在跑 ✗ → 起一个（`--max 12` 小步跑 ✔）|
| 草稿越积越多 | `make coverage` 的"草稿"列 | 没跑 `rework` ✗ → 跑它 ✔ |
| 覆盖率不动 | `make coverage-gaps MATERIAL=…` | 缺口点的材料太薄 ✗（`thickness` 低 ✗）或已重出满 2 轮 ✔ |
| 出题总被打回 | 看 `questions.verify_report` 的 `problems` ✗ | 材料代码密集、作者在猜 ✗ → 提高门槛（题少而准 ✗）比硬推更值 ✔ |
| `make check` 报"话题未定义" | 该 key 还没进考纲 ✗ | 用 `app.cli.topics add` 补叶子 ✔（点 key 进考纲后，防重复判据才成立 ✔）|

## 指路

- 出题契约与层 skill：`quizforge-author` / `quizforge-l1..l4` ✔
- 项目定位与铁律：`quizforge-init` ✔
- 当前进展：`docs/STATUS.md` ✔
