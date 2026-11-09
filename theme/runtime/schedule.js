/* ===========================================================================
 * schedule.js —— 日程页：左边的**计划清单**，右边的**日历**。
 *
 * ## 为什么是两块而不是一块
 *
 * 计划是"要做什么"（可以一直没有日期），日程是"哪个时段做什么"（必须有起止）。
 * 把条目带个日期就当它排进了日历，是最常见的那种错 —— 于是这一页把它们并排摆着：
 * 左边那栏随时能拖进右边，**拖之前它不在日历上**。
 *
 * ## 数据从哪来
 *
 * 全在服务端（`/api/plans`、`/api/plan-schedule` 另见 `api/app/routers/plans.py`）。
 * 这一页不做本地缓存：日历只看当前这一屏的时间窗，切视图就重新取 ——
 * 一次请求几十毫秒，比维护一份"本地改了什么、服务端改了什么"的对账便宜得多。
 *
 * ## 重画的尺度
 *
 * 数据变了就整页重画（`render()`）。只有一种例外：拖拽期间不重画，
 * 只动被拖那一个元素的 `transform` —— 否则跟手的元素会在半路被替换掉。
 * ========================================================================= */
(function () {
  'use strict';

  var QF = window.QF;
  var ui = QF.ui;
  var h = ui.h;
  var api = QF.api;

  //: 时间轴上的一小时多少像素。48 是"一眼能看出半小时的差"与"24 小时塞得下"之间的折中。
  var HOUR = 48;
  //: 凌晨那段的那条横带（2026-11-09 用户："凌晨时段弄一个折叠条"）。
  //: 折叠时 0:00–6:00 压成这么高的一条；展开后这条**留在原地**，点它收回去
  //:（原先只在折叠态画它，于是展开了就没有收回的入口）。
  //: 34 而不是 26：26 时这条与紧跟的 `06:00` 标签只隔 2px，读起来是两行挤在一起
  //:（用户 2026-11-09："折叠的那个地方太挤了"）。
  var NIGHT_H = 34;
  var NIGHT_FROM = 6;
  //: 拖拽/建事件对齐到多少分钟。
  var SNAP = 15;
  //: 月视图固定 6 行（42 格）：高度稳定，翻月时不跳。
  var CELLS = 42;

  /** 手机上换一套排法（2026-11-09 用户："日历和任务针对手机单独做一版"）。
   *  断点与 `theme/mobile.css` 同一条：600px。 */
  var NARROW = window.matchMedia ? window.matchMedia('(max-width: 600px)') : null;

  function isNarrow() {
    return !!(NARROW && NARROW.matches);
  }

  var PANE_KEY = 'qf.sch.pane';

  /** 上次停在「计划」还是「日历」。这是这一页的主入口，每次打开都从头选太烦。 */
  function loadPane() {
    try {
      var saved = window.localStorage.getItem(PANE_KEY);
      return saved === 'plan' || saved === 'calendar' ? saved : 'calendar';
    } catch (e) {
      return 'calendar';
    }
  }

  var state = {
    view: 'week', // day / week / month
    //: 2026-11-09：计划与日历拆成两个子页，用顶上的胶囊切（用户要求）。
    pane: loadPane(),
    cursor: new Date(),
    plans: [],
    events: [],
    //: 日历订阅（iCalendar）。读不到就当没有 —— 订阅挂了不该让整页打不开。
    subs: [],
    //: 刚重排出来的块：`{created: {}, moved: {}}`（id → true），4 秒后清掉。
    fresh: null,
    //: 被挪走的块的**旧位置**：在时间轴上留 4 秒虚线幽灵。
    ghosts: [],
    replanning: false,
    ready: false,
    //: 凌晨 0–6 点是否展开。默认收起 —— 正常人的日程在 09:00 之后，
    //: 每天先翻掉四分之一屏的空白是浪费（与 `scrollToFocus` 同一个理由）。
    nightOpen: false,
  };

  /** 折叠凌晨时，整根时间轴相对 00:00 上移了多少像素（展开时为 0）。 */
  function nightShift() {
    return state.nightOpen ? 0 : NIGHT_FROM * HOUR - NIGHT_H;
  }

  /** 横带下面还剩多少像素才开始有时间：展开时 00:00 就在条下面，折叠时 00:00 已被压掉。 */
  function nightTop() {
    return state.nightOpen ? NIGHT_H : 0;
  }

  /** 时间轴可见部分的起点（分钟）。 */
  function nightStartMin() {
    return state.nightOpen ? 0 : NIGHT_FROM * 60;
  }

  function toggleNight() {
    state.nightOpen = !state.nightOpen;
    renderCal();
    scrollToFocus();
  }

  //: 下次画完要不要把滚动条落到"现在"附近（换视图、翻页之后重新落一次）。
  var pendingScroll = true;

  /* ------------------------------------------------------------ 日期工具 */
  /* 一律用**本地时间**：对用户来说"今天"是他那台机器的今天。 */

  function pad(n) {
    return n < 10 ? '0' + n : String(n);
  }

  function startOfDay(d) {
    var x = new Date(d);
    x.setHours(0, 0, 0, 0);
    return x;
  }

  function addDays(d, n) {
    var x = new Date(d);
    x.setDate(x.getDate() + n);
    return x;
  }

  function addMonths(d, n) {
    var x = new Date(d);
    x.setDate(1);
    x.setMonth(x.getMonth() + n);
    return x;
  }

  /** 周一为一周之始（日历的默认习惯；`getDay()` 的 0 是周日，所以要挪一下） */
  function startOfWeek(d) {
    var x = startOfDay(d);
    return addDays(x, -((x.getDay() + 6) % 7));
  }

  function startOfMonth(d) {
    return new Date(d.getFullYear(), d.getMonth(), 1);
  }

  function sameDay(a, b) {
    return (
      a.getFullYear() === b.getFullYear() &&
      a.getMonth() === b.getMonth() &&
      a.getDate() === b.getDate()
    );
  }

  function isToday(d) {
    return sameDay(d, new Date());
  }

  function dayKey(d) {
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }

  function minutesOf(d) {
    return d.getHours() * 60 + d.getMinutes();
  }

  var WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];

  function dayLabel(d) {
    return d.getMonth() + 1 + '月' + d.getDate() + '日';
  }

  /* ------------------------------------------------------------ 可见范围 */

  function visibleRange() {
    if (state.view === 'day') {
      var one = startOfDay(state.cursor);
      return { from: one, to: addDays(one, 1), days: [one] };
    }
    if (state.view === 'month') {
      var first = startOfWeek(startOfMonth(state.cursor));
      return { from: first, to: addDays(first, CELLS), days: null, first: first };
    }
    var week = startOfWeek(state.cursor);
    var days = [];
    for (var i = 0; i < 7; i += 1) days.push(addDays(week, i));
    return { from: week, to: addDays(week, 7), days: days };
  }

  /* ------------------------------------------------------------ 取数 */

  function load() {
    var range = visibleRange();
    var query =
      '/schedule?from=' +
      encodeURIComponent(range.from.toISOString()) +
      '&to=' +
      encodeURIComponent(range.to.toISOString());
    var subs = api.get('/schedule/subscriptions').then(
      function (res) {
        return (res && res.subscriptions) || [];
      },
      function () {
        return [];
      }
    );
    return Promise.all([api.get('/plans'), api.get(query), subs]).then(
      function (res) {
        state.plans = (res[0] && res[0].plans) || [];
        state.events = (res[1] && res[1].events) || [];
        state.subs = res[2] || [];
        state.ready = true;
        pendingScroll = true; // 换视图/换日期之后重新落一次位置
        render();
      },
      function (err) {
        state.ready = true;
        ui.toast('日程没读出来：' + ((err && err.message) || '本地服务没响应'), 'bad', 3000);
        render();
      }
    );
  }

  function reload() {
    return load();
  }

  /* ------------------------------------------------------------ 顶栏 */

  function titleText() {
    var d = state.cursor;
    if (state.view === 'month') return d.getFullYear() + ' 年 ' + (d.getMonth() + 1) + ' 月';
    if (state.view === 'day') return dayLabel(d) + ' · ' + WEEKDAYS[(d.getDay() + 6) % 7];
    var week = startOfWeek(d);
    var last = addDays(week, 6);
    return dayLabel(week) + ' – ' + dayLabel(last);
  }

  var VIEWS = [
    { key: 'day', label: '日' },
    { key: 'week', label: '周' },
    { key: 'month', label: '月' },
  ];

  function renderBar() {
    document.getElementById('sch-title').textContent = titleText();
    var box = document.getElementById('sch-views');
    ui.clear(box);
    VIEWS.forEach(function (one) {
      box.appendChild(
        h(
          'button.sch-seg__item' + (state.view === one.key ? '.is-on' : ''),
          {
            type: 'button',
            role: 'tab',
            'aria-selected': state.view === one.key ? 'true' : 'false',
            onClick: function () {
              if (state.view === one.key) return;
              state.view = one.key;
              load();
            },
          },
          // 手机上"周"排出来的是议程，标签也说"列表"——不然名实不符
          one.key === 'week' && isNarrow() ? '列表' : one.label
        )
      );
    });
  }

  function shift(step) {
    if (state.view === 'day') state.cursor = addDays(state.cursor, step);
    else if (state.view === 'week') state.cursor = addDays(state.cursor, step * 7);
    else state.cursor = addMonths(state.cursor, step);
    load();
  }

  /* ------------------------------------------------------------ 左栏：计划 */

  function itemsOf(plan) {
    return plan.items || [];
  }

  function patchItem(item, body, done) {
    return api.patch('/plan-items/' + encodeURIComponent(item.id), body).then(function (data) {
      if (typeof done === 'function') done(data && data.item);
      else reload();
    });
  }

  /* ------------------------------------------------------------ 子页切换 */

  /** 顶上的胶囊：日历 / 计划（2026-11-09 用户：拆成两个子页，用滑块调）。
   *
   * 右边那个槽放**当前这页**的主操作 —— 计划是"新建计划"，日历的操作在它自己的
   * 工具条里（导航、视图、重排、新建事件），所以空着。 */
  function renderSwitch() {
    var seg = document.getElementById('sch-panes');
    var action = document.getElementById('sch-pane-action');
    if (!seg) return;
    ui.clear(seg);

    [
      { key: 'calendar', icon: 'calendar', label: '日历' },
      { key: 'plan', icon: 'task', label: '计划' },
    ].forEach(function (one) {
      var on = state.pane === one.key;
      seg.appendChild(
        h(
          'button.sch-panes__btn' + (on ? '.is-on' : ''),
          {
            type: 'button',
            role: 'tab',
            'aria-selected': on ? 'true' : 'false',
            title: one.label,
            'aria-label': one.label,
            onClick: function () {
              setPane(one.key);
            },
          },
          h('span', { html: ui.icon(one.icon, 17) })
        )
      );
    });

    if (!action) return;
    ui.clear(action);
    if (state.pane === 'plan') {
      action.appendChild(
        h('button.btn.btn--ghost.btn--sm', { type: 'button', onClick: createPlan }, '新建计划')
      );
    }
  }

  function setPane(pane) {
    if ((pane !== 'plan' && pane !== 'calendar') || state.pane === pane) return;
    state.pane = pane;
    try {
      window.localStorage.setItem(PANE_KEY, pane);
    } catch (e) {
      /* 隐私模式：记不住就记不住，不影响用 */
    }
    applyPane();
  }

  /** class 挂在 `#app-root` 上，CSS 只显示当前那页；两页都留在 DOM 里，切回来不重建。 */
  function applyPane() {
    var root = document.getElementById('app-root');
    if (root) {
      root.classList.toggle('is-plan', state.pane === 'plan');
      root.classList.toggle('is-cal', state.pane === 'calendar');
    }
    renderSwitch();
  }

  function renderSide() {
    var box = document.getElementById('sch-side');
    ui.clear(box);

    // 顶带连同"计划"栏名一起撤了：顶上那条胶囊已经写着这是哪一页（2026-11-09）。
    var body = h('div.sch-side__body');
    box.appendChild(body);

    if (!state.plans.length) {
      body.appendChild(
        h(
          'div.sch-empty',
          null,
          h('p.sch-empty__text', { text: '还没有计划。' }),
          h('p.sch-empty__hint', { text: '在对话里让 AI 给你订一份，或者右上角加一份空的。' })
        )
      );
    } else {
      state.plans.forEach(function (plan) {
        body.appendChild(planCard(plan));
      });
    }

    // 订阅摆在计划下面：它也是"要做什么"，只是事实在别处（课表、会议邀请）
    body.appendChild(subSection());
  }

  /* ------------------------------------------------------------ 左栏：订阅 */

  /** 只显示"课表来自哪份订阅"，不在这里改它。
   *
   * 订阅地址是**凭据**（课表的地址就是一串 JWT），它属于设置页那一段配置；
   * 日历页只回答"这一周为什么有这些块"。要同步或退订，去设置里做 ——
   * 同一个东西的开关只留一处。 */
  function subSection() {
    var wrap = h('div.sch-subs');
    wrap.appendChild(
      h(
        'div.sch-side__head.sch-side__head--sub',
        null,
        h('span.sch-side__title', { text: '订阅' })
      )
    );

    if (!state.subs.length) {
      wrap.appendChild(
        h('p.sch-subs__hint', {
          text: '还没有订阅。课表、会议邀请在「设置 → 日历」里加一串 iCalendar 地址。',
        })
      );
      return wrap;
    }

    state.subs.forEach(function (sub) {
      wrap.appendChild(
        h(
          'a.sch-subline',
          { href: 'settings.html#set-calendar', title: sub.url },
          h('span.sch-subline__name', { text: sub.title || sub.host || '订阅' }),
          h('span.sch-subline__meta', { text: sub.events + ' 块' })
        )
      );
    });
    wrap.appendChild(
      h('p.sch-subs__hint', { text: '在「设置 → 日历」里同步或退订。' })
    );
    return wrap;
  }

  function planCard(plan) {
    var items = itemsOf(plan);
    var done = items.filter(function (one) {
      return one.done;
    }).length;

    var card = h('div.sch-plan');

    var title = h('span.sch-plan__name', { text: plan.title || '未命名计划' });
    var rename = function () {
      var input = h('input.sch-plan__rename', { type: 'text', value: plan.title || '' });
      title.replaceWith(input);
      input.focus();
      input.select();
      var closed = false;
      var commit = function (save) {
        // 回车提交之后还会再来一次 blur：不加这道闸就会 PATCH 两次
        if (closed) return;
        closed = true;
        var next = String(input.value || '').trim();
        input.replaceWith(title);
        if (!save || !next || next === plan.title) return;
        api.patch('/plans/' + encodeURIComponent(plan.id), { title: next }).then(function () {
          plan.title = next;
          title.textContent = next;
        }, function (err) {
          ui.toast((err && err.message) || '改名没成功', 'bad', 2600);
        });
      };
      input.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter') commit(true);
        if (ev.key === 'Escape') commit(false);
      });
      input.addEventListener('blur', function () {
        commit(true);
      });
    };
    title.addEventListener('click', rename);

    card.appendChild(
      h(
        'div.sch-plan__head',
        null,
        title,
        h('span.sch-plan__count', { text: done + '/' + items.length }),
        h(
          'button.icon-btn.sch-plan__doc',
          {
            type: 'button',
            title: plan.docChars
              ? '打开计划书（' + plan.docChars + ' 字）'
              : '打开计划书',
            'aria-label': '打开计划书',
            onClick: function () {
              if (QF.plandoc) QF.plandoc.open(plan.id, plan.title);
              else ui.toast('计划书还没加载好，刷新一下页面', 'warn', 2000);
            },
          },
          h('span', { html: ui.icon('book', 14) })
        ),
        h(
          'button.icon-btn.sch-plan__del',
          {
            type: 'button',
            title: '删掉这份计划',
            'aria-label': '删掉这份计划',
            onClick: function () {
              ui.confirm('删掉「' + (plan.title || '未命名计划') + '」？它下面的条目与排出去的日程会一起删。', {
                okLabel: '删掉',
                danger: true,
              }).then(function (yes) {
                if (!yes) return;
                api.del('/plans/' + encodeURIComponent(plan.id)).then(reload, function (err) {
                  ui.toast((err && err.message) || '没删掉', 'bad', 2600);
                });
              });
            },
          },
          h('span', { html: ui.icon('trash', 14) })
        )
      )
    );

    var rows = h('div.sch-plan__rows');
    items.forEach(function (item) {
      rows.appendChild(itemRow(item));
    });
    card.appendChild(rows);

    var addInput = h('input.sch-plan__add', {
      type: 'text',
      placeholder: '＋ 添加一条',
      'aria-label': '添加一条待办',
    });
    var submit = function () {
      var text = String(addInput.value || '').trim();
      if (!text) return;
      addInput.disabled = true;
      api.post('/plan-items', { planId: plan.id, title: text }).then(
        function () {
          addInput.value = '';
          addInput.disabled = false;
          reload();
        },
        function (err) {
          addInput.disabled = false;
          ui.toast((err && err.message) || '没加上', 'bad', 2600);
        }
      );
    };
    addInput.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') submit();
    });
    card.appendChild(addInput);
    return card;
  }

  function itemRow(item) {
    var check = h(
      'button.sch-item__check' + (item.done ? '.is-done' : ''),
      {
        type: 'button',
        title: item.done ? '标成未完成' : '标成完成',
        'aria-label': item.done ? '标成未完成' : '标成完成',
        onClick: function () {
          // 传整行：卷子要贴在行**右侧**，贴在勾选框上会压住左栏
          toggleDone(item, row);
        },
      },
      h('span', { html: ui.icon('check', 11) })
    );

    // 宽屏那一行是 nowrap + 省略号：悬停要能看见全文（手机端会换行，用不上但不碍事）
    var text = h('span.sch-item__text' + (item.done ? '.is-done' : ''), {
      text: item.title,
      title: item.title,
    });
    text.addEventListener('dblclick', function () {
      var input = h('input.sch-item__rename', { type: 'text', value: item.title });
      text.replaceWith(input);
      input.focus();
      input.select();
      var closed = false;
      var commit = function (save) {
        if (closed) return;
        closed = true;
        var next = String(input.value || '').trim();
        input.replaceWith(text);
        if (!save || !next || next === item.title) return;
        patchItem(item, { title: next }, function () {
          item.title = next;
          text.textContent = next;
        });
      };
      input.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter') commit(true);
        if (ev.key === 'Escape') commit(false);
      });
      input.addEventListener('blur', function () {
        commit(true);
      });
    });

    var when = h(
      'button.sch-item__date' + (item.dueDate ? '.is-set' : ''),
      {
        type: 'button',
        title: '哪天做',
        onClick: function () {
          // 点一下就换成一个真的日期框：不弹层，改完直接落库
          var input = h('input.sch-item__dateinput', { type: 'date', value: item.dueDate || '' });
          when.replaceWith(input);
          input.focus();
          var closed = false;
          var commit = function (save) {
            if (closed) return;
            closed = true;
            var next = save ? String(input.value || '').trim() : String(item.dueDate || '');
            input.replaceWith(when);
            if (next === String(item.dueDate || '')) return;
            patchItem(item, { dueDate: next }, function () {
              item.dueDate = next || null;
              reload();
            });
          };
          input.addEventListener('change', function () {
            commit(true);
          });
          input.addEventListener('blur', function () {
            commit(true);
          });
          input.addEventListener('keydown', function (ev) {
            if (ev.key === 'Escape') commit(false);
          });
        },
      },
      item.dueDate ? shortDay(item.dueDate) : '哪天'
    );

    var star = h(
      'button.sch-item__star' + (item.important ? '.is-on' : ''),
      {
        type: 'button',
        title: '要紧',
        'aria-label': '要紧',
        onClick: function () {
          patchItem(item, { important: !item.important }, function () {
            item.important = !item.important;
            reload();
          });
        },
      },
      h('span', { html: ui.icon('star', 13) })
    );

    var row = h(
      'div.sch-item' + (item.done ? '.is-done' : ''),
      { draggable: 'true', title: '拖到右边日历里排期' },
      check,
      text,
      when,
      star
    );

    // 拖进日历：只有**真的放下**才建时间块（见 bindDrop）。
    row.addEventListener('dragstart', function (ev) {
      ev.dataTransfer.setData('text/x-qf-item', item.id);
      ev.dataTransfer.effectAllowed = 'copy';
      row.classList.add('is-dragging');
    });
    row.addEventListener('dragend', function () {
      row.classList.remove('is-dragging');
    });
    return row;
  }

  /** `2026-11-09` → `11月9日` */
  function shortDay(value) {
    var parts = String(value || '').split('-');
    if (parts.length !== 3) return value;
    return Number(parts[1]) + '月' + Number(parts[2]) + '日';
  }

  function createPlan() {
    api.post('/plans', { title: '新计划', items: [] }).then(reload, function (err) {
      ui.toast((err && err.message) || '没建成', 'bad', 2600);
    });
  }

  /* ------------------------------------------------------------ 重排 */

  function idSet(list) {
    var out = {};
    (list || []).forEach(function (one) {
      if (one && one.id) out[one.id] = true;
    });
    return out;
  }

  /** 摘掉一次性高亮与幽灵（4 秒后自己消失，不留在界面上）。 */
  function clearFresh() {
    state.fresh = null;
    state.ghosts = [];
    var marked = document.querySelectorAll('.sch-ev.is-new, .sch-ev.is-moved');
    for (var i = 0; i < marked.length; i += 1) marked[i].classList.remove('is-new', 'is-moved');
    var ghosts = document.querySelectorAll('.sch-ghost');
    for (var j = 0; j < ghosts.length; j += 1) {
      if (ghosts[j].parentNode) ghosts[j].parentNode.removeChild(ghosts[j]);
    }
  }

  /** 工具条下面那条临时提示：十二号字、六秒淡出，撤销是里面的文字按钮。 */
  function showNotice(text, onUndo) {
    var old = document.getElementById('sch-note');
    if (old && old.parentNode) old.parentNode.removeChild(old);
    var note = h('div.sch-note', { id: 'sch-note' }, h('span.sch-note__text', { text: text }));
    if (onUndo) {
      note.appendChild(
        h(
          'button.sch-note__action',
          {
            type: 'button',
            onClick: function () {
              if (note.parentNode) note.parentNode.removeChild(note);
              onUndo();
            },
          },
          '撤销'
        )
      );
    }
    document.getElementById('app-root').appendChild(note);
    requestAnimationFrame(function () {
      note.classList.add('is-in');
    });
    setTimeout(function () {
      note.classList.remove('is-in');
      setTimeout(function () {
        if (note.parentNode) note.parentNode.removeChild(note);
      }, 240);
    }, 6000);
  }

  function replan() {
    if (state.replanning) return;
    var btn = document.getElementById('sch-replan');
    var label = btn && btn.querySelector('.sch-replan__label');
    var old = label ? label.textContent : '';
    state.replanning = true;
    if (btn) btn.disabled = true;
    if (label) label.textContent = '排…';

    api.post('/schedule/replan', { horizonDays: 7, apply: true }).then(
      function (res) {
        state.replanning = false;
        if (btn) btn.disabled = false;
        if (label) label.textContent = old;

        var diff = (res && res.diff) || {};
        var counts = diff.counts || {};
        state.fresh = { created: idSet(diff.created), moved: idSet(diff.moved) };
        var movedItems = {};
        (diff.moved || []).forEach(function (one) {
          if (one && one.itemId) movedItems[one.itemId] = true;
        });
        // 幽灵只画"被挪走的那一拨"：删掉的不留（它们已经在提示里计数了）
        state.ghosts = (diff.deleted || []).filter(function (one) {
          return one && movedItems[one.itemId];
        });
        reload();
        setTimeout(clearFresh, 4000);

        var total = (counts.created || 0) + (counts.moved || 0) + (counts.deleted || 0);
        var text = total
          ? '已重排 · 新增 ' + (counts.created || 0) + ' / 移动 ' + (counts.moved || 0) +
            ' / 删除 ' + (counts.deleted || 0)
          : '已重排 · 没有需要改的地方';
        showNotice(
          text,
          total && res.runId
            ? function () {
                undoRun(res.runId);
              }
            : null
        );
      },
      function (err) {
        state.replanning = false;
        if (btn) btn.disabled = false;
        if (label) label.textContent = old;
        ui.toast((err && err.message) || '没排成', 'bad', 3600);
      }
    );
  }

  function undoRun(runId) {
    api.post('/schedule/runs/' + encodeURIComponent(runId) + '/undo', {}).then(
      function (res) {
        clearFresh();
        reload();
        ui.toast(
          '已撤销' + (res && res.restored ? ' · 还原 ' + res.restored + ' 块' : ''),
          'ok',
          2400
        );
      },
      function (err) {
        ui.toast((err && err.message) || '撤销失败', 'bad', 3600);
      }
    );
  }

  /* ------------------------------------------------------------ 门禁 */

  /** 勾完成 -> 先验一次门禁。
   *
   * 门禁是**协议**（见 `api/app/gates.py`）：这里只认 `kind`（quiz / text / none），
   * 不认"这是题库测验还是复述"。所以以后加一种门禁，这一页一个字都不用改。 */
  function toggleDone(item, node) {
    if (item.done) {
      patchItem(item, { done: false }, function () {
        item.done = false;
        reload();
      });
      return;
    }
    api.post('/gates/prepare', { itemId: item.id }).then(
      function (payload) {
        if (!payload || payload.gate === 'none' || payload.passed) {
          patchItem(item, { done: true }, function () {
            item.done = true;
            reload();
          });
          return;
        }
        openGate(item, payload, node);
      },
      function () {
        // 门禁读不出来（比如旧后端）：退化成直接勾掉 —— 别把人卡在这儿
        patchItem(item, { done: true }, function () {
          item.done = true;
          reload();
        });
      }
    );
  }

  var gate = { node: null, item: null, payload: null, choices: {}, rating: 0 };

  function closeGate() {
    if (gate.closer) document.removeEventListener('pointerdown', gate.closer);
    if (gate.node && gate.node.parentNode) gate.node.parentNode.removeChild(gate.node);
    gate = { node: null, item: null, payload: null, choices: {}, rating: 0, closer: null };
  }

  function openGate(item, payload, node) {
    closeGate();
    gate.item = item;
    gate.payload = payload;

    var box = h('div.sch-gate');
    var head = h(
      'div.sch-gate__head',
      null,
      h('span.sch-gate__title', { text: item.title }),
      h('span.sch-gate__tag', { text: payload.label || payload.gate })
    );
    var body = h('div.sch-gate__body');
    var hint = h('p.sch-gate__hint', { text: payload.prompt || '' });

    if (payload.kind === 'quiz') {
      (payload.questions || []).forEach(function (question, index) {
        var group = h('div.sch-gate__q');
        group.appendChild(
          h('p.sch-gate__stem', { text: index + 1 + '. ' + (question.stem || '') })
        );
        (question.options || []).forEach(function (option) {
          var key = String(option.key || '');
          var mark = h('span.sch-gate__radio');
          var row = h(
            'button.sch-gate__opt',
            {
              type: 'button',
              onClick: function () {
                gate.choices[question.id] = key;
                var all = group.querySelectorAll('.sch-gate__opt');
                for (var i = 0; i < all.length; i += 1) all[i].classList.remove('is-on');
                row.classList.add('is-on');
                mark.classList.add('is-on');
              },
            },
            mark,
            h('span.sch-gate__optkey', { text: key }),
            h('span', { text: String(option.text || '') })
          );
          group.appendChild(row);
        });
        body.appendChild(group);
      });
    } else {
      var area = h('textarea.sch-gate__area', {
        rows: '5',
        placeholder: payload.prompt || '写下来',
      });
      gate.area = area;
      body.appendChild(area);
      if (payload.degraded) {
        var stars = h('div.sch-gate__rating');
        for (var score = 1; score <= 5; score += 1) {
          (function (value) {
            var star = h(
              'button.sch-gate__star',
              {
                type: 'button',
                title: '自评 ' + value + ' 分（3 分以上才算过）',
                onClick: function () {
                  gate.rating = value;
                  var all = stars.querySelectorAll('.sch-gate__star');
                  for (var i = 0; i < all.length; i += 1) {
                    all[i].classList.toggle('is-on', i < value);
                  }
                },
              },
              h('span', { html: ui.icon('star', 12) })
            );
            stars.appendChild(star);
          })(score);
        }
        body.appendChild(
          h(
            'div.sch-gate__row',
            null,
            h('span.sch-gate__label', { text: '自评' }),
            stars,
            h('span.sch-gate__note', { text: '没有 AI 判分时以自评为准' })
          )
        );
      }
    }

    var error = h('p.sch-gate__error');
    var submitBtn = h('button.btn.btn--primary.btn--sm', { type: 'button' }, '提交');
    var skipBtn = h('button.btn.btn--ghost.btn--sm', { type: 'button' }, '跳过');
    skipBtn.addEventListener('click', closeGate);
    submitBtn.addEventListener('click', function () {
      submitBtn.disabled = true;
      error.textContent = '';
      var answer = {};
      if (payload.kind === 'quiz') {
        answer.choices = gate.choices;
      } else {
        answer.text = gate.area ? gate.area.value : '';
        answer.selfRating = gate.rating;
      }
      api.post('/gates/submit', { itemId: item.id, answer: answer }).then(
        function (res) {
          submitBtn.disabled = false;
          if (res && res.passed) {
            closeGate();
            ui.toast('过了 · ' + (res.detail || ''), 'ok', 2200);
            reload();
            return;
          }
          error.textContent = (res && res.detail) || '没过：再试一次，或者先跳过';
        },
        function (err) {
          submitBtn.disabled = false;
          error.textContent = (err && err.message) || '提交失败';
        }
      );
    });

    box.appendChild(head);
    if (payload.prompt) box.appendChild(hint);
    box.appendChild(body);
    box.appendChild(error);
    box.appendChild(h('div.sch-gate__foot', null, skipBtn, submitBtn));
    document.getElementById('app-root').appendChild(box);

    // 位置：贴在**行的右侧**（不压住左边那条待办清单），上下都不许跑出窗口。
    // 用实测高度收边：卷子有几道题不确定，写死 260 会让"提交"落在屏幕外。
    var rect = node && node.getBoundingClientRect ? node.getBoundingClientRect() : null;
    var root = document.getElementById('app-root').getBoundingClientRect();
    var width = 320;
    var height = box.offsetHeight || 300;
    var left = rect ? rect.right + 12 : window.innerWidth / 2 - width / 2;
    var top = rect ? rect.top - 8 : 140;
    var limit = window.innerHeight - height - 12;
    box.style.left = Math.max(12, Math.min(window.innerWidth - width - 12, left - root.left)) + 'px';
    box.style.top = Math.max(12, Math.min(limit, top) - root.top) + 'px';
    gate.node = box;

    // 点别处关掉（与编辑面板同一套习惯）。延到下一拍再挂：别被"打开它的那一次点击"关掉。
    setTimeout(function () {
      gate.closer = function (ev) {
        if (!gate.node || gate.node.contains(ev.target)) return;
        closeGate();
      };
      document.addEventListener('pointerdown', gate.closer);
    }, 0);

    if (gate.area) gate.area.focus();
  }

  /* ------------------------------------------------------------ 右栏：日历 */

  function eventsOn(day) {
    var from = startOfDay(day).getTime();
    var to = from + 24 * 3600 * 1000;
    return state.events.filter(function (one) {
      var s = new Date(one.startAt).getTime();
      var e = new Date(one.endAt).getTime();
      return s < to && e > from;
    });
  }

  function itemsDueOn(day) {
    var key = dayKey(day);
    var out = [];
    state.plans.forEach(function (plan) {
      itemsOf(plan).forEach(function (item) {
        if (item.dueDate === key) out.push({ item: item, plan: plan });
      });
    });
    return out;
  }

  function renderCal() {
    var box = document.getElementById('sch-cal');
    ui.clear(box);
    if (!state.ready) {
      box.appendChild(h('div.sch-loading', { text: '正在读取…' }));
      return;
    }
    if (state.view === 'month') {
      renderMonth(box);
      return;
    }
    if (isNarrow() && state.view === 'week') {
      // 手机上的"周"是**议程**：七列挤在 390px 里，每列 55px，标题只能剩一个字。
      // 议程从头读，不跟时间轴那一套"落到现在"——否则进来先看见周一的中段。
      pendingScroll = false;
      box.scrollTop = 0;
      renderAgenda(box, visibleRange().days);
      return;
    }
    renderTimeGrid(box, state.view === 'day' ? [startOfDay(state.cursor)] : visibleRange().days);
    if (pendingScroll) {
      pendingScroll = false;
      scrollToFocus();
    }
  }

  /**
   * 一进来先落在**有用的那一段**：包含今天就看现在前后，否则看早上八点。
   *
   * 不加这一下的话，页面打开永远是 00:00 —— 一个正常人的日程在 09:00 之后，
   * 于是"打开是一片空白，滚半天才看见东西"。这是日历类界面最常见的一处失礼。
   */
  function scrollToFocus() {
    var cal = document.getElementById('sch-cal');
    if (!cal) return;
    var range = visibleRange();
    var now = new Date();
    var anchor = 8;
    if (now >= range.from && now < range.to) anchor = Math.max(0, now.getHours() - 2);
    // 表头是 sticky 的（占 40px，但不算在滚动内容里），所以直接按小时数落位置。
    // 轴从 06:00 起（折叠时）或 00:00 起（展开时），两种状态下面都垫着那条 26px 的横带。
    cal.scrollTop =
      Math.max(0, ((anchor * 60 - nightStartMin()) / 60) * HOUR) + NIGHT_H;
  }

  /* ---- 手机上的议程（替代周视图） ---- */

  /** 一天一组，按时间排。
   *
   * 手机上七列并排每列只有 55px，事件标题被截成一个字（"大…"、"模…"）。
   * 议程把整屏还给一件事：时间一列、标题一行、地点跟在下面，点一行就开编辑。
   * 没有事的那天不占位置 —— 一周里空三天是常态，不该先翻三屏空白。
   */
  function renderAgenda(box, days) {
    var wrap = h('div.sch-agenda');
    var any = false;

    days.forEach(function (day) {
      var blocks = [];
      eventsOn(day).forEach(function (event) {
        var one = clipToDay(event, day);
        if (one) blocks.push(one);
      });
      if (!blocks.length) return;
      any = true;
      blocks.sort(function (a, b) {
        return a.startMin - b.startMin;
      });

      wrap.appendChild(
        h(
          'div.sch-agenda__head' + (isToday(day) ? '.is-today' : ''),
          null,
          h('span.sch-agenda__dow', { text: WEEKDAYS[(day.getDay() + 6) % 7] }),
          h('span.sch-agenda__num', { text: String(day.getDate()) }),
          isToday(day) ? h('span.sch-agenda__now', { text: '今天' }) : null
        )
      );
      blocks.forEach(function (one) {
        wrap.appendChild(agendaRow(one));
      });
    });

    if (!any) wrap.appendChild(h('p.sch-agenda__empty', { text: '这一周没有排事。' }));
    box.appendChild(wrap);
  }

  function agendaRow(one) {
    var event = one.event;
    var node = h(
      'button.sch-agenda__row'
        + (event.itemId ? '.is-plan' : '')
        + (event.subId ? '.is-sub' : '')
        + (event.allDay ? '.is-allday' : ''),
      {
        type: 'button',
        title: [
          event.title,
          whenText(event.startAt) + ' – ' + whenText(event.endAt),
          event.location,
          event.note,
        ]
          .filter(Boolean)
          .join(' · '),
        onClick: function () {
          editEvent(event, node.getBoundingClientRect());
        },
      },
      h(
        'span.sch-agenda__time',
        null,
        h('b', { text: clock(one.startMin) }),
        h('span', { text: clock(one.endMin) })
      ),
      h(
        'span.sch-agenda__main',
        null,
        h('span.sch-agenda__title', { text: event.title }),
        event.location ? h('span.sch-agenda__where', { text: event.location }) : null
      )
    );
    node.__eventId = event.id;
    return node;
  }

  /** 当天第几分钟 → `HH:MM`（24:00 也照写） */
  function clock(minutes) {
    var m = Math.max(0, Math.round(minutes));
    return pad(Math.min(24, Math.floor(m / 60))) + ':' + pad(m % 60);
  }

  /* ---- 月视图 ---- */

  function renderMonth(box) {
    var first = startOfWeek(startOfMonth(state.cursor));
    var month = state.cursor.getMonth();

    var grid = h('div.sch-month');
    WEEKDAYS.forEach(function (name) {
      grid.appendChild(h('div.sch-month__head', { text: name }));
    });

    for (var i = 0; i < CELLS; i += 1) {
      grid.appendChild(monthCell(addDays(first, i), month));
    }
    box.appendChild(grid);
  }

  function monthCell(day, month) {
    var outside = day.getMonth() !== month;
    var cell = h('div.sch-cell' + (outside ? '.is-out' : '') + (isToday(day) ? '.is-today' : ''), {
      'data-day': day.toISOString(),
    });

    var due = itemsDueOn(day).filter(function (one) {
      return !one.item.done;
    });
    cell.appendChild(
      h(
        'div.sch-cell__top',
        null,
        h('span.sch-cell__num', { text: String(day.getDate()) }),
        due.length ? h('span.sch-cell__dot', { title: due.length + ' 条到期' }) : null
      )
    );

    var list = h('div.sch-cell__list');
    eventsOn(day).slice(0, 3).forEach(function (event) {
      var start = new Date(event.startAt);
      list.appendChild(
        h(
          'button.sch-chip' + (event.itemId ? '.is-plan' : ''),
          {
            type: 'button',
            onClick: function (ev) {
              ev.stopPropagation();
              editEvent(event, cell.getBoundingClientRect());
            },
          },
          h('span.sch-chip__time', { text: pad(start.getHours()) + ':' + pad(start.getMinutes()) }),
          h('span.sch-chip__text', { text: event.title })
        )
      );
    });
    var more = eventsOn(day).length - 3;
    if (more > 0) list.appendChild(h('div.sch-cell__more', { text: '还有 ' + more + ' 项' }));
    cell.appendChild(list);

    // 新建要**右键**（用户要求）：左键留给"看"和"拖"，免得手一抖就在格子里
    // 排出一件事。右键是本机日历的惯例位置，浏览器菜单在这里本来也没用。
    cell.addEventListener('contextmenu', function (ev) {
      ev.preventDefault();
      if (ev.target.closest('.sch-chip')) return;
      // 月视图里没有时刻刻度：那天上午九点，一小时
      var start = new Date(day);
      start.setHours(9, 0, 0, 0);
      openEditor(cell.getBoundingClientRect(), {
        start: start,
        end: new Date(start.getTime() + 60 * 60000),
      });
    });

    bindDrop(cell, function (minutes, itemId) {
      var start = new Date(day);
      start.setHours(9, 0, 0, 0); // 月视图没有时刻刻度：统一排到当天九点
      return { start: start, itemId: itemId };
    });
    return cell;
  }

  /* ---- 日 / 周视图 ---- */

  function renderTimeGrid(box, days) {
    var wrap = h('div.sch-grid' + (days.length === 1 ? '.is-day' : ''));

    var rail = h('div.sch-rail');
    rail.appendChild(h('div.sch-rail__head'));
    // 这条两个状态都画：折叠时它是"被压掉的 0–6"，展开时它是收回去的把手。
    rail.appendChild(
      h('div.sch-rail__night' + (state.nightOpen ? '.is-open' : ''), {
        // 48px 宽的左栏装不下"凌晨 0–6 / 收起 0–6"：只留区间，方向交给箭头
        text: '0–6',
        title: state.nightOpen ? '收起凌晨时段（0:00–6:00）' : '展开凌晨时段（0:00–6:00）',
        onClick: toggleNight,
      })
    );
    for (var hour = state.nightOpen ? 0 : NIGHT_FROM; hour < 24; hour += 1) {
      // 展开后 00:00 也写出来：顶部现在有 34px 的折叠条挡着，不会再和它挤在一起，
      // 空着反而像少了一行（2026-11-09）。
      rail.appendChild(h('div.sch-rail__hour', { text: pad(hour) + ':00' }));
    }
    wrap.appendChild(rail);

    var cols = h('div.sch-cols');
    days.forEach(function (day) {
      cols.appendChild(timeColumn(day));
    });
    wrap.appendChild(cols);

    if (days.length === 1) wrap.appendChild(daySide(days[0]));
    box.appendChild(wrap);
  }

  function timeColumn(day) {
    var col = h('div.sch-col' + (isToday(day) ? '.is-today' : ''));
    col.appendChild(
      h(
        'div.sch-col__head',
        null,
        h('span.sch-col__dow', { text: WEEKDAYS[(day.getDay() + 6) % 7] }),
        h('span.sch-col__num', { text: String(day.getDate()) })
      )
    );

    var body = h('div.sch-col__body', { 'data-day': day.toISOString() });
    body.style.height = 24 * HOUR + nightTop() - nightShift() + 'px';
    // 小时线跟着轴一起下移：折叠时轴从 06:00 起，展开时从条下面起，
    // 两态的第一根线都在条的底边（条 26px）上。
    body.style.backgroundPosition = '0 ' + NIGHT_H + 'px';

    // 折叠条要横跨整行：左栏那条给文字，每列这一条只负责把底色连起来
    body.appendChild(
      h('div.sch-col__night', {
        title: state.nightOpen ? '收起凌晨时段（0:00–6:00）' : '展开凌晨时段（0:00–6:00）',
        onClick: toggleNight,
      })
    );

    // 时间块：绝对定位，位置由"从零点起多少分钟"换算，不铺 48 个半小时格子
    var blocks = [];
    eventsOn(day).forEach(function (event) {
      var one = clipToDay(event, day);
      if (!one) return;
      // 折叠凌晨时 0–6 点整段看不见；跨过 6:00 的块从 6:00 起画
      if (one.endMin <= nightStartMin()) return;
      one.startMin = Math.max(one.startMin, nightStartMin());
      blocks.push(one);
    });
    layoutLanes(blocks);
    blocks.forEach(function (one) {
      body.appendChild(eventBlock(one, day));
    });

    // 重排刚挪走的块：在**旧位置**留 4 秒虚线幽灵（不占车道、不吃事件）
    state.ghosts.forEach(function (ghost) {
      var start = new Date(ghost.startAt);
      var end = new Date(ghost.endAt);
      if (isNaN(start.getTime()) || isNaN(end.getTime())) return;
      if (!sameDay(start, day)) return;
      var node = h('div.sch-ghost', null, h('span.sch-ghost__title', { text: ghost.title || '' }));
      var startMin = minutesOf(start);
      var endMin = sameDay(end, day) ? minutesOf(end) : 24 * 60;
      if (endMin <= nightStartMin()) return;
      startMin = Math.max(startMin, nightStartMin());
      node.style.top = Math.max(0, (startMin / 60) * HOUR - nightShift() + nightTop()) + 'px';
      node.style.height = Math.max(18, ((endMin - startMin) / 60) * HOUR - 2) + 'px';
      body.appendChild(node);
    });

    // 与月视图同一条规矩：**右键**空白处新建（左键留给查看与拖拽）
    body.addEventListener('contextmenu', function (ev) {
      ev.preventDefault();
      if (ev.target.closest('.sch-ev')) return;
      var rect = body.getBoundingClientRect();
      // body 顶端先是那条横带（26px），再往下才是轴的起点：
      // 折叠时起点是 06:00，展开时是 00:00，两者都靠 nightShift/nightTop 换算。
      var minutes =
        Math.round(((ev.clientY - rect.top - nightTop() + nightShift()) / HOUR) * 60 / SNAP) * SNAP;
      minutes = Math.max(nightStartMin(), Math.min(23 * 60 + 45, minutes));
      var start = new Date(day);
      start.setHours(0, minutes, 0, 0);
      openEditor(
        { left: ev.clientX, top: ev.clientY, width: 0, height: 0 },
        { start: start, end: new Date(start.getTime() + 60 * 60000) }
      );
    });

    bindDrop(body, function (minutes, itemId) {
      var start = new Date(day);
      start.setHours(0, Math.round(minutes / SNAP) * SNAP, 0, 0);
      return { start: start, itemId: itemId };
    });
    col.appendChild(body);
    return col;
  }

  function clipToDay(event, day) {
    var from = startOfDay(day);
    var to = addDays(from, 1);
    var start = new Date(event.startAt);
    var end = new Date(event.endAt);
    var a = start < from ? from : start;
    var b = end > to ? to : end;
    if (b <= a) return null;
    return {
      event: event,
      startMin: (a - from) / 60000,
      endMin: (b - from) / 60000,
      lane: 0,
      lanes: 1,
    };
  }

  /** 重叠的时间块并排 —— 贪心分道，够用且不会把顺序打乱 */
  function layoutLanes(blocks) {
    var ends = [];
    blocks.forEach(function (one) {
      var lane = 0;
      while (ends[lane] !== undefined && ends[lane] > one.startMin) lane += 1;
      ends[lane] = one.endMin;
      one.lane = lane;
    });
    var total = Math.max(1, ends.length);
    blocks.forEach(function (one) {
      one.lanes = total;
    });
  }

  function eventBlock(one, day) {
    var event = one.event;
    var top = (one.startMin / 60) * HOUR - nightShift() + nightTop();
    var height = Math.max(18, ((one.endMin - one.startMin) / 60) * HOUR - 2);
    var width = 100 / one.lanes;
    // 半小时的块只有 34px：竖排两行（时间在上、标题在下）会被挤成半句。
    // 摆不下就改成横排一行，宁可少显示几个字，也别让字叠在一起。
    var tight = height < 40;
    var node = h(
      'div.sch-ev'
        + (event.itemId ? '.is-plan' : '')
        + (event.subId ? '.is-sub' : '')
        + (event.allDay ? '.is-allday' : '')
        + (tight ? '.is-tight' : '')
        // 刚重排出来的块：一次性的高亮（4 秒后由 `clearFresh` 摘掉，不常驻颜色）
        + (state.fresh && state.fresh.created[event.id] ? '.is-new' : '')
        + (state.fresh && state.fresh.moved[event.id] ? '.is-moved' : ''),
      {
        // 悬停里给全的那一份：标题 · 时段 · 地点 · 备注
        title: [
          event.title,
          whenText(event.startAt) + ' – ' + whenText(event.endAt),
          event.location,
          event.note,
        ]
          .filter(Boolean)
          .join(' · '),
      },
      h('span.sch-ev__time', { text: whenText(event.startAt) }),
      h('span.sch-ev__title', { text: event.title }),
      // 教室直接写在块上 —— 要悬停才看得见的教室等于没有。
      // 块矮到放不下第三行（半小时那种）时省掉，免得把标题挤成半句。
      event.location && !tight && height >= 44
        ? h('span.sch-ev__where', { text: event.location })
        : null,
      h('span.sch-ev__resize', { 'aria-hidden': 'true' })
    );
    node.style.top = top + 'px';
    node.style.height = height + 'px';
    node.style.left = 'calc(' + one.lane * width + '% + 2px)';
    node.style.width = 'calc(' + width + '% - 4px)';
    // 拖拽松手时要知道改的是哪一块（`dragBy` 只拿得到节点）
    node.__eventId = event.id;

    bindEventDrag(node, event, day);
    return node;
  }

  function whenText(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  /** 日视图右侧：今天到期 + 今天排了的事，一列读完 */
  function daySide(day) {
    var box = h('aside.sch-day');
    var due = itemsDueOn(day);
    var blocks = eventsOn(day);

    box.appendChild(h('h2.sch-day__h', { text: '当天' }));
    if (!due.length && !blocks.length) {
      box.appendChild(h('p.sch-day__empty', { text: '这天是空的。左边拖一条进来。' }));
      return box;
    }

    if (due.length) {
      box.appendChild(h('h3.sch-day__sub', { text: '到期 ' + due.length + ' 条' }));
      due.forEach(function (one) {
        box.appendChild(
          h(
            'div.sch-day__row' + (one.item.done ? '.is-done' : ''),
            null,
            h('span.sch-day__plan', { text: one.plan.title }),
            h('span.sch-day__text', { text: one.item.title })
          )
        );
      });
    }
    if (blocks.length) {
      box.appendChild(h('h3.sch-day__sub', { text: '已排 ' + blocks.length + ' 块' }));
      blocks.forEach(function (one) {
        box.appendChild(
          h(
            'div.sch-day__row',
            null,
            h('span.sch-day__time', { text: whenText(one.startAt) }),
            h(
              'div.sch-day__main',
              null,
              h('span.sch-day__text', { text: one.title }),
              // 备注在日视图里**展开写**：周视图那一格放不下，看一天的时候正好有地方
              one.note ? h('span.sch-day__note', { text: one.note }) : null
            ),
            one.location ? h('span.sch-day__where', { text: one.location }) : null
          )
        );
      });
    }
    return box;
  }

  /* ------------------------------------------------------------ 事件拖拽 */

  function bindEventDrag(node, event, day) {
    var handle = node.querySelector('.sch-ev__resize');

    // 钉住的块与**课表来的块**都不许拖：前者是"别动我"，后者是"这块的形状由订阅决定"
    //（`sub_id` 非空）。一手滑就把课挪走，而它应该跟着学校走 —— 见 docs/日程调度.md
    // 那张"谁决定放在哪"的表。点一下照旧开编辑器：要改在那里改，那是明确的动作。
    if (event.locked || event.subId) {
      node.classList.add('is-locked');
      if (handle) handle.remove();
      node.title =
        (node.title || '') + (event.subId ? '（课表来的，拖不动）' : '（已钉住，拖不动）');
      var downX = 0;
      var downY = 0;
      var moved = false;
      var onMove = function (e) {
        if (Math.abs(e.clientX - downX) > 4 || Math.abs(e.clientY - downY) > 4) moved = true;
      };
      node.addEventListener('pointerdown', function (ev) {
        if (ev.button !== 0) return;
        downX = ev.clientX;
        downY = ev.clientY;
        moved = false;
        document.addEventListener('pointermove', onMove);
        document.addEventListener('pointerup', function up(e) {
          document.removeEventListener('pointermove', onMove);
          document.removeEventListener('pointerup', up);
          if (!moved) editEvent(event, node.getBoundingClientRect());
        });
      });
      return;
    }

    if (handle) {
      handle.addEventListener('pointerdown', function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        dragBy(ev, node, function (minutes) {
          var end = new Date(new Date(event.endAt).getTime() + minutes * 60000);
          if (end.getTime() - new Date(event.startAt).getTime() < 15 * 60000) return null;
          return { endAt: end.toISOString() };
        });
      });
    }

    node.addEventListener('pointerdown', function (ev) {
      if (ev.button !== 0 || ev.target === handle) return;
      dragBy(ev, node, function (minutes) {
        if (!minutes) return null;
        var start = new Date(new Date(event.startAt).getTime() + minutes * 60000);
        var end = new Date(new Date(event.endAt).getTime() + minutes * 60000);
        return { startAt: start.toISOString(), endAt: end.toISOString() };
      }, function (moved) {
        // 没移动就是"点了一下"：开编辑器。移动过才算是拖。
        if (!moved) editEvent(event, node.getBoundingClientRect());
        return !moved;
      });
    });
  }

  /**
   * 一段通用的"按住拖，松手写库"。
   *
   * 拖的时候只改 `transform`，不重画 —— 重画会把正在跟手的这个元素换掉。
   * 松手才发请求，失败就把位移清掉（等于回弹），并说清楚没成。
   */
  function dragBy(ev, node, buildBody, onClickFallback) {
    var startY = ev.clientY;
    var minutes = 0;
    var moved = false;
    node.setPointerCapture(ev.pointerId);
    node.classList.add('is-drag');

    function onMove(e) {
      var dy = e.clientY - startY;
      if (Math.abs(dy) > 3) moved = true;
      minutes = Math.round(dy / (HOUR / 60) / SNAP) * SNAP;
      node.style.transform = 'translateY(' + (minutes / 60) * HOUR + 'px)';
    }

    function onUp(e) {
      node.removeEventListener('pointermove', onMove);
      node.removeEventListener('pointerup', onUp);
      node.releasePointerCapture(ev.pointerId);
      node.classList.remove('is-drag');
      node.style.transform = '';

      if (onClickFallback && onClickFallback(moved)) return;
      var body = buildBody(minutes);
      if (!body) return;
      api.patch('/schedule/' + encodeURIComponent(node.dataset.id || node.__eventId), body).then(
        reload,
        function (err) {
          ui.toast((err && err.message) || '改期没成功', 'bad', 2600);
          reload();
        }
      );
    }

    node.addEventListener('pointermove', onMove);
    node.addEventListener('pointerup', onUp);
  }

  /* ------------------------------------------------------------ 拖条目进日历 */

  function bindDrop(target, place) {
    var line = null;

    function ensureLine() {
      if (!line) {
        line = h('div.sch-dropline');
        target.appendChild(line);
      }
      return line;
    }

    target.addEventListener('dragover', function (ev) {
      if (!ev.dataTransfer.types || ev.dataTransfer.types.indexOf('text/x-qf-item') < 0) return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = 'copy';
      var rect = target.getBoundingClientRect();
      var y = Math.max(0, Math.min(rect.height - 2, ev.clientY - rect.top));
      var l = ensureLine();
      l.style.top = y + 'px';
      l.hidden = false;
    });

    target.addEventListener('dragleave', function (ev) {
      if (line && !target.contains(ev.relatedTarget)) line.hidden = true;
    });

    target.addEventListener('drop', function (ev) {
      var itemId = ev.dataTransfer.getData('text/x-qf-item');
      if (!itemId) return;
      ev.preventDefault();
      if (line) line.hidden = true;
      var rect = target.getBoundingClientRect();
      // 与右键新建同一套换算：先让开顶上那条横带，再补回被压掉的凌晨段
      var minutes = ((ev.clientY - rect.top - nightTop() + nightShift()) / HOUR) * 60;
      minutes = Math.max(nightStartMin(), minutes);
      var placed = place(minutes, itemId);
      if (!placed) return;

      var item = findItem(itemId);
      var span = (item && item.estimateMin) || 60;
      api
        .post('/schedule', {
          itemId: itemId,
          title: item ? item.title : '',
          startAt: placed.start.toISOString(),
          endAt: new Date(placed.start.getTime() + span * 60000).toISOString(),
        })
        .then(reload, function (err) {
          ui.toast((err && err.message) || '没排上', 'bad', 2600);
        });
    });
  }

  function findItem(itemId) {
    for (var i = 0; i < state.plans.length; i += 1) {
      var items = itemsOf(state.plans[i]);
      for (var j = 0; j < items.length; j += 1) {
        if (items[j].id === itemId) return items[j];
      }
    }
    return null;
  }

  /* ------------------------------------------------------------ 编辑器 */

  var editorOpen = false;
  var closer = null;

  function closeEditor() {
    var box = document.getElementById('sch-editor');
    box.hidden = true;
    ui.clear(box);
    editorOpen = false;
    if (closer) {
      document.removeEventListener('mousedown', closer);
      closer = null;
    }
  }

  function editEvent(event, rect) {
    openEditor(rect, {
      id: event.id,
      title: event.title,
      start: new Date(event.startAt),
      end: new Date(event.endAt),
      itemId: event.itemId,
      location: event.location,
      note: event.note,
      locked: event.locked,
      // 课表来的块在编辑器里要说明"为什么拖不动"（见下面的 pinRow）
      subId: event.subId,
    });
  }

  /** `rect` 可以是 getBoundingClientRect 的结果，也可以是 `{left, top}`（鼠标位置） */
  function openEditor(rect, draft) {
    var box = document.getElementById('sch-editor');
    box.hidden = false;
    ui.clear(box);
    editorOpen = true;

    var title = h('input.sch-edit__title', {
      type: 'text',
      value: draft.title || '',
      placeholder: '做什么',
      'aria-label': '标题',
    });
    var date = h('input.sch-edit__date', { type: 'date', value: dayKey(draft.start) });
    var from = h('input.sch-edit__time', { type: 'time', value: whenText(draft.start.toISOString()) });
    var to = h('input.sch-edit__time', { type: 'time', value: whenText(draft.end.toISOString()) });
    // 地点与备注：订阅来的块本来就有（ICS 的 LOCATION / DESCRIPTION），
    // 手动建的块也得能写 —— 否则"在哪个教室"只有课表能回答。
    var where = h('input.sch-edit__where', {
      type: 'text',
      value: draft.location || '',
      placeholder: '在哪（教室 / 线上 / 图书馆）',
      'aria-label': '地点',
    });
    var note = h('textarea.sch-edit__note', {
      rows: 2,
      value: draft.note || '',
      placeholder: '备注（要带的书、要交的东西）',
      'aria-label': '备注',
    });
    // 钉住 = 拖不动（`ScheduleEvent.locked`）。课程、考试、约好的时间被手一滑
    // 挪走是事故；求解器也不许动它。**默认关**，只有人明确勾上才锁。
    // 课表来的块（`subId`）本来就拖不动，勾选框在它身上是假的 —— 换一句说明，
    // 免得"没勾却拖不动"看起来像坏了。
    var pinned = h('input', { type: 'checkbox', checked: !!draft.locked });
    var pinRow = draft.subId
      ? h('div.sch-edit__pin.is-static', null, h('span', { text: '课表来的：日历上拖不动，改时间请在上面改' }))
      : h(
          'label.sch-edit__pin',
          { title: '钉住之后不能在日历上拖动' },
          pinned,
          h('span', { text: '钉住（拖不动）' })
        );

    function compose() {
      var start = new Date(date.value + 'T' + (from.value || '09:00'));
      var end = new Date(date.value + 'T' + (to.value || '10:00'));
      if (isNaN(start.getTime()) || isNaN(end.getTime())) return null;
      if (end <= start) return null;
      return { start: start, end: end };
    }

    var save = h('button.btn.btn--primary.btn--sm', { type: 'button' }, '保存');
    save.addEventListener('click', function () {
      var span = compose();
      if (!span) {
        ui.toast('结束时间要晚于开始时间', 'warn', 2000);
        return;
      }
      var body = {
        title: String(title.value || '').trim() || '未命名',
        startAt: span.start.toISOString(),
        endAt: span.end.toISOString(),
        location: String(where.value || '').trim(),
        note: String(note.value || '').trim(),
        locked: !!pinned.checked,
      };
      if (draft.itemId) body.itemId = draft.itemId;
      var call = draft.id
        ? api.patch('/schedule/' + encodeURIComponent(draft.id), body)
        : api.post('/schedule', body);
      call.then(
        function () {
          closeEditor();
          reload();
        },
        function (err) {
          ui.toast((err && err.message) || '没保存上', 'bad', 2600);
        }
      );
    });

    var actions = [save];
    if (draft.id) {
      var del = h('button.btn.btn--ghost.btn--sm', { type: 'button' }, '删掉');
      del.addEventListener('click', function () {
        api.del('/schedule/' + encodeURIComponent(draft.id)).then(
          function () {
            closeEditor();
            reload();
          },
          function (err) {
            ui.toast((err && err.message) || '没删掉', 'bad', 2600);
          }
        );
      });
      actions.unshift(del);
    }

    box.appendChild(
      h(
        'div.sch-edit',
        null,
        title,
        h('div.sch-edit__row', null, date, from, h('span.sch-edit__dash', { text: '–' }), to),
        where,
        note,
        pinRow,
        h('div.sch-edit__foot', null, actions)
      )
    );

    // 位置：贴着被点的地方，但不许跑出窗口（高度按实测算 —— 加了地点与备注，
    // 面板比原来高，写死 160 会让"保存"掉到屏幕外）
    var left = (rect.left || 0) + 8;
    var top = (rect.top || 0) + 8;
    var root = document.getElementById('app-root').getBoundingClientRect();
    var limit = window.innerHeight - box.offsetHeight - 8;
    box.style.left = Math.max(8, Math.min(window.innerWidth - 328, left - root.left)) + 'px';
    box.style.top = Math.max(8, Math.min(limit, top) - root.top) + 'px';

    title.focus();
    title.select();

    // 点别处关掉（capture 阶段判一下是不是在面板里）
    closer = function (ev) {
      if (box.contains(ev.target)) return;
      closeEditor();
    };
    setTimeout(function () {
      if (editorOpen) document.addEventListener('mousedown', closer);
    }, 0);
  }

  /* ------------------------------------------------------------ 装配 */

  function render() {
    applyPane();
    renderBar();
    renderSide();
    renderCal();
  }

  function bindBar() {
    // 工具条是静态 HTML，但外层壳可能在启动后重排一次 DOM（`QF.shell.mount`）：
    // 直接绑在那个节点上会随重排一起失效（实测"第一次点重排没反应"）。
    // 挂在 document 上按 id 委托，节点被换掉也照样接得住。
    document.addEventListener('click', function (ev) {
      var btn = ev.target && ev.target.closest ? ev.target.closest('#sch-replan') : null;
      if (btn) replan();
    });
    document.getElementById('sch-prev').addEventListener('click', function () {
      shift(-1);
    });
    document.getElementById('sch-next').addEventListener('click', function () {
      shift(1);
    });
    document.getElementById('sch-today').addEventListener('click', function () {
      state.cursor = new Date();
      load();
    });
    document.getElementById('sch-new').addEventListener('click', function () {
      var start = new Date(state.cursor);
      var now = new Date();
      start.setHours(state.view === 'day' ? now.getHours() : 9, 0, 0, 0);
      openEditor(
        { left: window.innerWidth / 2 - 150, top: 120 },
        { start: start, end: new Date(start.getTime() + 60 * 60000) }
      );
    });
  }

  function boot() {
    QF.shell.mount({});
    bindBar();
    load();
    // 跨过 600px 要换一套排法（七列 ↔ 议程、单行 ↔ 两行），不然转屏后还站在原地
    if (NARROW && NARROW.addEventListener) {
      NARROW.addEventListener('change', function () {
        if (!state.ready) return;
        renderBar();
        renderCal();
      });
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
