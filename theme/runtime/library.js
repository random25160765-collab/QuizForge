/* 资料：只读的资料来源库。
 *
 * 三条边界，与后端（`api/app/library.py`、`routers/library.py`）对齐：
 *
 * 1. **源目录只读**。这一页没有任何"改文件"的入口；能改的只有元数据，
 *    改完后端会把它标成 `origin: manual`（人定的）—— 界面上也照这个分档显示。
 * 2. **抽不出文字就如实说**。实测 55 份 PDF 里有 1 份是扫描书（Type 3 字体、没有
 *    字形到 Unicode 的映射），界面要明写"抽不出可用文字"并说清原因，
 *    而不是给一个空正文假装"这份资料没内容"。
 * 3. **索引攒批推进**。PDF 抽取实测 1.9 秒一个，一次请求抽完会让界面一直转圈、
 *    也没有进度可言；所以前端循环调 `POST /library/index`，每批若干个，随时能停。
 *
 * 与 `canvas.js` 一样，这一块是**可换宿主的模块**：现在挂在 `library.html` 上，
 * 三栏主界面落地时挂到左栏（计划 A7）——所以不要在这里假设"整页都是我的"。
 */
(function () {
  'use strict';

  var QF = (window.QF = window.QF || {});
  var ui = QF.ui;
  var api = QF.api;

  function h() {
    return ui.h.apply(ui, arguments);
  }

  //: 条目类型的显示名与色阶（同一套色阶区分论文 / 手册 / 网页文档）。
  var KIND_LABEL = {
    paper: '论文',
    book: '书',
    manual: '手册',
    report: '报告',
    webpage: '网页',
    blog: '博客',
    slides: '幻灯片',
    code: '代码',
    note: '笔记',
    other: '其他'
  };

  //: 正文质量的显示名。`garbled` 要说清**为什么**，不然用户只会以为坏了。
  var TEXT_LABEL = {
    ok: { text: '正文可用', tone: 'ok' },
    poor: { text: '正文可读性一般', tone: 'warn' },
    garbled: { text: '抽不出可用文字', tone: 'bad' },
    none: { text: '还没抓正文', tone: '' }
  };

  var TEXT_WHY = {
    garbled: '这一份的文件里没有"字形到 Unicode"的映射（扫描书转成的 PDF 常见），' +
      '任何基于文字层的工具都抽不出字，只能靠 OCR。库里如实标注，不拿乱码当正文。',
    poor: '能抽出文字，但数字与符号占比很高（表格密集的规范与手册常见），检索能用，正文请以原件为准。'
  };

  var state = {
    roots: [],
    items: [],
    query: '',
    citekey: '',
    detail: null,
    indexing: false,
    status: ''
  };

  var el = {};

  // ------------------------------------------------------------------ 启动

  function boot() {
    try {
      QF.shell.mount({});
    } catch (err) {
      if (window.console) console.warn('顶栏接线失败', err);
    }
    el.root = document.getElementById('library-root');
    el.pick = document.getElementById('library-root-pick');
    el.addRoot = document.getElementById('library-addroot');
    el.search = document.getElementById('library-search');
    el.clear = document.getElementById('library-clear');
    el.bar = document.getElementById('library-bar');
    el.list = document.getElementById('library-items');
    el.count = document.getElementById('library-count');
    el.indexBtn = document.getElementById('library-index');
    el.main = document.getElementById('library-main');
    el.side = document.getElementById('library-side');

    el.search.addEventListener('input', ui.debounce(function () {
      state.query = el.search.value.trim();
      el.clear.hidden = !state.query;
      loadItems();
    }, 260));
    el.search.addEventListener('keydown', function (ev) {
      ev.stopPropagation();
      if (ev.key === 'Escape') {
        el.search.value = '';
        state.query = '';
        el.clear.hidden = true;
        loadItems();
      }
    });
    el.clear.addEventListener('click', function () {
      el.search.value = '';
      state.query = '';
      el.clear.hidden = true;
      loadItems();
    });
    el.addRoot.addEventListener('click', addRootPrompt);
    el.indexBtn.addEventListener('click', toggleIndex);
    el.pick.addEventListener('change', function () {
      if (el.pick.value === '__add__') {
        addRootPrompt();
        return;
      }
      api
        .post('/library/roots', { action: 'remove', path: el.pick.value })
        .then(function () {
          ui.toast('已从清单里去掉（目录与文件都没动）', 'ok');
          loadRoots(true);
        })
        .catch(fail);
    });

    renderMain();
    renderSide();
    loadRoots();
  }

  function fail(err) {
    var message = (err && err.message) || '出了点问题';
    ui.toast(message, 'bad');
    state.status = message;
    renderBar();
  }

  // ------------------------------------------------------------------ 读

  function loadRoots(thenItems) {
    api
      .get('/library/roots')
      .then(function (res) {
        state.roots = res.roots || [];
        renderRootPicker(res);
        if (thenItems || !state.items.length) loadItems();
      })
      .catch(function (err) {
        if (err && err.status === 404) {
          // 路由没了：这一页会一直空着，**必须说出来**，否则看起来像"库里没有资料"
          state.status = '资料接口没挂上（/api/library）。后端没起来或路由没登记。';
          renderBar();
          return;
        }
        fail(err);
      });
  }

  function loadItems() {
    var path = '/library/items?limit=600' + (state.query ? '&q=' + encodeURIComponent(state.query) : '');
    api
      .get(path)
      .then(function (res) {
        state.items = res.items || [];
        renderBar(res.count);
        renderList();
        if (!state.citekey && state.items.length) selectItem(state.items[0].citekey);
        if (state.citekey && !state.items.some(function (one) { return one.citekey === state.citekey; })) {
          state.citekey = '';
          state.detail = null;
          renderMain();
          renderSide();
        }
      })
      .catch(fail);
  }

  function loadDetail(citekey) {
    api
      .get('/library/item?citekey=' + encodeURIComponent(citekey) + '&text=1200')
      .then(function (res) {
        state.detail = res;
        renderMain();
        renderSide();
      })
      .catch(function (err) {
        if (err && err.status === 404) {
          state.detail = null;
          state.status = '这个条目不见了：' + citekey;
          renderMain();
          renderBar();
          return;
        }
        fail(err);
      });
  }

  // ------------------------------------------------------------------ 画

  function renderRootPicker(res) {
    ui.clear(el.pick);
    (state.roots || []).forEach(function (path) {
      var short = path.split('/').filter(Boolean).pop() || path;
      el.pick.appendChild(
        h('option', { value: path, text: short + '（' + path + '）', title: path })
      );
    });
    el.pick.appendChild(h('option', { value: '__add__', text: '＋ 添加目录…' }));
    el.pick.title = '资料根：' + (state.roots.join(' · ') || '（没有）') + '；换一个会把它从清单里去掉';
    if (res && res.missing && res.missing.length) {
      state.status = '有根目录不存在：' + res.missing.join('、');
    }
  }

  function renderBar(count) {
    ui.clear(el.bar);
    var total = typeof count === 'number' ? count : state.items.length;
    var pending = state.items.filter(function (one) {
      return one.text.state === 'none' && !one.text.attempted;      // 试过没有正文的不算"待抓"
    }).length;
    var needMeta = state.items.filter(function (one) { return one.origin === 'inferred' && !one.authors.length; }).length;
    el.bar.appendChild(h('span.lib__baritem', { text: (state.query ? '命中 ' : '共 ') + total + ' 条' }));
    if (pending) el.bar.appendChild(h('span.lib__baritem.lib__baritem--dim', { text: '待抓正文 ' + pending }));
    if (needMeta) el.bar.appendChild(h('span.lib__baritem.lib__baritem--dim', { text: '待补元数据 ' + needMeta }));
    el.count.textContent = state.status || (state.indexing ? '正在抓正文…' : '资料只读；能改的只有元数据');
    // 抓正文时**不能禁用**这个按钮 —— 它的另一半职责就是"停下"。
    // 实测就是被 `disabled` 卡住的：一开抓，按钮点不动，只能等它跑完。
    el.indexBtn.classList.toggle('is-on', state.indexing);
    el.indexBtn.textContent = state.indexing ? '停下' : '抓正文';
    el.indexBtn.title = state.indexing
      ? '停下（已经抓到的不会丢，下次从剩下的接着抓）'
      : '抽正文（PDF 抽取较慢，分批推进，可随时停下）';
  }

  /** 把刚抓过的几条从"待抓"里划掉 —— 不动整列表（重画会顶掉滚动位置）。 */
  function renderPending(done) {
    done.forEach(function (one) {
      var hit = null;
      for (var i = 0; i < state.items.length; i++) {
        if (state.items[i].citekey === one.citekey) { hit = state.items[i]; break; }
      }
      if (hit) hit.text = { state: one.state || 'ok', chars: one.chars || 0, ratio: 0 };
    });
  }

  function renderList() {
    ui.clear(el.list);
    if (!state.items.length) {
      el.list.appendChild(
        h('div.lib__empty', {
          text: state.query ? '没有命中的条目。' : '这个根下没扫到条目（根目录是否还在？）'
        })
      );
      return;
    }
    state.items.forEach(function (one) {
      var text = TEXT_LABEL[one.text.state] || TEXT_LABEL.none;
      var row = h(
        'button.lib__row' + (one.citekey === state.citekey ? '.is-on' : ''),
        {
          type: 'button',
          title: one.citekey + ' · ' + one.rel,
          onClick: function () {
            selectItem(one.citekey);
          }
        },
        h(
          'span.lib__rowtop',
          null,
          h('span.lib__title', { text: one.title || one.rel }),
          h('span.lib__kind.lib__kind--' + one.kind, { text: KIND_LABEL[one.kind] || one.kind })
        ),
        h(
          'span.lib__rowbot',
          null,
          h('span.lib__meta', {
            text: [authorsLine(one), one.year || ''].filter(Boolean).join(' · ') || '（作者与年份待补）'
          }),
          h('span.lib__marks', null,
            one.origin === 'manual' ? h('span.lib__mark.lib__mark--manual', { text: '人定' }) : null,
            one.origin === 'llm' ? h('span.lib__mark.lib__mark--llm', { text: '模型' }) : null,
            one.text.state === 'garbled' ? h('span.lib__mark.lib__mark--bad', { text: '无正文' }) : null,
            one.text.state === 'poor' ? h('span.lib__mark.lib__mark--warn', { text: '正文一般' }) : null,
            one.text.state === 'none'
              ? h('span.lib__mark', { text: one.text.attempted ? '无正文' : '未抓' })
              : null
          )
        ),
        h('span.lib__key', { text: one.citekey })
      );
      row.setAttribute('aria-selected', one.citekey === state.citekey ? 'true' : 'false');
      el.list.appendChild(row);
    });
  }

  function authorsLine(one) {
    var names = one.authors || [];
    if (!names.length) return '';
    return names.slice(0, 2).join(', ') + (names.length > 2 ? ' 等' : '');
  }

  function selectItem(citekey) {
    if (state.citekey === citekey && state.detail) return;
    state.citekey = citekey;
    renderList();
    renderMain();
    renderSide();
    loadDetail(citekey);
  }

  function renderMain() {
    ui.clear(el.main);
    if (!state.citekey || !state.detail) {
      el.main.appendChild(
        h(
          'div.lib__welcome',
          null,
          h('div.lib__welcometitle', { text: state.items.length ? '左边挑一个条目' : '还没有条目' }),
          h('div.lib__welcometext', {
            text: state.items.length
              ? '这一页只读资料文件；能改的只有元数据（作者、年份、类型、主题）。'
              : '先确认资料根对不对（左上角那个下拉），再点「抓正文」把 PDF 的文字抽进本地缓存。'
          })
        )
      );
      return;
    }
    var one = state.detail;
    var text = TEXT_LABEL[one.text.state] || TEXT_LABEL.none;

    var head = h(
      'header.lib__detailhead',
      null,
      h('h1.lib__detailtitle', { text: one.title || one.rel }),
      h(
        'div.lib__detailline',
        null,
        h('code.lib__citekey', { text: one.citekey, title: '引用键：笔记里写它就算引用了这份资料' }),
        copyBtn('复制引用键', function () { return one.citekey; }),
        copyBtn('复制 @ 形式', function () { return '@' + one.citekey; }),
        copyBtn('复制 BibTeX', function () { return one.bibtex || ''; })
      ),
      h(
        'div.lib__detailtags',
        null,
        h('span.lib__kind.lib__kind--' + one.kind, { text: KIND_LABEL[one.kind] || one.kind }),
        h('span.lib__origin.lib__origin--' + (one.origin || 'inferred'), {
          text: { manual: '元数据：人定的', llm: '元数据：模型填的', inferred: '元数据：推断的（待核）' }[one.origin || 'inferred']
        }),
        h('span.lib__textstate.lib__textstate--' + (text.tone || 'none'), { text: text.text }),
        h('span.lib__path', { text: one.rel, title: one.path })
      )
    );

    var body = h('div.lib__detailbody');
    body.appendChild(metaPanel(one));
    if (text.tone === 'bad' || text.tone === 'warn') {
      body.appendChild(h('div.lib__notice.lib__notice--' + text.tone, { text: TEXT_WHY[one.text.state] || '' }));
    }
    body.appendChild(filesPanel(one));
    body.appendChild(textPanel(one));

    el.main.appendChild(head);
    el.main.appendChild(body);
  }

  function copyBtn(label, getter) {
    return h('button.lib__btn.lib__btn--tiny', {
      type: 'button',
      text: label,
      onClick: function () {
        var value = getter();
        if (!value) {
          ui.toast('没有可复制的内容', 'bad');
          return;
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(value).then(
            function () { ui.toast('已复制' + (label.indexOf('BibTeX') >= 0 ? ' BibTeX' : ''), 'ok'); },
            function () { ui.toast('浏览器不给复制权限', 'bad'); }
          );
          return;
        }
        ui.toast('这个浏览器不支持复制', 'bad');
      }
    });
  }

  /** 元数据：人可改，改完 origin 变 manual。**不猜的字段就留空**，别替用户编。 */
  function metaPanel(one) {
    var panel = h('section.lib__panel', null, h('div.lib__paneltitle', { text: '元数据' }));
    var grid = h('div.lib__meta');
    grid.appendChild(metaRow('标题', 'title', one.title, 'text'));
    grid.appendChild(metaRow('作者', 'authors', (one.authors || []).join(', '), 'text', '逗号分隔'));
    grid.appendChild(metaRow('年份', 'year', one.year || '', 'number'));
    grid.appendChild(metaRow('类型', 'kind', one.kind, 'text', kindHint()));
    grid.appendChild(metaRow('主题', 'topics', (one.topics || []).join(', '), 'text', '逗号分隔，取自目录'));
    if (one.arxiv) grid.appendChild(metaRow('arXiv', 'arxiv', one.arxiv, 'text'));
    panel.appendChild(grid);
    return panel;
  }

  function kindHint() {
    return Object.keys(KIND_LABEL).map(function (key) { return key + '=' + KIND_LABEL[key]; }).join('，');
  }

  function metaRow(label, key, value, type, hint) {
    var shown = value === 0 || value ? String(value) : '';
    var input = h('input.lib__field', {
      type: type || 'text',
      value: shown,
      placeholder: hint || '待补',
      title: hint || '',
      'aria-label': label
    });
    var save = function () {
      var next = input.value.trim();
      if (next === shown) return;
      var patch = {};
      if (key === 'authors' || key === 'topics') {
        patch[key] = next ? next.split(',').map(function (part) { return part.trim(); }).filter(Boolean) : [];
      } else if (key === 'year') {
        // 空 = **把这个字段去掉**，不是存 0。实测踩过：清空年份后元数据里留下 `year: 0`，
        // 于是列表里显示"0 年"，比没有还糟。
        patch[key] = next ? Number(next) || null : null;
      } else {
        patch[key] = next || null;
      }
      api
        .post('/library/meta', { citekey: state.citekey, meta: patch })
        .then(function () {
          ui.toast('元数据已更新（记为「人定的」）', 'ok');
          loadDetail(state.citekey);
          loadItems();
        })
        .catch(function (err) {
          input.value = shown;                       // 存不下就当没改过，别留一个假的界面状态
          fail(err);
        });
    };
    input.addEventListener('keydown', function (ev) {
      ev.stopPropagation();
      if (ev.key === 'Enter') {
        ev.preventDefault();
        input.blur();
      }
    });
    input.addEventListener('blur', save);
    return h('label.lib__metarow', null, h('span.lib__metakey', { text: label }), input);
  }

  function filesPanel(one) {
    var panel = h('section.lib__panel', null, h('div.lib__paneltitle', { text: '文件' }));
    panel.appendChild(
      h(
        'div.lib__file',
        null,
        h('span.lib__filerole', { text: '主文件' }),
        h('span.lib__filepath', { text: one.path, title: one.path }),
        h('span.lib__filesize', { text: ui.fmtBytes ? ui.fmtBytes(one.bytes) : one.bytes + ' B' })
      )
    );
    (one.assets || []).forEach(function (path) {
      panel.appendChild(
        h(
          'div.lib__file',
          null,
          h('span.lib__filerole.lib__filerole--asset', { text: '附属' }),
          h('span.lib__filepath', { text: path, title: path })
        )
      );
    });
    if (!(one.assets || []).length) {
      panel.appendChild(h('div.lib__hint', { text: '这一份没有外部附属资源（PDF 里嵌的图不算）。' }));
    }
    return panel;
  }

  function textPanel(one) {
    var state_ = one.text || { state: 'none' };
    var panel = h('section.lib__panel', null, h('div.lib__paneltitle', { text: '抽出来的正文' }));
    if (state_.state === 'none') {
      panel.appendChild(
        h('div.lib__hint', {
          text: state_.attempted
            ? '抓过了，但这类文件（代码、纯文本以外的格式）没有可抽的正文 —— 元数据与文件名照样能搜到它。'
            : '还没抓。点左下角「抓正文」——PDF 抽取慢（实测约 2 秒一份），会分批推进。'
        })
      );
      return panel;
    }
    panel.appendChild(
      h('div.lib__hint', {
        text: state_.chars + ' 字 · 字母/汉字占比 ' + Math.round((state_.ratio || 0) * 100) + '% · 真实词密度 ' +
          (state_.words === undefined ? '—' : Math.round(state_.words) + '/千字')
      })
    );
    if (one.text.head) {
      panel.appendChild(h('pre.lib__text', { text: one.text.head }));
    }
    return panel;
  }

  function renderSide() {
    ui.clear(el.side);
    if (!state.citekey || !state.detail) return;
    var one = state.detail;

    var panel = h('section.lib__panel', null, h('div.lib__paneltitle', { text: '谁引用了它' }));
    var cited = one.citedBy || [];
    if (!cited.length) {
      panel.appendChild(
        h('div.lib__hint', {
          text: '没有笔记引用它。在笔记里写 ' + one.citekey + ' 或 ' + one.rel.split('/').pop() + ' 就算引用（判定按字面来，不做模糊匹配）。'
        })
      );
    } else {
      cited.forEach(function (hit) {
        panel.appendChild(
          h(
            'div.lib__cited',
            null,
            h('div.lib__citedtitle', { text: hit.title || hit.path }),
            h('div.lib__citedpath', { text: hit.path + '（命中：' + hit.hit + '）' })
          )
        );
      });
    }
    el.side.appendChild(panel);

    var bib = h('section.lib__panel', null, h('div.lib__paneltitle', { text: 'BibTeX' }));
    bib.appendChild(h('pre.lib__text.lib__text--bib', { text: one.bibtex || '' }));
    el.side.appendChild(bib);
  }

  // ------------------------------------------------------------------ 动作

  function addRootPrompt() {
    var input = h('input.lib__field', { type: 'text', value: '', placeholder: '例如 /mnt/f/Documents' });
    ui.modal({
      title: '添加资料目录',
      size: 'sm',
      body: h('div', null, h('div.lib__hint', { text: '只加进清单，不动目录里的任何文件。' }), input),
      actions: [
        { label: '取消', kind: 'ghost', onClick: function (close) { close(); } },
        {
          label: '加上',
          kind: 'primary',
          onClick: function (close) {
            var path = input.value.trim();
            close();
            if (!path) return;
            api
              .post('/library/roots', { action: 'add', path: path })
              .then(function () {
                ui.toast('已加入清单', 'ok');
                loadRoots(true);
              })
              .catch(fail);
          }
        }
      ]
    });
    setTimeout(function () { input.focus(); }, 0);
  }

  /** 抓正文：分批循环，随时能停。 */
  function toggleIndex() {
    if (state.indexing) {
      state.indexing = false;
      renderBar();
      return;
    }
    state.indexing = true;
    var done = 0;
    renderBar();
    var step = function () {
      if (!state.indexing) return;
      api
        .post('/library/index', { limit: 3 })
        .then(function (res) {
          done += (res.done || []).length;
          state.status = '已抓 ' + done + ' 份' + (res.remaining ? '，还剩 ' + res.remaining : '');
          // 边抓边把"待抓"数往下走：不然进度只是个数字，看不出还剩多少
          var still = (res.remaining || 0) > 0;
          renderBar(still ? state.items.length : undefined);
          if (still) renderPending(res.done || []);
          if (res.remaining && (res.done || []).length) {
            setTimeout(step, 30);
            return;
          }
          state.indexing = false;
          state.status = '抓完了：这一轮 ' + done + ' 份';
          renderBar();
          loadItems();
          if (state.citekey) loadDetail(state.citekey);
        })
        .catch(function (err) {
          state.indexing = false;
          fail(err);
        });
    };
    step();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
