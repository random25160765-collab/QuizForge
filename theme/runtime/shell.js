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
  ];

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
    if (opts.onPick) {
      return h('button', {
        type: 'button',
        class: 'nav-item' + (active ? ' is-active' : ''),
        'aria-current': active ? 'page' : null,
        onClick: function () { opts.onPick(item.view); },
      }, nodes);
    }
    return h('a', {
      class: 'nav-item' + (active ? ' is-active' : ''),
      href: hrefFor(item.view),
    }, nodes);
  }

  function renderNav(opts) {
    var nav = document.getElementById('mainnav');
    if (!nav) return;
    ui.clear(nav);
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

  /* 当前页对应的图标指向自己，点了没反应会显得像坏了 —— 不显示。
     页面名取自 body[data-page]（shell.html 注入），两套构建下都有值。 */
  function hideSelfIcons() {
    var page = document.body.dataset.page || '';
    var self = { graph: 'btn-graph', wrongbook: 'btn-wrongbook' }[page];
    if (!self) return;
    var btn = document.getElementById(self);
    if (btn) btn.hidden = true;
  }

  /* ======================================================== 设置面板
   *
   * 它现在是全站共享的：任何页面点「设置」都是**在原地弹出来**，
   * 不跳页、不重载、不动当前页面正在做的事（图谱的布局、练习的作答都在）。
   * 面板里的改动都即时写进 store，关掉即生效。
   *
   * 图谱页不用它：那一页的设置是画布上的浮动面板（见 graph.js），
   * 只管力导向参数，与这里的三段（AI 批改 / 外观 / 数据）不是一回事。
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

    // **每个用户用自己的密钥**：填好之后存在自己的账号里，换设备不用重填。
    // 服务端只做转发（不少模型供应商不允许浏览器直连），不持有任何共享密钥。
    var connectionBlock = h('div', null,
      h('div.form__grid', { style: { marginTop: '12px' } },
        field('接口地址（OpenAI 兼容）', '例如 https://api.deepseek.com/v1', baseUrlInput),
        field('模型名', '例如 deepseek-chat / gpt-4o-mini', modelInput)),
      h('div', { style: { marginTop: '12px' } },
        field('API 密钥',
          '你自己的密钥，保存在你的账号里 —— 换设备不用重填。本站不提供共享密钥，AI 批改的用量记在你的账上。',
          apiKeyInput)));

    var aiForm = h('div.form__section', null,
      h('div.form__sectiontitle', { text: 'AI 批改（简答题）' }),
      switchRow('启用 AI 批改', '关闭时简答题提交后直接显示参考答案，由你自己判断对错。', aiConf.enabled, function (value) {
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
      }),
      field('字号缩放', null, fontScale),
      field('内容最大宽度（像素）', null, maxWidth));

    var stats = store.stats();
    var dataForm = h('div.form__section', null,
      h('div.form__sectiontitle', { text: '数据' }),
      h('div.statline', null, h('span', { text: '本地数据体积' }), h('b', { text: ui.fmtBytes(stats.storageBytes) })),
      h('div.statline', null, h('span', { text: '存储状态' }), h('b', { text: store.available ? 'localStorage 可用' : '不可用（内存模式，刷新会丢失）' })),
      h('div.statline', null, h('span', { text: '题库生成时间' }), h('b', { text: (D && D.generatedAt) || '未知' })),
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
    if (!bound) {
      bound = true;
      bindTheme();
      bindSettings();
      hideSelfIcons();
    }
    renderNav(opts);
  }

  QF.shell = { mount: mount };
  QF.settings = { open: openSettings };
})();
