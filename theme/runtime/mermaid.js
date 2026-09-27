/**
 * mermaid 图：**"方框 + 箭头"那一类，别送 TeX 引擎**。
 *
 * ## 为什么要有这条路
 *
 * 从前正文里的图只有一条路：写 `$$…$$`，交给离线 TikZJax 编。那是**真 TeX** ——
 * 一张图的固定开销 2.2 秒起，还常因为一个装饰性写法编不过（`phantom`、
 * `very near start` 这类最容易中招）。可模型画的图里有一多半是"六个方框一排、
 * 箭头连起来""宿主发给沙箱、沙箱回执"这种**流程/结构图**，那是 mermaid 的活：
 * 实测两张（`graph TD` + `sequenceDiagram`）**一共 65 毫秒**，而且几乎不挑写法。
 *
 * 用户原话（2026-09-27）：
 *
 * > 现在沙箱什么图都给 tex 引擎画，慢的要死。给它加一个 mermaid 图，
 * > 没必要用 tex 的图走 mermaid。
 *
 * ## 谁走哪条（分流写在这里，不写在调用方）
 *
 * * 正文里 ```mermaid 围栏 → 走这里（`md.js` 抽围栏时直接问 `QF.mermaid.slot`）；
 * * `$$…$$` 里是 `\begin{tikzpicture}` / `\draw` 这类**真在画笔**的 → 走 `QF.latex`。
 *
 * TeX 那条**一行没改**：真数学图（坐标轴、几何作图、电路、量子线路）还得是它。
 * mermaid 这条只是**多一条更快、更简单的路**，不是替代。
 *
 * ## 引擎是懒加载的，取不到就坦白
 *
 * 单文件 3.3MB。一页里多数消息根本没有图，所以**第一次真要画时才去取**
 *（`/assets/mermaid/mermaid.min.js`，本机，毫秒级）。取不到（没跑过
 * `make vendor`）就**把源码原样摆出来**并说明原因 —— 不假装画好了、也不留个
 * 转不完的圈：那种"看着像坏了"的观感，用户已经为别的事骂过一次。
 */
