/* 画布：JSON Canvas 的视图与交互。
 *
 * 形态照 `draft/canvas.png`（Obsidian Canvas）：白底圆角卡片、灰色**贝塞尔曲线带箭头**的连接、
 * 右缘竖排工具、底部居中的三个添加按钮、顶部居中的画布名。数据形状照源库里的真实画布
 * （`app/canvas.py` 的注释里有实测样本）。
 *
 * 五条讲究：
 *
 * 1. **坐标是任意的**（实测真实画布里有 `x=-17640`）。所以：平面用 CSS transform 平移缩放，
 *    内容坐标不做任何假设；进来先按包围盒 `fit()` —— 否则打开就是一片空白，用户以为坏了。
 * 2. **拖拽期间不重画整张图**。拖动只挪那一个元素、只重画它的连线；松手才落盘。
 *    一帧重画上百个 DOM 会把拖动卡成幻灯片。
 * 3. **落盘是攒批的**（一次拖拽 = 一次保存）。逐个位移存会把后端的快照轮转冲干净，
 *    「撤销」随即失效 —— 那正是画布上最容易毁掉的东西。
 * 4. **一稿一渲染**：卡片文本存进 `node.text`（纯文本 + 换行），显示时按纯文本走 ——
 *    画布不是笔记，不在这里渲染 Markdown（Obsidian 也是这么分的）。
 * 5. **不认识的东西不丢**：读写都由后端原样保留多余字段（`app/canvas.py`），前端只管认识的。
 */
