/**
 * 正文里的 LaTeX 图：交给**真 TeX 引擎**（TikZJax 的离线副本，见 `vendor/tikzjax/SOURCE.md`）。
 *
 * ## 分工
 *
 * `md.js` 抽公式时看到 `\begin{…}` 就问一句 `QF.latex.kind()`，是图就不往 KaTeX 送
 * —— KaTeX 认不了这些环境（它会把源码原样吐在正文里，一片红字）。判据刻意只看
 * "有没有 `\begin`"：不维护白名单，就不会再有"这个环境我没支持"这种事。
 * 引擎自带 `tikz` / `tikz-cd` / `pgfplots` / `circuitikz`（**前言是我们在
 * `run-tex.js` 里补过的**，见那份 SOURCE.md 的说明），所以按 TikZ 正常写法写就行。
 *
 * ## 为什么是"每批一个新文档"
 *
 * 引擎只在**文档加载那一刻**扫一遍页面里的 `text/tikz`（监听的是 `DOMContentLoaded`）。
 * 正文是流式长出来的，那次早过去了。试过两条不对的路：
 *
 *   * 在正文文档里补发合成的 `DOMContentLoaded` —— 不稳（占位一直停在"正在编译"，
 *     而同样的内容在一个**真文档**里能编出来）；
 *   * 隐藏 iframe + `document.write` —— `about:blank` 那个窗口的 load 早就触发过，
 *     不会再触发第二次，于是扫描器一次都不跑（不报错、也不出图）。
 *
 * 所以反过来：**让文档去等图** —— 每批都往 iframe 里装一份新文档（`srcdoc`），
 * 它自己的加载事件会正常到来，引擎自己会扫。
 *
 * ## 两个必须记住的坑
 *
 * 1. **转圈框与"坏图"标记也是 `<svg>`** —— 曾经按"有没有 svg"验收，两次把占位
 *    当成了图。判据必须是类名（`tikzjax-loader` / `tikzjax-broken`）＋真 viewBox。
 * 2. **工位不能隐藏**（`display:none`、`visibility:hidden` 都算）—— 引擎要量尺寸，
 *    量出 0 就静默卡住。只能移出视野。
 */
