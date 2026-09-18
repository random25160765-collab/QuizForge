/* 笔记：幕布（大纲）与 Obsidian（双链 / 反链 / 文件恢复）的融合。
 *
 * 这一页的全部意思就是**同一份 Markdown 两种渲染**：
 *   文档态 —— 读结构（标题、公式、列表、双链）；
 *   大纲态 —— 逐行改（缩进即子级、折叠、回车同级、Tab 缩进、Alt+↑↓ 挪块）。
 * 两者背后是同一个文件、同一套行号：在大纲里改一行，切回文档态看到的就是那一段。
 *
 * 编辑一律走 `/api/notes/line`（行级），不整篇覆盖 —— 行级才有"撤销一处改动"这种粒度，
 * 中断时也不会把整篇写坏。每次写之前后端都会先存一份快照（见 api/app/notelib.py）。
 *
 * 与站点其它页面一致：颜色从 CSS 变量走、顶栏交给 shell.js、只挂自己的 DOM。
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
    search: document.getElementById('notes-search'),
    tree: document.getElementById('notes-tree'),
    main: document.getElementById('notes-main'),
    side: document.getElementById('notes-side')
  };

  var state = {
    lib: '',
    libs: [],
    note: null,        // 当前打开的笔记（后端返回的整包：正文 + 大纲 + 出链 + 反链）
    mode: 'doc',       // doc | outline
    selected: -1,      // 大纲里选中的行号
    editing: false,    // 正在行内编辑（此时键盘交给输入框）
    sideOpen: false
  };
  var folded = {};     // 大纲里折起来的行号
  var dirFolded = {};  // 树上折起来的目录
  var treeCache = null; // 最近一次拿到的树：双链跳转要在它里面找目标

  function toast(msg, kind) {
    if (ui && ui.toast) ui.toast(msg, kind || 'info');
  }

  function fail(err) {
    toast((err && err.message) || '出错了', 'bad');
  }

  // 接口路径**不含 `/api` 前缀**：api.js 自己拼 BASE（`fetch(BASE + path)`）。
  // 这里写成 `/api/notes/...` 会变成 `/api/api/notes/...` → 404，
  // 而调用方的 .catch 会把它收成一条 toast —— 页面不报错、但一直空着（实测踩过）。
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

  /* ------------------------------------------------------------ 起手 */

  function boot() {
    // 顶栏（导航 / 主题 / 设置）统一由 shell.js 接线；包在 try 里，
    // 外壳的接线一旦早期抛错，别连累这一页也跟着不动
    try {
      QF.shell.mount({});
    } catch (err) {
      if (window.console) console.warn('顶栏接线失败', err);
    }
    bind();
    loadLibs();
  }

  function bind() {
    el.lib.addEventListener('change', function () {
      state.lib = el.lib.value;
      state.note = null;
      folded = {};
      dirFolded = {};
      el.search.value = '';
      renderMain();
      renderSide();
      loadTree();
    });

    var timer = null;
    el.search.addEventListener('input', function () {
      if (timer) clearTimeout(timer);
      var term = el.search.value.trim();
      timer = setTimeout(function () {
        if (term) runSearch(term);
        else loadTree();
      }, 220);
    });

    document.addEventListener('keydown', onKey);
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
            ui.h('div.empty', null, ui.h('div.empty__text', { text: '还没有笔记库。' }))
          );
          return;
        }
        state.libs.forEach(function (item) {
          el.lib.appendChild(
            ui.h('option', { value: item.name, text: item.name + '（' + item.notes + '）' })
          );
        });
        state.lib = state.libs[0].name;
        el.lib.value = state.lib;
        loadTree();
      })
      .catch(fail);
  }

  function loadTree() {
    if (!state.lib) return;
    api
      .get('/notes/tree?' + q({ lib: state.lib }))
      .then(function (tree) {
        treeCache = tree;
        ui.clear(el.tree);
        el.tree.appendChild(treeHead(tree));
        (tree.dirs || []).forEach(function (dir) {
          el.tree.appendChild(dirNode(dir, 1));
        });
        (tree.files || []).forEach(function (file) {
          el.tree.appendChild(fileNode(file, 1));
        });
      })
      .catch(fail);
  }

  function treeHead(tree) {
    return ui.h(
      'div.ntree__head',
      null,
      ui.h('span.ntree__count', { text: (tree.count || 0) + ' 条目' }),
      ui.h(
        'button.btn.btn--ghost',
        {
          type: 'button',
          text: '新建',
          title: '新建一篇笔记',
          onClick: function () {
            newNotePrompt('');
          }
        }
      )
    );
  }

  function dirNode(dir, depth) {
    var box = ui.h('div');
    var isFolded = !!dirFolded[dir.path];
    box.appendChild(
      ui.h(
        'button.ntree__dir',
        {
          type: 'button',
          style: { paddingLeft: 6 + depth * 10 + 'px' },
          onClick: function () {
            dirFolded[dir.path] = !dirFolded[dir.path];
            loadTree();
          }
        },
        ui.h('span.ntree__caret', { text: isFolded ? '▸' : '▾' }),
        ui.h('span.ntree__label', { text: dir.name }),
        ui.h('span.ntree__count', { text: String(dir.count || 0) })
      )
    );
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
    var isCanvas = file.kind === 'canvas';
    return ui.h(
      'button.ntree__file' + (state.note && state.note.path === file.path ? '.is-on' : ''),
      {
        type: 'button',
        title: file.path,
        style: { paddingLeft: 6 + depth * 10 + 'px' },
        onClick: function () {
          openNote(file.path);
        }
      },
      ui.h('span.ntree__caret', { text: isCanvas ? '◇' : '' }),
      ui.h('span.ntree__label', { text: file.title || file.name }),
      file.tags && file.tags.length ? ui.h('span.ntree__tagdot', { text: '#' }) : null
    );
  }

  /* ------------------------------------------------------------ 打开笔记 */

  function openNote(path, line) {
    if (!path) return;
    // 宽屏时右栏一直在，窄屏才需要"打开抽屉"
    if (window.innerWidth <= 1180) {
      state.sideOpen = true;
      el.side.classList.add('is-open');
    }
    api
      .get('/notes/note?' + q({ lib: state.lib, path: path }))
      .then(function (note) {
        state.note = note;
        state.selected = typeof line === 'number' ? line : -1;
        folded = {};
        renderMain();
        renderSide();
        loadTree(); // 让树里那一行高亮
      })
      .catch(fail);
  }

  function renderMain() {
    ui.clear(el.main);
    if (!state.note) {
      el.main.appendChild(
        ui.h(
          'div.empty',
          null,
          ui.h('div.empty__title', { text: '选一篇笔记' }),
          ui.h('div.empty__text', { text: '左边挑一篇，或者在上面检索。' })
        )
      );
      return;
    }
    var note = state.note;
    var head = ui.h('header.note__head');
    head.appendChild(
      ui.h('h1.note__title', {
        text: note.title,
        title: '双击改标题',
        onDblclick: renamePrompt
      })
    );
    head.appendChild(
      ui.h(
        'div.seg',
        null,
        ui.h(
          'button.seg__item' + (state.mode === 'doc' ? '.is-on' : ''),
          {
            type: 'button',
            text: '文档',
            onClick: function () {
              state.mode = 'doc';
              renderMain();
            }
          }
        ),
        ui.h(
          'button.seg__item' + (state.mode === 'outline' ? '.is-on' : ''),
          {
            type: 'button',
            text: '大纲',
            onClick: function () {
              state.mode = 'outline';
              renderMain();
            }
          }
        )
      )
    );
    var tools = ui.h('div.note__tools');
    if (note.can_undo) {
      tools.appendChild(
        ui.h('button.btn.btn--ghost', {
          type: 'button',
          text: '撤销上一次改动',
          title: '文件恢复：撤回这篇笔记最近一次改动（撤销本身也可撤销）',
          onClick: undoNote
        })
      );
    }
    tools.appendChild(
      ui.h('button.btn.btn--ghost', { type: 'button', text: '改名', onClick: renamePrompt })
    );
    tools.appendChild(
      ui.h('span.note__meta', {
        text: note.kind === 'canvas' ? '画布' : note.lines + ' 行'
      })
    );
    head.appendChild(tools);
    el.main.appendChild(head);

    var body = ui.h('div.note__body');
    if (note.kind === 'canvas') {
      body.appendChild(
        ui.h(
          'div.empty',
          null,
          ui.h('div.empty__title', { text: note.title }),
          ui.h('div.empty__text', { text: note.message || '画布的渲染在下一步做。' })
        )
      );
    } else if (state.mode === 'doc') {
      body.appendChild(renderDoc());
    } else {
      body.appendChild(renderOutline());
    }
    el.main.appendChild(body);
  }

  /* ------------------------------------------------- 文档态（Obsidian 那半边） */

  function renderDoc() {
    var box = ui.h('div.doc');
    var text = state.note.body || '';
    if (QF.md && QF.md.render) {
      try {
        box.appendChild(QF.md.render(text));
      } catch (err) {
        // 渲染器出错也别让整页白掉：退回纯文本，至少内容还在
        box.appendChild(ui.h('pre.note__raw', { text: text }));
      }
    } else {
      box.appendChild(ui.h('pre.note__raw', { text: text }));
    }
    linkifyWikilinks(box);
    return box;
  }

  /* ------------------------------------------------- 大纲态（幕布那半边） */

  var BULLET_GLYPH = { bullet: '•', heading: '', blank: '', code: '', quote: '❯', rule: '' };

  function renderOutline() {
    var box = ui.h('div.outline');
    var rows = state.note.outline || [];
    var hidden = foldedLines(rows);
    rows.forEach(function (row, idx) {
      if (hidden[idx]) return;
      var rowEl = ui.h('div.oline' + (idx === state.selected ? '.is-on' : ''), {
        dataset: { index: String(idx), kind: row.kind },
        style: { paddingLeft: 4 + row.level * 16 + 'px' },
        onClick: function () {
          state.selected = idx;
          renderMain();
        }
      });
      if (row.foldable) {
        rowEl.appendChild(
          ui.h('button.oline__fold' + (folded[idx] ? '.is-folded' : ''), {
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
        rowEl.appendChild(ui.h('span.oline__fold.oline__fold--none'));
      }
      rowEl.appendChild(
        ui.h('span.oline__bullet', { text: BULLET_GLYPH[row.kind] !== undefined ? BULLET_GLYPH[row.kind] : '•' })
      );
      rowEl.appendChild(
        ui.h('div.oline__text', {
          html: row.text
            ? QF.md && QF.md.renderInline
              ? QF.md.renderInline(row.text)
              : escapeHtml(row.text)
            : '<span class="oline__placeholder">（空行）</span>'
        })
      );
      rowEl.appendChild(
        ui.h('button.oline__edit', {
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
      ui.h('div.outline__hint', {
        html:
          '<kbd>回车</kbd> 同级新建 · <kbd>Tab</kbd> / <kbd>Shift+Tab</kbd> 缩进 · ' +
          '<kbd>Alt+↑</kbd> / <kbd>Alt+↓</kbd> 挪动整块 · <kbd>双击</kbd> 改这一行 · ' +
          '<kbd>退格</kbd> 清掉空行'
      })
    );
    return box;
  }

  /** 折起来的行号集合：折一行 = 把它下面层级更深的那些行一起藏掉。 */
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
    var input = ui.h('input.oline__input', { type: 'text', value: row.raw });
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
    if (state.mode !== 'outline' || !state.note || state.editing) return;
    if (state.note.kind !== 'note') return;
    var tag = (ev.target && ev.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
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
          // 新行直接进入编辑：幕布里回车就该能接着打字，不该还要再点一次
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
    var note = state.note;
    if (!note) return;

    var props = ui.h('section.npanel');
    props.appendChild(ui.h('div.npanel__title', { text: '属性' }));
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
        ui.h('div.npanel__hint', { text: '这个元数据头是导入时补的（带 qf_generated 标记，可批量回退）' })
      );
    }
    el.side.appendChild(props);

    if (note.kind === 'note') {
      el.side.appendChild(
        listPanel('反链 · 谁引用了我', note.backlinks || [], function (item) {
          openNote(item.path, item.line);
        })
      );
      el.side.appendChild(linkPanel('出链 · 我引用了谁', note.outlinks || []));
      if ((note.unresolved || []).length) {
        el.side.appendChild(unresolvedPanel(note.unresolved || []));
      }
    }
    loadTags();
  }

  function propRow(key, value, emptyText, onSave) {
    var valEl = ui.h('div.nprop__val' + (value ? '' : '.nprop__val--empty'), {
      text: value || emptyText,
      title: '点一下就能改'
    });
    valEl.addEventListener('click', function () {
      if (valEl.dataset.editing) return;
      valEl.dataset.editing = '1';
      var input = ui.h('input.oline__input', { type: 'text', value: value || '' });
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
    return ui.h('div.nprop', null, ui.h('span.nprop__key', { text: key }), valEl);
  }

  function saveMeta(patch) {
    api
      .post('/notes/meta', { lib: state.lib, path: state.note.path, meta: patch })
      .then(function (note) {
        state.note = note;
        renderMain();
        renderSide();
        toast('已保存', 'ok');
      })
      .catch(fail);
  }

  function listPanel(title, items, onPick) {
    var panel = ui.h('section.npanel', null, ui.h('div.npanel__title', { text: title }));
    if (!items.length) {
      panel.appendChild(ui.h('div.npanel__hint', { text: '还没有。' }));
      return panel;
    }
    items.forEach(function (item) {
      panel.appendChild(
        ui.h(
          'button.nitem',
          {
            type: 'button',
            title: item.snippet || item.path,
            onClick: function () {
              onPick(item);
            }
          },
          ui.h('div.nitem__title', { text: item.title || item.path }),
          item.snippet ? ui.h('div.nitem__sub', { text: item.snippet }) : null
        )
      );
    });
    return panel;
  }

  function linkPanel(title, links) {
    var panel = ui.h('section.npanel', null, ui.h('div.npanel__title', { text: title }));
    if (!links.length) {
      panel.appendChild(ui.h('div.npanel__hint', { text: '还没有。' }));
      return panel;
    }
    links.forEach(function (link) {
      var label = link.kind === 'note' ? link.title || link.target : link.target;
      var kindText = link.kind === 'note' ? '' : link.kind === 'asset' ? ' · 附件' : ' · 找不到';
      var node = ui.h(
        'button.nitem',
        {
          type: 'button',
          title: link.target,
          onClick: function () {
            if (link.kind === 'note' && link.resolved) openNote(link.resolved, link.line);
            else toast(link.kind === 'asset' ? '这是附件，还没做附件视图' : '这篇还不存在', 'info');
          }
        },
        ui.h('div.nitem__title', { text: label + kindText })
      );
      panel.appendChild(node);
    });
    return panel;
  }

  function unresolvedPanel(targets) {
    var panel = ui.h(
      'section.npanel',
      null,
      ui.h('div.npanel__title', { text: '未解析链接 · ' + targets.length + ' 条' }),
      ui.h('div.npanel__hint', { text: '这些指向还不存在的文件。点右边的「建」当场建一篇。' })
    );
    targets.slice(0, 20).forEach(function (target) {
      panel.appendChild(
        ui.h(
          'div.nprop',
          null,
          ui.h('span.nprop__val', { text: target, title: target }),
          ui.h('button.btn.btn--ghost', {
            type: 'button',
            text: '建',
            title: '按这个名字新建一篇笔记',
            onClick: function () {
              newNotePrompt(target);
            }
          })
        )
      );
    });
    return panel;
  }

  function loadTags() {
    api
      .get('/notes/tags?' + q({ lib: state.lib }))
      .then(function (res) {
        var items = (res.items || []).slice(0, 40);
        if (!items.length) return;
        var panel = ui.h('section.npanel', null, ui.h('div.npanel__title', { text: '标签' }));
        var chips = ui.h('div.nchips');
        items.forEach(function (row) {
          chips.appendChild(
            ui.h(
              'button.nchip',
              {
                type: 'button',
                title: row.count + ' 篇',
                onClick: function () {
                  el.search.value = row.tag;
                  runSearch(row.tag);
                }
              },
              ui.h('span', { text: '#' + row.tag }),
              ui.h('span.nchip__count', { text: String(row.count) })
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
    lastQuery = term;
    api
      .get('/notes/search?' + q({ q: term, lib: state.lib, limit: 60 }))
      .then(function (res) {
        var items = res.items || [];
        ui.clear(el.tree);
        el.tree.appendChild(
          ui.h(
            'div.ntree__head',
            null,
            ui.h('span.ntree__count', { text: '命中 ' + items.length + ' 处' }),
            ui.h('button.btn.btn--ghost', {
              type: 'button',
              text: '清除',
              onClick: function () {
                el.search.value = '';
                loadTree();
              }
            })
          )
        );
        if (!items.length) {
          el.tree.appendChild(ui.h('div.empty', null, ui.h('div.empty__text', { text: '没有命中。' })));
          return;
        }
        items.forEach(function (hit) {
          el.tree.appendChild(
            ui.h(
              'button.nsearch__item',
              {
                type: 'button',
                title: hit.path,
                onClick: function () {
                  openNote(hit.path, hit.line);
                }
              },
              ui.h(
                'div.nsearch__title',
                null,
                ui.h('span', { text: hit.title || hit.path }),
                ui.h('span.nsearch__where', { text: hit.where || '' })
              ),
              ui.h('div.nsearch__snippet', { text: hit.snippet || '' })
            )
          );
        });
      })
      .catch(fail);
  }

  /* ------------------------------------------------------------ 双链 */

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  /** 渲染完再走一遍 DOM，把 `[[目标|别名]]` 换成可点的元素。
   *
   * 为什么不改 Markdown 源：那会把"用户写的字"从渲染链路里改掉，
   * 而这页的"权威"就是文件本身。渲染后再贴，源文件一个字节都不动。
   * 代码块与行内代码里不动（那里写的是例子）。
   */
  function linkifyWikilinks(container) {
    if (!container || !document.createTreeWalker) return;
    var walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        if (!node.nodeValue || node.nodeValue.indexOf('[[') < 0) return NodeFilter.FILTER_REJECT;
        var parent = node.parentNode;
        var tag = parent && parent.nodeName;
        if (tag === 'CODE' || tag === 'PRE' || tag === 'A') return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    var targets = [];
    while (walker.nextNode()) targets.push(walker.currentNode);
    targets.forEach(function (node) {
      var text = node.nodeValue;
      var pattern = /!?\[\[([^\[\]]+)\]\]/g;
      var frag = document.createDocumentFragment();
      var last = 0;
      var match;
      while ((match = pattern.exec(text))) {
        if (match.index > last) frag.appendChild(document.createTextNode(text.slice(last, match.index)));
        var inner = match[1];
        var pipe = inner.split('|');
        var head = pipe[0].split('#')[0].trim();
        var embed = match[0].charAt(0) === '!';
        var label = (pipe[1] || head).trim();
        frag.appendChild(
          ui.h(
            'a.wikilink' + (embed ? '.wikilink--embed' : ''),
            {
              href: '#',
              dataset: { target: head },
              title: embed ? '嵌入：' + head : head,
              onClick: function (ev) {
                ev.preventDefault();
                follow(head);
              }
            },
            label
          )
        );
        last = match.index + match[0].length;
      }
      if (!last) return;
      if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
      node.parentNode.replaceChild(frag, node);
    });
  }

  /** 点一个双链：先按路径找，再按文件名找；找不到就提示可以当场建一篇。 */
  function follow(target) {
    if (!target) return;
    var hit = findInTree(target);
    if (hit) {
      openNote(hit.path);
      return;
    }
    toast('找不到「' + target + '」——右边未解析链接里可以一键建', 'info');
  }

  function findInTree(target) {
    // 与后端的解析规则一致：先当路径看，再当文件名看（双链通常只写文件名）
    var wanted = String(target).replace(/^\.\//, '').replace(/\\/g, '/').replace(/\.md$/i, '');
    var bare = wanted.split('/').pop();
    var found = null;
    (function walk(node) {
      if (found || !node) return;
      (node.files || []).forEach(function (file) {
        if (found) return;
        var path = file.path.replace(/\.md$/i, '');
        var name = file.name.replace(/\.md$/i, '');
        if (path === wanted || name === bare || (file.title || '') === bare) found = file;
      });
      (node.dirs || []).forEach(walk);
    })(treeCache);
    return found;
  }

  /* ------------------------------------------------------------ 改名 / 撤销 / 新建 */

  function renamePrompt() {
    if (!state.note || state.note.kind !== 'note') return;
    var current = state.note.path.replace(/\.md$/i, '');
    var input = ui.h('input.oline__input', { type: 'text', value: current });
    ui.modal({
      title: '改名（引用它的地方会一起改）',
      size: 'sm',
      body: ui.h('div', null, ui.h('div.npanel__hint', { text: '库内相对路径，不带 .md' }), input),
      actions: [
        { label: '取消', kind: 'ghost', onClick: function (close) { close(); } },
        {
          label: '改名',
          kind: 'primary',
          onClick: function (close) {
            var next = input.value.trim().replace(/^\/+/, '');
            if (!next || next === current) {
              close();
              return;
            }
            close();
            api
              .post('/notes/rename', { lib: state.lib, path: state.note.path, to: next + '.md' })
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
      ],
    });
    // `ui.modal` 没有 onOpen 钩子，卡片是刚插进 DOM 的 —— 聚焦要等这一帧过去
    setTimeout(function () {
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    }, 0);
  }
  function newNotePrompt(title) {
    var input = ui.h('input.oline__input', { type: 'text', value: title || '' });
    ui.modal({
      title: '新建笔记',
      size: 'sm',
      body: ui.h(
        'div',
        null,
        ui.h('div.npanel__hint', { text: '建在当前库的根目录下；带斜杠可以建到子目录。' }),
        input
      ),
      actions: [
        { label: '取消', kind: 'ghost', onClick: function (close) { close(); } },
        {
          label: '新建',
          kind: 'primary',
          onClick: function (close) {
            var name = input.value.trim();
            if (!name) {
              close();
              return;
            }
            close();
            var folder = '';
            var slash = name.lastIndexOf('/');
            if (slash > 0) {
              folder = name.slice(0, slash);
              name = name.slice(slash + 1);
            }
            api
              .post('/notes/create', { lib: state.lib, folder: folder, title: name })
              .then(function (note) {
                toast('建好了', 'ok');
                state.note = note;
                state.mode = 'outline';
                state.selected = 0;
                folded = {};
                renderMain();
                renderSide();
                loadTree();
              })
              .catch(fail);
          }
        }
      ],
    });
    setTimeout(function () {
      input.focus();
    }, 0);
  }

  function undoNote() {
    if (!state.note) return;
    api
      .post('/notes/undo', { lib: state.lib, path: state.note.path })
      .then(function (res) {
        toast('已撤销到上一版（' + res.restored + '）', 'ok');
        openNote(state.note.path);
      })
      .catch(fail);
  }

  /* ------------------------------------------------------------ 启动 */

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
