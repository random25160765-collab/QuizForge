/* 笔记：写作台（Obsidian 的结构 + 幕布的大纲）。
 *
 * 四种模式，都是**同一份 Markdown**：
 *   阅读 —— 渲染后的样子；
 *   分栏 —— 左边写、右边即时渲染（默认）；
 *   编辑 —— 全宽写，干扰最少；
 *   大纲 —— 逐行改（回车同级、Tab 缩进、Alt+↑↓ 挪块、折叠）。
 *
 * 三条底线：
 *  1. **你写的字是权威**：保存走 `/notes/body`，后端只换正文、原样保留 YAML 头（`notes.splice_body`）；
 *     每次写之前后端先存快照，所以「撤销」和「改动历史」一直都在。
 *  2. **不打断**：自动保存只更新状态文字，**不回写编辑框** —— 回写会顶掉光标，
 *     写着写着跳一下是最伤的（这一条是踩过才写下的）。
 *  3. **接口路径不含 `/api` 前缀**：api.js 自己拼 BASE。写成 `/api/notes/…` 会变成
 *     `/api/api/notes/…` → 404，而 `.catch` 把它收成一条 toast：页面不报错、一直空着。
 */
(function () {
  'use strict';

  var QF = window.QF || {};
  var ui = QF.ui;
  var api = QF.api;

  var rootEl = document.getElementById('notes-root');
  if (!rootEl) return;

  var el = {
    lib: document.getElementById('notes-lib'),
    newBtn: document.getElementById('notes-new'),
    search: document.getElementById('notes-search'),
    clear: document.getElementById('notes-clear'),
    tree: document.getElementById('notes-tree'),
    main: document.getElementById('notes-main'),
    side: document.getElementById('notes-side')
  };

  var state = {
    lib: '',
    libs: [],
    tree: null,
    note: null,
    mode: 'split',      // read | split | edit | outline
    selected: -1,       // 大纲里选中的行
    editing: false,     // 行内编辑中（键盘交给输入框）
    dirty: false,
    saving: false,
    savedAt: ''
  };
  var folded = {};      // 大纲折叠
  var dirFolded = {};   // 目录折叠
  var saveTimer = null;
  var prevTimer = null;
  var lastTyped = '';   // 自动配对用：上一次的编辑框内容
  var dragPath = '';    // 树里正在拖的条目

  var EMPTY_MARK =
    '<svg class="nempty__mark" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" ' +
    'stroke-linecap="round" stroke-linejoin="round"><path d="M6.5 3.5h8L19.5 8.5v12h-13Z"></path>' +
    '<path d="M14.5 3.5v5h5"></path><path d="M9.5 13h5M9.5 16.5h3"></path></svg>';

  /* ------------------------------------------------------------ 小工具 */

  function h() {
    return ui.h.apply(ui, arguments);
  }

  function toast(message, kind) {
    if (ui && ui.toast) ui.toast(message, kind || 'info');
  }

  function fail(err) {
    toast((err && err.message) || '出错了', 'bad');
  }

  function q(params) {
    return Object.keys(params)
      .filter(function (k) {
        return params[k] !== undefined && params[k] !== null && params[k] !== '';
      })
      .map(function (k) {
        return encodeURIComponent(k) + '=' + encodeURIComponent(params[k]);
      })
      .join('&');
  }

  function esc(text) {
    return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function nowHM() {
    var d = new Date();
    var pad = function (n) {
      return (n < 10 ? '0' : '') + n;
    };
    return pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  function isNote() {
    return state.note && state.note.kind === 'note';
  }

  /* ------------------------------------------------------------ 起手 */

  function boot() {
    try {
      QF.shell.mount({});
    } catch (err) {
      if (window.console) console.warn('顶栏接线失败', err);
    }
    applyTypography();
    bind();
    loadLibs();
  }

  function bind() {
    el.lib.addEventListener('change', function () {
      if (state.dirty) saveNow(true);
      state.lib = el.lib.value;
      state.note = null;
      folded = {};
      dirFolded = {};
      el.search.value = '';
      el.clear.hidden = true;
      renderMain();
      renderSide();
      loadTree();
    });

    // 新建是最高频动作：按钮点一下就在根目录建一篇，随即在树里改标题（不弹层）
    el.newBtn.addEventListener('click', function () {
      createNote('', '');
    });

    el.clear.addEventListener('click', function () {
      el.search.value = '';
      el.clear.hidden = true;
      loadTree();
    });

    var timer = null;
    el.search.addEventListener('input', function () {
      if (timer) clearTimeout(timer);
      var term = el.search.value.trim();
      el.clear.hidden = !term;
      timer = setTimeout(function () {
        if (term) runSearch(term);
        else loadTree();
      }, 220);
    });

    document.addEventListener('keydown', onKey);
    window.addEventListener('beforeunload', function (ev) {
      if (!state.dirty) return;
      // 还有没落下的字 —— 提醒一下，别让它悄悄消失
      ev.preventDefault();
      ev.returnValue = '';
    });
  }

  /* ------------------------------------------------------------ 库与树 */

  function loadLibs() {
    api
      .get('/notes/stats')
      .then(function (data) {
        state.libs = data.libraries || [];
        ui.clear(el.lib);
        if (!state.libs.length) {
          el.tree.appendChild(
            h('div.npanel__hint', { text: '还没有笔记库。用 tools/import_vault.py 导一个进来。' })
          );
          return;
        }
        state.libs.forEach(function (item) {
          el.lib.appendChild(h('option', { value: item.name, text: item.name + '（' + item.notes + '）' }));
        });
        state.lib = state.libs[0].name;
        el.lib.value = state.lib;
        loadTree();
      })
      .catch(fail);
  }

  function loadTree() {
    if (!state.lib) return Promise.resolve();
    // 返回这个 Promise：新建之后要"等树画完再就地把标题变成输入框"
    return api
      .get('/notes/tree?' + q({ lib: state.lib }))
      .then(function (tree) {
        state.tree = tree;
        ui.clear(el.tree);
        el.tree.appendChild(
          h(
            'div.ntree__head',
            null,
            h('span', { text: (tree.count || 0) + ' 条目' }),
            h('button.nbtn', {
              type: 'button',
              text: '全部展开/收起',
              title: '把目录全折起来',
              onClick: function () {
                var anyOpen = Object.keys(dirFolded).some(function (k) {
                  return !dirFolded[k];
                });
                dirFolded = {};
                Object.keys(collectDirs(tree)).forEach(function (p) {
                  dirFolded[p] = !anyOpen;
                });
                loadTree();
              }
            })
          )
        );
        (tree.dirs || []).forEach(function (dir) {
          el.tree.appendChild(dirNode(dir, 1));
        });
        (tree.files || []).forEach(function (file) {
          el.tree.appendChild(fileNode(file, 1));
        });
      })
      .catch(fail);
  }

  function collectDirs(node, out) {
    out = out || {};
    (node.dirs || []).forEach(function (child) {
      out[child.path] = true;
      collectDirs(child, out);
    });
    return out;
  }

  function dirNode(dir, depth) {
    var box = h('div');
    var isFolded = !!dirFolded[dir.path];
    var node = h(
      'button.ntree__dir',
      {
        type: 'button',
        title: dir.path,
        style: { paddingLeft: 4 + depth * 11 + 'px' },
        onClick: function () {
          dirFolded[dir.path] = !dirFolded[dir.path];
          loadTree();
        },
        onContextmenu: function (ev) {
          showMenu(ev, dirMenu(dir));
        }
      },
      h('span.ntree__caret' + (isFolded ? '.ntree__caret--folded' : ''), { text: '▾' }),
      h('span.ntree__label', { text: dir.name }),
      h('span.ntree__count', { text: String(dir.count || 0) })
    );
    // 拖到目录上 = 改父级（Trilium 的 hitMode `over`）
    node.addEventListener('dragover', function (ev) {
      if (!dragPath) return;
      ev.preventDefault();
      node.classList.add('is-drop');
    });
    node.addEventListener('dragleave', function () {
      node.classList.remove('is-drop');
    });
    node.addEventListener('drop', function (ev) {
      ev.preventDefault();
      node.classList.remove('is-drop');
      var from = dragPath || ev.dataTransfer.getData('text/qf-note');
      dragPath = '';
      if (from) moveNote(from, dir.path);
    });
    box.appendChild(node);
    if (isFolded) return box;
    (dir.dirs || []).forEach(function (child) {
      box.appendChild(dirNode(child, depth + 1));
    });
    (dir.files || []).forEach(function (file) {
      box.appendChild(fileNode(file, depth + 1));
    });
    return box;
  }

  function fileNode(file, depth) {
    var active = state.note && state.note.path === file.path;
    var dirtyHere = active && state.dirty;
    var label = file.prefix ? file.prefix + ' - ' + (file.title || file.name) : file.title || file.name;
    var node = h(
      'button.ntree__file' + (active ? '.is-on' : '') + (file.archived ? '.is-archived' : ''),
      {
        type: 'button',
        title: file.path,
        draggable: 'true',
        dataset: { path: file.path },
        style: { paddingLeft: 4 + depth * 11 + 'px' },
        onClick: function () {
          openNote(file.path);
        },
        onDblclick: function () {
          startInlineRename(file.path);
        },
        onContextmenu: function (ev) {
          showMenu(ev, fileMenu(file));
        },
        onDragstart: function (ev) {
          dragPath = file.path;
          if (ev.dataTransfer) {
            ev.dataTransfer.effectAllowed = 'move';
            ev.dataTransfer.setData('text/qf-note', file.path);
          }
        },
        onDragend: function () {
          dragPath = '';
        }
      },
      h('span.ntree__caret', { text: file.kind === 'canvas' ? '◇' : '' }),
      file.color ? h('span.ntree__color', { style: { background: file.color } }) : null,
      file.icon ? h('span.ntree__icon', { text: file.icon }) : null,
      h('span.ntree__label', { text: label }),
      dirtyHere ? h('span.ntree__dirty', { title: '还没保存' }) : null
    );
    return node;
  }

  /** 树上的右键菜单。不是要齐全，是要"手边那几个" —— 新建、改名、复制、删。 */
  function showMenu(ev, items) {
    ev.preventDefault();
    ev.stopPropagation();
    closeMenu();
    if (!items.length) return;
    var menu = h('div.nctx', { id: 'notes-context' });
    items.forEach(function (item) {
      if (item === '-') {
        menu.appendChild(h('div.nctx__sep'));
        return;
      }
      menu.appendChild(
        h('button.nctx__item' + (item.danger ? '.nctx__item--danger' : ''), {
          type: 'button',
          text: item.label,
          onClick: function () {
            closeMenu();
            item.run();
          }
        })
      );
    });
    document.body.appendChild(menu);
    var box = menu.getBoundingClientRect();
    menu.style.left = Math.min(ev.clientX, window.innerWidth - box.width - 8) + 'px';
    menu.style.top = Math.min(ev.clientY, window.innerHeight - box.height - 8) + 'px';
    setTimeout(function () {
      document.addEventListener('click', closeMenu, { once: true });
      document.addEventListener('contextmenu', closeMenu, { once: true });
    }, 0);
  }

  function closeMenu() {
    var found = document.getElementById('notes-context');
    if (found) found.remove();
  }

  function copyText(text, what) {
    var done = function () {
      toast('已复制' + (what || ''), 'ok');
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () {
        toast('复制失败（浏览器不给权限）', 'bad');
      });
      return;
    }
    toast('这个浏览器不支持复制', 'bad');
  }

  function fileMenu(file) {
    var folder = file.path.split('/').slice(0, -1).join('/');
    var stem = file.name.replace(/\.md$/i, '');
    return [
      { label: '新建同级笔记', run: function () { createNote(folder, ''); } },
      {
        label: '新建子笔记',
        run: function () {
          // 子笔记落在"以这篇命名"的目录里（Obsidian 的 folder note 惯例）
          createNote(folder ? folder + '/' + stem : stem, '');
        }
      },
      '-',
      { label: '重命名', run: function () { startInlineRename(file.path); } },
      { label: '复制双链 [[…]]', run: function () { copyText('[[' + (file.title || stem) + ']]', '双链'); } },
      { label: '复制路径', run: function () { copyText(file.path, '路径'); } },
      '-',
      {
        label: file.archived ? '取消归档' : '归档',
        run: function () {
          api
            .post('/notes/meta', {
              lib: state.lib,
              path: file.path,
              meta: { archived: file.archived ? '' : true }
            })
            .then(function () {
              toast(file.archived ? '已取消归档' : '已归档（树里淡显，检索仍能命中）', 'ok');
              loadTree();
            })
            .catch(fail);
        }
      },
      {
        label: '删除（进回收站）',
        danger: true,
        run: function () {
          ui.confirm('把「' + (file.title || stem) + '」移到回收站？文件不会被真删。', { okLabel: '移到回收站' }).then(
            function (yes) {
              if (!yes) return;
              api
                .post('/notes/delete', { lib: state.lib, path: file.path })
                .then(function () {
                  toast('已移到回收站', 'ok');
                  if (state.note && state.note.path === file.path) {
                    state.note = null;
                    renderMain();
                    renderSide();
                  }
                  loadTree();
                })
                .catch(fail);
            }
          );
        }
      }
    ];
  }

  function dirMenu(dir) {
    return [
      { label: '在这里新建笔记', run: function () { createNote(dir.path, ''); } },
      {
        label: dirFolded[dir.path] ? '展开' : '折叠',
        run: function () {
          dirFolded[dir.path] = !dirFolded[dir.path];
          loadTree();
        }
      },
      { label: '复制路径', run: function () { copyText(dir.path, '路径'); } }
    ];
  }

  function moveNote(from, folder) {
    if (!from) return;
    if (from.split('/').slice(0, -1).join('/') === folder) return;   // 已经在那个目录里
    api
      .post('/notes/move', { lib: state.lib, path: from, folder: folder })
      .then(function () {
        toast('已移动', 'ok');
        loadTree();
        if (state.note && state.note.path === from) {
          var wasDirty = state.dirty;
          state.note = null;
          openNote(from.split('/').pop());
          if (wasDirty) state.dirty = true;
        }
      })
      .catch(fail);
  }

  /* ------------------------------------------------------------ 打开笔记 */

  function openNote(path, line) {
    if (!path) return;
    if (state.note && state.note.path === path) return;
    if (state.dirty) saveNow(true);
    if (window.innerWidth <= 1180) {
      el.side.classList.add('is-open');
    }
    api
      .get('/notes/note?' + q({ lib: state.lib, path: path }))
      .then(function (note) {
        state.note = note;
        state.selected = typeof line === 'number' ? line : -1;
        state.dirty = false;
        state.saving = false;
        state.savedAt = nowHM();
        folded = {};
        // 打开就准备写：宽屏分栏（边写边看），窄屏纯编辑。阅读要自己切 ——
        // 这一页是写作台，默认状态应当是"能打字"。
        state.mode = note.kind === 'canvas' ? 'read' : window.innerWidth > 1180 ? 'split' : 'edit';
        renderMain();
        renderSide();
        loadTree();
      })
      .catch(fail);
  }

  /* ------------------------------------------------------------ 主区 */

  function renderMain() {
    ui.clear(el.main);
    if (!isNote() && !(state.note && state.note.kind === 'canvas')) {
      el.main.appendChild(emptyState());
      return;
    }
    el.main.appendChild(renderBar());
    if (state.note.kind === 'canvas') {
      var pane = h('div.note__pane');
      pane.appendChild(
        h(
          'div.nempty',
          null,
          h('div', { html: EMPTY_MARK }),
          h('div.nempty__title', { text: state.note.title }),
          h('div.nempty__text', { text: state.note.message || '画布的渲染在下一步做。' })
        )
      );
      el.main.appendChild(pane);
      return;
    }
    el.main.appendChild(renderBody());
  }

  function emptyState() {
    return h(
      'div.nempty',
      null,
      h('div', { html: EMPTY_MARK }),
      h('div.nempty__title', { text: '开始写' }),
      h('div.nempty__text', {
        text: '左边挑一篇，或者新建一篇。写作台默认是分栏：左边写 Markdown，右边即时渲染。'
      }),
      h(
        'div.nempty__actions',
        null,
        h('button.nbtn.nbtn--primary', {
          type: 'button',
          text: '新建一篇',
          onClick: function () {
            createNote('', '');
          }
        })
      )
    );
  }

  function renderBar() {
    var note = state.note;
    var bar = h('div.note__bar');

    var title = h('h1.note__title', {
      text: note.title,
      title: '双击改标题（会连同文件名一起改，引用它的地方也会跟着改）',
      onDblclick: renameTitle
    });
    bar.appendChild(title);

    bar.appendChild(
      h(
        'div.seg',
        null,
        [['read', '阅读'], ['split', '分栏'], ['edit', '编辑'], ['outline', '大纲']].map(function (pair) {
          return h(
            'button.seg__item' + (state.mode === pair[0] ? '.is-on' : ''),
            {
              type: 'button',
              text: pair[1],
              onClick: function () {
                if (state.mode === pair[0]) return;
                if (state.dirty) saveNow(true);
                state.mode = pair[0];
                renderMain();
              }
            }
          );
        })
      )
    );

    bar.appendChild(h('span.note__status' + (state.dirty ? '.note__status--dirty' : ''), {
      id: 'note-status',
      text: statusText()
    }));

    // 撤销摆在最容易够到的地方：改字最需要的就是"刚才那一步不算"
    if (state.note.can_undo) {
      bar.appendChild(
        h('button.nbtn', {
          type: 'button',
          text: '撤销',
          title: '回到上一次改动之前（每一版都留在「改动历史」里）',
          onClick: function () {
            if (state.dirty) saveNow(true);
            api
              .post('/notes/undo', { lib: state.lib, path: state.note.path })
              .then(function () {
                toast('已回到上一版', 'ok');
                var path = state.note.path;
                state.note = null;
                openNote(path);
              })
              .catch(fail);
          }
        })
      );
    }

    return bar;
  }

  function statusText() {
    if (!isNote()) return '';
    var body = currentText();
    var lines = body ? body.split('\n').length : 0;
    var chars = body.replace(/\s/g, '').length;
    var tail = state.saving
      ? '保存中…'
      : state.failed
        ? '保存失败，重试中…'
        : state.dirty
          ? '未保存'
          : '已保存 ' + state.savedAt;
    return lines + ' 行 · ' + chars + ' 字 · ' + tail;
  }

  /** Alt+L：加一个标签（写进 YAML 头，不是正文）。 */
  function addLabelPrompt() {
    if (!isNote()) return;
    var current = (state.note.tags || []).join(', ');
    var input = h('input.oline__input', { type: 'text', value: '', placeholder: '新标签' });
    ui.modal({
      title: '加标签',
      size: 'sm',
      body: h(
        'div',
        null,
        h('div.npanel__hint', { text: '现有的：' + (current || '（还没有）') }),
        input
      ),
      actions: [
        { label: '取消', kind: 'ghost', onClick: function (close) { close(); } },
        {
          label: '加上',
          kind: 'primary',
          onClick: function (close) {
            var name = input.value.trim().replace(/^#/, '');
            close();
            if (!name) return;
            var tags = (state.note.tags || []).slice();
            if (tags.indexOf(name) < 0) tags.push(name);
            saveMeta({ tags: tags });
          }
        }
      ]
    });
    setTimeout(function () {
      input.focus();
    }, 0);
  }

  function renderStatusOnly() {
    var node = document.getElementById('note-status');
    if (!node) return;
    node.textContent = statusText();
    node.className = 'note__status' + (state.dirty ? ' note__status--dirty' : '');
  }

  function currentText() {
    var area = document.querySelector('.nedit__area');
    if (area) return area.value;
    return (state.note && state.note.body) || '';
  }

  function renderBody() {
    if (state.mode === 'read') return renderRead();
    if (state.mode === 'outline') return renderOutline();
    return renderEditor(state.mode === 'split');
  }

  /* ------------------------------------------------------------ 阅读 */

  function renderRead() {
    var pane = h('div.note__pane');
    var inner = h('div.nread__inner');
    var text = state.note.body || '';
    if (!text.trim()) {
      inner.appendChild(h('div.nread__empty', { text: '（这篇还是空的 —— 切到「编辑」写点什么）' }));
    } else if (QF.md && QF.md.render) {
      try {
        inner.appendChild(QF.md.render(text));
      } catch (err) {
        inner.appendChild(h('pre', { text: text }));
      }
      linkify(inner);
    } else {
      inner.appendChild(h('pre', { text: text }));
    }
    pane.appendChild(h('div.nread', null, inner));
    return pane;
  }

  /** 渲染完再走一遍 DOM，把 `[[目标|别名]]` 换成可点的元素。
   *
   * 不改 Markdown 源 —— 源文件是权威，渲染后再贴，一个字节都不动。
   * 解析不到的目标给虚线样式（读的时候就知道哪条是断的）。
   */
  function linkify(container) {
    if (!container || !document.createTreeWalker) return;
    var known = knownTargets();
    var walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        if (!node.nodeValue || node.nodeValue.indexOf('[[') < 0) return NodeFilter.FILTER_REJECT;
        var parent = node.parentNode;
        var tag = parent && parent.nodeName;
        if (tag === 'CODE' || tag === 'PRE' || tag === 'A') return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    var nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach(function (node) {
      var text = node.nodeValue;
      var pattern = /!?\[\[([^\[\]]+)\]\]/g;
      var frag = document.createDocumentFragment();
      var last = 0;
      var match;
      var hit = false;
      while ((match = pattern.exec(text))) {
        hit = true;
        if (match.index > last) frag.appendChild(document.createTextNode(text.slice(last, match.index)));
        var inner = match[1];
        var parts = inner.split('|');
        var head = parts[0].split('#')[0].trim();
        var embed = match[0].charAt(0) === '!';
        var label = (parts[1] || head).trim();
        var missing = !known(head);
        frag.appendChild(
          h(
            'a.wikilink' + (embed ? '.wikilink--embed' : '') + (missing ? '.wikilink--missing' : ''),
            {
              href: '#',
              title: missing ? '还不存在：' + head + '（点一下可以建）' : head,
              onClick: function (ev) {
                ev.preventDefault();
                follow(head, missing);
              }
            },
            label
          )
        );
        last = match.index + match[0].length;
      }
      if (!hit) return;
      if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
      node.parentNode.replaceChild(frag, node);
    });
  }

  function knownTargets() {
    var index = {};
    (function walk(node) {
      if (!node) return;
      (node.files || []).forEach(function (file) {
        index[file.path.replace(/\.md$/i, '')] = file.path;
        index[file.name.replace(/\.md$/i, '')] = file.path;
        if (file.title) index[file.title] = file.path;
      });
      (node.dirs || []).forEach(walk);
    })(state.tree);
    return function (target) {
      var want = String(target).replace(/^\.\//, '').replace(/\\/g, '/').replace(/\.md$/i, '');
      return index[want] || index[want.split('/').pop()] || '';
    };
  }

  function follow(target, missing) {
    var find = knownTargets();
    var path = find(target);
    if (path) {
      openNote(path);
      return;
    }
    if (missing) {
      ui.confirm('「' + target + '」还不存在。现在建一篇？', { okLabel: '建一篇' }).then(function (yes) {
        if (yes) createNote('', target);
      });
      return;
    }
    toast('找不到「' + target + '」', 'info');
  }

  /* ------------------------------------------------------------ 编辑器 */

  function renderEditor(withPreview) {
    var pane = h('div.note__pane' + (withPreview ? '.note__pane--split' : ''));
    var src = editorPane();
    if (!withPreview) {
      pane.appendChild(src);
      pane.classList.remove('note__pane--split');
      return pane;
    }
    var out = h('div.nsplit__half.nsplit__half--out', { id: 'notes-preview-pane' });
    var preview = h('div.nread', null, h('div.nread__inner', { id: 'notes-preview' }));
    out.appendChild(preview);
    pane.appendChild(src);
    pane.appendChild(out);
    schedulePreview();
    return pane;
  }

  function editorPane() {
    var box = h('div.nedit');
    box.appendChild(renderPad());

    var area = h('textarea.nedit__area', {
      spellcheck: 'false',
      placeholder: '在这里写 Markdown。[[双链]] · $$公式$$ · Tab 缩进 · ⌘S 立即保存',
      value: state.note.body || ''
    });
    box.appendChild(area);

    lastTyped = area.value;
    area.addEventListener('input', function () {
      autoPair(area);
      lastTyped = area.value;
      markDirty();
      schedulePreview();
      scheduleSave();
    });
    area.addEventListener('blur', function () {
      if (state.dirty) saveNow();
    });
    area.addEventListener('keydown', onEditorKey);
    // 单向滚动同步：编辑器滚到哪，预览跟到哪（Trilium 的 useSyncedScrolling）。
    // 比例映射而不是精确对齐 —— 精确对齐要 md.js 在渲染时给每块打源码行号，
    // 那要改渲染器；先用比例，手感已经对了，精确对齐留作后续。
    area.addEventListener(
      'scroll',
      function () {
        var pane = document.getElementById('notes-preview-pane');
        if (!pane) return;
        var span = Math.max(1, area.scrollHeight - area.clientHeight);
        var pspan = Math.max(0, pane.scrollHeight - pane.clientHeight);
        pane.scrollTop = Math.min(1, Math.max(0, area.scrollTop / span)) * pspan;
      },
      { passive: true }
    );
    ['click', 'keyup', 'select'].forEach(function (name) {
      area.addEventListener(name, function () {
        highlightCursorLine();
      });
    });
    setTimeout(function () {
      area.focus();
      area.setSelectionRange(area.value.length, area.value.length);
    }, 0);
    return box;
  }

  function renderPad() {
    var pad = h('div.nedit__pad');
    var tools = [
      ['**', '粗', function () { wrapSel('**', '**', '粗体'); }],
      ['*', '斜', function () { wrapSel('*', '*', '斜体'); }],
      ['`', '码', function () { wrapSel('`', '`', 'code'); }],
      ['#', '标题', function () { linePrefix('## '); }],
      ['-', '列表', function () { linePrefix('- '); }],
      ['1.', '有序', function () { linePrefix('1. '); }],
      ['>', '引用', function () { linePrefix('> '); }],
      ['$$', '公式', function () { wrapSel('$$\n', '\n$$', 'x = y'); }],
      ['[]', '链接', function () { wrapSel('[', '](https://)', '说明'); }],
      ['[[]]', '双链', function () { wrapSel('[[', ']]', '笔记名'); }],
      ['---', '分隔', function () { linePrefix('---'); }],
      ['|', '表格', function () { insertText('| 列 | 列 |\n| --- | --- |\n|  |  |\n'); }]
    ];
    tools.forEach(function (tool, i) {
      if (i === 3 || i === 7 || i === 9) pad.appendChild(h('span.nedit__sep'));
      pad.appendChild(
        h('button.nedit__btn', { type: 'button', text: tool[1], title: tool[1], onClick: tool[2] })
      );
    });
    pad.appendChild(h('span.nedit__sep'));
    pad.appendChild(
      h('button.nedit__btn', {
        type: 'button',
        text: '⇥ 缩进',
        title: '给选中的行加一级缩进',
        onClick: function () {
          linePrefix(indentUnit());
        }
      })
    );
    return pad;
  }

  function indentUnit() {
    var unit = state.note && state.note.indent_unit;
    return unit ? unit : '\t';
  }

  function withArea(fn) {
    var area = document.querySelector('.nedit__area');
    if (!area) {
      toast('先切到「编辑」或「分栏」', 'info');
      return;
    }
    fn(area);
    lastTyped = area.value;
    markDirty();
    schedulePreview();
    scheduleSave();
    area.focus();
  }

  function insertText(text) {
    withArea(function (area) {
      var start = area.selectionStart;
      area.setRangeText(text, start, area.selectionEnd, 'end');
    });
  }

  function wrapSel(before, after, placeholder) {
    withArea(function (area) {
      var start = area.selectionStart;
      var end = area.selectionEnd;
      var picked = area.value.slice(start, end) || placeholder || '';
      area.setRangeText(before + picked + after, start, end, 'select');
      area.setSelectionRange(start + before.length, start + before.length + picked.length);
    });
  }

  /** 光标那一行在预览里对应哪个块：按文字找，找到就高亮并滚进视野。
   *
   * Trilium 是渲染时给每块打 `data-source-line`（`Markdown.tsx:232-317`），
   * 精确对应；我们没改渲染器，于是用"这一行前 24 个可见字"去匹配 ——
   * 对纯文本行够准，公式与代码块可能匹配不上，那就**不动**（宁可不跟，也别乱跳）。
   */
  function highlightCursorLine() {
    if (state.mode !== 'split') return;
    var area = document.querySelector('.nedit__area');
    var preview = document.getElementById('notes-preview');
    var pane = document.getElementById('notes-preview-pane');
    if (!area || !preview || !pane) return;
    var line = area.value.slice(0, area.selectionStart).split('\n').pop() || '';
    var probe = line.replace(/^[\s#>*+\-\d.`\[\]()]+/, '').slice(0, 24).trim();
    var blocks = preview.children;
    for (var i = 0; i < blocks.length; i++) {
      blocks[i].classList.remove('is-cur');
    }
    if (probe.length < 3) return;
    for (var j = 0; j < blocks.length; j++) {
      if ((blocks[j].textContent || '').indexOf(probe) >= 0) {
        blocks[j].classList.add('is-cur');
        var top = blocks[j].offsetTop;
        if (top < pane.scrollTop || top > pane.scrollTop + pane.clientHeight - 40) {
          pane.scrollTop = Math.max(0, top - pane.clientHeight / 3);
        }
        return;
      }
    }
  }

  function linePrefix(prefix) {
    withArea(function (area) {
      var value = area.value;
      var start = value.lastIndexOf('\n', area.selectionStart - 1) + 1;
      var end = value.indexOf('\n', area.selectionStart);
      if (end === -1) end = value.length;
      var line = value.slice(start, end);
      if (line.indexOf(prefix) === 0) {
        area.setRangeText(line.slice(prefix.length), start, end, 'end');
      } else {
        area.setRangeText(prefix + line, start, end, 'end');
      }
    });
  }

  /** 自动配对：敲出 `[[` 补 `]]`、敲出 `**` 补 `**`，光标停在中间。
   *
   * 只认"刚刚多出来两个字符"这一种情况 —— 不去猜你打算干什么。
   */
  function autoPair(area) {
    var now = area.value;
    if (now.length === lastTyped.length + 2) {
      var at = area.selectionStart;
      var typed = now.slice(at - 2, at);
      var closer = typed === '[[' ? ']]' : typed === '**' ? '**' : '';
      if (closer && now.slice(at, at + closer.length) !== closer) {
        area.setRangeText(closer, at, at, 'end');
        area.setSelectionRange(at, at);
      }
    }
  }

  function onEditorKey(ev) {
    var area = ev.target;
    if (ev.key === 'Tab') {
      ev.preventDefault();
      if (ev.shiftKey) {
        // 反缩进：把这一行开头的一级缩进去掉
        var value = area.value;
        var start = value.lastIndexOf('\n', area.selectionStart - 1) + 1;
        var unit = indentUnit();
        if (value.slice(start, start + unit.length) === unit) {
          area.setRangeText('', start, start + unit.length, 'preserve');
        } else {
          var spaces = value.slice(start).match(/^ {1,4}/);
          if (spaces) area.setRangeText('', start, start + spaces[0].length, 'preserve');
        }
      } else {
        insertText(indentUnit());
      }
      lastTyped = area.value;
      markDirty();
      scheduleSave();
      return;
    }
    if (ev.metaKey || ev.ctrlKey) {
      var key = ev.key.toLowerCase();
      if (key === 's') {
        ev.preventDefault();
        saveNow(true);
        return;
      }
      if (key === 'b') {
        ev.preventDefault();
        wrapSel('**', '**', '粗体');
        return;
      }
      if (key === 'i') {
        ev.preventDefault();
        wrapSel('*', '*', '斜体');
        return;
      }
      if (key === 'e') {
        ev.preventDefault();
        state.mode = state.mode === 'read' ? 'split' : 'read';
        if (state.dirty) saveNow(true);
        renderMain();
        return;
      }
    }
    ev.stopPropagation();
  }

  function markDirty() {
    state.dirty = true;
    renderStatusOnly();
  }

  function scheduleSave() {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(function () {
      saveNow();
    }, 900);
  }

  /** 保存正文。**不回写编辑框** —— 回写会顶掉光标。 */
  function saveNow(force) {
    var area = document.querySelector('.nedit__area');
    if (!isNote() || !area) return Promise.resolve();
    if (!state.dirty && !force) return Promise.resolve();
    var text = area.value;
    state.saving = true;
    renderStatusOnly();
    return api
      .post('/notes/body', { lib: state.lib, path: state.note.path, body: text })
      .then(function (note) {
        // 只在"编辑框里还是我刚发出去的那份"时才认为干净；
        // 否则说明保存期间又改了，保持未保存状态等下一轮。
        var stillSame = document.querySelector('.nedit__area');
        state.note = note;
        state.saving = false;
        state.retry = 0;
        state.savedAt = nowHM();
        state.failed = false;
        state.dirty = !(stillSame && stillSame.value === text);
        renderStatusOnly();
        renderSide();
      })
      .catch(function (err) {
        // 退避重试（Trilium 的 SpacedUpdate 同款）：写不进去时**不丢内容**，
        // 隔一会儿自己再试，越试越慢，上限 30 秒。
        state.saving = false;
        state.failed = true;
        state.dirty = true;
        state.retry = (state.retry || 0) + 1;
        var wait = Math.min(1000 * Math.pow(2, state.retry), 30000);
        renderStatusOnly();
        if (state.retry === 1) fail(err);
        if (saveTimer) clearTimeout(saveTimer);
        saveTimer = setTimeout(function () {
          saveNow();
        }, wait);
      });
  }

  function schedulePreview() {
    if (prevTimer) clearTimeout(prevTimer);
    prevTimer = setTimeout(paintPreview, 140);
  }

  function paintPreview() {
    var target = document.getElementById('notes-preview');
    if (!target) return;
    var area = document.querySelector('.nedit__area');
    var text = area ? area.value : state.note.body || '';
    var host = target.parentNode && target.parentNode.parentNode; // 保住滚动位置
    var keepTop = host ? host.scrollTop : 0;
    ui.clear(target);
    if (!text.trim()) {
      target.appendChild(h('div.nread__empty', { text: '右边是即时渲染 —— 开始打字就会出现在这里。' }));
      return;
    }
    if (QF.md && QF.md.render) {
      try {
        target.appendChild(QF.md.render(text));
      } catch (err) {
        target.appendChild(h('pre', { text: text }));
      }
      linkify(target);
    }
    if (host) host.scrollTop = keepTop;
  }

  /* ------------------------------------------------------------ 大纲（幕布那半边） */

  var BULLET = { bullet: '•', heading: '', blank: '', code: '', quote: '❯', rule: '', math: '' };

  function renderOutline() {
    var pane = h('div.note__pane');
    var box = h('div.outline');
    var rows = state.note.outline || [];
    var hidden = foldedLines(rows);
    rows.forEach(function (row, idx) {
      if (hidden[idx]) return;
      var rowEl = h('div.oline' + (idx === state.selected ? '.is-on' : ''), {
        dataset: { index: String(idx), kind: row.kind },
        style: { paddingLeft: 4 + row.level * 16 + 'px' },
        onClick: function () {
          state.selected = idx;
          renderMain();
        }
      });
      if (row.foldable) {
        rowEl.appendChild(
          h('button.oline__fold' + (folded[idx] ? '.is-folded' : ''), {
            type: 'button',
            title: folded[idx] ? '展开' : '折叠',
            text: folded[idx] ? '▸' : '▾',
            onClick: function (ev) {
              ev.stopPropagation();
              folded[idx] = !folded[idx];
              renderMain();
            }
          })
        );
      } else {
        rowEl.appendChild(h('span.oline__fold.oline__fold--none'));
      }
      rowEl.appendChild(h('span.oline__bullet', { text: BULLET[row.kind] !== undefined ? BULLET[row.kind] : '•' }));
      rowEl.appendChild(
        h('div.oline__text', {
          html: row.text
            ? QF.md && QF.md.renderInline
              ? QF.md.renderInline(row.text)
              : esc(row.text)
            : '<span class="oline__placeholder">（空行）</span>'
        })
      );
      rowEl.appendChild(
        h('button.oline__edit', {
          type: 'button',
          text: '改',
          title: '编辑这一行（双击行也行）',
          onClick: function (ev) {
            ev.stopPropagation();
            editLine(idx);
          }
        })
      );
      rowEl.addEventListener('dblclick', function () {
        editLine(idx);
      });
      box.appendChild(rowEl);
    });
    box.appendChild(
      h('div.outline__hint', {
        html:
          '<kbd>回车</kbd> 同级新建 · <kbd>Tab</kbd> / <kbd>Shift+Tab</kbd> 缩进 · ' +
          '<kbd>Alt+↑</kbd> / <kbd>Alt+↓</kbd> 挪动整块 · <kbd>双击</kbd> 改这一行 · ' +
          '<kbd>退格</kbd> 清掉空行'
      })
    );
    pane.appendChild(box);
    return pane;
  }

  function foldedLines(rows) {
    var hidden = {};
    var skipTo = -1;
    rows.forEach(function (row, idx) {
      if (idx <= skipTo) {
        hidden[idx] = true;
        return;
      }
      if (folded[idx] && row.foldable) {
        for (var j = idx + 1; j < rows.length; j++) {
          if (rows[j].level <= row.level) break;
          skipTo = j;
        }
      }
    });
    return hidden;
  }

  function editLine(idx) {
    var row = (state.note.outline || [])[idx];
    var rowEl = el.main.querySelector('.oline[data-index="' + idx + '"]');
    if (!row || !rowEl) return;
    state.editing = true;
    var input = h('input.oline__input', { type: 'text', value: row.raw });
    ui.clear(rowEl);
    rowEl.appendChild(input);
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    var done = false;
    function finish(save) {
      if (done) return;
      done = true;
      state.editing = false;
      if (save && input.value !== row.raw) lineOp('replace', idx, { raw: input.value });
      else renderMain();
    }
    input.addEventListener('keydown', function (ev) {
      ev.stopPropagation();
      if (ev.key === 'Enter') {
        ev.preventDefault();
        finish(true);
      } else if (ev.key === 'Escape') {
        ev.preventDefault();
        finish(false);
      }
    });
    input.addEventListener('blur', function () {
      finish(true);
    });
  }

  function onKey(ev) {
    var tag = (ev.target && ev.target.tagName) || '';
    var typing = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
    // ---- 全局：与 Trilium 对齐的那几个（`keyboard_actions.ts:126-141, 580-587)`）----
    if ((ev.metaKey || ev.ctrlKey) && !ev.altKey) {
      var key = (ev.key || '').toLowerCase();
      if (key === 'o' || key === 'p' || (key === 'n' && !state.note)) {
        // Ctrl+O 后面插入、Ctrl+P 建子笔记：我们按"落在同一目录 / 落在同名子目录"实现
        ev.preventDefault();
        var folder = '';
        if (key === 'p') {
          var here = state.note ? state.note.path.split('/').slice(0, -1).join('/') : '';
          var stem = state.note ? state.note.path.split('/').pop().replace(/\.md$/i, '') : '';
          folder = here ? here + '/' + stem : stem;
        } else if (state.note) {
          folder = state.note.path.split('/').slice(0, -1).join('/');
        }
        createNote(folder, '');
        return;
      }
    }
    if (!typing && ev.altKey && !ev.metaKey && !ev.ctrlKey) {
      var letter = (ev.key || '').toLowerCase();
      if (letter === 't') {
        // Alt+T 插入日期时间（Trilium 同款）
        ev.preventDefault();
        var now = new Date();
        var pad = function (n) { return (n < 10 ? '0' : '') + n; };
        insertText(now.getFullYear() + '-' + pad(now.getMonth() + 1) + '-' + pad(now.getDate()) + ' ' + pad(now.getHours()) + ':' + pad(now.getMinutes()));
        return;
      }
      if (letter === 'l' && isNote()) {
        // Alt+L 加标签（写进 YAML 头）
        ev.preventDefault();
        addLabelPrompt();
        return;
      }
    }
    if (state.mode !== 'outline' || !isNote() || state.editing) return;
    if (typing) return;
    var rows = state.note.outline || [];
    var idx = state.selected;
    if (idx < 0 || !rows[idx]) return;

    if (ev.key === 'Enter') {
      ev.preventDefault();
      lineOp('insert', idx);
    } else if (ev.key === 'Tab') {
      ev.preventDefault();
      lineOp(ev.shiftKey ? 'outdent' : 'indent', idx);
    } else if (ev.key === 'Backspace' && !rows[idx].text) {
      ev.preventDefault();
      lineOp('delete', idx);
    } else if (ev.altKey && (ev.key === 'ArrowUp' || ev.key === 'ArrowDown')) {
      ev.preventDefault();
      lineOp('move', idx, { delta: ev.key === 'ArrowDown' ? 1 : -1 });
    } else if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      ev.preventDefault();
      var next = idx + (ev.key === 'ArrowDown' ? 1 : -1);
      if (next >= 0 && next < rows.length) {
        state.selected = next;
        renderMain();
        var sel = el.main.querySelector('.oline[data-index="' + next + '"]');
        if (sel && sel.scrollIntoView) sel.scrollIntoView({ block: 'nearest' });
      }
    }
  }

  function lineOp(op, index, extra) {
    var payload = { lib: state.lib, path: state.note.path, op: op, index: index };
    Object.keys(extra || {}).forEach(function (key) {
      payload[key] = extra[key];
    });
    api
      .post('/notes/line', payload)
      .then(function (note) {
        state.note = note;
        if (op === 'insert') state.selected = Math.min(index + 1, (note.outline || []).length - 1);
        if (op === 'delete') state.selected = Math.max(0, index - 1);
        renderMain();
        renderSide();
        if (op === 'insert') {
          var fresh = state.selected;
          setTimeout(function () {
            editLine(fresh);
          }, 0);
        }
      })
      .catch(fail);
  }

  /* ------------------------------------------------------------ 右栏 */

  function renderSide() {
    ui.clear(el.side);
    if (!state.note) return;
    var note = state.note;

    var props = h('section.npanel');
    props.appendChild(h('div.npanel__title', { text: '属性' }));
    props.appendChild(
      propRow('标题', note.meta && note.meta.title ? note.meta.title : note.title, '未命名', function (v) {
        saveMeta({ title: v });
      })
    );
    props.appendChild(
      propRow(
        '标签',
        (note.tags || []).join(', '),
        '还没有标签',
        function (v) {
          saveMeta({
            tags: v
              .split(/[,，]\s*/)
              .map(function (s) {
                return s.trim();
              })
              .filter(Boolean)
          });
        }
      )
    );
    if (note.generated_header) {
      props.appendChild(
        h('div.npanel__hint', { text: '这个头是导入时补的（带 qf_generated 标记，可批量回退）' })
      );
    }
    el.side.appendChild(props);
    el.side.appendChild(typographyPanel());

    if (note.kind === 'note') {
      var heads = (note.outline || []).filter(function (row) {
        return row.kind === 'heading';
      });
      if (heads.length) {
        var outlinePanel = h('section.npanel', null, h('div.npanel__title', { text: '大纲' }));
        heads.forEach(function (row) {
          outlinePanel.appendChild(
            h(
              'button.nitem.nitem--head',
              {
                type: 'button',
                title: row.text,
                onClick: function () {
                  jumpToLine(row.index);
                }
              },
              h('span', { text: '　'.repeat(Math.max(0, row.level)) + row.text })
            )
          );
        });
        el.side.appendChild(outlinePanel);
      }

      el.side.appendChild(
        listPanel('反链 · 谁引用了我', note.backlinks || [], function (item) {
          openNote(item.path, item.line);
        })
      );
      el.side.appendChild(linkPanel('出链 · 我引用了谁', note.outlinks || []));
      if ((note.unresolved || []).length) el.side.appendChild(unresolvedPanel(note.unresolved));
      loadHistory();
    }
    loadTags();
  }

  /** 点侧栏大纲 → 切到编辑态并把光标放到那一行（不猜滚动位置，直接定位）。 */
  function jumpToLine(line) {
    if (state.mode === 'read' || state.mode === 'outline') {
      state.mode = 'edit';
      renderMain();
    }
    setTimeout(function () {
      var area = document.querySelector('.nedit__area');
      if (!area) return;
      var upto = area.value.split('\n').slice(0, line).join('\n');
      var at = upto.length;
      area.focus();
      area.setSelectionRange(at, at);
      // 大致把那一行滚到视野中：按行号比例算一个位置，够用
      var total = area.value.split('\n').length || 1;
      area.scrollTop = Math.max(0, (line / total) * area.scrollHeight - area.clientHeight / 3);
    }, 0);
  }

  /* ------------------------------------------------------------ 排版
   *
   * Trilium 把这四项放在 选项 → 外观 → 字体，并用服务端生成的 `api/fonts` 样式表下发
   * （`options/appearance_fonts.tsx` / `services/font.ts`）。我们是本地单页，
   * 用 CSS 变量 + localStorage 就够 —— 不为此多一次服务端往返。
   *
   * 之所以必须有：这一页是拿来看字、写字的地方，"字号不合适"不是审美问题，是不能用。
   */

  var TYPO_DEFAULTS = { size: 14, line: 1.82, width: 74, mono: false };

  function typoPrefs() {
    var out = {};
    Object.keys(TYPO_DEFAULTS).forEach(function (key) {
      out[key] = TYPO_DEFAULTS[key];
    });
    try {
      var saved = JSON.parse(window.localStorage.getItem('qf.notes.typo') || '{}');
      Object.keys(TYPO_DEFAULTS).forEach(function (key) {
        if (saved[key] !== undefined) out[key] = saved[key];
      });
    } catch (err) {
      /* 存坏了就用默认，不值得打扰用户 */
    }
    return out;
  }

  function applyTypography() {
    var prefs = typoPrefs();
    rootEl.style.setProperty('--notes-size', prefs.size + 'px');
    rootEl.style.setProperty('--notes-line', String(prefs.line));
    rootEl.style.setProperty('--notes-width', prefs.width + 'ch');
    rootEl.style.setProperty('--notes-mono', prefs.mono ? 'var(--font-mono, monospace)' : 'inherit');
    return prefs;
  }

  function setTypo(key, value) {
    var prefs = typoPrefs();
    prefs[key] = value;
    try {
      window.localStorage.setItem('qf.notes.typo', JSON.stringify(prefs));
    } catch (err) {
      /* 存不下就只在这次生效 */
    }
    applyTypography();
  }

  function typographyPanel() {
    var prefs = applyTypography();
    var panel = h('section.npanel');
    panel.appendChild(h('div.npanel__title', { text: '排版' }));

    function slider(label, key, min, max, step, format) {
      var row = h(
        'div.nprop',
        null,
        h('span.nprop__key', { text: label }),
        h('input.ntypo', {
          type: 'range',
          min: String(min),
          max: String(max),
          step: String(step),
          value: String(prefs[key]),
          onInput: function (ev) {
            setTypo(key, Number(ev.target.value));
            var out = row.querySelector('.ntypo__val');
            if (out) out.textContent = format(Number(ev.target.value));
          }
        }),
        h('span.ntypo__val', { text: format(prefs[key]) })
      );
      return row;
    }

    panel.appendChild(
      slider('字号', 'size', 12, 20, 1, function (v) {
        return v + 'px';
      })
    );
    panel.appendChild(
      slider('行距', 'line', 1.4, 2.4, 0.02, function (v) {
        return v.toFixed(2);
      })
    );
    panel.appendChild(
      slider('行宽', 'width', 50, 120, 2, function (v) {
        return v + ' 字';
      })
    );
    panel.appendChild(
      h(
        'div.nprop',
        null,
        h('span.nprop__key', { text: '等宽' }),
        h('button.nbtn', {
          type: 'button',
          text: prefs.mono ? '正文用等宽' : '正文用系统字体',
          title: '写作时用等宽更整齐；阅读时用系统字体更舒服',
          onClick: function () {
            setTypo('mono', !typoPrefs().mono);
            renderSide();
          }
        })
      )
    );
    return panel;
  }

  function propRow(key, value, emptyText, onSave) {
    var valEl = h('div.nprop__val' + (value ? '' : '.nprop__val--empty'), {
      text: value || emptyText,
      title: '点一下就能改'
    });
    valEl.addEventListener('click', function () {
      if (valEl.dataset.editing) return;
      valEl.dataset.editing = '1';
      var input = h('input.oline__input', { type: 'text', value: value || '' });
      ui.clear(valEl);
      valEl.appendChild(input);
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
      var done = false;
      function finish(save) {
        if (done) return;
        done = true;
        if (save && input.value !== value) onSave(input.value.trim());
        else renderSide();
      }
      input.addEventListener('keydown', function (ev) {
        ev.stopPropagation();
        if (ev.key === 'Enter') {
          ev.preventDefault();
          finish(true);
        } else if (ev.key === 'Escape') {
          ev.preventDefault();
          finish(false);
        }
      });
      input.addEventListener('blur', function () {
        finish(true);
      });
    });
    return h('div.nprop', null, h('span.nprop__key', { text: key }), valEl);
  }

  function saveMeta(patch) {
    api
      .post('/notes/meta', { lib: state.lib, path: state.note.path, meta: patch })
      .then(function (note) {
        state.note = note;
        renderSide();
        toast('已保存', 'ok');
      })
      .catch(fail);
  }

  function listPanel(title, items, onPick) {
    var panel = h('section.npanel', null, h('div.npanel__title', { text: title }));
    if (!items.length) {
      panel.appendChild(h('div.npanel__hint', { text: '还没有。' }));
      return panel;
    }
    items.forEach(function (item) {
      panel.appendChild(
        h(
          'button.nitem',
          {
            type: 'button',
            title: item.snippet || item.path,
            onClick: function () {
              onPick(item);
            }
          },
          h('div.nitem__title', { text: item.title || item.path }),
          item.snippet ? h('div.nitem__sub', { text: item.snippet }) : null
        )
      );
    });
    return panel;
  }

  function linkPanel(title, links) {
    var panel = h('section.npanel', null, h('div.npanel__title', { text: title }));
    if (!links.length) {
      panel.appendChild(h('div.npanel__hint', { text: '还没有。' }));
      return panel;
    }
    links.forEach(function (link) {
      var label = link.kind === 'note' ? link.title || link.target : link.target;
      var kindText = link.kind === 'note' ? '' : link.kind === 'asset' ? ' · 附件' : ' · 找不到';
      panel.appendChild(
        h(
          'button.nitem',
          {
            type: 'button',
            title: link.target,
            onClick: function () {
              if (link.kind === 'note' && link.resolved) openNote(link.resolved, link.line);
              else toast(link.kind === 'asset' ? '这是附件，还没做附件视图' : '这篇还不存在', 'info');
            }
          },
          h('div.nitem__title', { text: label + kindText })
        )
      );
    });
    return panel;
  }

  function unresolvedPanel(targets) {
    var panel = h(
      'section.npanel',
      null,
      h('div.npanel__title', { text: '未解析链接 · ' + targets.length })
    );
    targets.slice(0, 20).forEach(function (target) {
      panel.appendChild(
        h(
          'div.nprop',
          null,
          h('span.nprop__val', { text: target, title: target }),
          h('button.nbtn', {
            type: 'button',
            text: '建',
            title: '按这个名字新建一篇',
            onClick: function () {
              createNote('', target);
            }
          })
        )
      );
    });
    return panel;
  }

  function loadHistory() {
    api
      .get('/notes/snapshots?' + q({ lib: state.lib, path: state.note.path }))
      .then(function (res) {
        var all = res.items || [];
        var items = all.filter(function (it) {
          return !it.named;
        });
        var named = all.filter(function (it) {
          return it.named;
        });
        var panel = h(
          'section.npanel',
          null,
          h('div.npanel__title', { text: '改动历史 · ' + items.length }),
          h('button.nbtn', {
            type: 'button',
            text: '存一版',
            title: '手动留一版（命名快照）：不参与撤销游标，也不会被自动清理',
            onClick: snapshotPrompt
          })
        );
        if (items.length < 2 && !named.length) {
          panel.appendChild(h('div.npanel__hint', { text: '改过之后这里会记下每一版。' }));
          el.side.appendChild(panel);
          return;
        }
        named.concat(items).slice(0, 14).forEach(function (item) {
          panel.appendChild(
            h(
              'div.nhist__row',
              null,
              h('span.nhist__at', { text: item.at.replace('T', ' ') }),
              item.named
                ? h('span.nhist__why', { text: '★ ' + (item.why || '命名') })
                : item.current
                  ? h('span.nhist__cur', { text: '当前' })
                  : null,
              !item.current
                ? h('button.nbtn', {
                    type: 'button',
                    text: '差异',
                    title: '看看这一版到当前改了什么',
                    onClick: function () {
                      showDiff(item);
                    }
                  })
                : null,
              !item.current
                ? h('button.nbtn', {
                    type: 'button',
                    text: '恢复',
                    title: '回到这一版（当前这版会被记进历史，随时能回来）',
                    onClick: function () {
                      ui.confirm('回到 ' + item.at.replace('T', ' ') + ' 那一版？', { okLabel: '恢复' }).then(
                        function (yes) {
                          if (yes) restoreVersion(item.name);
                        }
                      );
                    }
                  })
                : null
            )
          );
        });
        el.side.appendChild(panel);
      })
      .catch(function () {
        /* 历史拉不到不影响正文 */
      });
  }

  function snapshotPrompt() {
    var input = h('input.oline__input', { type: 'text', value: '', placeholder: '比如「定稿」「交给模型前」' });
    ui.modal({
      title: '存一版',
      size: 'sm',
      body: h('div', null, h('div.npanel__hint', { text: '给这一版起个名字，将来能从历史里认出来。' }), input),
      actions: [
        { label: '取消', kind: 'ghost', onClick: function (close) { close(); } },
        {
          label: '存下',
          kind: 'primary',
          onClick: function (close) {
            var name = input.value.trim();
            close();
            if (state.dirty) saveNow(true);
            api
              .post('/notes/snapshot', { lib: state.lib, path: state.note.path, name: name })
              .then(function () {
                toast('已存一版', 'ok');
                renderSide();
              })
              .catch(fail);
          }
        }
      ]
    });
    setTimeout(function () {
      input.focus();
    }, 0);
  }

  /** 版本差异：后端出统一格式的行，这里只上色。 */
  function showDiff(item) {
    api
      .get('/notes/diff?' + q({ lib: state.lib, path: state.note.path, name: item.name }))
      .then(function (res) {
        var body = h('div.ndiff');
        if (!res.lines || !res.lines.length) {
          body.appendChild(h('div.npanel__hint', { text: '和当前一模一样。' }));
        } else {
          res.lines.forEach(function (line) {
            // `---` / `+++` 是文件头，不是"删了一行、加了一行" —— 别跟着算进颜色里
            var header = line.indexOf('+++') === 0 || line.indexOf('---') === 0;
            var cls = '.ndiff__line';
            if (header || line.charAt(0) === '@') cls += '.ndiff__line--at';
            else if (line.charAt(0) === '+') cls += '.ndiff__line--add';
            else if (line.charAt(0) === '-') cls += '.ndiff__line--del';
            body.appendChild(h('div' + cls, { text: line || ' ' }));
          });
        }
        ui.modal({
          title: '差异：+' + res.added + ' 行 · −' + res.removed + ' 行',
          size: 'lg',
          body: body,
          actions: [{ label: '关闭', kind: 'ghost', onClick: function (close) { close(); } }]
        });
      })
      .catch(fail);
  }

  function restoreVersion(name) {
    if (state.dirty) saveNow(true);
    api
      .post('/notes/restore', { lib: state.lib, path: state.note.path, name: name })
      .then(function () {
        toast('已恢复到那一版', 'ok');
        var path = state.note.path;
        state.note = null;
        openNote(path);
      })
      .catch(fail);
  }

  function loadTags() {
    api
      .get('/notes/tags?' + q({ lib: state.lib }))
      .then(function (res) {
        var items = (res.items || []).slice(0, 36);
        if (!items.length) return;
        var panel = h('section.npanel', null, h('div.npanel__title', { text: '标签' }));
        var chips = h('div.nchips');
        items.forEach(function (row) {
          chips.appendChild(
            h(
              'button.nchip',
              {
                type: 'button',
                title: row.count + ' 篇',
                onClick: function () {
                  el.search.value = row.tag;
                  el.clear.hidden = false;
                  runSearch(row.tag);
                }
              },
              h('span', { text: '#' + row.tag }),
              h('span.nchip__count', { text: String(row.count) })
            )
          );
        });
        panel.appendChild(chips);
        el.side.appendChild(panel);
      })
      .catch(function () {
        /* 标签面板拉不到不影响正文 */
      });
  }

  /* ------------------------------------------------------------ 检索 */

  function runSearch(term) {
    api
      .get('/notes/search?' + q({ q: term, lib: state.lib, limit: 60 }))
      .then(function (res) {
        var items = res.items || [];
        ui.clear(el.tree);
        el.tree.appendChild(
          h(
            'div.ntree__head',
            null,
            h('span', { text: '命中 ' + items.length + ' 处' }),
            h('button.nbtn', {
              type: 'button',
              text: '清除',
              onClick: function () {
                el.search.value = '';
                el.clear.hidden = true;
                loadTree();
              }
            })
          )
        );
        if (!items.length) {
          el.tree.appendChild(h('div.npanel__hint', { text: '没有命中。' }));
          return;
        }
        items.forEach(function (hit) {
          el.tree.appendChild(
            h(
              'button.nsearch__item',
              {
                type: 'button',
                title: hit.path,
                onClick: function () {
                  openNote(hit.path, hit.line);
                }
              },
              h(
                'div.nsearch__title',
                null,
                h('span.ntree__label', { text: hit.title || hit.path }),
                h('span.nsearch__where', { text: hit.where || '' })
              ),
              h('div.nsearch__snippet', { html: highlight(hit.snippet || '', term) })
            )
          );
        });
      })
      .catch(fail);
  }

  function highlight(text, term) {
    var safe = esc(text);
    if (!term) return safe;
    var pattern = esc(term).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    try {
      return safe.replace(new RegExp(pattern, 'gi'), function (hit) {
        return '<span class="nsearch__hl">' + hit + '</span>';
      });
    } catch (err) {
      return safe;
    }
  }

  /* ------------------------------------------------------------ 弹层 */

  function renameTitle() {
    if (!isNote()) return;
    var current = state.note.title || state.note.path.replace(/\.md$/i, '');
    var input = h('input.oline__input', { type: 'text', value: current });
    ui.modal({
      title: '改标题',
      size: 'sm',
      body: h(
        'div',
        null,
        h('div.npanel__hint', { text: '标题就是文件名。引用它的地方会一起改（旧编辑器那条 alwaysUpdateLinks）。' }),
        input
      ),
      actions: [
        { label: '取消', kind: 'ghost', onClick: function (close) { close(); } },
        {
          label: '改名',
          kind: 'primary',
          onClick: function (close) {
            var next = input.value.trim();
            if (!next || next === current) {
              close();
              return;
            }
            close();
            var folder = state.note.path.split('/').slice(0, -1).join('/');
            var to = (folder ? folder + '/' : '') + next + '.md';
            if (state.dirty) saveNow(true);
            api
              .post('/notes/rename', { lib: state.lib, path: state.note.path, to: to })
              .then(function (res) {
                toast('改名完成，更新了 ' + (res.updated_notes || []).length + ' 篇引用', 'ok');
                state.note = res.note;
                renderMain();
                renderSide();
                loadTree();
              })
              .catch(fail);
          }
        }
      ]
    });
    setTimeout(function () {
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    }, 0);
  }

  /** 建一篇笔记：**不弹层**，建完直接在树里把标题变成输入框。

   * Trilium 就是这么做的（`Ctrl+O` 在其后插入、`Ctrl+P` 建子笔记，建完在树里改标题
   * —— `keyboard_actions.ts:126-192`）。弹层要多点两次、还挡住你要看的位置，
   * 而"新建"是这一页最高频的动作，高频动作不该有仪式感。
   * 名字留空也能建（标题就叫"未命名"，你马上就能改），所以 `Ctrl+O` 是**一步**动作。
   */
  function createNote(folder, title) {
    api
      .post('/notes/create', { lib: state.lib, folder: folder || '', title: title || '' })
      .then(function (note) {
        state.note = note;
        // 与打开一篇同一个默认：宽屏**分栏**（右边就是即时渲染的那一半）。
        // 新建完只给一块空白编辑区，等于把"边写边看"这个最该有的东西藏起来了。
        state.mode = note.kind === 'canvas' ? 'read' : window.innerWidth > 1180 ? 'split' : 'edit';
        state.selected = 0;
        state.dirty = false;
        state.savedAt = nowHM();
        folded = {};
        if (folder && folder !== '') dirFolded[folder] = false;
        renderMain();
        renderSide();
        return loadTree().then(function () {
          startInlineRename(note.path);   // 名字就地改，别让我再找它一次
        });
      })
      .catch(fail);
  }

  /** 把树里某个条目变成"正在改名"的输入框（Trilium: 树上 Enter）。 */
  function startInlineRename(path) {
    var node = el.tree.querySelector('.ntree__file[data-path="' + cssEscape(path) + '"]');
    if (!node) return;
    var label = node.querySelector('.ntree__label');
    if (!label) return;
    var current = label.textContent || '';
    var input = h('input.ntree__rename', {
      type: 'text',
      value: current,
      // 保持原来的缩进，别让它在树里跳一格
      style: { marginLeft: node.style.paddingLeft || '0' }
    });
    // **换掉整行，不要把 input 塞进 button 里** —— `<input>` 嵌在 `<button>` 内是非法结构，
    // 浏览器会给它"按钮内的输入"待遇：聚焦不稳、回车还会去点那个按钮（实测就是打不进字）。
    node.replaceWith(input);
    input.focus();
    input.setSelectionRange(0, input.value.length);
    var done = false;
    function finish(save) {
      if (done) return;
      done = true;
      var next = input.value.trim();
      if (!save || !next || next === current) {
        loadTree();
        return;
      }
      var folder = path.split('/').slice(0, -1).join('/');
      api
        .post('/notes/rename', {
          lib: state.lib,
          path: path,
          to: (folder ? folder + '/' : '') + next + '.md'
        })
        .then(function (res) {
          // 改的就是当前打开这篇 → 换成新的对象（**路径也变了**，别拿旧路径去比新旧对象）
          if (state.note && state.note.path === path) state.note = res.note;
          if (res.updated_notes && res.updated_notes.length) {
            toast('改名完成，更新了 ' + res.updated_notes.length + ' 篇引用', 'ok');
          }
          renderMain();
          renderSide();
          loadTree();
        })
        .catch(fail);
    }
    input.addEventListener('keydown', function (ev) {
      ev.stopPropagation();
      if (ev.key === 'Enter') {
        ev.preventDefault();
        finish(true);
      } else if (ev.key === 'Escape') {
        ev.preventDefault();
        finish(false);
      }
    });
    input.addEventListener('blur', function () {
      finish(true);
    });
  }

  function cssEscape(value) {
    return String(value).replace(/["\\]/g, '\\$&');
  }

  /* ------------------------------------------------------------ 启动 */

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
