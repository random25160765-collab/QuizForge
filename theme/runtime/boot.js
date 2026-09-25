/* ===========================================================================
 * boot.js —— 启动编排（放在页面脚本之后加载）
 *
 * 顺序是有讲究的，每一步失败都得让用户看懂发生了什么：
 *
 *   1. GET /api/bank         题库为空 → 明确告诉用户去跑导入命令
 *   2. 进度装载              本地先灌快照，之后跟着流水增量收敛
 *   3. 启动页面应用 + hash 路由
 *
 * 页面脚本自己**不**启动：必须等这里走完，否则会出现"还没拿到数据就开始渲染"。
 *
 * （原先第一步是 `GET /api/auth/me`、未登录就跳登录页 —— 单用户本地形态下
 *   登录这件事整个没了，见 `api/app/deps.py`。）
 * ========================================================================= */
(function () {
  'use strict';

  var QF = (window.QF = window.QF || {});

  var api = QF.api;
  var page = (QF.config && QF.config.page) || 'quiz';

  /* ------------------------------------------------------------ 页面状态 */

  function root() {
    return document.getElementById('app-root');
  }

  function iconSvg(name) {
    var paths = {
      spinner: '',
      warn: '<path d="M12 4 2.5 20h19Z"/><path d="M12 10v4.5M12 17.4v.2"/>',
      book: '<path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H18a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H5.5A1.5 1.5 0 0 1 4 18.5Z"/>',
    };
    if (name === 'spinner') {
      return '<span class="bootstate__spinner"></span>';
    }
    return (
      '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" ' +
      'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' +
      (paths[name] || paths.warn) +
      '</svg>'
    );
  }

  /**
   * 渲染一屏状态页（加载中 / 出错 / 题库为空）。
   * 用 textContent 而不是 innerHTML 拼字符串：错误信息里可能带后端返回的内容。
   */
  function showState(options) {
    var el = root();
    if (!el) return;

    var box = document.createElement('div');
    box.className = 'bootstate';

    var icon = document.createElement('div');
    icon.className = 'bootstate__icon' + (options.tone ? ' is-' + options.tone : '');
    icon.innerHTML = iconSvg(options.icon || 'spinner');
    box.appendChild(icon);

    var title = document.createElement('h1');
    title.className = 'bootstate__title';
    title.textContent = options.title || '';
    box.appendChild(title);

    if (options.message) {
      var desc = document.createElement('p');
      desc.className = 'bootstate__text';
      desc.textContent = options.message;
      box.appendChild(desc);
    }

    if (options.code) {
      var code = document.createElement('pre');
      code.className = 'bootstate__code';
      code.textContent = options.code;
      box.appendChild(code);
    }

    var actions = options.actions || [];
    if (actions.length) {
      var row = document.createElement('div');
      row.className = 'bootstate__actions';
      actions.forEach(function (action, index) {
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn' + (index === 0 ? ' btn--primary' : '');
        btn.textContent = action.label;
        btn.onclick = action.onClick;
        row.appendChild(btn);
      });
      box.appendChild(row);
    }

    el.textContent = '';
    el.appendChild(box);
  }

  function replaceState(options) {
    // 已经有内容时（例如已经渲染出应用）不要再覆盖
    var el = root();
    if (el && el.querySelector('.bootstate') === null && el.childNodes.length) return;
    showState(options);
  }

  /* ------------------------------------------------------- 单用户：没有登录 */

  // 原先这里有两个函数：`loginUrl()` 拼带 next 的登录页地址，`toLogin()` 跳过去。
  // 单用户本地形态下没有登录页，401 也不再是「该登录了」的意思（本地不会有 401）。
  // 留着这两个函数只会让人以为还有登录这回事，所以删掉。

  /* ---------------------------------------------------------------- 装载 */

  function loadBank() {
    return api.get('/bank').then(function (bank) {
      if (!bank || !bank.questions || !bank.questions.length) {
        replaceState({
          icon: 'book',
          title: '题库还没有题目',
          message: '服务端已经连上了，但题库是空的。在服务器上导入 questions/ 下的题目，然后刷新本页。',
          code: 'docker compose exec api python -m app.cli.import_bank\n# 或本地：python3 -m tools.import_bank',
          actions: [{ label: '刷新', onClick: function () { location.reload(); } }],
        });
        return false;
      }
      // 公共题库打上来源标记。
      //
      // 字段名刻意**不叫 `source`**：题目自己的 `source` 是"出处"（材料与行号），
      // 解析抽屉的出处行（`qview.js`）与题库搜索的搜索域（`data.js`）都在用它。
      // 拿它记题源有两个后果：没有出处的题会把出处显示成 "public"，
      // 而且每道公共题都会因为正文含 "public" 而命中搜索（实测踩过）。
      // 筛选的权威判据仍是 `QF.data.myIds`（见下），这个字段只用于展示。
      (bank.questions || []).forEach(function (q) {
        q.bank = 'public';
      });
      // 自己的题单**并进同一个池子**：练习与组卷那两头只认 QF.data，
      // 这样它们不必为"题从哪来"分叉，只多一个来源标记。
      // 取不到就当没有（自己的题少一项，公共题库照常）。
      return api
        .get('/my/questions')
        .catch(function () {
          return { questions: [] };
        })
        .then(function (mine) {
          var mineIds = {};
          (mine && mine.questions ? mine.questions : []).forEach(function (row) {
            var q = Object.assign({}, row.payload || {});
            q.id = row.id;
            q.bank = 'mine';
            mineIds[q.id] = true;
            if (!q.pointKey) q.pointKey = row.pointKey || '';
            // 填空题的答案是题库自己的形状（一个带 accept 列表的字符串），
            // 用户题存的是人写的那一句 —— 这里对齐，否则刷题页判分认不出来。
            if (q.type === 'blank' && typeof q.answer === 'string') {
              var accepts = [q.answer].concat(q.accepts || []);
              q.answer = JSON.stringify(
                accepts.map(function (one) {
                  return { regex: [], accept: [String(one)] };
                })
              );
            }
            bank.questions.push(q);
          });
          QF.data.install(bank);
          // 来源记成 **id 集合**，而不只靠题对象上的字段：install() 会把题规整一遍，
          // 不认识的自定义字段留不留由它说了算 —— 实测公共题上的 `source` 没活下来。
          QF.data.myIds = mineIds;
          return true;
        });
    });
  }

  function loadProgress() {
    return api.get('/progress').then(function (snapshot) {
      // 灌进本地：之后所有读仍是同步的本地读，UI 层一行都不用改
      var info = QF.store.hydrate(snapshot);
      if (QF.sync) {
        // 上次没推成功的流水 / 基线重新入队。
        // 这是断网期间作答不会丢的关键：它们一直躺在本地队列里，
        // 而不是等下一次作答时才顺带被标脏。
        QF.sync.resume();
        if (info && info.keptLocal) QF.sync.flush();
      }
      return true;
    });
  }

  /** 只刷新进度，不动题库也不动界面 —— 供切回前台时调用 */
  function refreshProgress() {
    return api.get('/progress').then(function (snapshot) {
      return QF.store.hydrate(snapshot);
    });
  }

  function startPage() {
    if (page === 'wrongbook') {
      if (QF.wrongbook && QF.wrongbook.boot) QF.wrongbook.boot();
    } else if (page === 'chat') {
      if (QF.chat && QF.chat.boot) QF.chat.boot();
    } else if (QF.app && QF.app.boot) {
      QF.app.boot();
    }
    if (QF.router && QF.app) QF.router.start(QF.app);
  }

  /* ---------------------------------------------------------------- 启动 */

  /*: 启动自检的重试节奏（毫秒）。为什么必须退避重试：服务在 `--reload`（开发）或
   * 重启的那几秒里会**短暂**不应答，而自检只试一次 —— 撞上那一下，页面就永久钉在
   * "连不上服务"上（用户连着两次："（还是显示这个玩意）"）。这不是猜的：实测在**同一个
   * 页面**里，那条消息显示之后几秒 `fetch('/api/bank')` 是 200（99ms）—— 服务一直好着，
   * 只是开机那一瞬间没赶上。 */
  var BOOT_RETRY_MS = [400, 900, 1800, 3000, 5000];

  //: 错误页挂上之后，还隔多久自己再试一次。让"把服务起起来"这件事不必由人来告诉它。
  var BOOT_POLL_MS = 5000;

  /** 试一次完整的启动读取。返回 `false` = 界面已经说清了（题库空之类），不必重试。 */
  function startAttempt() {
    return loadBank().then(function (ok) {
      if (!ok) return false;
      return loadProgress().then(function () {
        return true;
      });
    });
  }

  function start() {
    var tries = 0;

    function showFailure(err) {
      // `api.js` 给**真正的网络失败**标的记号是 `status === 0`（请求压根没拿到响应）。
      // 从前这里写的是 `!err.status` —— 那把 `TypeError` 之类**我们自己写错**的错也
      // 归成了"服务没应答"，于是屏幕上一直在说服务的事，真凶一个字都不露
      // （这轮就是被它带偏的：真正的原因是 `QF.shell.mount is not a function`）。
      var isNetwork = err.status === 0;
      // 网络错误只说"本地服务没有在运行"会把人指错方向：页面若不是从服务自己的地址
      // 打开的（IDE 预览、别的端口、`file://` 快照），服务明明活着也照样连不上。
      // 所以把**两个地址**都摆出来 —— 一眼能看出是不是打开的地方不对。
      var here = location.origin || '(未知)';
      var there = (api && api.base) || here;
      replaceState({
        tone: 'bad',
        icon: 'warn',
        title: isNetwork ? '连不上服务' : '启动失败',
        message: isNetwork
          ? '本地服务没有应答（已重试 ' +
            tries +
            ' 次，每 ' +
            BOOT_POLL_MS / 1000 +
            ' 秒自己再试一次）。若它其实在运行，检查这一页是不是从服务自己的地址打开的：本页 ' +
            here +
            ' ，接口 ' +
            there +
            '。'
          : err.message || '未知错误',
        actions: [
          { label: '再试一次', onClick: run },
          { label: '刷新本页', onClick: function () { location.reload(); } },
        ],
      });
      if (isNetwork) window.setTimeout(run, BOOT_POLL_MS);
    }

    function run() {
      showState({
        title: '正在载入…',
        message: tries ? '服务还没应答，正在再试（第 ' + (tries + 1) + ' 次）…' : '读取题库与进度',
      });
      startAttempt()
        .then(function (ready) {
          if (!ready) return; // 题库空之类：界面已经说清了，再试没有意义
          startPage();
        })
        .catch(function (err) {
          // **先留痕**：这条链上出的错从前一个字都不留（只有一屏"连不上服务"），
          // 于是排查时全靠猜 —— 实测为此白跑了好几轮（服务明明好着、接口也通，
          // 却只看到"服务没应答"）。写了这一行，下次一眼就知道是哪一层的事。
          try {
            window.console.warn('[qf] 启动读取失败（第 ' + (tries + 1) + ' 次）：', err);
          } catch (e) {
            /* 打不出来不影响流程 */
          }
          // 只对**真正的网络失败**（`status === 0`）退避重试：401/500 是服务答了、要人
          // 处理的事；`status === undefined` 则是**代码自己抛的**（比如某个全局被覆盖 ——
          // 这轮踩过的那个 `QF.shell.mount is not a function`），重试一万次也没用，
          // 必须立刻原样报出来。
          if (err.status === 0 && tries < BOOT_RETRY_MS.length) {
            var delay = BOOT_RETRY_MS[tries];
            tries += 1;
            window.setTimeout(run, delay);
            return;
          }
          showFailure(err);
        });
    }

    run();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }

  QF.boot = { start: start, showState: showState, refreshProgress: refreshProgress };
})();
