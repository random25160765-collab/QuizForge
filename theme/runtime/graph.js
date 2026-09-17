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
    hud: document.getElementById('graph-hud'),
    hudHead: document.getElementById('graph-hud-head'),
    hudToggle: document.getElementById('graph-hud-toggle'),
    params: document.getElementById('graph-params'),
    paramsReset: document.getElementById('graph-params-reset'),
    subjectWrap: document.getElementById('graph-subject'),
    subjectValue: document.getElementById('graph-subject-value'),
    subjectMenu: document.getElementById('graph-subject-menu'),
    subjectBtn: document.getElementById('graph-subject-btn'),
    hudBody: document.querySelector('[data-hud-body]'),
    grip: document.getElementById('graph-hud-grip'),
  };

  var SEMANTIC = ['requires', 'part_of', 'contrast_with', 'implements'];
  var EDGE_LABEL = {
    requires: '前置', part_of: '组成', contrast_with: '易混', implements: '实现',
    co_occurs: '共现', belongs_to: '属于考纲', appears_in: '出现于', child_of: '考纲层级',
  };
  var REST = { requires: 70, part_of: 62, contrast_with: 74, implements: 66, co_occurs: 130 };

  /* ---------------------------------------------------------- 力导向参数
   * 这六个数原来写死在 step() / draw() / rebuild() 里。现在提到左栏：
   * 可调、即时生效、记在设置里（下次打开还是这一套）。
   * 默认值就是原来那几个数 —— 什么都不动 = 旧行为。
   * 滑块上给「读得出来」的值，作用到物理量时再换算（见 linkForce / centerForce），
   * 否则滑杆上会写着 0.0022 这种没法调的数。
   */
  var PARAM_DEFS = [
    { key: 'nodeSize', name: '节点大小', min: 0.5, max: 2, step: 0.05, def: 1, unit: '×' },
    { key: 'linkWidth', name: '连线粗细', min: 0.3, max: 2.5, step: 0.05, def: 1, unit: '×' },
    { key: 'distance', name: '连线距离', min: 0.5, max: 2, step: 0.05, def: 1, unit: '×' },
    { key: 'force', name: '连线弹力', min: 0.5, max: 15, step: 0.5, def: 4.5, unit: '' },
    { key: 'repel', name: '节点斥力', min: 100, max: 4000, step: 50, def: 900, unit: '' },
    { key: 'center', name: '向心力', min: 0, max: 8, step: 0.2, def: 2.2, unit: '' },
  ];
  var P = {};
  PARAM_DEFS.forEach(function (d) { P[d.key] = d.def; });

  function loadParams() {
    var saved = (store && store.settings && store.settings().graph) || {};
    PARAM_DEFS.forEach(function (d) {
      if (typeof saved[d.key] === 'number' && isFinite(saved[d.key])) P[d.key] = saved[d.key];
    });
  }

  function saveParams() {
    if (!store || !store.saveSettings) return;
    var patch = {};
    PARAM_DEFS.forEach(function (d) { patch[d.key] = P[d.key]; });
    store.saveSettings({ graph: patch });
  }

  // 弹力与向心力：滑块上的数比物理量小三个数量级
  function linkForce() { return P.force * 0.01; }
  function centerForce() { return P.center * 0.001; }

  // 参数一改就重新加热：否则布局已经收敛（alpha 趋近 0），推了滑杆没反应
  function reheat() { sim.alpha = Math.max(sim.alpha, 0.6); }
  // 配色：这一页只让两种彩色承载语义 ——
  //   青（--pri）＝语义关系（前置/组成/实现），琥珀（--amber）＝易混；
  //   其余关系与所有节点一律中性灰，靠位置、大小、明暗分层次。
  // 上一版把十二个色相按顶层主题铺满画布：上千个点叠在一起时，
  // 读出来的是噪声不是结构 —— 分类被当成了装饰。
  var EDGE_HUE = {
    requires: 'pri', part_of: 'pri', implements: 'pri',
    contrast_with: 'warn',
    co_occurs: 'line', belongs_to: 'line', appears_in: 'line', child_of: 'line',
  };
  // 易混有七千多条、结构边近万条：它们的粗细不是「要不要看得见」，而是「别糊成一片」
  var EDGE_ALPHA = {
    requires: 0.34, part_of: 0.24, implements: 0.22,
    contrast_with: 0.08, co_occurs: 0.08, belongs_to: 0.10, appears_in: 0.08, child_of: 0.10,
  };
  // 节点只留三档明暗：考纲最亮（骨架）、概念中灰（主体）、材料最暗（出处）
  var TYPE_TONE = { concept: 'fg2', topic: 'fg', material: 'fg3' };

  var state = {
    nodeTypes: { concept: true, topic: true, material: false },
    // 易混默认关：它是七千多条（占可见边八成）、且由词面匹配产出、精度偏松的一批，
    // 打开就把整张图压成一团；要看的时候点一下胶囊即可。
    edgeTypes: { requires: true, part_of: true, contrast_with: false, implements: true,
                 co_occurs: false, belongs_to: true, appears_in: false, child_of: true },
    showMirrors: false,
    search: '',
    subject: null,                // null = 全部学科；否则是顶层主题的 key
    focus: null,
    hover: null,
  };

  var data = null;
  var subjects = [];            // [{ key, name, count }]，按概念数排序
  var parentOf = new Map();     // 考纲：子 key → 父 key
  var adj = new Map();          // 邻域索引（含反向）
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

  function css(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v && v.trim()) || fallback;
  }

  // 画布上的颜色全部从站点的设计令牌里取 —— 这页不拥有自己的调色板，
  // 也就不会出现「切了主题还有一块颜色停在旧主题里」的情况。
  function tokens() {
    return {
      fg: css('--fg', '#E6EDF3'), fg2: css('--fg2', '#9AA7B4'), fg3: css('--fg3', '#75838F'),
      line: css('--line', '#2A3846'), lineStrong: css('--line-strong', '#3E5163'),
      pri: css('--pri', '#2DD4BF'), warn: css('--amber', '#FBBF24'), bg: css('--bg', '#0B1016'),
    };
  }
  var TOK = tokens();

  function edgeColor(type) {
    var hue = EDGE_HUE[type];
    return hue === 'pri' ? TOK.pri : (hue === 'warn' ? TOK.warn : TOK.line);
  }
  // 切主题时重取令牌：图是画在 canvas 上的，不重取就会留在一套配色里
  new MutationObserver(function () { TOK = tokens(); }).observe(document.documentElement,
    { attributes: true, attributeFilter: ['data-theme'] });

  function rootTopic(key) {
    var cur = key, guard = 0;
    while (parentOf.has(cur) && guard++ < 12) cur = parentOf.get(cur);
    return cur;
  }

  /* ------------------------------------------------------------ 载入数据 */
  /**
   * 取图谱数据 —— 只有 `/api/graph` 一个来源。
   *
   * 早先还会退回构建时写下的 `graph.json` 快照（那是离线单文件形态的降级）。
   * 离线形态淘汰后这条路没有生产者：图与页面由同一个服务提供，
   * 服务不在了页面本身也打不开 —— 留着只会掩盖真正的失败。
   */
  function loadData() {
    return fetch('/api/graph', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
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
    // 节点不再按主题着色：三档中性明暗 + 位置分簇，已经足够读出结构

    // 枢纽剪枝：全图最大度数 1018（中位数只有 9），少数几个枢纽的边叠在一起
    // 会糊成一块实心色团 —— 那不是配色问题，是线条数量问题，换什么颜色都救不了。
    // 规则：每个点最多画「权重最高的 24 条」，任何一条边只要进了某一端的前 24 就保留，
    // 所以没有点会变成孤岛；悬停/选中时不受此限，那一圈照旧全画。
    var FAN_CAP = 24;
    var byNode = new Map();
    data.links.forEach(function (l) {
      [l.source, l.target].forEach(function (id) {
        if (!byNode.has(id)) byNode.set(id, []);
        byNode.get(id).push(l);
      });
    });
    var keepEdge = new Set();
    byNode.forEach(function (list) {
      list.sort(function (a, b) { return (b.weight || 1) - (a.weight || 1); });
      for (var i = 0; i < list.length && i < FAN_CAP; i++) keepEdge.add(list[i]);
    });
    data.links.forEach(function (l) { l.__thin = !keepEdge.has(l); });

    // 主题清单不从这里推导：图谱里的考纲节点含有材料侧的镜像主题，
    // 键会撞车（实测按 key 建父表会得到 545 个假根）。真正的层级在题库那边，
    // 见 buildSubjects()。
  }

  /* ------------------------------------------------------------ 可见子图 */
  function rebuild(relayout) {
    var keep = new Set();
    if (state.focus) { keep.add(state.focus); (adj.get(state.focus) || []).forEach(function (id) { keep.add(id); }); }

    var nodes = data.nodes.filter(function (n) {
      if (!state.nodeTypes[n.type]) return false;
      if (n.__mirror && !state.showMirrors) return false;
      if (state.focus && n.type !== 'topic' && !keep.has(n.id) && n.topic !== state.focus) return false;
      // 分主题：只留落在这个主题子树里的节点。
      // 判据是「节点的 key（概念的 topic）在不在子树里」，用的是题库那棵树。
      if (state.subject) {
        var keys = subjectSet(state.subject);
        var mine = n.type === 'topic' ? keys[n.key] : (n.type === 'concept' ? keys[n.topic] : false);
        if (!mine) return false;
      }
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
      n.__tone = TYPE_TONE[n.type] || 'fg2';
      n.__r = radiusOf(n);
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
    var REP = P.repel;
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
      var rest = (REST[e.type] || (e.type === 'child_of' ? 110 : e.type === 'belongs_to' ? 90 : 150)) * P.distance;
      var dx = b.x - a.x, dy = b.y - a.y;
      var d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      var f = (d - rest) * linkForce();
      a.vx += dx / d * f; a.vy += dy / d * f;
      b.vx -= dx / d * f; b.vy -= dy / d * f;
    }
    for (var j = 0; j < nodes.length; j++) {
      var n = nodes[j];
      if (n.fixed) { n.vx = n.vy = 0; continue; }
      n.vx -= n.x * centerForce(); n.vy -= n.y * centerForce();
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
      if (!isHot && l.__thin) continue;   // 枢纽的边默认不画（见 prepare 里的剪枝）
      ctx.strokeStyle = edgeColor(l.type);
      ctx.globalAlpha = isHot ? 0.95 : (dim ? 0.05 : (EDGE_ALPHA[l.type] || 0.08));
      ctx.lineWidth = (Math.min(2.4, 0.7 + Math.log(1 + (l.weight || 1)) * 0.6) / view.k + 0.4) * P.linkWidth;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    }
    ctx.globalAlpha = 1;

    for (var j = 0; j < visibleNodes.length; j++) {
      var n = visibleNodes[j];
      var isHotNode = hot.has(n.id);
      ctx.globalAlpha = (!dim || isHotNode) ? 1 : 0.14;
      // 高亮时统一换成主色：一屏之内只有被选中的那一圈有彩色
      ctx.fillStyle = (dim && isHotNode) ? TOK.pri : (TOK[n.__tone] || TOK.fg2);
      ctx.beginPath();
      ctx.arc(n.x, n.y, n.__r, 0, Math.PI * 2);
      ctx.fill();
      if (n.type === 'topic') {
        ctx.strokeStyle = TOK.bg;
        ctx.lineWidth = 1.4;
        ctx.stroke();
      }
    }
    ctx.globalAlpha = 1;

    var q = state.search.toLowerCase();
    ctx.font = '12px ' + css('--font-sans', 'system-ui');
    ctx.textAlign = 'center';
    // 悬停到枢纽上时，它的邻居可能成百上千 —— 全给标签就把画布写成了一堵字。
    // 只标注其中最重要的那些（按出现次数），其余靠高亮本身就够读了。
    var labelBudget = null;
    if (dim) {
      labelBudget = new Set();
      visibleNodes.filter(function (n) { return hot.has(n.id); })
        .sort(function (a, b) { return (b.points || 0) - (a.points || 0); })
        .slice(0, 24)
        .forEach(function (n) { labelBudget.add(n.id); });
    }
    for (var m = 0; m < visibleNodes.length; m++) {
      var node = visibleNodes[m];
      var hit = q && ((node.label || '').toLowerCase().indexOf(q) >= 0
        || (node.key || '').toLowerCase().indexOf(q) >= 0);
      var big = (node.points || 0) >= 4 || (node.type === 'topic' && (node.depth || 0) <= 1);
      var isHotNode = hot.has(node.id);
      if (!(big || hit || isHotNode)) continue;
      if (dim && !hit && (!isHotNode || !labelBudget.has(node.id))) continue;
      ctx.globalAlpha = (isHotNode || hit) ? 1 : 0.72;
      var text = String(node.label || node.key || '').slice(0, 22);
      var w = ctx.measureText(text).width;
      ctx.fillStyle = TOK.bg;
      ctx.globalAlpha *= 0.78;
      ctx.fillRect(node.x - w / 2 - 4, node.y - node.__r - 16, w + 8, 15);
      ctx.globalAlpha = (isHotNode || hit) ? 1 : 0.72;
      ctx.fillStyle = hit ? TOK.warn : TOK.fg;
      ctx.fillText(text, node.x, node.y - node.__r - 5);
    }
    ctx.globalAlpha = 1;
    ctx.restore();
  }

  function loop() {
    // 收敛（alpha 归零）之后就不再跑物理：节点不再动，画面自然稳。
    // 仍然逐帧重画 —— 悬停、拖动、翻参数都靠它即时反映，而且一次重画
    // 本来就不贵（这里刻意不引入"脏标记"：漏标一处就会留下残影，
    // 而那种 bug 很难被看出来）。
    if (sim.alpha > 0) step();
    draw();
    window.requestAnimationFrame(loop);
  }

  /* ------------------------------------------------------------ 交互 */
  /* 这一页要铺满：高度不能靠 --topbar-h 去算。
     它是设计值（写着 58px），而顶栏的真实高度会变（实测 64px，且随导航与
     同步提示的有无而变）。差几像素，在文字页上看不出来，在这里就是画布在
     底部溢出一条缝、贴地的图例被切掉一角。所以直接量「顶栏底边到视口底」。 */
  function measureHeight() {
    var bar = document.querySelector('.topbar');
    var top = bar ? Math.round(bar.getBoundingClientRect().bottom) : 0;
    var sb = document.body.dataset.sb === 'on'
      ? parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--statusbar-h')) || 0
      : 0;
    document.documentElement.style.setProperty('--graph-h', Math.max(320, window.innerHeight - top - sb) + 'px');
  }

  function resize() {
    measureHeight();
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
      box.style.borderLeftColor = edgeColor(l.type);
      var head = h('div');
      head.appendChild(h('b', null, EDGE_LABEL[l.type] || l.type));
      head.appendChild(document.createTextNode(' ' + arrow + ' ' + (labelOf.get(other) || other)));
      box.appendChild(head);
      if (l.why) box.appendChild(h('div', 'why', l.why));
      el.panelEdges.appendChild(box);
    });

    // 聚焦/取消聚焦只是**换过滤器**，不该重新布局：`rebuild(true)` 会让全图重新
    // 排一次，那些与选中节点无关的点也跟着动、跟着闪（用户看到的就是这个）。
    // 位置留着，只有该出现的出现、该隐的隐。
    el.panelFocus.onclick = function () {
      state.focus = n.id;
      rebuild(false);
    };
    el.panelReset.onclick = function () {
      state.focus = null;
      rebuild(false);
      hidePanel();
    };
  }

  function hidePanel() { el.panel.hidden = true; }

  /* ------------------------------------------------------------ 筛选条 */
  // 胶囊：选中态用站点既有的那套（主色 12% 底 + 40% 边），
  // 不再往按钮上刷高饱和填充 —— 一排六个实心色块正是这一页最刺眼的地方。
  // 语义颜色改由左侧小圆点承担，信息不丢，音量降下来。
  function chip(label, on, dotColor, onClick) {
    var b = h('button', 'graph-chip' + (on ? ' is-on' : ''), null);
    b.type = 'button';
    if (dotColor) {
      var dot = h('i', 'graph-chip__dot');
      dot.style.background = dotColor;
      b.appendChild(dot);
    }
    b.appendChild(document.createTextNode(label));
    b.addEventListener('click', onClick);
    return b;
  }

  function renderChips() {
    var names = { concept: '概念', topic: '考纲', material: '材料' };
    el.nodeChips.innerHTML = '';
    // 节点是三档中性明暗，没有色相可分，所以胶囊不带圆点
    Object.keys(state.nodeTypes).forEach(function (type) {
      el.nodeChips.appendChild(chip(names[type], state.nodeTypes[type], null, function () {
        state.nodeTypes[type] = !state.nodeTypes[type];
        renderChips();
        rebuild(true);
      }));
    });
    el.nodeChips.appendChild(chip('镜像叶子', state.showMirrors, null, function () {
      state.showMirrors = !state.showMirrors;
      renderChips();
      rebuild(false);
    }));

    el.edgeChips.innerHTML = '';
    ['part_of', 'requires', 'contrast_with', 'implements', 'co_occurs'].forEach(function (type) {
      el.edgeChips.appendChild(chip(EDGE_LABEL[type], state.edgeTypes[type], edgeColor(type), function () {
        state.edgeTypes[type] = !state.edgeTypes[type];
        renderChips();
        rebuild(false);
      }));
    });
  }

  /* 主题选择器：平时只显示当前是哪一个，列表挂在自己下面，
     悬停（或键盘聚焦进来）才展开 —— 见 graph.css 里的 :hover / :focus-within。

     清单与题数都取自题库（QF.data），不从这个图谱自己推：
     图谱里的考纲节点混着材料侧的镜像主题，键会撞车；
     而题库那份是权威的树，还带着每个节点的子树（descendants）。 */
  function buildSubjects() {
    var D = QF.data;
    subjects = [];
    if (!D || !D.subjects || !D.subjects.length) return;
    D.subjects.forEach(function (t) {
      var n = (D.bankStats && D.bankStats.byTopic && D.bankStats.byTopic[t.key]) || 0;
      subjects.push({ key: t.key, name: t.name || t.key, count: typeof n === 'number' ? n : 0 });
    });
    subjects.sort(function (a, b) { return b.count - a.count; });
  }

  // 某个主题的整棵子树（含自己）→ 用于过滤。按 key 缓存。
  var subjectSets = {};
  function subjectSet(key) {
    if (!subjectSets[key]) {
      var set = {};
      var D = QF.data;
      var node = D && D.topicMap ? D.topicMap[key] : null;
      var keys = node && node.descendants ? [key].concat(node.descendants) : [key];
      keys.forEach(function (k) { set[k] = true; });
      subjectSets[key] = set;
    }
    return subjectSets[key];
  }

  function renderSubjects() {
    if (!el.subjectMenu) return;
    ui.clear(el.subjectMenu);

    function item(key, name, count) {
      var on = (state.subject || '') === (key || '');
      var btn = h('button', 'graph-subject__item' + (on ? ' is-on' : ''), null);
      btn.type = 'button';
      btn.setAttribute('role', 'option');
      btn.setAttribute('aria-selected', on ? 'true' : 'false');
      btn.appendChild(h('span', null, name));
      if (typeof count === 'number') btn.appendChild(h('i', null, String(count)));
      btn.addEventListener('click', function () {
        state.subject = key || null;
        renderSubjects();
        // 选完立刻收回：指针还停在原地，但列表不该赖着不走
        if (el.subjectWrap) el.subjectWrap.classList.add('is-picked');
        rebuild(true);   // 主题换了要重排：留着旧坐标会挤成一团
      });
      return btn;
    }

    el.subjectMenu.appendChild(item(null, '全部', null));
    subjects.forEach(function (s) { el.subjectMenu.appendChild(item(s.key, s.name, s.count)); });

    if (el.subjectValue) {
      var cur = subjects.filter(function (s) { return s.key === state.subject; })[0];
      el.subjectValue.textContent = cur ? cur.name : '全部';
    }
  }

  function renderLegend() {
    el.legend.innerHTML = '';
    el.legend.appendChild(h('div', null, '滚轮缩放 · 拖节点 · 点开看关系'));
    el.legend.appendChild(h('div', null, '点的大小＝出现次数；明暗＝考纲 / 概念 / 材料'));
    var line = h('div');
    SEMANTIC.concat(['belongs_to']).forEach(function (t) {
      var dot = h('i');
      dot.style.background = edgeColor(t);
      line.appendChild(dot);
      line.appendChild(document.createTextNode(EDGE_LABEL[t] + '  '));
    });
    el.legend.appendChild(line);
    el.legend.appendChild(h('div', null, '悬停或选中时，只把它那一圈点亮'));
  }

  /* ------------------------------------------------------------ 顶栏接线
   *
   * 顶栏是这个站的外壳，但**接线是每个页面各写一份**的（刷题页在 app.js、
   * 错题本在 wrongbook.js 里各接一遍）。图谱页起初漏了这一段，症状就是：
   * 左上角导航整片消失、右上角切主题没反应、设置点不开 —— 按钮都在，
   * 只是没人给它们挂事件。这里补齐，行为与错题本一致。
   */
  /* ---------------------------------------------------------- 浮动面板 */

  function radiusOf(n) {
    var base = n.type === 'concept'
      ? 3.5 + Math.min(Math.sqrt(1 + (n.points || 1) * 2), 7)
      : (n.type === 'topic' ? 4 + Math.max(0, 3 - (n.depth || 0)) : 5);
    return base * P.nodeSize;
  }

  function fmtParam(def, value) {
    var text = def.step < 1 ? value.toFixed(2) : String(value);
    return def.unit === '×' ? '×' + text : text;
  }

  function buildParams() {
    ui.clear(el.params);
    PARAM_DEFS.forEach(function (def) {
      var num = h('span', 'graph-param__num', fmtParam(def, P[def.key]));
      var top = h('div', 'graph-param__top');
      top.appendChild(h('span', 'graph-param__name', def.name));
      top.appendChild(num);

      var input = document.createElement('input');
      input.type = 'range';
      input.className = 'slider';
      input.min = def.min;
      input.max = def.max;
      input.step = def.step;
      input.value = P[def.key];
      input.setAttribute('aria-label', def.name);
      input.addEventListener('input', function () {
        P[def.key] = parseFloat(input.value);
        num.textContent = fmtParam(def, P[def.key]);
        applyParam(def.key);
      });
      // 松手才写盘：拖拽时每动一格都写一次 localStorage 没有意义
      input.addEventListener('change', saveParams);

      var row = h('div', 'graph-param');
      row.appendChild(top);
      row.appendChild(input);
      el.params.appendChild(row);
    });
  }

  function applyParam(key) {
    if (key === 'nodeSize') {
      visibleNodes.forEach(function (n) { n.__r = radiusOf(n); });
      return;   // 尺寸不影响布局，不用重新加热
    }
    if (key === 'linkWidth') return;   // 下一帧绘制自然生效
    reheat();                          // 其余四个都是力，得让布局重新跑一段
  }

  function hudConf() {
    var g = (store && store.settings && store.settings().graph) || {};
    return g.panel || {};
  }

  function clampHud(x, y) {
    var w = el.hud.offsetWidth, h = el.hud.offsetHeight;
    return {
      x: Math.max(8, Math.min(x, Math.max(8, stage.clientWidth - w - 8))),
      y: Math.max(8, Math.min(y, Math.max(8, stage.clientHeight - h - 8))),
    };
  }

  function placeHud(x, y) {
    var p = clampHud(x, y);
    el.hud.style.left = p.x + 'px';
    el.hud.style.top = p.y + 'px';
    return p;
  }

  // 位置与收展状态都记在设置里：拖到哪儿，下次打开还在哪儿
  function saveHud(patch) {
    if (!store || !store.saveSettings) return;
    var box = {
      x: parseInt(el.hud.style.left, 10) || 0,
      y: parseInt(el.hud.style.top, 10) || 0,
      collapsed: el.hud.classList.contains('is-collapsed'),
    };
    Object.keys(patch || {}).forEach(function (key) { box[key] = patch[key]; });
    store.saveSettings({ graph: { panel: box } });
  }

  function setCollapsed(flag) {
    el.hud.classList.toggle('is-collapsed', flag);
    el.hudToggle.setAttribute('aria-expanded', flag ? 'false' : 'true');
    el.hudToggle.title = flag ? '展开设置' : '收成一个球';
    // 只记状态，不动位置：面板是用户拖出来的，展开后就算探出画布边界，
    // 也应该自然地留在那儿 —— 把它整体往上顶一下反而更突兀。
    saveHud();
  }

  /* 拖动：抓住面板头就拖。松手时如果几乎没动，就当「点了一下」——
     收成球之后这颗球也得能点开，而球身上只有这一条能抓。 */
  function bindHudDrag() {
    var head = el.hudHead;
    var dragging = null;

    head.addEventListener('pointerdown', function (event) {
      // 展开时：只有面板头可拖，里面的控件不抢手势。
      // 收起时：整颗球都是拖拽区（球身就是那个按钮）—— 点开与拖动的区分
      // 交给松手时的位移判断，所以这里不能按「是不是按钮」把自己挡掉。
      var collapsed = el.hud.classList.contains('is-collapsed');
      if (!collapsed && event.target.closest('button, input, a, label')) return;
      dragging = {
        dx: event.clientX - (parseInt(el.hud.style.left, 10) || 0),
        dy: event.clientY - (parseInt(el.hud.style.top, 10) || 0),
        x0: event.clientX,
        y0: event.clientY,
        moved: 0,
      };
      try { head.setPointerCapture(event.pointerId); } catch (err) { /* 不支持就算了 */ }
      event.preventDefault();
    });

    head.addEventListener('pointermove', function (event) {
      if (!dragging) return;
      dragging.moved = Math.max(dragging.moved,
        Math.abs(event.clientX - dragging.x0) + Math.abs(event.clientY - dragging.y0));
      placeHud(event.clientX - dragging.dx, event.clientY - dragging.dy);
    });

    function end(event) {
      if (!dragging) return;
      var moved = dragging.moved > 3;
      dragging = null;
      try { head.releasePointerCapture(event.pointerId); } catch (err) { /* 已释放 */ }
      if (moved) saveHud();
      else if (el.hud.classList.contains('is-collapsed')) setCollapsed(false);
      // 展开态下点标题不做任何事：拖拽区不该兼作开关
    }

    head.addEventListener('pointerup', end);
    head.addEventListener('pointercancel', end);
  }

  /* 高度可调：拖底边那条把手。正文高度记进设置，下次打开还是这个高度；
     上限定在视口的 80%，免得把画布全遮住。 */
  function applyBodyHeight(px) {
    var max = Math.round(window.innerHeight * 0.8);
    el.hudBody.style.height = Math.max(120, Math.min(px, max)) + 'px';
    return el.hudBody.offsetHeight;
  }

  function bindGrip() {
    var grip = el.grip;
    if (!grip) return;
    var dragging = null;

    grip.addEventListener('pointerdown', function (event) {
      dragging = { y: event.clientY, h: el.hudBody.offsetHeight };
      try { grip.setPointerCapture(event.pointerId); } catch (err) { /* 不支持就算了 */ }
      event.preventDefault();
    });

    grip.addEventListener('pointermove', function (event) {
      if (!dragging) return;
      applyBodyHeight(dragging.h + (event.clientY - dragging.y));
    });

    function end(event) {
      if (!dragging) return;
      dragging = null;
      try { grip.releasePointerCapture(event.pointerId); } catch (err) { /* 已释放 */ }
      saveHud({ bodyH: el.hudBody.offsetHeight });
    }

    grip.addEventListener('pointerup', end);
    grip.addEventListener('pointercancel', end);
  }

  function buildHud() {
    loadParams();
    buildParams();

    var conf = hudConf();
    if (conf.collapsed) setCollapsed(true);
    // 默认落在画布中心偏左上：一进来就在眼前，又不压住中间那团最密的知识
    var cx = Math.round(stage.clientWidth / 2 - 120);
    var cy = Math.round(stage.clientHeight / 2 - 110);
    placeHud(typeof conf.x === 'number' ? conf.x : cx, typeof conf.y === 'number' ? conf.y : cy);

    if (el.hudBody) {
      applyBodyHeight(typeof conf.bodyH === 'number' ? conf.bodyH : el.hudBody.offsetHeight);
      bindGrip();
    }

    // 指针离开整块主题选择器之后，才允许下一次悬停重新展开列表
    if (el.subjectWrap) {
      el.subjectWrap.addEventListener('mouseleave', function () {
        el.subjectWrap.classList.remove('is-picked');
      });
    }

    if (el.paramsReset) {
      el.paramsReset.addEventListener('click', function () {
        PARAM_DEFS.forEach(function (def) { P[def.key] = def.def; });
        buildParams();
        saveParams();
        visibleNodes.forEach(function (n) { n.__r = radiusOf(n); });
        reheat();
      });
    }

    if (el.hudToggle) {
      el.hudToggle.addEventListener('click', function () {
        setCollapsed(!el.hud.classList.contains('is-collapsed'));
      });
    }

    bindHudDrag();

    // 画布尺寸变了：把面板夹回可见范围（它可能停在原来的右下角）
    window.addEventListener('resize', function () {
      placeHud(parseInt(el.hud.style.left, 10) || 0, parseInt(el.hud.style.top, 10) || 0);
      saveHud();
    });

    // 参数在别处被改过（导入数据会重写设置）时同步回来
    document.addEventListener('qf:data-changed', function () {
      loadParams();
      buildParams();
    });
  }

  function wireShell() {
    // 顶栏（主导航 / 主题 / 设置 / 当前页图标）统一由 shell.js 负责：
    // 这页不再自己拼一份导航，也不再往刷题页跳着开设置。
    // 包在 try 里：外壳的接线一旦早期抛错，会连累下面的图谱也跟着不动。
    try {
      QF.shell.mount({});
    } catch (err) {
      if (window.console) console.warn('顶栏接线失败', err);
    }
  }

  /* ------------------------------------------------------------ 启动 */
  function boot() {
    wireShell();
    buildHud();

    // 顶栏高度会变（同步提示出现/消失、导航项增减），变了就把画布重算一次
    if (window.ResizeObserver) {
      var bar = document.querySelector('.topbar');
      if (bar) new ResizeObserver(resize).observe(bar);
    }
    loadData().then(function (d) {
      if (el.boot) el.boot.hidden = true;
      if (!d || !d.nodes || !d.nodes.length) {
        el.count.textContent = '没有图谱数据';
        el.legend.textContent = '服务端没有返回图谱数据。';
        return;
      }
      data = d;
      prepare();
      buildSubjects();
      renderChips();
      renderSubjects();
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
