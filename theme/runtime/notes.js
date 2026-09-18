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
    treebar: document.getElementById('notes-treebar'),
    main: document.getElementById('notes-main'),
    side: document.getElementById('notes-side'),
    ribbon: document.getElementById('notes-ribbon'),
    vault: document.getElementById('notes-vault'),
    collapse: document.getElementById('notes-collapse')
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
    savedAt: '',
    tabs: [],           // 打开过的笔记（标签页）—— 库大、目录深，没有它就得来回找
    side: 'open',       // open | closed
    panel: 'outline'    // 侧栏当前面板：outline | links | props | history
  };

  /* 图标一律内联画：不引图标库、不引 CDN（仓库铁律），描边粗细统一 1.6。 */
  function svgIcon(paths) {
    return (
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" ' +
      'stroke-linecap="round" stroke-linejoin="round">' + paths + '</svg>'
    );
  }

  var ICONS = {
    outline: svgIcon('<path d="M4 6h16M4 12h10M4 18h13"/>'),
    links: svgIcon('<path d="M9.5 14.5 14.5 9.5"/><path d="M11 6.5 12.6 5a4 4 0 0 1 5.7 5.7L16.7 12"/><path d="M13 17.5 11.4 19a4 4 0 0 1-5.7-5.7L7.3 12"/>'),
    props: svgIcon('<path d="M4 7h16M4 12h16M4 17h10"/>'),
    history: svgIcon('<path d="M12 7v5l3 2"/><path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1"/><path d="M3.5 4.5V10H9"/>'),
    tidy: svgIcon('<path d="M5 4v4M3 6h4"/><path d="M17 16v5M14.5 18.5h5"/><path d="M13 4.5 9.5 11h3L9 17.5"/>'),
    type: svgIcon('<path d="M5 19h6M11 19h8"/><path d="M12 4v15"/>'),
    side: svgIcon('<path d="M4 5h16v14H4z"/><path d="M15 5v14"/>')
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
    // 侧栏开着还是收着，是"我的习惯"而不是"这次的状态" —— 记在本机，下次照旧
    try {
      var savedSide = window.localStorage.getItem('qf.notes.side');
      if (savedSide === 'closed' || savedSide === 'open') state.side = savedSide;
      var savedPanel = window.localStorage.getItem('qf.notes.panel');
      if (savedPanel) state.panel = savedPanel;
    } catch (err) {
      /* 读不到就用默认 */
    }
    rootEl.setAttribute('data-side', state.side);
    applyTypography();
    renderRibbon();
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
        if (el.vault) el.vault.textContent = state.lib;
        // 启动时也得画一次主区 —— 不然中间是一片空白，看着像坏了
        // （空态那句"左边挑一篇，或者新建一篇"就是在这里出来的）
        renderMain();
        renderSide();
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
        ui.clear(el.treebar);
        el.treebar.appendChild(
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

  /** 每深一层加一条发丝竖线（Obsidian 的分层线）。
   * 只靠内边距留白，层级在深目录里会糊成一片 —— 有了线，眼睛才知道自己在第几层。 */
  function indentGuides(depth) {
    var box = h('span.ntree__indents');
    for (var i = 1; i < depth; i++) box.appendChild(h('span.ntree__indent'));
    return box;
  }

  function dirNode(dir, depth) {
    var box = h('div');
    var isFolded = !!dirFolded[dir.path];
    var node = h(
      'button.ntree__dir',
      {
        type: 'button',
        title: dir.path,
        style: { paddingLeft: '4px' },
        onClick: function () {
          dirFolded[dir.path] = !dirFolded[dir.path];
          loadTree();
        },
        onContextmenu: function (ev) {
          showMenu(ev, dirMenu(dir));
        }
      },
      indentGuides(depth),
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
        style: { paddingLeft: '4px' },
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
      indentGuides(depth),
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
    if (QF.canvas && QF.canvas.unmount) QF.canvas.unmount();   // 换篇之前先收掉旧画布（它挂着 window 上的监听）
    if (state.dirty) saveNow(true);
    if (window.innerWidth <= 1180) {
      el.side.classList.add('is-open');
    }
    api
      .get('/notes/note?' + q({ lib: state.lib, path: path }))
      .then(function (note) {
        state.note = note;
        addTab(note);
        state.selected = typeof line === 'number' ? line : -1;
        state.dirty = false;
        state.saving = false;
        state.savedAt = nowHM();
        folded = {};
        // 打开就准备写：宽屏分栏（边写边看），窄屏纯编辑。阅读要自己切 ——
        // 打开一篇**默认是阅读**：多数时候是回来看，不是接着写。
        // 读着读着想改，就在面包屑右边按「编辑」或「分栏」—— 一步的事。
        state.mode = 'read';
        renderMain();
        renderSide();
        loadTree();
      })
      .catch(fail);
  }

  /* ------------------------------------------------------------ 主区 */

  function renderMain() {
    ui.clear(el.main);
    el.main.appendChild(renderTabs());
    if (!isNote() && !(state.note && state.note.kind === 'canvas')) {
      el.main.appendChild(emptyState());
      return;
    }
    el.main.appendChild(renderBar());
    if (state.note.kind === 'canvas') {
      // 画布**自己管视口与保存**（它有自己的坐标系、工具条与攒批策略）——
      // 塞进编辑器那一套会两头打架：一个想管滚动、一个想管 transform。
      var host = h('div.note__pane.note__pane--canvas', { id: 'notes-canvas' });
      el.main.appendChild(host);
      el.main.appendChild(renderStatusBar());
      QF.canvas.mount(host, {
        lib: state.lib,
        path: state.note.path,
        onOpenNote: function (target) {
          // `file` 节点里存的是**相对库根**的路径（与树里的 path 同一套），直接打开。
          // 也容忍写成 `[[…]]` 或 `./x` 的老习惯。
          var rel = String(target || '')
            .replace(/^\[\[|\]\]$/g, '')
            .replace(/^\.\//, '')
            .trim();
          if (rel) openNote(rel);
        },
        onStatus: function (text) {
          state.canvasStatus = text;
          renderStatusOnly();
        },
        onDirty: function (flag) {
          state.dirty = flag;
          renderStatusOnly();
        }
      });
      return;
    }
    el.main.appendChild(renderBody());
    el.main.appendChild(renderStatusBar());
  }

  /* ------------------------------------------------------------ 标签页

   * 库大、目录四五层，没有"我开过哪几篇"就得来回找文件。
   * 只记路径，标题每次从树/笔记里取 —— 改名之后标签页不会留旧名字。
   */

  function addTab(note) {
    if (!note || !note.path) return;
    var found = state.tabs.filter(function (tab) {
      return tab.path === note.path;
    })[0];
    if (found) {
      found.title = note.title;
      return;
    }
    state.tabs.push({ path: note.path, title: note.title });
    if (state.tabs.length > 12) state.tabs.shift();   // 标签页不是收藏夹，多了就挤掉最旧的
  }

  function closeTab(path) {
    var index = -1;
    state.tabs.forEach(function (tab, i) {
      if (tab.path === path) index = i;
    });
    if (index < 0) return;
    state.tabs.splice(index, 1);
    if (state.note && state.note.path === path) {
      var next = state.tabs[index] || state.tabs[index - 1];
      state.note = null;
      if (next) openNote(next.path);
      else renderMain();
    } else {
      renderMain();
    }
  }

  function renderTabs() {
    var bar = h('div.ntabs');
    state.tabs.forEach(function (tab) {
      var active = state.note && state.note.path === tab.path;
      var node = h(
        'button.ntab' + (active ? '.is-on' : ''),
        {
          type: 'button',
          title: tab.path,
          onClick: function () {
            if (!active) openNote(tab.path);
          }
        },
        h('span.ntab__label', { text: tab.title || tab.path.split('/').pop() }),
        h('span.ntab__x', {
          text: '×',
          title: '关掉这个标签页（笔记没有删）',
          onClick: function (ev) {
            ev.stopPropagation();
            closeTab(tab.path);
          }
        })
      );
      bar.appendChild(node);
    });
    bar.appendChild(h('div.ntabs__sp'));
    bar.appendChild(
      h(
        'div.ntabs__tools',
        null,
        h('button.nicon.nicon--mini', {
          type: 'button',
          text: '＋',
          title: '新建笔记（⌘O 同级 / ⌘P 子级）',
          onClick: function () {
            createNote('', '');
          }
        }),
        h('button.nicon.nicon--mini' + (state.side === 'open' ? '.is-on' : ''), {
          type: 'button',
          html: ICONS.side,
          title: state.side === 'open' ? '收起右侧栏' : '展开右侧栏',
          onClick: toggleSide
        })
      )
    );
    return bar;
  }

  function toggleSide(to) {
    state.side = to || (state.side === 'open' ? 'closed' : 'open');
    rootEl.setAttribute('data-side', state.side);
    try {
      window.localStorage.setItem('qf.notes.side', state.side);
      window.localStorage.setItem('qf.notes.panel', state.panel);
    } catch (err) {
      /* 存不下就只在这次生效 */
    }
    renderMain();
    renderSide();
  }

  /** 当前这份前端产物的构建戳：从自己的 script 标签上取（打包脚本给每个产物带了内容哈希）。 */
  function buildStamp() {
    var tag = document.querySelector('script[src*="runtime/notes.js"]');
    var found = tag && (tag.getAttribute('src') || '').match(/v=([0-9a-f]{8})/);
    return found ? found[1] : '';
  }

  /** 底部状态栏：与 Obsidian 一样把"这篇有多少东西"摆在脚边，不占正文。 */
  function renderStatusBar() {
    var note = state.note;
    var bar = h('div.nstatus');
    bar.appendChild(h('span', { text: '反链 ' + ((note.backlinks || []).length || 0) }));
    bar.appendChild(h('span', { text: '出链 ' + ((note.outlinks || []).length || 0) }));
    if ((note.unresolved || []).length) {
      bar.appendChild(h('span', { text: '断链 ' + note.unresolved.length }));
    }
    bar.appendChild(h('div.nstatus__sp'));
    bar.appendChild(
      h('span.note__status' + (state.dirty ? '.note__status--dirty' : ''), {
        id: 'note-status',
        text: statusText()
      })
    );
    bar.appendChild(h('span', { text: note.path }));
    // 这版前端的构建戳（打包时每个产物带着内容哈希）—— 用来一眼核对
    // "我看到的是不是刚改的那份"，省得靠猜缓存。
    var hash = buildStamp();
    if (hash) bar.appendChild(h('span.notes__stamp', { text: hash, title: '前端构建戳（改 theme/ 会自动重建）' }));
    return bar;
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
    // 这一条是**面包屑**（Obsidian 的位置）：右边放视图切换与撤销，标题在正文里
    var bar = h('div.ncrumb');
    var path = h('div.ncrumb__path');
    note.path.split('/').forEach(function (part, index, all) {
      if (index) path.appendChild(h('span.ncrumb__sep', { text: '/' }));
      path.appendChild(
        h('span.ncrumb__seg' + (index === all.length - 1 ? '.ncrumb__seg--last' : ''), { text: part })
      );
    });
    bar.appendChild(path);
    bar.appendChild(h('div.ncrumb__actions'));

    bar.appendChild(
      h(
        'div.seg',
        null,
        // 画布没有"阅读/分栏/编辑/大纲"这回事（它就是一块平面），不给它摆这排按钮
        (state.note.kind === 'canvas' ? [] : [['read', '阅读'], ['split', '分栏'], ['edit', '编辑'], ['outline', '大纲']]).map(function (pair) {
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

    // 状态文字搬到底部状态栏去了（id 不变，renderStatusOnly 照旧能找到它）

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
    // 画布的状态由它自己报（卡片数 / 连线数 / 保存进度）—— "0 行 · 0 字"对它没有意义
    if (state.note && state.note.kind === 'canvas') {
      if (state.saving) return '保存中…';
      return state.canvasStatus || '画布';
    }
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
    // 编辑 / 分栏 / 大纲：标题压成一行，剩下的高度全给正文 —— 写的时候别跟标题抢地方
    var wrap = h('div.nbody');
    wrap.appendChild(
      h(
        'div.nbody__slim',
        null,
        h('h1.note__title', {
          text: state.note.title,
          title: '双击改标题（会连同文件名一起改，引用它的地方也会跟着改）',
          onDblclick: renameTitle
        })
      )
    );
    wrap.appendChild(state.mode === 'outline' ? renderOutline() : renderEditor(state.mode === 'split'));
    return wrap;
  }

  /* ------------------------------------------------------------ 阅读 */

  function renderRead() {
    var pane = h('div.note__pane');
    var inner = h('div.nread__inner');
    // 标题属于文档本身（Obsidian 就是这样）：它不该另占一条栏，也不该跟正文分离
    inner.appendChild(
      h('h1.note__title', {
        text: state.note.title,
        title: '双击改标题（会连同文件名一起改，引用它的地方也会跟着改）',
        onDblclick: renameTitle
      })
    );
    if ((state.note.tags || []).length) {
      var tagRow = h('div.nbody__tags');
      state.note.tags.forEach(function (tag) {
        tagRow.appendChild(
          h('span.ntag', {
            text: '#' + tag,
            title: '点一下 = 搜这个标签',
            onClick: function () {
              el.search.value = '#' + tag;
              el.clear.hidden = false;
              runSearch('#' + tag);
            }
          })
        );
      });
      inner.appendChild(tagRow);
    }
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
    decorateCallouts(inner);
    pane.appendChild(h('div.nread', null, inner));
    return pane;
  }

  /* ------------------------------------------------------------ 提示框（callout）

   * 源里写 `> [!NOTE] 回顾`，而渲染器只把它当普通引用 —— 于是这些块跟别的引用一样平，
   * 一个"这是结论/这是提醒"的信号就丢了。这里补一步**后处理**：认出标记、按类型上色、
   * 把标记本身从正文里去掉。
   * 放在 notes.js 而不是改 md.js：这是笔记页的读法，聊天那边不需要这套形状。
   */
  /* 提示框（callout）—— 形状、图标、配色都照 Obsidian 的默认主题来。
   *
   * 图标取自用户给的参考页（"Callouts - Obsidian Help"）里的内联 SVG：那是 **Lucide** 图标集，
   * 描边 2、24 格视口。上一版我自己画了十二个近似图形，形状不对、颜色也只有一个色系，
   * 于是 note / tip / warning 看起来全一样 —— callout 的价值恰恰在"一眼认出这是哪一类"。
   *
   * 色值是 Obsidian 默认主题的 `--callout-*`（每类一个 RGB），别名按官方文档合并
   * （summary/tldr → abstract，hint/important → tip，check/done → success，
   * help/faq → question，caution/attention → warning，fail/missing → failure，
   * error → danger，cite → quote）。
   */
  var CALLOUTS = {
    note: ['说明', '#086DDD', '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"></path><path d="m15 5 4 4"></path>'],
    abstract: ['摘要', '#00BFBC', '<rect x="8" y="2" width="8" height="4" rx="1" ry="1"></rect><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"></path><path d="M12 11h4"></path><path d="M12 16h4"></path><path d="M8 11h.01"></path><path d="M8 16h.01"></path>'],
    info: ['信息', '#086DDD', '<circle cx="12" cy="12" r="10"></circle><path d="M12 16v-4"></path><path d="M12 8h.01"></path>'],
    todo: ['待办', '#086DDD', '<circle cx="12" cy="12" r="10"></circle><path d="m9 12 2 2 4-4"></path>'],
    tip: ['提示', '#00BFBC', '<path d="M12 3q1 4 4 6.5t3 5.5a1 1 0 0 1-14 0 5 5 0 0 1 1-3 1 1 0 0 0 5 0c0-2-1.5-3-1.5-5q0-2 2.5-4"></path>'],
    success: ['完成', '#08B94E', '<path d="M20 6 9 17l-5-5"></path>'],
    question: ['问题', '#EC7500', '<circle cx="12" cy="12" r="10"></circle><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"></path><path d="M12 17h.01"></path>'],
    warning: ['注意', '#E0AC00', '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"></path><path d="M12 9v4"></path><path d="M12 17h.01"></path>'],
    failure: ['失败', '#E93147', '<path d="M18 6 6 18"></path><path d="m6 6 12 12"></path>'],
    danger: ['危险', '#E93147', '<path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"></path>'],
    bug: ['缺陷', '#E93147', '<path d="M12 20v-9"></path><path d="M14 7a4 4 0 0 1 4 4v3a6 6 0 0 1-12 0v-3a4 4 0 0 1 4-4z"></path><path d="M14.12 3.88 16 2"></path><path d="M21 21a4 4 0 0 0-3.81-4"></path><path d="M21 5a4 4 0 0 1-3.55 3.97"></path><path d="M22 13h-4"></path><path d="M3 21a4 4 0 0 1 3.81-4"></path><path d="M3 5a4 4 0 0 0 3.55 3.97"></path><path d="M6 13H2"></path><path d="m8 2 1.88 1.88"></path><path d="M9 7.13V6a3 3 0 1 1 6 0v1.13"></path>'],
    example: ['例', '#7852EE', '<path d="M3 5h.01"></path><path d="M3 12h.01"></path><path d="M3 19h.01"></path><path d="M8 5h13"></path><path d="M8 12h13"></path><path d="M8 19h13"></path>'],
    quote: ['引用', '#9E9E9E', '<path d="M16 3a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2 1 1 0 0 1 1 1v1a2 2 0 0 1-2 2 1 1 0 0 0-1 1v2a1 1 0 0 0 1 1 6 6 0 0 0 6-6V5a2 2 0 0 0-2-2z"></path><path d="M5 3a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2 1 1 0 0 1 1 1v1a2 2 0 0 1-2 2 1 1 0 0 0-1 1v2a1 1 0 0 0 1 1 6 6 0 0 0 6-6V5a2 2 0 0 0-2-2z"></path>'],
  };

  //: Obsidian 官方文档里的别名 → 归到主类型（`[!summary]` 与 `[!abstract]` 是同一个）
  var CALLOUT_ALIAS = {
    summary: 'abstract', tldr: 'abstract',
    hint: 'tip', important: 'tip',
    check: 'success', done: 'success',
    help: 'question', faq: 'question',
    caution: 'warning', attention: 'warning',
    fail: 'failure', missing: 'failure',
    error: 'danger', cite: 'quote'
  };

  /** callout 的图标：与参考页同一套参数（描边 2、24 格、圆头圆角）。 */
  function calloutIcon(inner) {
    return (
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round">' + inner + '</svg>'
    );
  }

  function decorateCallouts(root) {
    if (!root || !root.querySelectorAll) return;
    var quotes = root.querySelectorAll('blockquote');
    Array.prototype.forEach.call(quotes, function (quote) {
      // 只看**第一个子节点**：渲染后正文里的换行是 <br> 而不是 \\n，
      // 拿整个 textContent 去匹配会把整段正文都当成标题（实测就是这么串成一坨的）
      var first = quote.firstChild;
      var head = (first && (first.textContent !== undefined ? first.textContent : first.nodeValue)) || '';
      var found = head.match(/\[!([A-Za-z]+)\]([-+])?[ \t]*(.*)/);
      if (!found) return;
      var raw = found[1].toLowerCase();
      var kind = CALLOUTS[raw] ? raw : CALLOUT_ALIAS[raw] || 'note';
      var spec = CALLOUTS[kind];
      // 标题里可能缠着源文件里的 `%%` 注释标记，去掉；实在取不到就用类型名
      var title = (found[3] || '').replace(/[%\\]+/g, '').trim().slice(0, 60) || spec[0];
      var fold = found[2] || '';

      // 把标记本身从正文里摘掉（它不该作为一个字出现在读物里）
      var walker = document.createTreeWalker(quote, NodeFilter.SHOW_TEXT, null);
      var node = walker.nextNode();
      while (node && (node.nodeValue || '').indexOf('[!') < 0) node = walker.nextNode();
      if (node) {
        // 连同**那一行的标题**一起去掉（不是只去标记）：
        // Obsidian 把 `> [!tip] 某标题` 里的"某标题"当作标题展示，正文里就不再重复它。
        // 只管到第一个换行/段落边界为止 —— 正文在后面的节点里，不动它。
        node.nodeValue = node.nodeValue.replace(/^[%\\\s]*\[![A-Za-z]+\][-+]?[ \t]*[^\n]*/, '');
        node.nodeValue = node.nodeValue.replace(/^[%\\]+/, '');
      }

      quote.classList.add('callout', 'callout--' + kind);
      if (fold) {
        // `[!note]-` 默认收起、`[!note]+` 默认展开 —— 点标题切换
        quote.classList.add('callout--foldable');
        if (fold === '-') quote.classList.add('is-folded');
        head.setAttribute('role', 'button');
        head.setAttribute('tabindex', '0');
        head.addEventListener('click', function () {
          quote.classList.toggle('is-folded');
        });
      }
      var head = h('div.callout__title');
      head.appendChild(h('span.callout__icon', { html: calloutIcon(spec[2]) }));
      head.appendChild(h('span', { text: title }));

      // 把正文收进 `.callout__content`（与 Obsidian 同一层结构）：
      // 一是折叠时"只藏正文、留住标题"才有东西可藏，二是标题行与正文的间距好控。
      var content = h('div.callout__content');
      while (quote.firstChild) content.appendChild(quote.firstChild);
      // 去掉正文首尾的 <br>：源里「> [!tip] 标题」后面那行空引用会渲染成一个 <br>，
      // 于是标题与正文之间凭空多出一整行空档（实测就是这个「距离太宽」）。
      // 只动首尾，正文内部用来分段的 <br> 留着。
      while (content.firstChild && content.firstChild.nodeName === 'BR') content.removeChild(content.firstChild);
      while (content.lastChild && content.lastChild.nodeName === 'BR') content.removeChild(content.lastChild);
      quote.appendChild(head);
      quote.appendChild(content);
    });
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

  /* 工具栏一律用图标。
   *
   * 上一版是文字（"粗 / 斜 / 码 / 标题 / 列表…"）：十三个词横排，眼睛要先读一遍才知道点哪个，
   * 而它抢的正是正文最上面那一行。图标 + 悬停提示才是这类常驻工具的形状 ——
   * 描边粗细与左栏图标栏一致（1.6），不引图标库。
   */
  var FORMAT_ICONS = {
    bold: '<path d="M7 4h6.5a3.5 3.5 0 0 1 0 7H7z"/><path d="M7 11h7.5a4 4 0 0 1 0 8H7z"/>',
    italic: '<path d="M14 4h-6M16 20H8M14.5 4 9.5 20"/>',
    code: '<path d="M9 8 5 12l4 4M15 8l4 4-4 4"/>',
    head: '<path d="M5 5v14M13 5v14M5 12h8"/><path d="M17 9v10M17 9l3-2v12"/>',
    list: '<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1"/><circle cx="4.5" cy="12" r="1"/><circle cx="4.5" cy="18" r="1"/>',
    ol: '<path d="M10 6h10M10 12h10M10 18h10"/><path d="M4 5h1.5v4M4 15.5c0-.8.7-1.5 1.5-1.5s1.5.7 1.5 1.5S4 18 4 18h3"/>',
    quote: '<path d="M6 7v10M11 7v10"/><path d="M15 9h4M15 15h4"/>',
    math: '<path d="M7 6h9M7 6l4 6-4 6h9"/><path d="M17 10v7"/>',
    link: '<path d="M9.5 14.5 14.5 9.5"/><path d="M11 6.5 12.6 5a4 4 0 0 1 5.7 5.7L16.7 12"/><path d="M13 17.5 11.4 19a4 4 0 0 1-5.7-5.7L7.3 12"/>',
    wiki: '<path d="M6 8 9 5.5 12 8l3-2.5L18 8"/><path d="M9 19l3-9 3 9"/><path d="M11 19h2"/>',
    hr: '<path d="M4 12h16"/>',
    table: '<path d="M4 6h16v12H4z"/><path d="M4 10h16M4 14h16M10 6v12"/>',
    indent: '<path d="M4 6h16M10 12h10M10 18h10"/><path d="M4 9.5 6.5 12 4 14.5"/>'
  };

  function renderPad() {
    var pad = h('div.nedit__pad');
    var tools = [
      ['bold', '加粗（已加粗则取消）', function () { wrapSel('**', '**', '粗体'); }],
      ['italic', '斜体', function () { wrapSel('*', '*', '斜体'); }],
      ['code', '行内代码', function () { wrapSel('`', '`', 'code'); }],
      ['head', '标题（加在行首）', function () { linePrefix('## '); }],
      ['list', '无序列表', function () { linePrefix('- '); }],
      ['ol', '有序列表', function () { linePrefix('1. '); }],
      ['quote', '引用', function () { linePrefix('> '); }],
      ['math', '公式块', function () { wrapSel('$$\\n', '\\n$$', 'x = y'); }],
      ['link', '链接', function () { wrapSel('[', '](https://)', '说明'); }],
      ['wiki', '双链到另一篇笔记', function () { wrapSel('[[', ']]', '笔记名'); }],
      ['hr', '分隔线', function () { linePrefix('---'); }],
      ['table', '插入表格', function () { insertText('| 列 | 列 |\\n| --- | --- |\\n|  |  |\\n'); }],
      ['indent', '缩进一级（Tab）', function () { linePrefix(indentUnit()); }]
    ];
    tools.forEach(function (tool, i) {
      if (i === 3 || i === 7) pad.appendChild(h('span.nedit__sep'));
      pad.appendChild(
        h('button.nedit__btn', {
          type: 'button',
          html: svgIcon(FORMAT_ICONS[tool[0]]),
          title: tool[1],
          'aria-label': tool[1],
          onClick: tool[2]
        })
      );
    });
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
      decorateCallouts(target);
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

  /* ------------------------------------------------------------ 侧栏

   * **一次只显示一个面板**（与 Obsidian 的右侧栏一致）。上一版把属性、排版、大纲、
   * 反链、出链、断链、历史、标签八块堆成一长条 —— 写东西时没人会滚那么远，
   * 真正要看的永远是"当前这一个"。面板由左边的图标栏切换，也可以从顶部这排切。
   */

  var PANELS = [
    ['outline', '大纲'],
    ['links', '连接'],
    ['props', '属性'],
    ['history', '历史'],
    ['tags', '标签'],
    ['tidy', '整理']
  ];

  var sideTargetEl = null;

  /** 面板里那些"稍后才异步填进来"的东西（历史、标签）往哪儿塞。 */
  function sideTarget() {
    return sideTargetEl || el.side;
  }

  function renderSide() {
    ui.clear(el.side);
    renderRibbon();
    if (!state.note) {
      // 没打开笔记时也把它填满：一条空栏比一句说明更让人以为坏了
      el.side.appendChild(
        h(
          'div.nside__scroll',
          null,
          h(
            'section.npanel',
            null,
            h('div.npanel__title', { text: '还没有打开笔记' }),
            h('div.npanel__hint', { text: '左边挑一篇（或按 ＋ 新建），这里会出现它的大纲、反链、属性与改动历史。' })
          )
        )
      );
      return;
    }

    var head = h('div.nside__head');
    var tabs = h('div.nside__tabs');
    PANELS.forEach(function (pair) {
      tabs.appendChild(
        h('button.nside__tab' + (state.panel === pair[0] ? '.is-on' : ''), {
          type: 'button',
          text: pair[1],
          onClick: function () {
            state.panel = pair[0];
            renderSide();
          }
        })
      );
    });
    head.appendChild(tabs);
    head.appendChild(
      h('button.nicon.nicon--mini', {
        type: 'button',
        html: ICONS.side,
        title: '收起右侧栏',
        onClick: function () {
          toggleSide('closed');
        }
      })
    );
    el.side.appendChild(head);

    var scroll = h('div.nside__scroll');
    el.side.appendChild(scroll);
    sideTargetEl = scroll;
    if (state.panel === 'props') sideProps(scroll);
    else if (state.panel === 'links') sideLinks(scroll);
    else if (state.panel === 'history') sideHistory(scroll);
    else if (state.panel === 'tags') sideTags(scroll);
    else if (state.panel === 'tidy') sideTidy(scroll);
    else sideOutline(scroll);
    // 不把它置空：历史和标签是**稍后**才填进来的，置空之后它们会挂到侧栏根上、
    // 跑到滚动区外面去。下次 renderSide 会重新赋值并清空，多挂一次也不会留下东西。
  }

  function sideOutline(target) {
    var heads = (state.note.outline || []).filter(function (row) {
      return row.kind === 'heading';
    });
    var panel = h('section.npanel', null, h('div.npanel__title', { text: '大纲 · ' + heads.length + ' 个标题' }));
    if (!heads.length) {
      panel.appendChild(h('div.npanel__hint', { text: '这篇还没有标题。写了 `# 标题` 这里就会出现。' }));
    }
    heads.forEach(function (row) {
      panel.appendChild(
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
    target.appendChild(panel);
  }

  function sideLinks(target) {
    var note = state.note;
    target.appendChild(
      listPanel('反链 · 谁引用了我', note.backlinks || [], function (item) {
        openNote(item.path, item.line);
      })
    );
    target.appendChild(linkPanel('出链 · 我引用了谁', note.outlinks || []));
    if ((note.unresolved || []).length) target.appendChild(unresolvedPanel(note.unresolved));
  }

  function sideProps(target) {
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
    // Trilium 的内置属性里用得上的那几个（见 docs/笔记模块设计.md 的词汇表）
    props.appendChild(
      propRow('图标', String((note.meta && note.meta.icon) || ''), '比如 bxs-flask 或 📐', function (v) {
        saveMeta({ icon: v });
      })
    );
    props.appendChild(
      propRow('颜色', String((note.meta && note.meta.color) || ''), '比如 #2DD4BF', function (v) {
        saveMeta({ color: v });
      })
    );
    if (note.generated_header) {
      props.appendChild(
        h('div.npanel__hint', { text: '这个头是导入时补的（带 qf_generated 标记，可批量回退）' })
      );
    }
    target.appendChild(props);
    target.appendChild(typographyPanel());
  }

  function sideHistory(target) {
    loadHistory();
  }

  function sideTags(target) {
    loadTags();
  }

  /* ------------------------------------------------------------ 整理（模型建议标签与双链）

   * 为什么做成「建议、逐条接受」而不是自动写：四个库一千多篇里多数没有元数据，
   * 全自动加没人敢信，一条条手加不现实 —— 中间那条路才走得通。
   * 建议**只在内存里**，点了「接受」才落盘（落盘前照例留快照，能撤销）。
   */
  var tidy = { candidates: null, picked: {}, items: null, busy: false, error: '' };

  function sideTidy(target) {
    var panel = h('section.npanel', null, h('div.npanel__title', { text: '整理 · 模型建议标签与双链' }));
    panel.appendChild(
      h('div.npanel__hint', {
        text: '先把「没有标签也没有链接」的找出来；模型只出建议，你点接受它才写进文件（写前留快照，可撤销）。'
      })
    );
    if (tidy.error) panel.appendChild(h('div.npanel__warn', { text: tidy.error }));

    panel.appendChild(
      h('button.nbtn' + (tidy.candidates ? '' : '.nbtn--primary'), {
        type: 'button',
        text: tidy.candidates ? '重新找一遍' : '找出最该整理的几篇',
        disabled: tidy.busy ? 'disabled' : null,
        onClick: function () {
          tidy.error = '';
          tidy.busy = true;
          tidy.items = null;
          renderSide();
          api
            .get('/notes/suggest/candidates?' + q({ lib: state.lib, limit: 8 }))
            .then(function (res) {
              tidy.candidates = res.items || [];
              tidy.picked = {};
              tidy.candidates.slice(0, 6).forEach(function (row) {
                tidy.picked[row.path] = true;
              });
              tidy.busy = false;
              renderSide();
            })
            .catch(function (err) {
              tidy.busy = false;
              tidy.error = (err && err.message) || '取清单失败';
              renderSide();
            });
        }
      })
    );

    if (tidy.candidates) {
      var picked = tidy.candidates.filter(function (row) {
        return tidy.picked[row.path];
      }).length;
      tidy.candidates.forEach(function (row) {
        panel.appendChild(
          h(
            'label.npick',
            null,
            h('input', {
              type: 'checkbox',
              checked: tidy.picked[row.path] ? 'checked' : null,
              onChange: function (ev) {
                tidy.picked[row.path] = ev.target.checked;
              }
            }),
            h('span.npick__title', { text: row.title || row.path }),
            h('span.npick__meta', {
              text: (row.tags.length ? '' : '无标签') + (row.tags.length || row.links ? '' : ' · ') + (row.links ? '' : '无链接')
            })
          )
        );
      });
      panel.appendChild(
        h('button.nbtn.nbtn--primary', {
          type: 'button',
          text: tidy.busy ? '正在问模型…' : '让模型建议（' + picked + ' 篇）',
          disabled: tidy.busy || !picked ? 'disabled' : null,
          onClick: runTidy
        })
      );
    }

    if (tidy.items) panel.appendChild(tidyReview());
    target.appendChild(panel);
  }

  function runTidy() {
    var paths = Object.keys(tidy.picked).filter(function (path) {
      return tidy.picked[path];
    });
    if (!paths.length) return;
    tidy.busy = true;
    tidy.error = '';
    tidy.items = null;
    renderSide();
    api
      .post('/notes/suggest', { lib: state.lib, paths: paths })
      .then(function (res) {
        tidy.busy = false;
        tidy.items = res.items || [];
        tidy.items.forEach(function (row) {
          row.__tags = (row.tags || []).slice();
          row.__links = (row.links || []).slice();
        });
        if (res.dropped) {
          // 模型编标题是常事：丢掉了要说出来，别让它悄悄消失
          toast('模型写了 ' + res.dropped + ' 个库里没有的标题，已经丢掉', 'ok');
        }
        if ((res.failed || []).length) {
          tidy.error = (res.failed[0].error || '有笔记没问出来') + '（' + res.failed.length + ' 篇）';
        }
        renderSide();
      })
      .catch(function (err) {
        tidy.busy = false;
        tidy.error = (err && err.message) || '问模型失败';
        renderSide();
      });
  }

  /** 逐条复核：默认全勾 —— 人要的是「删掉不对的」，不是「挑出对的」。 */
  function tidyReview() {
    var box = h('div.ntidy');
    var count = 0;
    tidy.items.forEach(function (row, index) {
      if (!row.__tags.length && !row.__links.length) return;
      var card = h('div.ntidy__card', null, h('div.ntidy__who', { text: row.title || row.path }));
      row.__tags.forEach(function (tag, tagIndex) {
        count += 1;
        card.appendChild(
          h(
            'label.ntidy__row',
            null,
            h('input', {
              type: 'checkbox',
              checked: 'checked',
              dataset: { kind: 'tag', row: String(index), index: String(tagIndex) }
            }),
            h('span.ntag', { text: '#' + tag })
          )
        );
      });
      row.__links.forEach(function (link, linkIndex) {
        count += 1;
        card.appendChild(
          h(
            'label.ntidy__row',
            null,
            h('input', {
              type: 'checkbox',
              checked: 'checked',
              dataset: { kind: 'link', row: String(index), index: String(linkIndex) }
            }),
            h('span.ntidy__link', { text: '[[' + link.target + ']]' }),
            h('span.ntidy__why', { text: link.why || '' })
          )
        );
      });
      box.appendChild(card);
    });
    if (!count) {
      box.appendChild(h('div.npanel__hint', { text: '模型这几篇都说不出什么 —— 「暂时不用整理」也是答案。' }));
      return box;
    }
    box.appendChild(
      h('button.nbtn.nbtn--primary', {
        type: 'button',
        text: '接受选中的（写进文件，可撤销）',
        onClick: acceptTidy
      })
    );
    return box;
  }

  function acceptTidy() {
    var picked = [];
    var boxes = el.side.querySelectorAll('.ntidy input[type=checkbox]');
    Array.prototype.forEach.call(boxes, function (node) {
      if (!node.checked) return;
      var row = tidy.items[Number(node.dataset.row)];
      if (!row) return;
      var index = Number(node.dataset.index);
      var bucket = picked.filter(function (item) {
        return item.path === row.path;
      })[0];
      if (!bucket) {
        bucket = { path: row.path, tags: [], links: [] };
        picked.push(bucket);
      }
      if (node.dataset.kind === 'tag') bucket.tags.push(row.__tags[index]);
      else bucket.links.push(row.__links[index]);
    });
    if (!picked.length) {
      toast('一条都没选', 'bad');
      return;
    }
    api
      .post('/notes/suggest/apply', { lib: state.lib, items: picked })
      .then(function (res) {
        toast('补了 ' + (res.applied || []).length + ' 篇', 'ok');
        tidy.items = null;
        tidy.candidates = null;
        var current = state.note && state.note.path;
        renderSide();
        loadTree();
        if (current) {
          state.note = null;
          openNote(current);
        }
      })
      .catch(fail);
  }

  /** 图标栏（最左边那一条）：点一个切到那个面板，再点一下收起侧栏。 */
  function renderRibbon() {
    ui.clear(el.ribbon);
    [
      ['outline', 'outline', '大纲'],
      ['links', 'links', '连接 · 反链与出链'],
      ['props', 'props', '属性与排版'],
      ['history', 'history', '改动历史与版本'],
      ['tags', 'props', '标签'],
      ['tidy', 'tidy', '整理：让模型建议标签与双链（逐条接受）']
    ].forEach(function (row) {
      var on = state.side === 'open' && state.panel === row[0];
      el.ribbon.appendChild(
        h('button.nribbon__btn' + (on ? '.is-on' : ''), {
          type: 'button',
          html: ICONS[row[1]],
          title: row[2],
          onClick: function () {
            if (on) {
              toggleSide('closed');
              return;
            }
            state.panel = row[0];
            if (state.side !== 'open') toggleSide('open');
            else renderSide();
          }
        })
      );
    });
    el.ribbon.appendChild(h('div.nribbon__sp'));
    el.ribbon.appendChild(
      h('button.nribbon__btn', {
        type: 'button',
        html: ICONS.side,
        title: state.side === 'open' ? '收起右侧栏' : '展开右侧栏',
        onClick: function () {
          toggleSide();
        }
      })
    );
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

  //: 行宽单位是"字"（ch）。默认给到 92 —— 上一版 72 太窄，长句一眼扫不完要换行两次。
  var TYPO_DEFAULTS = { size: 15, line: 1.72, width: 92, mono: false };

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
      slider('行宽', 'width', 60, 150, 2, function (v) {
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
          sideTarget().appendChild(panel);
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
        sideTarget().appendChild(panel);
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
        sideTarget().appendChild(panel);
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
        // 新建的是一张白纸，没什么可读 —— 直接给编辑态，省一次切换
        state.mode = note.kind === 'canvas' ? 'read' : 'edit';
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
