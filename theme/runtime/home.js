/* home.js —— 主页。
 *
 * 2026-11-07 起这一页是空白的（用户："资源栏和窗格组合逻辑全部不要"）；
 * 2026-11-08 用户说"空白的首页可以放东西了"，于是先放**今天的状态**：
 * 一条三段精力带 + 两个打卡按钮（从日程页左栏挪过来，见 `wellbeing.js`）。
 *
 * 只放这一件东西是有意的：首页是"今天要不要开工、先干哪件"的地方，
 * 它该回答的是**身体现在怎么样**，而不是再摆一屏日程 —— 日程有它自己的页。
 */
(function () {
  'use strict';

  var QF = window.QF;
  var ui = QF.ui;
  var h = ui.h;

  var WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

  function dateLine(now) {
    return now.getMonth() + 1 + '月' + now.getDate() + '日 · ' + WEEKDAYS[now.getDay()];
  }

  function hello(now) {
    var hour = now.getHours();
    if (hour < 5) return '夜深了';
    if (hour < 9) return '早上好';
    if (hour < 12) return '上午好';
    if (hour < 14) return '中午好';
    if (hour < 18) return '下午好';
    if (hour < 23) return '晚上好';
    return '夜深了';
  }

  function boot() {
    QF.shell.mount({});

    var root = document.getElementById('app-root');
    if (!root) return;
    // 每页只画一次：这个页面没有别的状态，重进靠整页加载
    ui.clear(root);

    var now = new Date();
    var inner = h(
      'div.home__inner',
      null,
      h('div.home__date', { text: dateLine(now) }),
      h('h1.home__hello', { text: hello(now) })
    );
    var slot = h('div.home__health');
    inner.appendChild(slot);

    // 设置入口。桌面上它由活动栏那颗齿轮承担，这一行只在手机上出现
    //（显隐交给 mobile.css，脚本不分平台判断）。底栏只有五个主入口，
    // 设置属于"我的"，挂在主页底部才不占那五格里的任何一格。
    inner.appendChild(
      h('a.home__settings', { href: 'settings.html', 'aria-label': '设置' },
        h('span', { text: '设置' }),
        h('span.home__settings__arrow', { text: '›' }))
    );

    root.appendChild(h('div.home', null, inner));
    // 主页没有列表，这一页期待同步的是"今天的状态"（打卡与精力带）
    if (QF.pullSync) {
      QF.pullSync.attach(inner, {
        onDone: function () {
          if (QF.wellbeing && QF.wellbeing.refresh) QF.wellbeing.refresh();
        },
      });
    }

    if (QF.wellbeing && QF.wellbeing.mount) QF.wellbeing.mount(slot);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
