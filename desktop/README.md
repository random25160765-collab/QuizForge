# QFStudio 桌面外壳（Electron）

把 `quizforge.exe`（PyInstaller 打出来的后端）作为子进程拉起来，用一个窗口装住它。
窗口加载 `http://127.0.0.1:<随机空闲端口>/` —— 前端静态文件由后端自己提供，所以
**不需要**再单独打包前端。

## 出包

**从 WSL 里一条命令**（前端 → 同步 → 后端 exe → 套壳 → 拷到桌面）：

```bash
make dist          # → 桌面上的 QFStudio Setup <版本>.exe
```

它由 `build/dist_win.py` 串起来，顺序不能换：

1. 在 Windows 上用那个 venv 跑 `build/package.py build`，产物在
   `%USERPROFILE%\Desktop\QF_dist\dist\quizforge.exe`；
2. 在本目录跑 `npm run dist`（electron-builder），它按 `extraResources` 把上一步那个
   exe 打进 `resources/backend/`；
3. 安装包拷到桌面（与 `build\dist\` 各留一份），哈希对不上就当场报错。

单独调外壳时：

```powershell
cd %USERPROFILE%\Desktop\QF_dist\src\desktop
npm run dist          # 只出安装包
npm run dist:dir      # 只出未压缩目录（调样式时更快）
```

`npm install` 由 `dist_win.py` 按需做（判据是 `node_modules\electron\dist\electron.exe`
在不在）；镜像与离线包那几条见下面第 2、3 条坑。

## 开发时

`npm start` 需要一个已经打好的后端：

```powershell
$env:QF_BACKEND = "D:\path\to\quizforge.exe"   # 或仓库里的 build\dist\quizforge.exe
cd desktop
npm start
```

没设 `QF_BACKEND` 时，它在 `resources/backend/quizforge.exe`（打包后才有）里找。

## 数据在哪

后端自己决定：`%USERPROFILE%\quizforge\`（库、设置、日志）。外壳不碰这个目录，
也不往 `%APPDATA%` 里塞东西 —— 卸载重装不会丢数据，换外壳也不影响。

旧的 `%USERPROFILE%\quizforge-beta\` 会在首次启动时自动搬过去（见
`api/app/config.py` 的 `frozen_data_dir`）。

## 构建时踩过的坑（都真实发生过）

1. **`ELECTRON_RUN_AS_NODE=1`** —— 从 WSL 里调 `powershell.exe` 启动 exe 时，这个变量会被带进去，
   Electron 于是以 Node 模式运行：没有窗口、`require('electron')` 报 `Cannot find module`、
   进程立刻退出且**没有任何日志**。从 WSL 调试前先 `Remove-Item Env:ELECTRON_RUN_AS_NODE`。
   **直接双击不受影响**（用户级/机器级环境变量里都没有它）。
2. **npm 12 默认拦 postinstall** —— `npm install` 不会下 Electron 二进制，
   `node_modules/electron/dist` 是空的。手动跑 `node node_modules/electron/install.js`，
   或 `npm install-scripts approve electron`。
3. **下载必须走镜像** —— `ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/`、
   `ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/`。
   直连会断在半路（实测只解出 1 个文件，8.7MB）。
4. **`extraResources` 的路径相对于 `desktop/`** —— 构建镜像里后端在
   `%USERPROFILE%\Desktop\QF_dist\dist\`，所以写 `../../dist/quizforge.exe`。
5. 排障日志与截图都在 **`%APPDATA%\quizforge-desktop\`**（目录名来自 `package.json`
   的 `name`，不是 `productName`）：`shell.log` 记启动失败、后端退出码、每页的 `probe`；
   `window.png` / `window-<页名>.png` 是它自己抓的截图。
6. **逐页探针**：设 `QF_SHELL_PROBE=chat,quiz,library` 再启动，外壳会在首屏之后依次打开
   这几页、各探一次状态并截图。白屏这类问题只能靠"渲染出来是什么"说话 ——
   `make dist-verify` 用的就是它（见 `build/dist_win.py` 的 `install_check`）。

## 几个约定

- **单实例**：再点一次图标是把已有窗口叫到前面，不会起第二个后端；
- **退出即收**：关窗口会结束子进程，不会留下后台的 `quizforge.exe`；
- **外链走系统浏览器**：站内链接留在窗口里；
- 后端 30 秒内没就绪会弹错误框并退出（而不是白屏等着）。
