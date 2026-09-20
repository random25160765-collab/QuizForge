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

  //: 类别标签**以服务端为准**（`GET /library/kinds` ← `app.library.KIND_LABEL`）。
  //: 这里这份只是首屏兜底 —— 抄第二份就会有一天对不上，而那种对不上很难查。
  var KIND_LABEL = {
    paper: '论文',
    manual: '手册',
    spec: '规范',
    report: '报告',
    book: '书',
    slides: '幻灯片',
    webpage: '网页',
    blog: '博客',
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
    view: null,        // 正在看的那份文件（/library/view 的返回）
    viewing: false,
    roots: [],
    items: [],
    query: '',
    citekey: '',
    detail: null,
    indexing: false,
    judging: false,
    kinds: [],
    status: '',
    // 左右两栏的开合（open | closed）。这是"我的习惯"不是"这次的状态" → 记本机。
    panels: { list: 'open', side: 'open' }
  };

  var el = {};

  // ------------------------------------------------------------------ 启动

  function boot() {
    // 这一页不加载 boot.js：设置（主题、工具挂载、资料根）得自己取一次
    if (QF.shell && QF.shell.pullSettings) QF.shell.pullSettings();
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
    el.classifyBtn = document.getElementById('library-classify');
    el.metaBtn = document.getElementById('library-meta');
    el.upload = document.getElementById('library-upload');
    el.file = document.getElementById('library-file');
    el.prog = document.getElementById('library-progress');
    el.progFill = document.getElementById('library-progressfill');
    el.main = document.getElementById('library-main');
    el.side = document.getElementById('library-side');
    el.tools = document.getElementById('library-tools');

    // 上次怎么摆的，这次照旧
    try {
      var saved = JSON.parse(window.localStorage.getItem('qf.library.panels') || '{}') || {};
      if (saved.list === 'open' || saved.list === 'closed') state.panels.list = saved.list;
      if (saved.side === 'open' || saved.side === 'closed') state.panels.side = saved.side;
    } catch (err) {
      /* 读不到就用默认（两栏都开着） */
    }
    applyPanels();
    renderTools();

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
    el.metaBtn.addEventListener('click', toggleMeta);
    el.classifyBtn.addEventListener('click', classifyPrompt);
    // 上传：用户自己把文件丢进来，剩下的（抽正文 + 模型判元数据）自动接着做
    el.upload.addEventListener('click', function () {
      if (el.file) el.file.click();
    });
    el.file.addEventListener('change', onPicked);
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
    loadKinds();
    loadRoots();
  }

  function loadKinds() {
    api
      .get('/library/kinds')
      .then(function (res) {
        state.kinds = res.kinds || [];
        var map = {};
        state.kinds.forEach(function (one) { map[one.key] = one.label; });
        if (Object.keys(map).length) KIND_LABEL = map;
      })
      .catch(function () {
        /* 拿不到就用兜底那份，不值得打扰用户 */
      });
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
        // 文件处理这层缺什么（抽不出文字时要拿它解释原因）
        state.toolchain = res.toolchain || { components: [] };
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
    // "还需要判"的计数：还没让模型判过的（`pending` / 老的 `inferred`）
    var needMeta = state.items.filter(function (one) {
      return (one.origin === 'pending' || one.origin === 'inferred') && !one.authors.length;
    }).length;
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
    el.classifyBtn.textContent = '模型归类';
    el.metaBtn.classList.toggle('is-on', state.judging);
    el.metaBtn.textContent = state.judging ? '停下' : '判元数据';
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
            // "这一条是模型判的"不再标：**现在全是模型判的**（元数据整条管线交给模型），
            // 一个每行都有的标记等于没有信息（用户："这个信息现在就变成冗余了"）。
            // 真正有信息量的是**例外**：人手工改过的、以及还没判过的。
            one.origin === 'manual' ? h('span.lib__mark.lib__mark--manual', { text: '人定' }) : null,
            one.origin === 'pending' || one.origin === 'inferred'
              ? h('span.lib__mark.lib__mark--warn', { text: '待判' })
              : null,
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
    // **换条目要退出查看态**：不然点另一个条目时，面板还显示着上一份文件，
    // 而且看不出是"没换"还是"换了但内容一样"（实测就是这么卡住的）。
    state.view = null;
    state.viewing = false;
    if (state.citekey === citekey && state.detail) return;
    state.citekey = citekey;
    // 换条目：三栏一起换内容，做一次过渡
    ui.swap(function () {
      renderList();
      renderMain();
      renderSide();
    }, el.root);
    loadDetail(citekey);
  }

  /** 左右两栏：收起不是"删掉"，是栅格那一列变 0 宽（栏本身留在 DOM 里、只是看不见）。
   *  踩过同款坑：用 `display: none` 收起来会让栅格子项少一个，后面那一列整体前移
   *  —— 主区会被塞进 0 宽那一格里（笔记页实测正文只剩 16px）。 */
  function applyPanels() {
    if (!el.root) return;
    el.root.setAttribute('data-lib-list', state.panels.list);
    el.root.setAttribute('data-lib-side', state.panels.side);
  }

  function togglePanel(which) {
    state.panels[which] = state.panels[which] === 'open' ? 'closed' : 'open';
    applyPanels();
    renderTools();
    try {
      window.localStorage.setItem('qf.library.panels', JSON.stringify(state.panels));
    } catch (err) {
      /* 存不下就只在这次生效 */
    }
  }

  /** 工具条上那两颗：左边管左栏、右边管右栏，位置就是它们管的那个方向。
   *  两个状态两个朝向，之间是**转过去**的（不是换一张图那样闪一下）。 */
  function panelBtn(which, label) {
    var open = state.panels[which] === 'open';
    // 左栏那颗：开着时箭头朝左（点它 = 把它往左边收），收起时反过来
    var base = which === 'list' ? 'chevronL' : 'chevronR';
    var btn = h('button.lib__fold' + (open ? '.is-on' : ''), {
      type: 'button',
      html: ui.icon(base, 15),
      style: open ? null : { transform: 'rotate(180deg)' },
      title: (open ? '收起' : '展开') + label,
      'aria-label': (open ? '收起' : '展开') + label,
      'aria-expanded': open ? 'true' : 'false',
      onClick: function () {
        togglePanel(which);
      }
    });
    return btn;
  }

  function renderTools() {
    if (!el.tools) return;
    ui.clear(el.tools);
    el.tools.appendChild(panelBtn('list', '文件列表'));
    el.tools.appendChild(h('span.lib__toolssp'));
    el.tools.appendChild(panelBtn('side', '引用与出处'));
  }

  function renderMain() {
    ui.clear(el.main);
    if (state.view || state.viewing) {
      el.main.appendChild(viewPanel());
      if (!state.viewing) renderSide();
      return;
    }
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
        // 同上：不标"模型填的"（那是常态），只标例外
        h('span.lib__origin.lib__origin--' + (one.origin || 'pending'), {
          text:
            {
              manual: '元数据：人定的',
              pending: '元数据：还没判（用模型判一下）',
              inferred: '元数据：还没判（用模型判一下）',
            }[one.origin || 'pending'] || ''
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
    grid.appendChild(kindRow(one.kind));
    grid.appendChild(metaRow('主题', 'topics', (one.topics || []).join(', '), 'text', '逗号分隔，取自目录'));
    if (one.arxiv) grid.appendChild(metaRow('arXiv', 'arxiv', one.arxiv, 'text'));
    panel.appendChild(grid);
    return panel;
  }

  /** 类型用下拉：类别是有限集合，手打只会打错（`spec` 打成 `specs` 这一次归类就白判了）。 */
  function kindRow(current) {
    var select = h('select.lib__field', { 'aria-label': '类型' });
    Object.keys(KIND_LABEL).forEach(function (key) {
      var option = h('option', { value: key, text: KIND_LABEL[key] + '（' + key + '）' });
      if (key === current) option.selected = true;
      select.appendChild(option);
    });
    select.addEventListener('change', function () {
      api
        .post('/library/meta', { citekey: state.citekey, meta: { kind: select.value } })
        .then(function () {
          ui.toast('类型已更新（记为「人定的」）', 'ok');
          loadDetail(state.citekey);
          loadItems();
        })
        .catch(fail);
    });
    select.addEventListener('keydown', function (ev) { ev.stopPropagation(); });
    return h('label.lib__metarow', null, h('span.lib__metakey', { text: '类型' }), select);
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
    function fileRow(role, path, size, index, asset) {
      return h(
        'div.lib__file',
        null,
        h('span.lib__filerole' + (asset ? '.lib__filerole--asset' : ''), { text: role }),
        h('span.lib__filepath', { text: path, title: path }),
        size ? h('span.lib__filesize', { text: ui.fmtBytes ? ui.fmtBytes(size) : size + ' B' }) : null,
        h('button.lib__open', {
          type: 'button',
          text: '查看',
          title: '在这一页里打开它（PDF 用浏览器自带阅读器；md / docx / pptx 换成能读的样子）',
          onClick: function () {
            openViewer(index);
          }
        })
      );
    }

    panel.appendChild(fileRow('主文件', one.path, one.bytes, -1, false));
    (one.assets || []).forEach(function (path, index) {
      panel.appendChild(fileRow('附属', path, 0, index, true));
    });
    if (!(one.assets || []).length) {
      panel.appendChild(h('div.lib__hint', { text: '这一份没有外部附属资源。' }));
    }
    return panel;
  }

  function textPanel(one) {
    var state_ = one.text || { state: 'none' };
    var panel = h('section.lib__panel', null, h('div.lib__paneltitle', { text: '抽出来的正文' }));
    if (state_.state === 'none') {
      // 说清"为什么没有"：是还没抓、是这类文件本来就没正文、还是**缺外部工具**
      var missing = ((state.toolchain || {}).components || []).filter(function (one) {
        return !one.available;
      });
      var why = state_.attempted
        ? '抓过了，但这份没有可抽的正文（代码、纯文本以外的格式，或者文档本身是空的）—— 元数据与文件名照样能搜到它。'
        : '还没抓。点左下角「抓正文」——PDF 抽取慢（实测约 2 秒一份），会分批推进。';
      if (missing.length) {
        why += ' 另外，这台机器上还缺：' + missing.map(function (one) {
          return one.which ? one.which : one.binary;
        }).join('、') + '（' + missing.map(function (one) {
          return one.why;
        }).join('；') + '）。缺的组件在清单里钉好哈希后会自动取一次。';
      }
      panel.appendChild(h('div.lib__hint', { text: why }));
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

  /** 打开一份文件：先去问后端"它该怎么看"，再换到查看面板。 */
  function openViewer(index) {
    if (!state.citekey) return;
    state.viewing = true;
    state.view = null;
    renderMain();
    api
      .get('/library/view?citekey=' + encodeURIComponent(state.citekey) + '&index=' + index)
      .then(function (data) {
        state.viewing = false;
        state.view = data;
        renderMain();
      })
      .catch(function (err) {
        state.viewing = false;
        state.view = { kind: 'error', name: '', note: (err && err.message) || '打不开这份文件' };
        renderMain();
      });
  }

  /** 查看面板：PDF 用 iframe（浏览器自带的阅读器最省事也最好用），
   * 图片用 img，md 用与笔记页**同一个**渲染器，docx 用后端转好的 HTML，
   * pptx 铺成一页页，其余照实说看不了并给个下载。 */
  function viewPanel() {
    var one = state.view || {};
    var pane = h('div.lib__viewer');

    pane.appendChild(
      h(
        'div.lib__viewbar',
        null,
        h('button.lib__back', {
          type: 'button',
          text: '← 返回条目',
          onClick: function () {
            state.view = null;
            renderMain();
          }
        }),
        h('span.lib__viewname', { text: one.name || '' , title: one.path || '' }),
        h('span.lib__viewkind', { text: VIEW_LABEL[one.kind] || one.kind || '' }),
        one.raw
          ? h('a.lib__download', { href: one.raw, download: one.name || '', text: '下载' })
          : null
      )
    );

    var body = h('div.lib__viewbody');
    if (state.viewing) {
      body.appendChild(h('div.lib__hint', { text: '正在打开…' }));
    } else {
      // 正文交给共用模块 —— 与窗格里装"一份文件"用的是同一份渲染。
      // 原先这里自己画了一遍，于是成了两份：一处改了另一处不会跟着改，
      // 而且"资料页能看、窗格里看不了"。已经取到的返回直接喂进去，不重复请求。
      QF.docview.paint(body, one);
    }
    pane.appendChild(body);
    return pane;
  }

  var VIEW_LABEL = {
    pdf: 'PDF',
    image: '图片',
    markdown: 'Markdown',
    docx: 'Word 文档',
    pptx: '幻灯片',
    text: '纯文本',
    legacy: '老式二进制（看不了）',
    binary: '二进制',
    error: '打不开'
  };

  function renderSide() {
    ui.clear(el.side);
    if (!state.citekey || !state.detail) return;
    var one = state.detail;

    var panel = h('section.lib__panel', null, h('div.lib__paneltitle', { text: '谁引用了它' }));
    var cited = one.citedBy || [];
    if (!cited.length) {
      panel.appendChild(
        h('div.lib__hint', {
          text: '没有笔记引用它。在笔记里写 ' + one.citekey + ' 或 ' + one.rel.split('/').pop() + ' 就算引用。'
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

  /** 把选中的目录加进清单（系统选择器与手输两条路都走这里）。 */
  function addRoot(path) {
    return api
      .post('/library/roots', { action: 'add', path: path })
      .then(function () {
        ui.toast('已加入清单', 'ok');
        loadRoots(true);
      })
      .catch(fail);
  }

  /** 添加资料目录：**先弹系统的文件夹选择器**（用户连着说了两遍）。
   *
   *  为什么要这样：路径是这个界面里最不该让人手打的东西 —— 打错一个字符就是
   *  "根目录不存在"，而"选一个目录"在系统里本来就是点两下的事。
   */
  function addRootPrompt() {
    api
      .post('/library/pick-folder', {})
      .then(function (res) {
        if (res && res.path) {
          addRoot(res.path);
          return;
        }
        if (res && res.cancelled) return;   // 用户按了取消：什么都不做，别报错
        // 这台上没有图形选择器（容器 / SSH / 无桌面环境）：退回手输，并把**原因**说清 ——
        // 静默失败的话，用户只会以为按钮坏了
        promptRootByHand((res && res.why) || '这台机器上没有可用的文件夹选择器。');
      })
      .catch(fail);
  }

  function promptRootByHand(why) {
    var input = h('input.lib__field', { type: 'text', value: '', placeholder: '例如 /mnt/f/Documents' });
    ui.modal({
      title: '手动填写资料目录',
      size: 'sm',
      body: h(
        'div',
        null,
        h('div.lib__hint', { text: why }),
        h('div.lib__hint', { text: '只加进清单，不动目录里的任何文件。' }),
        input
      ),
      actions: [
        { label: '取消', kind: 'ghost', onClick: function (close) { close(); } },
        {
          label: '加上',
          kind: 'primary',
          onClick: function (close) {
            var path = input.value.trim();
            close();
            if (!path) return;
            addRoot(path);
          }
        }
      ]
    });
    setTimeout(function () { input.focus(); }, 0);
  }

  /** 模型归类：调模型拿建议 → 人来复核 → 落盘（与笔记那套「建议-接受」同一套规矩）。 */
  function classifyPrompt() {
    var button = el.classifyBtn;
    button.disabled = true;
    button.textContent = '问模型…';
    api
      .post('/library/classify', {})
      .then(function (res) {
        button.disabled = false;
        button.textContent = '模型归类';
        showProposals(res);
      })
      .catch(function (err) {
        button.disabled = false;
        button.textContent = '模型归类';
        fail(err);
      });
  }

  function showProposals(res) {
    var items = res.items || [];
    var picked = {};
    if (!items.length) {
      ui.toast(res.note || '没有需要归类的条目（人定过与模型定过的都不再送）', 'ok');
      return;
    }
    items.forEach(function (one) { picked[one.citekey] = true; });
    var body = h('div.lib__review');
    body.appendChild(
      h('div.lib__hint', {
        text: '模型给了 ' + items.length + ' 条建议。勾上要接受的（默认全勾）；写进元数据时会记成「模型填的」，' +
          '人手改过的那些**不会被覆盖**。'
      })
    );
    items.forEach(function (one) {
      var check = h('input', { type: 'checkbox' });
      check.checked = true;
      check.addEventListener('change', function () { picked[one.citekey] = check.checked; });
      body.appendChild(
        h(
          'label.lib__reviewrow',
          null,
          check,
          h(
            'span.lib__reviewmain',
            null,
            h('span.lib__reviewtitle', { text: one.rel || one.citekey }),
            h(
              'span.lib__reviewkind',
              null,
              h('span.lib__kind', { text: KIND_LABEL[one.was] || one.was || '—' }),
              h('span.lib__reviewarrow', { text: '→' }),
              h('span.lib__kind.lib__kind--' + one.kind, { text: KIND_LABEL[one.kind] || one.kind }),
              (one.topics || []).length ? h('span.lib__reviewtopics', { text: one.topics.join(' · ') }) : null
            ),
            one.why ? h('span.lib__reviewwhy', { text: one.why }) : null
          )
        )
      );
    });
    if ((res.failed || []).length) {
      body.appendChild(
        h('div.lib__hint', {
          text: '这几份模型没给结论，下次再试：' +
            res.failed.map(function (one) { return one.rel || one.citekey; }).join('、')
        })
      );
    }
    if ((res.dropped || []).length) {
      body.appendChild(
        h('div.lib__hint', { text: '有 ' + res.dropped.length + ' 条引用键对不上库里，已丢掉（宁可少改，不可乱改）。' })
      );
    }
    ui.modal({
      title: '模型归类 · ' + items.length + ' 条建议',
      size: 'lg',
      body: body,
      actions: [
        { label: '取消', kind: 'ghost', onClick: function (close) { close(); } },
        {
          label: '接受勾选的',
          kind: 'primary',
          onClick: function (close) {
            var chosen = items.filter(function (one) { return picked[one.citekey]; });
            close();
            if (!chosen.length) return;
            api
              .post('/library/classify/apply', { items: chosen })
              .then(function (out) {
                ui.toast(
                  '已写入 ' + (out.written || []).length + ' 条' +
                    ((out.skipped || []).length ? '，跳过 ' + out.skipped.length + ' 条' : ''),
                  'ok'
                );
                loadItems();
                if (state.citekey) loadDetail(state.citekey);
              })
              .catch(fail);
          }
        }
      ]
    });
  }

  // ---------------------------------------------------------------- 进度条

  /** 画一条**确定性**进度（`done/total`）。不是转圈 —— 用户要的是"还剩多少"。 */
  function setProgress(done, total, label) {
    if (!el.prog || !el.progFill) return;
    if (!total) {
      el.prog.hidden = true;
      return;
    }
    el.prog.hidden = false;
    var pct = Math.max(0, Math.min(100, Math.round((done / total) * 100)));
    el.progFill.style.width = pct + '%';
    el.prog.title = (label || '处理中') + '：' + done + ' / ' + total + '（' + pct + '%）';
  }

  function clearProgress() {
    if (el.prog) el.prog.hidden = true;
    if (el.progFill) el.progFill.style.width = '0%';
  }

  // ---------------------------------------------------------------- 判元数据

  /** 判元数据：分批循环（后端是并发问模型的），随时能停。 */
  function toggleMeta() {
    if (state.judging) {
      state.judging = false;
      renderBar();
      return;
    }
    state.judging = true;
    var changed = 0;
    var failed = 0;
    var total = state.items.filter(function (one) {
      return (one.origin === 'pending' || one.origin === 'inferred') && !one.authors.length;
    }).length;
    if (!total) total = Math.max(1, state.items.length);   // 免得除以 0
    renderBar();
    var step = function () {
      if (!state.judging) {
        state.status = '停下了；已经判过的不会丢';
        clearProgress();
        renderBar();
        return;
      }
      api
        .post('/library/meta/llm', { limit: 12, scope: 'pending' })
        .then(function (res) {
          changed += (res.changed || []).length;
          failed += (res.failed || []).length;
          var left = res.remaining || 0;
          setProgress(changed + failed, changed + failed + left, '判元数据');
          state.status =
            '已判 ' + changed + ' 条' + (failed ? '（失败 ' + failed + '）' : '') + (left ? '，还剩 ' + left : '');
          renderBar();
          if (left) {
            setTimeout(step, 30);
            return;
          }
          state.judging = false;
          state.status =
            '判完了：这一轮 ' + changed + ' 条' + (failed ? '，失败 ' + failed + ' 条（可再点一次重试）' : '');
          clearProgress();
          renderBar();
          loadItems();
          if (state.citekey) loadDetail(state.citekey);
        })
        .catch(function (err) {
          state.judging = false;
          clearProgress();
          renderBar();
          fail(err);
        });
    };
    step();
  }

  // ---------------------------------------------------------------- 上传

  /** 上传完**自动**接着处理：抓正文（含判元数据）。
   *
   *  用户的原话是"用户上传一个文件，或者上传一批文件，然后 llm 自动处理" ——
   *  所以这里传完不撒手，直接接着跑；进度条上看得见两件事合成的一条线。
   */
  function onPicked() {
    var files = Array.prototype.slice.call((el.file && el.file.files) || []);
    if (!files.length) return;
    var dir =
      el.pick && el.pick.value && el.pick.value !== '__add__' ? el.pick.value : (state.roots[0] || '');
    if (!dir) {
      ui.toast('先选一个资料根（左上角那个下拉）', 'warn');
      return;
    }
    state.status = '正在上传 ' + files.length + ' 个文件…';
    renderBar();
    var ok = 0;
    var next = function (index) {
      if (index >= files.length) {
        el.file.value = '';
        state.status = '上传完 ' + ok + ' 个，自动接着处理';
        renderBar();
        ui.toast('已上传 ' + ok + ' 个，正在抽正文与判元数据…', 'ok');
        if (!state.indexing) toggleIndex();
        return;
      }
      setProgress(index, files.length, '上传');
      api
        .upload('/library/upload', files[index], { dir: dir })
        .then(function () {
          ok += 1;
          next(index + 1);
        })
        .catch(function (err) {
          ui.toast((files[index] || {}).name + ' 上传失败：' + err.message, 'warn');
          next(index + 1);
        });
    };
    next(0);
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
      if (!state.indexing) {
        state.status = '停下了；已经抓到的不会丢';
        clearProgress();
        renderBar();
        return;
      }
      api
        .post('/library/index', { limit: 12, force: false })
        .then(function (res) {
          done += (res.done || []).length;
          var left = res.remaining || 0;
          // 一次请求里"抽正文"与"判元数据"是一起做的（后端并发），所以进度就一条
          setProgress(done, done + left, '抓正文 + 判元数据');
          state.status =
            '已抓 ' + done + ' 份' + (left ? '，还剩 ' + left : '') + (res.hint ? '（' + res.hint + '）' : '');
          // 边抓边把"待抓"数往下走：不然进度只是个数字，看不出还剩多少
          var still = left > 0;
          renderBar(still ? state.items.length : undefined);
          if (still) renderPending(res.done || []);
          if (res.remaining && (res.done || []).length) {
            setTimeout(step, 30);
            return;
          }
          state.indexing = false;
          state.status = '抓完了：这一轮 ' + done + ' 份' + (res.hint ? '；' + res.hint : '');
          clearProgress();
          renderBar();
          loadItems();
          if (state.citekey) loadDetail(state.citekey);
        })
        .catch(function (err) {
          state.indexing = false;
          clearProgress();
          renderBar();
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
