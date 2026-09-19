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
    return api.get('/library/items?limit=600').then(function (res) {
      var list = (res && res.items) || [];
      if (!list.length) { err(box, '资料目录里还没扫出条目'); return; }
      var root = { dirs: {}, files: [] };
      list.forEach(function (one) {
        var parts = String(one.rel || one.citekey || '').split('/').filter(Boolean);
        parts.pop();                        // 最后一段是文件名，不进目录树
        var node = root;
        parts.forEach(function (seg) {
          node.dirs[seg] = node.dirs[seg] || { dirs: {}, files: [] };
          node = node.dirs[seg];
        });
        node.files.push(one);
      });
      paintItemNode(box, root, 2, '');
    }).catch(function () { err(box, '读不到资料条目'); });
  }

  function paintItemNode(box, node, level, prefix) {
    Object.keys(node.dirs).sort().forEach(function (name) {
      var child = node.dirs[name];
      var path = prefix ? prefix + '/' + name : name;
      box.appendChild(group(level, {
        key: 'libdir:' + path,
        label: name,
        iconSlot: true,
        count: countItems(child),
        load: function (body) { paintItemNode(body, child, level + 1, path); },
      }));
    });
    node.files.forEach(function (one) {
      box.appendChild(leaf(level, {
        label: one.title || one.citekey,
        icon: KIND_ICON[one.kind] || 'book',
        count: one.year || null,
        // 悬停看得见出处：类型 · 引用键 · 相对路径
        title: [one.kind, one.citekey, one.rel].filter(Boolean).join('\n'),
        kind: 'doc',
        ref: { citekey: one.citekey },
      }));
    });
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
        iconSlot: true,
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
          iconSlot: true,
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
