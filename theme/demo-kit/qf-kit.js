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
  };
})();
