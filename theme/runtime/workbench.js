/* 工作台 —— 把窗格引擎架起来，并把各个模块注册成"窗格里能装的东西"。
 *
 * 这一版的边界（写清楚，免得看起来像全接完了）：
 *   已接：空窗格、画布（`canvas.js` 本来就是 `mount(host, opts)`，是全仓库唯一达标的）
 *   未接：对话、资料、笔记、图谱 —— 它们现在都写死了自己的根容器，
 *         要当窗格内容得先把渲染改成 `mount(host, opts)`（对话是下一块，它在正中间）
 *
 * 视图只需要满足一件事：给一个容器，把自己画进去；关窗时把定时器/未落盘的东西收好。
 * 别的一概不管 —— 排版、比例、最大化、持久化都是引擎的事。
 */
(function () {
  var ui = QF.ui;
  var h = ui.h;
  var api = QF.api;

  /* ------------------------------------------------------------------ 空窗格 */

  function registerBlank() {
    QF.panes.register('blank', {
      title: '空',
      icon: 'plus',
      mount: function (host) {
        host.appendChild(
          h('div.panes__missing', null,
            h('p.panes__missing-t', null, '这个窗格还是空的'),
            h('p.panes__missing-s', null, '点右上角的列表图标换内容（画布已经能用，对话/资料/笔记正在接）。'),
            h('p.panes__missing-s', null, 'Alt+\\ 左右拆 · Alt+- 上下拆 · Alt+Z 最大化 · Alt+W 关掉'))
        );
      },
    });
  }

  /* ------------------------------------------------------------------ 画布 */

  var canvasOwner = null;   // 画布是单例（`canvas.js` 里那个 `live`）：同一时刻只能有一块活着

  function walkFiles(node, out) {
    (node.files || []).forEach(function (file) { out.push(file); });
    var dirs = node.dirs || {};
    Object.keys(dirs).forEach(function (key) { walkFiles(dirs[key], out); });
    return out;
  }

  function registerCanvas() {
    QF.panes.register('canvas', {
      title: '画布',
      icon: 'grip',
      // 同一样东西再开一次就切过去（`key` 决定"什么算同一个标签"）
      key: function (opts) { return (opts.lib || '') + '/' + (opts.path || ''); },
      mount: function (host, opts, ctx) {
        var paneId = ctx && ctx.leaf ? ctx.leaf.id : null;

        if (!opts.lib || !opts.path) {
          picker(host, paneId);
          return;
        }
        // 单例提醒：`canvas.js` 的 `mount()` 开头会把自己上一块卸掉，
        // 所以两个窗格都装画布时，先开的那块会空掉 —— 与其让人以为坏了，不如先说清
        if (canvasOwner && canvasOwner !== paneId) {
          ui.toast('画布一次只能开一块：另一个窗格里的那块会空着', 'warn', 3600);
        }
        canvasOwner = paneId;

        host.appendChild(h('div.panes__canvas'));
        var box = host.querySelector('.panes__canvas');
        QF.canvas.mount(box, {
          lib: opts.lib,
          path: opts.path,
          onOpenNote: function (target) {
            // 画布上的 `file` 卡片指向某篇笔记时，告诉宿主去开那一篇（工作台里暂时只提示）
            var rel = String(target || '').replace(/^\[\[|\]\]$/g, '').replace(/^\.\//, '').trim();
            if (rel) ui.toast('画布指向的笔记：' + rel, 'info', 2600);
          },
          onStatus: function () { /* 窗格没有自己的状态行，忽略 */ },
          onDirty: function () { /* 同上：落盘由 `canvas.js` 自己按 500ms 节流做 */ },
        });
      },
      unmount: function () {
        QF.canvas.unmount();
        canvasOwner = null;
      },
    });
  }

  /** 没选画布时先让人选一块：列出各库里 kind 为 canvas 的文件。 */
  function picker(host, paneId) {
    var box = h('div.panes__missing');
    box.appendChild(h('p.panes__missing-t', null, '选一块画布'));
    var listBox = h('div.wkb__pick');
    box.appendChild(listBox);
    host.appendChild(box);

    api.get('/notes/stats').then(function (data) {
      var libs = (data && data.libraries) || [];
      if (!libs.length) {
        listBox.appendChild(h('p.panes__missing-s', null, '还没有笔记库'));
        return;
      }
      var jobs = libs.map(function (lib) {
        return api.get('/notes/tree?lib=' + encodeURIComponent(lib.name)).then(function (tree) {
          var files = walkFiles(tree || {}, []).filter(function (file) { return file.kind === 'canvas'; });
          files.forEach(function (file) {
            var item = h('button.wkb__pick-item', { type: 'button' });
            item.appendChild(h('span.wkb__pick-lib', null, lib.name));
            item.appendChild(h('span', null, file.name));
            item.addEventListener('click', function () {
              // 开一个标签（已有同名的就切过去）；引擎负责挂载与持久化
              QF.panes.open('canvas', { lib: lib.name, path: file.path }, file.name);
            });
            listBox.appendChild(item);
          });
        }).catch(function () { /* 某个库读不到就跳过，不拦别的库 */ });
      });
      Promise.all(jobs).then(function () {
        if (!listBox.childNodes.length) {
          listBox.appendChild(h('p.panes__missing-s', null, '这四个库里没有 .canvas 文件'));
        }
      });
      return paneId;   // 只是表明这个参数留着：将来要做"记住上次选的那块"时用得上
    }).catch(function () {
      listBox.appendChild(h('p.panes__missing-s', null, '读不到笔记库列表'));
    });
  }

  /* ------------------------------------------------------------------ 笔记 */

  function registerNote() {
    QF.panes.register('note', {
      title: '笔记',
      icon: 'book',
      key: function (opts) { return (opts.lib || '') + '/' + (opts.path || ''); },
      mount: function (host, opts) {
        var doc = h('div.panes__doc');
        host.appendChild(doc);
        if (!opts.lib || !opts.path) {
          doc.appendChild(h('p.panes__muted', { text: '没给笔记路径' }));
          return;
        }
        doc.appendChild(h('p.panes__muted', { text: '正在读 ' + opts.path + '…' }));
        api.get('/notes/note?lib=' + encodeURIComponent(opts.lib) + '&path=' + encodeURIComponent(opts.path))
          .then(function (note) {
            ui.clear(doc);
            doc.appendChild(h('h1', { text: note.title || opts.path }));
            var meta = h('div.panes__meta');
            (note.tags || []).forEach(function (tag) {
              meta.appendChild(h('span.panes__chip', { text: '#' + tag }));
            });
            if (note.backlinks && note.backlinks.length) {
              meta.appendChild(h('span.panes__muted', { text: '被引 ' + note.backlinks.length + ' 处' }));
            }
            doc.appendChild(meta);
            // 正文交给共用的 Markdown 零件（与笔记页、对话里用的是同一份）
            doc.appendChild(h('div.md', null, QF.md.render(note.body || '')));
          })
          .catch(function (err) {
            ui.clear(doc);
            doc.appendChild(h('p.panes__muted', {
              text: '读不到这篇笔记：' + ((err && err.message) || err),
            }));
          });
      },
    });
  }

  /* ------------------------------------------------------------------ 资料条目 */

  function registerDoc() {
    QF.panes.register('doc', {
      title: '资料',
      icon: 'book',
      key: function (opts) { return opts.citekey || ''; },
      mount: function (host, opts) {
        var doc = h('div.panes__doc');
        host.appendChild(doc);
        if (!opts.citekey) {
          doc.appendChild(h('p.panes__muted', { text: '没给引用键' }));
          return;
        }
        doc.appendChild(h('p.panes__muted', { text: '正在读 ' + opts.citekey + '…' }));
        api.get('/library/item?citekey=' + encodeURIComponent(opts.citekey) + '&text=4000')
          .then(function (data) {
            // 接口返回的是**摊平的字典**：brief() 的字段 + bibtex / citedBy / assets / files，
            // 传了 `text` 参数时再多一个 `text` 对象（state / chars / ratio / head）。
            // 踩过：我原先按 `data.item` 取，取不到；正文也当成字符串，实际在 `text.head` 里。
            var item = (data && data.item) || data || {};
            var info = (data && data.text) || {};
            var text = info.head || '';
            ui.clear(doc);
            doc.appendChild(h('h1', { text: item.title || opts.citekey }));
            var meta = h('div.panes__meta');
            if (item.authors && item.authors.length) {
              meta.appendChild(h('span.panes__muted', { text: item.authors.join('、') }));
            }
            if (item.year) meta.appendChild(h('span.panes__muted', { text: String(item.year) }));
            if (item.kind) meta.appendChild(h('span.panes__chip', { text: item.kind }));
            (item.topics || []).forEach(function (topic) {
              meta.appendChild(h('span.panes__chip', { text: topic }));
            });
            meta.appendChild(h('span.panes__chip', { text: item.citekey || opts.citekey }));
            doc.appendChild(meta);
            if (item.source) {
              doc.appendChild(h('p.panes__muted', { text: '来源：' + item.source }));
            }
            if (item.files && item.files.length > 1) {
              doc.appendChild(h('p.panes__muted', {
                text: '这条还带 ' + (item.files.length - 1) + ' 个附属资源',
              }));
            }
            if (item.citedBy && item.citedBy.length) {
              doc.appendChild(h('p.panes__muted', {
                text: '被这些笔记引用：' + item.citedBy.map(function (one) { return one.title || one.path; }).join('、'),
              }));
            }
            if (text) {
              var label = { pdf: 'PDF 抽取', md: '原文', html: '网页抽取' }[info.state] || info.state || '';
              doc.appendChild(h('p.panes__muted', {
                text: '正文 ' + (info.chars || text.length) + ' 字' + (label ? ' · ' + label : ''),
              }));
              doc.appendChild(h('div.md', null, QF.md.render(text)));
            } else {
              doc.appendChild(h('p.panes__muted', { text: '还没有抽取到正文（资料页里点"建索引"就会缓存一份）' }));
            }
          })
          .catch(function (err) {
            ui.clear(doc);
            doc.appendChild(h('p.panes__muted', {
              text: '读不到这份资料：' + ((err && err.message) || err),
            }));
          });
      },
    });
  }

  /* ------------------------------------------------------------------ 装配 */

  var DEFAULT_LAYOUT = {
    kind: 'split',
    id: 's-boot',
    dir: 'row',
    ratio: 0.55,
    a: { kind: 'stage', id: 'p-boot-a', view: 'canvas', opts: {} },
    b: { kind: 'stage', id: 'p-boot-b', view: 'blank', opts: {} },
  };

  function boot() {
    var root = document.getElementById('workbench-root');
    if (!root) return;
    ui.clear(root);
    registerBlank();
    registerCanvas();
    registerNote();
    registerDoc();

    // 主题必须显式初始化：`ui.theme.current()` 的兜底是 dark，
    // 不调这一句，整页（含顶栏）会是深色 —— app.js / chat.js / wrongbook.js 各自都调了
    ui.theme.init();

    // 顶栏是 shell.js 的事：不传 onPick 就渲染成带 hash 的链接（跳去刷题页那四态）
    if (QF.shell && QF.shell.mount) QF.shell.mount({});

    var wrap = h('div.wkb');
    root.appendChild(wrap);
    QF.panes.mount(wrap, { default: DEFAULT_LAYOUT });
  }

  QF.workbench = {
    boot: boot,
    DEFAULT_LAYOUT: DEFAULT_LAYOUT,
    // 资源树接上之后就用它：树上点一下 = 在这一格开一个标签
    openResource: function (kind, ref, title) { QF.panes.open(kind, ref, title); },
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