(function () {
  'use strict';

  var QF = (window.QF = window.QF || {});

  /** 引擎入口（构建时由 `vendor/mermaid/` 拷进 `/assets/mermaid/`）。 */
  var ENGINE = '__ORIGIN__/assets/mermaid/mermaid.min.js';

  /** 已经在取的引擎（只取一次；失败也记着，别反复重试）。 */
  var loading = null;
  /** 引擎装好了没有。 */
  var ready = false;
  /** 画过的图（源码 → SVG）。同一段源码反复渲染是常事（流式重渲染、翻历史），
   *  命中就不必再进一次引擎 —— 也顺手消掉了重渲染时的闪动。 */
  var cache = {};
  var cacheOrder = [];
  var CACHE_MAX = 60;
  // **不再用"JS 侧一个数组 + 占位只记编号"那套**（`QF.latex` 用的是它，
  // 但它在流式重渲染下会卡死，见 `slot` 的注释）：源码随节点走。
  var seq = 0;

  function origin() {
    try {
      return window.location.origin || '';
    } catch (err) {
      return '';
    }
  }

  function cssVar(name, fallback) {
    try {
      var got = getComputedStyle(document.documentElement).getPropertyValue(name);
      got = String(got || '').trim();
      return got || fallback;
    } catch (err) {
      return fallback;
    }
  }

  function esc(text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  /** 取引擎（幂等）。返回 Promise。 */
  function ensure() {
    if (ready) return Promise.resolve(true);
    if (loading) return loading;
    loading = new Promise(function (resolve, reject) {
      var tag = document.createElement('script');
      tag.src = ENGINE.replace('__ORIGIN__', origin());
      tag.async = true;
      tag.onload = function () {
        if (!window.mermaid) {
          reject(new Error('引擎取回来了，但页面上没有 mermaid 对象'));
          return;
        }
        try {
          window.mermaid.initialize({
            startOnLoad: false,
            // **不执行标签里的 HTML**：模型写的东西不该有脚本能力
            securityLevel: 'strict',
            // 主题色跟着页面走（**别用 mermaid 自带的暗色**：它不认我们的 token，
            // 白底主题下会画出一块自说自话的黑图）
            theme: 'base',
            fontFamily: cssVar('--font-sans', 'system-ui, sans-serif'),
            themeVariables: {
              background: cssVar('--bg', '#141414'),
              primaryColor: cssVar('--bg2', '#1c1c1c'),
              primaryTextColor: cssVar('--fg', '#e8e8e8'),
              primaryBorderColor: cssVar('--line', '#3a3a3a'),
              secondaryColor: cssVar('--bg2', '#1c1c1c'),
              tertiaryColor: cssVar('--bg', '#141414'),
              lineColor: cssVar('--fg3', '#8a8a8a'),
              textColor: cssVar('--fg', '#e8e8e8'),
              fontSize: '13px',
            },
          });
        } catch (err) {
          // **配色出错不该让整条路死掉**：填错一个键就会让 `initialize` 抛异常
          //（沙箱那条实测踩过：`colors.bg2` 不存在 → `undefined` → mermaid 当场抛
          // `reading 'h'`，图一张都画不出来）。退回 mermaid 自己的主题再试一次。
          try {
            window.mermaid.initialize({ startOnLoad: false, securityLevel: 'strict' });
          } catch (again) {
            reject(err);
            return;
          }
        }
        ready = true;
        resolve(true);
      };
      tag.onerror = function () {
        reject(new Error('取不到 mermaid 引擎（' + tag.src + '）'));
      };
      document.head.appendChild(tag);
    });
    return loading;
  }

  /** 引擎不认这段源码时，它可能已经往页面里插了个残余节点 —— 清掉，别留垃圾。 */
  function tidy(id) {
    ['d' + id, id].forEach(function (one) {
      var node = document.getElementById(one);
      if (node && node.parentNode) node.parentNode.removeChild(node);
    });
  }

  function render(src) {
    var key = String(src);
    if (Object.prototype.hasOwnProperty.call(cache, key)) {
      return Promise.resolve(cache[key]);
    }
    return ensure().then(function () {
      var id = 'qfmm' + ++seq;
      return window.mermaid.render(id, key).then(
        function (out) {
          var svg = (out && out.svg) || '';
          if (out && typeof out.bindFunctions === 'function') {
            // mermaid 的交互（点击折叠之类）要绑一次 —— 我们没用到，但绑上不亏
            try {
              out.bindFunctions(document);
            } catch (err) {
              /* 绑不上不影响图 */
            }
          }
          cache[key] = svg;
          cacheOrder.push(key);
          if (cacheOrder.length > CACHE_MAX) delete cache[cacheOrder.shift()];
          return svg;
        },
        function (err) {
          tidy(id);
          throw err;
        }
      );
    });
  }

  /**
   * 还没画的图：**源码随节点走**（`data-merfig` 里，百分号编码）。
   *
   * 原先是照 `QF.latex` 那套抄的：JS 侧一个数组、占位只记编号。那套在
   * **流式重渲染**下会把人卡死 —— 正文每来一段就整块重渲染一次，占位节点被换成
   * 新对象，而异步结果还往那个**已经不在页面上**的旧节点里写；新节点上那个编号
   * 早被"消费"掉了，于是永远停在"正在画图…"。用户的原话（2026-09-27）：
   *
   * > （卡了半天出不来。理想状态是秒出）
   *
   * 源码放在 DOM 里就没有这个问题：谁渲染出来的节点都带着自己那段源码，
   * 谁来 `fill` 都能画 —— 缓存里的、重渲染的、回放的，一视同仁。
   *
   * 编码用 `encodeURIComponent`：输出只有 `A-Za-z0-9-_.!~*'()` 与 `%`，
   * 放进双引号属性里**不必再转义**，也不会像 LaTeX 那样被引号括号写崩。
   */
  function slot(src) {
    var raw = String(src == null ? '' : src);
    // **立刻开始取引擎**：这一段正文里既然有图，那 3.3MB 现在就该在路上 ——
    // 别等 `fill` 那一步再排队（那是"点了才去买面粉"）。
    ensure().catch(function () {
      /* 取不到也不在这里报：`fill` 会把原因写在图上 */
    });
    return (
      '<div class="merfig is-loading" data-merfig="' +
      encodeURIComponent(raw) +
      '">正在画图…</div>'
    );
  }

  /** 画失败了就把**源码摆出来**：让人（和模型）看见写的是什么，而不是一句"失败了"。 */
  function showSource(place, src, why) {
    place.className = 'merfig is-failed';
    place.innerHTML =
      '<div class="merfig__why">' +
      esc(why || '这张 mermaid 图没画出来') +
      '</div><pre class="merfig__src"><code>' +
      esc(src) +
      '</code></pre>';
  }

  /** 把 `root` 里还没画的占位换成真图（异步）。与 `QF.latex.fill` 同时被调用（见 md.js）。 */
  function fill(root) {
    if (!root || !root.querySelectorAll) return;
    var places = root.querySelectorAll('.merfig[data-merfig]:not(.is-drawn)');
    for (var i = 0; i < places.length; i++) {
      (function (place) {
        var raw = place.getAttribute('data-merfig') || '';
        if (!raw) return;
        var src = raw;
        try {
          src = decodeURIComponent(raw);
        } catch (err) {
          /* 属性被谁改坏了就按原样画一次（让引擎自己报错，比静默不动强） */
        }
        render(src).then(
          function (svg) {
            // **属性留着**，只在画好时打上 `is-drawn`：流式重渲染会造出新的占位节点，
            // 它带着同一份源码 —— 下一轮 `fill` 直接就画（走上面的源码缓存，很快）。
            // 从前这里把属性删了，新节点上那个编号就成了没人认领的死号，
            // 于是永远停在"正在画图…"（2026-09-27 用户报障）。
            place.className = 'merfig is-drawn';
            place.innerHTML = svg;
          },
          function (err) {
            showSource(place, src, (err && err.message) || '');
          }
        );
      })(places[i]);
    }
  }

  QF.mermaid = { render: render, slot: slot, fill: fill, ensure: ensure };
})();
