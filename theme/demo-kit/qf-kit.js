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

  /** 与 app 同一套色：演示看起来像是这个应用的一部分，而不是另一个网站。 */
  var colors = {
    bg: '#0e1116',
    panel: '#161b22',
    panel2: '#1c2430',
    line: '#2a3240',
    line2: '#39465a',
    fg: '#e6edf3',
    fg2: '#c3ccd8',
    dim: '#8b97a6',
    pri: '#4da3ff',
    ok: '#41d67a',
    warn: '#ffb454',
    bad: '#ff5c6c',
    // 多条序列就用它，别自己挑色（挑出来往往对比度不够）
    series: ['#4da3ff', '#41d67a', '#ffb454', '#c56bff', '#ff9f43', '#6be3ff', '#ff5c6c', '#9ad1ff'],
  };

  /* ------------------------------------------------------------ 挂载 */

  /** 一行挂载：`QFKit.mount(<App/>)`。没给节点就找 `#app`，再退回 body。 */
  function mount(element, node) {
    var host = node || document.getElementById('app') || document.body;
    if (!ReactDOM.createRoot) {
      ReactDOM.render(element, host); // 万一用的是 17 那份 React
      return host;
    }
    ReactDOM.createRoot(host).render(element);
    return host;
  }

  /* ------------------------------------------------------------ 动画 */

  /**
   * 帧驱动的步进量。用法：`const t = QFKit.useTicker(1)` —— 每帧 +1。
   *
   * 为什么不用 `setInterval`：它和渲染不同步，慢机器上会漂、快机器上会堆。
   * 这个钩子用 `requestAnimationFrame`，并且把"暂停"做成参数：
   * `useTicker(1, { paused })` —— 播放/暂停按钮直接传 state 就行。
   */
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
            return value + (dt * rate) / 16.667;
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
