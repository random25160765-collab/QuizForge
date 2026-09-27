---
name: quizforge-ingest
description: 把一份**新材料**（PDF / 书 / 网页）入库到 quizforge 的资料库与检索层——归一（normalize，不截断）→ 切片（ingest）→ 向量化（ops drive，走 GPU）→ 对账与检索抽验，以及这条链上实测踩过的坑（`TEXT_LIMIT` 截断、手起 embed 抢显存、重跑撞外键、元数据 pending）。当用户说「这份 PDF 入库」「多了两份材料」「把它加进资料库」「新材料进检索」或需要判断某份材料为什么搜不到、向量算到哪了时使用。
---

# quizforge 材料入库

## 一句话前提

**材料的权威在库**（`data/quizforge.db`）：切片在 `material_slices`、窗口向量在 `slice_embeddings`、
材料本身在 `materials`。盘上只有两样东西：原件（资料根，**只读**）与**归一产物**
（`data/library/.text/{citekey}.md` + `.json`）。

两套存储、一个锚：

| | A 资料库 | B 材料（检索层） |
|---|---|---|
| 元数据 | `data/library/{citekey}.yaml` | `materials` 表 |
| 正文 | `.text/{citekey}.md`（派生，可重建） | 指向**同一份** `.text/*.md` |
| 行坐标 | 没有 | **有**（切片 + 行区间） |
| 谁读它 | `attach_material`（按 citekey） | `search_material` / `search_library` / `read_material` |

锚是 **citekey**（`app.library.citekey_for`，A/B 共用同一规则）与 **sha256**。

## 五步链（每步都有判据，别跳）

设 `PY=api/.venv/bin/python`。

**1. 放到资料根**（`reference/`，是软链，指向真正的盘，如 `/mnt/f/Documents`）

**2. 归一** —— 原件 → 行可寻址、带 `##` 层级的 markdown：

```bash
$PY -m pipeline.normalize --subject other "reference/新文件.pdf"
```

判据：输出里 `NNNN 行 · NN 章 · NN 页 [ok]`。**必须不截断**（见坑 1）。
产物：`data/library/.text/{citekey}.md` + `.json`（旁注里带 `at`/`state`/`pages`/`sha256`，
A 侧据此认「这份抽过了」——**别删它**）。

**3. 切片入库**（进 B 的**检索层**，`depth` 默认就是 `检索`）：

```bash
$PY -m pipeline.ingest data/library/.text/{citekey}.md --subject other
```

判据：`NN 片 · NNNNN tokens · **已入库**`。**文件名要用真正的 citekey**（新材料常常不是
文件名派生的，例如 PDF 元数据缺失时是 `doc-ff46e6e2` 这种）——先 `ls data/library/.text/ | grep ...`
看清，别拿 `grep 关键词` 去凑，凑空就等于把**整个目录**交给了 ingest。

**4. 向量化 —— 一律交给调度器，走 GPU**：

```bash
$PY -m pipeline.ops plan --slots 5 --json      # 先看它打算开什么车道（会说明理由）
$PY -m pipeline.ops drive --materials {citekey}
```

判据：车道行写着 `gpu(CUDA)`，结尾 `**对账平了**`、退出码 0（不平则 **3**，见坑 4）。

**5. 验证**（两道，缺一不可）：

```bash
$PY -m pipeline.ops audit --material {citekey} --exact --json     # 窗级：stored == planned、state 齐
# 再抽一句原文做检索，必须命中这本：
$PY -c "import sys; sys.path.insert(0,'api')
from app import tools
from app.db import get_session_factory
db = get_session_factory()()
out = tools.call(db,'search_library',{'query':'<原文里的一句话>'},{'conversationId':'probe'},mounts=None,allow=None)
got=[o for o in (out if isinstance(out,tuple) else (out,)) if isinstance(o,dict)][0]
print([(h['material'],h['startLine'],h['via']) for h in got['hits'][:3]])"
```

## 坑（全是实测，别再踩）

1. **别走「资料库抽正文 → 入库」那条路。** A 的抽取卡在 `attachments.TEXT_LIMIT = 200_000`
   字符，英文书会被砍掉一大截（实测 Cardelli 那本博士论文只剩 40%：6,142 行 vs 完整的
   9,793 行 / 254 章）。入库**一律**走 `normalize`（它用 `limit=0`）。若已经误抽过一份
   `.txt`，删掉 `.txt`+`.meta.json`，别让它抢在 `.md` 前面被 A 读到。
2. **不要手起 `pipeline.embed`。** 向量化由 `ops drive` 调度（它按机器余量算车道、管内存、
   写账本）。手起的那种和它抢**显存与内存** —— 2026-09-27 手起三条 embed，最后把 WSL 搞崩了。
   闸门现在按材料互斥：同一本材料起第二条会被拒（退出码 2，并点出占用者的 pid）；
   **不同材料不互斥**。
3. **`make watch` 只是仪表盘**：它不干活，而且没活了就自己退出。屏幕上"预计 0 分钟 ×
   NN 窗/秒"是**历史速率**。真在干活的是 `ops drive`。
4. **`drive` 对账不平会退 3。** 那就**再跑一次同一条命令** —— 对账认得"半片"（中断留下的
   半算切片），会接着补。判定"干完了"只看第 5 步的对账，不看退出码 0 与否的历史印象。
5. **重跑一本已经算过向量的材料**：`dbstore.save_slices` 会先删该本的派生向量与图
   （否则 `DELETE FROM material_slices` 会撞外键）；`point_sources` **不碰** ——
   出题层材料被重跑时会**报错停下**，那是刻意的（出处是出题账本的地基）。
6. **元数据 `origin: pending`（上游超时）不影响检索**：正文与向量照常。但目前**没有"只重试
   元数据"的入口**（`index` 见到 `at` 就跳过），要补就 `POST /api/library/meta` 手工填，
   或对那一份 `POST /api/library/index {"force": true}` 重抽一遍。
7. **`cpu` 车道是真 CPU**（约 1.2 窗/秒），`gpu` 车道的写法是 `gpu(CUDA)`（实测 26 窗/秒）。
   计划行里没有 `(CUDA)` 就说明它没在动 GPU —— 先看 `ops plan` 的 `why`。

## 收尾交代

* 一本材料算完的样子：`materials` 一行 + `material_slices` N 片 + `slice_embeddings`
  （每片至少一窗）→ `audit --exact` 报 `齐`。
* 想要版本库里也有：`make db-snapshot`（快照是 **SQLite 库本身**，`db/quizforge.db.gz`）。
