/* QuizForge 桌面外壳（Electron）。
 *
 * 它只做四件事：
 *   1. 找一个空闲端口，把**后端 exe** 作为子进程拉起来（`--port <p> --no-browser`）；
 *   2. 等 `/api/health` 通了再开窗口（避免先看到一片白）；
 *   3. 窗口加载 `http://127.0.0.1:<port>/`（后端自己也提供前端静态文件）；
 *   4. 退出时把子进程收掉 —— 不能让 `quizforge.exe` 在后台留下来。
 *
 * 后端在哪：
 *   * 打包后：`resources/backend/quizforge.exe`（electron-builder 的 extraResources）；
 *   * 开发时：环境变量 `QF_BACKEND` 指过去（比如仓库根的 `build/dist/quizforge.exe`）。
 *
 * 为什么不让 Electron 直接 loadFile 那些 html：后端的接口、SSE 流、静态资源
 * 全在同一个源上，走 http 最省事，也不会踩 file:// 的跨域。
 */

const { app, BrowserWindow, shell, dialog } = require('electron');
const { spawn } = require('node:child_process');
const net = require('node:net');
const path = require('node:path');
const fs = require('node:fs');
const http = require('node:http');

const PROD_BACKEND = path.join(process.resourcesPath || '', 'backend', 'quizforge.exe');
const DEV_BACKEND = process.env.QF_BACKEND || '';

/** 排障用的日志：GUI 应用没有控制台，出问题只能写文件。位置 `%APPDATA%\QuizForge\shell.log`。 */
let LOG_PATH = '';
function log() {
  if (!LOG_PATH) {
    try {
      LOG_PATH = path.join(app.getPath('userData'), 'shell.log');
    } catch (err) {
      return;
    }
  }
  const line = `[${new Date().toISOString()}] ${Array.from(arguments).join(' ')}\n`;
  try {
    // `appendFileSync` 不会建父目录：首次启动时 `%APPDATA%\QuizForge` 还不存在，
    // 日志会静默丢失（排查"应用一启动就退"时因此误判成"main.js 没执行"）。
    fs.mkdirSync(path.dirname(LOG_PATH), { recursive: true });
    fs.appendFileSync(LOG_PATH, line);
  } catch (err) {
    /* 日志写不进去也不能影响启动 */
  }
}

// 顶层就设好日志与兜底：打包后的应用没有控制台，任何一处早期异常都不会留下痕迹
try {
  LOG_PATH = path.join(app.getPath('userData'), 'shell.log');
} catch (err) {
  /* 忽略：log() 里还会再试一次 */
}
process.on('uncaughtException', (err) => log('uncaught', (err && err.stack) || String(err)));
process.on('unhandledRejection', (err) => log('rejection', String(err)));
log('start', 'electron=' + process.versions.electron, 'asar=' + (__dirname || ''));

let backend = null;
let win = null;
let quitting = false;

/** 让系统给一个空闲端口（先 listen(0) 再关掉）。 */
function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.once('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const port = srv.address().port;
      srv.close(() => resolve(port));
    });
  });
}

/** 首选的固定端口。
 *
 *  **为什么不能每次都随机**：浏览器的 `localStorage` 是按 **origin** 存的，而 origin
 *  含端口。随机端口 = 每次启动都是一个"全新的站点"，于是主题、侧栏宽度、"深度思考"
 *  这些开关每次回到默认；而开发端（固定 8100）是粘的 —— 用户看到的就是
 *  "桌面包里行为不一样"，还很难联想到端口。
 *
 *  固定端口被占（另一个程序、上一次没退干净的进程）才退回随机：宁可这一次不粘，
 *  也不能起不来。
 */
const PREFERRED_PORTS = [8731, 8732, 8733];

function portFree(port) {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.once('error', () => resolve(false));
    srv.listen(port, '127.0.0.1', () => srv.close(() => resolve(true)));
  });
}

async function pickPort() {
  for (const port of PREFERRED_PORTS) {
    if (await portFree(port)) return port;
  }
  log('固定端口都被占了，退回随机端口（这一次的设置不会跨启动留住）');
  return freePort();
}

/** 轮询 `/api/health`，直到通了或者超时。 */
function waitHealthy(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tick = () => {
      const req = http.get(
        { host: '127.0.0.1', port, path: '/api/health', timeout: 1500 },
        (res) => {
          res.resume();
          if (res.statusCode === 200) return resolve();
          retry();
        }
      );
      req.on('timeout', () => req.destroy());
      req.on('error', retry);
    };
    const retry = () => {
      if (Date.now() > deadline) return reject(new Error('后端在 30 秒内没有就绪'));
      setTimeout(tick, 300);
    };
    tick();
  });
}

