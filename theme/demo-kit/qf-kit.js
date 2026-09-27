/**
 * QFKit —— 演示沙箱的前端套件。
 *
 * ## 为什么要有它
 *
 * 演示页面原先是个**空白页**：模型每次都要从零发明布局、配色、坐标轴、
 * 动画循环。结果是"能跑但难看"——手画的刻度是歪的、图例是随手贴的、
 * 每个演示一套配色，还常常把动画写成 `setInterval` 改 DOM。
 * 这不是模型不会写，是**没人告诉它有哪些现成的东西**。
 *
 * 所以这里把四样交出来：**配色令牌**（与 app 同一套）、**布局组件**
 * （Card / Frame / Row / Legend / KV）、**动画钩子**（`useTicker`）、
 * **坐标轴**（d3 的 `scales` + `useAxis`）。
 *
 * ## 这一层刻意不做的事
 *
 * 不做通用图表库 —— 演示要的是"把机制画出来"，不是又一套 ECharts。
 * 给到坐标轴与色板这个粒度，剩下的路径（怎么画、画什么形状）留给模型，
 * 那正是它该发挥的地方。
 *
 * 依赖（都由沙箱页面按顺序先加载好）：React 18 / ReactDOM 18 / htm / d3 v7。
 * Tailwind 也在，但**可选**：下面这些 `.qf-*` 类不依赖它。
 */
