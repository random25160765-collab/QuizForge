/* ===========================================================================
 * boot.js —— 启动编排（放在页面脚本之后加载）
 *
 * 顺序是有讲究的，每一步失败都得让用户看懂发生了什么：
 *
 *   1. GET /api/auth/me      未登录 → 带 next 跳登录页
 *   2. GET /api/bank         题库为空 → 明确告诉用户去跑导入命令
 *   3. 进度装载              本地先灌快照，之后跟着流水增量收敛
 *   4. 启动页面应用 + hash 路由
 *
 * 页面脚本自己**不**启动：必须等这里走完，否则会出现"还没鉴权就开始渲染"。
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

  /* -------------------------------------------------------------- 跳登录 */

  function loginUrl() {
    var next = location.pathname + location.search + location.hash;
    return '/login.html?next=' + encodeURIComponent(next);
  }

  function toLogin() {
    location.replace(loginUrl());
  }

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
      QF.data.install(bank);
      return true;
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

  function start() {
    showState({ title: '正在载入…', message: '校验登录状态并读取题库' });

    // 任何请求遇到 401 都直接回登录页，避免用户在半个页面上继续操作
    api.onUnauthorized(toLogin);

    api.auth
      .me()
      .then(function (me) {
        if (!me || !me.user) {
          toLogin();
          return false;
        }
        QF.user = me.user;
        return loadBank().then(function (ok) {
          if (!ok) return false;
          return loadProgress().then(function () {
            return true;
          });
        });
      })
      .then(function (ready) {
        if (!ready) return;
        startPage();
      })
      .catch(function (err) {
        var isNetwork = !err.status;
        replaceState({
          tone: 'bad',
          icon: 'warn',
          title: isNetwork ? '连不上服务器' : '启动失败',
          message: isNetwork
            ? '网络不可用，或后端没有在运行。检查服务后重试。'
            : err.message || '未知错误',
          actions: [
            { label: '重试', onClick: function () { location.reload(); } },
            { label: '回到登录页', onClick: toLogin },
          ],
        });
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }

  QF.boot = { start: start, showState: showState, refreshProgress: refreshProgress };
})();