(function () {
  'use strict';

  var QF = (window.QF = window.QF || {});
  var ui = QF.ui;
  var api = QF.api;

  function h() {
    return ui.h.apply(ui, arguments);
  }

  //: SVG 的一切都要 `createElementNS` 造。实测踩过：`ui.h('svg…')` 造出来的是
  //: **HTML 命名空间**的元素（`namespaceURI` 是 xhtml），浏览器根本不按 SVG 渲染 ——
  //: 元素在 DOM 里找得到、`getBoundingClientRect().width` 却是 0，也就是"连线根本看不见"。
  var SVG_NS = 'http://www.w3.org/2000/svg';

  function svgEl(tag, attrs) {
    var el = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs || {}).forEach(function (key) {
      el.setAttribute(key, attrs[key]);
    });
    return el;
  }

  //: Obsidian 画布的六色预设（节点与连线共用编号）。它存在 `color` 字段里，取值为 "1".."6"。
  var PALETTE = {
    1: '#e05252',
    2: '#e08f52',
    3: '#e0c552',
    4: '#5bbf6a',
    5: '#52b6e0',
    6: '#a97be0'
  };

  var ZOOM_MIN = 0.25;
  var ZOOM_MAX = 3;
  var SIDES = ['top', 'right', 'bottom', 'left'];
  var SAVE_DELAY = 500;

  //: 当前挂载的那一块画布。这一页一次只开一篇，所以单例就够（多开会串状态）。
  var live = null;

  function mount(host, options) {
    unmount();
    var state = {
      host: host,
      lib: options.lib,
      path: options.path,
      onOpenNote: options.onOpenNote || function () {},
      onStatus: options.onStatus || function () {},
      onDirty: options.onDirty || function () {},
      data: { nodes: [], edges: [] },
      bbox: null,
      zoom: 1,
      pan: { x: 0, y: 0 },
      selected: '',
      editing: '',
      pending: [],
      timer: null,
      drag: null,
      loaded: false
    };
    live = state;
    build(state);
    load(state);
  }

  function unmount() {
    if (live && live.timer) clearTimeout(live.timer);
    if (live && live.host) ui.clear(live.host);
    live = null;
  }

  /** 把整块画布画出来。挂载时一次，之后只重画平面内部。 */
  function build(state) {
    var host = state.host;
    ui.clear(host);

    var title = h('div.ncanvas__title', { text: state.path.split('/').pop().replace(/\.canvas$/i, '') });
    var plane = h('div.ncanvas__plane');
    // `overflow: visible` 是关键：路径坐标是**内容坐标**（可能负几万），
    // SVG 自己只是个不裁剪的坐标系，别指望它的宽高能装下内容。
    var edges = svgEl('svg', {
      class: 'ncanvas__edges',
      style: 'overflow: visible',
      width: '1',
      height: '1'
    });
    // defs 放在**平级**的另一个 svg 里，不放进 edges —— `paint()` 会 `ui.clear(edges)`
    // 重画所有连线，把 defs 一起清掉，箭头就没了（实测：边能画出来，箭头全丢）。
    var defs = svgEl('svg', { class: 'ncanvas__defs', width: '0', height: '0' });
    defs.appendChild(arrowDefs());
    var layer = h('div.ncanvas__nodes');
    plane.appendChild(defs);
    plane.appendChild(edges);
    plane.appendChild(layer);

    var surface = h('div.ncanvas__surface', null, plane);
    surface.addEventListener('pointerdown', function (ev) {
      onSurfaceDown(state, ev);
    });
    surface.addEventListener('wheel', function (ev) {
      onWheel(state, ev);
    }, { passive: false });
    surface.addEventListener('dblclick', function (ev) {
      onSurfaceDouble(state, ev);
    });

    var rail = h('div.ncanvas__rail');
    [
      ['适应窗口', 'fit', '把内容缩放居中（刚打开时自动做一次）', function () { fit(state); }],
      ['放大', 'in', '', function () { zoomBy(state, 1.2); }],
      ['缩小', 'out', '', function () { zoomBy(state, 1 / 1.2); }],
      ['回到原点', 'origin', '回到内容左上角（1:1）', function () { origin(state); }]
    ].forEach(function (item) {
      rail.appendChild(
        h('button.ncanvas__tool', {
          type: 'button',
          html: toolIcon(item[1]),
          title: item[0],
          'aria-label': item[0],
          onClick: item[3]
        })
      );
    });

    var dock = h(
      'div.ncanvas__dock',
      null,
      h('button.ncanvas__add', {
        type: 'button',
        text: '＋ 卡片',
        title: '新建一张文字卡片（双击空白处也行）',
        onClick: function () {
          addNode(state, { type: 'text', x: centerX(state), y: centerY(state) });
        }
      }),
      h('button.ncanvas__add', {
        type: 'button',
        text: '＋ 分组',
        title: '新建一个分组框（先把卡片摆好，再框住它们）',
        onClick: function () {
          addNode(state, { type: 'group', x: centerX(state), y: centerY(state) });
        }
      }),
      h('button.ncanvas__add', {
        type: 'button',
        text: '⇢ 当前笔记',
        title: '把正在看的这篇笔记作为卡片放上来',
        onClick: function () {
          addNode(state, { type: 'file', file: state.path, x: centerX(state), y: centerY(state) });
        }
      })
    );

    var hint = h('div.ncanvas__hint', {
      text: '双击空白处新建卡片 · 拖卡片右缘的小圆点连线 · 双击卡片改文字 · 选中后按 Delete 删掉'
    });

    host.appendChild(h('div.ncanvas', null, title, surface, rail, dock, hint));
    state.surface = surface;
    state.plane = plane;
    state.edges = edges;
    state.layer = layer;

    host.tabIndex = 0;
    host.addEventListener('keydown', function (ev) {
      onKey(state, ev);
    });
  }

  /** 箭头。按颜色各做一个 marker —— SVG 的 `context-stroke` 支持面还不够，
   * 而"连线有箭头"是参考图里最显眼的一处（表示方向），不能省。 */
  function arrowDefs() {
    var defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
    var colors = { base: 'currentColor' };
    Object.keys(PALETTE).forEach(function (key) {
      colors['c' + key] = PALETTE[key];
    });
    Object.keys(colors).forEach(function (name) {
      var marker = document.createElementNS('http://www.w3.org/2000/svg', 'marker');
      marker.setAttribute('id', 'ncanvas-arrow-' + name);
      marker.setAttribute('viewBox', '0 0 10 10');
      marker.setAttribute('refX', '9');
      marker.setAttribute('refY', '5');
      marker.setAttribute('markerWidth', '7');
      marker.setAttribute('markerHeight', '7');
      marker.setAttribute('orient', 'auto-start-reverse');
      var tip = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      tip.setAttribute('d', 'M 0 1 L 9 5 L 0 9 z');
      tip.setAttribute('fill', colors[name]);
      marker.appendChild(tip);
      defs.appendChild(marker);
    });
    return defs;
  }

  function toolIcon(name) {
    var paths = {
      fit: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
      in: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5M11 8v6M8 11h6"/>',
      out: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5M8 11h6"/>',
      origin: '<path d="M4 4h6M4 4v6"/><path d="M4 4l7 7"/><path d="M4 20h16M20 4v16"/>'
    };
    return (
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" ' +
      'stroke-linecap="round" stroke-linejoin="round">' + (paths[name] || '') + '</svg>'
    );
  }

  // ------------------------------------------------------------------ 读写

  function load(state) {
    api
      .get('/notes/canvas?' + qs({ lib: state.lib, path: state.path }))
      .then(function (data) {
        state.data = data;
        state.bbox = data.bbox || null;
        state.loaded = true;
        paint(state);
        fit(state);
        if (data.broken) {
          ui.toast('这块画布读不全（' + data.broken + '），先按空的显示', 'bad');
        }
        state.onStatus('已加载 · ' + state.data.nodes.length + ' 张卡片 · ' + state.data.edges.length + ' 条连线');
      })
      .catch(function (err) {
        state.onStatus('读不到这块画布');
        ui.toast((err && err.message) || '读不到这块画布', 'bad');
      });
  }

  /** 攒批落盘：一次拖拽、一串位移，都只算一个版本。 */
  function queue(state, op) {
    state.pending.push(op);
    state.onDirty(true);
    state.onStatus('有改动，正在保存…');
    if (state.timer) clearTimeout(state.timer);
    state.timer = setTimeout(function () {
      flush(state);
    }, SAVE_DELAY);
  }

  function flush(state) {
    if (state.timer) {
      clearTimeout(state.timer);
      state.timer = null;
    }
    if (!state.pending.length) return;
    var ops = state.pending;
    state.pending = [];
    api
      .post('/notes/canvas', { lib: state.lib, path: state.path, ops: ops })
      .then(function (res) {
        state.data = res.canvas;
        state.bbox = res.bbox;
        state.onDirty(false);
        state.onStatus('已保存 ' + nowHM());
        if (state.editing === '') paint(state);   // 编辑中的别重画，会顶掉输入框里的字
      })
      .catch(function (err) {
        state.pending = ops.concat(state.pending);   // 失败就留着，下次一起再送
        state.onStatus('保存失败，改动还在');
        ui.toast((err && err.message) || '画布保存失败', 'bad');
      });
  }

  // ------------------------------------------------------------------ 画

  function paint(state) {
    ui.clear(state.layer);
    ui.clear(state.edges);
    // 分组框先画，其余按 y 排 —— 分组当底、卡片在上，谁压谁一目了然
    var ordered = state.data.nodes.slice().sort(function (a, b) {
      if (a.type === 'group' && b.type !== 'group') return -1;
      if (b.type === 'group' && a.type !== 'group') return 1;
      return a.y - b.y;
    });
    ordered.forEach(function (node) {
      state.layer.appendChild(nodeEl(state, node));
    });
    state.data.edges.forEach(function (edge) {
      state.edges.appendChild(edgeEl(state, edge));
    });
    applyTransform(state);
  }

  function nodeEl(state, node) {
    var el = h('div.ncanvas__node.ncanvas__node--' + node.type + (state.selected === node.id ? '.is-on' : ''), {
      dataset: { id: node.id },
      style: {
        left: node.x + 'px',
        top: node.y + 'px',
        width: node.width + 'px',
        height: node.height + 'px'
      }
    });
    if (node.color && PALETTE[String(node.color)]) {
      el.style.setProperty('--node-accent', PALETTE[String(node.color)]);
      el.classList.add('has-color');
    }

    if (node.type === 'group') {
      el.appendChild(h('div.ncanvas__group-label', { text: node.label || '分组' }));
    } else if (node.type === 'file') {
      var target = String(node.file || '');
      el.appendChild(h('div.ncanvas__file', { text: target.split('/').pop().replace(/\.md$/i, '') }));
      el.appendChild(h('div.ncanvas__filepath', { text: target }));
      el.addEventListener('dblclick', function (ev) {
        ev.stopPropagation();
        state.onOpenNote(target);
      });
    } else {
      el.appendChild(h('div.ncanvas__text', { text: node.text || '' }));
      if (!String(node.text || '').trim()) el.classList.add('is-empty');
      el.addEventListener('dblclick', function (ev) {
        ev.stopPropagation();
        startEdit(state, node);
      });
    }

    // 四个方位的连线把小圆点：平时隐形，悬停/选中才出现（照参考图那种干净的样子）
    if (node.type !== 'group') {
      SIDES.forEach(function (side) {
        el.appendChild(
          h('span.ncanvas__port.ncanvas__port--' + side, {
            dataset: { side: side },
            title: '从这里拖到另一张卡片 = 连线'
          })
        );
      });
      el.appendChild(h('span.ncanvas__grip', { title: '拖这个角改大小' }));
    }

    el.addEventListener('pointerdown', function (ev) {
      onNodeDown(state, ev, node);
    });
    return el;
  }

  function edgeEl(state, edge) {
    var from = find(state, edge.fromNode);
    var to = find(state, edge.toNode);
    if (!from || !to) return h('g');
    var d = edgePath(from, to, edge);
    var tone = edge.color && PALETTE[String(edge.color)] ? 'c' + edge.color : 'base';
    var path = svgEl('path', {
      class: 'ncanvas__edge',
      d: d,
      'marker-end': 'url(#ncanvas-arrow-' + tone + ')'
    });
    if (tone !== 'base') path.setAttribute('stroke', PALETTE[String(edge.color)]);
    var group = svgEl('g', {});
    group.dataset.id = edge.id;
    group.appendChild(path);
    // 一条透明的粗线盖在上面：线太细点不中，删连线的手感全靠它
    group.appendChild(svgEl('path', { class: 'ncanvas__edge-hit', d: d }));
    group.addEventListener('pointerdown', function (ev) {
      ev.stopPropagation();
      ui.confirm('删掉这条连线？', { okLabel: '删掉' }).then(function (yes) {
        if (!yes) return;
        removeEdge(state, edge.id);
      });
    });
    if (edge.label) {
      var mid = midPoint(from, to, edge);
      var label = svgEl('text', {
        class: 'ncanvas__edge-label',
        x: String(mid.x),
        y: String(mid.y),
        'text-anchor': 'middle'
      });
      label.textContent = edge.label;
      group.appendChild(label);
    }
    return group;
  }

  /** 从任意事件目标往上找那张卡片。SVG 元素上 `closest` 的行为与 HTML 不同，
   * 所以自己走一遍父链 —— 比依赖它的实现细节稳。 */
  function nodeOf(target) {
    var el = target;
    while (el && el !== document) {
      if (el.classList && el.classList.contains('ncanvas__node')) return el;
      el = el.parentNode;
    }
    return null;
  }

  function find(state, id) {
    for (var i = 0; i < state.data.nodes.length; i++) {
      if (state.data.nodes[i].id === id) return state.data.nodes[i];
    }
    return null;
  }

  /** 自动挑方位：没写 fromSide/toSide 时，挑面对面的一对，曲线才不会绕回去。 */
  function sideOf(from, to, key) {
    var dx = to.x + to.width / 2 - (from.x + from.width / 2);
    var dy = to.y + to.height / 2 - (from.y + from.height / 2);
    if (Math.abs(dx) >= Math.abs(dy)) return dx >= 0 ? 'right' : 'left';
    return dy >= 0 ? 'bottom' : 'top';
  }

  function anchor(node, side) {
    if (side === 'top') return { x: node.x + node.width / 2, y: node.y };
    if (side === 'bottom') return { x: node.x + node.width / 2, y: node.y + node.height };
    if (side === 'left') return { x: node.x, y: node.y + node.height / 2 };
    return { x: node.x + node.width, y: node.y + node.height / 2 };
  }

  function normal(side) {
    if (side === 'top') return { x: 0, y: -1 };
    if (side === 'bottom') return { x: 0, y: 1 };
    if (side === 'left') return { x: -1, y: 0 };
    return { x: 1, y: 0 };
  }

  /** 三次贝塞尔：控制点沿方位法线外推，得到参考图里那种"柔和拐弯"的线。 */
  function edgePath(from, to, edge) {
    var a = anchor(from, edge.fromSide || sideOf(from, to));
    var b = anchor(to, edge.toSide || sideOf(to, from));
    var na = normal(edge.fromSide || sideOf(from, to));
    var nb = normal(edge.toSide || sideOf(to, from));
    var span = Math.max(60, Math.min(240, Math.hypot(b.x - a.x, b.y - a.y) * 0.45));
    var c1 = { x: a.x + na.x * span, y: a.y + na.y * span };
    var c2 = { x: b.x + nb.x * span, y: b.y + nb.y * span };
    return (
      'M ' + a.x + ' ' + a.y +
      ' C ' + c1.x + ' ' + c1.y + ', ' + c2.x + ' ' + c2.y + ', ' + b.x + ' ' + b.y
    ).replace(/(\d)\.(\d+)/g, function (all, head, tail) {
      return head + '.' + tail.slice(0, 2);
    });
  }

  function midPoint(from, to, edge) {
    var a = anchor(from, edge.fromSide || sideOf(from, to));
    var b = anchor(to, edge.toSide || sideOf(to, from));
    return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 - 6 };
  }

  function applyTransform(state) {
    state.plane.style.transform =
      'translate(' + state.pan.x + 'px, ' + state.pan.y + 'px) scale(' + state.zoom + ')';
  }

  // ------------------------------------------------------------------ 视口

  function surfaceSize(state) {
    var rect = state.surface.getBoundingClientRect();
    return { width: rect.width || 800, height: rect.height || 600 };
  }

  function centerX(state) {
    var size = surfaceSize(state);
    return Math.round((size.width / 2 - state.pan.x) / state.zoom - 125);
  }

  function centerY(state) {
    var size = surfaceSize(state);
    return Math.round((size.height / 2 - state.pan.y) / state.zoom - 30);
  }

  function fit(state) {
    var box = state.bbox || { x: 0, y: 0, width: 0, height: 0 };
    var size = surfaceSize(state);
    if (!box.width && !box.height) {
      state.zoom = 1;
      state.pan = { x: size.width / 2, y: size.height / 2 };
      applyTransform(state);
      return;
    }
    var pad = 60;
    var zoom = Math.min((size.width - pad * 2) / box.width, (size.height - pad * 2) / box.height, 1.4);
    state.zoom = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, zoom));
    state.pan = {
      x: (size.width - box.width * state.zoom) / 2 - box.x * state.zoom,
      y: (size.height - box.height * state.zoom) / 2 - box.y * state.zoom
    };
    applyTransform(state);
  }

  function origin(state) {
    state.zoom = 1;
    state.pan = { x: 60, y: 60 };
    applyTransform(state);
  }

  function zoomBy(state, factor, at) {
    var size = surfaceSize(state);
    var point = at || { x: size.width / 2, y: size.height / 2 };
    var before = state.zoom;
    var next = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, before * factor));
    if (next === before) return;
    // 以鼠标（或视口中心）为锚点缩放：锚点下的内容保持不动
    state.pan.x = point.x - ((point.x - state.pan.x) / before) * next;
    state.pan.y = point.y - ((point.y - state.pan.y) / before) * next;
    state.zoom = next;
    applyTransform(state);
  }

  /** 屏幕坐标 → 内容坐标。拖拽与"双击在哪新建"都靠它。 */
  function toCanvas(state, clientX, clientY) {
    var rect = state.surface.getBoundingClientRect();
    return {
      x: (clientX - rect.left - state.pan.x) / state.zoom,
      y: (clientY - rect.top - state.pan.y) / state.zoom
    };
  }

  // ------------------------------------------------------------------ 交互

  function onWheel(state, ev) {
    ev.preventDefault();
    if (ev.ctrlKey || ev.metaKey) {
      var rect = state.surface.getBoundingClientRect();
      zoomBy(state, ev.deltaY < 0 ? 1.12 : 1 / 1.12, { x: ev.clientX - rect.left, y: ev.clientY - rect.top });
      return;
    }
    state.pan.x -= ev.deltaX;
    state.pan.y -= ev.deltaY;
    applyTransform(state);
  }

  function onSurfaceDown(state, ev) {
    if (nodeOf(ev.target)) return;
    select(state, '');
    var start = { x: ev.clientX, y: ev.clientY, panX: state.pan.x, panY: state.pan.y };
    state.surface.setPointerCapture(ev.pointerId);
    state.drag = { kind: 'pan', start: start };
    var move = function (moveEv) {
      state.pan.x = start.panX + (moveEv.clientX - start.x);
      state.pan.y = start.panY + (moveEv.clientY - start.y);
      applyTransform(state);
    };
    var up = function () {
      state.drag = null;
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }

  function onSurfaceDouble(state, ev) {
    if (nodeOf(ev.target)) return;
    var point = toCanvas(state, ev.clientX, ev.clientY);
    addNode(state, { type: 'text', x: Math.round(point.x), y: Math.round(point.y) });
  }

  function onNodeDown(state, ev, node) {
    ev.stopPropagation();
    if (state.editing) finishEdit(state);
    var port = closestOf(ev.target, 'ncanvas__port');
    if (port) {
      startConnect(state, node, port.dataset.side, ev);
      return;
    }
    if (closestOf(ev.target, 'ncanvas__grip')) {
      startResize(state, node, ev);
      return;
    }
    select(state, node.id);
    if (node.type === 'group') return;      // 分组框不做整体拖动（拖里面的卡片就够了）
    startMove(state, node, ev);
  }

  function select(state, id) {
    if (state.selected === id) return;
    state.selected = id;
    var nodes = state.layer.querySelectorAll('.ncanvas__node');
    Array.prototype.forEach.call(nodes, function (el) {
      el.classList.toggle('is-on', el.dataset.id === id);
    });
  }

  function startMove(state, node, ev) {
    var start = toCanvas(state, ev.clientX, ev.clientY);
    var origin = { x: node.x, y: node.y };
    var el = state.layer.querySelector('.ncanvas__node[data-id="' + node.id + '"]');
    var moved = false;
    var move = function (moveEv) {
      var point = toCanvas(state, moveEv.clientX, moveEv.clientY);
      var dx = Math.round(point.x - start.x);
      var dy = Math.round(point.y - start.y);
      if (!moved && Math.abs(dx) + Math.abs(dy) < 2) return;
      moved = true;
      node.x = origin.x + dx;
      node.y = origin.y + dy;
      if (el) {
        el.style.left = node.x + 'px';
        el.style.top = node.y + 'px';
      }
      repaintEdges(state);
    };
    var up = function () {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      if (!moved) return;
      queue(state, { op: 'update_node', id: node.id, x: node.x, y: node.y });
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }

  function startResize(state, node, ev) {
    var start = toCanvas(state, ev.clientX, ev.clientY);
    var origin = { width: node.width, height: node.height };
    var el = state.layer.querySelector('.ncanvas__node[data-id="' + node.id + '"]');
    var move = function (moveEv) {
      var point = toCanvas(state, moveEv.clientX, moveEv.clientY);
      node.width = Math.max(60, Math.round(origin.width + (point.x - start.x)));
      node.height = Math.max(40, Math.round(origin.height + (point.y - start.y)));
      if (el) {
        el.style.width = node.width + 'px';
        el.style.height = node.height + 'px';
      }
      repaintEdges(state);
    };
    var up = function () {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      queue(state, { op: 'update_node', id: node.id, width: node.width, height: node.height });
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }

  /** 从卡片边缘的小圆点拖到另一张卡片上 = 连线（照 Obsidian 的手法）。 */
  function startConnect(state, node, side, ev) {
    var preview = svgEl('path', { class: 'ncanvas__edge ncanvas__edge--live' });
    state.edges.appendChild(preview);
    var start = anchor(node, side);
    var hover = '';
    var move = function (moveEv) {
      var point = toCanvas(state, moveEv.clientX, moveEv.clientY);
      preview.setAttribute('d', 'M ' + start.x + ' ' + start.y + ' L ' + point.x + ' ' + point.y);
      var box = nodeOf(document.elementFromPoint(moveEv.clientX, moveEv.clientY));
      hover = box && box.dataset.id !== node.id ? box.dataset.id : '';
      Array.prototype.forEach.call(state.layer.querySelectorAll('.ncanvas__node'), function (item) {
        item.classList.toggle('is-target', item.dataset.id === hover);
      });
    };
    var up = function () {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      preview.remove();
      Array.prototype.forEach.call(state.layer.querySelectorAll('.ncanvas__node'), function (item) {
        item.classList.remove('is-target');
      });
      if (!hover) return;
      queue(state, { op: 'add_edge', fromNode: node.id, toNode: hover, fromSide: side });
      // 先本地加一条，再等后端回（不然拖完半天看不到线）
      state.data.edges.push({ id: 'temp-' + Date.now(), fromNode: node.id, toNode: hover, fromSide: side });
      paint(state);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }

  function repaintEdges(state) {
    ui.clear(state.edges);
    state.data.edges.forEach(function (edge) {
      state.edges.appendChild(edgeEl(state, edge));
    });
  }

  function addNode(state, spec) {
    var op = { op: 'add_node', type: spec.type || 'text' };
    if (spec.file) op.file = spec.file;
    if (spec.x !== undefined) op.x = spec.x;
    if (spec.y !== undefined) op.y = spec.y;
    // 本地先摆一张占位的，让动作立刻有反馈；后端回来的才是准数（含它算好的位置）
    var ghost = {
      id: 'temp-' + Date.now(),
      type: op.type,
      x: op.x || 0,
      y: op.y || 0,
      width: spec.type === 'group' ? 420 : spec.type === 'file' ? 400 : 250,
      height: spec.type === 'group' ? 320 : spec.type === 'file' ? 240 : 60,
      text: op.type === 'text' ? '' : undefined,
      label: op.type === 'group' ? '分组' : undefined,
      file: op.file
    };
    state.data.nodes.push(ghost);
    paint(state);
    select(state, ghost.id);
    flushNow(state, [op]);
  }

  function removeNode(state, id) {
    state.data.nodes = state.data.nodes.filter(function (node) {
      return node.id !== id;
    });
    state.data.edges = state.data.edges.filter(function (edge) {
      return edge.fromNode !== id && edge.toNode !== id;
    });
    select(state, '');
    paint(state);
    flushNow(state, [{ op: 'delete_node', id: id }]);
  }

  function removeEdge(state, id) {
    state.data.edges = state.data.edges.filter(function (edge) {
      return edge.id !== id;
    });
    repaintEdges(state);
    flushNow(state, [{ op: 'delete_edge', id: id }]);
  }

  /** 立即送一批（用于"增删"这类一次性动作，不必等防抖）。 */
  function flushNow(state, ops) {
    state.pending = ops;
    flush(state);
  }

  // ------------------------------------------------------------------ 行内编辑

  function startEdit(state, node) {
    state.editing = node.id;
    var el = state.layer.querySelector('.ncanvas__node[data-id="' + node.id + '"]');
    if (!el) return;
    var area = h('textarea.ncanvas__editor', { value: node.text || '' });
    var text = el.querySelector('.ncanvas__text');
    if (text) text.style.visibility = 'hidden';
    el.appendChild(area);
    area.focus();
    area.setSelectionRange(area.value.length, area.value.length);
    area.addEventListener('keydown', function (ev) {
      ev.stopPropagation();
      if (ev.key === 'Escape') {
        ev.preventDefault();
        finishEdit(state, false);
      }
    });
    area.addEventListener('blur', function () {
      finishEdit(state, true);
    });
  }

  function finishEdit(state, save) {
    var id = state.editing;
    state.editing = '';
    if (!id) return;
    var el = state.layer.querySelector('.ncanvas__node[data-id="' + id + '"]');
    var area = el && el.querySelector('.ncanvas__editor');
    var node = find(state, id);
    if (!area || !node) return;
    var next = area.value;
    area.remove();
    if (save === false || next === (node.text || '')) {
      paint(state);
      return;
    }
    node.text = next;
    queue(state, { op: 'update_node', id: id, text: next });
    paint(state);
  }

  function onKey(state, ev) {
    var tag = (ev.target && ev.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA') return;
    if ((ev.key === 'Delete' || ev.key === 'Backspace') && state.selected) {
      ev.preventDefault();
      var node = find(state, state.selected);
      if (!node) return;
      ui.confirm('删掉「' + (node.text || node.label || node.file || '这张卡片').slice(0, 20) + '」？', {
        okLabel: '删掉'
      }).then(function (yes) {
        if (yes) removeNode(state, node.id);
      });
      return;
    }
    if (ev.key === 'Enter' && state.selected) {
      var picked = find(state, state.selected);
      if (picked && picked.type === 'text') {
        ev.preventDefault();
        startEdit(state, picked);
      }
      return;
    }
    if ((ev.metaKey || ev.ctrlKey) && (ev.key === '0' || ev.key === '=' || ev.key === '-')) {
      ev.preventDefault();
      if (ev.key === '0') fit(state);
      else zoomBy(state, ev.key === '=' ? 1.2 : 1 / 1.2);
    }
  }

  function qs(params) {
    return Object.keys(params)
      .map(function (key) {
        return encodeURIComponent(key) + '=' + encodeURIComponent(params[key]);
      })
      .join('&');
  }

  function nowHM() {
    var now = new Date();
    var pad = function (value) {
      return (value < 10 ? '0' : '') + value;
    };
    return pad(now.getHours()) + ':' + pad(now.getMinutes());
  }

  QF.canvas = { mount: mount, unmount: unmount, flush: flush };
})();
