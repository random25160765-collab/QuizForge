/* 抽屉的边缘手势：从左边缘往右划展开左栏，在抽屉上往左划收回去
 * （右栏反过来：右边缘往左划展开、往右划收回）。
 *
 * 手机上左栏是盖在正文上的抽屉，原先只能点顶栏那颗按钮，手指划不动
 *（用户 2026-11-09："无论是手机还是平板，都没办法通过右划调出左边的面板，
 * 而只能点击那个按钮。几个页面都是这样"）。
 *
 * 只做**判定**（划够了就切），不做跟手位移：抽屉的收/展本来就是 180ms 的
 * translate，让它自己跑完比逐帧跟手干净 —— 跟手还要再处理松手回弹与"划一半
 * 停住"，两种手感混在同一个抽屉上，比"一划到底"更容易出错。
 *
 * 四条避免误触的约定（都是拿真手指比过的）：
 *   * **只有从边缘起手才算展开**（24px 以内）：正文里的横向滚动 —— 代码块、
 *     长公式、题目选项 —— 不该把抽屉拽出来；
 *   * 横向位移要压过纵向 1.2 倍才算数：竖着滚列表时手指总会歪一点；
 *   * 先走够 12px 才判方向：那之前的位移是在区分"点一下"和"划一下"；
 *   * 展开要求位移 ≥48px，收起也是 —— 同一个阈值，来回手感对称。
 *
 * 事件挂在 `document` 上（捕获阶段不用）：抽屉、遮罩、正文都在这棵树下，
 * 谁起手都收得到；是不是"该管的手"由 `enabled()` 决定（窄屏才启用）。
 */
(function () {
  var QF = (window.QF = window.QF || {});

  var EDGE = 24;      // 边缘感应宽度（px）
  var SLOP = 12;      // 走够这么远才判方向
  var TRIGGER = 48;   // 划够这么远才切换

  /** 绑一个抽屉的边缘手势。
   *
   * @param {object} opts
   *   - `edge`   'left'（默认）| 'right'：抽屉从哪一侧滑出来
   *   - `enabled()` 现在该不该管（窄屏切进切出时会变，所以是个函数）
   *   - `isOpen()`  抽屉现在开着没有
   *   - `set(open)` 切到开 / 关（**只认目标态**，不要 toggle：连着划两下时
   *                 toggle 会跟状态赛跑，第二下可能反过来）
   */
  function bind(opts) {
    var edge = opts.edge === 'right' ? 'right' : 'left';
    var dir = edge === 'right' ? -1 : 1;   // 「展开」时手指该走的方向
    var active = null;

    function onStart(ev) {
      if (!opts.enabled() || ev.touches.length !== 1) return;
      var t = ev.touches[0];
      var open = opts.isOpen();
      if (!open) {
        var inEdge = edge === 'left'
          ? t.clientX <= EDGE
          : t.clientX >= window.innerWidth - EDGE;
        if (!inEdge) return;
      }
      active = { x: t.clientX, y: t.clientY, open: open, axis: '', ok: false };
    }

    function onMove(ev) {
      if (!active || ev.touches.length !== 1) return;
      var t = ev.touches[0];
      var dx = t.clientX - active.x;
      var dy = t.clientY - active.y;
      if (!active.axis) {
        if (Math.abs(dx) < SLOP && Math.abs(dy) < SLOP) return;
        if (Math.abs(dx) <= Math.abs(dy) * 1.2) {
          active = null;   // 竖着走：这是滚动列表，不是划抽屉
          return;
        }
        // 方向得对：关着的时候要往「出来」的方向划，开着的时候要往「回去」的方向划
        var right = active.open ? dx * dir < 0 : dx * dir > 0;
        if (!right) {
          active = null;
          return;
        }
        active.axis = 'x';
      }
      // 意图明确了才拦默认行为（横向滚动 / 浏览器自己的边缘回退）
      if (ev.cancelable) ev.preventDefault();
    }

    function onEnd(ev) {
      if (!active) return;
      var t = (ev.changedTouches && ev.changedTouches[0]) || null;
      var dx = t ? t.clientX - active.x : 0;
      var open = active.open;
      active = null;
      if (!open && dx * dir >= TRIGGER) opts.set(true);
      else if (open && dx * dir <= -TRIGGER) opts.set(false);
    }

    document.addEventListener('touchstart', onStart, { passive: true });
    document.addEventListener('touchmove', onMove, { passive: false });
    document.addEventListener('touchend', onEnd, { passive: true });
    document.addEventListener('touchcancel', function () { active = null; }, { passive: true });
  }

  QF.swipe = { bind: bind };
})();
