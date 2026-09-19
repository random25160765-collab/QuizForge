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
 *      同一条道理也用在**标签**上：切标签只切显示，不重建内容。
 *   3. **比例是相对的**：存的是 0~1 的比例而不是像素，所以窗口一缩放不用重算，
 *      纵向也不会因为顶栏高度变化而错位。
 *
 * 窗格里装的是**标签**（照参考图：每个分组顶上一条自己的标签栏）：
 *   一个窗格可以开好几样东西、来回切、单独关掉。`open()` 是唯一的入口 ——
 *   资源树上点一下、从树上拖进来，落到的地方都是"当前窗格的当前标签"。
 *
 * 命名：一律用 `panes` 前缀。**不要用 `.pn` 或 `.wb`** —— 那两个在
 *   `app.css` 里已经各有主人（`.wb` 是错题本的网格，一撞就把窗格裁成 340px 宽的窄条，
 *   而且数值全是对的、只有看图才发现）。
 *
 * 视图契约（与 `canvas.js` 那份一致，它是仓库里唯一达标的范例）：
 *   QF.panes.register('note', {
 *     title: '笔记',
 *     icon: 'book',
 *     mount(host, opts),    // 往 host 里渲染；opts 是该标签自己的参数
 *     unmount(opts),        // 可选：标签关掉时收摊（清定时器、存未落盘的改动）
 *   });
 * 视图只认这一件事：给它一个容器，它把自己画进去。
 */
