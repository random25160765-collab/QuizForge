/* home.js —— 主页（暂时空白）。
 *
 * 资源栏与窗格组合逻辑已经全部移除（用户："主页的资源栏和窗格组合逻辑全部不要，
 * 暂时先留一个空白页面"）。这一页现在只做一件事：把外壳（活动栏 / 状态栏）挂起来，
 * 正文留白 —— 等 Obsidian 桥接做好之后再往这里填内容。
 */
(function () {
  function boot() {
    QF.shell.mount({});
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
