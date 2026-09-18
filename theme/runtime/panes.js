/* 可组合窗格 —— tmux 式。
 *
 * 为什么是"树"而不是"几栏"：
 *   tmux 的窗口是一棵**二叉树**（每个内部节点是一次拆分，叶子才是窗格），
 *   所以"左三右七、右下再上下分"这种任意组合都能表达，而"三栏"表达不了 ——
 *   固定几列等于把树写死成一层，用户永远只能在那一层里换内容。
 *
 * 三条与 tmux 对齐的规矩：
 *   1. **拆分是就地拆**：拆的是**当前窗格**（活跃窗格），不是"再开一栏"。
 *      所以同一个位置能拆了拆、合了合，最后收成一格或铺满一整片。
 *   2. **叶子不重挂**：布局变化时只**搬动**已有 DOM（`appendChild` 搬家会保留
 *      内部状态：滚动位置、画布缩放、正在编辑的输入框），绝不重建 ——
 *      否则每拆一次，旁边的画布就"复位"一次，那没法用。
 *   3. **比例是相对的**：存的是 0~1 的比例而不是像素，所以窗口一缩放不用重算，
 *      纵向也不会因为顶栏高度变化而错位。
 *
 * 命名：一律用 `panes` 前缀。**不要用 `.pn` 或 `.wb`** —— 那两个在
 *   `app.css` 里已经各有主人（`.wb` 是错题本的网格，一撞就把窗格裁成 340px 宽的窄条，
 *   而且数值全是对的、只有看图才发现）。
 *
 * 视图契约（与 `canvas.js` 那份一致，它是仓库里唯一达标的范例）：
 *   QF.panes.register('note', {
 *     title: '笔记',
 *     icon: 'book',
 *     mount(host, opts),    // 往 host 里渲染；opts 是该窗格自己的参数
 *     unmount(),            // 可选：窗格关掉时收摊（清定时器、存未落盘的改动）
 *   });
 * 视图只认这一件事：给它一个容器，它把自己画进去。
 */
