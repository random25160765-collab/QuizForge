/* sidetree.js —— 资源树：一棵大树，三个根（对话 / 资料 / 笔记），加一个笔记图谱位。
 *
 * 用户的原话是"所有的资源全部在左边被收起来，在右边的画布随意组合"，所以这一块的
 * 职责只有两件：**列出来**，以及**把东西送进右边**。送的方式两种 ——
 *   点一下   → 在工作台的当前窗格里开一个标签
 *   拖过去   → 落到拖到的那一格（'text/panes-resource' 由 panes.js 接）
 * 在别的页面（练习中心、错题本……）点树上的东西时，先把"要开什么"记在本机，
 * 再跳去工作台 —— 用户的心智是"我要看这个"，不该因为当前在哪一页而失效。
 *
 * 懒加载：笔记库有 1113 篇，展开哪个库才读哪个库的树。
 */
(function () {
  var ui = QF.ui;
  var h = ui.h;
  var api = QF.api;

  var OPEN_KEY = 'qf.sidetree.v1';       // 展开状态（"要开什么"那件事归引擎管，见 openResource）
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

  /** 打开一样资源 —— 这件事归引擎（别的页面也要用，比如错题本的"在工作台打开"）。 */
  function send(kind, ref, title) {
    QF.panes.openResource(kind, ref, title);
  }

  /* ------------------------------------------------------------------ 树零件 */

  function caret(open) {
    var node = h('span.stree__caret');
    node.innerHTML = ui.icon('chevronR', 12);
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
    } else if (opts.iconSlot) {
      // 只留位置不画东西：目录行不配图标（一级就有十几个，同款图标重复十几遍是噪音，
      // 参考图那棵树也是不给目录配图标），但名字要和带图标的兄弟行对齐
      node.appendChild(h('span.stree__icon.stree__icon--slot'));
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
      iconSlot: opts.iconSlot,
      count: opts.count,
      caret: true,
      open: open,
      cls: 'stree__row--head',
      title: opts.title,
    });
    var body = h('div.stree__body');
    box.appendChild(head);
    box.appendChild(body);
    if (opts.actions) {
      // 「＋」平时不出现，悬停才亮 —— 目录行上常驻一个按钮会让整棵树变吵
      var more = h('button.stree__more', {
        type: 'button',
        title: '新建…',
        'aria-label': '新建',
        html: ui.icon('plus', 13),
      });
      more.addEventListener('click', function (ev) {
        ev.stopPropagation();
        openMenu(more, opts.actions());
      });
      head.appendChild(more);
    }
    if (opts.drop) {
      bindDrop(head, opts.drop);
      head.addEventListener('contextmenu', function (ev) {
        if (!opts.actions) return;
        ev.preventDefault();
        openMenu(head, opts.actions());
      });
    }

    function paint() {
      var isOpen = !!readOpen()[opts.key];
      var mark = head.querySelector('.stree__caret');
      if (mark) mark.style.transform = isOpen ? 'rotate(90deg)' : '';
      body.hidden = !isOpen;
      if (isOpen && opts.load && !body.dataset.loaded) {
        body.dataset.loaded = '1';
        var hint = h('p.stree__hint', { text: '正在读…' });
        body.appendChild(hint);
        // 读完（或读完失败）就摘掉。踩过：它只挂不摘，于是每个展开过的组下面都
        // 永久挂着一行孤零零的"正在读…"，列表越长越扎眼。
        // 同步的 load 也走这里 —— 它们同帧就摘，不会闪。
        var drop = function () { if (hint.parentNode) hint.parentNode.removeChild(hint); };
        var done = opts.load(body);
        if (done && done.then) done.then(drop, drop);
        else drop();
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
    if (opts.kind || opts.move) {
      node.addEventListener('dragstart', function (ev) {
        // 两套载荷都带上：拖到右边窗格 = "打开它"，拖到另一个文件夹 = "搬过去"
        if (opts.kind) {
          var payload = JSON.stringify({ kind: opts.kind, ref: opts.ref, title: opts.label });
          ev.dataTransfer.setData('text/panes-resource', payload);
          // 有些浏览器只认 text/plain，多写一份不亏
          ev.dataTransfer.setData('text/plain', payload);
        }
        if (opts.move) ev.dataTransfer.setData(MOVE_MIME, JSON.stringify(opts.move));
        ev.dataTransfer.effectAllowed = 'copyMove';
      });
    }
    return node;
  }

  function err(box, message) {
    box.appendChild(h('p.stree__hint', { text: message }));
  }

  /* ------------------------------------------------- 树上动一下 = 磁盘上动一下 */

  /* 这一栏画的是**用户的真实文件夹**，所以这里的操作没有中间层：
   *   拖一个条目到某个文件夹 → 真的移动（笔记走 `/notes/move`，资料走 `/library/move`）
   *   在文件夹上新建          → 真的 mkdir（笔记 `/notes/folder`，资料 `/library/mkdir`）
   * 做完**重画整棵树**：磁盘才是权威，不在这里维护一份影子状态（维护一份迟早对不上）。 */

  var MOVE_MIME = 'text/qf-move';

  function toastErr(prefix, err) {
    ui.toast(prefix + '：' + ((err && err.message) || err), 'error');
  }

  function doMove(from, to) {
    if (!from || !to || from.kind !== to.kind) {
      ui.toast('只能在同一类里挪：笔记归笔记、资料归资料', 'warn');
      return;
    }
    if (from.dir === to.dir) return;        // 已经在这个文件夹里了
    var call = from.kind === 'note'
      ? api.post('/notes/move', { lib: from.lib, path: from.path, folder: to.dir })
      : api.post('/library/move', { path: from.path, to: to.dir });
    call.then(function () {
      ui.toast('挪好了：' + (from.title || ''));
      paint();
    }).catch(function (err) { toastErr('没挪成', err); });
  }

  /** 起个名字（新建文件夹 / 新建笔记都是先问名字，再落盘） */
  function askName(title, placeholder) {
    return new Promise(function (resolve) {
      var input = h('input.input', { type: 'text', placeholder: placeholder || '' });
      var done = false;
      function finish(value) {
        if (done) return;
        done = true;
        resolve(value);
      }
      var box = ui.modal({
        title: title,
        body: h('div.form__field', null, input),
        actions: [
          { label: '取消', kind: 'ghost', onClick: function () { finish(''); } },
          { label: '建', kind: 'primary', onClick: function () { finish(input.value.trim()); } },
        ],
      });
      input.addEventListener('keydown', function (ev) {
        if (ev.key !== 'Enter') return;
        finish(input.value.trim());
        box.close();
      });
      setTimeout(function () { input.focus(); }, 30);
    });
  }

  function doCreate(where, kind) {
    askName(kind === 'folder' ? '新建文件夹' : '新建笔记', '名字').then(function (name) {
      if (!name) return;
      var call = where.kind === 'note'
        ? (kind === 'folder'
          ? api.post('/notes/folder', { lib: where.lib, folder: joinRel(where.dir, name) })
          : api.post('/notes/create', { lib: where.lib, folder: where.dir || '', title: name, kind: 'note' }))
        : api.post('/library/mkdir', { dir: joinPath(where.dir, name) });
      call.then(function () {
        ui.toast('建好了：' + name);
        paint();
      }).catch(function (err) { toastErr('没建成', err); });
    });
  }

  /** 文件夹行上那个「＋」的菜单。**不追求齐全**，就是手边这几个。 */
  function folderActions(where) {
    var items = [{ label: '新建文件夹', run: function () { doCreate(where, 'folder'); } }];
    if (where.kind === 'note') {
      items.push({ label: '新建笔记', run: function () { doCreate(where, 'note'); } });
    }
    return items;
  }

  /* 小菜单：点空白或按 Esc 关掉。样式见 pane.css 的 `.stree__menu`。 */
  var menuEl = null;
  function closeMenu() {
    if (!menuEl) return;
    menuEl.remove();
    menuEl = null;
    document.removeEventListener('mousedown', onMenuAway, true);
    document.removeEventListener('keydown', onMenuKey, true);
  }
  function onMenuAway(ev) { if (menuEl && !menuEl.contains(ev.target)) closeMenu(); }
  function onMenuKey(ev) { if (ev.key === 'Escape') closeMenu(); }
  function openMenu(anchor, items) {
    closeMenu();
    var box = h('div.stree__menu');
    items.forEach(function (item) {
      box.appendChild(h('button.stree__menu-item', {
        type: 'button',
        text: item.label,
        onClick: function () { closeMenu(); item.run(); },
      }));
    });
    document.body.appendChild(box);
    var r = anchor.getBoundingClientRect();
    box.style.left = Math.round(r.left) + 'px';
    box.style.top = Math.round(r.bottom + 2) + 'px';
    menuEl = box;
    setTimeout(function () {
      document.addEventListener('mousedown', onMenuAway, true);
      document.addEventListener('keydown', onMenuKey, true);
    }, 0);
  }

  function joinRel(dir, name) {
    return (dir ? dir.replace(/\/+$/, '') + '/' : '') + name;
  }

  function joinPath(dir, name) {
    return (dir || '').replace(/[\\/]+$/, '') + '/' + name;
  }

  /** 让一个目录行接受拖放：落上来就是"挪进这个目录"。 */
  function bindDrop(node, target) {
    node.addEventListener('dragover', function (ev) {
      var types = ev.dataTransfer ? ev.dataTransfer.types : null;
      if (!types || Array.prototype.indexOf.call(types, MOVE_MIME) < 0) return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = 'move';
      node.classList.add('is-drop');
    });
    node.addEventListener('dragleave', function () { node.classList.remove('is-drop'); });
    node.addEventListener('drop', function (ev) {
      node.classList.remove('is-drop');
      var raw = ev.dataTransfer && ev.dataTransfer.getData(MOVE_MIME);
      if (!raw) return;      // 不是"树内搬家"（可能是别处拖来的东西），不接
      ev.preventDefault();
      ev.stopPropagation();
      var from = null;
      try { from = JSON.parse(raw); } catch (e2) { from = null; }
      // target 可以是函数：多根时"资料"那一行该落到哪个根，得等数据回来才知道
      doMove(from, typeof target === 'function' ? target() : target);
    });
  }

  /* ------------------------------------------------------------------ 三个根 */

  function paintConversations(box) {
    return api.get('/chat/conversations').then(function (res) {
      var list = (res && res.conversations) || [];
      // 「新对话」放在最前：会话列表挪进左栏之后，这儿就成了唯一的入口
      var fresh = leaf(2, { label: '新对话', icon: 'plus', title: '开一条新对话' });
      fresh.addEventListener('click', function () {
        api.post('/chat/conversations', {}).then(function (out) {
          var id = out && out.conversation && out.conversation.id;
          location.href = id ? 'chat.html?c=' + encodeURIComponent(id) : 'chat.html';
        }).catch(function (err2) {
          ui.toast('开不了新对话：' + ((err2 && err2.message) || err2), 'error');
        });
      }, true);
      box.appendChild(fresh);
      list.forEach(function (one) {
        box.appendChild(leaf(2, {
          label: one.title || '未命名对话',
          icon: 'robot',
          title: (one.preview || '') + (one.count ? '\n' + one.count + ' 条消息' : ''),
          // 带 id 过去：对话页认这个参数，落到那一条上
          href: 'chat.html?c=' + encodeURIComponent(one.id),
        }));
      });
    }).catch(function () { err(box, '读不到对话列表'); });
  }

  /* 条目图标按类型分 —— 一列全是一样的书，等于没有图标 */
  var KIND_ICON = {
    paper: 'print',
    book: 'book',
    manual: 'list',
    spec: 'cpu',
    report: 'flag',
    slides: 'play',
    code: 'grip',
    note: 'bulb',
    webpage: 'info',
    blog: 'info',
  };

  /** 资料按**目录**分层。
   *  条目自己带着相对路径（`cuda/layout_algebra.pdf`），拿它当文件夹用即可 ——
   *  原先 70 多条平铺成一坨，只能靠滚，和「笔记」那边的形状也不一致。 */
  function paintItems(box) {
    return Promise.all([
      api.get('/library/items?limit=600'),
      // 根要从服务端拿：目录的**绝对路径**得拼出来（落盘操作用的是真实路径）
      api.get('/library/roots'),
    ]).then(function (both) {
      var list = (both[0] && both[0].items) || [];
      var roots = (both[1] && both[1].roots) || [];
      if (!list.length) { err(box, '资料目录里还没扫出条目'); return; }
      // 条目带着绝对路径：按它归到各自的根下（多根时先分一层，免得两个根的同名目录混在一起）
      var buckets = roots.map(function (root) {
        return { root: root, name: baseName(root), tree: { dirs: {}, files: [] } };
      });
      list.forEach(function (one) {
        var abs = String(one.path || '');
        var hit = null;
        buckets.forEach(function (b) { if (!hit && underRoot(abs, b.root)) hit = b; });
        if (!hit) return;     // 不在任何根里（根被挪走了？）—— 不画，也不猜
        var rel = abs.slice(String(hit.root).replace(/[\\/]+$/, '').length).replace(/^[\\/]+/, '');
        var parts = rel.split(/[\\/]/).filter(Boolean);
        parts.pop();          // 最后一段是文件名，不进目录树
        var node = hit.tree;
        parts.forEach(function (seg) {
          node.dirs[seg] = node.dirs[seg] || { dirs: {}, files: [] };
          node = node.dirs[seg];
        });
        node.files.push(one);
      });
      // 只有一个根时不留"根"这一层（现在的样子），多根才分
      state.libRoot = buckets.length === 1 ? buckets[0].root : '';
      buckets.forEach(function (b) {
        if (buckets.length === 1) {
          paintItemNode(box, b.tree, 2, b.root);
          return;
        }
        box.appendChild(group(2, {
          key: 'libroot:' + b.root,
          label: b.name,
          iconSlot: true,
          count: countItems(b.tree),
          drop: { kind: 'doc', dir: b.root },
          actions: function () { return folderActions({ kind: 'doc', dir: b.root }); },
          load: function (body) { paintItemNode(body, b.tree, 3, b.root); },
        }));
      });
    }).catch(function () { err(box, '读不到资料条目'); });
  }

  /** 目录一层：绝对路径一路带下去，落盘时才有的可写 */
  function paintItemNode(box, node, level, absDir) {
    Object.keys(node.dirs).sort().forEach(function (name) {
      var child = node.dirs[name];
      var dir = joinPath(absDir, name);
      box.appendChild(group(level, {
        key: 'libdir:' + dir,
        label: name,
        iconSlot: true,
        count: countItems(child),
        drop: { kind: 'doc', dir: dir },
        actions: function () { return folderActions({ kind: 'doc', dir: dir }); },
        load: function (body) { paintItemNode(body, child, level + 1, dir); },
      }));
    });
    node.files.forEach(function (one) {
      var title = one.title || one.citekey;
      box.appendChild(leaf(level, {
        label: title,
        icon: KIND_ICON[one.kind] || 'book',
        count: one.year || null,
        // 悬停看得见出处：类型 · 引用键 · 相对路径
        title: [one.kind, one.citekey, one.rel].filter(Boolean).join('\n'),
        kind: 'doc',
        ref: { citekey: one.citekey },
        move: { kind: 'doc', path: String(one.path || ''), dir: absDir, title: title },
      }));
    });
  }

  /** 路径的最后一段（根的名字） */
  function baseName(path) {
    var parts = String(path || '').split(/[\\/]/).filter(Boolean);
    return parts.length ? parts[parts.length - 1] : String(path || '');
  }

  /** 这个绝对路径是不是在那个根下面（去掉尾斜杠再比前缀，别让 `a/b` 骗过 `a/bc`） */
  function underRoot(abs, root) {
    var base = String(root || '').replace(/[\\/]+$/, '');
    return abs === base || abs.indexOf(base + '/') === 0 || abs.indexOf(base + '\\') === 0;
  }

  /** 这个目录下一共有多少条目（目录行右侧那个数字） */
  function countItems(node) {
    var n = node.files.length;
    Object.keys(node.dirs).forEach(function (name) { n += countItems(node.dirs[name]); });
    return n;
  }

  function paintVault(box, lib) {
    return api.get('/notes/tree?lib=' + encodeURIComponent(lib.name)).then(function (tree) {
      var root = h('div');
      box.appendChild(root);
      paintNode(root, tree, 2, lib.name, '');
      if (!root.childNodes.length) err(box, '这个库里没有笔记');
    }).catch(function () { err(box, '读不到这个库'); });
  }

  /** 目录一层。
   *
   *  `dirRel` 是这一层的**库内相对路径** —— 落盘操作要的是它（'' 就是库根）。
   *  注意 `/notes/tree` 的 `dirs` 是**列表**（不是字典，见 notelib.tree 的 finalize），
   *  每个节点自己带着 `name` / `path` / `count`：原先按字典用 `Object.keys` 取到的是
   *  下标，展开笔记库会看到一排 `0 1 2` 的目录名。
   *  节点上的 `path` 是库名打头的（`Math/子目录`），落盘时**不用**它 —— 自己顺着父链拼。
   */
  function paintNode(box, node, level, lib, dirRel) {
    (node.dirs || []).forEach(function (child) {
      var name = child.name;
      var rel = joinRel(dirRel, name);
      var sub = group(level, {
        key: 'dir:' + lib + '/' + rel,
        label: name,
        // `count` 是含子目录的总数（后端算好的），比自己数一遍可靠
        count: child.count || null,
        iconSlot: true,
        drop: { kind: 'note', lib: lib, dir: rel },
        actions: function () { return folderActions({ kind: 'note', lib: lib, dir: rel }); },
        load: function (body) { paintNode(body, child, level + 1, lib, rel); },
      });
      box.appendChild(sub);
    });
    (node.files || []).forEach(function (file) {
      var title = file.name.replace(/\.md$/, '');
      if (file.kind === 'canvas') {
        box.appendChild(leaf(level, {
          label: file.name,
          icon: 'grip',
          title: '画布 · ' + file.path,
          kind: 'canvas',
          ref: { lib: lib, path: file.path },
          // 画布也是文件：拖到别的文件夹就是把它搬过去
          move: { kind: 'note', lib: lib, path: file.path, dir: dirRel, title: file.name },
        }));
        return;
      }
      if (file.kind !== 'note') return;    // 附件不进树（它们跟着笔记走）
      box.appendChild(leaf(level, {
        label: title,
        icon: 'book',
        title: file.path,
        kind: 'note',
        ref: { lib: lib, path: file.path },
        move: { kind: 'note', lib: lib, path: file.path, dir: dirRel, title: title },
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
          iconSlot: true,
          // 库这一行本身就是**库根目录**：能接拖放（搬回来）、能在根上新建
          drop: { kind: 'note', lib: lib.name, dir: '' },
          actions: function () { return folderActions({ kind: 'note', lib: lib.name, dir: '' }); },
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
      // 只有一个根时，这一行就是个目录（根本身）：能接拖放、能在根上建
      // 多根时 state.libRoot 是空串，落到这一行就自然不接（两个根的同名目录混不起）
      drop: function () { return state.libRoot ? { kind: 'doc', dir: state.libRoot } : null; },
      actions: function () {
        return state.libRoot ? folderActions({ kind: 'doc', dir: state.libRoot }) : [];
      },
      load: paintItems,
    }));
    box.appendChild(group(0, {
      key: 'root:notes',
      label: '笔记',
      icon: 'list',
      load: paintVaults,
    }));
    // 笔记图谱：一间库一张图（笔记为节点、双链为边）。
    // 入口留在左栏（用户的原话"文档站的知识图谱就放左栏"），图本身开在右边窗格里 ——
    // 左栏只有 240px 宽，真画起来没法看。
    box.appendChild(group(0, {
      key: 'root:notegraph',
      label: '笔记图谱',
      icon: 'target',
      load: function (body) {
        return api.get('/notes/stats').then(function (data) {
          var libs = (data && data.libraries) || [];
          if (!libs.length) { err(body, '还没有笔记库'); return; }
          libs.forEach(function (lib) {
            body.appendChild(leaf(2, {
              label: lib.name + ' 的双链',
              icon: 'target',
              count: lib.notes || lib.count || null,
              title: '把这一库的笔记双链画成图（开在右边窗格里）',
              kind: 'notegraph',
              ref: { lib: lib.name },
            }));
          });
        }).catch(function () { err(body, '读不到笔记库'); });
      },
    }));
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
