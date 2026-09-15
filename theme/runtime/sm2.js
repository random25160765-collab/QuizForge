/* ===========================================================================
 * sm2.js —— SM2 间隔重复调度
 *
 * 四档自评映射到 SM2 的 quality：
 *   0 重来 again → q=2（本次遗忘，重置连续正确计数）
 *   1 困难 hard  → q=3（勉强答对，间隔打折、难度系数下调）
 *   2 一般 good  → q=4（标准路径）
 *   3 简单 easy  → q=5（轻松答对，间隔上调、难度系数上调）
 *
 * 状态字段：ef（难度系数，>=1.3）、interval（天）、reps（连续正确次数）、
 *           lapses（遗忘次数）、due（下次复习时间戳）
 * ========================================================================= */
(function () {
  'use strict';

  var QF = (window.QF = window.QF || {});

  var DAY = 86400000;
  var MIN_EF = 1.3;
  var MAX_EF = 2.8;
  var MAX_INTERVAL_DAYS = 365;
  var AGAIN_MINUTES = 10; // 「重来」在 10 分钟后重新出现

  var GRADE_META = [
    { key: 'again', label: '重来', hint: '完全不会', tone: 'bad', q: 2 },
    { key: 'hard', label: '困难', hint: '勉强想起', tone: 'warn', q: 3 },
    { key: 'good', label: '一般', hint: '正常答出', tone: 'ok', q: 4 },
    { key: 'easy', label: '简单', hint: '毫不费力', tone: 'info', q: 5 },
  ];

  function fresh(now) {
    var t = now || Date.now();
    return { ef: 2.5, interval: 0, reps: 0, lapses: 0, lastGrade: null, due: t, lastAt: t };
  }

  function ensure(state, now) {
    var base = fresh(now);
    if (!state || typeof state !== 'object') return base;
    return {
      ef: typeof state.ef === 'number' && state.ef > 0 ? state.ef : base.ef,
      interval: typeof state.interval === 'number' ? state.interval : 0,
      reps: typeof state.reps === 'number' ? state.reps : 0,
      lapses: typeof state.lapses === 'number' ? state.lapses : 0,
      lastGrade: state.lastGrade == null ? null : state.lastGrade,
      due: typeof state.due === 'number' ? state.due : base.due,
      lastAt: typeof state.lastAt === 'number' ? state.lastAt : base.due,
    };
  }

  /**
   * 根据自评档位计算下一个复习状态。
   * @param {object} state 现有状态（可为 null）
   * @param {number} grade 0..3
   * @param {number} [now]
   * @returns {object} 新状态（不修改入参）
   */
  function update(state, grade, now) {
    var t = now || Date.now();
    var s = ensure(state, t);
    var g = Math.max(0, Math.min(3, Math.round(grade)));
    var q = GRADE_META[g].q;

    var ef = s.ef + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02));
    var interval;
    var reps = s.reps;
    var lapses = s.lapses;

    if (q < 3) {
      // 遗忘：重置连续正确计数，短间隔重现
      reps = 0;
      lapses += 1;
      interval = AGAIN_MINUTES / (60 * 24);
    } else {
      if (reps === 0) {
        interval = 1;
      } else if (reps === 1) {
        interval = 6;
      } else {
        interval = s.interval * ef;
      }
      if (g === 1) interval *= 0.6; // 困难：间隔打折
      if (g === 3) {
        interval *= 1.3; // 简单：间隔拉长
        ef += 0.1;
      }
      if (g === 1) ef -= 0.15;
      reps += 1;
    }

    ef = Math.max(MIN_EF, Math.min(MAX_EF, ef));
    interval = Math.max(AGAIN_MINUTES / (60 * 24), Math.min(MAX_INTERVAL_DAYS, interval));

    return {
      ef: Math.round(ef * 1000) / 1000,
      interval: Math.round(interval * 1000) / 1000,
      reps: reps,
      lapses: lapses,
      lastGrade: g,
      due: t + interval * DAY,
      lastAt: t,
    };
  }

  /** 预览「如果选这一档，下次什么时候复习」 */
  function preview(state, grade, now) {
    var next = update(state, grade, now);
    return {
      state: next,
      text: humanInterval(next.interval),
      dueLabel: QF.ui ? QF.ui.fmtRelative(next.due) : new Date(next.due).toLocaleString(),
    };
  }

  function humanInterval(days) {
    if (days < 1 / 24) return Math.max(1, Math.round(days * 24 * 60)) + ' 分钟后';
    if (days < 1) return Math.round(days * 24) + ' 小时后';
    if (days < 30) return Math.round(days) + ' 天后';
    if (days < 365) return (days / 30).toFixed(1).replace(/\.0$/, '') + ' 个月后';
    return (days / 365).toFixed(1).replace(/\.0$/, '') + ' 年后';
  }

  /** 从记录集合里筛出到期待复习的（按到期时间升序） */
  function dueList(records, now) {
    var t = now || Date.now();
    var out = [];
    Object.keys(records || {}).forEach(function (id) {
      var rec = records[id];
      if (!rec || !rec.sm2) return;
      if (rec.mastered) return;
      if (typeof rec.sm2.due === 'number' && rec.sm2.due <= t) out.push(rec);
    });
    return out.sort(function (a, b) {
      return a.sm2.due - b.sm2.due;
    });
  }

  /** 概览：今日到期 / 未来 7 天 / 已掌握 / 未开始 */
  function overview(records, allIds, now) {
    var t = now || Date.now();
    var dueToday = 0;
    var week = 0;
    var learning = 0;
    var scheduled = 0;
    var inSeven = t + 7 * DAY;

    Object.keys(records || {}).forEach(function (id) {
      var rec = records[id];
      if (!rec || !rec.sm2) return;
      if (rec.mastered) return;
      scheduled += 1;
      if (rec.sm2.due <= t) dueToday += 1;
      else if (rec.sm2.due <= inSeven) week += 1;
      if (rec.sm2.reps > 0) learning += 1;
    });

    return {
      dueToday: dueToday,
      week: week,
      learning: learning,
      scheduled: scheduled,
      untouched: Math.max(0, (allIds || []).length - scheduled),
    };
  }

  QF.sm2 = {
    fresh: fresh,
    ensure: ensure,
    update: update,
    preview: preview,
    dueList: dueList,
    overview: overview,
    humanInterval: humanInterval,
    grades: GRADE_META,
    DAY: DAY,
  };
})();
