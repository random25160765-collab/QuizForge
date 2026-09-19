/* notegraph.js —— 笔记双链图谱的渲染（一个库一张图）。
 *
 * 为什么单独一件：它有两个用处 —— **资源页**里的一个窗格、**笔记页**里的一个标签。
 * 原先长在 workbench.js 里，那笔记页就加载不到（pane 视图的注册只发生在那一页）。
 * 契约与 canvas.js 那份一致：`mount(host, opts)`，自己画进给它的容器。
 */
(function () {
  var QF = (window.QF = window.QF || {});
  var ui = QF.ui;
  var h = ui.h;
  var api = QF.api;

  var SVG_NS = 'http://www.w3.org/2000/svg';

  function svgEl(name, attrs) {
    var node = document.createElementNS(SVG_NS, name);
    Object.keys(attrs || {}).forEach(function (key) {
      if (attrs[key] != null) node.setAttribute(key, String(attrs[key]));
    });
    return node;
  }

  /**
   * 小而够用的力导向布局。仓库不引依赖（铁律），图谱页那套是写死在页面里的单体，
   * 抽不出来复用 —— 所以这里自己写一份：斥力 + 弹簧 + 向心，跑固定步数就停。
   * 节点几百个以内一次布局几十毫秒；再多就把步数降下来（宁可松一点，也别卡住）。
   */
  function layout(nodes, edges, cw, ch) {
    var n = nodes.length;
    if (!n) return;
    var byId = {};
    var i;
    var j;
    for (i = 0; i < n; i++) {
      var angle = (i / n) * Math.PI * 2;
      nodes[i].x = cw / 2 + Math.cos(angle) * Math.min(cw, ch) * 0.34;
      nodes[i].y = ch / 2 + Math.sin(angle) * Math.min(cw, ch) * 0.34;
      nodes[i].vx = 0;
      nodes[i].vy = 0;
      byId[nodes[i].id] = nodes[i];
    }
    var links = [];
    edges.forEach(function (edge) {
      var a = byId[edge.source];
      var b = byId[edge.target];
      if (a && b && a !== b) links.push({ a: a, b: b });
    });
    // 连得多的点给更大半径，同时它也更"重"（响应小一点），读图时更清楚
    nodes.forEach(function (nd) { nd.deg = 0; });
    links.forEach(function (l) { l.a.deg += 1; l.b.deg += 1; });

    var steps = n > 400 ? 90 : n > 200 ? 150 : 260;
    var REP = 2600;
    var SPRING = 0.02;
    var REST = 96;
    var CENTER = 0.0055;
    var DAMP = 0.82;

    for (var step = 0; step < steps; step++) {
      for (i = 0; i < n; i++) {
        var p = nodes[i];
        var px = p.x;
        var py = p.y;
        for (j = i + 1; j < n; j++) {
          var q = nodes[j];
          var dx = q.x - px;
          var dy = q.y - py;
          var d2 = dx * dx + dy * dy;
          if (d2 < 0.01) { d2 = 0.01; dx = 0.1; dy = 0.1; }
          var d = Math.sqrt(d2);
          var f = REP / d2;
          var ux = dx / d;
          var uy = dy / d;
          p.vx -= ux * f;
          p.vy -= uy * f;
          q.vx += ux * f;
          q.vy += uy * f;
        }
      }
      links.forEach(function (l) {
        var dx = l.b.x - l.a.x;
        var dy = l.b.y - l.a.y;
        var d = Math.sqrt(dx * dx + dy * dy) || 0.01;
        var f = (d - REST) * SPRING;
        var ux = dx / d;
        var uy = dy / d;
        l.a.vx += ux * f * 1.4;
        l.a.vy += uy * f * 1.4;
        l.b.vx -= ux * f;
        l.b.vy -= uy * f;
      });
      for (i = 0; i < n; i++) {
        var nd = nodes[i];
        nd.vx += (cw / 2 - nd.x) * CENTER;
        nd.vy += (ch / 2 - nd.y) * CENTER;
        nd.vx *= DAMP;
        nd.vy *= DAMP;
        nd.x = Math.max(14, Math.min(cw - 14, nd.x + nd.vx));
        nd.y = Math.max(14, Math.min(ch - 14, nd.y + nd.vy));
      }
    }
  }
  /** 把图谱画进 `host`（`opts.lib` 是库名）。 */
  function mount(host, opts) {
    var wrap = h('div.ngraph');
    host.appendChild(wrap);
    var lib = opts && opts.lib;
    if (!lib) {
      wrap.appendChild(h('p.panes__muted', { text: '没给库名' }));
      return;
    }
    wrap.appendChild(h('p.panes__muted', { text: '正在读 ' + lib + ' 的双链…' }));
    api.get('/notes/graph?lib=' + encodeURIComponent(lib)).then(function (data) {
      ui.clear(wrap);
      var nodes = (data.nodes || []).map(function (one) {
        return { id: one.id, title: one.title || one.id, tags: one.tags || [], out: one.out || 0 };
      });
      var edges = data.edges || [];
      if (!nodes.length) {
        wrap.appendChild(h('p.panes__muted', { text: '这个库里没有笔记' }));
        return;
      }
      // 注意：这里**不能**把宽高叫 `w` / `h` —— 本文件顶部有 `var h = ui.h`（建 DOM 用的），
      // 在同一个函数里再声明一个 `var h`，提升之后整个作用域里的 `h(...)` 全变成拿数字当函数
      //（实测报的就是 "h is not a function"）。叫 vw / vh。
      var box = wrap.getBoundingClientRect();
      var vw = Math.max(360, Math.round(box.width) || 720);
      var vh = Math.max(300, Math.round(box.height) || 520);
      layout(nodes, edges, vw, vh);

      var svg = svgEl('svg', { viewBox: '0 0 ' + vw + ' ' + vh, class: 'ngraph__svg' });
      var byId = {};
      nodes.forEach(function (nd) { byId[nd.id] = nd; });
      edges.forEach(function (edge) {
        var a = byId[edge.source];
        var b = byId[edge.target];
        if (!a || !b) return;
        svg.appendChild(svgEl('line', {
          x1: a.x, y1: a.y, x2: b.x, y2: b.y, class: 'ngraph__edge',
        }));
      });
      var isolated = 0;
      nodes.forEach(function (nd) {
        if (!nd.deg) isolated += 1;
        var r = 4 + Math.min(7, Math.sqrt(nd.deg) * 2.6);
        var dot = svgEl('circle', { cx: nd.x, cy: nd.y, r: r, class: 'ngraph__node' + (nd.deg ? '' : ' is-lone') });
        var title = svgEl('title');
        title.textContent = nd.title + '\n' + nd.id + (nd.deg ? '\n连接 ' + nd.deg : '\n（没有链接）');
        dot.appendChild(title);
        dot.addEventListener('click', function () {
          QF.panes.open('note', { lib: lib, path: nd.id }, nd.title);
        });
        svg.appendChild(dot);
      });
      wrap.appendChild(svg);
      wrap.appendChild(h('p.panes__muted.ngraph__foot', {
        text: '《' + lib + '》' + nodes.length + ' 篇 · ' + edges.length + ' 条双链 · '
          + isolated + ' 篇没有链接（点一个圆点就在旁边打开那篇）',
      }));
    }).catch(function (err) {
      ui.clear(wrap);
      wrap.appendChild(h('p.panes__muted', { text: '读不到图谱：' + ((err && err.message) || err) }));
    });
  }

  QF.notegraph = { mount: mount };
})();
