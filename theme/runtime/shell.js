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
    { view: 'home', label: '个人中心', icon: 'cpu' },
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
  // 顺序照用户给的：主页 · 对话 · 练习 · 资料（从上到下）。
  // 2026-11-07：笔记页与资源树一起删了（笔记改为将来直接桥接本机 Obsidian），
  // 主页暂时是一个空白页 —— 这颗入口留着，内容后面再填。
  var RAIL = [
    { href: 'workbench.html', label: '主页', icon: 'grip', page: 'workbench' },
    { href: 'chat.html', label: '对话', icon: 'robot', page: 'chat' },
    { href: 'quiz.html', label: '练习中心', icon: 'play', pages: ['quiz', 'graph'],
      hint: '练习 · 组卷 · 复习 · 图谱' },
    { href: 'library.html', label: '资料', icon: 'book', page: 'library' },
    // 错题本**不在这儿**：它是"练什么"的一部分，入口归练习中心（见 NAV 最后一项）
  ];

  function railButton(item, page) {
    // 当前页**高亮**而不是隐藏：图标条上少一个图标，比"点了没反应"更让人困惑
    // 一个条目可以对应多个页面（练习中心就兼着图谱页）
    var on = item.pages ? item.pages.indexOf(page) >= 0 : page === item.page;
    var node = h('a.rail__btn' + (on ? '.is-active' : ''),
      { href: item.href, title: item.label + (item.hint ? ' · ' + item.hint : ''), 'aria-label': item.label });
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
      // 顶栏整条收掉（见 app.css 的 `body[data-topbar='off']`）：
      // 空着一条 bar 在对话/笔记/资料页上，用户的原话是"这个 bar 是多余的"。
      document.body.dataset.topbar = 'off';
      return;
    }
    nav.hidden = false;
    document.body.dataset.topbar = 'on';
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
    if (!btn) return;
    btn.addEventListener('click', openSettings);
    // 设置是一个页面了：在这一页上把齿轮点亮（活动栏其余项由 renderRail 负责高亮）
    if ((document.body.dataset.page || '') === 'settings') btn.classList.add('is-active');
  }

  /* ======================================================== 设置
   *
   * 2026-11-07 起它是**独立页面**（settings.html，版式见 runtime/settings.js），
   * 不再原地弹浮窗：设置项多、每一项都带说明，一张弹窗塞不下、滚起来也难受。
   * `openSettings()` 只负责导航；表单本身仍由这里造（`buildSettings()`），
   * 页面与别处调用的都是同一份，不会两处漂移。
   *
   * 图谱页不用它：那一页的设置是画布上的浮动面板（见 graph.js），
   * 只管力导向参数，与这里这四段（AI / 联网搜索 / 外观 / 数据）不是一回事。
   */
  function openSettings() {
    if ((document.body.dataset.page || '') === 'settings') return;   // 已经在这页
    window.location.href = 'settings.html';
  }

  /** 把设置表单造成一个 `.form` 节点（四段：AI / 联网搜索 / 外观 / 数据）。
   *  改动即写 store，没有"保存"按钮。 */
  function buildSettings() {
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
      placeholder: 'sk-…（存在本机）',
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

    // **用本机自己那份密钥**：填好之后存在本机库里，下次打开就在。
    // 服务端只做转发（不少模型供应商不允许浏览器直连），不持有任何共享密钥。
    var connectionBlock = h('div', null,
      h('div.form__grid', { style: { marginTop: '12px' } },
        field('接口地址（OpenAI 兼容）', '例如 https://api.deepseek.com/v1', baseUrlInput),
        field('模型名', '例如 deepseek-chat / gpt-4o-mini', modelInput),
        field('上下文预算（token）', '越大能带进的对话越多；小窗口模型可调低，服务端另设上限', contextInput)),
      h('div', { style: { marginTop: '12px' } },
        field('API 密钥',
          '你自己的密钥，存在本机库里（不随仓库走）。用量按天记在本机，方便看花了多少。',
          apiKeyInput)));

    // **现在能不能用**：2026-11-07 起没有内测通道了，只有"填了密钥"与"没填"两态。
    var channelRow = h('p', {
      text: '正在读取…',
      style: { margin: '-4px 0 10px', fontSize: '12px', lineHeight: '1.6', color: 'var(--fg2)' },
    });
    api
      .get('/ai/usage')
      .then(function (st) {
        var mode = (st && st.mode) || '';
        var mine = (st && st.model) || '';
        channelRow.textContent =
          mode === 'user'
            ? '当前：已配置' + (mine ? ' · ' + mine : '')
            : '当前：还没配 —— 对话与批改都用不了，填上密钥即可。';
      })
      .catch(function () {
        channelRow.textContent = '';
      });

    var aiForm = h('div.form__section', null,
      h('div.form__sectiontitle', { text: 'AI（批改与对话）' }),
      h('p', {
        text: '密钥填在下面；也可以直接改 config/settings.local.json 的 ai 块（可拷贝，见「数据」段）。',
        style: { margin: '-2px 0 8px', fontSize: '12px', lineHeight: '1.6', color: 'var(--fg3)' },
      }),
      channelRow,
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

    /* ------------------------------------------------------ 联网搜索
     *
     * 给 AI 一对"搜 + 读"的手（实现见 `api/app/websearch.py`）——
     * 它的知识有截止日期，而库里那几路检索全在**本机**，问到外面的事就答不了。
     *
     * 服务商清单**从服务端拿**、不在这儿抄一份：加一家只改后端一处，而且"哪几家
     * 能用、默认地址是什么"本来就该由后端说了算（它才知道接口形状）。
     */
    var searchConf = conf.search || {};
    var providerEndpoints = {};
    var providerKeyless = {};
    var searchStatus = h('p', {
      text: '正在读取当前状态…',
      style: { margin: '-4px 0 10px', fontSize: '12px', lineHeight: '1.6', color: 'var(--fg2)' },
    });
    var searchEndpointInput = h('input.input.input--mono', {
      type: 'text',
      spellcheck: 'false',
      placeholder: '留空 = 用该服务商的默认地址',
      value: searchConf.endpoint || '',
      onInput: function (event) {
        store.saveSettings({ search: { endpoint: event.target.value.trim() } });
      },
    });
    var searchKindSelect = h('select.select', {
      onChange: function (event) {
        var kind = event.target.value;
        // 切服务商时**把接口地址清空**：那个框留空 = 用该家的默认地址（见后端
        // `websearch._normalize`）。不清的话上一家的地址会被带过去 ——
        // 一个"看着改了、其实没有"的错。
        searchEndpointInput.value = '';
        searchEndpointInput.setAttribute(
          'placeholder', '留空 = ' + (providerEndpoints[kind] || '用默认地址'));
        store.saveSettings({ search: { kind: kind, endpoint: '' } });
      },
    });
    var searchKeyInput = h('input.input.input--mono', {
      type: 'password',
      autocomplete: 'off',
      placeholder: '留空就用免密钥那条',
      value: searchConf.apiKey || '',
      onInput: function (event) {
        store.saveSettings({ search: { apiKey: event.target.value.trim() } });
      },
    });
    api
      .get('/chat/websearch')
      .then(function (st) {
        var list = st.providers || [];
        list.forEach(function (one) {
          providerEndpoints[one.kind] = one.endpoint;
          providerKeyless[one.kind] = !!one.keyless;
          searchKindSelect.appendChild(
            h('option', { value: one.kind, text: one.label + (one.keyless ? ' —— 免密钥' : '') }));
        });
        // 没存过就选**服务端列表的第一个**（那家就是后端的默认，见 `websearch.PROVIDERS`）——
        // 不在这儿写死一个名字，免得两边各有一份默认值。
        var want = String(searchConf.kind || '');
        if (!providerEndpoints[want]) want = list.length ? list[0].kind : '';
        searchKindSelect.value = want;
        if (providerEndpoints[want]) {
          searchEndpointInput.setAttribute('placeholder', '留空 = ' + providerEndpoints[want]);
        }
        // **实际在走哪条路**由服务端说 —— 光看这个框空着就说"没配置"，
        // 会让人对着一个其实能用的功能找问题。
        searchStatus.textContent = st.ready
          ? '当前：可以联网 · ' + (st.label || '')
            + (st.source === 'keyless' ? ' · 不用配任何东西' : ' · 你自己的密钥')
          : '当前：用不了 —— ' + (st.message || '');
      })
      .catch(function () {
        searchStatus.textContent = '';
      });

    var searchForm = h('div.form__section', null,
      h('div.form__sectiontitle', { text: '联网搜索' }),
      h('p', {
        // 这一段是**纯文本**（旁边 AI 那段也是），不解析 Markdown —— 别在这儿写 `**强调**`，
        // 星号会原样显示出来。
        text: '让 AI 能上外网查它自己不知道的东西（最新的版本、现在的推荐做法、'
          + '某个数字的出处）。它只读网页，不动你的任何数据；引用时会把网址写出来'
          + '给你核对。',
        style: { margin: '-2px 0 8px', fontSize: '12px', lineHeight: '1.6', color: 'var(--fg3)' },
      }),
      searchStatus,
      switchRow('启用联网搜索', '关掉之后这一组工具不再声明给模型 —— 它就不会去搜。',
        searchConf.enabled !== false, function (value) {
          store.saveSettings({ search: { enabled: value } });
        }),
      h('div.form__grid', { style: { marginTop: '12px' } },
        field('服务商', '默认那条免密钥；往下三家要各自申请一把密钥', searchKindSelect),
        field('接口地址', '一般留空 —— 只有自建网关时才需要改', searchEndpointInput)),
      h('div', { style: { marginTop: '12px' } },
        field('API 密钥', '只有选收费那三家才要填。填了就用你的（额度、稳定性、更长的摘要）；'
          + '留空就走免密钥那条（必应，一次最多 10 条，个人小量够用）。',
          searchKeyInput)));

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

    /* ------------------------------------------------------ Obsidian（笔记）
     *
     * 笔记已经不在 quizforge 里存了：读写都走本机 Obsidian 的桥接。
     * 传输是**vault 里的一个信箱目录**（不走网络 —— WSL 的网络不可靠，见
     * docs/Obsidian桥接协议.md）。没连上时，对话里那五个笔记工具直接不可用。
     */
    var obsConf = conf.obsidian || {};
    var vaultInput = h('input.input.input--mono', {
      type: 'text',
      spellcheck: 'false',
      placeholder: '例如 F:\\Notes 或 /mnt/f/Notes',
      value: obsConf.vaultPath || '',
      onInput: function (event) {
        store.saveSettings({ obsidian: { vaultPath: event.target.value.trim() } });
        // 路径一改，上一次的检测结果就不作数了
        if (obsStatus) obsStatus.textContent = '还没检测。';
      },
    });
    var obsStatus = h('p', {
      text: '还没检测。',
      style: { margin: '-4px 0 10px', fontSize: '12px', lineHeight: '1.6', color: 'var(--fg2)' },
    });
    var obsForm = h('div.form__section', null,
      h('div.form__sectiontitle', { text: 'Obsidian（笔记）' }),
      h('p', {
        text: '笔记不在 quizforge 里存。这里填 Obsidian vault 的路径，再到 Obsidian 里启用'
          + '「QuizForge 桥接」插件；两边通过 vault 里的一个信箱目录通信，不走网络。'
          + '没连上时，对话里的笔记读写工具就用不了。',
        style: { margin: '-2px 0 8px', fontSize: '12px', lineHeight: '1.6', color: 'var(--fg3)' },
      }),
      field('vault 路径', 'Windows 路径（F:\\Notes）与 WSL 路径（/mnt/f/Notes）都认', vaultInput),
      h('div.btnrow', { style: { marginTop: '12px' } },
        h('button.btn', {
          type: 'button',
          onClick: function () {
            obsStatus.textContent = '正在检测…';
            // 带上输入框里的值：刚打完还没同步到后端，也能立刻检测
            api.get('/obsidian/status?path=' + encodeURIComponent(vaultInput.value || '')).then(function (st) {
              obsStatus.textContent = st && st.ok
                ? '已连上：' + (st.vault || 'vault')
                  + (st.pluginVersion ? ' · 插件 ' + st.pluginVersion : '')
                : '没连上：' + ((st && st.error) || '未知原因');
            }).catch(function (err) {
              obsStatus.textContent = '没连上：' + ((err && err.message) || err);
            });
          },
        }, h('span', { html: ui.icon('refresh', 15) }), h('span', { text: '检测连接' }))),
      obsStatus);

    var stats = store.stats();
    /* 题库生成时间：设置页**不装载题库**（那是几 MB，为一个时间戳不值），
     * 所以 `QF.data.meta` 是空的。已装载题库的页面直接用现成的值；
     * 没装载就取轻量的 `/bank/version` 补上（同一个来源，只是不带题目）。 */
    var bankAtRow = h('div.statline', null,
      h('span', { text: '题库生成时间' }),
      h('b', { text: (D && D.generatedAt) || '读取中…' }));
    if (!(D && D.generatedAt)) {
      api.get('/bank/version')
        .then(function (v) {
          bankAtRow.querySelector('b').textContent = (v && v.importedAt) || '未知';
        })
        .catch(function () {
          bankAtRow.querySelector('b').textContent = '未知';
        });
    }

    /* ------------------------------------------------------ 设置文件
     * 整份设置住在 `config/settings.local.json`（可拷贝，类比 VS Code 的 settings.json）。
     * CLI/脚本改它 → 这里打开就看得到；这里改完 → 也写回去（见 store.saveSettings）。 */
    var settingsFileRow = h('div.statline', null,
      h('span', { text: '设置文件' }),
      h('b', { text: '读取中…' }));
    api.get('/settings').then(function (out) {
      settingsFileRow.querySelector('b').textContent = (out && out.file) || '（还没建）';
    }).catch(function () {
      settingsFileRow.querySelector('b').textContent = '读不到';
    });

    function copySettings() {
      var text = JSON.stringify(store.settings(), null, 2);
      if (!navigator.clipboard || !navigator.clipboard.writeText) {
        ui.toast('这个浏览器没有剪贴板接口', 'error');
        return;
      }
      navigator.clipboard.writeText(text).then(function () {
        ui.toast('整份设置已复制（可直接给新用户）', 'ok');
      }, function () {
        ui.toast('复制失败：浏览器不给剪贴板权限', 'error');
      });
    }

    function importSettings() {
      if (!navigator.clipboard || !navigator.clipboard.readText) {
        ui.toast('这个浏览器读不了剪贴板：直接改设置文件更快', 'error');
        return;
      }
      navigator.clipboard.readText().then(function (text) {
        var data = JSON.parse(text);
        if (!data || typeof data !== 'object') throw new Error('不是 JSON 对象');
        store.adoptSettings(data);
        // 导入是**整份替换**（与平时的 patch 合并区分开）
        return api.put('/settings', { settings: store.settings(), replace: true });
      }).then(function () {
        ui.toast('已用剪贴板里的设置覆盖', 'ok');
        window.setTimeout(function () { window.location.reload(); }, 400);
      }).catch(function (err) {
        ui.toast('导入失败：' + ((err && err.message) || err), 'error');
      });
    }

    var dataForm = h('div.form__section', null,
      h('div.form__sectiontitle', { text: '数据' }),
      h('div.statline', null, h('span', { text: '本地数据体积' }), h('b', { text: ui.fmtBytes(stats.storageBytes) })),
      h('div.statline', null, h('span', { text: '存储状态' }), h('b', { text: store.available ? 'localStorage 可用' : '不可用（内存模式，刷新会丢失）' })),
      bankAtRow,
      // 构建戳：`make web` 之后数字就该变，拿来一眼核对"我看的是不是最新那份"
      h('div.statline', null, h('span', { text: '前端构建戳' }), h('b', { text: myBuild() || '未知' })),
      // 整份设置文件：路径 + 复制 / 从剪贴板导入
      settingsFileRow,
      h('div.btnrow', { style: { marginTop: '10px' } },
        h('button.btn', {
          type: 'button',
          onClick: copySettings,
        }, h('span', { html: ui.icon('download', 15) }), h('span', { text: '复制设置 JSON' })),
        h('button.btn', {
          type: 'button',
          onClick: importSettings,
        }, h('span', { html: ui.icon('upload', 15) }), h('span', { text: '从剪贴板导入设置' }))),
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

  return h('div.form', null, aiForm, searchForm, obsForm, appearanceForm, dataForm);
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
        /* 除了"这一页的构建戳"，还要盯**常驻 Python 壳**的指纹。
           壳的页面由服务端现渲染（`tools.shell_page()`），它的环境准备里装着要装一次的
           东西（matplotlib、中文字体）—— 而改服务端**不会**动 `make web` 的构建戳。
           只盯构建戳的话：改了壳模板 → 页面不刷新 → 页面里的壳还是旧的 → 实测就是这样
           绕了一圈（用户以为中文装好了，壳里跑出来"一个中文字形都没有"，模型很诚实地
           回答"这台机器画不出汉字"）。基准值来自建壳那一刻（chat.js 写在
           `data-qf-shell` 上）；没建过壳的页面没有它，就跳过这一条。 */
        var shellMine = document.documentElement.getAttribute('data-qf-shell') || '';
        var shellTheirs = (info && info.shell) || '';
        if (shellMine && shellTheirs && shellTheirs !== shellMine && !busyEditing()) {
          location.reload();
          return;
        }
        var page = (QF.config && QF.config.page) || '';
        var theirs = ((info && info.pageStamps) || {})[page] || '';
        if (!theirs || theirs === mine || busyEditing()) return;
        location.reload();
      }).catch(function () { /* 服务没在跑：下一轮再说 */ });
    }, BUILD_POLL_MS);
  }

  /** 骨架 → 内容 的那一次交接。
   *
   *  用户的原话："在五个主页面之间转来转去的时候，有了一点过渡感，但是渲染的时候
   *  还是会闪一下。"—— 那"一下"就是这里：顶栏/活动栏是静态的，页面正文先是
   *  `bootstate` 骨架（HTML 里就有的），等鉴权与数据回来之后被整块换掉。
   *
   *  **这次交接只能有一套动画**，两套一起上就是"整屏白闪一下"：
   *  启动时 `app.js` 的 `render()` 第一次进来会走 `ui.viewSwap`（同文档过渡，
   *  浏览器把骨架屏留成快照、内容在它上面淡入 —— 底下**一直有东西**，不露底色），
   *  而这里又给 `#app-root` 加一条从 `opacity: 0` 淡进来的动画。后者的底是
   *  **页面底色**（加这个类的时候骨架屏已经不在 `#app-root` 里了），于是它演起来
   *  就是白屏一闪。更糟的是两者错开：过渡 250ms 先演完（内容已经清清楚楚），
   *  淡入这时才开始 —— 实测 `#app-root` 的透明度在启动后 **334ms 掉到 31%** 再爬回。
   *  用户那句"任何页面刷新都会导致画面闪烁一下"、"图谱/错题本到其他页面切换会闪"
   *  都是它（只有这两个方向差别最大：深色画布/长列表撞上白底）。
   *
   *  所以：**有同文档过渡就让它一个人做完**，`qf-painted` 留给没有那套 API
   *  （或系统关掉了动效）的场合当兜底 —— 那种情况下没有过渡，淡入仍然是好的。
   *
   *  只做**一次**（`done` 守卫）：页面自己后续的重画（换笔记、翻页）不该跟着闪 ——
   *  那些是"局部换内容"，闪一下反而像卡顿。
   */
  function revealFirstPaint() {
    var root = document.getElementById('app-root');
    if (!root || !window.MutationObserver) return;
    var done = false;
    var check = function () {
      if (done) return;
      var box = document.getElementById('app-root');
      if (!box) return;
      if (box.querySelector('.bootstate')) return;      // 还停在骨架上：再等
      if (!box.childElementCount) return;                // 空的：再等
      done = true;
      if (QF.ui && QF.ui.usedViewTransition && QF.ui.usedViewTransition()) return;
      box.classList.add('qf-painted');
      window.setTimeout(function () {
        box.classList.remove('qf-painted');
      }, 320);
    };
    new MutationObserver(check).observe(root, { childList: true, subtree: true });
    check();
  }

  /* 注：**页面之间的过渡不在这里** —— 主力是浏览器自己的跨文档 View Transitions
   *（入场券内联在 `theme/shell.html` 的 `<head>`，拦截逻辑在 `ui.js` 的
   * `leaveWithFade`）。只有测不到那条 at-rule 的浏览器才退回"自己淡出 120ms →
   * 跳 → 新页淡入"那两半。
   * 我一度在这儿又写了一套（`.is-leaving` + `qf-page-in`），结果是死代码：
   * `ui.js` 那份先 `preventDefault`，我的判断 `if (ev.defaultPrevented) return` 直接放行。
   * 已经删掉；"切换不够丝滑"的另一半真因是**首帧被同步脚本推迟**，见 `build_web.py`
   * 的 `_script_tag`（全都加了 `defer`）。 */

  QF.shell = {
    mount: function (opts) {
      var out = mount(opts);
      revealFirstPaint();
      return out;
    },
    pullSettings: pullSettings,
    build: myBuild,
    watchBuild: watchBuild,
  };
  // `open` 是导航（各页的入口都还这么调）；`build` 给设置页装配用（见 runtime/settings.js）
  QF.settings = { open: openSettings, build: buildSettings };
})();