(function () {
  var ui = QF.ui;
  var h = ui.h;

  // 布局格式变过一次（叶子从"一个视图"变成"一串标签"），所以换个键：
  // 旧的那份读不出来就自然退回默认布局，不需要写迁移代码
  var KEY = 'qf.panes.v2';
  var MIN_RATIO = 0.12;
  var MAX_RATIO = 0.88;

  var views = {};                    // 视图注册表：type -> { title, icon, mount, unmount, key }
  var live = {};                     // 窗格实例：leafId -> { el, bodyEl, tabsEl, hosts, mounted }
  var seq = 0;
  var hostEl = null;
  var saveLater = null;

  var state = {
    root: null,                      // { kind:'split', dir, ratio, a, b } | { kind:'stage', id, at, tabs:[…] }
    active: null,
    zoomed: null,
  };

  function uid(prefix) {
    seq += 1;
    return prefix + seq;
  }

  /* ------------------------------------------------------------------ 标签 */

  /**
   * 把老形状的叶子升级成新形状。
   *
   * 踩过：叶子从"一个视图"（`{view, opts}`）改成"一串标签"（`{tabs, at}`）时，
   * 我漏了**启动那份默认布局**还是老写法 —— 结果工作台一打开，两个窗格一个标签
   * 都没有，只剩个「+」，看着像空的。所以这里统一兜住：凡是 `view` 有、`tabs` 没有的，
   * 就当成"一个标签"补上（顺便把早先存下来的布局也一起兼容了）。
   */
  function normalize(node) {
    if (!node || typeof node !== 'object') return node;
    if (node.kind === 'split') {
      normalize(node.a);
      normalize(node.b);
      return node;
    }
    if (node.kind === 'stage' && !Array.isArray(node.tabs)) {
      var one = makeTab(node.view || 'blank', node.opts, node.title);
      node.tabs = [one];
      node.at = 0;
      delete node.view;
      delete node.opts;
    }
    return node;
  }

  /** 一个标签的身份：同一样东西开两次，应当**切到已有那个**而不是再开一个。 */
  function tabKey(view, opts) {
    var def = views[view];
    if (def && def.key) return view + ':' + def.key(opts || {});
    return view + ':' + JSON.stringify(opts || {});
  }

  function makeTab(view, opts, title) {
    return { key: tabKey(view, opts), view: view, opts: opts || {}, title: title || '' };
  }

  function stage(view, opts, title) {
    return { kind: 'stage', id: uid('p'), at: 0, tabs: [makeTab(view || 'blank', opts, title)] };
  }

  function activeTab(leaf) {
    if (!leaf || !leaf.tabs || !leaf.tabs.length) return null;
    var at = leaf.at || 0;
    if (at < 0 || at >= leaf.tabs.length) at = 0;
    return leaf.tabs[at];
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
    if (box) box.el.classList.add('is-fresh');
  }

  /** 关掉当前窗格：它的父拆分塌成兄弟那一支。 */
  function close(id) {
    var target = id || state.active;
    var all = leaves();
    if (all.length <= 1) {
      ui.toast('还剩最后一个窗格，先拆一个再关这个', 'warn');
      return;
    }
    var leaf = findLeaf(target);
    var parent = parentOf(target);
    if (!parent || !leaf) return;
    var sibling = parent.a === leaf ? parent.b : parent.a;
    var grand = parentOf(parent.id) || null;

    if (!grand) {
      state.root = sibling;
    } else if (grand.a === parent) {
      grand.a = sibling;
    } else {
      grand.b = sibling;
    }

    dropPane(target);
    if (state.zoomed === target) state.zoomed = null;
    state.active = sibling.id;
    render();
    save();
  }

  /** 打开一样东西：落在**当前窗格**里。已经开着就切过去，不重复开。 */
  function open(view, opts, title) {
    var leaf = findLeaf(state.active) || leaves()[0];
    if (!leaf) return;
    if (!leaf.tabs) leaf.tabs = [];
    var want = tabKey(view, opts);
    var at = -1;
    leaf.tabs.forEach(function (item, i) {
      if (item.key === want) at = i;
    });
    if (at < 0) {
      // 唯一一个还是"空"标签时，直接把它换成要开的东西（否则会留下一个空标签）
      if (leaf.tabs.length === 1 && leaf.tabs[0].view === 'blank') {
        leaf.tabs[0] = makeTab(view, opts, title);
        at = 0;
      } else {
        leaf.tabs.push(makeTab(view, opts, title));
        at = leaf.tabs.length - 1;
      }
    } else if (title) {
      leaf.tabs[at].title = title;   // 名字可能会变（改了标题），以最新的为准
    }
    leaf.at = at;
    render();
    save();
  }

  /** 关掉某个标签。关掉最后一个就退回"空"标签（窗格本身还在）。 */
  function closeTab(paneId, key) {
    var leaf = findLeaf(paneId || state.active);
    if (!leaf || !leaf.tabs) return;
    var at = -1;
    leaf.tabs.forEach(function (item, i) {
      if (item.key === key) at = i;
    });
    if (at < 0) return;
    dropTab(leaf, leaf.tabs[at]);
    leaf.tabs.splice(at, 1);
    if (!leaf.tabs.length) leaf.tabs.push(makeTab('blank'));
    if (leaf.at >= leaf.tabs.length) leaf.at = leaf.tabs.length - 1;
    if (leaf.at > at) leaf.at -= 1;
    render();
    save();
  }

  function activateTab(paneId, key) {
    var leaf = findLeaf(paneId);
    if (!leaf || !leaf.tabs) return;
    leaf.tabs.forEach(function (item, i) {
      if (item.key === key) leaf.at = i;
    });
    state.active = leaf.id;
    // 换标签 = 换一整块内容（用户："不同页面…之间的切换都太过生硬"）。
    // 只淡窗格那一片：左边的资源树不动。
    ui.swap(render, document.querySelector('.panes'));
    save();
  }

  /** 换掉**当前标签**装什么（窗格头那个列表图标用它）。 */
  function setView(view, opts, title) {
    var leaf = findLeaf(state.active) || leaves()[0];
    if (!leaf) return;
    var old = activeTab(leaf);
    var fresh = makeTab(view, opts, title);
    if (old) {
      dropTab(leaf, old);
      leaf.tabs[leaf.at] = fresh;
    } else {
      leaf.tabs = [fresh];
      leaf.at = 0;
    }
    ui.swap(render, document.querySelector('.panes'));
    save();
  }

  var PENDING_KEY = 'qf.panes.pending';

  /**
   * 把一样东西送到工作台 —— **任何页面**都能调。
   *
   *   在工作台：当场在**当前窗格**开一个标签
   *   在别处：把"要开什么"记在本机，跳到工作台，由它启动时消费掉
   *
   * 用户的心智是"我要看这个"，不该因为当前在哪一页而失效。原先这段写在资源树里，
   * 于是错题本想加一个"在工作台打开"就得再抄一份 —— 现在归引擎。
   */
  function openResource(kind, ref, title) {
    if (document.body.dataset.page === 'workbench' && hostEl) {
      open(kind, ref, title);
      return true;
    }
    try {
      localStorage.setItem(PENDING_KEY, JSON.stringify({ kind: kind, ref: ref, title: title }));
    } catch (e) { /* 无痕模式忽略：那就只是这次跳不过去 */ }
    location.href = 'workbench.html';
    return false;
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
    Object.keys(live).forEach(function (id) { dropPane(id); });
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
      title: '拖动调整比例（双击回中）',
      role: 'separator',
    });
    bar.addEventListener('pointerdown', function (ev) {
      ev.preventDefault();
      var rect = wrap.getBoundingClientRect();
      var vertical = node.dir === 'col';
      bar.classList.add('is-dragging');
      document.body.classList.add(vertical ? 'panes-resizing-v' : 'panes-resizing-h');
      try { bar.setPointerCapture(ev.pointerId); } catch (e) { /* 老浏览器忽略 */ }

      function onMove(move) {
        var next = vertical
          ? (move.clientY - rect.top) / (rect.height || 1)
          : (move.clientX - rect.left) / (rect.width || 1);
        node.ratio = Math.min(MAX_RATIO, Math.max(MIN_RATIO, next));
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
    bar.addEventListener('dblclick', function () {
      node.ratio = 0.5;
      wrap.firstChild.style.flex = '0.5 1 0px';
      wrap.lastChild.style.flex = '0.5 1 0px';
      save();
    });
    return bar;
  }

  /** 拖进来的是不是"一样资源"（资源树的叶子会带这个类型） */
  function hasResource(ev) {
    var types = (ev.dataTransfer && ev.dataTransfer.types) || [];
    var list = Array.prototype.slice.call(types);
    return list.indexOf('text/panes-resource') >= 0 || list.indexOf('text/plain') >= 0;
  }

  function readResource(ev) {
    var dt = ev.dataTransfer;
    if (!dt) return null;
    var raw = dt.getData('text/panes-resource') || dt.getData('text/plain') || '';
    if (!raw) return null;
    try {
      var got = JSON.parse(raw);
      // 只认"有 kind 的结构"：随手拖一段文字进来不该被当成资源
      return got && got.kind ? got : null;
    } catch (e) {
      return null;
    }
  }

  function paneEl(leaf) {
    var box = live[leaf.id];
    if (!box) box = live[leaf.id] = makePane(leaf);
    box.el.classList.toggle('is-active', state.active === leaf.id);
    box.el.classList.toggle('is-zoomed', state.zoomed === leaf.id);
    paintTabs(box, leaf);
    return box.el;
  }

  function makePane(leaf) {
    var tabsEl = h('div.panes__tabs', { role: 'tablist' });
    var acts = h('div.panes__acts');
    var head = h('div.panes__head', null, tabsEl, h('span.panes__spacer'), acts);
    var bodyEl = h('div.panes__body');
    var el = h('section.panes__pane', { 'data-pane': leaf.id }, head, bodyEl);

    var box = {
      el: el, bodyEl: bodyEl, tabsEl: tabsEl, leaf: leaf,
      hosts: {},          // tabKey -> 那个标签自己的容器（切换只切显示，内容不重建）
      mounted: {},        // tabKey -> { def, cleanup }
    };
    fillActs(box, acts);
    el.addEventListener('pointerdown', function () { focus(leaf.id); }, true);
    // 从资源树拖进来 = 在这一格里打开它（拖到哪格就落在哪格，不看当前活跃的是谁）
    el.addEventListener('dragover', function (ev) {
      if (!hasResource(ev)) return;
      ev.preventDefault();
      if (ev.dataTransfer) ev.dataTransfer.dropEffect = 'copy';
      el.classList.add('is-drop');
    });
    el.addEventListener('dragleave', function (ev) {
      if (ev.target === el) el.classList.remove('is-drop');
    });
    el.addEventListener('drop', function (ev) {
      el.classList.remove('is-drop');
      var payload = readResource(ev);
      if (!payload) return;
      ev.preventDefault();
      state.active = leaf.id;
      open(payload.kind, payload.ref, payload.title);
    });
    return box;
  }

  function fillActs(box, acts) {
    var leaf = box.leaf;

    var pick = h('button.panes__act', { title: '这个窗格装什么', type: 'button' });
    pick.innerHTML = ui.icon('list', 14);
    pick.addEventListener('click', function (ev) { ev.stopPropagation(); openPicker(leaf, acts); });

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

    [pick, row, col, max, shut].forEach(function (btn) { acts.appendChild(btn); });
  }

  /** 画标签栏：每个标签一个标题 + 一个关掉的叉。 */
  function paintTabs(box, leaf) {
    box.leaf = leaf;
    var strip = box.tabsEl;
    ui.clear(strip);
    (leaf.tabs || []).forEach(function (item, i) {
      var def = views[item.view];
      var title = item.title || (def ? def.title : item.view);
      var on = i === (leaf.at || 0);
      var node = h('div.panes__tab' + (on ? '.is-on' : ''), {
        role: 'tab',
        draggable: 'true',
        title: title,
        'aria-selected': on ? 'true' : 'false',
      });
      if (def && def.icon) {
        var mark = h('span.panes__tabicon');
        mark.innerHTML = ui.icon(def.icon, 13);
        node.appendChild(mark);
      }
      node.appendChild(h('span.panes__tabtitle', { text: title }));
      var x = h('button.panes__tabx', { type: 'button', title: '关掉这个标签', 'aria-label': '关掉这个标签' });
      x.innerHTML = ui.icon('close', 12);
      x.addEventListener('click', function (ev) {
        ev.stopPropagation();
        closeTab(leaf.id, item.key);
      });
      node.appendChild(x);
      node.addEventListener('click', function () { activateTab(leaf.id, item.key); });
      // 拖动标签：把这一格拖到别的格里去（tmux 的 move-pane，用标签当抓手）
      node.addEventListener('dragstart', function (ev) {
        ev.dataTransfer.setData('text/panes-tab', leaf.id + '|' + item.key);
        ev.dataTransfer.effectAllowed = 'move';
      });
      strip.appendChild(node);
    });
    // 标签末尾那个「+」：参考图里就这么一个（浏览器式标签栏的标配），
    // 点开是"这一格要装什么"的菜单。空白处双击同样能开。
    var plus = h('button.panes__newtab', { type: 'button', title: '新开一个标签', 'aria-label': '新开一个标签' });
    plus.innerHTML = ui.icon('plus', 13);
    plus.addEventListener('click', function (ev) {
      ev.stopPropagation();
      state.active = leaf.id;
      openPicker(leaf, plus);
    });
    strip.appendChild(plus);

    // 空白处双击＝再开一个标签（与"新建标签页"一个意思）
    strip.addEventListener('dblclick', function (ev) {
      if (ev.target.closest('.panes__tab')) return;
      state.active = leaf.id;
      open('blank');
    });
    syncHosts(box, leaf);
  }

  /** 内容容器：每个标签一个，切标签只切显示 —— 不重建，所以滚动位置与编辑状态都还在。 */
  function syncHosts(box, leaf) {
    var keep = {};
    (leaf.tabs || []).forEach(function (item, i) {
      keep[item.key] = true;
      var host = box.hosts[item.key];
      if (!host) {
        host = box.hosts[item.key] = h('div.panes__host');
        box.bodyEl.appendChild(host);
        mountTab(box, item, host);
      }
      var on = i === (leaf.at || 0);
      host.classList.toggle('is-on', on);
      host.hidden = !on;
    });
    Object.keys(box.hosts).forEach(function (key) {
      if (keep[key]) return;
      var host = box.hosts[key];
      unmountTab(box, key);
      if (host.parentNode) host.parentNode.removeChild(host);
      delete box.hosts[key];
    });
  }

  function mountTab(box, item, host) {
    var def = views[item.view];
    box.mounted[item.key] = { def: def || null, cleanup: null };
    host.innerHTML = '';
    if (!def) {
      host.appendChild(missing(item));
      return;
    }
    var out = def.mount(host, item.opts || {}, { leaf: box.leaf, tab: item, panes: api });
    box.mounted[item.key].cleanup = typeof out === 'function' ? out : null;
  }

  function unmountTab(box, key) {
    var run = box.mounted[key];
    if (!run) return;
    if (run.def && run.def.unmount) {
      try { run.def.unmount(); } catch (e) { /* 收摊失败不该拦住关标签 */ }
    }
    if (run.cleanup) {
      try { run.cleanup(); } catch (e) { /* 同上 */ }
    }
    delete box.mounted[key];
  }

  function dropTab(leaf, item) {
    var box = live[leaf.id];
    if (box) unmountTab(box, item.key);
  }

  function dropPane(id) {
    var box = live[id];
    if (!box) return;
    Object.keys(box.mounted).forEach(function (key) { unmountTab(box, key); });
    box.el.remove();
    delete live[id];
  }

  function missing(item) {
    return h('div.panes__missing', null,
      h('p.panes__missing-t', null, '这个标签要装「' + item.view + '」，但那个视图还没接进来'),
      h('p.panes__missing-s', null, '点窗格头上的列表图标换一个，或者把它的 mount(host, opts) 写好并 register 一下'));
  }

  function emptyHint() {
    return h('div.panes__missing', null, h('p.panes__missing-t', null, '窗格是空的'));
  }

  /** 换内容的小菜单：列出所有已注册的视图。 */
  function openPicker(leaf, anchor) {
    var old = document.querySelector('.panes__menu');
    if (old) old.remove();
    var menu = h('div.panes__menu', { role: 'menu' });
    Object.keys(views).forEach(function (type) {
      var def = views[type];
      var item = h('button.panes__menu-item' + (activeTab(leaf) && activeTab(leaf).view === type ? '.is-on' : ''),
        { type: 'button' });
      item.innerHTML = ui.icon(def.icon || 'list', 14);
      item.appendChild(h('span', null, def.title));
      item.addEventListener('click', function (ev) {
        ev.stopPropagation();
        menu.remove();
        state.active = leaf.id;
        open(type);
      });
      menu.appendChild(item);
    });
    if (!Object.keys(views).length) {
      menu.appendChild(h('p.panes__menu-empty', null, '还没有注册任何视图'));
    }
    document.body.appendChild(menu);
    var area = anchor.getBoundingClientRect();
    menu.style.top = Math.round(area.bottom + 6) + 'px';
    menu.style.left = Math.round(Math.min(area.left, window.innerWidth - menu.offsetWidth - 12)) + 'px';

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
    else if (key === 'w' || key === 'W') {
      ev.preventDefault();
      // 有标签先关标签，只剩一个标签时才关窗格 —— 与编辑器里的习惯一致
      var leaf = findLeaf(state.active);
      var item = activeTab(leaf);
      if (leaf && leaf.tabs && leaf.tabs.length > 1 && item) closeTab(leaf.id, item.key);
      else close();
    }
  }

  /* ------------------------------------------------------------------ 持久化与状态栏 */

  function layout() {
    return { root: state.root, active: state.active, zoomed: state.zoomed };
  }

  function save() {
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
          state.root = normalize(got.root);
          state.zoomed = findLeaf(got.zoomed) ? got.zoomed : null;
          state.active = findLeaf(got.active) ? got.active : leaves()[0].id;
          return;
        }
      } catch (e) { /* 坏掉的布局不如不要，退回默认 */ }
    }
    state.root = normalize(defaultLayout) || stage('blank');
    state.active = leaves()[0].id;
    state.zoomed = null;
  }

  function paintStatus() {
    var all = leaves();
    var leaf = findLeaf(state.active);
    var item = activeTab(leaf);
    var def = item ? views[item.view] : null;
    var name = item ? (item.title || (def ? def.title : item.view)) : '空';
    var order = all
      .map(function (one, i) { return one.id === state.active ? '[' + (i + 1) + ']' : String(i + 1); })
      .join(' ');
    ui.statusbar.set([
      { kind: 'stat', label: '窗格', value: name + ' ' + order + (state.zoomed ? ' · 最大化中' : '') },
      { spacer: true },
      { kind: 'hint', key: 'Alt+\\', label: '左右拆' },
      { kind: 'hint', key: 'Alt+-', label: '上下拆' },
      { kind: 'hint', key: 'Alt+Z', label: '最大化' },
      { kind: 'hint', key: 'Alt+W', label: '关标签' },
    ]);
  }

  /* ------------------------------------------------------------------ 出厂 */

  function mount(host, options) {
    hostEl = host;
    host.classList.add('panes');
    if (options && options.layout) {
      state.root = normalize(options.layout);
      state.active = leaves()[0].id;
    } else {
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

  var api = {
    mount: mount,
    register: register,
    state: state,
    layout: layout,
    split: split,
    close: close,
    zoom: zoom,
    focus: focus,
    open: open,
    openTab: open,
    openResource: openResource,
    closeTab: closeTab,
    activateTab: activateTab,
    setView: setView,
    reset: reset,
    views: views,
    save: save,
  };

  QF.panes = api;
})();