(function () {
  var ui = QF.ui;
  var h = ui.h;

  var KEY = 'qf.panes.v1';           // 布局存这儿：只有"哪个窗格装什么、怎么分、多大比"，不含内容状态
  var MIN_RATIO = 0.12;              // 拖到极窄就抓不住了，留个下限
  var MAX_RATIO = 0.88;

  var views = {};                    // 视图注册表：type -> { title, icon, mount, unmount }
  var live = {};                     // 窗格实例：leafId -> { el, bodyEl, headEl, def, cleanup }
  var seq = 0;                       // 本地 id 计数（只在本机布局里用，不进任何持久数据）
  var hostEl = null;
  var saveLater = null;

  var state = {
    root: null,                      // 二叉树：{ kind:'split', dir, ratio, a, b } | { kind:'stage', id, view, opts }
    active: null,                    // 活跃窗格 id（拆分/关闭/换内容都作用在它身上，同 tmux 的 current pane）
    zoomed: null,                    // 最大化中的窗格 id（同 tmux 的 zoom：只显示它，再按一次还原）
  };

  function uid(prefix) {
    seq += 1;
    return prefix + seq;
  }

  function stage(view, opts) {
    return { kind: 'stage', id: uid('p'), view: view || 'blank', opts: opts || {} };
  }

  /* ------------------------------------------------------------------ 树的读写 */

  function walk(node, fn, parent) {
    if (!node) return;
    fn(node, parent);
    if (node.kind === 'split') {
      walk(node.a, fn, node);
      walk(node.b, fn, node);
    }
  }

  function findLeaf(id) {
    var hit = null;
    walk(state.root, function (node) {
      if (node.kind === 'stage' && node.id === id) hit = node;
    });
    return hit;
  }

  function leaves() {
    var out = [];
    walk(state.root, function (node) {
      if (node.kind === 'stage') out.push(node);
    });
    return out;
  }

  /** 找某个节点的父亲 —— **拆分节点也要能找**（关窗格时要往上接祖父）。
   *  踩过：这里原先只匹配 `kind === 'stage'`，于是 `parentOf(拆分节点)` 永远返回 null，
   *  `close()` 拿不到祖父就把整棵树替换成了兄弟那一支 —— 表现为"关掉一格，剩一格"。 */
  function parentOf(id) {
    var hit = null;
    walk(state.root, function (node, parent) {
      if (node.id === id) hit = parent;
    });
    return hit;
  }

  /* ------------------------------------------------------------------ 操作 */

  /** 就地拆分当前窗格：dir='row' 左右并排，dir='col' 上下叠。 */
  function split(dir, view) {
    var target = findLeaf(state.active) || leaves()[0];
    if (!target) return;
    var fresh = stage(view || 'blank');
    var parent = parentOf(target.id);
    var node = {
      kind: 'split',
      id: uid('s'),
      dir: dir === 'col' ? 'col' : 'row',
      ratio: 0.5,
      a: target,
      b: fresh,
    };
    if (!parent) {
      state.root = node;
    } else if (parent.a === target) {
      parent.a = node;
    } else {
      parent.b = node;
    }
    state.active = fresh.id;
    state.zoomed = null;             // 拆分时取消最大化，否则新窗格看不见（tmux 也是这个行为）
    render();
    save();
    var box = live[fresh.id];
    // 新窗格淡入一下：拆完一眼能看出"新的是哪个"（`viewSwap` 收的是回调，不是元素，别拿它当动画）
    if (box) box.el.classList.add('is-fresh');
  }

  /** 关掉当前窗格：它的父拆分塌成兄弟那一支。 */
  function close(id) {
    var target = id || state.active;
    var all = leaves();
    if (all.length <= 1) {
      // 只剩一个就不关了：关掉会剩一片空白，用户会以为"坏了"
      ui.toast('还剩最后一个窗格，先拆一个再关这个', 'warn');
      return;
    }
    var parent = parentOf(target);
    if (!parent) return;
    var sibling = parent.a === findLeaf(target) ? parent.b : parent.a;
    var grand = parentOf(parent.id) || null;

    if (!grand) {
      state.root = sibling;
    } else if (grand.a === parent) {
      grand.a = sibling;
    } else {
      grand.b = sibling;
    }

    var box = live[target];
    if (box) {
      if (box.def && box.def.unmount) {
        try { box.def.unmount(box.leaf); } catch (e) { /* 收摊失败不该拦住关窗 */ }
      }
      if (box.cleanup) { try { box.cleanup(); } catch (e) { /* 同上 */ } }
      box.el.remove();
      delete live[target];
    }
    if (state.zoomed === target) state.zoomed = null;
    state.active = sibling.id;
    render();
    save();
  }

  /** 换当前窗格装什么（tmux 里相当于把那个 pane 里的程序换掉）。 */
  function setView(view, opts) {
    var target = findLeaf(state.active) || leaves()[0];
    if (!target) return;
    target.view = view;
    target.opts = opts || {};
    var box = live[target.id];
    if (box) {
      if (box.def && box.def.unmount) {
        try { box.def.unmount(box.leaf); } catch (e) { /* 忽略 */ }
      }
      if (box.cleanup) { try { box.cleanup(); } catch (e) { /* 忽略 */ } }
      box.cleanup = null;
      mountInto(box, target);
    }
    render();
    save();
  }

  /** 最大化 / 还原（tmux 的 zoom）。 */
  function zoom(id) {
    var target = id || state.active;
    state.zoomed = state.zoomed === target ? null : target;
    render();
    save();
  }

  function focus(id) {
    if (state.active === id) return;
    state.active = id;
    render();
    save();
  }

  function reset(layout) {
    Object.keys(live).forEach(function (id) {
      var box = live[id];
      if (box.cleanup) { try { box.cleanup(); } catch (e) { /* 忽略 */ } }
    });
    live = {};
    state.root = layout || stage('blank');
    state.active = leaves()[0].id;
    state.zoomed = null;
    render();
    save();
  }

  /* ------------------------------------------------------------------ 渲染 */

  function render() {
    if (!hostEl) return;
    hostEl.innerHTML = '';
    var root = state.zoomed ? findLeaf(state.zoomed) : state.root;
    if (!root) {
      hostEl.appendChild(emptyHint());
      return;
    }
    hostEl.appendChild(buildTree(root));
    paintStatus();
  }

  function buildTree(node) {
    if (node.kind === 'stage') return paneEl(node);

    var wrap = h('div.panes__split' + (node.dir === 'col' ? '.is-col' : '.is-row'));
    var a = buildTree(node.a);
    var b = buildTree(node.b);
    var ratio = Math.min(MAX_RATIO, Math.max(MIN_RATIO, node.ratio == null ? 0.5 : node.ratio));
    a.style.flex = ratio + ' 1 0px';
    b.style.flex = (1 - ratio) + ' 1 0px';
    wrap.appendChild(a);
    wrap.appendChild(divider(node, wrap));
    wrap.appendChild(b);
    return wrap;
  }

  function divider(node, wrap) {
    var bar = h('div.panes__div' + (node.dir === 'col' ? '.is-col' : '.is-row'), {
      title: '拖动调整比例',
      role: 'separator',
    });
    bar.addEventListener('pointerdown', function (ev) {
      ev.preventDefault();
      var rect = wrap.getBoundingClientRect();
      var vertical = node.dir === 'col';            // 上下叠 -> 拖动改高度
      bar.classList.add('is-dragging');
      document.body.classList.add(vertical ? 'panes-resizing-v' : 'panes-resizing-h');
      try { bar.setPointerCapture(ev.pointerId); } catch (e) { /* 老浏览器忽略 */ }

      function onMove(move) {
        var next = vertical
          ? (move.clientY - rect.top) / (rect.height || 1)
          : (move.clientX - rect.left) / (rect.width || 1);
        node.ratio = Math.min(MAX_RATIO, Math.max(MIN_RATIO, next));
        // 只改这一层的两条 flex，不整棵重画 —— 重画会打断正在拖的手感
        wrap.firstChild.style.flex = node.ratio + ' 1 0px';
        wrap.lastChild.style.flex = (1 - node.ratio) + ' 1 0px';
      }
      function onUp() {
        bar.classList.remove('is-dragging');
        document.body.classList.remove('panes-resizing-v', 'panes-resizing-h');
        bar.removeEventListener('pointermove', onMove);
        bar.removeEventListener('pointerup', onUp);
        bar.removeEventListener('pointercancel', onUp);
        save();
      }
      bar.addEventListener('pointermove', onMove);
      bar.addEventListener('pointerup', onUp);
      bar.addEventListener('pointercancel', onUp);
    });
    // 双击分隔条：对半分（拆歪了想回正时的快捷）
    bar.addEventListener('dblclick', function () {
      node.ratio = 0.5;
      wrap.firstChild.style.flex = '0.5 1 0px';
      wrap.lastChild.style.flex = '0.5 1 0px';
      save();
    });
    return bar;
  }

  function paneEl(leaf) {
    var box = live[leaf.id];
    if (!box) {
      box = live[leaf.id] = makePane(leaf);
    }
    box.el.classList.toggle('is-active', state.active === leaf.id);
    box.el.classList.toggle('is-zoomed', state.zoomed === leaf.id);
    return box.el;
  }

  function makePane(leaf) {
    var body = h('div.panes__body');
    var title = h('span.panes__title');
    var head = h('div.panes__head', null, title, h('span.panes__spacer'), buttons());
    var el = h('section.panes__pane', { 'data-pane': leaf.id }, head, body);

    var box = { el: el, bodyEl: body, headEl: head, titleEl: title, leaf: leaf, def: null, cleanup: null };

    // 点哪儿哪儿是活跃窗格（同 tmux：键盘与拆分都作用在活跃窗格上）
    el.addEventListener('pointerdown', function () { focus(leaf.id); }, true);

    function buttons() {
      var wrap = h('div.panes__acts');
      var pick = h('button.panes__act', { title: '这个窗格装什么', type: 'button' });
      pick.innerHTML = ui.icon('list', 14);
      pick.addEventListener('click', function (ev) {
        ev.stopPropagation();
        openPicker(leaf, wrap);
      });
      var row = h('button.panes__act', { title: '左右拆（Alt+\\）', type: 'button' });
      row.innerHTML = ui.icon('chevronR', 14);
      row.addEventListener('click', function (ev) { ev.stopPropagation(); state.active = leaf.id; split('row'); });
      var col = h('button.panes__act', { title: '上下拆（Alt+-）', type: 'button' });
      col.innerHTML = ui.icon('list', 14);
      col.style.transform = 'rotate(90deg)';
      col.addEventListener('click', function (ev) { ev.stopPropagation(); state.active = leaf.id; split('col'); });
      var max = h('button.panes__act', { title: '最大化（Alt+Z）', type: 'button' });
      max.innerHTML = ui.icon('target', 14);
      max.addEventListener('click', function (ev) { ev.stopPropagation(); zoom(leaf.id); });
      var shut = h('button.panes__act', { title: '关掉这个窗格（Alt+W）', type: 'button' });
      shut.innerHTML = ui.icon('close', 14);
      shut.addEventListener('click', function (ev) { ev.stopPropagation(); close(leaf.id); });
      wrap.appendChild(pick);
      wrap.appendChild(row);
      wrap.appendChild(col);
      wrap.appendChild(max);
      wrap.appendChild(shut);
      return wrap;
    }

    mountInto(box, leaf);
    return box;
  }

  function mountInto(box, leaf) {
    var def = views[leaf.view];
    box.def = def || null;
    box.leaf = leaf;
    box.titleEl.textContent = def ? def.title : leaf.view;
    box.bodyEl.innerHTML = '';
    if (!def) {
      box.bodyEl.appendChild(missing(leaf));
      return;
    }
    // 视图自己可能会往别的容器挂浮层，`mount` 返回的清理函数由引擎保管
    var out = def.mount(box.bodyEl, leaf.opts || {}, { leaf: leaf });
    box.cleanup = typeof out === 'function' ? out : null;
  }

  function missing(leaf) {
    return h('div.panes__missing', null,
      h('p.panes__missing-t', null, '这个窗格要装「' + leaf.view + '」，但那个视图还没接进来'),
      h('p.panes__missing-s', null, '点上面的列表图标换一个，或者把它的 mount(host, opts) 写好并 register 一下'));
  }

  function emptyHint() {
    return h('div.panes__missing', null, h('p.panes__missing-t', null, '窗格是空的'));
  }

  /** 换内容的小菜单：列出所有已注册的视图。 */
  function openPicker(leaf, anchor) {
    var open = document.querySelector('.panes__menu');
    if (open) open.remove();
    var menu = h('div.panes__menu', { role: 'menu' });
    Object.keys(views).forEach(function (type) {
      var def = views[type];
      var item = h('button.panes__menu-item' + (leaf.view === type ? '.is-on' : ''), { type: 'button' });
      item.innerHTML = ui.icon(def.icon || 'list', 14);
      item.appendChild(h('span', null, def.title));
      item.addEventListener('click', function (ev) {
        ev.stopPropagation();
        menu.remove();
        state.active = leaf.id;
        if (leaf.view !== type) setView(type);
      });
      menu.appendChild(item);
    });
    if (!Object.keys(views).length) {
      menu.appendChild(h('p.panes__menu-empty', null, '还没有注册任何视图'));
    }
    document.body.appendChild(menu);
    var box = anchor.getBoundingClientRect();
    menu.style.top = Math.round(box.bottom + 6) + 'px';
    menu.style.left = Math.round(Math.min(box.left, window.innerWidth - menu.offsetWidth - 12)) + 'px';

    function away(ev) {
      if (!menu.contains(ev.target)) {
        menu.remove();
        document.removeEventListener('pointerdown', away, true);
      }
    }
    setTimeout(function () { document.addEventListener('pointerdown', away, true); }, 0);
  }

  /* ------------------------------------------------------------------ 键盘（tmux 那几个动作的无前缀版本） */

  function onKey(ev) {
    if (!ev.altKey || ev.ctrlKey || ev.metaKey) return;
    var key = ev.key;
    if (key === '\\') { ev.preventDefault(); split('row'); }
    else if (key === '-') { ev.preventDefault(); split('col'); }
    else if (key === 'z' || key === 'Z') { ev.preventDefault(); zoom(); }
    else if (key === 'w' || key === 'W') { ev.preventDefault(); close(); }
  }

  /* ------------------------------------------------------------------ 持久化与状态栏 */

  function layout() {
    return { root: state.root, active: state.active, zoomed: state.zoomed };
  }

  function save() {
    // 拖动分隔条时每次 pointermove 都存一下太浪费：合并到一帧之后
    if (saveLater) return;
    saveLater = setTimeout(function () {
      saveLater = null;
      try {
        localStorage.setItem(KEY, JSON.stringify(layout()));
      } catch (e) { /* 无痕模式/配额满：布局存不上不影响用 */ }
    }, 250);
  }

  function load(defaultLayout) {
    var raw = null;
    try { raw = localStorage.getItem(KEY); } catch (e) { raw = null; }
    if (raw) {
      try {
        var got = JSON.parse(raw);
        if (got && got.root && got.root.kind) {
          state.root = got.root;
          state.zoomed = findLeaf(got.zoomed) ? got.zoomed : null;
          state.active = findLeaf(got.active) ? got.active : leaves()[0].id;
          return;
        }
      } catch (e) { /* 坏掉的布局不如不要，退回默认 */ }
    }
    state.root = defaultLayout || stage('blank');
    state.active = leaves()[0].id;
    state.zoomed = null;
  }

  function paintStatus() {
    var all = leaves();
    var current = findLeaf(state.active);
    var def = current ? views[current.view] : null;
    var order = all
      .map(function (leaf, i) { return leaf.id === state.active ? '[' + (i + 1) + ']' : String(i + 1); })
      .join(' ');
    // `ui.statusbar.set` 收的是 **item 数组**（不是 key/value）；顺手把四个键位摆在状态栏里，
    // 免得"能拆"这件事只有看过源码的人知道
    ui.statusbar.set([
      { kind: 'stat', label: '窗格', value: (def ? def.title : '空') + ' ' + order + (state.zoomed ? ' · 最大化中' : '') },
      { spacer: true },
      { kind: 'hint', key: 'Alt+\\', label: '左右拆' },
      { kind: 'hint', key: 'Alt+-', label: '上下拆' },
      { kind: 'hint', key: 'Alt+Z', label: '最大化' },
      { kind: 'hint', key: 'Alt+W', label: '关闭' },
    ]);
  }

  /* ------------------------------------------------------------------ 出厂 */

  function mount(host, options) {
    hostEl = host;
    host.classList.add('panes');
    if (options && options.layout) {
      state.root = options.layout;
      state.active = leaves()[0].id;
    } else {
      // 没存过布局就用调用方给的默认布局（传 `layout` 则是强制用这一份）
      load(options && options.default);
    }
    // 布局里的 id 是上次那份，恢复后 live 里没有它们；paneEl 会按需建
    Object.keys(live).forEach(function (id) { delete live[id]; });
    render();
    document.addEventListener('keydown', onKey);
    return function () {
      document.removeEventListener('keydown', onKey);
      hostEl = null;
    };
  }

  function register(type, def) {
    views[type] = def || {};
  }

  QF.panes = {
    mount: mount,
    register: register,
    state: state,
    layout: layout,
    split: split,
    close: close,
    zoom: zoom,
    focus: focus,
    setView: setView,
    reset: reset,
    views: views,
    save: save,
  };
})();