function backendPath() {
  if (DEV_BACKEND && fs.existsSync(DEV_BACKEND)) return DEV_BACKEND;
  if (PROD_BACKEND && fs.existsSync(PROD_BACKEND)) return PROD_BACKEND;
  return '';
}

async function startBackend(port) {
  const exe = backendPath();
  if (!exe) {
    throw new Error(
      '找不到后端程序。打包后它应当在 resources/backend/quizforge.exe；' +
        '开发时请把 QF_BACKEND 指向 build/dist/quizforge.exe。'
    );
  }
  log('spawn', exe, 'port', port);
  backend = spawn(exe, ['--port', String(port), '--no-browser'], {
    // 数据目录由后端自己决定（`~/quizforge`），这里不覆盖
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  backend.on('error', (err) => log('spawn error', (err && err.stack) || String(err)));
  // **后端输出默认不落盘**：它把每条 HTTP 访问日志都打到 stdout，而浏览器一开页面就是
  // 几百个请求（tikzjax 那批），逐条 `appendFileSync` 会把主进程事件循环拖死 ——
  // 实测表现是"日志停在某一秒、窗口白屏、定时器不触发"。排障时才开 QF_SHELL_VERBOSE=1。
  const VERBOSE = process.env.QF_SHELL_VERBOSE === '1';
  backend.stdout.on('data', (buf) => {
    if (VERBOSE) log('out', buf.toString().trim());
  });
  backend.stderr.on('data', (buf) => {
    if (VERBOSE) log('err', buf.toString().trim());
  });
  backend.on('exit', (code, signal) => {
    backend = null;
    // signal 有值 = 被外部杀掉（Windows 上是 SIGTERM）；code 有值 = 自己退出
    log('backend exit', 'code=' + code, 'signal=' + signal, 'quitting=' + quitting);
    if (!quitting) {
      dialog.showErrorBox(
        'QuizForge 后端退出了',
        `退出码 ${code}${signal ? '（信号 ' + signal + '）' : ''}。日志：${LOG_PATH}`
      );
      app.quit();
    }
  });
  await waitHealthy(port, 30000);
}

function createWindow(port) {
  win = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: '#f7f9fb',
    autoHideMenuBar: true,
    title: 'QuizForge',
    webPreferences: {
      // 前端是纯静态页面 + fetch，不需要 Node 能力
      nodeIntegration: false,
      contextIsolation: true,
    },
  });
  win.loadURL(`http://127.0.0.1:${port}/`);

  // 渲染进程的报错要能落到日志里：装上看得见的只是"白屏"，原因在控制台
  win.webContents.on('console-message', (event, level, message, line, sourceId) => {
    log('console', level, message, `${sourceId}:${line}`);
  });
  win.webContents.on('did-fail-load', (event, code, desc, url) => {
    log('fail-load', code, desc, url);
  });
  win.webContents.on('render-process-gone', (event, details) => {
    log('render-gone', JSON.stringify(details));
  });

  // 白屏排查：**不等 did-finish-load**（tikzjax 那批资源会让它迟迟不触发），
  // 起来 8 秒后直接问页面要状态，并且抓一张图存到 userData 里。
  //
  // `tag` 只影响截图文件名：`window.png` 是首屏，逐页探针那几张是 `window-<页名>.png`。
  const probe = (tag) => {
    const name = `window${tag ? '-' + tag : ''}.png`;
    win.webContents
      .executeJavaScript(
        `JSON.stringify({
          href: location.href,
          ready: document.readyState,
          page: (window.QF && QF.config && QF.config.page) || null,
          hasChat: !!(window.QF && QF.chat),
          rootKids: (document.getElementById('app-root') || {}).childElementCount || 0,
          viewKids: (document.querySelector('.view') || {}).childElementCount || 0,
          viewRect: (() => { const v = document.querySelector('.view'); if (!v) return null;
            const b = v.getBoundingClientRect(); return [Math.round(b.width), Math.round(b.height)]; })(),
          // 对话页输入框上方那一排（模式 / 深度思考 / **滤镜**）：药丸缺了要看得见。
          // 它在整页文字里排在 200 字之后，所以单独取一次这一行。
          // 注意：这段是模板字面量，注释里**不能出现反引号** —— 它会把模板提前结束
          //（实测：装上直接弹 "A JavaScript error occurred in the main process"）。
          tools: (() => { const t = document.querySelector('.chat__tools');
            return t ? (t.innerText || '').replace(/\\s+/g, ' ').slice(0, 80) : null; })(),
          text: (document.body.innerText || '').slice(0, 200)
        })`
      )
      .then((out) => log('probe', tag ? `${tag} ${out}` : out))
      .catch((err) => log('probe fail', tag ? `${tag} ${err && err.message}` : String(err && err.message)));

    win.webContents
      .capturePage()
      .then((img) => {
        fs.writeFileSync(path.join(LOG_PATH ? path.dirname(LOG_PATH) : '.', name), img.toPNG());
        log('shot', name);
      })
      .catch((err) => log('shot fail', String(err && err.message)));
  };
  probe();
  setTimeout(probe, 8000);
  setTimeout(probe, 20000);

  // **逐页探针**：`QF_SHELL_PROBE=chat,quiz,library` 时，首屏探完再依次打开这几页，
  // 各探一次、各抓一张图。
  //
  // 为什么要有它：白屏这类问题只能拿"渲染出来是什么"说话 —— 接口全绿、文件都在，
  // 页面上照样可以是空的（实测撞过：数据目录空着时对话页一片空白，而 `/api/health`
  // 一切正常）。安装版又没法开 DevTools，于是只能让外壳自己把每一页拍下来。
  const TOUR = String(process.env.QF_SHELL_PROBE || '')
    .split(',')
    .map((one) => one.trim())
    .filter(Boolean);

  if (TOUR.length) {
    log('tour', TOUR.join(','));

    /** 打开一页并**确认真的到了那儿**（`loadURL` 的 promise 只保证它自己那一步）。
     *
     *  为什么要再核对地址：上一页如果在做 hash 跳转，`loadURL` 可能被顶掉而 promise
     *  照样落定 —— 于是"探的是 library、记的却是 quiz"。实测就撞过一次，那一行
     *  日志把人往"资料页会自己跳走"上带。地址对不上就再等一会儿，还不对就如实记。
     */
    const goto = async (page) => {
      const url = `http://127.0.0.1:${port}/${page}.html?probe=1`;
      try {
        await win.loadURL(url);
      } catch (err) {
        log('tour fail', page, String(err && err.message));
      }
      const deadline = Date.now() + 8000;
      const want = `/${page}.html`;
      while (Date.now() < deadline && String(win.webContents.getURL()).indexOf(want) < 0) {
        await new Promise((resolve) => setTimeout(resolve, 200));
      }
      log('tour at', page, win.webContents.getURL());
    };

    setTimeout(async () => {
      for (const page of TOUR) {
        await goto(page);
        // 页面脚本要等 `boot.js` 把数据取回来才画，2.5 秒是"本地库"的经验值
        await new Promise((resolve) => setTimeout(resolve, 2500));
        probe(page);
      }
    }, 12000);
  }

  // 站内的外链交给系统浏览器，不在应用窗口里打开
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('http://127.0.0.1')) return { action: 'allow' };
    shell.openExternal(url);
    return { action: 'deny' };
  });

  win.on('closed', () => {
    win = null;
  });
}

function stopBackend() {
  if (!backend) return;
  try {
    backend.kill();
  } catch (err) {
    /* 已经退了 */
  }
  backend = null;
}

// 只开一个实例：第二个实例把已有窗口叫到前面来
const gotLock = app.requestSingleInstanceLock();
log('lock', gotLock);
if (!gotLock) {
  log('已有实例在跑，退出');
  app.quit();
} else {
  app.on('second-instance', () => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });

  app.whenReady().then(async () => {
    try {
      LOG_PATH = path.join(app.getPath('userData'), 'shell.log');
      log('boot', 'resourcesPath=' + process.resourcesPath);
      log('backend', backendPath() || '(none)');
      const port = await pickPort();
      log('port', port);
      await startBackend(port);
      log('backend ready');
      createWindow(port);
    } catch (err) {
      log('启动失败', (err && err.stack) || String(err));
      dialog.showErrorBox('启动失败', String((err && err.message) || err));
      app.quit();
    }
  });

  app.on('window-all-closed', () => {
    log('window-all-closed');
    app.quit();
  });

  app.on('before-quit', () => {
    log('before-quit');
    quitting = true;
    stopBackend();
  });
}
