/* ===========================================================================
 * shell.js —— 顶栏外壳（全站共享）
 *
 * 这一块以前在三个页面里各写了一遍：主导航、主题按钮、设置按钮、
 * 「当前页图标不显示」。三份必然漂移，实测漂出两个后果：
 *
 *   1. 设置面板长在 app.js 里，于是「设置」只存在于刷题页。从错题本或图谱
 *      点设置要先整页跳到 quiz.html#settings，关掉再整页跳回来 —— 图谱
 *      好不容易算稳的布局与视角，在那一跳里全丢了。
 *   2. 知识图谱页把自己的导航换成了「刷题 / 错题本 / 知识图谱」，
 *      原来的「工作台 / 练习 / 组卷 / 复习」不见了 —— 同一块顶栏，
 *      三个页面三种结构。
 *
 * 现在它是运行时的一部分（每个页面都加载）：页面只需 mount 一次，
 * 要么什么都不用说（非刷题页），要么告诉它「当前视图 + 点了怎么切」（刷题页）。
 * ========================================================================= */
(function () {
  'use strict';

  var QF = (window.QF = window.QF || {});
  var ui = QF.ui;
  var h = ui.h;
  var api = QF.api;
  var store = QF.store;
  var ai = QF.ai;
  var D = QF.data;

  /* ------------------------------------------------------------ 主导航 */
  /* 这四个是**视图**（刷题页内部的四种状态），不是四个页面。
     所以在刷题页点它是原地切换，在别的页面点是带着 hash 回刷题页。 */
  var NAV = [
    { view: 'home', label: '工作台', icon: 'cpu' },
    { view: 'practice', label: '练习', icon: 'play' },
    { view: 'paper', label: '组卷', icon: 'list' },
    { view: 'review', label: '复习', icon: 'refresh', badge: true },
    // 题库图谱归练习中心：它不是一个独立去处，而是"练什么"的一部分。
    // 带 href 的项渲染成链接（图谱有自己的页面），其余三项是原地切视图。
    { view: 'graph', label: '图谱', icon: 'target', href: 'graph.html' },
    // 错题本是一个独立页面（不是刷题页内部的一个视图），所以带 href 渲染成链接，
    // 与"图谱"同一套。它属于练习中心 —— 用户说"这个错题本应该在练习中心而不是
    // 出现在（活动栏）这里"。
    { view: 'wrongbook', label: '错题本', icon: 'flag', href: 'wrongbook.html' },
  ];

  /* ------------------------------------------------------------ 活动栏 */
  /* 最左那条窄图标条。原先这些页面入口挤在顶栏里（页面一多就挤不下，
     而且每个页面还会各自把顶栏改成自己的样子），现在归到这儿 ——
     顶栏于是能瘦下来，把位置让给"打开的标签"。 */
  var RAIL = [
    { key: 'side', label: '资源', icon: 'grip', hint: '收起 / 展开资源栏（Alt+B）' },
    { href: 'quiz.html', label: '练习中心', icon: 'play', pages: ['quiz', 'graph'],
      hint: '练习 · 组卷 · 复习 · 图谱' },
    { href: 'chat.html', label: '对话', icon: 'robot', page: 'chat' },
    { href: 'library.html', label: '资料', icon: 'book', page: 'library' },
    { href: 'notes.html', label: '笔记', icon: 'list', page: 'notes' },
    // 错题本**不在这儿**：它是"练什么"的一部分，入口归练习中心（见 NAV 最后一项）
  ];

  var SIDE_KEY = 'qf.side.open';

  function sideOpen() {
    return document.body.dataset.side !== 'closed';
  }

  /** 窄屏下资源栏是**浮层抽屉**（app.css 的 1180px 断点：position fixed + z-index 70）。
   *  这时候它是盖在内容上的，不是并排 —— 所以窄屏不能沿用"上次开着"的习惯。 */
  function drawerMode() {
    return window.innerWidth <= 1180;
  }

  function setSide(open, persist) {
    document.body.dataset.side = open ? 'open' : 'closed';
    // 首屏那次**不写本机**：窄屏默认收起只是"这一屏该怎么摆"，
    // 不该把大屏上的习惯一起改掉（不然回到宽窗口，资源栏就再也默认不开了）
    if (persist !== false) {
      try { localStorage.setItem(SIDE_KEY, open ? '1' : '0'); } catch (e) { /* 无痕模式忽略 */ }
    }
    var btn = document.querySelector('#rail-nav .rail__btn[data-key="side"]');
    if (btn) {
      btn.classList.toggle('is-on', open);
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
  }

  function railButton(item, page) {
    var node;
    if (item.key === 'side') {
      node = h('button.rail__btn', { type: 'button', 'data-key': 'side',
        title: item.label + ' · ' + item.hint, 'aria-label': item.label });
    } else {
      // 当前页**高亮**而不是隐藏：图标条上少一个图标，比"点了没反应"更让人困惑
      // 一个条目可以对应多个页面（练习中心就兼着图谱页）
      var on = item.pages ? item.pages.indexOf(page) >= 0 : page === item.page;
      node = h('a.rail__btn' + (on ? '.is-active' : ''),
        { href: item.href, title: item.label + (item.hint ? ' · ' + item.hint : ''), 'aria-label': item.label });
    }
    node.innerHTML = ui.icon(item.icon, 17);
    return node;
  }

  function renderRail() {
    var box = document.getElementById('rail-nav');
    if (!box) return;
    var page = document.body.dataset.page || '';
    ui.clear(box);
    RAIL.forEach(function (item) { box.appendChild(railButton(item, page)); });
  }

  /** 资源栏的状态：读回上次的选择，并把两个入口（图标条那颗、资源栏右上角那个箭头）接上 */
  function bindSide() {
    var saved = null;
    try { saved = localStorage.getItem(SIDE_KEY); } catch (e) { /* 同上 */ }
    // 窄屏一律先收起：抽屉默认开着就等于一进来拿 264px 盖住右边的内容
    // （实测 1000px 宽时正好压在画布那两行字上，看着像"内容被裁了"）
    setSide(!drawerMode() && saved !== '0', false);

    var box = document.getElementById('rail-nav');
    if (box && !box.dataset.sideBound) {
      box.dataset.sideBound = '1';
      box.addEventListener('click', function (ev) {
        var btn = ev.target && ev.target.closest ? ev.target.closest('[data-key="side"]') : null;
        if (!btn) return;
        ev.preventDefault();
        setSide(!sideOpen());
      });
    }
    // 拖窗口跨过断点（并排 <-> 抽屉）时重新摆一次：抽屉模式一进入就收起，
    // 免得"并排时开着、缩窄后变成一块盖住内容的浮层"
    var wasDrawer = drawerMode();
    window.addEventListener('resize', function () {
      var now = drawerMode();
      if (now === wasDrawer) return;
      wasDrawer = now;
      setSide(false, false);
    });
    document.addEventListener('keydown', function (ev) {
      if (!ev.altKey || ev.ctrlKey || ev.metaKey) return;
      if (ev.key === 'b' || ev.key === 'B') { ev.preventDefault(); setSide(!sideOpen()); }
    });
  }

  function hrefFor(view) {
    // 离线单文件没有 router.js，退回不带 hash 的地址
    return 'quiz.html' + (QF.router && QF.router.hashFor ? QF.router.hashFor(view) : '');
  }

  function navItem(item, opts) {
    var active = opts.view === item.view;
    // 图标用偶数尺寸：15px 会让图标落在半像素上
    var nodes = [h('span', { html: ui.icon(item.icon, 16) }), h('span', { text: item.label })];
    if (item.badge) {
      var due = store && store.dueIds ? store.dueIds().length : 0;
      if (due) nodes.push(h('span.nav-item__badge', { text: String(due) }));
    }
    // 带 href 的项一律是链接（如"图谱"另有一页）；其余按调用方给不给 onPick 决定
    if (opts.onPick && !item.href) {
      return h('button', {
        type: 'button',
        class: 'nav-item' + (active ? ' is-active' : ''),
        'aria-current': active ? 'page' : null,
        onClick: function () { opts.onPick(item.view); },
      }, nodes);
    }
    return h('a', {
      class: 'nav-item' + (active ? ' is-active' : '') + (item.href ? ' nav-item--link' : ''),
      href: item.href || hrefFor(item.view),
      'aria-current': active ? 'page' : null,
    }, nodes);
  }

  function renderNav(opts) {
    var nav = document.getElementById('mainnav');
    if (!nav) return;
    ui.clear(nav);
    // 只有练习中心那一族会给出 `view`（刷题页给 view + onPick，图谱页只给 view）。
    // 别的页面这一栏**留空** —— 这几个是练习中心**内部**的分段，不该出现在每页顶栏上：
    // 顶栏曾经在八个页面里都摆着"工作台/练习/组卷/复习"，而活动栏上还有一个入口，
    // 同一件事出现两遍就是这个毛病。
    if (!opts || !opts.view) {
      nav.hidden = true;
      return;
    }
    nav.hidden = false;
    NAV.forEach(function (item) { nav.appendChild(navItem(item, opts)); });
  }

  /* ------------------------------------------------------------ 顶栏动作 */

  function bindTheme() {
    var btn = document.getElementById('btn-theme');
    if (!btn || !ui.theme) return;
    btn.addEventListener('click', function () {
      ui.theme.toggle();
      if (store) store.saveSettings({ theme: ui.theme.current() });
    });
  }

  function bindSettings() {
    var btn = document.getElementById('btn-settings');
    if (btn) btn.addEventListener('click', openSettings);
  }

  /* ======================================================== 设置面板
   *
   * 它现在是全站共享的：任何页面点「设置」都是**在原地弹出来**，
   * 不跳页、不重载、不动当前页面正在做的事（图谱的布局、练习的作答都在）。
   * 面板里的改动都即时写进 store，关掉即生效。
   *
   * 图谱页不用它：那一页的设置是画布上的浮动面板（见 graph.js），
   * 只管力导向参数，与这里的三段（AI / 外观 / 数据）不是一回事。
   */
  function openSettings() {
    var conf = store.settings();
    var aiConf = conf.ai;
    var resultBox = h('div');

    function field(label, hint, node) {
      return h('label.field', null, h('span.field__label', { text: label }), node,
        hint ? h('span.field__hint', { text: hint }) : null);
    }

    function switchRow(title, desc, value, onToggle) {
      var sw = h('button.switch', {
        type: 'button',
        class: value ? 'is-on' : '',
        role: 'switch',
        'aria-checked': value ? 'true' : 'false',
        onClick: function () {
          var next = !sw.classList.contains('is-on');
          sw.classList.toggle('is-on', next);
          sw.setAttribute('aria-checked', next ? 'true' : 'false');
          onToggle(next);
        },
      });
      return h('div.switchrow', null,
        h('div.switchrow__text', null,
          h('div.switchrow__title', { text: title }),
          desc ? h('div.switchrow__desc', { text: desc }) : null),
        sw);
    }

    var apiKeyInput = h('input.input.input--mono', {
      type: 'password',
      autocomplete: 'off',
      placeholder: 'sk-…（保存在你的账号里）',
      value: aiConf.apiKey || '',
      onInput: function (event) {
        store.saveSettings({ ai: { apiKey: event.target.value.trim() } });
      },
    });
    var baseUrlInput = h('input.input.input--mono', {
      type: 'text',
      spellcheck: 'false',
      value: aiConf.baseUrl || '',
      onInput: function (event) {
        store.saveSettings({ ai: { baseUrl: event.target.value.trim() } });
      },
    });
    var modelInput = h('input.input.input--mono', {
      type: 'text',
      spellcheck: 'false',
      value: aiConf.model || '',
      onInput: function (event) {
        store.saveSettings({ ai: { model: event.target.value.trim() } });
      },
    });
    // 对话的上下文预算：小窗口模型调低它，长材料才塞得下而不是被服务端截断
    var contextInput = h('input.input.input--mono', {
      type: 'number',
      min: '1000',
      step: '1000',
      placeholder: '默认 8000',
      value: aiConf.maxContextTokens || '',
      onInput: function (event) {
        var value = parseInt(event.target.value, 10);
        store.saveSettings({ ai: { maxContextTokens: isNaN(value) ? 0 : value } });
      },
    });

    // **每个用户用自己的密钥**：填好之后存在自己的账号里，换设备不用重填。
    // 服务端只做转发（不少模型供应商不允许浏览器直连），不持有任何共享密钥。
    var connectionBlock = h('div', null,
      h('div.form__grid', { style: { marginTop: '12px' } },
        field('接口地址（OpenAI 兼容）', '例如 https://api.deepseek.com/v1', baseUrlInput),
        field('模型名', '例如 deepseek-chat / gpt-4o-mini', modelInput),
        field('上下文预算（token）', '越大能带进的对话越多；小窗口模型可调低，服务端另设上限', contextInput)),
      h('div', { style: { marginTop: '12px' } },
        field('API 密钥',
          '你自己的密钥，保存在你的账号里 —— 换设备不用重填。本站不提供共享密钥，用量记在你的账上。',
          apiKeyInput)));

    var aiForm = h('div.form__section', null,
      h('div.form__sectiontitle', { text: 'AI（批改与对话）' }),
      h('p', {
        text: '优先级：你自己填的密钥 > 本站的内测通道。不填密钥就用内测通道（站长的那份额度）。',
        style: { margin: '-2px 0 8px', fontSize: '12px', lineHeight: '1.6', color: 'var(--fg3)' },
      }),
      switchRow('启用 AI', '关闭时简答题提交后直接显示参考答案，由你自己判断对错；对话页也不再可用。', aiConf.enabled, function (value) {
        store.saveSettings({ ai: { enabled: value } });
      }),
      connectionBlock,
      switchRow('要求模型返回 JSON', '若供应商不支持 response_format=json_object，会自动去掉该参数重试。', aiConf.jsonMode !== false, function (value) {
        store.saveSettings({ ai: { jsonMode: value } });
      }),
      switchRow('失败时降级为自评', 'AI 不可用时仍然可以按参考答案自评并排入复习计划。', aiConf.fallbackSelfRate !== false, function (value) {
        store.saveSettings({ ai: { fallbackSelfRate: value } });
      }),
      h('div.btnrow', { style: { marginTop: '12px' } },
        h('button.btn', {
          type: 'button',
          onClick: function () {
            var btn = this;
            ui.clear(resultBox);
            resultBox.appendChild(h('div.statline', null, h('span.spinner'), h('span', { text: '正在测试连接…' })));
            ai.ping().then(function (res) {
              ui.clear(resultBox);
              if (res.ok) {
                resultBox.appendChild(h('div.statline', { style: { color: 'var(--ok)' } },
                  h('span', { html: ui.icon('check', 15) }),
                  h('span', { text: '连接成功 · ' + res.latencyMs + ' ms · 模型 ' + res.model + ' · 回显「' + res.sample + '」' })));
              } else {
                resultBox.appendChild(h('div.statline', { style: { color: 'var(--bad)' } },
                  h('span', { html: ui.icon('warn', 15) }),
                  h('span', { text: res.message })));
              }
              resultBox.appendChild(h('div.staturl', { text: '请求地址：' + res.url }));
            });
            window.setTimeout(function () { btn.blur(); }, 0);
          },
        }, h('span', { html: ui.icon('spark', 15) }), h('span', { text: '测试连接' }))),
      resultBox,
      h('p.form__note', {
        style: { marginTop: '12px' },
        html:
          '提示：浏览器直连模型接口需要该服务允许跨域（CORS），并且密钥会出现在页面请求中——' +
          '仅在你自己信任的本机环境使用。<br>若接口不允许跨域，可改用本地服务（如 <code>ollama</code>，' +
          '地址填 <code>http://localhost:11434/v1</code>）并通过 <code>OLLAMA_ORIGINS=*</code> 放开跨域。',
      }));

    var fontScale = h('input.slider', {
      type: 'range',
      min: '0.85',
      max: '1.35',
      step: '0.05',
      value: String(conf.fontScale || 1),
      onInput: function (event) {
        var value = parseFloat(event.target.value);
        store.saveSettings({ fontScale: value });
        document.documentElement.style.setProperty('--font-scale', String(value));
      },
    });
    var maxWidth = h('input.slider', {
      type: 'range',
      min: '680',
      max: '1200',
      step: '20',
      value: String(conf.maxWidth || 880),
      onInput: function (event) {
        var value = parseInt(event.target.value, 10);
        store.saveSettings({ maxWidth: value });
        document.documentElement.style.setProperty('--content-max', value + 'px');
      },
    });

    var appearanceForm = h('div.form__section', null,
      h('div.form__sectiontitle', { text: '外观' }),
      switchRow('浅色主题', '长时间阅读公式与代码时，可切换到低强度浅色配色。', conf.theme === 'light', function (value) {
        ui.theme.set(value ? 'light' : 'dark');
        // 和旁边那几行一样走设置仓库：只写本机的话，下次进页面会被服务端那份按回去
        if (store) store.saveSettings({ theme: ui.theme.current() });
      }),
      field('字号缩放', null, fontScale),
      field('内容最大宽度（像素）', null, maxWidth));

    var stats = store.stats();
    var dataForm = h('div.form__section', null,
      h('div.form__sectiontitle', { text: '数据' }),
      h('div.statline', null, h('span', { text: '本地数据体积' }), h('b', { text: ui.fmtBytes(stats.storageBytes) })),
      h('div.statline', null, h('span', { text: '存储状态' }), h('b', { text: store.available ? 'localStorage 可用' : '不可用（内存模式，刷新会丢失）' })),
      h('div.statline', null, h('span', { text: '题库生成时间' }), h('b', { text: (D && D.generatedAt) || '未知' })),
      // 构建戳：`make web` 之后数字就该变，拿来一眼核对"我看的是不是最新那份"
      h('div.statline', null, h('span', { text: '前端构建戳' }), h('b', { text: myBuild() || '未知' })),
      h('div.btnrow', { style: { marginTop: '12px' } },
        h('button.btn', {
          type: 'button',
          onClick: function () {
            downloadJSON('quizforge-progress-' + ui.dayKey() + '.json', store.exportAll());
            ui.toast('已导出进度数据', 'ok');
          },
        }, h('span', { html: ui.icon('download', 15) }), h('span', { text: '导出全部数据' })),
        h('button.btn', {
          type: 'button',
          onClick: function () {
            pickFile(function (text) {
              try {
                var payload = JSON.parse(text);
                var merged = store.importAll(payload);
                ui.toast('已导入 ' + merged.records + ' 条记录', 'ok');
                // 数据变了，让当前页面自己刷新 —— 面板不该知道页面长什么样
                notify('imported');
              } catch (err) {
                ui.toast('导入失败：' + err.message, 'error');
              }
            });
          },
        }, h('span', { html: ui.icon('upload', 15) }), h('span', { text: '导入数据' })),
        h('button.btn.btn--danger', {
          type: 'button',
          onClick: function () {
            ui.confirm('将清空本机所有作答记录、错题与复习计划，且无法恢复。确定继续吗？', {
              okLabel: '清空',
              danger: true,
            }).then(function (ok) {
              if (!ok) return;
              store.resetAll();
              ui.toast('已清空本机数据', 'ok');
              notify('reset');
            });
          },
        }, h('span', { html: ui.icon('trash', 15) }), h('span', { text: '清空进度' }))));

    ui.modal({
      title: '设置',
      size: 'lg',
      body: h('div.form', null, aiForm, appearanceForm, dataForm),
      actions: [{ label: '完成', kind: 'primary' }],
    });
  }

  /* 数据被导入或清空之后广播一次：页面各自的反应不一样
     （刷题页重画、清空后回工作台；错题本重画列表），面板不替它们决定。 */
  function notify(kind) {
    document.dispatchEvent(new CustomEvent('qf:data-changed', { detail: { kind: kind } }));
  }

  function downloadJSON(filename, payload) {
    var blob = new Blob([JSON.stringify(payload, null, 1)], { type: 'application/json' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () {
      URL.revokeObjectURL(url);
    }, 1000);
  }

  function pickFile(onLoad) {
    var input = document.createElement('input');
    input.type = 'file';
    input.accept = '.json,application/json';
    input.onchange = function () {
      var file = input.files && input.files[0];
      if (!file) return;
      var reader = new FileReader();
      reader.onload = function () {
        onLoad(String(reader.result));
      };
      reader.onerror = function () {
        ui.toast('读取文件失败', 'error');
      };
      reader.readAsText(file);
    };
    input.click();
  }

  /* ------------------------------------------------------------ 挂载 */

  var bound = false;

  /**
   * mount({ view, onPick })
   *
   *   view / onPick  只有刷题页给：给它就渲染成「原地切视图」的按钮，
   *                  不给就渲染成带 hash 的链接（点了回刷题页的对应视图）。
   *
   * 事件只绑一次：刷题页每次 render 都会 mount 一遍导航，
   * 每遍都绑一次的话，点一下主题会切两下。
   */
  function mount(opts) {
    opts = opts || {};
    renderRail();
    if (!bound) {
      bound = true;
      bindTheme();
      bindSettings();
      bindSide();
      watchBuild();
      // 工具挂载开关现在长在活动栏里，所以由外壳拉起（原来只有对话页调它）
      if (QF.mounts && QF.mounts.load) QF.mounts.load();
    }
    renderNav(opts);
  }

  /* ------------------------------------------------------- 设置（主题等） */

  var settingsPull = null;

  /**
   * 把服务端那份设置取回来 —— **每个页面取一次**，拿回来之后主题才定。
   *
   * 练习中心 / 错题本 / 对话这三页不用调：`boot.js` 的 `GET /progress` 顺手就把
   * 设置灌进来了。其余几页没有 `boot.js`，不自己取就会退回兜底值 ——
   * 兜底是 `dark`，于是"练习中心浅色、工作台深色"这种同一浏览器两副面孔。
   *
   * 取不到（服务挂了、离线）也不闹：用本机那一份，页面照常画。
   */
  function pullSettings() {
    if (settingsPull) return settingsPull;
    settingsPull = api
      .get('/progress')
      .then(function (snapshot) {
        if (store && store.hydrateSettings) store.hydrateSettings(snapshot);
        return true;
      })
      .catch(function () {
        return false;
      })
      .then(function (ok) {
        ui.theme.init();
        return ok;
      });
    return settingsPull;
  }

  /* ------------------------------------------------- 跟上新构建（开发纪律） */

  /* 改了 `theme/` 要 `make web`，产物换了，可**已经打开的那一页不会自己知道** ——
     用户看到的还是旧的，还以为没改。这事真发生过，所以让页面自己盯着：
     每几秒问一次轻接口 `/api/build`，发现"我这一页"的指纹变了就自己刷新。 */
  var BUILD_POLL_MS = 3000;

  /** 这一页是从哪一次构建来的（构建时写在 head 里的 meta，见 tools/build_web.py） */
  function myBuild() {
    var meta = document.querySelector('meta[name="qf-build"]');
    return meta ? meta.getAttribute('content') || '' : '';
  }

  /** 正在编辑就先不刷 —— 把用户正在写的东西冲掉，比"晚几秒更新"糟得多 */
  function busyEditing() {
    var el = document.activeElement;
    if (el && (/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || el.isContentEditable)) return true;
    var modal = document.getElementById('modal-root');
    return !!(modal && modal.childElementCount);
  }

  function watchBuild() {
    var mine = myBuild();
    // 没有这个 meta（很老的产物）就什么都不做：不拿猜出来的东西当成依据
    if (!mine || watchBuild.timer) return;
    watchBuild.timer = setInterval(function () {
      if (document.visibilityState !== 'visible') return;
      api.get('/build').then(function (info) {
        var page = (QF.config && QF.config.page) || '';
        var theirs = ((info && info.pageStamps) || {})[page] || '';
        if (!theirs || theirs === mine || busyEditing()) return;
        location.reload();
      }).catch(function () { /* 服务没在跑：下一轮再说 */ });
    }, BUILD_POLL_MS);
  }

  QF.shell = { mount: mount, pullSettings: pullSettings, build: myBuild, watchBuild: watchBuild };
  QF.settings = { open: openSettings };
})();