window.QFKit = (function () {
  'use strict';

  var React = window.React;
  var ReactDOM = window.ReactDOM;
  var d3 = window.d3;
  var h = React.createElement;

  /**
   * 与 app **同一套色**，而且是**跟着主题走的**：演示页带着 `data-theme`，
   * 白底主题下这里取到的就是白底那一套（值在 `qf-kit.css` 里，两套都从
   * `theme/app.css` 抄来）。
   *
   * ## 为什么是取值器（getter）而不是一张常量表
   *
   * 演示里的颜色分两种用法：CSS 那份（`.qf-*` 组件）天然跟着主题；而**模型在 JS 里
   * 用的**这份是给 d3 画坐标轴、着色用的 —— 写成常量就会死死钉在深色那套上，
   * 于是切到白底主题时，页面白、图里的线和字还是浅色，糊成一片。
   * 所以这里每次读**当前的** `--qf-*`（`getComputedStyle` 拿的就是主题算完的值）。
   *
   * 拿不到（比如套件在别处被引用）就退回下面那份深色 fallback，不至于变成空色。
   */
  var TOKEN_FALLBACK = {
    bg: '#0b1016', // --bg
    panel: '#131a22', // --bg2
    panel2: '#1e2936', // --bg3
    line: '#38495a', // --line
    line2: '#4e6478', // --line-strong
    fg: '#e6edf3', // --fg
    fg2: '#9aa7b4', // --fg2
    dim: '#75838f', // --fg3
    pri: '#2dd4bf', // --pri
    pri2: '#14b8a6', // --pri-2
    pri3: '#5eead4', // --pri-3
    ok: '#34d399', // --ok
    warn: '#fbbf24', // --amber
    bad: '#f87171', // --bad
    blue: '#60a5fa', // --blue
  };

  /** 读当前主题下的一个 token（`pri` → `--qf-pri`）。 */
  function token(name) {
    var raw = '';
    try {
      raw = getComputedStyle(document.documentElement).getPropertyValue('--qf-' + name);
    } catch (err) {
      raw = '';
    }
    return (raw || '').trim() || TOKEN_FALLBACK[name] || '';
  }

  var colors = {};
  Object.keys(TOKEN_FALLBACK).forEach(function (key) {
    Object.defineProperty(colors, key, {
      enumerable: true,
      get: function () {
        return token(key);
      },
    });
  });

  // 多条序列就用它，别自己挑色。**本站在用的 accent 色只有这几个**，所以给六条
  // （再多就得靠亮度差拉，而不是新造色号 —— 页面上多出一个不知从哪来的紫，
  // 整页就不像这个应用了）。顺序是按"分得最开"排的：青 → 蓝 → 黄 → 红 → 深青 → 绿。
  // 白底上要用手写文字/细线那类强调色，用 `colors.pri3`（`pri` 是亮青，衬白底不够）。
  Object.defineProperty(colors, 'series', {
    enumerable: true,
    get: function () {
      return [colors.pri, colors.blue, colors.warn, colors.bad, colors.pri3, colors.ok];
    },
  });

  /* ------------------------------------------------------------ 挂载 */

  /** 一行挂载：`QFKit.mount(<App/>)`。没给节点就找 `#app`，再退回 body。 */
  function mount(element, node) {
    var host = node || document.getElementById('app') || document.body;
    // 渲染完才量得到颜色；模型多半还会用状态改画面，所以隔几拍再看两眼。
    var guard = function () {
      [80, 500, 1500].forEach(function (ms) {
        window.setTimeout(function () {
          guardContrast(host);
        }, ms);
      });
    };
    if (!ReactDOM.createRoot) {
      ReactDOM.render(element, host); // 万一用的是 17 那份 React
      guard();
      return host;
    }
    ReactDOM.createRoot(host).render(element);
    guard();
    return host;
  }

  /* ------------------------------------------------------------ 动画 */

  /**
   * 帧驱动的步进量。用法：`const t = QFKit.useTicker()` —— 默认**每秒 6 步**。
   *
   * 为什么不用 `setInterval`：它和渲染不同步，慢机器上会漂、快机器上会堆。
   * 这个钩子用 `requestAnimationFrame`，并且把"暂停"做成参数：
   * `useTicker(1, { paused })` —— 播放/暂停按钮直接传 state 就行。
   *
   * ## 口径改过一次（2026-09-25）：从"每帧 +1"改成"每秒几步"
   *
   * 从前是每帧 +1（≈ 每秒 60 步），骨架里写着 `useTicker(1)`，模型照抄 ——
   * 于是**所有演示都闪得看不清**（用户："沙箱内部时间流速似乎有点过快，
   * agent 做的动图闪得飞快"）。60 步/秒是逐帧动画的节奏，不是"讲一个机制"的节奏：
   * 冒泡排序 20 个数，1/3 秒就演完了，眼睛跟不上。
   *
   * 现在 `speed` 是**倍率**，基准 `STEPS_PER_SECOND = 6`（一步 ≈ 167ms）：
   * 这一步大约是一句话读完的时间，能看清每一拍又不至于等。
   * 要快就给 2–3，要慢给 0.5。**别拿它当性能旋钮** ——
   * 一步快过 100ms（`speed = 10`）就回到"糊成一片"了，那正是这次要修的毛病。
   */
  var STEPS_PER_SECOND = 6;

  function useTicker(speed, options) {
    var paused = !!(options && options.paused);
    var rate = typeof speed === 'number' ? speed : 1;
    var state = React.useState(0);
    var tick = state[0];
    var setTick = state[1];

    React.useEffect(
      function () {
        if (paused) return undefined;
        var raf = 0;
        var last = performance.now();
        var loop = function (now) {
          var dt = Math.min(64, now - last); // 切走标签页再回来时别一次跳很远
          last = now;
          setTick(function (value) {
            // 每秒 `STEPS_PER_SECOND * rate` 步（`dt` 已经是毫秒）
            return value + (dt / 1000) * STEPS_PER_SECOND * rate;
          });
          raf = requestAnimationFrame(loop);
        };
        raf = requestAnimationFrame(loop);
        return function () {
          cancelAnimationFrame(raf);
        };
      },
      [paused, rate]
    );
    return tick;
  }

  /* ------------------------------------------------------------ 坐标图 */

  /**
   * 坐标图的外壳：**边距、比例尺、坐标轴的位置一次给全**。
   *
   *     <QFKit.Plot width={640} height={300} xDomain={[0, 50]} yDomain={[0, 1]}
   *                 xLabel="时钟">
   *       {({ scales }) => <g>{bars}</g>}   // 坐标一律用 scales.x() / scales.y() 算
   *     </QFKit.Plot>
   *
   * 为什么要一层壳：`scales()` 一直有默认边距，但"轴与数据要落进边距里"得调用方
   * 自己记得 —— 漏一次 translate，y 轴就贴在画布左边缘、刻度被切掉半个。
   * 包成组件之后这件事不再靠记性（`useAxis` 现在也自己就位了，老写法同样受益）。
   */
  function Plot(props) {
    var spec = {
      width: props.width || 640,
      height: props.height || 300,
      xDomain: props.xDomain,
      yDomain: props.yDomain,
      margin: props.margin,
    };
    var s = scales(spec);
    var ref = React.useRef(null);
    useAxis(ref, {
      scales: s,
      xTicks: props.xTicks,
      yTicks: props.yTicks,
      xLabel: props.xLabel,
      yLabel: props.yLabel,
    });
    var inner = typeof props.children === 'function' ? props.children({ scales: s }) : props.children;
    return h(
      'svg',
      {
        className: 'qf-svg',
        viewBox: '0 0 ' + spec.width + ' ' + spec.height,
        width: '100%',
        role: 'img',
      },
      h('g', { ref: ref }),
      h('g', { transform: 'translate(' + s.margin.left + ',' + s.margin.top + ')' }, inner)
    );
  }

  /* ------------------------------------------------------------ 公式 */

  /**
   * 公式的 HTML。沙箱里同样装好了 KaTeX（与正文**同一份**，由服务端注入），
   * 因为模型偶尔要在演示里写数学 —— 从前只能拿 Unicode 硬拼。
   */
  function texHtml(source, block) {
    var src = String(source == null ? '' : source);
    if (window.katex) {
      try {
        return window.katex.renderToString(src, {
          displayMode: block !== false,
          throwOnError: false,
          errorColor: '#F87171',
          strict: 'ignore',
          trust: false,
        });
      } catch (err) {
        /* 落到下面的原样文本 */
      }
    }
    return (
      '<code>' +
      src.replace(/[&<>]/g, function (ch) {
        return ch === '&' ? '&amp;' : ch === '<' ? '&lt;' : '&gt;';
      }) +
      '</code>'
    );
  }

  /** 一段公式：`<QFKit.Math tex="x^2 + y^2" />`；`inline` 传 true 就随文字走。 */
  function MathBlock(props) {
    var block = !props.inline;
    return h('div', {
      className: 'qf-math' + (block ? '' : ' qf-math--inline'),
      dangerouslySetInnerHTML: { __html: texHtml(props.tex, block) },
    });
  }

  /* ------------------------------------------------------------ 步进 */

  /**
   * "一格一格"的步进：`const step = QFKit.useStepper(400)` —— 每 400ms 加一。
   *
   * `useTicker` 是**连续**的（插值、缓动这种真正逐帧的东西用它）；
   * 这个钩子是**离散**的：机制演示大多是"一拍一拍"的 —— 比较一次、交换一次、
   * 流水线走一格。用整数步去写，才不会出现"半拍"那种中间态。
   *
   * **每拍多少毫秒由你（模型）判断**：先数清这个演示一共演几拍，再让整段落在
   * 3–10 秒。20 个数冒泡 ≈ 200 拍 —— 400ms 一拍要 80 秒，太长了：那种就该
   * 一次跳好几拍、或把规模讲小，而**不是**把每拍压到 20ms（看不清就白画了）。
   * 拿不准给 400ms。
   *
   * 这里用了 `setInterval`（**不是** tool 描述里禁止的那种"setInterval 改 DOM"）：
   * 离散步进本来就该按时间走，改的是 React 状态、渲染交给 React。
   */
  function useStepper(stepMs, options) {
    var ms = typeof stepMs === 'number' && stepMs > 0 ? stepMs : 400;
    var paused = !!(options && options.paused);
    var state = React.useState(0);
    var step = state[0];
    var setStep = state[1];
    React.useEffect(
      function () {
        if (paused) return undefined;
        var timer = window.setInterval(function () {
          setStep(function (value) {
            return value + 1;
          });
        }, ms);
        return function () {
          window.clearInterval(timer);
        };
      },
      [paused, ms]
    );
    return step;
  }

  /* ------------------------------------------------------------ 兜底 */

  /**
   * 对比度兜底：**只在明显读不清时**动手。
   *
   * 为什么需要：模型常常自己写死颜色 —— 实测有一份演示写了 65 个裸色号、
   * `QFKit.colors` **一次没用**。那些色是照深底挑的，用户切到白底主题后，
   * 底变白了、字还是白的（用户："底是白的，但是 llm 画出来的东西里面，
   * 字也变白了"）。提示词说了不听，那就让它**坏不了**。
   *
   * 渲染完扫一遍：谁的"字 vs 它自己的底"对比度低于 `MIN_RATIO`，就把字色换成
   * 当前主题的字色。只改字色、只改读不清的那些 —— 深色卡片上的白字对比度够，
   * 一动不动（所以它不会把正常的配色改花）。
   */
  var MIN_RATIO = 2;

  function _rgb(text) {
    var m = /rgba?\(([^)]+)\)/.exec(String(text || ""));
    if (!m) return null;
    var parts = m[1].split(",").map(function (v) {
      return parseFloat(v);
    });
    if (parts.length < 3) return null;
    // 半透明的当"没有底色"，继续往上找
    if (parts.length > 3 && (isNaN(parts[3]) ? 1 : parts[3]) < 0.5) return null;
    if (parts.slice(0, 3).some(isNaN)) return null;
    return [parts[0], parts[1], parts[2]];
  }

  function _lum(rgb) {
    var f = rgb.map(function (v) {
      v = v / 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * f[0] + 0.7152 * f[1] + 0.0722 * f[2];
  }

  function _ratio(a, b) {
    var la = _lum(a) + 0.05;
    var lb = _lum(b) + 0.05;
    return la > lb ? la / lb : lb / la;
  }

  /** 往上找第一个真正的底色（透明的继续往上，最后退回主题底色）。 */
  function _backdrop(el) {
    var node = el;
    while (node && node.nodeType === 1) {
      var bg = _rgb(window.getComputedStyle(node).backgroundColor);
      if (bg) return bg;
      node = node.parentElement;
    }
    return _rgb(token('bg')) || [255, 255, 255];
  }

  function guardContrast(node) {
    var root = node || document.getElementById('app') || document.body;
    if (!root || !root.querySelectorAll) return 0;
    var fg = token('fg');
    var fixed = 0;
    var list = [root];
    var all = root.querySelectorAll('*');
    for (var i = 0; i < all.length; i++) list.push(all[i]);
    list.forEach(function (el) {
      // SVG 里的 <text> 没有文本子节点，但它的字是要看的
      var svgText = el.tagName === 'text' || el.tagName === 'tspan';
      var own = '';
      if (!svgText) {
        for (var k = 0; k < el.childNodes.length; k++) {
          if (el.childNodes[k].nodeType === 3) own += el.childNodes[k].nodeValue || '';
        }
        if (!own.trim()) return;
      }
      var cs = window.getComputedStyle(el);
      var cur = _rgb(cs.color);
      if (svgText) {
        var fill = _rgb(cs.fill);
        if (fill) cur = fill;
      }
      if (!cur || !fg) return;
      if (_ratio(cur, _backdrop(el)) >= MIN_RATIO) return;
      if (svgText) el.style.fill = fg;
      else el.style.color = fg;
      fixed++;
    });
    return fixed;
  }

  /* ------------------------------------------------------------ mermaid */

  /**
   * `QFKit.Mermaid` —— **流程图/框图/时序图/状态图这类"方框加箭头"，用它**。
   *
   * 为什么加它（2026-09-27 用户）：
   *
   * > 现在沙箱什么图都给 tex 引擎画，慢的要死。给它加一个 mermaid 图，
   * > 没必要用 tex 的图走 mermaid。
   *
   * 从前在演示里画一张"三步流水线"，要么自己摆 div/SVG（费劲且常常歪），
   * 要么在正文写 TikZ 让真引擎编（**每张 2.2 秒起**、还挑写法）。mermaid 是
   * **毫秒级**，而且源码就是图本身 —— 改一个词就换一张图。真数学图（坐标轴、
   * 几何、电路）才该回到 `$$…$$` 那条 TeX 路（那时用正文，不是沙箱）。
   *
   * 两种写法都认（与 `QFKit.Shader` 同一条规矩，理由也一样）：
   *
   *   ① JSX：`<QFKit.Mermaid code={"graph TD\n A[取数]-->B[算]"} />`
   *   ② 命令式：`QFKit.Mermaid(host.current, { code: 'graph TD\n A-->B' })`
   *      → 返回 `{ set(code), dispose(), stop(), code }`
   *
   * **引擎是懒加载的**（单文件 3.3MB）：第一次真要画时才去取一次。取不到就把
   * 源码原样摆出来并说明 —— 不假装画好了、也不留个转不完的圈。
   */
  var mermaidSeq = 0;
  var mermaidLoad = null;

  /**
   * 沙箱页面是 `srcdoc`，**相对路径解析不了** —— 引擎地址只能是绝对地址。
   * 优先问浏览器自己（`srcdoc` 会继承父页的 origin），拿不到时退回服务端注入的那句
   *（`_demo_page` 里 `window.__QF_ORIGIN__`，它跟着页面一起过 `__ORIGIN__` 替换）。
   */
  function assetOrigin() {
    try {
      var got = window.location && window.location.origin;
      if (got && got !== 'null') return got;
    } catch (err) {
      /* 读不到就走下面那句 */
    }
    return String(window.__QF_ORIGIN__ || '');
  }

  function mmEsc(text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  /** 取引擎（幂等，只取一次）。 */
  function mermaidEnsure() {
    if (window.mermaid) return Promise.resolve(window.mermaid);
    if (mermaidLoad) return mermaidLoad;
    mermaidLoad = new Promise(function (resolve, reject) {
      var tag = document.createElement('script');
      tag.src = assetOrigin() + '/assets/mermaid/mermaid.min.js';
      tag.async = true;
      tag.onload = function () {
        if (!window.mermaid) {
          reject(new Error('引擎取回来了，但页面上没有 mermaid 对象'));
          return;
        }
        // 与正文那条同一份规矩（见 theme/runtime/mermaid.js）：
        // 不执行标签里的 HTML；配色跟着当前主题走（别用 mermaid 自带的暗色）。
        //
        // **键名是 `colors` 那一套**（`bg` / `panel` / `panel2` / `line` / `fg` / `fg3→dim`，
        // 见上面的 `TOKEN_FALLBACK`），**不是 CSS 变量的名字**。这里写错过一次：填了
        // `colors.bg2`（CSS 里是 `--bg2` 没错，但 `colors` 上的键叫 `panel`）→
        // `primaryColor` 成了 `undefined` → mermaid 的 `initialize` **当场抛异常**
        //（`Cannot read properties of undefined (reading 'h')`），一张图都画不出来。
        var base = {
          startOnLoad: false,
          securityLevel: 'strict',
          theme: 'base',
          themeVariables: {
            background: colors.bg,
            primaryColor: colors.panel,
            primaryTextColor: colors.fg,
            primaryBorderColor: colors.line,
            secondaryColor: colors.panel2,
            tertiaryColor: colors.bg,
            lineColor: colors.dim,
            textColor: colors.fg,
            fontSize: '13px',
          },
        };
        try {
          window.mermaid.initialize(base);
        } catch (err) {
          // **配色出错不该让整条路死掉**：退回 mermaid 自己的主题再试一次
          //（图难看一点，但画得出来；这里宁可难看也不能"什么都没有"）。
          try {
            window.mermaid.initialize({ startOnLoad: false, securityLevel: 'strict' });
          } catch (again) {
            reject(err);
            return;
          }
        }
        resolve(window.mermaid);
      };
      tag.onerror = function () {
        reject(new Error('取不到 mermaid 引擎（' + tag.src + '）'));
      };
      document.head.appendChild(tag);
    });
    return mermaidLoad;
  }

  /** 画不出来时**把源码摆出来**：让人和模型都看见写的是什么。 */
  function mmShowSource(host, code, why) {
    host.className = 'qf-mermaid is-bad';
    host.innerHTML =
      '<div class="qf-mermaid__why">' +
      mmEsc(why || '这张 mermaid 图没画出来') +
      '</div><pre class="qf-mermaid__src">' +
      mmEsc(code) +
      '</pre>';
  }

  /** 把一段 mermaid 源码画进 `host`。失败不抛（当场显示原因），返回 `null`。 */
  function mermaidDraw(host, code) {
    var src = String(code == null ? '' : code);
    return mermaidEnsure().then(
      function (mm) {
        var id = 'qfmm-' + ++mermaidSeq;
        return mm.render(id, src).then(
          function (out) {
            host.className = 'qf-mermaid';
            host.innerHTML = (out && out.svg) || '';
            return out;
          },
          function (err) {
            // 引擎认不了时可能已经往页面里插了个残余节点 —— 清掉
            ['d' + id, id].forEach(function (one) {
              var node = document.getElementById(one);
              if (node && node.parentNode) node.parentNode.removeChild(node);
            });
            mmShowSource(host, src, (err && err.message) || '');
            return null;
          }
        );
      },
      function (err) {
        mmShowSource(host, src, (err && err.message) || '');
        return null;
      }
    );
  }

  /** JSX 那份：`<QFKit.Mermaid code={…} />`（`src` / `text` 也认作源码）。 */
  function MermaidView(props) {
    var opts = props || {};
    var code = String(opts.code || opts.src || opts.text || '');
    var ref = React.useRef(null);
    React.useEffect(
      function () {
        if (ref.current) mermaidDraw(ref.current, code);
        return undefined;
      },
      [code]
    );
    return h('div', { className: 'qf-mermaid', ref: ref });
  }

  /** 命令式那份：`QFKit.Mermaid(host, { code })` → 句柄（见上面那段说明）。 */
  function mermaidOn(host, options) {
    var opts = options || {};
    var handle = { host: host, code: String(opts.code || opts.src || opts.text || '') };
    handle.set = function (next) {
      handle.code = String(next == null ? '' : next);
      return mermaidDraw(host, handle.code);
    };
    handle.dispose = function () {
      if (host) host.innerHTML = '';
    };
    handle.stop = handle.dispose;
    mermaidDraw(host, handle.code);
    return handle;
  }

  /** `QFKit.Mermaid` —— **两种写法都认**（第一个参数是 DOM 节点就是命令式）。 */
  function Mermaid(a, b) {
    if (a && a.nodeType === 1) return mermaidOn(a, b);
    return MermaidView(a);
  }

  /* ------------------------------------------------------------ 组件 */

  /** 一块面板。演示里的每一组内容都该装进 Card，别裸着贴在页面上。 */
  function Card(props) {
    return h(
      'div',
      { className: 'qf-card' },
      props.title ? h('div', { className: 'qf-card__title' }, props.title) : null,
      props.children
    );
  }

  /**
   * 演示的外框：标题 + 一句"你在看什么" + 右侧控件位。
   *
   * 这句 caption 不是装饰 —— 没有它，用户看到一张会动的图也不知道该看哪。
   */
  function Frame(props) {
    return h(
      'div',
      { className: 'qf-frame' },
      h(
        'div',
        { className: 'qf-frame__head' },
        h('h1', { className: 'qf-frame__title' }, props.title || '演示'),
        props.right ? h('div', { className: 'qf-frame__right' }, props.right) : null
      ),
      props.caption ? h('div', { className: 'qf-frame__caption' }, props.caption) : null,
      props.children
    );
  }

  function Row(props) {
    return h('div', { className: 'qf-row', style: props.style }, props.children);
  }

  function Btn(props) {
    return h(
      'button',
      {
        type: 'button',
        className: 'qf-btn' + (props.on ? ' is-on' : ''),
        onClick: props.onClick,
        disabled: props.disabled,
      },
      props.children
    );
  }

  /** 图例。items: [{ label, color }] 或 { label, color, shape: 'line' | 'dot' | 'box' } */
  function Legend(props) {
    var items = props.items || [];
    return h(
      'div',
      { className: 'qf-legend' },
      items.map(function (item, index) {
        var shape = item.shape || 'box';
        return h(
          'div',
          { className: 'qf-legend__row', key: index },
          h('span', {
            className: 'qf-legend__sw is-' + shape,
            style: {
              background: shape === 'line' ? 'transparent' : item.color,
              borderColor: item.color,
              color: item.color,
            },
          }),
          h('span', null, item.label)
        );
      })
    );
  }

  /** 键值对（参数、计数、当前步……）。items: [[key, value], …] */
  function KV(props) {
    return h(
      'div',
      { className: 'qf-kv' },
      (props.items || []).map(function (pair, index) {
        return h(
          'div',
          { className: 'qf-kv__row', key: index },
          h('span', null, pair[0]),
          h('span', { className: 'qf-kv__val' }, pair[1])
        );
      })
    );
  }

  /** 小字注脚。放在演示底部，用来写"这个现象说明了什么"。 */
  function Note(props) {
    return h('div', { className: 'qf-note' }, props.children);
  }

  function Chip(props) {
    return h('span', { className: 'qf-chip', style: { color: props.color, borderColor: props.color } }, props.children);
  }

  /* ------------------------------------------------------------ 坐标轴 */

  /**
   * 造一组比例尺。**数据与坐标轴共用同一组**，这是"图对得上"的唯一保证 ——
   * 手写的坐标轴之所以歪，就是因为它和数据各算各的。
   *
   * spec: { width, height, xDomain, yDomain, margin }
   * 返回 { x, y, plot: { width, height } }，其中 x/y 是 d3 的比例尺。
   */
  function scales(spec) {
    var margin = spec.margin || { top: 12, right: 16, bottom: 30, left: 44 };
    var plot = {
      width: Math.max(1, spec.width - margin.left - margin.right),
      height: Math.max(1, spec.height - margin.top - margin.bottom),
    };
    var x = d3.scaleLinear().domain(spec.xDomain || [0, 1]).range([0, plot.width]);
    var y = d3.scaleLinear().domain(spec.yDomain || [0, 1]).range([plot.height, 0]);
    return { x: x, y: y, plot: plot, margin: margin };
  }

  /**
   * 把坐标轴画进一个 `<g ref>`（在**模型的 svg 里**，所以坐标与数据天然一致）。
   *
   *     const g = React.useRef(null);
   *     const s = QFKit.scales({ width: 640, height: 300, xDomain: [0, 10], yDomain: [-1, 1] });
   *     QFKit.useAxis(g, { scales: s, xLabel: 't', yLabel: 'v', xTicks: 5 });
   *     // 数据就用 s.x(...) / s.y(...) 算，别自己换算
   */
  function useAxis(ref, spec) {
    var deps = [
      spec && spec.scales ? spec.scales.x.domain().join() : '',
      spec && spec.scales ? spec.scales.y.domain().join() : '',
      spec && spec.xTicks,
      spec && spec.yTicks,
      ref,
    ];
    React.useEffect(
      function () {
        if (!ref.current || !d3) return undefined;
        var s = spec.scales;
        var g = d3.select(ref.current);
        g.selectAll('*').remove();
        // **轴按边距就位**：从前这一步要调用方自己写（在 `<g ref>` 外面再套一层
        // `translate(margin.left, margin.top)`），忘了就是"y 轴压在画布左边缘、
        // 刻度被切掉"（用户截图："这个坐标图好像没渲染正常——左边贴着了"）。
        // 收进来，调用方只要用 `s.x()` / `s.y()` 算坐标就对齐了。
        if (s.margin) {
          g.attr('transform', 'translate(' + (s.margin.left || 0) + ',' + (s.margin.top || 0) + ')');
        }
        var xAxis = d3
          .axisBottom(s.x)
          .ticks(spec.xTicks || 5)
          .tickSizeOuter(0);
        var yAxis = d3
          .axisLeft(s.y)
          .ticks(spec.yTicks || 5)
          .tickSizeOuter(0);

        g.append('g')
          .attr('transform', 'translate(0,' + s.plot.height + ')')
          .call(xAxis);
        g.append('g').call(yAxis);
        if (spec.xLabel) {
          g.append('text')
            .attr('class', 'qf-axis-label')
            .attr('x', s.plot.width)
            .attr('y', s.plot.height + 26)
            .attr('text-anchor', 'end')
            .text(spec.xLabel);
        }
        if (spec.yLabel) {
          g.append('text')
            .attr('class', 'qf-axis-label')
            .attr('x', 4)
            .attr('y', -2)
            .text(spec.yLabel);
        }
        // d3 默认画出来的文字是黑描边，深色底上必须先统一
        g.selectAll('text').attr('fill', colors.dim).attr('font-size', 11);
        g.selectAll('line,path').attr('stroke', colors.line2);
        return undefined;
      },
      deps
    );
  }

  /** 一段路径的辅助：把点序列转成 d3 的 line 生成器（含平滑选项）。 */
  function line(accessors) {
    var x = accessors.x;
    var y = accessors.y;
    var gen = d3
      .line()
      .x(function (d) {
        return x(d);
      })
      .y(function (d) {
        return y(d);
      });
    if (accessors.curve === 'smooth') gen.curve(d3.curveMonotoneX);
    return gen;
  }

  /* ------------------------------------------------------------ 着色器 */

  /**
   * 直接写 GLSL：`<QFKit.Shader frag={...} />`（或 `QFKit.Shader({ frag })`）。
   *
   * ## 为什么给的是 GLSL，而不是 three.js
   *
   * 演示要讲的是**机制本身** —— warp 怎么调度、tile 怎么流过、带宽怎么被吃满 ——
   * 而在 GPU 上算这件事的语言就是 GLSL。three.js 那一层（场景图 / 材质 / 相机）
   * 大多数演示用不上，却给模型留了一大片"摆得像那么回事"的地方。用户的话：
   * "threejs 我感觉有时候不好用（llm 会滥用），直接 glsl 编程更好"。
   * 所以这里给的是**最窄的一条路**：一块画布、一对着色器、四个约定好的 uniform。
   *
   * ## 约定（少一条就编不出来，所以写死在这里、也写进了工具说明）
   *
   * * **WebGL2**；片元着色器第一行必须是 `#version 300 es`。我们**不替你补**这一行
   *   （也不塞任何前言）—— 补了行号就全错位，报错里那句 `ERROR: 0:12:` 对不上它写的东西；
   * * 片元里要自己声明输出：`out vec4 fragColor;`（WebGL2 的硬要求）；
   * * **顶点着色器可以不写**：默认那个把画布铺满，给片元一个 `v_uv`（0..1）；
   * * 现成的 uniform：`u_time`（秒，暂停时不涨）、`u_resolution`（像素宽高）、
   *   `u_frame`（第几帧）。机制演示要的时间与尺寸都在这里，不必自己管时钟。
   *
   * ## 编不出来不白屏
   *
   * GLSL 的报错**原样打在画面上**（`ERROR: 0:12: …`，行号能用），并记进
   * `QFKit.shaderErrors()` —— 演示回传时它会跟着交给模型。这是这套沙箱的老规矩：
   * 编不出来就把引擎的原话给它、让它自己改对，而不是让人对着白屏猜。
   */
  var shaderSlots = [];
  //: 上一次 `snapshot()` 发现的"这张图可能是纯色/空白"这类说明（回执里要带上）
  var snapshotNotes = [];

  var DEFAULT_VERT = [
    '#version 300 es',
    'in vec2 a_pos;',
    'out vec2 v_uv;',
    'void main() {',
    '  v_uv = a_pos * 0.5 + 0.5;',
    '  gl_Position = vec4(a_pos, 0.0, 1.0);',
    '}',
  ].join('\n');

  //: GLSL ES 1.00 那一侧的默认顶点着色器。**版本必须跟片元的一致** ——
  //: 不一致时 GL 只甩一句 `Fragment shader version does not match other shader versions`，
  //: 而它**不说是哪边该改**。实测模型为此来回两轮、还先往错的方向改（把片元退回 1.00，
  //: 报错照旧 —— 因为要改的是另一半）。所以这里**自动配平**：片元写了 `#version 300 es`
  //: 就用 300 es 那份，没写（1.00 风格：`gl_FragColor`）就用 1.00 那份。不用谁去背。
  var DEFAULT_VERT_100 = [
    'attribute vec2 a_pos;',
    'varying vec2 v_uv;',
    'void main() {',
    '  v_uv = a_pos * 0.5 + 0.5;',
    '  gl_Position = vec4(a_pos, 0.0, 1.0);',
    '}',
  ].join('\n');

  function fragIs300(frag) {
    return /^[ \t]*#version[ \t]+300[ \t]+es/m.test(String(frag || ''));
  }

  function compileShader(gl, kind, source, label) {
    var sh = gl.createShader(kind);
    gl.shaderSource(sh, source);
    gl.compileShader(sh);
    if (gl.getShaderParameter(sh, gl.COMPILE_STATUS)) return { shader: sh };
    var log = String(gl.getShaderInfoLog(sh) || '').trim();
    gl.deleteShader(sh);
    return { error: label + '编不过：\n' + (log || '（引擎没给原因）') };
  }

  function linkProgram(gl, vs, fs) {
    var v = compileShader(gl, gl.VERTEX_SHADER, vs, '顶点着色器');
    if (v.error) return { error: v.error };
    var f = compileShader(gl, gl.FRAGMENT_SHADER, fs, '片元着色器');
    if (f.error) return { error: f.error };
    var prog = gl.createProgram();
    gl.attachShader(prog, v.shader);
    gl.attachShader(prog, f.shader);
    gl.bindAttribLocation(prog, 0, 'a_pos');
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      var log = String(gl.getProgramInfoLog(prog) || '').trim();
      return { error: '这一对着色器链不起来：\n' + (log || '（引擎没给原因）') };
    }
    return { program: prog };
  }

  /* 语义 → 名字的样子。这些写法**都被实测见过**：`u_t` / `u_time` / `uTime` / `iTime`、
   * `u_res` / `u_resolution` / `uResolution` / `uRes` / `iResolution`。
   * 之所以按语义认而不是列死名字：**名字没对上不会报错** —— 未赋值的 uniform 是 0，
   * 画面于是变成一整块纯色或干脆不动（实测：模型写 `u_res`，我们只认 `u_resolution`，
   * 它那行 `gl_FragCoord.xy / u_res` 直接除以 0，出来一整块纯蓝）。那是这类代码里
   * 最难查的一种"没报错但全错"。 */
  var TIME_RE = /^(u_?t|u_?time|time|i_?time)$/i;
  var RES_RE = /^(u_?res|u_?resolution|res|resolution|i_?resolution)$/i;
  var FRAME_RE = /^(u_?frame|frame|i_?frame)$/i;

  /**
   * 问 GL：这一对程序里**实际有哪些** uniform。按语义分好类返回。
   *
   *     { time: [loc…], res: [loc…], frame: [loc…], names: ['u_t', 'u_res'] }
   *
   * `names` 是回执里要报给模型的（"自动喂上了哪些"），它据此就知道该怎么命名。
   */
  function bindUniforms(gl, prog) {
    var out = { time: [], res: [], frame: [], names: [] };
    var total = gl.getProgramParameter(prog, gl.ACTIVE_UNIFORMS);
    for (var i = 0; i < total; i += 1) {
      var info = gl.getActiveUniform(prog, i);
      if (!info) continue;
      var name = String(info.name || '').replace(/\[0\]$/, '');
      var loc = gl.getUniformLocation(prog, name);
      if (!loc) continue;
      // 标量还是 vec2 得看声明：`uniform float u_t` 与 `uniform vec2 u_t` 都可能出现
      var pair = { loc: loc, vec2: info.type === gl.FLOAT_VEC2 };
      if (TIME_RE.test(name)) {
        out.time.push(pair);
        out.names.push(name);
      } else if (RES_RE.test(name)) {
        out.res.push(pair);
        out.names.push(name);
      } else if (FRAME_RE.test(name)) {
        out.frame.push(pair);
        out.names.push(name);
      }
    }
    return out;
  }

  function ShaderView(props) {
    props = props || {};
    var canvasRef = React.useRef(null);
    var errState = React.useState('');
    var error = errState[0];
    var setError = errState[1];
    var frag = String(props.frag || '');
    // 没给顶点着色器就**按片元的版本来配**（见 DEFAULT_VERT_100 的说明）
    var vert = props.vert ? String(props.vert) : fragIs300(frag) ? DEFAULT_VERT : DEFAULT_VERT_100;
    var paused = !!props.paused;
    var height = props.height ? String(props.height) : '';
    // 命令式那边的句柄（有的话）：把画布交出去，并接住它塞进来的自定义 uniform
    var handle = props.handle || null;

    React.useEffect(
      function () {
        var canvas = canvasRef.current;
        if (!canvas) return undefined;
        if (!frag) {
          // **缺源码要当场说清**（实测模型定义了 `frag` 却忘了传，于是静默什么都不画，
          // 回执只能说"没有用 QFKit.Shader"—— 那句话把它带偏了一整轮）。
          var miss =
            '没拿到片元着色器源码：组件写法要 `<QFKit.Shader frag={…} />`，' +
            '命令式则 `QFKit.Shader(节点, { frag })`。';
          var dead = { canvas: null, error: miss };
          shaderSlots.push(dead);
          setError(miss);
          if (typeof props.onError === 'function') props.onError(miss);
          return function () {
            shaderSlots = shaderSlots.filter(function (one) {
              return one !== dead;
            });
          };
        }
        var gl = canvas.getContext('webgl2', {
          antialias: true,
          alpha: false,
          // **必须开**：不开的话帧画完缓冲就被清，`toDataURL` 取回来是黑图 ——
          // 而"把画面回传给模型看"正是这条路的另一半（见 `snapshot`）。
          preserveDrawingBuffer: true,
        });
        // 先把这一格挂进账上（**失败也要挂**）：回传时 `shaderErrors()` 要能拿到原话。
        var slot = { canvas: canvas, error: '' };
        shaderSlots.push(slot);
        if (handle) {
          handle.canvas = canvas;
          handle.gl = gl;
        }
        if (!gl) {
          slot.error = '这个浏览器没有 WebGL2';
          setError('这个浏览器没有 WebGL2 —— 换个新一点的 Chrome / Edge 就能跑。');
          if (typeof props.onError === 'function') props.onError('这个浏览器没有 WebGL2');
          return function () {
            shaderSlots = shaderSlots.filter(function (one) {
              return one !== slot;
            });
          };
        }
        var built = linkProgram(gl, vert, frag);
        if (built.error) {
          slot.error = built.error;
          setError(built.error);
          if (typeof props.onError === 'function') props.onError(built.error);
          if (typeof props.onStatus === 'function') props.onStatus({ ok: false, error: built.error });
          return function () {
            shaderSlots = shaderSlots.filter(function (one) {
              return one !== slot;
            });
          };
        }
        setError('');
        // 模型常常自己带一个"告诉我编过没有"的回调（实测它写过 `onStatus`）——
        // 认它，否则它那张卡片上的"编译：…"会一直停在省略号上。
        if (typeof props.onStatus === 'function') props.onStatus({ ok: true });
        var prog = built.program;
        gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
        gl.bufferData(
          gl.ARRAY_BUFFER,
          new Float32Array([-1, -1, 3, -1, -1, 3]), // 一个盖住全屏的大三角
          gl.STATIC_DRAW
        );
        gl.enableVertexAttribArray(0);
        gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
        gl.useProgram(prog);
        var uniform = bindUniforms(gl, prog);
        // 记在账上：回执里要告诉模型"哪些 uniform 是自动喂的"（它据此就知道怎么命名）
        slot.uniforms = uniform.names;

        // 分辨率：CSS 尺寸 × dpr，**dpr 封顶 2** —— 4K 屏上按 3 倍铺会白烧一倍像素。
        var fit = function () {
          var dpr = Math.min(2, window.devicePixelRatio || 1);
          var w = Math.max(1, Math.round(canvas.clientWidth * dpr));
          var h = Math.max(1, Math.round((canvas.clientHeight || canvas.clientWidth * 0.625) * dpr));
          if (canvas.width !== w || canvas.height !== h) {
            canvas.width = w;
            canvas.height = h;
          }
        };
        fit();
        var observer = window.ResizeObserver ? new window.ResizeObserver(fit) : null;
        if (observer) observer.observe(canvas);

        var raf = 0;
        var frame = 0;
        var started = window.performance.now();
        var frozen = 0; // 暂停时冻结在哪个时刻
        var draw = function () {
          fit();
          gl.viewport(0, 0, canvas.width, canvas.height);
          var t = (frozen || window.performance.now() - started) / 1000;
          var one = 0;
          for (one = 0; one < uniform.time.length; one += 1) {
            gl.uniform1f(uniform.time[one].loc, t);
          }
          for (one = 0; one < uniform.res.length; one += 1) {
            if (uniform.res[one].vec2) {
              gl.uniform2f(uniform.res[one].loc, canvas.width, canvas.height);
            } else {
              gl.uniform1f(uniform.res[one].loc, canvas.width);
            }
          }
          for (one = 0; one < uniform.frame.length; one += 1) {
            gl.uniform1f(uniform.frame[one].loc, frame);
          }
          // 命令式那边塞进来的自定义 uniform（`setUniform('uSep', 0.3)`）——
          // **每帧照写一遍**（排在自动那几个之后，所以调用方想覆盖 uTime 也覆盖得掉），
          // 调用方因此不必知道我们什么时候画。
          if (handle && handle.custom) {
            Object.keys(handle.custom).forEach(function (name) {
              var loc = gl.getUniformLocation(prog, name);
              if (!loc) return;
              var value = handle.custom[name];
              if (typeof value === 'number') gl.uniform1f(loc, value);
              else if (value && value.length === 2) gl.uniform2f(loc, value[0], value[1]);
              else if (value && value.length === 3) gl.uniform3f(loc, value[0], value[1], value[2]);
              else if (value && value.length === 4) {
                gl.uniform4f(loc, value[0], value[1], value[2], value[3]);
              }
            });
          }
          gl.drawArrays(gl.TRIANGLES, 0, 3);
          frame += 1;
        };
        var loop = function () {
          draw();
          raf = window.requestAnimationFrame(loop);
        };
        if (paused) {
          frozen = window.performance.now() - started;
          draw();
        } else {
          loop();
        }
        return function () {
          if (raf) window.cancelAnimationFrame(raf);
          if (observer) observer.disconnect();
          shaderSlots = shaderSlots.filter(function (one) {
            return one !== slot;
          });
          // 主动放手：一个页面里几十个演示各自留一个上下文的话，浏览器会踢掉最老的
          var lose = gl.getExtension('WEBGL_lose_context');
          if (lose) {
            try {
              lose.loseContext();
            } catch (err) {
              /* 放不掉就算了，不影响任何事 */
            }
          }
        };
      },
      [frag, vert, paused]
    );

    // 注意：这里的 `h` 是**裸的 `React.createElement`**（不是 app 运行时那个认
    // `div.cls` 简写的 `h`）。写 `'canvas.qf-shader__canvas'` 的话 React 会当它是
    // **标签名里带点**的自定义元素（`CANVAS.QF-SHADER__CANVAS`）—— 既没有 `getContext`，
    // 也匹配不到 `querySelectorAll('canvas')`。实测就是这么炸的（`canvas.getContext is
    // not a function`），所以一律走 `className`。
    return h(
      'div',
      { className: 'qf-shader', style: height ? { height: height } : null },
      h('canvas', { className: 'qf-shader__canvas', ref: canvasRef }),
      error ? h('pre', { className: 'qf-shader__err', text: error }) : null
    );
  }

  /**
   * `QFKit.Shader` —— **两种写法都认**。
   *
   *   ① JSX（推荐）：`<QFKit.Shader frag={…} />`
   *   ② 命令式：`QFKit.Shader(host, { frag, height })` → 返回 `{ stop() }`
   *
   * 为什么非认第二种（2026-09-27 实测）：模型很自然地把它当**函数**调 —— 它写的是
   * `QFKit.Shader(host.current, { fragment: FRAG, width, height, uniform, onError })`
   * （那是它见过的 WebGL 封装的样子）。而组件那份内部要用 hooks，被当函数调用就是
   * `Minified React error #321`（Invalid hook call）：**一块画布都没建出来** →
   * `snapshot()` 拿不到画面 → 模型手里只剩一行 React 报错，于是它**照着想象说
   * "画面在卡片里：两列波…"**（用户的原话："llm 瞎说"）。
   * 与其在提示词里反复求它写 JSX，不如**两种都支持**：第一个参数是 DOM 节点就是命令式。
   */
  function Shader(a, b) {
    if (a && a.nodeType === 1) return shaderOn(a, b);
    return ShaderView(a);
  }

  /**
   * 命令式那份：`QFKit.Shader(host, { frag, … })` → 返回一个**句柄**：
   *
   *     { canvas, gl, setUniform(name, value), resize(w, h), dispose(), stop(), error }
   *
   * 这个形状是**照模型猜的样子定的**（2026-09-27 实测：它连写三版，分别按
   * `{canvas, setUniform, dispose, resize}` 与"React 组件"来用）。既然那是它见过的
   * 封装的样子，与其在提示词里反复纠正，不如**就长成它期待的样子** —— 但要真能用：
   * `setUniform` 每帧照写一遍，`resize` 改的是 CSS 尺寸（分辨率由套件跟着 dpr 走）。
   *
   * **没给 `frag` 是硬错**：实测它定义了 `frag` 却忘了传，于是这里静默不建画布，
   * 回执只能说"没有用 QFKit.Shader"—— 那句话把它带偏了（它明明用了）。
   * 缺源码时必须当场说清：这是命令式调用的第一个必填项。
   */
  function shaderOn(host, options) {
    var opts = options || {};
    var frag = String(opts.frag || opts.fragment || '');
    var handle = { host: host, canvas: null, gl: null, custom: {}, error: '' };
    handle.setUniform = function (name, value) {
      handle.custom[String(name)] = value;
      return handle;
    };
    handle.resize = function (width, height) {
      if (!handle.canvas) return handle;
      if (width) {
        handle.canvas.style.width = typeof width === 'number' ? width + 'px' : String(width);
      }
      if (height) {
        handle.canvas.style.height = typeof height === 'number' ? height + 'px' : String(height);
      }
      return handle;
    };
    handle.dispose = function () {
      return handle.stop();
    };
    handle.stop = function () {
      if (!handle.root) return handle;
      try {
        handle.root.unmount();
      } catch (err) {
        /* 卸不掉就算了（页面要关了） */
      }
      handle.root = null;
      return handle;
    };
    if (!frag) {
      handle.error =
        '没拿到片元着色器源码：命令式调用要给 `frag` —— `QFKit.Shader(宿主节点, { frag })`；' +
        '当组件用则写 `<QFKit.Shader frag={…} />`。';
      // 记进账上 → 回执里会原样报给模型（比"没有用 QFKit.Shader"有用得多）
      shaderSlots.push({ canvas: null, error: handle.error });
      if (typeof opts.onError === 'function') opts.onError(handle.error);
      return handle;
    }
    try {
      handle.root = ReactDOM.createRoot(host);
      handle.root.render(
        h(ShaderView, {
          frag: frag,
          vert: String(opts.vert || opts.vertex || ''),
          paused: !!opts.paused,
          height: opts.height
            ? typeof opts.height === 'number'
              ? opts.height + 'px'
              : String(opts.height)
            : '',
          handle: handle,
          onError: opts.onError,
        })
      );
    } catch (err) {
      if (typeof opts.onError === 'function') opts.onError(err);
    }
    return handle;
  }

  /** 现在这些着色器**编不过**的原话（回传时一起交给模型）。 */
  function shaderErrors() {
    var out = [];
    shaderSlots.forEach(function (one) {
      if (one && one.error) out.push(one.error);
    });
    return out;
  }

  /**
   * 把演示画面拍成 PNG（base64，**不带** `data:` 前缀 —— 服务端那边是按 PNG 拼前缀的）。
   *
   * 不传就拍**所有**着色器画布；传元素就拍它里面能找到的画布。
   * 顺手缩到 `maxWidth`（默认 1100）：回传那条路上限 1.5MB，2K 的原图一超就被丢掉，
   * 丢掉比缩小糟得多（模型看不见画面 = 又回到盲写）。
   * 画布被跨源内容污染时（贴了外部图）取不到 —— 那一张跳过，不影响别的。
   */
  /**
   * **就地**读一遍画布（同步）。返回 `{ images, notes }`。
   *
   * 注意它读的是"此刻"——对没开 `preserveDrawingBuffer` 的 WebGL 画布，帧外读到的
   * 是空的（缓冲已经丢了）。要可靠就用 `captureInFrame`（回执走的就是它）。
   */
  function readCanvases(target, options) {
    var opts = options || {};
    var canvases = [];
    if (target && String(target.tagName || '').toUpperCase() === 'CANVAS') {
      canvases = [target];
    } else if (target && target.querySelectorAll) {
      canvases = Array.prototype.slice.call(target.querySelectorAll('canvas'));
    }
    if (!canvases.length) {
      // **页面上所有的画布都算**：套件自己建的那些，以及**手写的**（实测模型第一版
      // 就是自己 `canvas.getContext('webgl2')` 一路写下来的 —— 那版画面本来是对的，
      // 而这里的旧版本只数套件自己的画布，于是回执说"没有画布"，把它逼着改了五版）。
      // 编不过的套件画布不算（那是空白块，回传只是噪声）。
      var seen = [];
      shaderSlots.forEach(function (one) {
        if (one.error || !one.canvas) return;
        seen.push(one.canvas);
      });
      Array.prototype.slice.call(document.querySelectorAll('canvas')).forEach(function (one) {
        if (seen.indexOf(one) < 0) seen.push(one);
      });
      canvases = seen;
    }
    var maxWidth = opts.maxWidth || 1100;
    snapshotNotes = [];
    var out = [];
    canvases.forEach(function (src) {
      if (!src || !src.width || !src.height) return;
      var scale = Math.min(1, maxWidth / src.width);
      var off = document.createElement('canvas');
      off.width = Math.max(1, Math.round(src.width * scale));
      off.height = Math.max(1, Math.round(src.height * scale));
      var ctx = off.getContext('2d');
      ctx.drawImage(src, 0, 0, off.width, off.height);
      try {
        var data = ctx.getImageData(0, 0, off.width, off.height).data;
        var seenColor = {};
        var count = 0;
        // 抽样就够（每隔 ~1000 个像素看一眼），只为回答"它是不是一整块纯色"
        for (var i = 0; i < data.length; i += 4000) {
          seenColor[data[i] + ',' + data[i + 1] + ',' + data[i + 2]] = 1;
          count += 1;
        }
        if (count > 8 && Object.keys(seenColor).length <= 1) {
          snapshotNotes.push(
            '有一张 ' + src.width + '×' + src.height + ' 的图在**我这边**是纯色。' +
              '注意：这**不代表页面没画** —— 用户那边的画面多半是好的，只是我在这一帧之外' +
              '读不到它（WebGL 画布默认不保留绘图缓冲）。' +
              '**别为了一张截图去改一份看得见效果的演示。**' +
              '真要让像素回到我这边：给那个 canvas 开 `preserveDrawingBuffer: true`，' +
              '或者用 `QFKit.Shader`（它已经开了）。'
          );
        }
      } catch (err) {
        /* 取不到像素就算了（污染过的画布）—— 图还是照拍 */
      }
      try {
        out.push(String(off.toDataURL('image/png')).replace(/^data:image\/png;base64,/, ''));
      } catch (err) {
        /* 污染过的画布取不到，跳过 */
      }
    });
    return { images: out, notes: snapshotNotes };
  }

  /** 就地拍成 base64 数组（旧接口，别处在用）。 */
  function snapshot(target, options) {
    return readCanvases(target, options).images;
  }

  /* ------------------------------------------------------------ 帧内截图 */

  var captureWaiters = [];
  var frameHooked = false;

  /**
   * 在**别人画完那一帧、还没被清掉之前**把画布读出来（异步）。
   *
   * 为什么必须这样：WebGL 画布默认**不保留绘图缓冲**（`preserveDrawingBuffer: false`），
   * 一帧合成完内容就丢了 —— 在别处（比如 900ms 后的回执里）去读，读到的是空的。
   * 于是回执只能说"这张图是纯色"，而模型会据此以为**演示坏了**、甚至准备去改一份本来
   * 好好的代码（用户的原话："画面有了，效果有了，但 llm 听起来不像对的"）。
   *
   * 做法：把 `requestAnimationFrame` 包一层 —— 谁的回调（套件的、还是页面手写的）跑完，
   * 我们**当场**读一遍。这样手写的 canvas **不必为截图改任何东西**。
   * 页面压根没在跑 rAF（暂停了）就超时退回"就地读一次"（多半是空的，但总不能就不报）。
   */
  function captureInFrame(timeout) {
    return new Promise(function (resolve) {
      var done = false;
      var finish = function (shot) {
        if (done) return;
        done = true;
        resolve(shot);
      };
      captureWaiters.push(finish);
      if (!frameHooked) hookFrames();
      window.setTimeout(function () {
        var at = captureWaiters.indexOf(finish);
        if (at >= 0) captureWaiters.splice(at, 1);
        finish(readCanvases(undefined, { maxWidth: 1100 }));
      }, timeout || 1400);
    });
  }

  function hookFrames() {
    frameHooked = true;
    var raf = window.requestAnimationFrame;
    window.requestAnimationFrame = function (callback) {
      return raf.call(window, function (stamp) {
        try {
          if (callback) callback(stamp);
        } finally {
          // **就在这一帧里读**：此刻缓冲还在
          if (captureWaiters.length) {
            var waiters = captureWaiters;
            captureWaiters = [];
            var shot = readCanvases(undefined, { maxWidth: 1100 });
            waiters.forEach(function (done) {
              done(shot);
            });
          }
        }
      });
    };
  }

  /* ------------------------------------------------------------ 回传 */

  //: 这次运行的身份，由服务端注入（`_demo_page` 写 `window.__QF_RUN__`）。
  //: 没有它就没有回执 —— 宿主那条 `qfRun` 回填路是靠它认领的。
  var RUN_ID = String((window.__QF_RUN__ || ''));
  var reported = false;

  /**
   * 把这次演示的结果交回宿主（与 Python 那段是**同一条** `qfRun` 回填路）。
   *
   * ## 为什么必须有它
   *
   * 模型看不见沙箱里发生了什么。这条路在演示上**从来没通过** —— 零件不带 `runId`、
   * 页面也没有桥，于是模型写出来的着色器**它自己一次都没见过**：只能凭想象说
   * "应该出来了"，而用户那边看到的可能是一片黑。用户的话："到我看见画面为止。"
   *
   * `images` 默认是**自动拍的**（`snapshot()`，缩到 ≤1100px）：GLSL 写在哪儿、
   * 画面长什么样，一张图胜过一段描述。模型自己调用时传 `images` 就按它给的走。
   *
   * 报错（页面抛的 + 着色器编不过的）**一律并进 `text`**，`ok` 也跟着变假 ——
   * 让模型拿到的是"哪里错了"的原话，而不是一句"没成功"。
   */
  /**
   * 回执里那句"这一版到底渲染成什么样"。
   *
   * 为什么非要有：没有报错时原先是**空文本** → 模型拿到「（没有输出）」，只能自己猜
   *（实测它就猜了一次，而且方向是错的）。有这一句，它至少知道：几块画布、哪些
   * uniform 被自动喂上了 —— 后一条尤其有用，它据此就知道该怎么命名。
   */
  function summarize() {
    var live = shaderSlots.filter(function (one) {
      return !one.error;
    });
    // **页面上所有的画布都算**，包括手写的 —— 判据只看套件自己的那几块，
    // 会把"其实画得好好的手写 canvas"说成没有（实测就这么把模型逼着改了五版）。
    var all = [];
    shaderSlots.forEach(function (one) {
      if (one.canvas) all.push(one.canvas);
    });
    Array.prototype.slice.call(document.querySelectorAll('canvas')).forEach(function (one) {
      if (all.indexOf(one) < 0) all.push(one);
    });
    if (!all.length) {
      if (shaderSlots.length) {
        return '页面里没有画布渲染成功（原因见下面的报错）—— 这时候的截图是空白/黑图。';
      }
      // **"没有画布"不等于出错**：这一版可能是纯 DOM/文字的画面（方块、箭头、文字动画全用
      // 页面元素拼）—— 那很正常，而从前这里会报"要么还没开始画，要么画面画在了别处"，
      // 于是**一个好好的演示看着像坏了**（2026-09-27 用户原话："这个 demo 正常，但是没有用
      // 着色器，但看上去像坏了"）。这里只**中性陈述事实**，不带任何"出问题了"的味道。
      return '这一版没有画布：画面由页面元素（DOM/文字）构成，不是 canvas 渲染。';
    }
    var bits = ['渲染完成：' + all.length + ' 块画布'];
    bits.push(
      live.length ? '其中 ' + live.length + ' 块来自 QFKit.Shader' : '全是页面自己写的 canvas'
    );
    var bad = shaderSlots.length - live.length;
    if (bad > 0) bits.push('另有 ' + bad + ' 块套件画布编不过（报错在下面）');
    if (live.length) {
      var names = [];
      live.forEach(function (one) {
        (one.uniforms || []).forEach(function (name) {
          if (names.indexOf(name) < 0) names.push(name);
        });
      });
      bits.push(
        names.length
          ? '自动喂上的 uniform：' + names.join('、')
          : '没有声明时间/分辨率这类 uniform —— 画面若不动，多半就是这个原因'
      );
    }
    return bits.join('；') + '。';
  }

  function report(payload) {
    if (reported) return false; // 只报一次（模型自己报过，自动那一次就让位）
    reported = true;
    var body = payload || {};
    var errs = [];
    try {
      errs = (window.__QF_ERRORS__ || []).slice(0, 6);
    } catch (err) {
      errs = [];
    }
    errs = errs.concat(shaderErrors());
    var joined = errs.join('\n');
    // **React 的压缩报错要翻译一句**：`Minified React error #321` 对模型（和人）都没信息量，
    // 而它其实是个很具体的写法问题。实测：模型拿到它只会去猜"编译器怎么了"，然后照着
    // 想象描述画面（用户的原话："llm 瞎说"）。所以这里把话说明白。
    if (/Minified React error #321/.test(joined)) {
      joined +=
        '\n（React #321 = 在组件外面用了 hooks：`QFKit.Shader` 当**函数**调用时，第一个参数' +
        '要给一个 DOM 节点 —— `QFKit.Shader(host.current, {...})`；当**组件**用就写 JSX ' +
        '`<QFKit.Shader frag={…} />`。两种都支持，别把它当普通函数 `QFKit.Shader({frag})` 调。）';
    }

    /** 图拿到（或拿不到）之后，把这一版的话说完、发回宿主。 */
    var finish = function (images, notes) {
      var text = String(body.text || '');
      if (!text) {
        // 模型自己没报话时，**无论如何都给一句有信息量的话** —— 有报错的时候更要给：
        // 光一句 "ERROR: 0:4" 说明不了另一块画布到底渲出来没有。原先这两者还是互斥的
        //（有报错就不给总结），于是模型只能自己猜（实测它就猜了一次，方向还是错的）。
        text = summarize();
      }
      if (joined) text = (text ? text + '\n' : '') + joined;
      // 截图时发现的问题（比如"这张在我这边是纯色"—— 附上原因与"别改演示"的提醒）
      if (notes && notes.length) text = (text ? text + '\n' : '') + notes.join('\n');
      var ok = body.ok === undefined ? !errs.length : !!body.ok;
      try {
        parent.postMessage({ qfRun: RUN_ID, ok: ok, text: text, images: images || [] }, '*');
      } catch (err) {
        /* 不在 iframe 里（直接打开这一页）就没什么可回传的 */
      }
    };

    // 图**在帧内读**（见 `captureInFrame`）：这样连手写的 canvas 都不必为截图改任何代码。
    // 调用方自己给了图就按它给的走（`QFKit.report({ images })`）。
    if (body.images !== undefined) {
      finish(body.images || [], []);
      return true;
    }
    captureInFrame().then(function (shot) {
      finish((shot && shot.images) || [], (shot && shot.notes) || []);
    });
    return true;
  }

  /* **自动那一报**：等一会儿再拍 —— 着色器第一帧之前画布还是空的。900ms 够走完首帧，
   * 又不至于让模型干等。模型自己 `QFKit.report()` 报过的话，这一次自动让位。 */
  if (RUN_ID) {
    var autoReport = function () {
      window.setTimeout(function () {
        report({});
      }, 900);
    };
    if (document.readyState === 'loading') {
      window.addEventListener('DOMContentLoaded', autoReport);
    } else {
      autoReport();
    }
  }

  return {
    React: React,
    ReactDOM: ReactDOM,
    d3: d3,
    colors: colors,
    mount: mount,
    useTicker: useTicker,
    useStepper: useStepper,
    guardContrast: guardContrast,
    Plot: Plot,
    Math: MathBlock,
    Mermaid: Mermaid,
    tex: texHtml,
    useAxis: useAxis,
    scales: scales,
    line: line,
    Card: Card,
    Frame: Frame,
    Row: Row,
    Btn: Btn,
    Legend: Legend,
    KV: KV,
    Note: Note,
    Chip: Chip,
    Shader: Shader,
    shaderErrors: shaderErrors,
    snapshot: snapshot,
    report: report,
  };
})();
