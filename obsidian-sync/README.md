# QFStudio Sync

Obsidian 插件：把当前 vault 里的 Markdown 与 QFStudio 的**云端笔记副本**双向同步。

它和 `quizforge-bridge` 是**两个独立插件**，互不调用：

| 插件 | 负责 | 走网络吗 |
| --- | --- | --- |
| `quizforge-bridge` | QF ↔ 本地文件（文件信箱） | 否 |
| `quizforge-sync`（本插件） | 本地文件 ↔ 云端（Turso） | 是（端到端加密） |

所以两件事可以分开装：只想要 QF 读写本地笔记，装 bridge 就够；想在手机上也能读写，
两个都装。

## 安装

1. 把 `manifest.json` 与 `main.js` 两个文件拷到 vault 的
   `.obsidian/plugins/quizforge-sync/`。插件必须是**单文件**：Obsidian 加载插件时
   模块内的相对 `require('./codec.js')` 解析不到（实测 "Failed to load plugin"），
   所以加密与口径那一层内联在 `main.js` 的 `// #region codec` 段里。
2. Obsidian → 设置 → 第三方插件 → 关闭安全模式 → 启用 **QFStudio Sync**。
3. 在同名的设置页填两样：

   - **Turso 地址**：`libsql://xxx.turso.io`（插件自动补 `/v2/pipeline`）
   - **Turso token**：QF 设置 → 同步里复制

   没有"口令"这一栏（2026-11-10 起）：端到端密钥由这两样推导，用户不用保管东西。

## 它怎么工作

- 启动 3 秒后跑第一轮，之后每 60 秒一轮（可改，最小 15 秒）；保存笔记后 2 秒补一轮。
- **推**：扫 `.md`，与上次同步记录的 hash 比对，只推变化的那几篇。
- **拉**：按 `at` 游标分页读云端，写回文件时**保留本地 frontmatter**，只换正文。
- **冲突**：本地也改过（hash 与上次同步记录不一致）→ 云端版本另存为
  `xxx.conflict-2026-11-09.md`，**不覆盖**你的文件。
- **删除**：本地删掉 → 推一条墓碑；云端删掉 → 本地文件移入 Obsidian 回收站
  （如果删除到达前你又改过它，则保留文件，避免误删）。
- 跳过 `.obsidian/`、`.trash/`、`.quizforge-bridge/`、`.git/` 与其它隐藏目录。

云端只存**去掉 frontmatter 的正文**。所以手机上新建的笔记没有 frontmatter，
而桌面端拉下来时会保留你本地那份头。

首版只同步 `.md` 正文：图片、canvas、附件不同步。

## 自检

```bash
node obsidian-sync/selftest.mjs          # 加解密往返
api/.venv/bin/python -m pytest api/tests/test_plugin_codec.py -q   # 与 Python 侧对拍
```

加密与口径（`sha16` / `splitFront` / scrypt / AES-GCM）只在 `main.js` 的
`// #region codec` 段里写一次；`selftest.mjs` 按标记把这一段原样截出来执行，
所以对拍验的就是插件真正跑的那份代码。

## 排错

- **校验值对不上**：说明填的地址 / 令牌属于另一个库。三台机器必须指向同一个库；
  换过令牌等于换了加密身份，云端旧密文解不开（后端会重写 `check` 并把加密行重推）。
- **Turso HTTP 4xx**：token 过期或地址抄错（`libsql://` 与 `https://` 都行）。
- 插件日志前缀 `[qf-sync]`，在开发者工具（Ctrl+Shift+I）的 Console 里看。
