/* ===========================================================================
 * router.js —— 极简 hash 路由
 *
 * 这个应用原本是单文件离线页，视图切换全靠内存里的 state.view，
 * 刷新就回到工作台、也没法把「正在复习」这类状态发给别人。
 * 改成 hash 路由的最小代价方案：用 #/practice 这类地址，
 * 不占用服务端路由（后端只管 /api 与静态文件），刷新与前进后退都自然可用。
 *
 * 只做双向同步，不接管渲染：视图由 app.js 决定，这里只负责地址栏。
 * ========================================================================= */
(function () {
  'use strict';

  var QF = (window.QF = window.QF || {});

  // 视图 → 地址。home 用 #/home 而不是空 hash，
  // 这样从其它视图点「工作台」能明确产生一条历史记录
  var HASH = {
    home: '#/home',
    practice: '#/practice',
    paper: '#/paper',
    review: '#/review',
  };

  var VIEW = {
    '': 'home',
    '#': 'home',
    '#/': 'home',
    '#/home': 'home',
    '#/practice': 'practice',
    '#/paper': 'paper',
    '#/review': 'review',
  };

  var app = null;
  var applying = false;

  function viewFromLocation() {
    return VIEW[location.hash] || 'home';
  }

  /** 视图变化时回写地址（replace 用于初始同步，避免多出一条历史记录） */
  function push(view) {
    var hash = HASH[view];
    if (!hash || location.hash === hash) return;
    history.pushState(null, '', hash);
  }

  function applyFromLocation() {
    if (!app) return;
    var view = viewFromLocation();
    var current = app.currentView();
    if (view === current) return;
    applying = true;
    try {
      app.setView(view);
    } finally {
      applying = false;
    }
  }

  function start(appApi) {
    app = appApi;

    window.addEventListener('hashchange', applyFromLocation);
    // pushState 不触发 hashchange，前进/后退才触发；
    // 监听 popstate 覆盖 pushState 场景下的历史移动
    window.addEventListener('popstate', applyFromLocation);

    // 首次进入：把地址规范化成 #/home，再按它决定初始视图
    var view = viewFromLocation();
    if (!location.hash) history.replaceState(null, '', HASH.home);
    if (view !== app.currentView()) {
      applying = true;
      try {
        app.setView(view);
      } finally {
        applying = false;
      }
    }
  }

  /** 供 app.js 判断当前是否由路由驱动（避免重复 push） */
  function isApplying() {
    return applying;
  }

  QF.router = {
    start: start,
    push: push,
    isApplying: isApplying,
    hashFor: function (view) {
      return HASH[view] || HASH.home;
    },
  };
})();
