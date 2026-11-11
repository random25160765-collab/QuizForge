/* pullsync.js —— 下拉同步（手指在列表顶上往下拉）
 *
 * 为什么要有它：自动同步（`api/app/autosync.py`）原来是每 5 秒一次 Turso 探测，
 * 手机上等于每 5 秒一次 HTTPS，射频一直醒着 —— 待机耗电的主因（用户 2026-11-11）。
 * 现在自动节拍放宽到 30 秒，**"刚改完就要同步"交回给人**：在列表顶上拉一下。
 *
 * 只认 touch：鼠标拖不是这个语汇，桌面照旧用设置页的「立即同步」。
 * 提示是一条**固定定位**的小胶囊 —— 手势不该让长列表重排（那比同步本身还贵）。
 */
(function () {
  'use strict';

  var QF = window.QF || (window.QF = {});
  var PULL_MAX = 88;   // 最多跟到这么远（再拉也不多给反馈）
  var TRIGGER = 52;    // 松手时超过这个距离才真的同步
  var REST = -56;      // 收起时停在屏幕外（连阴影也看不见）

  var pill = null;
  var pillTimer = 0;
  var busy = false;

  function node() {
    if (pill) return pill;
    pill = document.createElement('div');
    pill.className = 'pullsync';
    document.body.appendChild(pill);
    return pill;
  }

  function show(offset, text, tone) {
    var el = node();
    if (pillTimer) window.clearTimeout(pillTimer);
    pillTimer = 0;
    el.textContent = text;
    el.classList.add('is-on');
    el.classList.toggle('is-ok', tone === 'ok');
    el.classList.toggle('is-bad', tone === 'bad');
    el.style.transform = 'translate(-50%, ' + (offset - 56) + 'px)';
  }

  function hide(delay) {
    if (pillTimer) window.clearTimeout(pillTimer);
    pillTimer = window.setTimeout(function () {
      pillTimer = 0;
      var el = node();
      el.classList.remove('is-on', 'is-ok', 'is-bad');
      el.style.transform = 'translate(-50%, ' + REST + 'px)';
    }, delay || 0);
  }

  /** 结果怎么念：同步日志里那三个字段（见 `sync.status()` 的 `last`）。 */
  function resultText(last) {
    if (last && last.ok === false) return { text: '同步没成功', tone: 'bad' };
    if (last && (last.pushed || last.pulled)) {
      return { text: '已同步 · 推 ' + (last.pushed || 0) + ' 拉 ' + (last.pulled || 0), tone: 'ok' };
    }
    return { text: '已是最新', tone: 'ok' };
  }

  function poll(tries, onDone) {
    QF.api.get('/sync/status').then(function (st) {
      if (st && st.running && tries < 50) {
        window.setTimeout(function () { poll(tries + 1, onDone); }, 1200);
        return;
      }
      var result = resultText(st && st.last);
      show(TRIGGER, result.text, result.tone);
      hide(1600);
      busy = false;
      // 同步完让页面自己把数据再拉一遍 —— 收藏、置顶、日程这些都可能被对面改过
      if (typeof onDone === 'function') {
        try {
          onDone();
        } catch (err) {
          /* 刷新失败不影响"同步成功"这个结论 */
        }
      }
    }, function () {
      show(TRIGGER, '同步状态读不到', 'bad');
      hide(1800);
      busy = false;
    });
  }

  function run(onDone) {
    if (busy) return;
    busy = true;
    show(TRIGGER, '正在同步…');
    QF.api.post('/sync/run', {}).then(function () {
      poll(0, onDone);
    }, function (err) {
      // 400 是"没配置 / 开关关着"（见 `routers/sync_api.py`），其余按网络失败说
      var text = err && err.status === 400 ? '还没配置同步' : '同步没成功';
      show(TRIGGER, text, 'bad');
      hide(1800);
      busy = false;
    });
  }

  /**
   * 挂到某个滚动容器上。`onDone` 在同步结束后调用（页面自己刷新数据）。
   *
   * 判定只有三条：单指、容器已在顶端（`scrollTop <= 0`）、往下拉。
   * 任何一条不满足就把手势还给列表 —— 横滑留给抽屉（`swipe.js`），上滑留给滚动。
   */
  function attach(scroller, options) {
    if (!scroller || scroller.__qfPull) return;
    scroller.__qfPull = true;
    var startY = 0;
    var startX = 0;
    var pulling = false;
    var pull = 0;
    var onDone = options && options.onDone;

    scroller.addEventListener('touchstart', function (ev) {
      if (busy || ev.touches.length !== 1) return;
      if (scroller.scrollTop > 0) return;
      startY = ev.touches[0].clientY;
      startX = ev.touches[0].clientX;
      pulling = true;
      pull = 0;
    }, { passive: true });

    scroller.addEventListener('touchmove', function (ev) {
      if (!pulling) return;
      var dy = ev.touches[0].clientY - startY;
      var dx = ev.touches[0].clientX - startX;
      if (dy <= 0 || Math.abs(dx) > Math.abs(dy) || scroller.scrollTop > 0) {
        pulling = false;
        if (pull) hide(0);
        pull = 0;
        return;
      }
      pull = Math.min(PULL_MAX, dy * 0.55);
      show(pull, pull >= TRIGGER ? '松手同步' : '下拉同步');
      // 拦掉系统回弹：不拦的话拉到一半会被它抢走，手感是"一卡一卡"
      ev.preventDefault();
    }, { passive: false });

    var end = function () {
      if (!pulling) return;
      pulling = false;
      if (pull >= TRIGGER) {
        pull = 0;
        run(onDone);
      } else {
        pull = 0;
        hide(0);
      }
    };
    scroller.addEventListener('touchend', end, { passive: true });
    scroller.addEventListener('touchcancel', end, { passive: true });
  }

  QF.pullSync = { attach: attach };
})();
