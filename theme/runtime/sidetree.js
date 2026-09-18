/* sidetree.js —— 资源树：一棵大树，三个根（对话 / 资料 / 文档），加一个文档图谱位。
 *
 * 用户的原话是"所有的资源全部在左边被收起来，在右边的画布随意组合"，所以这一块的
 * 职责只有两件：**列出来**，以及**把东西送进右边**。送的方式两种 ——
 *   点一下   → 在工作台的当前窗格里开一个标签
 *   拖过去   → 落到拖到的那一格（'text/panes-resource' 由 panes.js 接）
 * 在别的页面（练习中心、错题本……）点树上的东西时，先把"要开什么"记在本机，
 * 再跳去工作台 —— 用户的心智是"我要看这个"，不该因为当前在哪一页而失效。
 *
 * 懒加载：文档库有 1113 篇，展开哪个库才读哪个库的树。
 */
(function () {
  var ui = QF.ui;
  var h = ui.h;
  var api = QF.api;

  var OPEN_KEY = 'qf.sidetree.v1';       // 展开状态
  var PENDING_KEY = 'qf.panes.pending';  // 从别的页面点树时，暂存"要开什么"
  var state = { open: null, busy: false };
  var hostEl = null;

  function readOpen() {
    if (state.open) return state.open;
    var raw = null;
    try { raw = JSON.parse(localStorage.getItem(OPEN_KEY) || '{}'); } catch (e) { raw = {}; }
    state.open = raw && typeof raw === 'object' ? raw : {};
    return state.open;
  }

  function saveOpen() {
    try { localStorage.setItem(OPEN_KEY, JSON.stringify(state.open || {})); } catch (e) { /* 无痕模式忽略 */ }
  }

  /* ------------------------------------------------------------------ 送东西去右边 */

  function onWorkbench() {
    return document.body.dataset.page === 'workbench';
  }

  /** 打开一样资源：在工作台就当场开标签，在别的页面就记下来再跳过去。 */
  function send(kind, ref, title) {
    if (onWorkbench() && QF.panes && QF.panes.open) {
      QF.panes.open(kind, ref, title);
      return;
    }
    // 记在本机：工作台启动时会把它消费掉（比 URL 传参干净，也不用改路由表）
    try {
      localStorage.setItem(PENDING_KEY, JSON.stringify({ kind: kind, ref: ref, title: title }));
    } catch (e) { /* 忽略 */ }
    location.href = 'workbench.html';
  }

  /* ------------------------------------------------------------------ 树零件 */

  function caret(open) {
    var node = h('span.stree__caret');
    node.innerHTML = ui.icon(open ? 'chevronR' : 'chevronR', 12);
    if (open) node.style.transform = 'rotate(90deg)';
    return node;
  }

  function row(level, opts) {
    var node = h('div.stree__row' + (opts.cls ? '.' + opts.cls : ''), {
      title: opts.title || opts.label,
      draggable: opts.drag ? 'true' : null,
      'data-level': String(level),
    });
    node.style.paddingLeft = (8 + level * 12) + 'px';
    if (opts.caret) node.appendChild(caret(!!opts.open));
    else node.appendChild(h('span.stree__caret.stree__caret--none'));
    if (opts.icon) {
      var mark = h('span.stree__icon');
      mark.innerHTML = ui.icon(opts.icon, 13);
      node.appendChild(mark);
    }
    node.appendChild(h('span.stree__label', { text: opts.label }));
    if (opts.count != null) node.appendChild(h('span.stree__count', { text: String(opts.count) }));
    return node;
  }

  /** 一个可展开的组：点标题那一行展开/收起，内容懒加载。 */
  function group(level, opts) {
    var open = !!readOpen()[opts.key];
    var box = h('div.stree__group');
    var head = row(level, {
      label: opts.label,
      icon: opts.icon,
      count: opts.count,
      caret: true,
      open: open,
      cls: 'stree__row--head',
      title: opts.title,
    });
    var body = h('div.stree__body');
    box.appendChild(head);
    box.appendChild(body);

    function paint() {
      var isOpen = !!readOpen()[opts.key];
      var mark = head.querySelector('.stree__caret');
      if (mark) mark.style.transform = isOpen ? 'rotate(90deg)' : '';
      body.hidden = !isOpen;
      if (isOpen && opts.load && !body.dataset.loaded) {
        body.dataset.loaded = '1';
        body.appendChild(h('p.stree__hint', { text: '正在读…' }));
        opts.load(body);
      }
    }

    head.addEventListener('click', function () {
      var now = !readOpen()[opts.key];
      readOpen()[opts.key] = now;
      saveOpen();
      paint();
    });
    paint();
    return box;
  }

  /** 一片叶子：点一下送进右边，也可以拖过去。 */
  function leaf(level, opts) {
    var node = row(level, {
      label: opts.label,
      icon: opts.icon,
      count: opts.count,
      title: opts.title || opts.label,
      drag: !!opts.kind,
    });
    node.classList.add('stree__row--leaf');
    if (opts.dim) node.classList.add('is-dim');
    node.addEventListener('click', function () {
      if (opts.href) { location.href = opts.href; return; }
      if (opts.kind) send(opts.kind, opts.ref, opts.label);
    });
    if (opts.kind) {
      node.addEventListener('dragstart', function (ev) {
        var payload = JSON.stringify({ kind: opts.kind, ref: opts.ref, title: opts.label });
        ev.dataTransfer.setData('text/panes-resource', payload);
        // 有些浏览器只认 text/plain，多写一份不亏
        ev.dataTransfer.setData('text/plain', payload);
        ev.dataTransfer.effectAllowed = 'copy';
      });
    }
    return node;
  }

  function err(box, message) {
    box.appendChild(h('p.stree__hint', { text: message }));
  }

  /* ------------------------------------------------------------------ 三个根 */

  function paintConversations(box) {
    api.get('/chat/conversations').then(function (res) {
      var list = (res && res.conversations) || [];
      if (!list.length) { err(box, '还没有对话'); return; }
      list.forEach(function (one) {
        box.appendChild(leaf(2, {
          label: one.title || '未命名对话',
          icon: 'robot',
          title: (one.preview || '') + (one.count ? '\n' + one.count + ' 条消息' : ''),
          href: 'chat.html',        // 对话页自己管会话切换；将来做成标签时把这里换成 send('chat', …)
        }));
      });
    }).catch(function () { err(box, '读不到对话列表'); });
  }

  function paintItems(box) {
    api.get('/library/items?limit=600').then(function (res) {
      var list = (res && res.items) || [];
      if (!list.length) { err(box, '资料目录里还没扫出条目'); return; }
      list.forEach(function (one) {
        var bits = [];
        if (one.year) bits.push(one.year);
        if (one.kind) bits.push(one.kind);
        box.appendChild(leaf(2, {
          label: one.title || one.citekey,
          icon: 'book',
          count: one.year || null,
          title: (bits.join(' · ') || '') + '\n' + (one.citekey || ''),
          kind: 'doc',
          ref: { citekey: one.citekey },
        }));
      });
    }).catch(function () { err(box, '读不到资料条目'); });
  }

  function paintVault(box, lib) {
    api.get('/notes/tree?lib=' + encodeURIComponent(lib.name)).then(function (tree) {
      var root = h('div');
      box.appendChild(root);
      paintNode(root, tree, 2, lib.name);
      if (!root.childNodes.length) err(box, '这个库里没有笔记');
    }).catch(function () { err(box, '读不到这个库'); });
  }

  function paintNode(box, node, level, lib) {
    var dirs = node.dirs || {};
    Object.keys(dirs).sort().forEach(function (name) {
      var sub = group(level, {
        key: 'dir:' + lib + '/' + name,
        label: name,
        icon: 'list',
        load: function (body) { paintNode(body, dirs[name], level + 1, lib); },
      });
      box.appendChild(sub);
    });
    (node.files || []).forEach(function (file) {
      if (file.kind === 'canvas') {
        box.appendChild(leaf(level, {
          label: file.name,
          icon: 'grip',
          title: '画布 · ' + file.path,
          kind: 'canvas',
          ref: { lib: lib, path: file.path },
        }));
        return;
      }
      if (file.kind !== 'note') return;    // 附件不进树（它们跟着笔记走）
      box.appendChild(leaf(level, {
        label: file.name.replace(/\.md$/, ''),
        icon: 'book',
        title: file.path,
        kind: 'note',
        ref: { lib: lib, path: file.path },
      }));
    });
  }

  function paintVaults(box) {
    api.get('/notes/stats').then(function (data) {
      var libs = (data && data.libraries) || [];
      if (!libs.length) { err(box, '还没有笔记库'); return; }
      libs.forEach(function (lib) {
        box.appendChild(group(1, {
          key: 'vault:' + lib.name,
          label: lib.name,
          icon: 'list',
          count: lib.notes || lib.count || null,
          load: function (body) { paintVault(body, lib); },
        }));
      });
    }).catch(function () { err(box, '读不到笔记库'); });
  }

  /* ------------------------------------------------------------------ 装配 */

  function paint() {
    if (!hostEl) return;
    ui.clear(hostEl);
    var box = h('div.stree');
    hostEl.appendChild(box);

    box.appendChild(group(0, {
      key: 'root:chat',
      label: '对话',
      icon: 'robot',
      load: paintConversations,
    }));
    box.appendChild(group(0, {
      key: 'root:library',
      label: '资料',
      icon: 'book',
      load: paintItems,
    }));
    box.appendChild(group(0, {
      key: 'root:notes',
      label: '文档',
      icon: 'list',
      load: paintVaults,
    }));
    // 文档图谱：还没做（笔记为节点、双链为边）。先把位置留出来，
    // 免得用户以为"这东西没有"，而不是"还没做"
    var graph = leaf(0, { label: '文档图谱', icon: 'target', dim: true, title: '还没做：笔记之间的双链网络（题库的知识图谱在练习中心里）' });
    graph.addEventListener('click', function (ev) {
      ev.stopPropagation();
      ui.toast('文档图谱还没做：笔记为节点、双链为边', 'info', 3200);
    }, true);
    box.appendChild(graph);
  }

  function boot() {
    hostEl = document.getElementById('side-body');
    if (!hostEl) return;
    // 骨架说明换掉（shell.html 里那句"正在接"）
    paint();
  }

  QF.sidetree = { paint: paint, send: send };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
