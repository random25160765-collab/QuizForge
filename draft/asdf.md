先把那 6 条删掉，同时开始查材料数的事。
删除完成：那 6 条是父子链，删掉根节点（235）时 `ON DELETE CASCADE` 把 5 个子节点一并带走，`messages 461 → 455`，现在 `foreign_key_check` 干净。

材料那步我脚本写错了（拿的是合并后的库、不是快照），重来一遍：
关键线索出来了：云端的 60 份**全是 `tt-metal`**（`source_path` 全在 `/home/rd/Source/tt-metal`，09-16 导入）；本机独有那 55 份 `source_path` 全在 **`/home/rd/Desktop/quizforge` 仓内**，09-21 导入，且**只切了片、没出图/没出知识点**。继续把机制查实：
`source_path` 一栏就定案了——那 55 份的来源是 **`data/library/.text/<slug>.txt`**（本机资料库导出的正文），而云端的 60 份来自 `/home/rd/Source/tt-metal/tech_reports`（试点材料）。两条**不同的入库路径**。再确认最后一环：谁扫这两个目录。
两条入库路径清楚了，但还差最后一环：这 55 份是**本机资料库**（`data/library/`）导出来的，而 `data/` 是被 gitignore 的。确认写入方与目录实况：
资料库实况对上了：`data/library/` 下是 59 个 `<slug>.yaml` 元数据 + 112 个 `.text/*.txt` 正文抽取；`data/` 被 gitignore。最后核实"云端重建"那一步的规程写法：
机制查清了，最后补两个数据点（档位与待入库的条数）：
## 悬空消息：已删

删根节点 `id=235` 时 `ON DELETE CASCADE` 把 5 个子节点一并带走（`messages 461 → 455`），附件没有指向它们的，`foreign_key_check` 现在干净、`integrity_check` ok。

## 材料数：两条不同的入库路径，不是谁删了谁

| | 60 份（云端也有） | 55 份（本机独有） |
|---|---|---|
| `source_path` | `/home/rd/Source/tt-metal/tech_reports/…` | **`/home/rd/Desktop/quizforge/data/library/.text/<slug>.txt`** |
| `subject` | 全部 `tt-metal` | cuda / nvidia / swe / riscv / verilog / stm32 / amd / cxl / python / ic / linux / c / toolchain / other / library（15 类） |
| `updated_at` | 2026-09-16 09:45、09:51 | 2026-09-21 07:29:48–49（2 秒内一批） |
| `depth` 档位 | **`出题`** | **`检索`**（默认值） |
| 切片 | 215（`path` 形如 `xxx.md#L1-L84`，带 `tokens`） | 504（`path` 形如 `("(前言)（上）")`，每片 250 行，`tokens` 为空） |
| 图 / 知识点 / 图清单 | figures 350 · knowledge_points 981 · point_candidates 3799 **全挂在这 60 份上** | **0 / 0 / 0** |

最后一行就是铁证，和 `docs/STATUS.md` §2026-09-21 的原文逐字对上：

> ② **资料库入库**（`pipeline/intake.py` + `make intake`）—— 登记进 `materials`（`source_path` 指**抽取缓存** `.text/{citekey}.txt`…），再复用 `ingest.split_slices` 切段。**抽点 / 知识点 / 概念 / 题一行都不写** —— 那条"难道灌水论文我也要出成题目做吗"是它的根据。

`Makefile:333` 也写着同一句：`# 资料库入库：登记 + 切片，**止步于此**（不抽点 / 不归概念 / 不出题）`。

**所以机制是**：那 55 份是你 09-21 在本机跑 `make intake` 把**资料库**（`data/library/`，59 个 `<slug>.yaml` + 112 个 `.text/*.txt`）登记进库的产物；云端那份快照是 09-22「只搬内容资产」重建的，而那步跑在**流水线那条谱系**上——本机的资料库入库它从来没见过。**不是它删了 55，而是它压根不知道有这 55 份。**（顺带把我上一轮的"垃圾/更早一代"彻底钉死：那是最近一次入库，且是正经材料。）

**为什么这件事必须有个裁定**：那 55 份的 `source_path` 指向 `data/library/.text/` 的**本机抽取缓存**，而 `data/` 被 gitignore。**哪怕把这些行带进快照，换台机器也全是断链**——源件与缓存都只在本机。但 `docs/STATUS.md` 自己定的规矩是「快照只装内容资产（题目 / 考纲 / 图谱 / **材料** / 向量）」——资料库材料也是材料，于是规矩与机制打架：要么快照不再宣称"材料"是全量，要么资料库得有自己的搬家通道（把原件一起带走，或让新机器重跑 `make intake`）。

## 顺手发现的两件事

1. **资料库还有 4 个条目没入库**：`trefethenbau`、`risc-riscv`、`risc-riscv-2`、`rmstmxxx-stm`——有 yaml、没有 `materials` 行。`make intake` 是幂等的，可以补。（注意 `risc-riscv` 与库里已有的 `risc`、`rmstmxxx-stm` 与 `rmstmxxx` 看着是同件异名，补之前先确认会不会造出重复。）
2. 本机库现在是 **115 份**：60 份 `depth=出题`（tt-metal 试点，图/点都在这条链上）+ 55 份 `depth=检索`（资料库）。这与云端快照的 60 份会长期不一致——**下次 `make db-snapshot` 就会把 115 份写进快照**，需要你先裁定上面那条规矩。

要我怎么走？三个方向：① 维持现状（资料库属本机，快照只装流水线材料，在文档里写死这条）；② 给资料库做搬家通道（快照带行 + 源件另备一份，`source_path` 改成可重定位的写法）；③ 先把那 4 个待入库的补齐再说。