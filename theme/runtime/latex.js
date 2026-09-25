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
  /**
   * 一批最多等多久。
   *
   * 从 20 秒放宽到 90 秒（实测）：合法的重图确实会慢 —— 对数轴 + `samples=400` 的幅频图
   * 在 20 秒里出不来，而被掐掉之后用户看到的却是"编不出来"。真正会**卡死**的那些
   * （图里有中文、`shader=interp` 之类）现在都在 `render` 入口被拦下并直接说清原因，
   * 所以放宽是安全的：卡死的图不会因此多等，只是"慢但能出来"的图有机会出来。
   */
  var RENDER_TIMEOUT = 90000;

  function origin() {
    return window.location.origin || '';
  }

  /**
   * 这段数学交给**引擎**（TikZJax）吗？—— 只认"内容里真在画笔"的那种。
   *
   * 从前这里只问一句"有没有 `\begin{…}`"，于是**矩阵也被当成图**：模型写
   * `$$\begin{pmatrix}…\end{pmatrix}$$`，这一句就送去引擎编译 —— 用户看到的是
   * 五张"矩阵图"各等两秒多（实测：一张图的固定开销 2.2 秒起），而 **KaTeX 排同样的矩阵
   * 是瞬间的**。用户原话："你不能输出矩阵吗？…… （有的东西不要走 latex！！！）"
   *
   * 判据换成"有没有画图的东西"：
   *   * 画图环境：`tikzpicture` / pgfplots 的各种 `axis` / `tikzcd` / `circuitikz` / `scope` …
   *   * 画图命令：`\draw` `\node` `\path` `\fill…` `\addplot…` `\matrix` `\tikz…`
   * 其余（`pmatrix`/`bmatrix`/`vmatrix`/`cases`/`aligned`/`array` …）一律走 KaTeX：
   * 立刻出结果，也不占引擎。
   *
   * 两侧白名单是**反着列**的：KaTeX 认的东西有限、能逐个查（它也只在认不出时才退化成
   * 源码），所以那一侧不必列；而画图这一侧**宁可多列** —— 漏掉一个环境名，图就会被送去
   * KaTeX 变成一坨源码，那比慢更糟。
   */
  var DRAW_ENV = /\\begin\s*\{\s*(tikzpicture|axis|semilogyaxis|loglogaxis|polaraxis|smithchart|ternaryaxis|groupplot|tikzcd|circuitikz|scope|pgfonlayer|quantikz|pspicture)\*?\s*[\[}]/;
  var DRAW_CMD = /\\(draw|filldraw|fill|shade|shadedraw|clip|node|path|addplot3|addplot|tikz|tikzset|usetikzlibrary|matrix)\b/;
  function kind(tex) {
    var src = String(tex == null ? '' : tex);
    return DRAW_ENV.test(src) || DRAW_CMD.test(src) ? 'tex' : '';
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

  /**
   * 把工位文档**丢掉**（下一次 `ensureFrame()` 会重建一份干净的）。
   *
   * 为什么必须有这一手：工位是**复用**的，而等结果用的全是 `doc.contentWindow.setTimeout`。
   * 一旦里面那次 TeX 编译卡住（比如宏包缺失/版本不匹配时进了一个不收敛的循环），
   * **它自己文档里的定时器就再也不会触发** —— 于是"超时判定"永远到不了，占位永远停在
   * "正在用 TeX 编译这张图…"，而且因为工位复用，**后面每一张图都跟着卡**。
   * 用户的原话："tex编译一直出不来"。
   */
  function dropFrame() {
    if (!frame) return;
    try {
      frame.remove();
    } catch (err) {
      /* 摘不掉也不该影响别的 */
    }
    frame = null;
  }

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

    var handled = false;   // 已经有人接手（settleIn 进过）
    var finished = false;  // 真出了结果、或已判失败
    // **父窗口**的兜底闹钟：工位自己的定时器靠不住（见 dropFrame 的说明）。
    // 到点还没结果 → 判这一批失败 + 丢掉工位，让下一批从干净的一页重来。
    var watchdog = window.setTimeout(function () {
      if (finished) return;
      finished = true;
      dropFrame();
      items.forEach(function (one) {
        /* 走到这里说明**引擎什么都没回**（既没出图、也没留 `tikzjax-broken`）——
         * 常见成因是 TeX 停在报错提示上等人按键，而它的报错**打在 worker 的 console 里**，
         * 应用这一侧听不到（钩的是 iframe 的 console）。所以别只说"卡住了、简化一点"，
         * 把**按源码能判出来的**原因一并说给他（`whyFailed` 认那几种已知写法）。 */
        var guess = whyFailed(one.tex);
        one.fail(
          new Error(
            '编译超过 ' +
              Math.round((RENDER_TIMEOUT + 4000) / 1000) +
              ' 秒还没有结果 —— 引擎卡住了（多半是某个宏包或某句写法在这台上走不通）。' +
              '工位已经重建，让他改一版再试。' +
              (guess ? '可能的成因：' + guess : '')
          )
        );
      });
    }, RENDER_TIMEOUT + 4000);

    var settleIn = function (root, removeAfter) {
      if (handled) return;
      handled = true;
      var started = Date.now();
      var settle = function () {
        var found = [];
        var done = true;
        var broken = 0;
        items.forEach(function (one, index) {
          var holder = root.querySelector('.one[data-i="' + index + '"]');
          var svg = holder && holder.querySelector('svg');
          var cls = svg ? svg.getAttribute('class') || '' : '';
          // 转圈框与"坏图"标记**也是 svg**，别当成图（这个坑踩过两回）
          if (svg && !/tikzjax-loader|tikzjax-broken/.test(cls)) found.push(adopt(svg));
          else {
            found.push(null);
            done = false;
            if (svg && /tikzjax-broken/.test(cls)) broken += 1; // 引擎已经把这张判死了
          }
        });
        /* **引擎说"编不出来"时就别再等了。**
         *
         * 它判死的那张会渲染成一个 `tikzjax-broken` 的 svg，而这里的循环只认"出图" ——
         * 于是明明结论已经到手，还要一路等到 90 秒的看门狗，最后报一句
         * "编译超过 94 秒还没有结果 —— 引擎卡住了（多半是某个宏包在这台上跑不动）"。
         * 实测（用户："三张 TikZ 图…引擎的bug"）：那三张在实验室里**几秒内就明确报错退出**，
         * 而应用里报的是"卡住 94 秒" —— 我照着那句去追"谁卡住了"，白追了一轮。
         * 现在：**这一批全都判死**就直接落到下面那段结账，把真原因（`lastError`）说出来。 */
        /* **TeX 已经停死了** —— 那就别再等。
         *
         * 缺文件（引擎 fetch 到 404、把空内容当正文读）时，TeX 会停在报错提示上等人按键：
         * 一个 svg 都不出，日志里留两句铁证 —— `! Emergency stop.` 与
         * `End of file on the terminal!`（实测：pgfplots 缺 `tikzlibrarypgfplots.
         * surfshading.code.tex` 时就是这个长相）。这种情形只等看门狗的话，用户要等 94 秒
         * 才看到一句"引擎卡住了"，而真正的原因（`! …` 那一行）早就躺在 `lastError` 里了。
         * 这两句只在 TeX 真的停下来时出现，所以拿它结账是安全的。 */
        var stopped = /End of file on the terminal|!\s*Emergency stop/.test(lastError);
        if (!done && !stopped && broken < items.length && Date.now() - started < RENDER_TIMEOUT) {
          doc.contentWindow.setTimeout(settle, 200);
          return;
        }
        finished = true;
        window.clearTimeout(watchdog);
        if (removeAfter && removeAfter.parentNode) removeAfter.parentNode.removeChild(removeAfter);
        items.forEach(function (one, index) {
          if (found[index]) {
            one.ok(found[index]);
          } else {
            // 编不出来：把源码原样给他看，比一块空白强（他会知道是哪段没吃下去）。
            //
            // 原因要**挑有用的那一段**：`lastError` 里装的是引擎抛的
            // "TikZJax: TeX did not produce input.dvi."，**后面跟着整份 TeX 日志** ——
            // 从前从**头**截 220 字，截到的正是那句没信息量的开头（"This is e-TeX, Version …"），
            // 真正说明问题的 `! Package PGF Math Error: Unknown function 'of'` 那行被截掉了。
            // 所以先在整段里找 `!` 开头的那一行（TeX 的报错行，实测就在日志里）。
            var why = String(lastError || '')
              .replace(/\s+/g, ' ')
              .trim();
            var mark = why.match(/!\s[^!]{4,180}/);
            if (mark) why = mark[0].trim();
            one.fail(
              new Error(
                '引擎编不出这段 LaTeX：' +
                  whyFailed(one.tex) +
                  (why ? '：' + why.slice(0, 200) : '（引擎没报原因）')
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

  /**
   * 编不出来时，把"为什么"说得具体一点。
   *
   * 实测（draft/dfgh.md 里 7 张图编不出来，逐个丢进引擎跑）：这台引擎**没有 pgfplots**
   * （连最简的 `\begin{axis}\addplot {x};` 都编不出来），也没有 tikz-feynhand；
   * 而提示词从前明写着它们在 —— 模型照着写，用户那边就是"一堆图出不来"。
   * 提示词已改成实话，这里再把"缺什么、该改用什么"当场说给他听。
   */
  /**
   * 含中文就**自己包进 CJK 环境**。
   *
   * 为什么引擎侧不做这件事：`CJK` 宏包是**环境级**的 —— 前言里那句 `\usepackage{CJK}` 只是把
   * 宏包装上，真正把 UTF-8 汉字映到 `gbsnu` 那些字形上的，是正文里的
   * `\begin{CJK}{UTF8}{gbsn}…\end{CJK}`。而模型写图只会写 `\begin{tikzpicture}…` ——
   * 凭什么要求它记得外面还得套一层？于是汉字裸在 picture 里 → `inputenc` 报错 →
   * **TeX 停在报错提示上等人按键**（既不出 dvi 也不返回）→ 用户那边就是"这张图编不出来"，
   * 而且一卡就是 94 秒超时。
   *
   * 实测（用户："三张 TikZ 图，引擎的 bug，修一下"）：那三张图都含中文、都没包 CJK 环境；
   * 我拿同一份内容在实验室里**只多加这一层**，第二张立刻照常出图。所以补在这里。
   *
   * 包在最外层是安全的（整段 tikzpicture 放进 CJK 环境不影响 TikZ 自己的排版，
   * 逐个验过）；已经自己包了的就不再重复包。
   */
  var CJK_CHAR = /[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]/;

  function withCJK(tex) {
    var text = String(tex || '');
    if (!CJK_CHAR.test(text)) return text;
    if (/\\begin\{CJK\*?\}/.test(text)) return text;
    return '\\begin{CJK}{UTF8}{gbsn}\n' + text + '\n\\end{CJK}';
  }

  /**
   * 把引擎**不认的写法**就地改对（两种改法都在实验室里逐条验过），而不是让它整张失败。
   *
   * 两处都是"模型写得很自然、这台引擎却吃不下"的：
   *
   * 1. **`shader=interp`**（曲面按高度插值上色）。这台引擎的 pgf 驱动是 `pgfsys-ximera.def`，
   *    不支持函数式着色，实测报 `! Package pgfplots Error: … (shader=interp) is NOT available
   *    for the selected driver`，整张图直接没有。**摘掉它**之后 `surf` 用默认的面片着色，
   *    曲面照旧出来（少了渐变，形状与网格都在）。**代价说清楚**：图与"按高度上色"那句说明
   *    不再对应 —— 所以提示词里仍然明写"不许写 `shader=interp`"，这里只是兜底。
   *
   * 2. **`symbolic x coords={取指, 译码, …}`**（分类轴的键是中文）。那些键是 pgfplots 要
   *    **解析**的，而中文在这台引擎上是"活动字符"（CJK 靠它映字形；e-TeX 只有 8 位，绕不开），
   *    两者冲突，实测报 `! Extra \else. \pgf@plotstreampoint …`（报在 pgf 内核宏上）。
   *    **改法**：键换成 ASCII（`k1, k2, …`）、数据点里的键跟着换、中文挪进 `xticklabels`
   *    （那是**排版**出去的，不参与解析）—— 实测出图正常，刻度上就是原来的中文。
   *
   * 两处都**先看有没有必要**（没写 interp、坐标里没中文就原样返回），所以绝大多数图一个字符都不动。
   */
  function rewriteUnsupported(tex) {
    var src = String(tex == null ? '' : tex);

    // 1) shader=interp：摘掉（连它前面的逗号一起，免得留下 `[surf, , ]` 这种）
    if (/shader\s*=\s*interp/.test(src)) {
      src = src.replace(/\s*,?\s*shader\s*=\s*interp\b/g, '');
    }

    // 2) 中文的分类轴键
    var m = src.match(/symbolic\s+x\s+coords\s*=\s*\{([^}]*)\}/);
    if (m && CJK_CHAR.test(m[1])) {
      var labels = m[1].split(',').map(function (one) {
        return one.trim();
      });
      var keys = labels.map(function (_one, i) {
        return 'k' + (i + 1);
      });
      // 数据点里的键先换（此刻 `symbolic x coords` 里还是中文，改它不影响）
      labels.forEach(function (label, i) {
        if (!label) return;
        var esc = label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        src = src.replace(new RegExp('\\(\\s*' + esc + '\\s*(?=[,)])', 'g'), '(' + keys[i]);
      });
      // 再换声明本身，并把中文交给 xticklabels（模型自己写过就不重复写）
      var hasLabels = /xticklabels\s*=/.test(src);
      src = src.replace(
        /symbolic\s+x\s+coords\s*=\s*\{[^}]*\}(\s*,)?/,
        'symbolic x coords={' + keys.join(', ') + '},' +
          (hasLabels ? '' : '\n  xticklabels={' + labels.join(', ') + '},')
      );
    }
    return src;
  }

  /**
   * 摘掉模型误写在图里的**前言命令**（`\usepackage` / `\documentclass` / `\begin{document}` …）。
   *
   * 提示词里明说过"不必也不能自己写 `\usepackage`"，但它偶尔还是写。后果很硬：
   * TeX 报 `! LaTeX Error: Can be used only in preamble.` —— **整张图**没了
   * （实验室实测确认：一张本该正常的极小图，前面多一行 `\usepackage{amsmath}` 就编不出来）。
   *
   * 而这些句子十有八九是"它以为要装的包"，前言里其实早装好了（amsmath、amssymb、bm、
   * mathtools、pgfplots、tikz-cd、circuitikz、CJK …）。所以**替它摘掉**比让它整张失败好：
   * 真要是前言里没有的包，摘掉之后会以"未定义的控制序列"报出来 —— 那才是准确的信号，
   * 而且能看出来缺的是哪个命令。
   */
  function stripPreamble(tex) {
    return String(tex == null ? '' : tex)
      .replace(/\\documentclass(\[[^\]]*\])?\{[^}]*\}/g, '')
      .replace(/\\usepackage(\[[^\]]*\])?\{[^}]*\}/g, '')
      .replace(/\\(begin|end)\s*\{\s*document\s*\}/g, '');
  }

  /** 画一张（异步）。一批里的图合成一次编译。 */
  function render(tex) {
    // 中文原来在这里被**拦下**：那时候引擎没有中文字形，TeX 会停在交互提示上（CPU 0%、
    // 永远不出 dvi），所以入口直接拒掉、把原因说清。现在**引擎会排中文了** —— 宏包侧装了
    // CJK（前言里的 `\usepackage{CJK}` + `gbsnu` 全族 .tfm + 码点对照），字形侧由浏览器
    // 按 `fonts.css` 里那份 Arphic 宋体画（按需下载，1.33MB）。所以这道拦截撤掉，
    // 改成**替它把 CJK 环境套上**（见上面 `withCJK` 里那段实测）。
    //
    // 兜底仍然在：引擎自己有 15 秒的渲染时限，超时就出 `tikzjax-broken`，
    // 于是"排不出来"表现为**一句明确的失败**，不是无限卡住（实测如此）。
    tex = withCJK(rewriteUnsupported(stripPreamble(tex)));
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

  function whyFailed(tex) {
    var src = String(tex || "");
    /* **中文不能放进 `symbolic x coords`**（实测，2026-09-26）。
     *
     * CJK 是靠把高位字节的字符变成**活动字符**来映字形的（e-TeX 只有 8 位，绕不开），
     * 而 `symbolic x coords={取指, 译码, …}` 里的键是 pgfplots **要解析**的东西 ——
     * 活动字符会把它的 `\if…\else` 弄乱，报出来是
     * `! Extra \else. \pgf@plotstreampoint …`（报在 **pgf 内核宏**上，看着像引擎坏了）。
     *
     * 这是 TeX 8 位编码与 pgf 解析器的固有冲突，**修不了**；但能绕开，而且写法很自然：
     * 坐标用 ASCII 键，中文放到 `xticklabels`（那是**排版**出去的，不参与解析）。
     * 实验室实测：`symbolic x coords={A, B, C}` + `xticklabels={取指, 译码, 执行}` 正常出图。
     */
    var sym = src.match(/symbolic\s+x\s+coords\s*=\s*\{([^}]*)\}/);
    if (sym && /[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]/.test(sym[1])) {
      return (
        "`symbolic x coords` 里放了中文 —— 这些键是 pgfplots **要解析**的，而中文在这台引擎上" +
        "是「活动字符」（CJK 靠它映字形），两者冲突，报出来是 `! Extra \\else. \\pgf@plotstreampoint …`。" +
        "改法：坐标用 ASCII 键，中文交给 `xticklabels` —— " +
        "`symbolic x coords={A, B, C}` 加 `xticklabels={取指, 译码, 执行}`（实测这样能出图），" +
        "数据点也跟着写成 `(A,2)`。"
      );
    }
    if (/shader\s*=\s*interp/.test(src)) {
      return (
        "`shader=interp` 这台引擎不支持（它的 pgf 驱动是 pgfsys-ximera.def，实测报 " +
        "surface shading is NOT available for the selected driver）—— 去掉它，直接写 " +
        "`\\addplot3[surf] {…};`。"
      );
    }
    if (/\\begin\s*\{\s*axis\s*\}|pgfplots|addplot/.test(src)) {
      return (
        "这段 axis 图编不出来：多半是用了它没装的库或冷门选项" +
        "（`\\usepgfplotslibrary{…}` 只有常见那几个）—— 简化一下再试。"
      );
    }
    if (/feynhand/.test(src)) {
      return "这台引擎没有 tikz-feynhand：费曼图请用 TikZ 手画（`\\draw` + 顶点）。";
    }
    /* 这里的正则**别再写双反斜杠**了：`\\s` 在正则里是"一个反斜杠后跟字母 s"，
     * 不是"空白" —— 上一版这条就写成了 `/\\begin\\s*\\{…/`，于是**永远匹配不到**，
     * 用户那段 tikzcd 拉回图拿到的是最笼统的兜底文案（实测：模型与用户都看不出所以然）。 */
    if (/\\begin\s*\{\s*(tikzcd|CD)\s*\}/.test(src)) {
      return "这段 tikz-cd 用了它不支持的写法（`phantom`、`very near start` 这类装饰最容易中招）。";
    }
    return "多半是用了它不支持的宏包或装饰性选项 —— 把写法简化一下再试。";
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
  /* 再挂两处**更早**的时机：用户第一次碰页面（pointerdown / keydown）—— 那一刻首屏那些
   * 忙活已经过去，人却还要打字；以及聊天那边"发出消息"时直接调（见 `chat.js` 的
   * `onSendClick`）—— 模型的思考时间几秒起，正好够引擎把冷启动做完。
   * 三处都幂等（`prewarmed` 挡着），也都过 `prewarm` 里那道计费/2G 判断。 */
  ['pointerdown', 'keydown'].forEach(function (ev) {
    window.addEventListener(ev, prewarm, { once: true, passive: true });
  });

  if (typeof window.requestIdleCallback === 'function') {
    window.requestIdleCallback(prewarm, { timeout: 3000 });
  } else {
    // Safari 还没有 requestIdleCallback：晚一点直接做，别和首屏抢
    window.setTimeout(prewarm, 1500);
  }


  /* ---- 下面几处是被我误删后、从已提交版本原样取回的（只取回，不改动别的）---- */


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
  var CACHE_TAG = 'qf.latex.v3';

  var CACHE_MAX = 60; /* 最多存几张 */

  var CACHE_CHARS = 1500000; /* 或总字符上限，先到先弃 */

  /**
   * 键 = **两个独立哈希 + 长度**。
   *
   * 为什么不止一个哈希：撞了不是多编一次，而是**串图** —— 把别人的图摆到这段
   * LaTeX 下面（比不缓存更坏）。两个 32 位哈希各不相同（FNV-1a 与 djb2），
   * 再加上长度，实际碰撞概率可以当没有。
   */


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

  QF.latex = { kind: kind, render: render, slot: slot, fill: fill, prewarm: prewarm };
})();

