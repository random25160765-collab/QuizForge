# QuizForge Bridge（Obsidian 插件）

让 QuizForge 的对话读写这个 vault 里的笔记。**文件信箱，不走网络** —— WSL 的网络太乱，
两边只通过 vault 里的一个目录交换 JSON 文件。

协议：`docs/Obsidian桥接协议.md`（仓库里）。

## 安装

1. 把本目录（`manifest.json` + `main.js`）整个拷到：

   ```
   <vault>/.obsidian/plugins/quizforge-bridge/
   ```

2. Obsidian → 设置 → 第三方插件 → 关闭"安全模式" → 在已安装插件里启用 **QuizForge Bridge**。
3. 启用后会看到提示"QuizForge Bridge 已启用"，同时 vault 根下多出一个目录：

   ```
   <vault>/.quizforge-bridge/
   ```

4. 在 QuizForge 的设置 → Obsidian 里填这个 vault 的路径（`F:\Notes` 或 `/mnt/f/Notes` 都认），
   点「检测连接」，应显示 `已连上：<vault 名> · 插件 0.1.0`。

## 它在做什么

- 插件启动时在 `<vault>/.quizforge-bridge/` 建 `inbox/`、`outbox/`、`tmp/`，写 `bridge.json`，
  并每秒 touch 一次 `heartbeat`。
- QuizForge 要读/搜/改笔记时，往 `inbox/` 放一个 `req-<id>.json`，插件处理完在 `outbox/`
  放 `res-<id>.json`。
- 写文件一律"先写 tmp、再 rename"，对端不会读到半个 JSON。
- 一次只处理一个请求；处理前先看有没有同 id 的响应（重放安全）。

## 边界

- **只在桌面版可用**（需要本地文件路径）；移动端不加载。
- 插件不做网络监听、不开端口。
- 只读/写 vault 里的 Markdown；不碰 `.obsidian/`、`.trash/`、`.quizforge-bridge/`。
- 写入走 `app.vault.process`，frontmatter 原样保留；历史靠 Obsidian 自己的 file recovery。

## 卸载

在 Obsidian 里停用插件即可（会删掉 `bridge.json` 与 `heartbeat`）。
`.quizforge-bridge/` 目录可以手动删；里面只有临时请求/响应文件。
