/* ===========================================================================
 * wellbeing.js —— 「今天还剩多少脑力」这一块：三段精力带 + 两个打卡按钮。
 *
 * ## 为什么单独一个模块
 *
 * 它先说在日程页左栏，2026-11-08 用户把它挪到了首页（"现在空白的首页可以放东西了"）。
 * 挪的时候只搬一次代码：谁想再放第二个地方，调 `QF.wellbeing.mount(box)` 即可。
 *
 * ## 它只画，不算
 *
 * 曲线、深度预算、该不该催睡**全在服务端**（`app/wellbeing/`，策略注册表）。
 * 前端要是自己拿睡眠时长算一条曲线，就会有两份真相 —— 而且两边一改就不一致。
 * 这里只做三件事：把 48 槽折成早/午/晚三段、显示昨晚与预算、发打卡请求。
 * ========================================================================= */
(function () {
  'use strict';

  var QF = window.QF;
  var ui = QF.ui;
  var h = ui.h;
  var api = QF.api;

  //: 三段怎么切（48 个半小时槽）+ 给悬停看的那句话。早 7:00–12:00、午 12:00–18:00、晚 18:00–23:00。
  var BANDS = [
    ['早', 14, 24, '07:00–12:00'],
    ['午', 24, 36, '12:00–18:00'],
    ['晚', 36, 46, '18:00–23:00'],
  ];

  function pad(n) {
    return n < 10 ? '0' + n : String(n);
  }

  function hhmm(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  function hoursText(minutes) {
    var m = Math.max(0, Math.round(Number(minutes) || 0));
    if (m < 60) return m + 'm';
    var rest = m % 60;
    return Math.floor(m / 60) + 'h' + (rest ? pad(rest) + 'm' : '');
  }

  function bandAvg(slots, left, right) {
    var sum = 0;
    var count = 0;
    for (var index = left; index < right && index < slots.length; index += 1) {
      sum += Number(slots[index]) || 0;
      count += 1;
    }
    return count ? sum / count : 0;
  }

  var state = { body: null, failed: false, busy: false, node: null };

  function checkIn(which) {
    if (state.busy) return;
    state.busy = true;
    api.post('/wellbeing/sleep/' + which, {}).then(
      function (res) {
        state.busy = false;
        state.body = (res && res.today) || state.body;
        state.failed = false;
        paint();
      },
      function (err) {
        state.busy = false;
        ui.toast((err && err.message) || '没记上', 'bad', 2600);
      }
    );
  }

  function cardHead() {
    var body = state.body || {};
    var meta = [
      (body.regime && body.regime.title) || body.profileLabel || '',
      '深度预算 ' + hoursText((body.curve && body.curve.deepBudgetMin) || 0),
    ]
      .filter(Boolean)
      .join(' · ');
    return h(
      'div.wb-card__head',
      null,
      h('span.wb-card__title', { text: '今天的精力' }),
      meta ? h('span.wb-card__meta', { text: meta }) : null
    );
  }

  function strip() {
    var slots = (state.body.curve && state.body.curve.slots) || [];
    var row = h('div.wb-card__strip');
    BANDS.forEach(function (one) {
      var avg = bandAvg(slots, one[1], one[2]);
      // **用长度表示精力，不用颜色深浅**：浅色档里 `--bg2` 是纯白，
      // 而 `--pri` 本身是很浅的薄荷绿 —— 按浓度调出来三段几乎一样，
      // 等于什么都没说。长度在深浅两档里都读得出来。
      var fill = h('i.wb-card__fill');
      fill.style.width = Math.round(8 + avg * 92) + '%';
      var bar = h('div.wb-card__band', {
        title: one[3] + ' · 精力 ' + Math.round(avg * 100) + '%',
      });
      bar.appendChild(fill);
      // 标签在前：`[早][▬▬▬]` 一眼就是"这一段是早上"，反过来（`[▬▬▬][早]`）
      // 会让人以为标签属于右边那一段。
      row.appendChild(
        h('div.wb-card__cell', null, h('span.wb-card__cap', { text: one[0] }), bar)
      );
    });
    return row;
  }

  function foot() {
    var body = state.body;
    var active = body.active || null;
    var sleep = body.sleep || {};

    var sleepBtn = h(
      'button.wb-card__btn' + (active ? '.is-on' : ''),
      {
        type: 'button',
        title: active ? '已经记下入睡时刻' : '记下现在去睡',
        onClick: function () {
          if (!active) checkIn('start');
        },
      },
      active ? '已记录 · ' + hhmm(active.bedAt) : '睡了'
    );
    var wakeBtn = h(
      'button.wb-card__btn',
      { type: 'button', title: '记下现在起床', onClick: function () { checkIn('wake'); } },
      '起床了'
    );

    var line = [
      sleep.lastNightMin ? '昨晚 ' + hoursText(sleep.lastNightMin) : '昨晚没记',
      Number(sleep.debtHours) > 0 ? '还欠 ' + sleep.debtHours + 'h' : '',
    ]
      .filter(Boolean)
      .join(' · ');

    return h(
      'div.wb-card__foot',
      null,
      h('span.wb-card__line', {
        text: line,
        title: (body.curve && body.curve.headline) || '',
      }),
      h('span.wb-card__acts', null, sleepBtn, wakeBtn)
    );
  }

  function paint() {
    var node = state.node;
    if (!node) return;
    ui.clear(node);

    var card = h('section.wb-card');
    if (!state.body) {
      card.classList.add('is-quiet');
      card.appendChild(
        h('div.wb-card__head', null, h('span.wb-card__title', { text: '今天的精力' }))
      );
      card.appendChild(
        h(
          'div.wb-card__foot',
          null,
          h('span.wb-card__line', { text: state.failed ? '读不到精力数据' : '正在算…' }),
          state.failed
            ? h('button.wb-card__btn', { type: 'button', onClick: load }, '重试')
            : null
        )
      );
      node.appendChild(card);
      return;
    }

    card.appendChild(cardHead());
    card.appendChild(strip());
    card.appendChild(foot());
    node.appendChild(card);
  }

  function load() {
    return api.get('/wellbeing/today').then(
      function (res) {
        state.body = res || null;
        state.failed = !res;
        paint();
      },
      function () {
        state.body = null;
        state.failed = true;
        paint();
      }
    );
  }

  /** 挂到某个容器里（容器被清空、卡片重建）。重复 mount 会各管各的。 */
  function mount(box) {
    state.node = box;
    paint(); // 先把骨架画出来：接口慢的时候首页也不该是一块空白
    return load();
  }

  QF.wellbeing = { mount: mount, refresh: load };
})();
