/* settings.js —— 设置页（全屏，取代原来那张浮窗）。
 *
 * 表单**不在这里**：`QF.settings.build()`（runtime/shell.js）造那四段
 * （AI / 联网搜索 / 外观 / 数据），与以前浮窗用的是同一份代码。
 * 这里只负责版式：左边一列分类，右边是内容；点分类滚到那一段，滚动时高亮。
 *
 * 这样改控件去 shell.js、改版式来这里，设置项不会出现两份。
 */
(function () {
  var ui = QF.ui;
  var h = ui.h;

  // key 与 buildSettings() 里四段的顺序一一对应（第 i 段配第 i 组）
  var GROUPS = [
    { key: 'ai', label: 'AI', hint: '密钥 / 模型 / 上下文' },
    { key: 'search', label: '联网搜索', hint: '服务商与密钥' },
    { key: 'obsidian', label: 'Obsidian', hint: 'vault 路径与连接' },
    { key: 'appearance', label: '外观', hint: '主题 / 字号 / 宽度' },
    { key: 'data', label: '数据', hint: '导入 / 导出 / 清空' },
  ];

  var root = null;
  var navEl = null;
  var sections = [];
  var scrollRaf = 0;

  /** 真正在滚的是舞台里那个 `.view`（见 app.css 的 `.stage > .view`），不是 window。 */
  function viewEl() {
    return document.querySelector('.view');
  }

  function markActive(key) {
    if (!navEl) return;
    var items = navEl.querySelectorAll('.setnav__item');
    for (var i = 0; i < items.length; i++) {
      items[i].classList.toggle('is-on', items[i].dataset.to === key);
    }
  }

  /** 读到哪一段：以"段落顶边越过视口上方 48px"为界，取最后一个越过的。
   *
   *  到底时直接高亮**最后一段**：最后一段下面没有足够内容把它顶到视口上方，
   *  按"越过 48px"判它永远进不了选中态 —— 点「数据」只会滚到底，导航却停在上一项
   *  （实测：点数据滚到 825，高亮仍是「联网搜索」）。 */
  function activeKeyNow() {
    var view = viewEl();
    if (!view) return sections.length ? sections[0].dataset.setKey : '';
    if (view.scrollTop + view.clientHeight >= view.scrollHeight - 2) {
      return sections.length ? sections[sections.length - 1].dataset.setKey : '';
    }
    var top = view.getBoundingClientRect().top;
    var key = sections.length ? sections[0].dataset.setKey : '';
    for (var i = 0; i < sections.length; i++) {
      if (sections[i].getBoundingClientRect().top - top <= 48) key = sections[i].dataset.setKey;
      else break;
    }
    return key;
  }

  function onScroll() {
    if (scrollRaf) return;
    scrollRaf = window.requestAnimationFrame(function () {
      scrollRaf = 0;
      markActive(activeKeyNow());
    });
  }

  /** 点分类：滚到那一段（留 12px 顶部余量；`.view` 自己有内距，别顶死）。 */
  function scrollToKey(key) {
    var view = viewEl();
    if (!view) return;
    for (var i = 0; i < sections.length; i++) {
      if (sections[i].dataset.setKey !== key) continue;
      var delta = sections[i].getBoundingClientRect().top - view.getBoundingClientRect().top;
      view.scrollTo({ top: view.scrollTop + delta - 12, behavior: 'smooth' });
      markActive(key);
      return;
    }
  }

  function render() {
    if (!root) return;
    var view = viewEl();
    var keep = view ? view.scrollTop : 0;

    sections = [];
    var page = h('div.setpage');
    var nav = h('aside.setpage__nav');
    nav.appendChild(h('h1.setpage__title', { text: '设置' }));
    nav.appendChild(h('p.setpage__sub', { text: '改完立刻生效，全部存在本机。' }));

    navEl = h('nav.setnav', { 'aria-label': '设置分类' });
    GROUPS.forEach(function (group) {
      var item = h('button.setnav__item', {
        type: 'button',
        'data-to': group.key,
        onClick: function () { scrollToKey(group.key); },
      }, h('span', { text: group.label }),
        h('span.setnav__hint', { text: group.hint }));
      navEl.appendChild(item);
    });
    nav.appendChild(navEl);

    var body = h('div.setpage__body');
    var form = QF.settings.build();
    var boxes = form.querySelectorAll('.form__section');
    for (var i = 0; i < boxes.length; i++) {
      var key = (GROUPS[i] && GROUPS[i].key) || 'g' + i;
      boxes[i].dataset.setKey = key;
      boxes[i].setAttribute('id', 'set-' + key);
      sections.push(boxes[i]);
    }
    body.appendChild(form);

    page.appendChild(nav);
    page.appendChild(body);
    root.textContent = '';
    root.appendChild(page);

    if (view) {
      view.removeEventListener('scroll', onScroll);
      view.addEventListener('scroll', onScroll, { passive: true });
    }
    markActive(activeKeyNow());
    if (view && keep) view.scrollTop = keep;
  }

  function boot() {
    root = document.getElementById('app-root');
    if (!root) return;
    QF.shell.mount({});
    /* 先取**设置文件**那份（它是权威）：CLI 或脚本改过 `config/settings.local.json`，
     * 打开这一页就该看到 —— 否则界面上永远是浏览器缓存里那份旧的。
     * 取不到再退回 `/progress` 快照；两条都失败就用本机缓存。
     * `pullSettings()` 内部会 `ui.theme.init()`，主题在第一次 render 前就定好。 */
    var ready = QF.api.get('/settings').then(function (out) {
      if (out && out.settings) QF.store.adoptSettings(out.settings);
      return QF.shell.pullSettings ? QF.shell.pullSettings() : null;
    }).catch(function () {
      return QF.shell.pullSettings ? QF.shell.pullSettings() : null;
    });
    ready.then(render, render);

    /* 导入 / 清空之后，数据那一段的几个数会变 —— 整页重画一次，滚动位置保住。
     * 这类动作刚发生，焦点不在输入框里，重建不会把谁正在写的东西冲掉。 */
    document.addEventListener('qf:data-changed', function () {
      render();
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
