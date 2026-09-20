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

  /** 一枚图标：宽度与箭头槽相同（14px），落在**箭头那一列**上。 */
  function mark(name) {
    var node = h('span.stree__icon');
    node.innerHTML = ui.icon(name, 13);
    return node;
  }

  function row(level, opts) {
    var node = h('div.stree__row' + (opts.cls ? '.' + opts.cls : ''), {
      title: opts.title || opts.label,
      draggable: opts.drag ? 'true' : null,
      'data-level': String(level),
    });
    node.style.paddingLeft = (8 + level * 12) + 'px';
    // 箭头列与图标列**是同一列**（都 14px，都居中）。
    // 踩过：原先目录行画箭头、条目行先空一个箭头位再画图标，同一层里
    // "目录的箭头"与"条目的图标"差 20px —— 树干上的点连不成一条竖线
    // （用户原话：文件树的图标应该和左侧的第一个字符/图标对齐）。
    // 规则：有箭头就画箭头；条目把图标画在箭头的位置上；两者都没有才留空位。
    if (opts.caret) node.appendChild(caret(!!opts.open));
    if (opts.icon) node.appendChild(mark(opts.icon));
    else if (!opts.caret) node.appendChild(h('span.stree__caret.stree__caret--none'));
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
    if (opts.actions) {
      // 「＋」平时不出现，悬停才亮 —— 目录行上常驻一个按钮会让整棵树变吵
      var more = h('button.stree__more' + (opts.plusAlways ? '.stree__more--on' : ''), {
        type: 'button',
        title: opts.plusTitle || '新建…',
        'aria-label': opts.plusTitle || '新建',
        html: ui.icon('plus', 13),
      });
      more.addEventListener('click', function (ev) {
        ev.stopPropagation();
        // `plusRun` 给了就直接干：常用的那件事不该藏进一层菜单里
        // （用户："'新对话'那个做成图标放在右上角的框里" —— 点一下就该开一条）
        if (opts.plusRun) { opts.plusRun(); return; }
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
      // 能搬的东西就要能拖：会话没有 `kind`（点击是跳页，不是送进窗格），
      // 但它有 `move` —— 只认 kind 的话，对话行根本拖不动，分组就成了摆设。
      drag: !!(opts.kind || opts.move),
    });
    node.classList.add('stree__row--leaf');
    if (opts.dim) node.classList.add('is-dim');
    if (opts.menu) {
      node.addEventListener('contextmenu', function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        openMenu(node, opts.menu());
      });
    }
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
      ui.toast('只能在同一类里挪：笔记归笔记、资料归资料、对话归对话', 'warn');
      return;
    }
    if (from.dir === to.dir) return;        // 已经在这个文件夹里了
    var call = from.kind === 'chat'
      ? api.patch('/chat/conversations/' + from.id, { folder: to.dir })
      : from.kind === 'note'
        ? api.post('/notes/move', { lib: from.lib, path: from.path, folder: to.dir })
        : api.post('/library/move', { path: from.path, to: to.dir });
    call.then(function () {
      ui.toast('挪好了：' + (from.title || ''));
      paint();
    }).catch(function (err) { toastErr('没挪成', err); });
  }

  /** 起个名字（新建文件夹 / 新建笔记都是先问名字，再落盘） */
  function askName(title, placeholder, value, okLabel) {
    return new Promise(function (resolve) {
      var input = h('input.input', { type: 'text', placeholder: placeholder || '', value: value || '' });
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
          { label: okLabel || '建', kind: 'primary', onClick: function () { finish(input.value.trim()); } },
        ],
      });
      input.addEventListener('keydown', function (ev) {
        if (ev.key !== 'Enter') return;
        finish(input.value.trim());
        box.close();
      });
      setTimeout(function () {
        input.focus();
        // 预填时全选：改名多半是"只改一小段"，不该逼人从头敲
        if (value) input.select();
      }, 30);
    });
  }

  function doCreate(where, kind) {
    askName(kind === 'folder' ? '新建文件夹' : '新建笔记', '名字').then(function (name) {
      if (!name) return;
      // 对话的分组只活在库里（不是磁盘目录），所以它走 chat 那套接口
      if (where.kind === 'chat') {
        api.post('/chat/folders', { path: joinRel(where.dir, name) }).then(function () {
          ui.toast('建好了：' + name);
          paint();
        }).catch(function (err) { toastErr('没建成', err); });
        return;
      }
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

  /** 上层把文件拖进来时的落点。`where` 来自 drop 目标（见 bindDrop），
   *  里面带着"往哪个目录写"的绝对路径/库内相对路径。 */
  function uploadInto(where, files) {
    if (!where) {
      ui.toast('先落在一个目录上（把文件拖到某个文件夹那一行）', 'warn');
      return;
    }
    var list = Array.prototype.slice.call(files || []);
    if (!list.length) return;
    var where2 = where.dir === '' ? '根目录' : (where.label || '这个目录');
    ui.toast('正在放进「' + where2 + '」：' + list.length + ' 个文件…');
    var done = 0;
    var chain = Promise.resolve();
    list.forEach(function (one) {
      chain = chain.then(function () {
        var path = where.kind === 'note' ? '/notes/upload' : '/library/upload';
        var fields = where.kind === 'note'
          ? { lib_name: where.lib, folder: where.dir || '' }
          : { dir: where.dir };
        return api.upload(path, one, fields).then(function () { done += 1; });
      });
    });
    chain
      .then(function () {
        // 资料那侧放完就能在树里看到（它是扫出来的）；笔记库里附件不进树
        ui.toast(
          where.kind === 'note'
            ? '放进去了 ' + done + ' 个文件（在「' + where2 + '」的目录里，附件不进树）'
            : '放进去了 ' + done + ' 个文件'
        );
        paint();
      })
      .catch(function (err) { toastErr('没放进去', err); paint(); });
  }

  /** 让一个目录行接受拖放：树内的条目落上来是"挪进这个目录"，
   *  **从系统拖来的文件**落上来是"放进这个目录"（真的写进那个文件夹）。 */
  function bindDrop(node, target) {
    function targetNow() { return typeof target === 'function' ? target() : target; }
    node.addEventListener('dragover', function (ev) {
      var dt = ev.dataTransfer;
      var types = dt ? dt.types : null;
      if (!types) return;
      var hasFile = Array.prototype.indexOf.call(types, 'Files') >= 0;
      var hasMove = Array.prototype.indexOf.call(types, MOVE_MIME) >= 0;
      if (!hasFile && !hasMove) return;
      ev.preventDefault();
      dt.dropEffect = hasFile ? 'copy' : 'move';
      node.classList.add('is-drop');
    });
    node.addEventListener('dragleave', function () { node.classList.remove('is-drop'); });
    node.addEventListener('drop', function (ev) {
      node.classList.remove('is-drop');
      var dt = ev.dataTransfer;
      if (!dt) return;
      if (dt.files && dt.files.length) {
        ev.preventDefault();
        ev.stopPropagation();
        uploadInto(targetNow(), dt.files);
        return;
      }
      var raw = dt.getData(MOVE_MIME);
      if (!raw) return;      // 不是"树内搬家"（可能是别处拖来的东西），不接
      ev.preventDefault();
      ev.stopPropagation();
      var from = null;
      try { from = JSON.parse(raw); } catch (e2) { from = null; }
      // target 可以是函数：多根时"资料"那一行该落到哪个根，得等数据回来才知道
      doMove(from, targetNow());
    });
  }

  /* ------------------------------------------------------------------ 三个根 */

  /* 一级内容从第 1 层起（根是第 0 层）。
   * 原先对话与资料都从**第 2 层**起 —— 一进根就缩进 24px，比笔记那边深一格，
   * 用户的原话是"下面的对话往左收一点"。三个根现在同一套缩进。 */
  var CHILD_LEVEL = 1;

  /** 开一条新对话。组头那个「＋」和菜单共用它（在分组上开就落进那个分组）。 */
  function newConversation(dir) {
    api.post('/chat/conversations', { folder: dir || '' }).then(function (out) {
      var id = out && out.conversation && out.conversation.id;
      // 一律在窗格里开（这一页是主页，跳走就丢了当前的窗格布局）
      send('chat', id ? { id: id } : { fresh: true }, '对话');
    }).catch(function (err2) {
      ui.toast('开不了新对话：' + ((err2 && err2.message) || err2), 'error');
    });
  }

  function createChatFolder(parent) {
    askName('新建分组', '可以用 / 分层，例如 考研/数学').then(function (name) {
      if (!name) return;
      doCreate({ kind: 'chat', dir: parent || '' }, 'folder');
    });
  }

  function renameChatFolder(path) {
    askName('分组改名 / 搬家', '新名字（含 / 就是换一层）', path, '改').then(function (name) {
      if (!name || name === path) return;
      api.patch('/chat/folders', { path: path, to: name }).then(function () {
        ui.toast('改好了：' + name);
        paint();
      }).catch(function (err) { toastErr('没改成', err); });
    });
  }

  function deleteChatFolder(path) {
    // 后端在里面还有东西时会拒绝 —— 所以这里不需要再吓唬用户一次
    api.del('/chat/folders?path=' + encodeURIComponent(path)).then(function () {
      ui.toast('删掉了：' + path);
      paint();
    }).catch(function (err) { toastErr('没删成', err); });
  }

  /** 分组行的右键菜单：手边这几件 */
  function chatFolderActions(path) {
    return [
      { label: '新建对话', run: function () { newConversation(path); } },
      { label: '新建子分组', run: function () { createChatFolder(path); } },
      { label: '改名 / 搬家', run: function () { renameChatFolder(path); } },
      { label: '删掉这个分组', run: function () { deleteChatFolder(path); } },
    ];
  }

  /** 会话行的右键菜单 */
  function chatActions(one) {
    return [
      {
        label: one.pinned ? '取消置顶' : '置顶',
        run: function () {
          api.patch('/chat/conversations/' + one.id, { pinned: !one.pinned }).then(function () {
            paint();
          }).catch(function (err) { toastErr('没改成功', err); });
        },
      },
      {
        label: '改个名字',
        run: function () {
          askName('改个名字', '对话标题', one.title || '', '改').then(function (name) {
            if (!name || name === one.title) return;
            api.patch('/chat/conversations/' + one.id, { title: name }).then(function () {
              paint();
            }).catch(function (err) { toastErr('没改成', err); });
          });
        },
      },
      {
        label: '移到根目录',
        run: function () {
          if (!one.folder) return;
          api.patch('/chat/conversations/' + one.id, { folder: '' }).then(function () {
            ui.toast('挪到最外面了');
            paint();
          }).catch(function (err) { toastErr('没挪成', err); });
        },
      },
    ];
  }

  function paintConversations(box) {
    return api.get('/chat/conversations').then(function (res) {
      var list = (res && res.conversations) || [];
      var paths = (res && res.folders) || [];
      // 目录树：后端给的分组（含空的）+ 每条会话自己的 folder
      var root = { dirs: {}, files: [] };
      function nodeAt(path) {
        var cur = root;
        String(path || '').split('/').filter(Boolean).forEach(function (seg) {
          cur.dirs[seg] = cur.dirs[seg] || { dirs: {}, files: [] };
          cur = cur.dirs[seg];
        });
        return cur;
      }
      paths.forEach(function (one) { nodeAt(one); });
      list.forEach(function (one) { nodeAt(one.folder || '').files.push(one); });

      if (!list.length && !paths.length) {
        err(box, '还没有对话。右上角那个「＋」开一条。');
        return;
      }
      paintChatNode(box, root, CHILD_LEVEL, '');
    }).catch(function () { err(box, '读不到对话列表'); });
  }

  /** 对话这一支的一层：先分组、后会话（与资料那边同一个形状）。 */
  function paintChatNode(box, node, level, prefix) {
    Object.keys(node.dirs).sort().forEach(function (name) {
      var child = node.dirs[name];
      var path = prefix ? prefix + '/' + name : name;
      box.appendChild(group(level, {
        key: 'chatdir:' + path,
        label: name,
        count: countItems(child),
        // 拖到分组上 = 把这条对话挪进去（底层就是改 conversations.folder）
        drop: { kind: 'chat', dir: path, label: name },
        actions: function () { return chatFolderActions(path); },
        load: function (body) { paintChatNode(body, child, level + 1, path); },
      }));
    });
    node.files.forEach(function (one) {
      box.appendChild(leaf(level, {
        label: one.title || '未命名对话',
        // 置顶的换一枚星：一排同样的机器人头看不出哪条是我钉住的
        icon: one.pinned ? 'star' : 'robot',
        title: (one.preview || '') + (one.count ? '\n' + one.count + ' 条消息' : ''),
        // 带 id 过去：对话页认这个参数，落到那一条上
        // **不跳页**：把会话当成一种资源送进右边的窗格（与资料、文档同一条规矩）。
        // 用户："我在资源页面点击对话的时候，就应该直接把对话给我看。"
        kind: 'chat',
        ref: { id: one.id },
        move: { kind: 'chat', id: one.id, dir: one.folder || '', title: one.title || '' },
        menu: function () { return chatActions(one); },
      }));
    });
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
        // 层级与对话、笔记两边对齐：根是第 0 层，根里的第一层内容是 CHILD_LEVEL。
        // （原先这里写死 2，一进根就缩 24px —— 用户说"往左收一点"。）
        if (buckets.length === 1) {
          paintItemNode(box, b.tree, CHILD_LEVEL, b.root);
          return;
        }
        box.appendChild(group(CHILD_LEVEL, {
          key: 'libroot:' + b.root,
          label: b.name,
          count: countItems(b.tree),
          drop: { kind: 'doc', dir: b.root, label: b.name },
          actions: function () { return folderActions({ kind: 'doc', dir: b.root }); },
          load: function (body) { paintItemNode(body, b.tree, CHILD_LEVEL + 1, b.root); },
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
        count: countItems(child),
        drop: { kind: 'doc', dir: dir, label: name },
        actions: function () { return folderActions({ kind: 'doc', dir: dir }); },
        load: function (body) { paintItemNode(body, child, level + 1, dir); },
      }));
    });
    node.files.forEach(function (one) {
      var title = one.title || one.citekey;
      box.appendChild(leaf(level, {
        label: title,
        // 统一**不配图标、不带年份**：同一层里一人一个图标 + 有的有年份有的没有，
        // 整列全是碎块（用户："还是很辣眼睛"）。类型与年份在悬停提示里，一个不少。
        // 缩进与目录行的箭头对齐 —— 目录靠箭头、文件靠缩进，节奏就出来了。
        title: [one.kind, one.year || '', one.citekey, one.rel].filter(Boolean).join('\n'),
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
        drop: { kind: 'note', lib: lib, dir: rel, label: name },
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
        // 同上：普通笔记不配图标（一列几十个同样的书本图标＝噪音）；
        // 画布保留它那个 `grip` —— 它确实是另一种东西，值得一眼看出来
        title: file.path,
        kind: 'note',
        ref: { lib: lib, path: file.path },
        move: { kind: 'note', lib: lib, path: file.path, dir: dirRel, title: title },
      }));
    });
  }

  /** 把一个**真实文件夹**挂成笔记库 —— 之后在树上新建 / 改名 / 搬家都落在它里面。 */
  function addVault() {
    var input = h('input.input', {
      type: 'text',
      placeholder: '文件夹的完整路径，例如 F:\\Vaults\\Math',
    });
    var box = ui.modal({
      title: '添加笔记库',
      body: h(
        'div.form__field',
        null,
        input,
        h('p.form__hint', {
          text: '这个目录会被当成一个笔记库：在树上新建、改名、搬家都直接落在它里面。只写进库清单，不动其中已有的文件。',
        })
      ),
      actions: [
        { label: '取消', kind: 'ghost' },
        {
          label: '添加',
          kind: 'primary',
          onClick: function () {
            var value = input.value.trim();
            if (!value) return false;                       // 没填就别关
            api.post('/notes/roots', { action: 'add', path: value })
              .then(function () {
                ui.toast('挂上了：' + value);
                paint();
              })
              .catch(function (err) { toastErr('没挂上', err); });
            return true;
          },
        },
      ],
    });
    setTimeout(function () { input.focus(); }, 30);
    return box;
  }

  function removeVault(lib) {
    ui.confirmDialog(
      '把「' + lib.name + '」从库清单里去掉？',
      { okLabel: '去掉', danger: true, title: '从清单去掉（不动文件）' }
    ).then(function (ok) {
      if (!ok) return;
      api.post('/notes/roots', { action: 'remove', path: lib.root })
        .then(function () { ui.toast('去掉了：' + lib.name); paint(); })
        .catch(function (err) { toastErr('没去掉', err); });
    });
  }

  function paintVaults(box) {
    return api.get('/notes/stats').then(function (data) {
      var libs = (data && data.libraries) || [];
      libs.forEach(function (lib) {
        box.appendChild(group(1, {
          key: 'vault:' + lib.name,
          label: lib.name,
            // 库这一行本身就是**库根目录**：能接拖放（搬回来）、能在根上新建
          drop: { kind: 'note', lib: lib.name, dir: '', label: lib.name },
          actions: function () {
            var items = folderActions({ kind: 'note', lib: lib.name, dir: '' });
            // 导入进来的库不给移除（那是数据的一部分），只给用户自己挂进来的
            if (lib.external) {
              items.push({
                label: '从清单去掉（不动文件）',
                run: function () { removeVault(lib); },
              });
            }
            return items;
          },
          count: lib.notes || lib.count || null,
          load: function (body) { paintVault(body, lib); },
        }));
      });
      // 挂一个真实文件夹当库 —— 用户的原话是"一开始要求用户选择一个文件夹"
      var add = leaf(2, {
        label: '添加笔记库…',
        icon: 'plus',
        title: '挑一个真实文件夹当笔记库（笔记本体就存在那里）',
      });
      add.addEventListener('click', function () { addVault(); }, true);
      box.appendChild(add);
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
      // 组头右上角那个「＋」= 开一条新对话（用户要求把它做成图标放在那儿）；
      // 分组相关的几件事在右键菜单里，同一个 actions 也挂在组头上
      plusRun: newConversation,
      plusTitle: '新建对话',
      plusAlways: true,
      drop: { kind: 'chat', dir: '', label: '对话' },
      actions: function () { return chatFolderActions(''); },
      load: paintConversations,
    }));
    box.appendChild(group(0, {
      key: 'root:library',
      label: '资料',
      icon: 'book',
      // 只有一个根时，这一行就是个目录（根本身）：能接拖放、能在根上建
      // 多根时 state.libRoot 是空串，落到这一行就自然不接（两个根的同名目录混不起）
      drop: function () {
        return state.libRoot ? { kind: 'doc', dir: state.libRoot, label: '资料' } : null;
      },
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
    // 笔记图谱**不在这儿**了：它是笔记的东西，入口归笔记页（用户："笔记的图谱去
    // 专门的笔记页！不要和其他的在一个树里"）。渲染那份代码在 notegraph.js，
    // 资源页要开图谱另有窗格视图（workbench.js 里注册的 'notegraph'）。
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
