/* 知识图谱视图（Obsidian 风格的一张力导向图）。
 *
 * 三件事和站点其它页面保持一致：
 *  1. 外观 —— 颜色一律从 CSS 变量读（浅色/深色、切主题即时跟着变），不写死色值；
 *  2. 数据 —— 在线时请求 `/api/graph`（实时），离线（双击 HTML / 静态服务）退回
 *     `graph.json` 快照，所以这条离心底线还在；
 *  3. 结构 —— 只挂自己的 DOM，不碰别人的状态（顶栏/状态栏由 shell 与 boot.js 管）。
 *
 * 力导向是手写的：斥力走均匀网格分桶（1565 个节点、5401 条边，O(n²) 会明显掉帧），
 * 弹簧按关系类型给不同自然长度（语义关系紧、结构边松），图才有层次。
 */
(function () {
  'use strict';

  var canvas = document.getElementById('graph-canvas');
  if (!canvas) return;

  // 运行时把工具、状态、题库都挂在 window.QF 下 —— 取法必须和错题本一致
  // （它开头就是 `var ui = QF.ui`）。之前写成 window.ui / 裸 ui 都取不到，
  // 于是顶栏接线被整段跳过：导航空、主题与设置点不动。
  var QF = window.QF || {};
  var ui = QF.ui;
  var store = QF.store;

  var stage = document.getElementById('graph-stage');
  var ctx = canvas.getContext('2d');
  var el = {
    count: document.getElementById('graph-count'),
    boot: document.getElementById('graph-boot'),
    tip: document.getElementById('graph-tip'),
    legend: document.getElementById('graph-legend'),
    nodeChips: document.getElementById('graph-nodechips'),
    edgeChips: document.getElementById('graph-edgechips'),
    search: document.getElementById('graph-search'),
    relayout: document.getElementById('graph-relayout'),
    panel: document.getElementById('graph-panel'),
    panelTitle: document.getElementById('graph-panel-title'),
    panelKey: document.getElementById('graph-panel-key'),
    panelTags: document.getElementById('graph-panel-tags'),
    panelDef: document.getElementById('graph-panel-def'),
    panelFacts: document.getElementById('graph-panel-facts'),
    panelEdges: document.getElementById('graph-panel-edges'),
    panelFocus: document.getElementById('graph-panel-focus'),
    panelReset: document.getElementById('graph-panel-reset'),
    panelClose: document.getElementById('graph-panel-close'),
  };

  var SEMANTIC = ['requires', 'part_of', 'contrast_with', 'implements'];
  var EDGE_LABEL = {
    requires: '前置', part_of: '组成', contrast_with: '易混', implements: '实现',
    co_occurs: '共现', belongs_to: '属于考纲', appears_in: '出现于', child_of: '考纲层级',
  };
  var REST = { requires: 70, part_of: 62, contrast_with: 74, implements: 66, co_occurs: 130 };
  // 顶层主题的配色：等距色相，明度挑在深/浅两套主题下都读得清的位置
  var PALETTE = ['#2DD4BF', '#FBBF24', '#60A5FA', '#F472B6', '#A78BFA', '#34D399',
                 '#FB923C', '#38BDF8', '#E879F9', '#4ADE80', '#F87171', '#818CF8'];
  var TYPE_COLOR = { concept: '#2DD4BF', topic: '#A78BFA', material: '#34D399' };
  var EDGE_COLOR = {
    requires: '#FBBF24', part_of: '#34D399', contrast_with: '#F87171', implements: '#38BDF8',
    co_occurs: '#64748B', belongs_to: '#64748B', appears_in: '#64748B', child_of: '#64748B',
  };

  var state = {
    nodeTypes: { concept: true, topic: true, material: false },
    edgeTypes: { requires: true, part_of: true, contrast_with: true, implements: true,
                 co_occurs: false, belongs_to: true, appears_in: false, child_of: true },
    showMirrors: false,
    search: '',
    focus: null,
    hover: null,
  };

  var data = null;
  var parentOf = new Map();     // 考纲：子 key → 父 key
  var adj = new Map();          // 邻域索引（含反向）
  var colorOf = new Map();      // 概念/考纲 → 颜色（按顶层主题）
  var sim = { nodes: [], index: new Map(), edges: [], alpha: 0 };
  var visibleNodes = [];
  var visibleEdges = [];
  var view = { x: 0, y: 0, k: 1 };
  var W = 0, H = 0, DPR = Math.min(window.devicePixelRatio || 1, 2);
  var dragging = null, panning = null;

  /* ------------------------------------------------------------ 小工具 */
  function h(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function hash(s) {
    var hh = 0;
    for (var i = 0; i < String(s).length; i++) hh = (hh * 31 + String(s).charCodeAt(i)) | 0;
    return Math.abs(hh);
  }

  function css(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v && v.trim()) || fallback;
  }

  function tokens() {
    return { fg: css('--fg', '#E6EDF3'), fg2: css('--fg2', '#9AA7B4'),
             line: css('--line', '#2A3846') };
  }
  var TOK = tokens();
  // 切主题时重取令牌：图是画在 canvas 上的，不重取就会留在一套配色里
  new MutationObserver(function () { TOK = tokens(); }).observe(document.documentElement,
    { attributes: true, attributeFilter: ['data-theme'] });

  function rootTopic(key) {
    var cur = key, guard = 0;
    while (parentOf.has(cur) && guard++ < 12) cur = parentOf.get(cur);
    return cur;
  }

  /* ------------------------------------------------------------ 载入数据 */
  function loadData() {
    return fetch('/api/graph', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; })
      .then(function (live) {
        if (live && live.nodes && live.nodes.length) return live;
        // 离线或接口不可用：用构建时写下的快照（同一份拼图逻辑产出的）
        return fetch('graph.json', { cache: 'no-store' })
          .then(function (r) { return r.ok ? r.json() : null; })
          .catch(function () { return null; });
      });
  }

  function prepare() {
    data.nodes.forEach(function (n) {
      if (n.type === 'topic' && n.mirror) n.__mirror = true;
    });
    data.links.forEach(function (l) {
      if (l.type === 'child_of') parentOf.set(l.target.slice(2), l.source.slice(2));
      if (!adj.has(l.source)) adj.set(l.source, []);
      adj.get(l.source).push(l.target);
      if (!adj.has(l.target)) adj.set(l.target, []);
      adj.get(l.target).push(l.source);
    });
    data.nodes.forEach(function (n) {
      var group = n.type === 'topic' ? rootTopic(n.key)
        : (n.type === 'concept' ? (n.topic ? rootTopic(n.topic) : '') : '');
      if (group) colorOf.set(n.id, PALETTE[hash(group) % PALETTE.length]);
    });
  }

  /* ------------------------------------------------------------ 可见子图 */
  function rebuild(relayout) {
    var keep = new Set();
    if (state.focus) { keep.add(state.focus); (adj.get(state.focus) || []).forEach(function (id) { keep.add(id); }); }

    var nodes = data.nodes.filter(function (n) {
      if (!state.nodeTypes[n.type]) return false;
      if (n.__mirror && !state.showMirrors) return false;
      if (state.focus && n.type !== 'topic' && !keep.has(n.id) && n.topic !== state.focus) return false;
      return true;
    });
    var ids = new Set(nodes.map(function (n) { return n.id; }));
    var links = data.links.filter(function (l) {
      return state.edgeTypes[l.type] && ids.has(l.source) && ids.has(l.target);
    });

    var rebuilt = relayout || sim.nodes.length !== nodes.length;
    if (rebuilt) {
      var index = new Map();
      nodes.forEach(function (n, i) { index.set(n.id, i); });
      layoutInitial(nodes);
      sim = {
        nodes: nodes, index: index, alpha: 1,
        edges: links.map(function (l) {
          return { s: index.get(l.source), t: index.get(l.target), type: l.type, weight: l.weight || 1 };
        }).filter(function (e) { return e.s !== undefined && e.t !== undefined; }),
      };
    } else {
      // 只换了过滤器：位置留着（点一下开关就"跳一下"是不舒服的）
      sim.edges = links.map(function (l) {
        return { s: sim.index.get(l.source), t: sim.index.get(l.target), type: l.type, weight: l.weight || 1 };
      }).filter(function (e) { return e.s !== undefined && e.t !== undefined; });
      sim.alpha = Math.max(sim.alpha, 0.5);
    }
    nodes.forEach(function (n) {
      n.__color = colorOf.get(n.id) || TYPE_COLOR[n.type] || TOK.fg2;
      n.__r = n.type === 'concept'
        ? 3.5 + Math.min(Math.sqrt(1 + (n.points || 1) * 2), 7)
        : (n.type === 'topic' ? 4 + Math.max(0, 3 - (n.depth || 0)) : 5);
    });
    visibleNodes = nodes;
    visibleEdges = links;
    el.count.textContent = nodes.length + ' 节点 · ' + links.length + ' 边 · 语义 ' +
      links.filter(function (l) { return SEMANTIC.indexOf(l.type) >= 0; }).length;
  }

  function layoutInitial(nodes) {
    // 按顶层主题分簇摆开：开局就糊成一团的话，前几秒完全看不出结构
    var groups = new Map();
    nodes.forEach(function (n) {
      var g = n.type === 'topic' ? rootTopic(n.key)
        : (n.type === 'concept' ? (n.topic ? rootTopic(n.topic) : 'misc') : 'material');
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(n);
    });
    var gi = 0;
    var total = Math.max(groups.size, 1);
    groups.forEach(function (members) {
      var angle = (gi / total) * Math.PI * 2;
      var cx = Math.cos(angle) * 320, cy = Math.sin(angle) * 240;
      members.forEach(function (n, j) {
        n.x = cx + Math.cos(j * 2.4) * 46 + j * 1.5;
        n.y = cy + Math.sin(j * 2.4) * 46;
        n.vx = 0; n.vy = 0;
      });
      gi++;
    });
  }

  /* ------------------------------------------------------------ 力导向 */
  var CELL = 46;
  function repel() {
    var nodes = sim.nodes;
    if (!nodes.length) return;
    var grid = new Map();
    for (var i = 0; i < nodes.length; i++) {
      var key = (nodes[i].x / CELL | 0) + ',' + (nodes[i].y / CELL | 0);
      if (!grid.has(key)) grid.set(key, []);
      grid.get(key).push(i);
    }
    var REP = 900;
    for (var a = 0; a < nodes.length; a++) {
      var na = nodes[a];
      var gx = na.x / CELL | 0, gy = na.y / CELL | 0;
      for (var dx = -1; dx <= 1; dx++) {
        for (var dy = -1; dy <= 1; dy++) {
          var bucket = grid.get((gx + dx) + ',' + (gy + dy));
          if (!bucket) continue;
          for (var k = 0; k < bucket.length; k++) {
            var b = bucket[k];
            if (b <= a) continue;
            var nb = nodes[b];
            var ex = na.x - nb.x, ey = na.y - nb.y;
            var d2 = ex * ex + ey * ey;
            if (d2 < 1) { ex = Math.random() - 0.5; ey = Math.random() - 0.5; d2 = 0.5; }
            if (d2 > 60000) continue;
            var f = REP / d2, d = Math.sqrt(d2);
            na.vx += ex / d * f; na.vy += ey / d * f;
            nb.vx -= ex / d * f; nb.vy -= ey / d * f;
          }
        }
      }
    }
  }

  function step() {
    var nodes = sim.nodes;
    if (!nodes.length) return;
    repel();
    for (var i = 0; i < sim.edges.length; i++) {
      var e = sim.edges[i];
      var a = nodes[e.s], b = nodes[e.t];
      if (!a || !b) continue;
      var rest = REST[e.type] || (e.type === 'child_of' ? 110 : e.type === 'belongs_to' ? 90 : 150);
      var dx = b.x - a.x, dy = b.y - a.y;
      var d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      var f = (d - rest) * 0.045;
      a.vx += dx / d * f; a.vy += dy / d * f;
      b.vx -= dx / d * f; b.vy -= dy / d * f;
    }
    for (var j = 0; j < nodes.length; j++) {
      var n = nodes[j];
      if (n.fixed) { n.vx = n.vy = 0; continue; }
      n.vx -= n.x * 0.0022; n.vy -= n.y * 0.0022;
      n.vx *= 0.82; n.vy *= 0.82;
      n.x += Math.max(-12, Math.min(12, n.vx)) * sim.alpha;
      n.y += Math.max(-12, Math.min(12, n.vy)) * sim.alpha;
    }
    sim.alpha = Math.max(0.02, sim.alpha * 0.994);
  }

  /* ------------------------------------------------------------ 绘制 */
  function hotSet() {
    var set = new Set();
    var base = state.hover || state.focus;
    set.add(base);
    if (base) (adj.get(base) || []).forEach(function (id) { set.add(id); });
    set.delete(undefined);
    return set;
  }

  function draw() {
    ctx.clearRect(0, 0, W, H);
    ctx.save();
    ctx.translate(W / 2 + view.x, H / 2 + view.y);
    ctx.scale(view.k, view.k);

    var hot = hotSet();
    var dim = hot.size > 0;

    for (var i = 0; i < visibleEdges.length; i++) {
      var l = visibleEdges[i];
      var a = sim.nodes[sim.index.get(l.source)], b = sim.nodes[sim.index.get(l.target)];
      if (!a || !b) continue;
      var isHot = dim && hot.has(l.source) && hot.has(l.target);
      ctx.strokeStyle = EDGE_COLOR[l.type] || TOK.line;
      ctx.globalAlpha = isHot ? 0.95 : (dim ? 0.06 : (SEMANTIC.indexOf(l.type) >= 0 ? 0.5 : 0.16));
      ctx.lineWidth = Math.min(2.4, 0.7 + Math.log(1 + (l.weight || 1)) * 0.6) / view.k + 0.4;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    }
    ctx.globalAlpha = 1;

    for (var j = 0; j < visibleNodes.length; j++) {
      var n = visibleNodes[j];
      ctx.globalAlpha = (!dim || hot.has(n.id)) ? 1 : 0.16;
      ctx.fillStyle = n.__color;
      ctx.beginPath();
      ctx.arc(n.x, n.y, n.__r, 0, Math.PI * 2);
      ctx.fill();
      if (n.type === 'topic') {
        ctx.strokeStyle = css('--bg', '#0B1016');
        ctx.lineWidth = 1.4;
        ctx.stroke();
      }
    }
    ctx.globalAlpha = 1;

    var q = state.search.toLowerCase();
    ctx.font = '12px ' + css('--font-sans', 'system-ui');
    ctx.textAlign = 'center';
    for (var m = 0; m < visibleNodes.length; m++) {
      var node = visibleNodes[m];
      var hit = q && ((node.label || '').toLowerCase().indexOf(q) >= 0
        || (node.key || '').toLowerCase().indexOf(q) >= 0);
      var big = (node.points || 0) >= 4 || (node.type === 'topic' && (node.depth || 0) <= 1);
      var isHotNode = hot.has(node.id);
      if (!(big || hit || isHotNode)) continue;
      if (dim && !isHotNode && !hit) continue;
      ctx.globalAlpha = (isHotNode || hit) ? 1 : 0.72;
      var text = String(node.label || node.key || '').slice(0, 22);
      var w = ctx.measureText(text).width;
      ctx.fillStyle = css('--bg', '#0B1016');
      ctx.globalAlpha *= 0.78;
      ctx.fillRect(node.x - w / 2 - 4, node.y - node.__r - 16, w + 8, 15);
      ctx.globalAlpha = (isHotNode || hit) ? 1 : 0.72;
      ctx.fillStyle = hit ? css('--amber', '#FBBF24') : TOK.fg;
      ctx.fillText(text, node.x, node.y - node.__r - 5);
    }
    ctx.globalAlpha = 1;
    ctx.restore();
  }

  function loop() {
    step();
    draw();
    window.requestAnimationFrame(loop);
  }

  /* ------------------------------------------------------------ 交互 */
  function resize() {
    W = stage.clientWidth;
    H = stage.clientHeight;
    canvas.width = Math.max(1, W * DPR);
    canvas.height = Math.max(1, H * DPR);
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  }
  window.addEventListener('resize', resize);

  function pick(px, py) {
    var x = (px - W / 2 - view.x) / view.k;
    var y = (py - H / 2 - view.y) / view.k;
    var best = null, bestD = 14 / view.k;
    for (var i = 0; i < visibleNodes.length; i++) {
      var n = visibleNodes[i];
      var d = Math.sqrt((n.x - x) * (n.x - x) + (n.y - y) * (n.y - y));
      if (d < Math.max(bestD, n.__r + 4 / view.k)) { best = n; bestD = d; }
    }
    return best;
  }

  canvas.addEventListener('mousemove', function (ev) {
    var x = ev.offsetX, y = ev.offsetY;
    if (dragging) {
      dragging.x = (x - W / 2 - view.x) / view.k;
      dragging.y = (y - H / 2 - view.y) / view.k;
      dragging.fixed = true;
      sim.alpha = Math.max(sim.alpha, 0.5);
      return;
    }
    if (panning) {
      view.x += x - panning.x;
      view.y += y - panning.y;
      panning = { x: x, y: y };
      return;
    }
    var n = pick(x, y);
    if (n) {
      state.hover = n.id;
      el.tip.hidden = false;
      el.tip.style.left = Math.min(x + 14, W - 280) + 'px';
      el.tip.style.top = (y + 14) + 'px';
      el.tip.innerHTML = '';
      var b = h('b', null, n.label || n.key);
      var s = h('span', null, kindInfo(n));
      el.tip.appendChild(b);
      el.tip.appendChild(s);
    } else {
      state.hover = null;
      el.tip.hidden = true;
    }
  });

  canvas.addEventListener('mousedown', function (ev) {
    var n = pick(ev.offsetX, ev.offsetY);
    if (n) { dragging = n; canvas.classList.add('is-dragging'); }
    else panning = { x: ev.offsetX, y: ev.offsetY };
  });

  window.addEventListener('mouseup', function () {
    if (dragging) dragging.fixed = false;
    dragging = null;
    panning = null;
    canvas.classList.remove('is-dragging');
  });

  canvas.addEventListener('click', function (ev) {
    var n = pick(ev.offsetX, ev.offsetY);
    if (n) showPanel(n);
    else hidePanel();
  });

  canvas.addEventListener('wheel', function (ev) {
    ev.preventDefault();
    var k = Math.max(0.15, Math.min(4, view.k * Math.exp(-ev.deltaY * 0.0016)));
    var mx = ev.offsetX - W / 2, my = ev.offsetY - H / 2;
    view.x = mx - (mx - view.x) * (k / view.k);
    view.y = my - (my - view.y) * (k / view.k);
    view.k = k;
  }, { passive: false });

  function kindInfo(n) {
    if (n.type === 'concept') {
      return (n.kind || '') + ' · 出现 ' + (n.points || 0) + ' 次 / ' + (n.materials || 0) +
        ' 份材料 · ' + (n.questions || 0) + ' 题';
    }
    if (n.type === 'topic') return '考纲 · 第 ' + (n.depth || 0) + ' 层' + (n.mirror ? '（点的镜像）' : '');
    return '材料 · ' + (n.subject || '');
  }

  /* ------------------------------------------------------------ 详情面板 */
  function topicPath(key) {
    var nameOf = new Map();
    data.nodes.forEach(function (n) { if (n.type === 'topic') nameOf.set(n.key, n.label); });
    var path = [], cur = key, guard = 0;
    while (cur && guard++ < 12) { path.unshift(nameOf.get(cur) || cur); cur = parentOf.get(cur); }
    return path.join(' / ');
  }

  function showPanel(n) {
    el.panel.hidden = false;
    el.panelTitle.textContent = n.label || n.key;
    el.panelKey.textContent = n.key || '';

    el.panelTags.innerHTML = '';
    var tags = [];
    if (n.type === 'concept') tags.push('概念 · ' + (n.kind || ''));
    else if (n.type === 'topic') tags.push('考纲节点');
    else tags.push('材料');
    if (n.status === 'auto') tags.push('自动归并');
    if (n.status === 'confirmed') tags.push('人工确认');
    if (n.mirror) tags.push('点的镜像叶子');
    tags.forEach(function (t) { el.panelTags.appendChild(h('span', null, t)); });

    el.panelDef.textContent = n.definition || '';

    var facts = [];
    if (n.type === 'concept') {
      facts.push(['出现', (n.points || 0) + ' 次（' + (n.materials || 0) + ' 份材料）']);
      facts.push(['题目', (n.questions || 0) + ' 道']);
      facts.push(['考纲', n.topic ? topicPath(n.topic) : '（待归类）']);
      if ((n.aliases || []).length) facts.push(['同义', n.aliases.join('、')]);
      if (n.confidence != null && n.confidence < 0.8) facts.push(['归并', n.confidence.toFixed(2) + ' · 值得人看一眼']);
      if ((n.questionIds || []).length) facts.push(['题号', n.questionIds.join(' ')]);
    }
    el.panelFacts.innerHTML = '';
    facts.forEach(function (pair) {
      el.panelFacts.appendChild(h('dt', null, pair[0]));
      el.panelFacts.appendChild(h('dd', pair[0] === '题号' ? 'mono' : null, pair[1]));
    });

    var labelOf = new Map();
    data.nodes.forEach(function (x) { labelOf.set(x.id, x.label || x.key); });
    var edges = data.links.filter(function (l) { return l.source === n.id || l.target === n.id; })
      .sort(function (a, b) {
        return (SEMANTIC.indexOf(b.type) >= 0 ? 1 : 0) - (SEMANTIC.indexOf(a.type) >= 0 ? 1 : 0);
      });
    el.panelEdges.innerHTML = '';
    if (!edges.length) {
      el.panelEdges.appendChild(h('div', 'graph-empty', '没有连出去的关系'));
    }
    edges.slice(0, 40).forEach(function (l) {
      var out = l.source === n.id;
      var other = out ? l.target : l.source;
      var arrow = (l.type === 'requires' || l.type === 'part_of' || l.type === 'implements')
        ? (out ? '→' : '←') : '—';
      var box = h('div', 'graph-edge');
      box.style.borderLeftColor = EDGE_COLOR[l.type] || css('--pri', '#2DD4BF');
      var head = h('div');
      head.appendChild(h('b', null, EDGE_LABEL[l.type] || l.type));
      head.appendChild(document.createTextNode(' ' + arrow + ' ' + (labelOf.get(other) || other)));
      box.appendChild(head);
      if (l.why) box.appendChild(h('div', 'why', l.why));
      el.panelEdges.appendChild(box);
    });

    el.panelFocus.onclick = function () { state.focus = n.id; rebuild(true); };
    el.panelReset.onclick = function () { state.focus = null; rebuild(true); hidePanel(); };
  }

  function hidePanel() { el.panel.hidden = true; }

  /* ------------------------------------------------------------ 筛选条 */
  function chip(label, on, color, onClick) {
    var b = h('button', 'graph-chip' + (on ? ' is-on' : ''), label);
    b.type = 'button';
    if (on && color) b.style.background = color;
    b.addEventListener('click', onClick);
    return b;
  }

  function renderChips() {
    var names = { concept: '概念', topic: '考纲', material: '材料' };
    el.nodeChips.innerHTML = '';
    Object.keys(state.nodeTypes).forEach(function (type) {
      el.nodeChips.appendChild(chip(names[type], state.nodeTypes[type], TYPE_COLOR[type], function () {
        state.nodeTypes[type] = !state.nodeTypes[type];
        renderChips();
        rebuild(true);
      }));
    });
    el.nodeChips.appendChild(chip('镜像叶子', state.showMirrors, css('--line-strong', '#3E5163'), function () {
      state.showMirrors = !state.showMirrors;
      renderChips();
      rebuild(false);
    }));

    el.edgeChips.innerHTML = '';
    ['part_of', 'requires', 'contrast_with', 'implements', 'co_occurs'].forEach(function (type) {
      el.edgeChips.appendChild(chip(EDGE_LABEL[type], state.edgeTypes[type], EDGE_COLOR[type], function () {
        state.edgeTypes[type] = !state.edgeTypes[type];
        renderChips();
        rebuild(false);
      }));
    });
  }

  function renderLegend() {
    el.legend.innerHTML = '';
    el.legend.appendChild(h('div', null, '滚轮缩放 · 拖节点 · 点开看关系'));
    el.legend.appendChild(h('div', null, '颜色＝顶层主题（同色＝同一块知识）；大小＝出现次数'));
    var line = h('div');
    SEMANTIC.forEach(function (t) {
      var dot = h('i');
      dot.style.background = EDGE_COLOR[t];
      line.appendChild(dot);
      line.appendChild(document.createTextNode(EDGE_LABEL[t] + '  '));
    });
    el.legend.appendChild(line);
  }

  /* ------------------------------------------------------------ 顶栏接线
   *
   * 顶栏是这个站的外壳，但**接线是每个页面各写一份**的（刷题页在 app.js、
   * 错题本在 wrongbook.js 里各接一遍）。图谱页起初漏了这一段，症状就是：
   * 左上角导航整片消失、右上角切主题没反应、设置点不开 —— 按钮都在，
   * 只是没人给它们挂事件。这里补齐，行为与错题本一致。
   */
  function wireShell() {
    // 全部包在 try 里：这一段只负责"外壳好用"，一旦它在早期抛错，
    // 整页（包括下面的图谱）都会跟着不动 —— 实测就是这么崩的：
    // 导航被清空后没有回填，主题与设置的事件也没挂上。
    try {
      var nav = document.getElementById('mainnav');
      // 注意：`ui` / `store` 是运行时脚本里的**裸全局**（错题本、刷题页都这么用），
      // 不能写成 window.ui —— 那样取不到，于是下面整段被跳过。
      if (nav && typeof ui !== 'undefined' && ui) {
        ui.clear(nav);
        addNav(nav, 'quiz.html', 'play', '刷题', false);
        addNav(nav, 'wrongbook.html', 'book', '错题本', false);
        addNav(nav, null, null, '知识图谱', true);
      }
    } catch (err) {
      if (window.console) console.warn('顶栏导航接线失败', err);
    }

    try {
      var themeBtn = document.getElementById('btn-theme');
      if (themeBtn && typeof ui !== 'undefined' && ui && ui.theme) {
        themeBtn.addEventListener('click', function () {
          ui.theme.toggle();
          if (typeof store !== 'undefined' && store) store.saveSettings({ theme: ui.theme.current() });
        });
      }
      var settingsBtn = document.getElementById('btn-settings');
      if (settingsBtn) {
        settingsBtn.addEventListener('click', function () {
          window.location.href = 'quiz.html#settings';
        });
      }
    } catch (err) {
      if (window.console) console.warn('顶栏动作接线失败', err);
    }
  }

  function addNav(nav, href, icon, label, active) {
    var node = document.createElement(href ? 'a' : 'span');
    node.className = 'nav-item' + (active ? ' is-active' : '');
    if (href) node.href = href;
    if (icon && typeof ui !== 'undefined' && ui.icon) node.innerHTML = ui.icon(icon, 15);
    var text = document.createElement('span');
    text.textContent = label;
    node.appendChild(text);
    nav.appendChild(node);
  }

  /* ------------------------------------------------------------ 启动 */
  function boot() {
    wireShell();
    loadData().then(function (d) {
      if (el.boot) el.boot.hidden = true;
      if (!d || !d.nodes || !d.nodes.length) {
        el.count.textContent = '没有图谱数据';
        el.legend.textContent = '服务端没有返回图谱数据，仓库里也没有 graph.json 快照。';
        return;
      }
      data = d;
      prepare();
      renderChips();
      renderLegend();
      resize();
      rebuild(true);
      loop();

      el.search.addEventListener('input', function () { state.search = this.value.trim(); });
      el.relayout.addEventListener('click', function () {
        sim.nodes.forEach(function (n) { n.fixed = false; });
        rebuild(true);
      });
      el.panelClose.addEventListener('click', hidePanel);
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