(function () {
  'use strict';

  var QF = (window.QF = window.QF || {});

  /** 引擎入口（构建时由 `vendor/tikzjax/` 拷进 `/assets/tikzjax/`）。 */
  var ENGINE = '__ORIGIN__/assets/tikzjax/tikzjax.js';
  /** 攒多久算一批：WASM 初始化不便宜，逐个编译会一顿一顿。 */
  var BATCH_MS = 120;
  /** 一批最多等多久。 */
  var RENDER_TIMEOUT = 20000;

  function origin() {
    return window.location.origin || '';
  }

  /**
   * 这段 TeX 交给引擎吗？**只看有没有 `\begin{…}`** —— 不列白名单，
   * 就不会再有"这个环境没支持"（吃过这个亏：手写的迷你渲染器 + 一串提示词约束）。
   */
  function kind(tex) {
    return /\\begin\s*\{/.test(String(tex == null ? '' : tex)) ? 'tex' : '';
  }

  /* ------------------------------------------------------------ 取图 */

  /**
   * 把引擎给的 SVG 收拾一下。
   *
   * **裁到内容**是必须的一步：引擎的输出带着一圈 `-72 -72` 的纸边（1 英寸），
   * 而我们从前又用 `width:100%` 去铺满栏宽 —— 两个叠起来就是"有的太大有的太小"：
   * 小图被拉成一整栏、大图里又空一片（用户："图有的太大有的太小"）。
   * 现在按**它画了多大**就显示多大（TikZ 的 1cm ≈ 28.45pt ≈ 37.9px），
   * 上限是栏宽。太小就让模型把坐标画大点 —— 那才是它该判断的事。
   */
  function adopt(svg) {
    var pad = 2; // 一点点余量：描边粗的图别贴边
    try {
      var box = svg.getBBox();
      if (box && box.width > 1 && box.height > 1) {
        svg.setAttribute(
          'viewBox',
          [box.x - pad, box.y - pad, box.width + pad * 2, box.height + pad * 2].join(' ')
        );
        // 1pt = 1/72in、1px = 1/96in → pt 当 px 用会小掉四分之一，所以按 4/3 折算
        svg.setAttribute('width', Math.round((box.width + pad * 2) * (4 / 3)) + 'px');
        svg.setAttribute('height', Math.round((box.height + pad * 2) * (4 / 3)) + 'px');
        svg.removeAttribute('style');
      }
    } catch (err) {
      /* 量不到就按引擎给的原样（至少不会更坏） */
    }
    var text = svg.outerHTML
      .replace(/(stroke|fill)="(black|#000000|#000)"/g, '$1="currentColor"')
      .replace(/style="([^"]*)"/g, function (whole, body) {
        return 'style="' + body.replace(/(stroke|fill)\s*:\s*(black|#000000|#000)/g, '$1:currentColor') + '"';
      });
    return '<div class="diagram__svg">' + text + '</div>';
  }

  var frame = null;

  function ensureFrame() {
    if (frame && frame.parentNode) return frame;
    frame = document.createElement('iframe');
    frame.className = 'latex-frame';
    frame.setAttribute('aria-hidden', 'true');
    frame.setAttribute('tabindex', '-1');
    // **只移出视野，绝不隐藏**：引擎要量尺寸（`getBBox`），隐藏元素量出来是 0。
    frame.style.cssText = 'position:fixed;left:-10000px;top:0;width:1000px;height:800px;border:0';
    document.body.appendChild(frame);
    // 引擎把失败原因写在 console 里（"TeX did not produce input.dvi" + TeX 的日志）。
    // 不抓就走失了 —— 那样用户只看到"没编出来"，不知道是自己哪句写法不被支持
    // （实测：他有一段带 `phantom` / `very near start` 的 tikz-cd 就编不出来）。
    try {
      var win = frame.contentWindow;
      win.onerror = function (msg) {
        lastError = String(msg);
      };
      ['error', 'warn'].forEach(function (level) {
        var original = win.console && win.console[level];
        if (!original) return;
        win.console[level] = function () {
          try {
            lastError = Array.prototype.slice
              .call(arguments)
              .map(function (one) {
                return String((one && one.message) || one);
              })
              .join(' ')
              .slice(0, 400);
          } catch (err) {
            /* 抓不到就算了 */
          }
          return original.apply(win.console, arguments);
        };
      });
    } catch (err) {
      /* 挂不上不影响编译 */
    }
    return frame;
  }

  var queue = [];
  var timer = 0;
  var lastError = '';

  /**
   * 把一个批次交给引擎。
   *
   * **第一份文档建起来之后留着复用**：引擎的 WASM 初始化不便宜，每批都新开一份
   * 文档就等于每批重来一次（用户："编译太慢"）。所以之后每批只是往**同一个工位
   * 文档**里追加一批 `.one`，并在**那份文档**上补发一次 `DOMContentLoaded`
   * —— 引擎的扫描函数挂在那个事件上，派发就会走到（在正文文档里补发不稳，
   * 但在**工位文档**里，那个文档本来就是这个引擎的地盘，Workers/WASM 都是热的）。
   * 万一这条路没出图（短超时），就退回"新开一份文档"这条一直好使的退路。
   */
  function batch() {
    var items = queue;
    queue = [];
    timer = 0;
    lastError = '';
    if (!items.length) return;

    var doc;
    try {
      doc = ensureFrame();
    } catch (err) {
      items.forEach(function (one) {
        one.fail(err);
      });
      return;
    }

    var theme = (document.documentElement.getAttribute('data-theme') || 'dark').replace(/[^a-z]/g, '');
    var blocks = items
      .map(function (one, index) {
        return (
          '<div class="one" data-i="' +
          index +
          '"><script type="text/tikz">' +
          String(one.tex).replace(/<\/script/gi, '<\/script') +
          '</script></div>'
        );
      })
      .join('');

    var handled = false;
    var settleIn = function (root, removeAfter) {
      if (handled) return;
      handled = true;
      var started = Date.now();
      var settle = function () {
        var found = [];
        var done = true;
        items.forEach(function (one, index) {
          var holder = root.querySelector('.one[data-i="' + index + '"]');
          var svg = holder && holder.querySelector('svg');
          var cls = svg ? svg.getAttribute('class') || '' : '';
          // 转圈框与"坏图"标记**也是 svg**，别当成图（这个坑踩过两回）
          if (svg && !/tikzjax-loader|tikzjax-broken/.test(cls)) found.push(adopt(svg));
          else {
            found.push(null);
            done = false;
          }
        });
        if (!done && Date.now() - started < RENDER_TIMEOUT) {
          doc.contentWindow.setTimeout(settle, 200);
          return;
        }
        if (removeAfter && removeAfter.parentNode) removeAfter.parentNode.removeChild(removeAfter);
        items.forEach(function (one, index) {
          if (found[index]) {
            one.ok(found[index]);
          } else {
            // 编不出来：把源码原样给他看，比一块空白强（他会知道是哪段没吃下去）
            one.fail(
              new Error(
                '引擎编不出这段 LaTeX（多半是用了它不支持的宏包或装饰性选项 —— 把写法简化一下再试）' +
                  (lastError ? '：' + lastError.replace(/\s+/g, ' ').slice(0, 220) : '（引擎没报原因）')
              )
            );
          }
        });
      };
      doc.contentWindow.setTimeout(settle, 250);
    };

    var dom = null;
    try {
      dom = doc.contentDocument;
    } catch (err) {
      dom = null;
    }
    var warm = dom && dom.body && dom.querySelector('script[src*="tikzjax"]');

    if (warm) {
      // 热工位：追加一批，补发事件，短超时；不出图就退回复用不了 → 新开文档
      var host = dom.createElement('div');
      host.className = 'batch';
      host.innerHTML = blocks;
      dom.body.appendChild(host);
      try {
        dom.dispatchEvent(new Event('DOMContentLoaded'));
      } catch (err) {
        lastError = '补发 DOMContentLoaded 失败：' + err.message;
      }
      var shortcut = Date.now();
      var check = function () {
        if (handled) return;
        var any = host.querySelector('svg');
        var cls = any ? any.getAttribute('class') || '' : '';
        if (any && !/tikzjax-loader|tikzjax-broken/.test(cls)) {
          settleIn(host, host);
          return;
        }
        if (Date.now() - shortcut < 2500) {
          doc.contentWindow.setTimeout(check, 200);
          return;
        }
        // 退回新文档（下面那段）
        if (host.parentNode) host.parentNode.removeChild(host);
        fresh();
      };
      doc.contentWindow.setTimeout(check, 200);
      return;
    }

    // 用函数声明（会被提升）：热工位那条路的超时回退要调它，而它在下面才写 ——
    // 写成 `var` 就会撞上"还没赋值就被调用"（实测：TypeError: fresh is not a function，
    // 于是编不出来时占位永远停住，比显示源码更坏）。
    function fresh() {
      var page =
        '<!doctype html><html data-theme="' +
        theme +
        '"><head><meta charset="utf-8"><title>latex</title>' +
        '<link rel="stylesheet" href="' +
        origin() +
        '/assets/tikzjax/fonts.css">' +
        '</head><body>' +
        blocks +
        '<script src="' +
        ENGINE.replace('__ORIGIN__', origin()) +
        '"></scr' +
        'ipt></body></html>';
      doc.onload = function () {
        if (doc.contentDocument) settleIn(doc.contentDocument, null);
      };
      doc.srcdoc = page;
      // 兜底：onload 万一没来，到点也去问一次
      window.setTimeout(function () {
        if (!handled && doc.contentDocument) settleIn(doc.contentDocument, null);
      }, 1500);
    }
    fresh();
  }

  /** 画一张（异步）。一批里的图合成一次编译。 */
  function render(tex) {
    return new Promise(function (resolve, reject) {
      queue.push({
        tex: tex,
        ok: resolve,
        fail: function (err) {
          reject(err);
        },
      });
      if (!timer) timer = window.setTimeout(batch, BATCH_MS);
    });
  }

  /**
   * 字体表要在**本文档**里也挂一份。
   *
   * 引擎产出的 SVG 是"文字 + 字体名"（`cmr10` / `cmmi10` 这些），字体靠 `@font-face`
   * 提供。我们把 SVG 从工位搬进正文文档之后，如果正文这边没有那份 `@font-face`，
   * 浏览器就只有缺字形的回退字体 —— 每个公式符号都变成一个**方框**
   * （用户："字体没有正确渲染，出来的是框框"）。
   */
  function ensureFonts() {
    if (document.querySelector('link[data-latex-fonts]')) return;
    var link = document.createElement('link');
    link.rel = 'stylesheet';
    link.setAttribute('data-latex-fonts', '1');
    link.href = origin() + '/assets/tikzjax/fonts.css';
    document.head.appendChild(link);
  }

  /* ------------------------------------------------------------ 编好的图存下来 */

  /**
   * **刷新页面不该重编**（用户："刷新页面会重编这个不太好，处理一下"）。
   *
   * 编译一次要跑 WASM、几秒起步；而同一段 LaTeX 编出来的 SVG 是**确定的**
   * （我们把颜色统一成了 `currentColor`，所以它跟主题也无关）。所以按源码哈希存下来：
   * 命中就直接摆进正文（同步、零等待、连工位都不用建）。
   *
   * `CACHE_TAG` 是**总开关**：引擎换了、前言补了包、adopt 的改写规则变了 —— 任何
   * 一件都会让旧结果不再正确，改这个字符串即可整体作废（别去逐条删）。
   */
  var CACHE_TAG = 'qf.latex.v2';
  var CACHE_MAX = 60; /* 最多存几张 */
  var CACHE_CHARS = 1500000; /* 或总字符上限，先到先弃 */

  /**
   * 键 = **两个独立哈希 + 长度**。
   *
   * 为什么不止一个哈希：撞了不是多编一次，而是**串图** —— 把别人的图摆到这段
   * LaTeX 下面（比不缓存更坏）。两个 32 位哈希各不相同（FNV-1a 与 djb2），
   * 再加上长度，实际碰撞概率可以当没有。
   */
  function cacheKey(tex) {
    var text = String(tex);
    var fnv = 2166136261;
    var djb = 5381;
    for (var i = 0; i < text.length; i++) {
      var code = text.charCodeAt(i);
      fnv = ((fnv ^ code) * 16777619) >>> 0;
      djb = ((djb * 33) ^ code) >>> 0;
    }
    return CACHE_TAG + ':' + fnv.toString(36) + '-' + djb.toString(36) + '-' + text.length.toString(36);
  }

  function cacheGet(key) {
    try {
      var raw = window.localStorage.getItem(key);
      if (!raw) return null;
      return JSON.parse(raw).html || null;
    } catch (err) {
      return null; // 存不下/坏了都当没有，别拦住这次渲染
    }
  }

  /** 超量就按时间淘汰（localStorage 可枚举，不用另存索引）。 */
  function sweep() {
    try {
      var store = window.localStorage;
      var rows = [];
      for (var i = 0; i < store.length; i++) {
        var key = store.key(i);
        if (!key || key.indexOf(CACHE_TAG + ':') !== 0) continue;
        var raw = store.getItem(key) || '';
        var at = 0;
        try {
          at = JSON.parse(raw).at || 0;
        } catch (err) {
          at = 0;
        }
        rows.push({ key: key, at: at, len: raw.length });
      }
      var total = rows.reduce(function (sum, one) {
        return sum + one.len;
      }, 0);
      if (rows.length <= CACHE_MAX && total <= CACHE_CHARS) return;
      rows.sort(function (a, b) {
        return a.at - b.at; // 最旧的先走
      });
      for (var j = 0; j < rows.length; j++) {
        if (rows.length - j <= CACHE_MAX && total <= CACHE_CHARS) break;
        store.removeItem(rows[j].key);
        total -= rows[j].len;
      }
    } catch (err) {
      /* 淘汰失败不影响这次渲染 */
    }
  }

  function cachePut(key, html) {
    try {
      window.localStorage.setItem(key, JSON.stringify({ at: Date.now(), html: html }));
      sweep();
    } catch (err) {
      // 配额满了：清一轮再来一次，还不行就算了
      sweep();
      try {
        window.localStorage.setItem(key, JSON.stringify({ at: Date.now(), html: html }));
      } catch (again) {
        /* 放弃缓存，不影响本次渲染 */
      }
    }
  }

  /* ------------------------------------------------------------ 给正文用的出口 */

  /**
   * 还没编译的图：`tex` 留在 JS 这边（不进 HTML —— 模型写的 LaTeX 里什么字符都可能有，
   * 塞进属性要反复转义），DOM 里只放一个编号占位。占位符也不该被"复制正文"带走。
   */
  var slots = [];

  function slot(tex) {
    slots.push(tex);
    return (
      '<div class="diagram-wrap is-loading" data-latex="' + (slots.length - 1) + '">正在用 TeX 编译这张图…</div>'
    );
  }

  /** 把 `root` 里的占位换成真图（异步）。 */
  function fill(root) {
    if (!root || !root.querySelectorAll) return;
    var places = root.querySelectorAll('[data-latex]');
    for (var i = 0; i < places.length; i++) {
      (function (place) {
        var tex = slots[Number(place.getAttribute('data-latex'))];
        if (tex === undefined) return;
        place.removeAttribute('data-latex');
        ensureFonts();
        var key = cacheKey(tex);
        var hit = cacheGet(key);
        if (hit) {
          // 命中：同步摆上，刷新页面时图是"一打开就在"（不闪、不跑 WASM）
          place.className = 'diagram-wrap';
          place.innerHTML = hit;
          return;
        }
        render(tex).then(
          function (html) {
            place.className = 'diagram-wrap';
            place.innerHTML = html;
            cachePut(key, html);
          },
          function (err) {
            place.className = 'diagram-wrap is-failed';
            place.textContent =
              '这段 LaTeX 没编出来：' +
              ((err && err.message) || '未知原因') +
              String.fromCharCode(10) +
              tex;
          }
        );
      })(places[i]);
    }
  }

  /* ------------------------------------------------------------ 后台静默常备 */

  /**
   * 页面一得空就把引擎热起来，**不等第一张图**。
   *
   * 与 Pyodide 那份预热是同一个意思（服务端那套见 `api/app/startup.py`：进程起来就把
   * 重的东西备好，别让第一个用户等）—— 引擎在浏览器里，所以由页面自己来。热一遍要
   * 一次真编译（WASM + Worker + 字体都在那一下里初始化），所以拿一段**最小的 TikZ**
   * 去喂它：出来的东西直接丢掉，只为让 `batch()` 建好那份**一直留着的工位文档**。
   * 之后正文里的图走的就是热路径，省掉那 ~700ms 的初始化。
   *
   * 两条克制：
   * * **计费 / 龟速网络不预下**（那几个 MB 的 WASM 不该在人没要的时候就花掉他的流量）——
   *   真要用的那一刻照样能编，只是要等；
   * * 空闲才做（`requestIdleCallback`），页面正忙就先紧着页面。
   */
  var MINIMAL_TEX = '\\begin{tikzpicture}\\draw (0,0) -- (0.02,0);\\end{tikzpicture}';
  var prewarmed = false;

  function prewarm() {
    if (prewarmed) return;
    prewarmed = true;
    var conn = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
    if (conn && (conn.saveData || /(^|-)2g$/.test(conn.effectiveType || ''))) return;
    ensureFonts(); // 顺手把字体表挂上（正文那边迟早要，而且是同一次下载）
    // 记时间是为了**能量出来**：上一轮我只能证明"机制在跑"，说不出省了多少
    // （DOM 上探不到预热那一次什么时候完工 —— 见 chat.js 里 Python 那边同理）。
    var t0 = window.performance.now();
    var done = function () {
      /* 成功的产物我们不要；失败也不用管（真要用的那一刻会自己再编） */
    };
    render(MINIMAL_TEX).then(
      function () {
        try {
          window.console.info(
            '[qf] LaTeX 引擎已备好：' + Math.round(window.performance.now() - t0) + 'ms —— 第一张图不必再等初始化'
          );
        } catch (err) {
          /* 打不出来不影响任何事 */
        }
        done();
      },
      done
    );
  }


  /* 静默常备：页面空闲（或最多 3 秒后）先热一遍。这里只负责"什么时候"，
   * "要不要"由 `prewarm` 自己判（计费网络、重复调用都在它那儿挡掉）。 */
  if (typeof window.requestIdleCallback === 'function') {
    window.requestIdleCallback(prewarm, { timeout: 3000 });
  } else {
    // Safari 还没有 requestIdleCallback：晚一点直接做，别和首屏抢
    window.setTimeout(prewarm, 1500);
  }

  QF.latex = { kind: kind, render: render, slot: slot, fill: fill, prewarm: prewarm };
})();

