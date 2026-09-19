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
            body.appendChild(meta);
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
        // **原件在最上面**：这一格是"看这份资料"的地方，正文只是它的抽取物。
        // 原先只给抽取的正文（前 4000 字），想看 PDF 还得回资料页 —— 用户的原话是
        // "在资源页面无法查看资料"。查看器与资料页共用（docview.js）。
        var fileBox = h('div.panes__docfile');
        doc.appendChild(fileBox);
        QF.docview.render(fileBox, { citekey: opts.citekey, index: -1 }, { head: false });

        // 元数据与抽取的正文收在下面（默认折起：要先看到资料本身）
        var fold = h('details.panes__docinfo', null, h('summary', { text: '元数据与抽取的正文' }));
        var body = h('div.panes__docbody');
        fold.appendChild(body);
        doc.appendChild(fold);
        body.appendChild(h('p.panes__muted', { text: '正在读 ' + opts.citekey + '…' }));
        api.get('/library/item?citekey=' + encodeURIComponent(opts.citekey) + '&text=4000')
          .then(function (data) {
            // 接口返回的是**摊平的字典**：brief() 的字段 + bibtex / citedBy / assets / files，
            // 传了 `text` 参数时再多一个 `text` 对象（state / chars / ratio / head）。
            // 踩过：我原先按 `data.item` 取，取不到；正文也当成字符串，实际在 `text.head` 里。
            var item = (data && data.item) || data || {};
            var info = (data && data.text) || {};
            var text = info.head || '';
            ui.clear(body);
            body.appendChild(h('h2.panes__doctitle', { text: item.title || opts.citekey }));
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
              body.appendChild(h('p.panes__muted', { text: '来源：' + item.source }));
            }
            if (item.files && item.files.length > 1) {
              body.appendChild(h('p.panes__muted', {
                text: '这条还带 ' + (item.files.length - 1) + ' 个附属资源',
              }));
            }
            if (item.citedBy && item.citedBy.length) {
              body.appendChild(h('p.panes__muted', {
                text: '被这些笔记引用：' + item.citedBy.map(function (one) { return one.title || one.path; }).join('、'),
              }));
            }
            if (text) {
              var label = { pdf: 'PDF 抽取', md: '原文', html: '网页抽取' }[info.state] || info.state || '';
              body.appendChild(h('p.panes__muted', {
                text: '正文 ' + (info.chars || text.length) + ' 字' + (label ? ' · ' + label : ''),
              }));
              body.appendChild(h('div.md', null, QF.md.render(text)));
            } else {
              body.appendChild(h('p.panes__muted', { text: '还没有抽取到正文（资料页里点"建索引"就会缓存一份）' }));
            }
          })
          .catch(function (err) {
            ui.clear(body);
            body.appendChild(h('p.panes__muted', {
              text: '读不到这份资料：' + ((err && err.message) || err),
            }));
          });
      },
    });
  }

  /* ------------------------------------------------------------------ 一份文件 */

  function registerFile() {
    QF.panes.register('file', {
      title: '文件',
      icon: 'book',
      key: function (opts) {
        return (opts.citekey || '') + '#' + (opts.index == null ? -1 : opts.index);
      },
      // 查看器自己管滚动与铺满（样板见 docview.css），所以这里只把容器给它
      mount: function (host, opts) {
        return QF.docview.render(host, opts, {});
      },
    });
  }

  /* ------------------------------------------------------------------ 题目 */

  /** 在题库里按 id 找一道题。数据层换过名字，这里几种形状都认一遍，读得到就行。 */
  function findQuestion(id) {
    var D = QF.data;
    if (!D || !id) return null;
    var list = null;
    if (typeof D.questions === 'function') list = D.questions();
    else if (D.questions) list = D.questions;
    (list || []).forEach(function () { /* 兼容遍历 */ });
    for (var i = 0; i < (list || []).length; i++) {
      if (list[i] && list[i].id === id) return list[i];
    }
    return null;
  }

  function registerQuestion() {
    QF.panes.register('question', {
      title: '题目',
      icon: 'star',
      key: function (opts) { return (opts && (opts.id || (opts.question && opts.question.id))) || ''; },
      mount: function (host, opts) {
        var q = (opts && opts.question) || findQuestion(opts && opts.id);
        var box = h('div.panes__doc.panes__doc--q');
        host.appendChild(box);
        if (!q) {
          box.appendChild(h('p.panes__muted', {
            text: '找不到这道题（题库里没有这个 id：' + ((opts && opts.id) || '(空)') + '）',
          }));
          return;
        }
        // 直接复用刷题页与错题本那套卡片：题面、选项、判定条、解析的表现完全一致
        box.appendChild(QF.qview.card(q, { locked: true }));
        box.appendChild(QF.qview.explainPanel(q, { open: false }));
      },
    });
  }

  /* ------------------------------------------------------------------ 笔记图谱 */

  function registerNoteGraph() {
    // 渲染在 `notegraph.js`：笔记页也要用同一份，所以它不在这一页
    QF.panes.register('notegraph', {
      title: '笔记图谱',
      icon: 'target',
      key: function (opts) { return opts.lib || ''; },
      mount: QF.notegraph.mount,
    });
  }

  var DEFAULT_LAYOUT = {
    kind: 'split',
    id: 's-boot',
    dir: 'row',
    ratio: 0.55,
    a: {
      kind: 'stage',
      id: 'p-boot-a',
      at: 0,
      tabs: [{ key: 'canvas:', view: 'canvas', opts: {}, title: '' }],
    },
    b: {
      kind: 'stage',
      id: 'p-boot-b',
      at: 0,
      tabs: [{ key: 'blank:{}', view: 'blank', opts: {}, title: '' }],
    },
  };

  function boot() {
    var root = document.getElementById('workbench-root');
    if (!root) return;

    // 设置（主题、工具挂载）在服务端那份里，而这一页不加载 boot.js ——
    // 不取回来就退回写死的兜底值：干净浏览器里表现为"工作台深色、练习中心浅色"。
    // 取回来再画，免得更明显：先画一帧深色、再跳成浅色。
    var ready = QF.shell && QF.shell.pullSettings ? QF.shell.pullSettings() : Promise.resolve();
    ready.then(function () { paint(root); });
  }

  function paint(root) {
    ui.clear(root);
    registerBlank();
    registerCanvas();
    registerNote();
    registerDoc();
    registerQuestion();
    registerFile();
    registerNoteGraph();

    // 主题必须显式初始化：`ui.theme.current()` 的兜底是 dark，
    // 不调这一句，整页（含顶栏）会是深色 —— app.js / chat.js / wrongbook.js 各自都调了
    // 顶栏是 shell.js 的事：不传 onPick 就渲染成带 hash 的链接（跳去刷题页那四态）
    if (QF.shell && QF.shell.mount) QF.shell.mount({});

    var wrap = h('div.wkb');
    root.appendChild(wrap);
    QF.panes.mount(wrap, { default: DEFAULT_LAYOUT });

    // 在别的页面点资源树时会留下一件"要开什么"，这里消费掉 ——
    // 用户的心智是"我要看这个"，不该因为当时不在工作台就失效
    var pending = null;
    try { pending = JSON.parse(localStorage.getItem('qf.panes.pending') || 'null'); } catch (e) { pending = null; }
    if (pending && pending.kind) {
      try { localStorage.removeItem('qf.panes.pending'); } catch (e) { /* 忽略 */ }
      QF.panes.open(pending.kind, pending.ref, pending.title);
    }
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
