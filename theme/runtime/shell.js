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
    // 日程排在主页之后、对话之前：它是"今天要做什么"那一层，
    // 与"跟 AI 说话"是两条不同的入口（用户："主页之外再加一颗日程图标，主页继续留白"）。
    { href: 'schedule.html', label: '日程', icon: 'calendar', page: 'schedule' },
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

  /* ------------------------------------------------------------ 手机底栏 */
  /* 与 RAIL 的去处一致，只是顺序按拇指可达性排：对话 → 练习 → 资料 → 主页 → 日程。
     * 图谱**不单列**：它一直是"练什么"的一部分（桌面上练习中心就兼着图谱页），
       手机上更不能为它再占一格 —— 练习项用 `pages` 让图谱页也高亮。
     * 设置也不在底栏：它归主页，见 runtime/home.js 的 `home__settings`。 */
  var TABS = [
    { href: 'chat.html', label: '对话', icon: 'robot', page: 'chat' },
    { href: 'quiz.html', label: '练习', icon: 'play', pages: ['quiz', 'graph'] },
    { href: 'library.html', label: '资料', icon: 'book', page: 'library' },
    { href: 'workbench.html', label: '主页', icon: 'grip', page: 'workbench' },
    { href: 'schedule.html', label: '日程', icon: 'calendar', page: 'schedule' },
  ];

  function renderTabbar() {
    var box = document.getElementById('tabbar');
    if (!box) return;
    var page = document.body.dataset.page || '';
    ui.clear(box);
    TABS.forEach(function (item) {
      // 与 railButton 同一套判定：一个入口可以对应多个页面
      var on = item.pages ? item.pages.indexOf(page) >= 0 : page === item.page;
      var node = h('a.tabbar__btn' + (on ? '.is-active' : ''),
        { href: item.href, 'aria-label': item.label });
      if (on) node.setAttribute('aria-current', 'page');
      node.innerHTML = ui.icon(item.icon, 22) + '<span>' + item.label + '</span>';
      box.appendChild(node);
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
      placeholder: '例如 https://api.deepseek.com/v1',
      value: aiConf.baseUrl || '',
      onInput: function (event) {
        store.saveSettings({ ai: { baseUrl: event.target.value.trim() } });
      },
    });
    var modelInput = h('input.input.input--mono', {
      type: 'text',
      spellcheck: 'false',
      placeholder: '例如 deepseek-chat / gpt-4o-mini',
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
        field('接口地址（OpenAI 兼容）', null, baseUrlInput),
        field('模型名', null, modelInput),
        field('上下文预算（token）', '小窗口模型可调低', contextInput)),
      h('div', { style: { marginTop: '12px' } },
        field('API 密钥', '只存本机，不随仓库走。', apiKeyInput)));

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
            : '当前：未配置 —— 填上密钥即可。';
      })
      .catch(function () {
        channelRow.textContent = '';
      });

    var aiForm = h('div.form__section', null,
      h('div.form__sectiontitle', { text: 'AI（批改与对话）' }),
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
          '浏览器直连需服务商允许跨域（CORS）。不支持时可用本地 <code>ollama</code>：' +
          '地址 <code>http://localhost:11434/v1</code>，并以 <code>OLLAMA_ORIGINS=*</code> 放开跨域。',
      }));

    /* ------------------------------------------------------ 全局记忆
     *
     * 这些是**跨对话长期有效**的信息，但**不自动进提示词**（用户 2026-11-09 定的：
     * 放在那边，让 agent 有需要的时候自己查，像 coding agent 的 global memory）。
     * 对话里走 `list_memory` / `edit_memory` 工具；这里的增删改直接落库，
     * 下一轮它来查时看到的就是新的。
     *
     * 类型只有四档，与后端 `memory.KINDS` 对齐；文案不必逐字一样，但键不能漂。
     */
    var MEMORY_KINDS = [
      ['profile', '身份 / 背景'],
      ['preference', '偏好 / 要求'],
      ['project', '在做的事'],
      ['fact', '记住的事实'],
    ];

    function memoryKindSelect(value) {
      var select = h(
        'select.select.memrow__kind',
        { 'aria-label': '记忆类型' },
        MEMORY_KINDS.map(function (pair) {
          return h('option', { value: pair[0], text: pair[1] });
        })
      );
      select.value = value || 'fact';
      return select;
    }

    function memoryStamp(iso) {
      var d = new Date(iso || '');
      if (isNaN(d.getTime())) return '';
      return (
        '更新于 ' + (d.getMonth() + 1) + '月' + d.getDate() + '日 ' +
        (d.getHours() < 10 ? '0' : '') + d.getHours() + ':' +
        (d.getMinutes() < 10 ? '0' : '') + d.getMinutes()
      );
    }

    /** 一条记忆：标题 / 类型 / 正文，改完点保存（别每敲一个字就 PATCH 一次） */
    function memoryRow(one) {
      var current = one;
      var title = h('input.input.memrow__title', {
        type: 'text',
        placeholder: '一句话标题',
        'aria-label': '记忆标题',
        value: current.title || '',
      });
      var kind = memoryKindSelect(current.kind);
      var body = h('textarea.textarea.memrow__body', {
        rows: '3',
        placeholder: '详细一点：它该在什么时候用上？',
        'aria-label': '记忆正文',
        value: current.body || '',
      });
      var meta = h('span.memrow__meta', { text: memoryStamp(current.updatedAt) });

      var save = h('button.btn.btn--sm', { type: 'button', onClick: doSave }, '保存');
      var del = h('button.btn.btn--ghost.btn--sm', {
        type: 'button',
        title: '删掉这条记忆',
        onClick: doDelete,
      }, '删除');

      function doSave() {
        var nextTitle = String(title.value || '').trim();
        var nextBody = String(body.value || '').trim();
        if (!nextTitle && !nextBody) {
          ui.toast('标题和正文至少要有一个', 'warn', 2000);
          return;
        }
        save.disabled = true;
        api
          .patch('/memory/' + encodeURIComponent(current.id), {
            title: nextTitle,
            body: nextBody,
            kind: kind.value,
          })
          .then(
            function (data) {
              save.disabled = false;
              if (data && data.memory) {
                current = data.memory;
                title.value = current.title || '';
                body.value = current.body || '';
                kind.value = current.kind || 'fact';
                meta.textContent = memoryStamp(current.updatedAt);
              }
              ui.toast('已保存', 'ok', 1400);
            },
            function (err) {
              save.disabled = false;
              ui.toast((err && err.message) || '没保存上', 'bad', 2600);
            }
          );
      }

      function doDelete() {
        ui.confirm('删掉「' + (current.title || '这条记忆') + '」？之后 AI 就查不到它了。', {
          okLabel: '删掉',
          danger: true,
        }).then(function (yes) {
          if (!yes) return;
          api.del('/memory/' + encodeURIComponent(current.id)).then(
            function () {
              if (row.parentNode) row.parentNode.removeChild(row);
              ui.toast('已删掉', 'ok', 1400);
              if (!memList.children.length) showEmpty();
            },
            function (err) {
              ui.toast((err && err.message) || '没删掉', 'bad', 2600);
            }
          );
        });
      }

      var row = h(
        'div.memrow',
        null,
        h('div.memrow__head', null, title, kind),
        body,
        h('div.memrow__foot', null, meta, save, del)
      );
      return row;
    }

    var memList = h('div.memlist');
    var memEmpty = h('p.form__hint', {
      text: '还没有记过东西。往下写一条，AI 需要时会来查。',
    });

    function showEmpty() {
      if (!memList.children.length) memList.appendChild(memEmpty);
    }

    var newTitle = h('input.input.memrow__title', {
      type: 'text',
      placeholder: '一句话标题（可留空，从正文第一行取）',
      'aria-label': '新记忆标题',
    });
    var newKind = memoryKindSelect('fact');
    var newBody = h('textarea.textarea.memrow__body', {
      rows: '3',
      placeholder: '例如：他正在自学数字芯片设计，解释尽量给电路层面的直觉',
      'aria-label': '新记忆正文',
    });
    var addBtn = h('button.btn.btn--primary.btn--sm', {
      type: 'button',
      onClick: function () {
        var title = String(newTitle.value || '').trim();
        var body = String(newBody.value || '').trim();
        if (!title && !body) {
          ui.toast('标题和正文至少要有一个', 'warn', 2000);
          return;
        }
        addBtn.disabled = true;
        api
          .post('/memory', { title: title, body: body, kind: newKind.value })
          .then(
            function (data) {
              addBtn.disabled = false;
              var memory = data && data.memory;
              if (!memory) return;
              if (memEmpty.parentNode === memList) memList.removeChild(memEmpty);
              memList.appendChild(memoryRow(memory));
              newTitle.value = '';
              newBody.value = '';
              newKind.value = 'fact';
              ui.toast('记住了', 'ok', 1400);
            },
            function (err) {
              addBtn.disabled = false;
              ui.toast((err && err.message) || '没记上', 'bad', 2600);
            }
          );
      },
    }, '记下来');

    api.get('/memory').then(
      function (data) {
        var list = (data && data.memories) || [];
        ui.clear(memList);
        if (!list.length) {
          showEmpty();
          return;
        }
        list.forEach(function (one) {
          memList.appendChild(memoryRow(one));
        });
      },
      function () {
        ui.clear(memList);
        memList.appendChild(h('p.form__hint', { text: '记忆没读出来 —— 刷新一下再试。' }));
      }
    );

    var memoryForm = h(
      'div.form__section',
      null,
      h('div.form__sectiontitle', { text: '全局记忆' }),
      h('p', {
        text:
          '跨对话长期有效，但不自动进提示词：AI 需要时会自己来查，学习模式下也能自己记、自己改。',
        style: { margin: '-2px 0 10px', fontSize: '12px', lineHeight: '1.6', color: 'var(--fg3)' },
      }),
      memList,
      h('div.memnew', null,
        h('div.form__sectiontitle', { text: '记一条新的', style: { margin: '14px 0 10px' } }),
        h('div.memrow__head', null, newTitle, newKind),
        newBody,
        h('div.btnrow', { style: { marginTop: '10px' } }, addBtn,
          h('span.form__hint', { text: '写得越具体越有用；别整段抄对话。' }))));

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
          searchKindSelect.appendChild(
            h('option', { value: one.kind, text: one.label + (one.keyless ? '（免密钥）' : '') }));
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
          ? '当前：可用 · ' + (st.label || '')
            + (st.source === 'keyless' ? ' · 免密钥' : ' · 自带密钥')
          : '当前：不可用 —— ' + (st.message || '');
      })
      .catch(function () {
        searchStatus.textContent = '';
      });

    var searchForm = h('div.form__section', null,
      h('div.form__sectiontitle', { text: '联网搜索' }),
      h('p', {
        // 这一段是**纯文本**，不解析 Markdown —— 别在这儿写 `**强调**`，
        // 星号会原样显示出来。
        text: '让 AI 联网查资料并给出网址；它只读网页，不动你的数据。',
        style: { margin: '-2px 0 8px', fontSize: '12px', lineHeight: '1.6', color: 'var(--fg3)' },
      }),
      searchStatus,
      switchRow('启用联网搜索', '关掉后模型拿不到搜索工具。',
        searchConf.enabled !== false, function (value) {
          store.saveSettings({ search: { enabled: value } });
        }),
      h('div.form__grid', { style: { marginTop: '12px' } },
        field('服务商', null, searchKindSelect),
        field('接口地址', '只有自建网关才需要改', searchEndpointInput)),
      h('div', { style: { marginTop: '12px' } },
        field('API 密钥', '收费服务商才需要填。', searchKeyInput)));

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
      switchRow('浅色主题', '低强度配色，适合长时间阅读。', conf.theme === 'light', function (value) {
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
        setObsStatus('未检测', '');
      },
    });
    // 状态与按钮同一行（`testrow`）：动作与结果挨着，别让一行小字自己飘在下面
    var obsStatus = h('span.testnote', { text: '未检测' });

    function setObsStatus(text, kind) {
      obsStatus.textContent = text;
      obsStatus.classList.toggle('is-ok', kind === 'ok');
      obsStatus.classList.toggle('is-bad', kind === 'bad');
    }

    var obsForm = h('div.form__section', null,
      h('div.form__sectiontitle', { text: 'Obsidian（笔记）' }),
      h('p', {
        text: '笔记存在 vault 里，不经过网络：填好路径，再在 Obsidian 里启用「QuizForge 桥接」插件。',
        style: { margin: '-2px 0 8px', fontSize: '12px', lineHeight: '1.6', color: 'var(--fg3)' },
      }),
      field('vault 路径', null, vaultInput),
      h('div.btnrow.testrow', null,
        h('button.btn', {
          type: 'button',
          onClick: function () {
            setObsStatus('正在检测…', '');
            // 带上输入框里的值：刚打完还没同步到后端，也能立刻检测
            api.get('/obsidian/status?path=' + encodeURIComponent(vaultInput.value || '')).then(function (st) {
              if (st && st.ok) {
                setObsStatus('已连上 · ' + (st.vault || 'vault')
                  + (st.pluginVersion ? ' · 插件 ' + st.pluginVersion : ''), 'ok');
              } else {
                setObsStatus('没连上：' + ((st && st.error) || '未知原因'), 'bad');
              }
            }).catch(function (err) {
              setObsStatus('没连上：' + ((err && err.message) || err), 'bad');
            });
          },
        }, h('span', { html: ui.icon('refresh', 15) }), h('span', { text: '检测连接' })),
        obsStatus));

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

    /* ------------------------------------------------------ 跨机器同步（Turso）
     *
     * 云端-缓存：Turso 是云上那份共享副本，本地 SQLite 是工作副本。
     * 行级表与资料整包都由后台节拍自动对齐（api/app/autosync.py）；
     * 这里只填连接信息、看状态、以及需要时手动催一轮。
     * 对话四张表、全局记忆、计划书端到端加密，密钥就是 `config/snapshot.key`
     * —— 两台机器必须同一份。其余数据明文上传。令牌不会显示回来。
     */
    var syncConf = conf.turso || {};
    var syncStatusLine = h('p', {
      text: '正在读取同步状态…',
      style: { margin: '-4px 0 10px', fontSize: '12px', lineHeight: '1.6', color: 'var(--fg2)' },
    });
    var syncResult = h('div');
    var syncUrlInput = h('input.input.input--mono', {
      type: 'text',
      spellcheck: 'false',
      placeholder: 'libsql://你的库.turso.io',
      value: syncConf.url || '',
      onInput: function (event) {
        store.saveSettings({ turso: { url: event.target.value.trim() } });
      },
    });
    var syncTokenInput = h('input.input.input--mono', {
      type: 'password',
      autocomplete: 'off',
      placeholder: 'Turso 数据库令牌（存在本机，不会上传）',
      value: syncConf.token || '',
      onInput: function (event) {
        store.saveSettings({ turso: { token: event.target.value.trim() } });
      },
    });

    function paintSync(st) {
      if (!st) {
        syncStatusLine.textContent = '读不到同步状态。';
        return;
      }
      if (!st.enabled) {
        syncStatusLine.textContent = '已关闭 —— 打开下面的开关才会同步。';
        return;
      }
      if (!st.configured) {
        syncStatusLine.textContent = '还没配置 —— 填上数据库地址与令牌就能用。';
        return;
      }
      var bits = ['已配置', '待推送 ' + (st.pending || 0) + ' 行'];
      var auto = st.auto || {};
      if (auto.started) {
        bits.push('自动同步：每 ' + Math.round(auto.interval || 0) + ' 秒');
      } else if (auto.enabled === false) {
        bits.push('自动同步：已关闭');
      } else {
        bits.push('自动同步：未启动');
      }
      bits.push(
        st.keyReady
          ? '加密口令：有'
          : '加密口令：没有（对话 / 记忆 / 计划书不会上传）'
      );
      var last = st.last;
      if (last && last.ok) {
        bits.push('上次 ' + last.at + ' · 推 ' + (last.pushed || 0) + ' / 拉 ' + (last.pulled || 0));
        var lib = last.library || {};
        var libLabels = { push: '已上传', pull: '已下载', 'pull+push': '已对齐', same: '已一致' };
        if (lib.action && lib.action !== 'idle') {
          bits.push('资料包' + (libLabels[lib.action] || lib.action)
            + (lib.materials ? '（' + lib.materials + ' 份材料）' : ''));
        }
      } else if (last && !last.ok) {
        bits.push('上次失败：' + last.error);
      } else {
        bits.push('还没同步过');
      }
      syncStatusLine.textContent = bits.join(' · ');
    }

    function refreshSync() {
      api.get('/sync/status').then(paintSync).catch(function () {
        syncStatusLine.textContent = '读不到同步状态。';
      });
    }

    function pollSync(deadline) {
      api.get('/sync/status').then(function (st) {
        paintSync(st);
        if (st && st.running && Date.now() < deadline) {
          window.setTimeout(function () { pollSync(deadline); }, 1500);
          return;
        }
        ui.clear(syncResult);
        var last = st && st.last;
        if (last && last.ok) {
          syncResult.appendChild(h('div.statline', { style: { color: 'var(--ok)' } },
            h('span', { html: ui.icon('check', 15) }),
            h('span', { text: '同步完成 · 推 ' + (last.pushed || 0) + ' 行 · 拉 '
              + (last.pulled || 0) + ' 行 · ' + (last.seconds || 0) + ' 秒' })));
        } else {
          syncResult.appendChild(h('div.statline', { style: { color: 'var(--bad)' } },
            h('span', { html: ui.icon('warn', 15) }),
            h('span', { text: '同步失败：' + ((last && last.error) || '未知原因') })));
        }
      }).catch(function (err) {
        ui.clear(syncResult);
        syncResult.appendChild(h('div.statline', { style: { color: 'var(--bad)' } },
          h('span', { text: '同步失败：' + ((err && err.message) || err) })));
      });
    }

    var syncForm = h('div.form__section', null,
      h('div.form__sectiontitle', { text: '同步（Turso）' }),
      h('p', {
        text: 'Turso 是云上共享副本，本地 SQLite 是工作副本，两边自动对齐。'
          + '对话、分组、消息、附件、全局记忆、计划书端到端加密（两台机器用同一份 snapshot.key），'
          + '其余数据明文。',
        style: { margin: '-2px 0 8px', fontSize: '12px', lineHeight: '1.6', color: 'var(--fg3)' },
      }),
      syncStatusLine,
      switchRow('启用跨机器同步', '关闭后不推不拉，本机数据不受影响。',
        syncConf.enabled !== false, function (value) {
          store.saveSettings({ turso: { enabled: value } });
        }),
      h('div.form__grid', { style: { marginTop: '12px' } },
        field('数据库地址', null, syncUrlInput),
        field('数据库令牌', '在 Turso 控制台生成。', syncTokenInput)),
      h('div.btnrow', { style: { marginTop: '12px' } },
        h('button.btn', {
          type: 'button',
          onClick: function () {
            ui.clear(syncResult);
            syncResult.appendChild(h('div.statline', null, h('span.spinner'), h('span', { text: '正在同步…（首次会久一点）' })));
            api.post('/sync/run', {}).then(function (res) {
              if (res && res.started === false) {
                ui.clear(syncResult);
                syncResult.appendChild(h('div.statline', { text: '已经有一次同步在跑，等它结束。' }));
              }
              pollSync(Date.now() + 10 * 60 * 1000);
            }).catch(function (err) {
              ui.clear(syncResult);
              syncResult.appendChild(h('div.statline', { style: { color: 'var(--bad)' } },
                h('span', { text: '没开始：' + ((err && err.message) || err) })));
            });
          },
        }, h('span', { html: ui.icon('refresh', 15) }), h('span', { text: '立即同步' }))),
      syncResult);

    refreshSync();

    /* ---- 日历订阅（iCalendar）----
     *
     * 课表、会议邀请都是一串 .ics 地址，贴进来由后端拉取并按 RFC 5545 展开
     * （见 `app/ics.py`）。为什么放在设置里而不是日历页：**地址是凭据** ——
     * 学校课表的订阅地址就是一串 JWT，拿到地址等于拿到整个人的课表。
     * 它属于"这台机器的配置"，不属于"今天的日程"，所以只在设置里出现一次，
     * 日历页只显示"课表来自哪份订阅"。 */
    var calUrlInput = h('input.input.input--mono', {
      type: 'url',
      spellcheck: 'false',
      autocomplete: 'off',
      placeholder: 'https://…/calendar.ics',
    });
    var calList = h('div.calsubs');
    var calNote = h('p', {
      text: '',
      style: { margin: '8px 0 0', fontSize: '12px', lineHeight: '1.6', color: 'var(--fg3)' },
    });

    function calSyncWhen(iso) {
      if (!iso) return '';
      var d = new Date(iso);
      if (isNaN(d.getTime())) return '';
      var pad = function (n) { return n < 10 ? '0' + n : String(n); };
      return (d.getMonth() + 1) + '月' + d.getDate() + '日 ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
    }

    function calRow(one) {
      var sync = h('button.btn.btn--sm', { type: 'button', text: '同步' });
      var drop = h('button.btn.btn--ghost.btn--sm', { type: 'button', text: '退订' });

      sync.addEventListener('click', function () {
        sync.disabled = true;
        sync.textContent = '同步中…';
        api.post('/schedule/subscriptions/' + encodeURIComponent(one.id) + '/sync', {}).then(
          function (res) {
            calNote.textContent = '已同步 ' + ((res && res.total) || 0) + ' 块'
              + (res && res.removed ? '，去掉 ' + res.removed + ' 块' : '');
            refreshCalendars();
          },
          function (err) {
            sync.disabled = false;
            sync.textContent = '同步';
            calNote.textContent = (err && err.message) || '同步失败';
            ui.toast(calNote.textContent, 'bad', 4200);
          }
        );
      });

      drop.addEventListener('click', function () {
        ui.confirm('退订「' + (one.title || '这份订阅') + '」？它同步进来的 '
          + one.events + ' 块会一起删掉。', { okLabel: '退订', danger: true })
          .then(function (yes) {
            if (!yes) return;
            api.del('/schedule/subscriptions/' + encodeURIComponent(one.id)).then(
              function () {
                calNote.textContent = '已退订。';
                refreshCalendars();
              },
              function (err) {
                calNote.textContent = (err && err.message) || '没退掉';
                ui.toast(calNote.textContent, 'bad', 3200);
              }
            );
          });
      });

      var meta = one.events + ' 块';
      if (one.lastSyncAt) meta += ' · 上次 ' + calSyncWhen(one.lastSyncAt);

      return h('div.calsub', null,
        h('div.calsub__main', null,
          h('span.calsub__name', { text: one.title || one.host || '订阅', title: one.url }),
          h('span.calsub__meta', { text: meta, title: one.lastResult || '' })),
        h('div.calsub__ops', null, sync, drop));
    }

    function refreshCalendars() {
      api.get('/schedule/subscriptions').then(function (out) {
        var items = (out && out.subscriptions) || [];
        ui.clear(calList);
        if (!items.length) {
          calList.appendChild(h('p.calsubs__empty', { text: '还没有订阅。贴一个地址，下面的框里点「订阅」。' }));
          return;
        }
        items.forEach(function (one) { calList.appendChild(calRow(one)); });
      }, function (err) {
        ui.clear(calList);
        calList.appendChild(h('p.calsubs__empty', {
          text: '读不到订阅：' + ((err && err.message) || '本地服务没响应'),
        }));
      });
    }

    function subscribeCalendar() {
      var url = String(calUrlInput.value || '').trim();
      if (!url) {
        calUrlInput.focus();
        return;
      }
      calNote.textContent = '正在拉取…';
      api.post('/schedule/subscriptions', { url: url }).then(function (res) {
        calUrlInput.value = '';
        calNote.textContent = '已导入 ' + ((res && res.total) || 0) + ' 块。';
        ui.toast(calNote.textContent, 'ok', 2600);
        refreshCalendars();
      }, function (err) {
        calNote.textContent = (err && err.message) || '订阅失败';
        ui.toast(calNote.textContent, 'bad', 4200);
      });
    }

    calUrlInput.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') {
        ev.preventDefault();
        subscribeCalendar();
      }
    });

    refreshCalendars();

    var calendarForm = h('div.form__section', null,
      h('div.form__sectiontitle', { text: '日历订阅（iCalendar）' }),
      h('p', {
        text: '贴一个 .ics 地址即可订阅课表；课程有调整时回来点「同步」。'
          + '地址里通常带令牌，所以只存本机、不跟同步走。',
        style: { margin: '-2px 0 10px', fontSize: '12px', lineHeight: '1.6', color: 'var(--fg3)' },
      }),
      h('div.form__grid', null,
        field('订阅地址', 'webcal:// 换成 https://', calUrlInput)),
      h('div.btnrow', { style: { marginTop: '10px' } },
        h('button.btn', { type: 'button', onClick: subscribeCalendar },
          h('span', { html: ui.icon('plus', 15) }), h('span', { text: '订阅' }))),
      calNote,
      calList);

    /* ------------------------------------------------------ 作息与健康策略
     *
     * 这一段的选项**全部来自后端注册表**（`/wellbeing/profiles`、`/gates`）：
     * 以后加一个精力档位或一种门禁，这里不改代码，下拉里自动就有。
     * 存的都是 `settings.health` 这个块（随同步走，不含秘密）。
     */
    var healthConf = conf.health || {};
    var healthOptions = [];   // 档位注册表（异步填）
    var gateOptions = [];     // 门禁注册表（异步填）

    function saveHealth(patch) {
      store.saveSettings({ health: patch });
    }

    var bedInput = h('input.input', {
      type: 'time',
      value: healthConf.targetBedtime || '23:00',
      onInput: function (event) {
        saveHealth({ targetBedtime: event.target.value });
      },
    });
    var wakeInput = h('input.input', {
      type: 'time',
      value: healthConf.targetWake || '07:00',
      onInput: function (event) {
        saveHealth({ targetWake: event.target.value });
      },
    });
    var graceInput = h('input.input', {
      type: 'number',
      min: '0',
      max: '180',
      step: '5',
      value: healthConf.curfewGraceMin == null ? 0 : healthConf.curfewGraceMin,
      onInput: function (event) {
        var value = parseInt(event.target.value, 10);
        saveHealth({ curfewGraceMin: isNaN(value) ? 0 : Math.max(0, Math.min(180, value)) });
      },
    });

    var profileSelect = h('select.input', {
      onChange: function (event) {
        saveHealth({ defaultProfile: event.target.value });
      },
    });
    var deepSelect = h('select.input', {
      onChange: function (event) {
        saveHealth({ deepPref: event.target.value });
      },
    });
    [['morning', '上午（默认）'], ['evening', '傍晚'], ['both', '都行']].forEach(function (one) {
      deepSelect.appendChild(h('option', { value: one[0] }, one[1]));
    });
    deepSelect.value = healthConf.deepPref || 'morning';

    var gateSelects = {};
    ['art', 'science', 'skill'].forEach(function (domain) {
      var select = h('select.input', {
        onChange: function (event) {
          var next = Object.assign({}, healthConf.gateDefaults || {});
          next[domain] = event.target.value;
          healthConf = Object.assign({}, healthConf, { gateDefaults: next });
          saveHealth({ gateDefaults: next });
        },
      });
      gateSelects[domain] = select;
    });

    var regimeRows = h('div');

    function fillProfileOptions(select, value) {
      ui.clear(select);
      healthOptions.forEach(function (one) {
        select.appendChild(h('option', { value: one.key }, one.label));
      });
      if (value) select.value = value;
    }

    function fillGateOptions(select, value) {
      ui.clear(select);
      // 注册表里本来就有 `none`（"不设门禁"），别再塞一个占位项 —— 两个同名选项
      // 长得一样、存的值却不同（'' 与 'none'），看着像 bug。
      gateOptions.forEach(function (one) {
        select.appendChild(h('option', { value: one.key }, one.label));
      });
      select.value = value || 'none';
    }

    function patchRegime(id, patch) {
      api.patch('/wellbeing/regimes/' + encodeURIComponent(id), patch).then(
        function () {
          refreshRegimes();
        },
        function (err) {
          ui.toast((err && err.message) || '改不了', 'bad', 2600);
        }
      );
    }

    function regimeRow(one) {
      var start = h('input.input', {
        type: 'date',
        value: one.startDate || '',
        onChange: function (event) {
          patchRegime(one.id, { startDate: event.target.value });
        },
      });
      var end = h('input.input', {
        type: 'date',
        value: one.endDate || '',
        onChange: function (event) {
          patchRegime(one.id, { endDate: event.target.value || null });
        },
      });
      var pick = h('select.input', {
        onChange: function (event) {
          patchRegime(one.id, { profile: event.target.value });
        },
      });
      fillProfileOptions(pick, one.profile);
      var note = h('input.input', {
        type: 'text',
        value: one.note || '',
        placeholder: '备注（可省）',
        onChange: function (event) {
          patchRegime(one.id, { note: event.target.value });
        },
      });
      var del = h(
        'button.btn.btn--ghost.btn--sm',
        {
          type: 'button',
          onClick: function () {
            api.del('/wellbeing/regimes/' + encodeURIComponent(one.id)).then(refreshRegimes, function () {
              ui.toast('删不掉', 'bad', 2400);
            });
          },
        },
        '删除'
      );
      return h(
        'div.form__grid',
        { style: { alignItems: 'end', marginBottom: '8px' } },
        field('起', null, start),
        field('止（空 = 一直）', null, end),
        field('档位', null, pick),
        field('备注', null, note),
        h('div.btnrow', null, del)
      );
    }

    function refreshRegimes() {
      api.get('/wellbeing/regimes').then(
        function (res) {
          ui.clear(regimeRows);
          var rows = (res && res.regimes) || [];
          if (!rows.length) {
            regimeRows.appendChild(
              h('p.form__note', { text: '还没有档位时间线：每天都用上面的默认档位。' })
            );
            return;
          }
          rows.forEach(function (one) {
            regimeRows.appendChild(regimeRow(one));
          });
        },
        function () {
          ui.clear(regimeRows);
        }
      );
    }

    var addRegime = h(
      'button.btn.btn--ghost.btn--sm',
      {
        type: 'button',
        onClick: function () {
          var now = new Date();
          var iso =
            now.getFullYear() +
            '-' +
            ('0' + (now.getMonth() + 1)).slice(-2) +
            '-' +
            ('0' + now.getDate()).slice(-2);
          api
            .post('/wellbeing/regimes', {
              title: '恢复期',
              startDate: iso,
              profile: profileSelect.value || healthConf.defaultProfile || 'standard',
            })
            .then(refreshRegimes, function (err) {
              ui.toast((err && err.message) || '加不了', 'bad', 2600);
            });
        },
      },
      h('span', { html: ui.icon('plus', 15) }),
      h('span', { text: '加一段' })
    );

    var healthForm = h('div.form__section', null,
      h('div.form__sectiontitle', { text: '作息与健康策略' }),
      h('p', {
        text: '自动排程按这套作息估算「今天还剩多少脑力」；身体状态可按日期分段，按当天档位算。',
        style: { margin: '-2px 0 10px', fontSize: '12px', lineHeight: '1.6', color: 'var(--fg3)' },
      }),
      h('div.form__grid', null,
        field('目标入睡', '睡前留一小时不排深度任务', bedInput),
        field('目标起床', '起床后半小时内不排任务', wakeInput),
        field('催睡宽限（分钟）', '到点后多少分钟开始劝睡', graceInput)),
      switchRow(
        '到点催睡',
        '只在对话里提醒一句，不硬锁任何功能；你说继续就继续，下一轮还会提。',
        healthConf.curfewEnabled !== false,
        function (value) {
          healthConf = Object.assign({}, healthConf, { curfewEnabled: value });
          saveHealth({ curfewEnabled: value });
        }
      ),
      h('div.form__grid', null,
        field('默认精力档位', '某天没有命中下面的时间线时用它', profileSelect),
        field('深度用脑偏好', '决定「硬活」往哪半天放', deepSelect)),
      h('p', {
        text: '档位时间线',
        style: { margin: '16px 0 6px', fontSize: '12px', fontWeight: '600', color: 'var(--fg2)' },
      }),
      regimeRows,
      h('div.btnrow', { style: { marginTop: '6px' } }, addRegime),
      h('p', {
        text: '学习块门禁（默认按学科；逐条也可以在计划里指定）',
        style: { margin: '16px 0 6px', fontSize: '12px', fontWeight: '600', color: 'var(--fg2)' },
      }),
      h('div.form__grid', null,
        field('文科', null, gateSelects.art),
        field('理科', null, gateSelects.science),
        field('技能', null, gateSelects.skill)));

    refreshRegimes();
    api.get('/wellbeing/profiles').then(
      function (res) {
        healthOptions = (res && res.profiles) || [];
        fillProfileOptions(profileSelect, healthConf.defaultProfile || (res && res.defaultProfile));
        // 设置里没写的项，显示**后端正在用的值**：不写默认值而显示 23:00，
        // 会让人以为"我改了"——其实改的是另一个数（这类假象最难查）。
        if (!healthConf.targetBedtime && res && res.targets && res.targets.bed) {
          bedInput.value = res.targets.bed;
        }
        if (!healthConf.targetWake && res && res.targets && res.targets.wake) {
          wakeInput.value = res.targets.wake;
        }
        if (healthConf.curfewGraceMin == null && res && res.curfew) {
          graceInput.value = res.curfew.graceMin;
        }
        if (!healthConf.deepPref && res && res.deepPref) {
          deepSelect.value = res.deepPref;
        }
        // 时间线行是在 profiles 到位之前画的：填完再重画一遍
        refreshRegimes();
      },
      function () {}
    );
    api.get('/gates').then(
      function (res) {
        gateOptions = (res && res.gates) || [];
        api.get('/wellbeing/profiles').then(function (info) {
          var mine = healthConf.gateDefaults || {};
          var effective = (info && info.gateDefaults) || {};
          ['art', 'science', 'skill'].forEach(function (domain) {
            // 逐条指定的优先，否则显示系统默认（文科复述 / 理科测验 / 技能交产出物）
            fillGateOptions(gateSelects[domain], mine[domain] || effective[domain] || 'none');
          });
        }, function () {});
      },
      function () {}
    );

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

  return h('div.form', null, aiForm, memoryForm, searchForm, obsForm, appearanceForm, syncForm,
    calendarForm, healthForm, dataForm);
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
    renderTabbar();
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
