/* ===========================================================================
 * chat.js —— 对话（学习前台）
 *
 * 这一页对应的是**最常发生的那件事**：截一段材料问 AI、追问下去、直到弄懂。
 * 所以它刻意不像一个「聊天软件」，而像一个能长出题目与引用的笔记本：
 *
 *   左栏 = 会话（一次学习就是一条线）   主区 = 这一条线的消息
 *
 * 三个与刷题页不同的地方，都是有意的：
 *
 * 1. **消息有 id**。它不是屏幕上的一段字，而是库里的行 —— 所以将来可以在一条
 *    回答下面挂题目卡片、挂材料引用、挂「这一步你没想对」的诊断。
 * 2. **流式**。字是一个一个到的（`api.stream`）。等待十几秒却只看到一个转圈，
 *    与看着它把话说完，是两种完全不同的体验。
 * 3. **停止按钮是真的停止**。它 abort 掉连接 —— 服务端会收到断开，
 *    把已经生成的部分留成 `partial`（而不是丢掉）。
 *
 * 还没做的（刻意留白，见 api/app/routers/chat.py 的说明）：
 * 工具调用、题目卡片、材料引用、从题目页「问 AI」带上下文跳过来（入口留了
 * `?ask=` 与 `?c=`）。
 * ========================================================================= */
(function () {
  'use strict';

  var QF = (window.QF = window.QF || {});
  var ui = QF.ui;
  var api = QF.api;
  var h = ui.h;

  var rootEl = null;
  var asideEl = null;
  var asideHeadEl = null;
  var listEl = null;
  var threadEl = null;
  var noticeEl = null;
  var jumpBtn = null;
  var notesEl = null;
  var inputEl = null;
  var sendBtn = null;
  var hintEl = null;
  var barEl = null;
  var treeEl = null;
  var demoEl = null;
  var clipInput = null;
  var pendingEl = null;

  /** `/api/ai/usage` 的结果：走哪条通道、能不能用（决定提示怎么写） */
  var aiState = null;

  var state = {
    list: [], // 会话列表（服务端给的，含条数与预览）
    current: null, // 当前会话 id
    messages: [], // **整棵树**（一条不落），当前分支是按 parentId 算出来的
    picks: {}, // 用户在某个分叉上选过哪一支：{ 父节点 key: 子消息 id }
    busy: false, // 正在流式
    controller: null, // AbortController：停止按钮用它
    live: null, // 正在长的那条
    query: '', // 搜索框里的字（≥2 字就把左栏换成搜索结果）
    results: [], // 搜索结果
    editing: 0, // 正在就地编辑的那条用户消息（0 = 没有）
    treeOpen: false, // 对话树面板开着没有
    pending: [], // 已上传、还没随消息发出去的附件
    demo: null, // 正在右侧面板里跑的演示（{title, html}）
  };

  var searchTimer = null;

  /* ------------------------------------------------------------ 对话树 */

  /**
   * 对话树：全屏的**圆角矩形连接图**。
   *
   * 早先是一列缩进文字（`├─` 那种）。那玩意儿的毛病不是不好看，是**读不出形状**：
   * 分叉在哪、哪条枝更长、当前站在哪一支上，都得一行行对；节点一多就彻底糊了。
   * 现在换成图：每个节点一个圆角矩形，父子之间画连线，当前分支高亮。
   *
   * 三件刻意的事：
   *
   * * **布局是算出来的，不是抻出来的**（层 = 深度，行 = 同层顺序，tidy tree）。
   *   没有力导向、没有物理，所以它**不会抖**，也不会因为点一下就把整张图重排。
   * * **形态可调**：横排/竖排、疏密、适应窗口。会话树的形状因人而异
   *   （有人爱看时间往右流，有人爱看自上而下），让用户自己定。
   * * 点任意一个节点 = 切到那条分支上（`revealMessage`），与原来一致。
   */

  var SVG_NS = 'http://www.w3.org/2000/svg';

  var TREE = {
    dir: 'h', // h：时间往右；v：自上而下
    gapX: 130, // 层间距
    gapY: 20, // 同层节点间距
    w: 212, // 节点宽
    view: { x: 0, y: 0, k: 1 },
    drag: null,
    bounds: null,
  };

  function sv(tag, attrs, kids) {
    var node = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs || {}).forEach(function (key) {
      if (attrs[key] !== null && attrs[key] !== undefined) node.setAttribute(key, attrs[key]);
    });
    (kids || []).forEach(function (kid) {
      if (kid) node.appendChild(kid);
    });
    return node;
  }

  /** 节点上那几行字：先按字数硬折，超了就把最后一行打省略号。 */
  function treeLines(message) {
    var text = String(message.content || '').replace(/\s+/g, ' ').trim();
    if (!text) {
      var kinds = (message.parts || []).map(function (part) {
        return part.type === 'card' ? '题卡' : part.type === 'file' ? '附件' : part.type === 'demo' ? '演示' : part.type === 'tool_call' ? '工具' : part.type === 'action' ? '凭条' : null;
      }).filter(Boolean);
      text = kinds.length ? '（' + kinds.join(' · ') + '）' : message.status === 'error' ? '（失败）' : '（空）';
    }
    var per = 17;
    var lines = [];
    for (var i = 0; i < text.length && lines.length < 3; i += per) lines.push(text.slice(i, i + per));
    if (text.length > per * 3) lines[2] = lines[2].slice(0, per - 1) + '…';
    return lines.length ? lines : ['（空）'];
  }

  function treeNodeHeight(message) {
    return 30 + treeLines(message).length * 15;
  }

  /** 把整棵树算成 { nodes, edges }（纯几何，不含 DOM）。 */
  function treeGeometry() {
    var byParent = {};
    state.messages.forEach(function (message) {
      var key = keyOf(message.parentId);
      (byParent[key] = byParent[key] || []).push(message);
    });
    Object.keys(byParent).forEach(function (key) {
      byParent[key].sort(function (a, b) {
        return a.id - b.id;
      });
    });
    var kidsOf = function (id) {
      return byParent[keyOf(id)] || [];
    };

    // 同层里每个节点"占多宽"：横向排时节点是横躺的，占位由**高度**决定；
    // 纵向排时节点并排站着，占位由**宽度**决定。用错了就会出现"纵向时两个
    // 根节点挨在一起、文字被邻居压掉"（实测就是这么露出来的）。
    var acrossSize = function (message) {
      return TREE.dir === 'h' ? treeNodeHeight(message) : TREE.w;
    };

    // 第一趟（自底向上）：每个节点"独占"多宽 —— 叶子的大小，或它所有子树的合计
    var band = {};
    function measure(message) {
      var kids = kidsOf(message.id);
      var own = acrossSize(message);
      if (!kids.length) {
        band[message.id] = own;
        return own;
      }
      var span = kids.reduce(function (sum, kid) {
        return sum + measure(kid);
      }, 0);
      span += TREE.gapY * (kids.length - 1);
      band[message.id] = Math.max(own, span);
      return band[message.id];
    }

    var onPath = {};
    activePath().forEach(function (message) {
      onPath[message.id] = true;
    });

    // 第二趟（自顶向下）：在自己那块高度里居中，再把子节点铺在下面
    var nodes = [];
    var edges = [];
    function place(message, depth, top) {
      var height = treeNodeHeight(message);
      var center = top + band[message.id] / 2;
      nodes.push({
        message: message,
        depth: depth,
        lines: treeLines(message),
        h: height,
        across: center, // 同层里的位置（横排时是 y，竖排时是 x）
        onPath: !!onPath[message.id],
        forks: kidsOf(message.id).length,
      });
      var kids = kidsOf(message.id);
      if (!kids.length) return;
      var span = kids.reduce(function (sum, kid) {
        return sum + band[kid.id];
      }, 0);
      span += TREE.gapY * (kids.length - 1);
      var cursor = top + (band[message.id] - span) / 2;
      kids.forEach(function (kid) {
        place(kid, depth + 1, cursor);
        edges.push({ from: message.id, to: kid.id, onPath: onPath[message.id] && onPath[kid.id] });
        cursor += band[kid.id] + TREE.gapY;
      });
    }

    var roots = byParent.root || [];
    var cursor = 0;
    roots.forEach(function (root, index) {
      measure(root); // 必须先量出自己的高度，place 才知道该往下排多深
      place(root, 0, cursor);
      cursor += band[root.id] + TREE.gapY * 2;
      if (index === roots.length - 1) cursor -= TREE.gapY * 2;
    });

    // 坐标：横排时 x = 层 × 步长、y = 同层位置；竖排时两者对调。
    // 纵向时"层间距"要按节点**高度**留（节点是躺着的），所以步长另算一套。
    var step = TREE.dir === 'h' ? TREE.w + TREE.gapX : 96 + TREE.gapX;
    nodes.forEach(function (node) {
      var along = node.depth * step;
      node.x = TREE.dir === 'h' ? along : node.across;
      node.y = TREE.dir === 'h' ? node.across : along;
    });
    var at = {};
    nodes.forEach(function (node) {
      at[node.message.id] = node;
    });
    edges.forEach(function (edge) {
      edge.a = at[edge.from];
      edge.b = at[edge.to];
    });

    return { nodes: nodes, edges: edges.filter(function (edge) {
      return edge.a && edge.b;
    }) };
  }

  function treePath(edge) {
    var a = edge.a;
    var b = edge.b;
    var ah = a.h; // 纵向时从节点底边出发
    if (TREE.dir === 'h') {
      var x1 = a.x + TREE.w;
      var y1 = a.y;
      var x2 = b.x;
      var y2 = b.y;
      var mid = (x2 - x1) / 2;
      return 'M' + x1 + ' ' + y1 + 'C' + (x1 + mid) + ' ' + y1 + ',' + (x2 - mid) + ' ' + y2 + ',' + x2 + ' ' + y2;
    }
    var vy1 = a.y + ah;
    var vx1 = a.x + TREE.w / 2;
    var vy2 = b.y;
    var vx2 = b.x + TREE.w / 2;
    var vmid = (vy2 - vy1) / 2;
    return 'M' + vx1 + ' ' + vy1 + 'C' + vx1 + ' ' + (vy1 + vmid) + ',' + vx2 + ' ' + (vy2 - vmid) + ',' + vx2 + ' ' + vy2;
  }

  /** 主区顶上那个入口：显示这棵树有多大、几处分叉。 */
  function renderBar() {
    if (!barEl) return;
    ui.clear(barEl);

    var counts = {};
    state.messages.forEach(function (message) {
      var key = keyOf(message.parentId);
      counts[key] = (counts[key] || 0) + 1;
    });
    var forks = Object.keys(counts).filter(function (key) {
      return counts[key] > 1;
    }).length;

    barEl.appendChild(
      h(
        'button.chat__treebtn' + (state.treeOpen ? '.is-on' : ''),
        {
          type: 'button',
          onClick: function () {
            state.treeOpen = !state.treeOpen;
            if (state.treeOpen) TREE.view = { x: 0, y: 0, k: 1 };
            renderBar();
            renderTree();
          },
        },
        '对话树 · ' + state.messages.length + ' 个节点' + (forks ? ' · ' + forks + ' 处分叉' : '')
      )
    );
  }

  function renderTree() {
    if (!treeEl) return;
    ui.clear(treeEl);
    if (!state.treeOpen) return;

    var close = function () {
      state.treeOpen = false;
      renderBar();
      renderTree();
    };

    var screen = h('div.chattree__screen', null, treeBar(close), h('div.chattree__canvas'));
    treeEl.appendChild(screen);
    drawTree(screen.querySelector('.chattree__canvas'));
  }

  function treeBar(close) {
    var dirButton = h(
      'button.chattree__btn',
      {
        type: 'button',
        onClick: function () {
          TREE.dir = TREE.dir === 'h' ? 'v' : 'h';
          TREE.dir === 'h' ? (TREE.gapX = 130) : (TREE.gapX = 60);
          dirButton.textContent = TREE.dir === 'h' ? '时间：横向' : '时间：纵向';
          renderTree();
        },
      },
      TREE.dir === 'h' ? '时间：横向' : '时间：纵向'
    );
    var denseButton = h(
      'button.chattree__btn',
      {
        type: 'button',
        onClick: function () {
          TREE.dense = !TREE.dense;
          TREE.gapY = TREE.dense ? 8 : 20;
          TREE.w = TREE.dense ? 168 : 212;
          denseButton.textContent = TREE.dense ? '疏密：紧凑' : '疏密：宽松';
          renderTree();
        },
      },
      TREE.dense ? '疏密：紧凑' : '疏密：宽松'
    );

    return h(
      'div.chattree__bar',
      null,
      h('span.chattree__title', { text: '对话树' }),
      h('span.chattree__sub', {
        text: state.messages.length + ' 个节点 · 亮的是当前分支 · 点节点切过去',
      }),
      dirButton,
      denseButton,
      h(
        'button.chattree__btn',
        {
          type: 'button',
          onClick: function () {
            TREE.view = { x: 0, y: 0, k: 1 };
            renderTree();
          },
        },
        '适应窗口'
      ),
      iconButton('close', '关闭（Esc）', close)
    );
  }

  function drawTree(host) {
    if (!host) return;
    var model = treeGeometry();
    var rect = host.getBoundingClientRect();
    var width = Math.max(320, rect.width);
    var height = Math.max(240, rect.height);

    var svg = sv('svg', { class: 'chattree__svg', width: '100%', height: '100%' });
    var layer = sv('g', { class: 'chattree__layer' });

    // 先画连线，再画节点（节点压在线上）
    model.edges.forEach(function (edge) {
      layer.appendChild(
        sv('path', {
          class: 'ctedge' + (edge.onPath ? ' is-onpath' : ''),
          d: treePath(edge),
        })
      );
    });

    model.nodes.forEach(function (node) {
      var message = node.message;
      var group = sv('g', {
        class:
          'ctnode' +
          (node.onPath ? ' is-onpath' : '') +
          (message.role === 'user' ? ' is-user' : '') +
          (message.status === 'error' ? ' is-error' : '') +
          (message.id === state.editing ? ' is-editing' : ''),
        transform: 'translate(' + node.x + ',' + (node.y - node.h / 2) + ')',
      });
      group.appendChild(
        sv('rect', { class: 'ctnode__box', x: 0, y: 0, width: TREE.w, height: node.h, rx: 12, ry: 12 })
      );
      group.appendChild(
        sv('text', { class: 'ctnode__who', x: 12, y: 18 }, [
          document.createTextNode(message.role === 'user' ? '我' : 'AI'),
        ])
      );
      if (node.forks > 1) {
        group.appendChild(
          sv('text', { class: 'ctnode__fork', x: TREE.w - 12, y: 18, 'text-anchor': 'end' }, [
            document.createTextNode('⑂' + node.forks),
          ])
        );
      }
      node.lines.forEach(function (line, index) {
        group.appendChild(
          sv('text', { class: 'ctnode__line', x: 12, y: 36 + index * 15 }, [
            document.createTextNode(line),
          ])
        );
      });

      // 点节点 = 切到那条分支（与原实现一致）
      group.addEventListener('click', function (event) {
        event.stopPropagation();
        state.treeOpen = false;
        revealMessage(message.id);
      });
      layer.appendChild(group);
    });

    svg.appendChild(layer);
    host.appendChild(svg);
    host.appendChild(
      h('div.chattree__hint', { text: '滚轮缩放 · 拖动平移' })
    );

    // 适应窗口：算完 bbox 再定缩放与偏移
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    model.nodes.forEach(function (node) {
      minX = Math.min(minX, node.x);
      minY = Math.min(minY, node.y - node.h / 2);
      maxX = Math.max(maxX, node.x + TREE.w);
      maxY = Math.max(maxY, node.y + node.h / 2);
    });
    if (!isFinite(minX)) return;
    var pad = 40;
    var scale = Math.min((width - pad * 2) / Math.max(1, maxX - minX), (height - pad * 2) / Math.max(1, maxY - minY), 1.1);
    TREE.bounds = { minX: minX, minY: minY, maxX: maxX, maxY: maxY };
    TREE.view.k = scale;
    TREE.view.x = (width - (maxX - minX) * scale) / 2 - minX * scale;
    TREE.view.y = (height - (maxY - minY) * scale) / 2 - minY * scale;
    applyTreeView(svg);

    svg.addEventListener('wheel', function (event) {
      event.preventDefault();
      var factor = event.deltaY < 0 ? 1.12 : 1 / 1.12;
      var k = Math.max(0.2, Math.min(2.4, TREE.view.k * factor));
      var rect2 = svg.getBoundingClientRect();
      var px = event.clientX - rect2.left;
      var py = event.clientY - rect2.top;
      TREE.view.x = px - (px - TREE.view.x) * (k / TREE.view.k);
      TREE.view.y = py - (py - TREE.view.y) * (k / TREE.view.k);
      TREE.view.k = k;
      applyTreeView(svg);
    });

    svg.addEventListener('pointerdown', function (event) {
      TREE.drag = { x: event.clientX, y: event.clientY, vx: TREE.view.x, vy: TREE.view.y };
      svg.setPointerCapture(event.pointerId);
      svg.classList.add('is-panning');
    });
    svg.addEventListener('pointermove', function (event) {
      if (!TREE.drag) return;
      TREE.view.x = TREE.drag.vx + (event.clientX - TREE.drag.x);
      TREE.view.y = TREE.drag.vy + (event.clientY - TREE.drag.y);
      applyTreeView(svg);
    });
    var endDrag = function () {
      TREE.drag = null;
      svg.classList.remove('is-panning');
    };
    svg.addEventListener('pointerup', endDrag);
    svg.addEventListener('pointercancel', endDrag);
  }

  function applyTreeView(svg) {
    var layer = svg.querySelector('.chattree__layer');
    if (!layer) return;
    layer.setAttribute(
      'transform',
      'translate(' + TREE.view.x + ',' + TREE.view.y + ') scale(' + TREE.view.k + ')'
    );
  }

  /* ------------------------------------------------------------ 分支 */

  function keyOf(parentId) {
    return parentId === null || parentId === undefined ? 'root' : String(parentId);
  }

  function childrenOf(parentId) {
    return state.messages
      .filter(function (m) {
        return keyOf(m.parentId) === keyOf(parentId);
      })
      .sort(function (a, b) {
        return a.id - b.id;
      });
  }

  /**
   * 当前该显示的那一条线：从根往下走，每个分叉上取用户选过的那一支，
   * 没选过就跟着最新的一支（"再生成"之后自然跟着新答案，不用额外告诉界面）。
   *
   * 这样切换分支不用回服务端问 —— 树早就在手里了。
   */
  function activePath() {
    var path = [];
    var parentId = null;
    for (var guard = 0; guard < 500; guard++) {
      var kids = childrenOf(parentId);
      if (!kids.length) break;
      var picked = state.picks[keyOf(parentId)];
      var chosen = null;
      for (var i = 0; i < kids.length; i++) {
        if (String(kids[i].id) === String(picked)) chosen = kids[i];
      }
      if (!chosen) chosen = kids[kids.length - 1];
      path.push(chosen);
      parentId = chosen.id;
    }
    return path;
  }

  function pickBranch(parentId, child) {
    state.picks[keyOf(parentId)] = child.id;
    paintThread();
    scrollToEnd(false);
  }

  function siblingsOf(message) {
    return childrenOf(message.parentId);
  }

  var LOCAL_ID = 0;

  /**
   * 图标按钮。
   *
   * 之前"附件"和"编辑并重发"是两个裸文字按钮，在一屏以内容为主的界面里
   * 又吵又难看。换成同一种细线条图标按钮（16px 描边、低对比、hover 才提亮），
   * 与界面里其它控件一致 —— 提示文字走 `title`，不占版面。
   */
  var ICONS = {
    clip: '<path d="M8.5 12.8 14.7 6.6a3.1 3.1 0 0 1 4.4 4.4l-7.6 7.6a5 5 0 0 1-7.1-7.1l7.4-7.4"/>',
    pencil:
      '<path d="M4 20h4l10.5-10.5a2.1 2.1 0 0 0-3-3L5 17v3z"/><path d="M13.4 6.6 17.4 10.6"/>',
    pin: '<path d="M9.5 4h5l-1 5.5 3.5 3.5H7l3.5-3.5L9.5 4z"/><path d="M12 13v7"/>',
    expand: '<path d="M4 9V4h5M20 15v5h-5M4 4l6 6M20 20l-6-6"/>',
    copy:
      '<rect x="9" y="9" width="11.5" height="11.5" rx="2.4"/>' +
      '<path d="M15 6.2A2.7 2.7 0 0 0 12.3 3.5H6.5A3 3 0 0 0 3.5 6.5v5.8A2.7 2.7 0 0 0 6.2 15"/>',
    retry: '<path d="M20.2 12a8.2 8.2 0 1 1-2.6-6"/><path d="M20.4 3.4v5.4h-5.4"/>',
    down: '<path d="M12 5.5v13"/><path d="m6 12.5 6 6 6-6"/>',
    note:
      '<path d="M5 4.5h14v15H5z"/><path d="M8.5 9h7M8.5 12.5h7M8.5 16h4"/>',
    trash:
      '<path d="M4.5 7h15"/><path d="M9.5 7V4.5h5V7"/><path d="M6.5 7l1 12.5h9L17.5 7"/>',
    close: '<path d="M6 6l12 12M18 6 6 18"/>',
  };

  /**
   * 复制到剪贴板。
   *
   * 用 Clipboard API，失败时退回"看不见的 textarea + execCommand"那条老路：
   * 非安全上下文（http 的局域网地址之类）里 `navigator.clipboard` 可能不存在，
   * 而"点了没反应"比报错更让人困惑。
   */
  function copyText(text, okText) {
    var value = String(text || '');
    if (!value) return;
    var done = function () {
      ui.toast(okText || '已复制', 'info', 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(value).then(done, function () {
        if (fallbackCopy(value)) done();
        else ui.toast('这个浏览器不让复制，手动选一下吧', 'warn', 2500);
      });
      return;
    }
    if (fallbackCopy(value)) done();
    else ui.toast('这个浏览器不让复制，手动选一下吧', 'warn', 2500);
  }

  function fallbackCopy(value) {
    try {
      var area = h('textarea', {
        style: { position: 'fixed', top: '-1000px', left: '-1000px', opacity: '0' },
      });
      area.value = value;
      document.body.appendChild(area);
      area.select();
      var ok = document.execCommand && document.execCommand('copy');
      area.remove();
      return !!ok;
    } catch (err) {
      return false;
    }
  }

  /**
   * 这条消息的**正文**：只要文本零件。
   *
   * 复制按钮复制的是它，而不是 `content` —— 助手消息的 `content` 是投影，
   * 真正给人看的是文本零件；工具调用、引用、题卡那些复制出去也没有意义。
   */
  function proseOf(message) {
    var parts =
      message.parts && message.parts.length
        ? message.parts
        : [{ type: 'text', text: message.content || '' }];
    return parts
      .filter(function (part) {
        return part && part.type === 'text' && part.text;
      })
      .map(function (part) {
        return String(part.text).trim();
      })
      .filter(function (text) {
        return text;
      })
      .join('\n\n')
      .trim();
  }

  function iconButton(name, title, onClick, extra) {
    return h('button.chaticon' + (extra || ''), {
      type: 'button',
      title: title,
      'aria-label': title,
      onClick: onClick,
      html:
        '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" ' +
        'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' +
        (ICONS[name] || ICONS.close) +
        '</svg>',
    });
  }

  /* ------------------------------------------------------------ 便签 */

  /**
   * 聊天时的便签：想到什么随手记一笔，不打断对话。
   *
   * 存在 settings 里（`QF.store.notes`），所以本地优先、跨设备跟着账号走 ——
   * 没有新表、新接口、新的冲突规则。代价是每条都跟着设置整份同步，
   * 因此有条数与字数上限（见 store.js 里那两个常量）。
   *
   * 「写进输入框」是它与对话之间唯一的接口，而且**只填不发** ——
   * 便签是素材，什么时候用、怎么用，由人决定。
   */
  var notesOpen = false;
  var notesDraft = '';
  var notesEditing = '';

  function openNotes() {
    notesOpen = true;
    notesEditing = '';
    renderNotes();
  }

  function closeNotes() {
    notesOpen = false;
    notesDraft = '';
    notesEditing = '';
    renderNotes();
  }

  function noteTime(note) {
    var d = new Date(note.editedAt || note.at);
    var pad = function (value) {
      return (value < 10 ? '0' : '') + value;
    };
    return (
      d.getMonth() +
      1 +
      '月' +
      d.getDate() +
      '日 ' +
      pad(d.getHours()) +
      ':' +
      pad(d.getMinutes()) +
      (note.editedAt ? ' · 改过' : '')
    );
  }

  function saveNote() {
    var text = String(notesDraft || '').trim();
    if (!text) return;
    if (notesEditing) {
      QF.store.updateNote(notesEditing, text);
    } else if (!QF.store.addNote(text, state.current)) {
      ui.toast('便签满了（200 条）—— 先清理几条再记新的', 'warn', 3200);
      return;
    }
    notesDraft = '';
    notesEditing = '';
    renderNotes();
    ui.toast('已记下', 'info', 1200);
  }

  /** 把便签写进输入框（**不发送**）：便签是素材，怎么用由人决定。 */
  function insertNote(text) {
    var body = String(text || '').trim();
    if (!body || !inputEl) return;
    var current = String(inputEl.value || '');
    inputEl.value = current ? current.replace(/\s+$/, '') + '\n' + body : body;
    growInput();
    closeNotes();
    inputEl.focus();
  }

  function noteNode(note) {
    var box = h('div.chatnotes__item');

    if (notesEditing === note.id) {
      var draft = h('textarea.chatnotes__input', {
        rows: '3',
        value: note.text,
        onInput: function (event) {
          notesDraft = event.target.value;
        },
        onKeydown: function (event) {
          if (event.key === 'Escape') {
            event.preventDefault();
            notesEditing = '';
            notesDraft = '';
            renderNotes();
          } else if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
            event.preventDefault();
            saveNote();
          }
        },
      });
      notesDraft = note.text;
      box.appendChild(draft);
      box.appendChild(
        h(
          'div.chatnotes__acts',
          null,
          h('button.chatnotes__act.is-pri', { type: 'button', onClick: saveNote }, '保存'),
          h(
            'button.chatnotes__act',
            {
              type: 'button',
              onClick: function () {
                notesEditing = '';
                notesDraft = '';
                renderNotes();
              },
            },
            '取消'
          )
        )
      );
      return box;
    }

    box.appendChild(
      h(
        'div.chatnotes__meta',
        null,
        h('span.chatnotes__time', { text: noteTime(note) }),
        note.cid && note.cid === state.current
          ? h('span.chatnotes__tag', { text: '记于本次对话' })
          : null
      )
    );
    box.appendChild(h('div.chatnotes__text', { text: note.text }));
    box.appendChild(
      h(
        'div.chatnotes__acts',
        null,
        h(
          'button.chatnotes__act.is-pri',
          {
            type: 'button',
            onClick: function () {
              insertNote(note.text);
            },
          },
          '写进输入框'
        ),
        h(
          'button.chatnotes__act',
          {
            type: 'button',
            onClick: function () {
              copyText(note.text, '便签已复制');
            },
          },
          '复制'
        ),
        h(
          'button.chatnotes__act',
          {
            type: 'button',
            onClick: function () {
              notesEditing = note.id;
              notesDraft = note.text;
              renderNotes();
            },
          },
          '编辑'
        ),
        h(
          'button.chatnotes__act.is-bad',
          {
            type: 'button',
            onClick: function () {
              if (QF.store.removeNote(note.id)) {
                renderNotes();
                ui.toast('已删除', 'info', 1200);
              }
            },
          },
          '删除'
        )
      )
    );
    return box;
  }

  function renderNotes() {
    if (!notesEl) return;
    ui.clear(notesEl);
    if (!notesOpen) return;

    var notes = QF.store.notes();
    var saveBtn = h(
      'button.btn.btn--primary.chatnotes__save',
      {
        type: 'button',
        disabled: !String(notesDraft || '').trim(),
        onClick: saveNote,
      },
      '记下来'
    );
    var draft = h('textarea.chatnotes__input', {
      rows: '3',
      placeholder: '随手记点什么…（Ctrl / ⌘ + Enter 保存）',
      value: notesDraft,
      onInput: function (event) {
        notesDraft = event.target.value;
        saveBtn.disabled = !String(notesDraft || '').trim();
      },
      onKeydown: function (event) {
        if (event.key === 'Escape') {
          event.preventDefault();
          closeNotes();
        } else if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
          event.preventDefault();
          saveNote();
        }
      },
    });

    notesEl.appendChild(
      h(
        'div.chatnotes__backdrop',
        {
          onClick: function (event) {
            if (event.target === event.currentTarget) closeNotes();
          },
        },
        h(
          'div.chatnotes__sheet',
          null,
          h(
            'div.chatnotes__head',
            null,
            h('span.chatnotes__title', { text: '便签' }),
            h('span.chatnotes__count', {
              text: notes.length ? notes.length + ' 条' : '',
            }),
            iconButton('close', '关闭（Esc）', closeNotes)
          ),
          h('div.chatnotes__compose', null, draft, saveBtn),
          notes.length
            ? h('div.chatnotes__list', null, notes.map(noteNode))
            : h('div.chatnotes__empty', {
                text: '还没有便签。聊天时想到什么就记一笔 —— 它会跟着你的账号同步，也能一键写进输入框。',
              })
        )
      )
    );
    draft.focus();
  }

  /* ------------------------------------------------------------ 骨架 */

  function buildSkeleton() {
    rootEl.textContent = '';

    // 左栏只建一次：搜索框如果跟着列表一起重画，打字打到一半就会丢焦点
    asideEl = h('aside.chat__aside');
    asideHeadEl = h('div.chat__asidehead');
    listEl = h('div.chat__list');
    asideEl.appendChild(asideHeadEl);
    asideEl.appendChild(listEl);
    threadEl = h('div.chat__thread', { role: 'log', 'aria-live': 'polite' });
    // 滚上去就露出「回到最新」（见 refreshJump）
    threadEl.addEventListener('scroll', refreshJump);
    inputEl = h('textarea.chat__input', {
      rows: '1',
      placeholder: '问点什么，或者贴一段材料…（Enter 发送，Shift+Enter 换行）',
      onInput: growInput,
      onKeydown: onKeydown,
    });
    sendBtn = h('button.btn.btn--primary.chat__send', { type: 'button', onClick: onSendClick }, '发送');
    clipInput = h('input.chat__file', {
      type: 'file',
      multiple: true,
      onChange: onPickFiles,
    });
    pendingEl = h('div.chat__pending');
    hintEl = h('div.chat__hint');
    noticeEl = h('div.chat__notice');
    barEl = h('div.chat__bar');
    treeEl = h('div.chattree', { role: 'dialog', 'aria-label': '对话树' });
    demoEl = h('div.chatdemo', { role: 'dialog', 'aria-label': '演示' });
    notesEl = h('div.chatnotes', { role: 'dialog', 'aria-label': '便签' });
    rootEl.appendChild(
      h(
        'div.chat',
        null,
        asideEl,
        h(
          'section.chat__main',
          null,
          barEl,
          threadEl,
          noticeEl,
          h(
            'div.chat__composer',
            null,
            pendingEl,
            (jumpBtn = iconButton('down', '回到最新', function () {
              scrollToEnd(true);
            }, '.chat__jump')),
            h(
              'div.chat__box',
              null,
              iconButton('clip', '加附件（文档、代码、PDF、截图）', function () {
                if (clipInput) clipInput.click();
              }),
              iconButton('note', '便签（边聊边记）', function () {
                if (notesOpen) closeNotes();
                else openNotes();
              }),
              clipInput,
              inputEl,
              sendBtn
            ),
            hintEl
          )
        ),
        treeEl,
        demoEl,
        notesEl
      )
    );
  }

  /* ------------------------------------------------------------ 左栏 */

  function renderAside() {
    if (!listEl) return;

    // 头部只建一次：搜索框在里面，跟着列表一起重画会丢焦点（打字打到一半就断）
    if (!asideHeadEl.childNodes.length) {
      asideHeadEl.appendChild(
        h('button.btn.btn--primary.chat__new', { type: 'button', onClick: onNewClick }, '新对话')
      );
      asideHeadEl.appendChild(
        h('input.input.chat__search', {
          type: 'search',
          placeholder: '搜过去的消息…',
          value: state.query,
          onInput: onSearchInput,
        })
      );
    }

    ui.clear(listEl);

    if (state.query.trim().length >= 2) {
      renderResults();
      return;
    }

    if (!state.list.length) {
      listEl.appendChild(
        h('div.chat__emptylist', { text: '还没有对话。上面这个按钮开始第一条。' })
      );
      return;
    }

    var list = h('div.chatlist');
    state.list.forEach(function (conv) {
      var item = h(
        'div.chatlist__item' +
          (conv.id === state.current ? '.is-on' : '') +
          (conv.pinned ? '.is-pinned' : ''),
        {
          onClick: function () {
            if (conv.id !== state.current) openConversation(conv.id);
          },
        },
        h('div.chatlist__title', { text: conv.title || '未命名对话' }),
        h(
          'div.chatlist__meta',
          null,
          h('span', { text: ui.fmtRelative(conv.updatedAtMs) }),
          conv.messageCount ? h('span', { text: '· ' + conv.messageCount + ' 条' }) : null
        ),
        conv.preview ? h('div.chatlist__preview', { text: conv.preview }) : null,
        iconButton(
          'pin',
          conv.pinned ? '取消置顶' : '置顶',
          function (event) {
            event.stopPropagation(); // 别顺带把会话也切了
            togglePin(conv);
          },
          '.chatlist__pin' + (conv.pinned ? '.is-on' : '')
        ),
        h('button.chatlist__more', {
          type: 'button',
          title: '重命名 / 删除 / 导出',
          'aria-label': '更多',
          onClick: function (event) {
            event.stopPropagation(); // 别顺带把会话也切了
            openMenu(conv);
          },
        }, '⋯')
      );
      list.appendChild(item);
    });
    listEl.appendChild(list);
  }

  /* ------------------------------------------------------------ 搜索 */

  function renderResults() {
    if (!state.results.length) {
      listEl.appendChild(
        h('div.chat__emptylist', { text: '没有搜到「' + state.query.trim() + '」。' })
      );
      return;
    }
    var box = h('div.chatlist');
    state.results.forEach(function (hit) {
      box.appendChild(
        h(
          'div.chatlist__item',
          {
            onClick: function () {
              openHit(hit);
            },
          },
          h('div.chatlist__title', { text: hit.title }),
          h(
            'div.chatlist__meta',
            null,
            h('span', { text: hit.role === 'user' ? '我问的' : '它答的' }),
            h('span', { text: '· ' + ui.fmtRelative(hit.atMs) })
          ),
          h('div.chatlist__preview', { text: hit.snippet })
        )
      );
    });
    listEl.appendChild(box);
  }

  function onSearchInput(event) {
    state.query = event.target.value || '';
    if (searchTimer) clearTimeout(searchTimer);
    // 300ms：打字过程中每一下都发一次请求，既浪费也会让结果闪
    searchTimer = setTimeout(runSearch, 300);
  }

  function runSearch() {
    var query = state.query.trim();
    if (query.length < 2) {
      state.results = [];
      renderAside();
      return;
    }
    api
      .get('/chat/search?q=' + encodeURIComponent(query))
      .then(function (res) {
        if (state.query.trim() !== query) return; // 用户已经改了字，过期结果不画
        state.results = (res && res.items) || [];
        renderAside();
      })
      .catch(function (err) {
        ui.toast(err.message, 'error');
      });
  }

  function openHit(hit) {
    openConversation(hit.conversationId).then(function () {
      revealMessage(hit.messageId);
    });
  }

  /**
   * 把某条消息翻到眼前：沿它的父链把每一层的"选择"设成它，于是当前分支必然经过它。
   *
   * 命中可能落在**任何一条分支**上（包括已经被"重新回答"顶下去的那条）——
   * 不这么做的话，搜到了却看不见，用户只会觉得搜索坏了。
   */
  function revealMessage(messageId) {
    var byId = {};
    state.messages.forEach(function (m) {
      byId[m.id] = m;
    });

    var chain = [];
    var node = byId[messageId];
    for (var guard = 0; node && guard < 500; guard++) {
      chain.unshift(node);
      node = node.parentId ? byId[node.parentId] : null;
    }

    // 每一层都设成"要走这条链"：**包括根那一条** —— 目标是根上的消息时
    // （比如在对话树里点一条第一问），按"往下走"的写法会一次都不执行，
    // 于是点了没反应。写成"给每个节点设它父节点那一层的选择"就没有这个洞。
    state.picks = {};
    chain.forEach(function (node) {
      state.picks[keyOf(node.parentId)] = node.id;
    });
    paintThread();

    var row = threadEl ? threadEl.querySelector('[data-id="' + messageId + '"]') : null;
    if (!row) return;
    row.scrollIntoView({ block: 'center', behavior: 'smooth' });
    row.classList.add('is-flash');
    setTimeout(function () {
      row.classList.remove('is-flash');
    }, 1800);
  }

  function loadList() {
    return api
      .get('/chat/conversations')
      .then(function (res) {
        state.list = (res && res.conversations) || [];
        renderAside();
      })
      .catch(function (err) {
        ui.toast(err.message, 'error');
      });
  }

  /* ------------------------------------------------------------ 主区 */

  function renderEmptyThread() {
    // 工具栏也在这儿刷新一次：空对话不走 paintThread（它直接画引导页），
    // 只在 paintThread 里刷的话，刚进页面时"对话树"这个入口根本不出现
    renderBar();
    ui.clear(threadEl);
    var examples = [
      '用一句话说清 circular buffer 在 tt-metal 里解决什么问题',
      '我贴一段材料，你讲讲它到底在说什么',
      '这道题我选错了，帮我看看是哪一步想歪了',
    ];
    var box = h(
      'div.chat__intro',
      null,
      h('h2.chat__introt', { text: '从哪句开始都行' }),
      h('p.chat__introp', {
        text: '这里适合「截一段材料问到底」这种学法。做错的题也可以直接贴过来问。',
      })
    );
    examples.forEach(function (text) {
      box.appendChild(
        h('button.chat__example', {
          type: 'button',
          onClick: function () {
            inputEl.value = text;
            growInput();
            inputEl.focus();
          },
        }, text)
      );
    });
    threadEl.appendChild(box);
  }

  function paintThread() {
    renderBar();
    renderTree();
    if (!state.messages.length) {
      renderEmptyThread();
      return;
    }
    ui.clear(threadEl);
    activePath().forEach(function (m) {
      threadEl.appendChild(messageRow(m));
    });
    scrollToEnd(true);
  }

  /** `‹ 2 / 3 ›`：同一个父节点下的几个分支，翻着看（LibreChat 的 SiblingSwitch）。 */
  function siblingSwitch(m, siblings) {
    var index = 0;
    siblings.forEach(function (item, i) {
      if (item.id === m.id) index = i;
    });
    var prev = h('button.chatmsg__sib', {
      type: 'button',
      title: '上一个分支',
      disabled: index === 0,
      onClick: function () {
        pickBranch(m.parentId, siblings[index - 1]);
      },
    });
    prev.textContent = '‹';
    var next = h('button.chatmsg__sib', {
      type: 'button',
      title: '下一个分支',
      disabled: index === siblings.length - 1,
      onClick: function () {
        pickBranch(m.parentId, siblings[index + 1]);
      },
    });
    next.textContent = '›';
    return h(
      'div.chatmsg__siblings',
      null,
      prev,
      h('span.chatmsg__sibcount', { text: index + 1 + ' / ' + siblings.length }),
      next
    );
  }

  function messageRow(m) {
    var isUser = m.role === 'user';
    var body = h('div.chatmsg__body');

    // 有兄弟就显示切换器：没有它，"重新回答"过的旧分支就永远够不着了
    var siblings = siblingsOf(m);
    if (siblings.length > 1) body.appendChild(siblingSwitch(m, siblings));

    if (isUser) {
      if (state.editing === m.id) {
        body.appendChild(userEditor(m));
      } else {
        body.appendChild(h('div.chatmsg__text', { text: m.content }));
        // 用户消息也有零件 —— 附件就挂在这一侧（助手那一侧的零件走 partsNode）
        (m.parts || []).forEach(function (part) {
          if (part && part.type === 'file') body.appendChild(fileNode(part));
        });
        body.appendChild(
          h(
            'div.chatmsg__useractions',
            null,
            iconButton('copy', '复制我这条', function () {
              copyText(m.content, '已复制我这条');
            }),
            iconButton('pencil', '编辑并重发', function () {
              editMessage(m);
            })
          )
        );
      }
    } else {
      // 正文是投影，零件才是真相：旧消息没有 parts 时按正文兜一个
      body.appendChild(
        partsNode(m.parts && m.parts.length ? m.parts : [{ type: 'text', text: m.content || '' }])
      );
    }
    var row = h(
      'div.chatmsg' + (isUser ? '.chatmsg--user' : '.chatmsg--assistant') + (m.status === 'error' ? '.is-error' : ''),
      { dataset: { id: String(m.id || '') } },
      h('div.chatmsg__who', { text: isUser ? '我' : 'AI' }),
      body
    );
    decorateAssistant(row, body, m);
    return row;
  }

  /**
   * 就地编辑一条用户消息。
   *
   * **改的不是原来那一条**：服务端会新落一条用户消息（同一父节点下的兄弟），
   * 于是旧那条连同它的回答都留在树上。这不是洁癖 —— 轨迹是这里最值钱的东西，
   * 「我当时问的到底是什么」以后要靠它回答；而且旧分支随时还能翻回去。
   */
  function userEditor(m) {
    var box = h('div.chatmsg__edit');
    var area = h('textarea.chatmsg__editarea', { rows: '3' });
    area.value = m.content || '';

    function close() {
      state.editing = 0;
      paintThread();
    }

    box.appendChild(area);
    box.appendChild(
      h(
        'div.chatmsg__editfoot',
        null,
        h(
          'button.btn.btn--primary.chatmsg__editsave',
          {
            type: 'button',
            onClick: function () {
              var text = String(area.value || '').trim();
              if (!text || state.busy || text === String(m.content || '').trim()) {
                close();
                return;
              }
              close();
              // parentId 显式给出来（可能是 null）：第一条消息就在根上，
              // 不显式说的话服务端会把它挂到会话末尾去
              send({ content: text, parentId: m.parentId === undefined ? null : m.parentId });
            },
          },
          '保存并重发'
        ),
        h('button.chatmsg__editcancel', { type: 'button', onClick: close }, '取消'),
        h('span.chatmsg__edithint', { text: '旧的那条会留在对话树里' })
      )
    );
    return box;
  }

  function editMessage(m) {
    if (state.busy || !m || m.role !== 'user') return;
    state.editing = m.id;
    paintThread();
    var area = threadEl.querySelector('.chatmsg__editarea');
    if (area) {
      area.focus();
      area.setSelectionRange(area.value.length, area.value.length);
    }
  }

  /* ------------------------------------------------------------ 附件与演示 */

  /**
   * 附件：图片直接显示，别的给一个链接 + 元数据。
   *
   * 抽不出正文的图片要**明说**"AI 看不到图像内容" —— 用户传张截图然后纳闷
   * 为什么它答非所问，是最容易消耗信任的一种情况。
   */
  function fileNode(part) {
    var url = '/api/chat/attachments/' + encodeURIComponent(String(part.attachmentId || ''));
    var box = h('div.chatfile');

    if (part.kind === 'image') {
      box.appendChild(
        h('img.chatfile__img', { src: url, alt: part.name || '附件', loading: 'lazy' })
      );
    }
    box.appendChild(
      h(
        'div.chatfile__row',
        null,
        h(
          'a.chatfile__link',
          { href: url, target: '_blank', rel: 'noreferrer' },
          h('span.chatfile__name', { text: part.name || '附件' })
        ),
        h('span.chatfile__meta', { text: attachmentMeta(part) })
      )
    );
    if (part.kind === 'image' && !part.textChars) {
      box.appendChild(
        h('div.chatfile__hint', {
          text: 'AI 看不到图像内容 —— 想让它讲图里的事，把关键文字抄进消息里。',
        })
      );
    }
    return box;
  }

  // 演示的 CSP：**允许联网**（要能跑"各种脚本"：CDN 上的 d3 / three.js、调自己的 API
  // 都得能用）。仍然关着的是 frame/object —— 演示不该再套娃。
  // 真正挡住"碰到我们"的那一道是 iframe 的 `sandbox`（**不带** allow-same-origin），
  // 不是 CSP：CSP 管的是它能往外拿什么，sandbox 管的是它能不能碰我们。
  /**
   * 沙箱页面的 CSP。
   *
   * **必须显式写出我们自己的源**：沙箱 iframe 没有 `allow-same-origin`，它的
   * `'self'` 是一个"不透明源"、匹配不到我们的服务器 —— 于是
   * `/assets/pyodide/pyodide.js`（本地那份 Python 运行时就放在那儿）会被自己的 CSP
   * 拦掉。写成 `https:` 也不行：开发环境是 `http://127.0.0.1`。
   *
   * 另外仍然允许 https：模型写的演示可以用 CDN 上的库（能联网时），
   * 而那**不再**是跑 Python 的前提 —— 运行时是本机那一份。
   */
  function demoCsp() {
    var origin = location.origin;
    return (
      "default-src 'none'; " +
      "script-src 'unsafe-inline' 'unsafe-eval' blob: " +
      origin +
      " https:; " +
      "style-src 'unsafe-inline' " +
      origin +
      " https:; " +
      "img-src data: blob: " +
      origin +
      " https:; " +
      "font-src data: " +
      origin +
      " https:; " +
      "media-src data: blob: " +
      origin +
      " https:; " +
      "connect-src " +
      origin +
      " https:; " +
      "worker-src blob:; " +
      "frame-src 'none'; object-src 'none';"
    );
  }

  /**
   * 把 CSP 塞进演示文档，并把服务端留的 `__ORIGIN__` 换成绝对地址。
   *
   * 为什么非要宿主来填：沙箱页面里**相对路径解析不了**（srcdoc 文档的 base 是
   * `about:srcdoc`，`new URL('/assets/…')` 会直接抛 "Invalid URL"），
   * 而沙箱自己的 `location.origin` 是不透明的、它也拼不出我们的地址。
   * 本地 Python 运行时就落在 `/assets/pyodide/`，所以这一步是它能不能加载的前提。
   */
  function withCsp(html) {
    html = String(html || '').split('__ORIGIN__').join(location.origin);
    var meta = '<meta http-equiv="Content-Security-Policy" content="' + demoCsp() + '">';
    if (/<head[^>]*>/i.test(html)) {
      return html.replace(/<head[^>]*>/i, function (found) {
        return found + meta;
      });
    }
    if (/<html[^>]*>/i.test(html)) {
      return html.replace(/<html[^>]*>/i, function (found) {
        return found + '<head>' + meta + '</head>';
      });
    }
    return '<!doctype html><html><head>' + meta + '</head><body>' + html + '</body></html>';
  }

  /**
   * 演示在消息里只是一张**卡片**：标题 + 说明 + 打开按钮。
   *
   * 真正跑它的是右侧那个整屏高的面板 —— 嵌在消息流里又窄又矮，
   * 稍微像样一点的可视化都会被框住（那是第一版的问题）。
   */
  function demoCard(part) {
    return h(
      'div.chatdemo__card',
      null,
      h(
        'div.chatdemo__cardmain',
        null,
        h('div.chatdemo__cardtitle', { text: part.title || '演示' }),
        h('div.chatdemo__cardnote', { text: '在右侧面板里打开' })
      ),
      h('button.btn.btn--ghost.chatdemo__open', {
        type: 'button',
        text: '打开演示',
        onClick: function () {
          openDemo(part);
        },
      })
    );
  }

  /**
   * 沙箱回传的运行结果，按 runId 存。
   *
   * 为什么不塞进消息零件：零件是**库里那份**（刷新会重读），而这是"这一次运行"
   * 的产物 —— 沙箱页面刷新后会自己重跑一遍再回传，所以前端临时持有就够了。
   */
  var demoRuns = {};
  var demoRunEl = null;

  function demoRunOf(runId) {
    return runId ? demoRuns[runId] || null : null;
  }

  // 沙箱页面（Python 那种）跑完会 postMessage 回来：{qfRun: runId, ok, text}
  window.addEventListener('message', function (event) {
    var data = event.data;
    if (!data || !data.qfRun) return;
    demoRuns[data.qfRun] = { ok: data.ok !== false, text: String(data.text || '') };
    if (state.demo && state.demo.runId === data.qfRun) {
      state.demo.run = demoRuns[data.qfRun];
      paintDemoRun();
    }
  });

  /**
   * 把沙箱输出发回给模型 —— 这是输出能回到它手里的**唯一一条路**，而且由人按。
   *
   * 不自动追发：那等于替用户说话，还可能在他还没看结果时白烧一轮额度。
   * 摆一个按钮在这儿，什么时候发、发哪一次，他说了算。
   */
  function sendDemoRun(run) {
    if (state.busy) return;
    var text = String((run && run.text) || '').trim();
    if (!text) {
      ui.toast('这次运行没有输出可发', 'warn');
      return;
    }
    state.demo = null;
    renderDemoPanel();
    send({ content: '沙箱里跑出来的结果（原样贴给你）：\n```\n' + text + '\n```\n按这个结果说下去。' });
  }

  function paintDemoRun() {
    if (!demoRunEl) return;
    ui.clear(demoRunEl);
    if (!state.demo || !state.demo.runId) return;

    var run = state.demo.run;
    if (!run) {
      demoRunEl.appendChild(
        h('div.chatdemo__runwait', { text: '沙箱还在跑…（跑完这里会显示输出）' })
      );
      return;
    }

    demoRunEl.appendChild(
      h(
        'div.chatdemo__runhead',
        null,
        h('span.chatdemo__runlabel' + (run.ok ? '' : '.is-bad'), {
          text: run.ok ? '沙箱输出' : '沙箱报错',
        }),
        h(
          'button.btn.btn--ghost.chatdemo__runsend',
          {
            type: 'button',
            onClick: function () {
              sendDemoRun(run);
            },
          },
          '把输出发给它'
        ),
        h(
          'button.chatdemo__runcopy',
          {
            type: 'button',
            onClick: function () {
              copyText(run.text || '', '输出已复制');
            },
          },
          '复制'
        )
      )
    );
    demoRunEl.appendChild(h('pre.chatdemo__runtxt', { text: run.text || '（没有输出）' }));
  }

  function openDemo(part) {
    // runId 要带过来：沙箱跑完会把输出 postMessage 回来，靠它认领到这次运行
    state.demo = {
      title: part.title || '演示',
      html: String(part.html || ''),
      runId: String(part.runId || ''),
      run: demoRunOf(part.runId) || null,
    };
    renderDemoPanel();
  }

  /**
   * 演示面板（右侧整屏高）。
   *
   * 安全**不靠"相信模型"**：iframe 上只有 `allow-scripts`，**不带**
   * `allow-same-origin` —— 里面的脚本拿不到我们的 cookie / localStorage，
   * 也碰不到我们的 DOM；再叠 `referrerpolicy="no-referrer"`，
   * 连"这个页面从哪来"都不往外带。
   */
  function renderDemoPanel() {
    if (!demoEl) return;
    ui.clear(demoEl);
    if (!state.demo) return;

    var close = function () {
      state.demo = null;
      renderDemoPanel();
    };

    // 输出那一块单独拎出来：沙箱回传时只重画它，**不重建 iframe**
    // （重建 = 重新跑一遍 = 又回传一次，会打转）
    var runBox = h('div.chatdemo__run');
    demoRunEl = runBox;

    demoEl.appendChild(
      h(
        'div.chatdemo__backdrop',
        {
          onClick: function (event) {
            if (event.target === event.currentTarget) close();
          },
        },
        h(
          'div.chatdemo__panel',
          null,
          h(
            'div.chatdemo__head',
            null,
            h('span.chatdemo__title', { text: state.demo.title }),
            iconButton('close', '关闭（Esc）', close)
          ),
          h('iframe.chatdemo__frame', {
            sandbox: 'allow-scripts',
            referrerpolicy: 'no-referrer',
            title: state.demo.title,
            srcdoc: withCsp(state.demo.html),
          }),
          runBox
        )
      )
    );

    paintDemoRun();
  }

  /* ------------------------------------------------------------ 题卡 */

  /**
   * 题卡的作答状态，按题目 id 存。
   *
   * 为什么不能存在 DOM 里：流式过程中每个字都会触发一次重画，
   * 而重画会把卡片重建 —— 用户填到一半的空就会被打回去。
   */
  var cardDrafts = {};

  function draftOf(questionId) {
    if (!cardDrafts[questionId]) {
      cardDrafts[questionId] = {
        choice: '',
        picked: [],
        blanks: [],
        text: '',
        response: null,
        result: null,
        busy: false,
      };
    }
    return cardDrafts[questionId];
  }

  /**
   * 一张可作答的题卡。
   *
   * 判分与写回**完全走刷题页那条路**（`QF.engine.grade` + `QF.store.applyResult`）：
   * 记录口径、SM2、掌握度因此天然一致，不需要为对话另写一套 ——
   * 这是"卡片是消息的一种零件"最值钱的地方。
   */
  function questionCard(part) {
    var host = h('div.chatcard__host');

    function paint() {
      ui.clear(host);
      host.appendChild(cardBody(part, paint));
    }

    paint();
    return host;
  }

  function cardBody(part, repaint) {
    var payload = part.payload || {};
    var questionId = String(payload.questionId || '');
    var box = h('div.chatcard');
    if (!questionId) return box;

    var question = QF.data && QF.data.get ? QF.data.get(questionId) : null;
    var draft = draftOf(questionId);

    box.appendChild(
      h(
        'div.chatcard__head',
        null,
        h('span.chatcard__id', { text: questionId }),
        payload.layer ? h('span.chatcard__tag', { text: payload.layer }) : null,
        payload.wing ? h('span.chatcard__tag', { text: payload.wing }) : null,
        payload.difficulty ? h('span.chatcard__tag', { text: '难度 ' + payload.difficulty }) : null
      )
    );
    box.appendChild(h('div.chatcard__stem', null, QF.md.render(payload.stem || '')));

    if (!question) {
      // 题库换过、这道题取不到了：说清去哪儿看，而不是给一个点不动的空壳
      box.appendChild(
        h('div.chatcard__note', { text: '题面读不到了（题库可能变过）。去刷题页搜 ' + questionId + '。' })
      );
      return box;
    }

    // 答过就不重来一遍：结果与讲评直接摆出来（记录是权威，刷新也不会变回去）
    var record = QF.store && QF.store.records ? QF.store.records()[questionId] : null;
    if (draft.result) {
      box.appendChild(cardResult(question, draft.result, draft.response, draft, repaint));
      return box;
    }
    if (record && record.attempts) {
      box.appendChild(
        cardResult(
          question,
          { status: record.lastStatus, score: record.lastScore },
          record.lastResponse,
          draft,
          repaint
        )
      );
      return box;
    }

    box.appendChild(cardInput(question, draft, repaint));
    return box;
  }

  function cardInput(question, draft, repaint) {
    var box = h('div.chatcard__body');
    var kind = question.type;

    if (kind === 'single' || kind === 'multi') {
      (question.options || []).forEach(function (option) {
        var on =
          kind === 'single' ? draft.choice === option.key : draft.picked.indexOf(option.key) >= 0;
        box.appendChild(
          h(
            'button.chatcard__opt' + (on ? '.is-on' : ''),
            {
              type: 'button',
              onClick: function () {
                if (kind === 'single') {
                  draft.choice = option.key;
                } else {
                  draft.picked = on
                    ? draft.picked.filter(function (key) {
                        return key !== option.key;
                      })
                    : draft.picked.concat([option.key]);
                }
                repaint();
              },
            },
            h('span.chatcard__key', { text: option.key }),
            h('span.chatcard__opttext', { html: QF.md.renderInline(option.text || '') })
          )
        );
      });
    } else if (kind === 'blank') {
      var count = (question.answer || []).length || 1;
      for (var index = 0; index < count; index++) {
        box.appendChild(
          h('input.chatcard__blank', {
            type: 'text',
            placeholder: '第 ' + (index + 1) + ' 空',
            value: draft.blanks[index] || '',
            onInput: (function (at) {
              return function (event) {
                draft.blanks[at] = event.target.value;
              };
            })(index),
          })
        );
      }
    } else {
      box.appendChild(
        h('textarea.chatcard__text', {
          rows: '3',
          placeholder: '写出你的答案，提交后由 AI 批改',
          value: draft.text,
          onInput: function (event) {
            draft.text = event.target.value;
          },
        })
      );
    }

    var needsAI = QF.engine.grade(question, emptyResponse(question)).requiresAI;
    var submit = h(
      'button.btn.btn--primary.chatcard__submit',
      {
        type: 'button',
        disabled: draft.busy,
        onClick: function () {
          var response = readResponse(question, draft);
          if (QF.engine.isResponseEmpty(question, response)) {
            ui.toast('还没作答呢', 'warn');
            return;
          }
          gradeCard(question, response, draft, repaint);
        },
      },
      draft.busy ? '批改中…' : needsAI ? '提交（AI 批改）' : '提交'
    );
    box.appendChild(h('div.chatcard__actions', null, submit));
    return box;
  }

  function emptyResponse(question) {
    return question.type === 'multi' || question.type === 'blank' ? [] : '';
  }

  function readResponse(question, draft) {
    if (question.type === 'single') return draft.choice;
    if (question.type === 'multi') return draft.picked.slice();
    if (question.type === 'blank') {
      var count = (question.answer || []).length || 1;
      var out = [];
      for (var index = 0; index < count; index++) out.push(draft.blanks[index] || '');
      return out;
    }
    return draft.text;
  }

  /** 判分 + 写回。客观题本地判，简答走 AI —— 两条都与刷题页共用同一条写回路径。 */
  function gradeCard(question, response, draft, repaint) {
    var result = QF.engine.grade(question, response);

    if (result.requiresAI && QF.ai && QF.ai.grade) {
      draft.busy = true;
      repaint();
      QF.ai
        .grade(question, response)
        .then(function (aiResult) {
          draft.busy = false;
          draft.response = response;
          draft.result = QF.ai.toResult(aiResult, question);
          QF.store.applyResult(question, response, draft.result);
          repaint();
        })
        .catch(function (err) {
          // 批改失败也要落一条（与刷题页一致）：否则这次作答两头都不算
          draft.busy = false;
          draft.response = response;
          draft.result = {
            status: 'ungraded',
            correct: false,
            score: null,
            blanks: [],
            expected: '见参考答案',
            aiError: err.message,
          };
          QF.store.applyResult(question, response, draft.result);
          ui.toast('AI 批改失败：' + err.message, 'error', 7000);
          repaint();
        });
      return;
    }

    draft.response = response;
    draft.result = result;
    QF.store.applyResult(question, response, result);
    repaint();
  }

  function cardResult(question, result, response, draft, repaint) {
    var box = h('div.chatcard__body');
    var described = QF.engine.describeStatus(result.status || 'ungraded');
    var tone = described.tone || 'muted';

    box.appendChild(
      h(
        'div.chatcard__verdict.is-' + tone,
        null,
        h('span.chatcard__verdicttext', { text: described.label }),
        result.score === null || result.score === undefined
          ? null
          : h('span.chatcard__score', { text: result.score + ' 分' })
      )
    );

    var mine = formatResponse(response);
    if (mine) box.appendChild(h('div.chatcard__mine', { text: '你的作答：' + mine }));

    box.appendChild(
      h(
        'div.chatcard__expected',
        null,
        h('div.chatcard__label', { text: '正确答案' }),
        QF.md.render(QF.engine.expectedText(question))
      )
    );

    if (question.explanation) {
      box.appendChild(
        h(
          'details.chatcard__why',
          null,
          h('summary', { text: '讲评' }),
          QF.md.render(question.explanation)
        )
      );
    }

    box.appendChild(
      h(
        'div.chatcard__actions',
        null,
        // 这道题与对话的接口：把"我答了什么"告诉 AI，它就知道该讲哪儿
        h(
          'button.btn.chatcard__ask',
          {
            type: 'button',
            onClick: function () {
              askAboutCard(question, response, result);
            },
          },
          '让 AI 讲讲这道题'
        ),
        h(
          'button.chatcard__again',
          {
            type: 'button',
            onClick: function () {
              draft.result = null;
              draft.response = null;
              repaint();
            },
          },
          '再做一次'
        )
      )
    );
    return box;
  }

  function formatResponse(response) {
    if (Array.isArray(response)) {
      return response
        .filter(function (item) {
          return String(item || '').trim();
        })
        .join(' / ');
    }
    return String(response === null || response === undefined ? '' : response).trim();
  }

  /**
   * 「让 AI 讲讲这道题」。
   *
   * 把作答与结果写进一句**人话**发出去 —— 这是题卡与对话之间唯一的接缝，
   * 而且刻意不把正确答案写进去：AI 自己会用工具把题与答案取来
   * （`get_existing_questions(includeAnswer)`），上下文因此不白占。
   */
  function askAboutCard(question, response, result) {
    if (state.busy) return;
    var verdict =
      result.status === 'correct'
        ? '答对了'
        : result.status === 'partial'
          ? '只对了一部分'
          : result.status === 'ungraded'
            ? '还没批改'
            : '答错了';
    var mine = formatResponse(response);
    send({
      content:
        '这道题（' +
        question.id +
        '）我' +
        verdict +
        (mine ? '，我的作答是：' + mine : '') +
        '。讲讲这道题该怎么想。',
    });
  }

  /* ------------------------------------------------------------ 凭条 */

  /**
   * 待确认的动作凭条（收藏 / 已掌握）。
   *
   * 状态**从记录里读**，不另存一份 —— 所以刷新之后它自己就对上了。
   * 点一下走的是刷题页 / 错题本里那条老路（`store.toggleFlag` / `store.setMastered`），
   * 于是落盘、同步、幂等、跨设备这些都是现成的。
   *
   * 换句话说：这里是"AI 提议"的界面，但**改状态的那一下永远是人按的**。
   */
  function actionNode(part) {
    var host = h('div.chatreceipt__host');

    function paint() {
      ui.clear(host);
      host.appendChild(receiptBody(part, paint));
    }

    paint();
    return host;
  }

  function receiptBody(part, repaint) {
    var payload = part.payload || {};
    var questionId = String(payload.questionId || '');
    var box = h('div.chatreceipt');
    if (!questionId) return box;

    var records = QF.store && QF.store.records ? QF.store.records() : {};
    var record = records[questionId] || {};
    var mastered = payload.kind === 'mastered';
    var on = !!record[mastered ? 'mastered' : 'flagged'];

    var copy = mastered
      ? { ask: '要标成「已掌握」吗？', do: '标记掌握', state: '已标记掌握 · 错题本不再催它', undo: '取消这个标记' }
      : { ask: '要把这题收进收藏夹吗？', do: '加入收藏夹', state: '已在收藏夹里', undo: '移出收藏夹' };

    box.appendChild(
      h(
        'div.chatreceipt__head',
        null,
        h('span.chatreceipt__id', { text: questionId }),
        payload.layer ? h('span.chatreceipt__tag', { text: payload.layer }) : null,
        payload.wing ? h('span.chatreceipt__tag', { text: payload.wing }) : null
      )
    );

    box.appendChild(h('div.chatreceipt__text', { text: on ? copy.state : copy.ask }));
    box.appendChild(
      h(
        'div.chatreceipt__actions',
        null,
        h(
          'button.btn.btn--ghost.chatreceipt__go',
          {
            type: 'button',
            onClick: function () {
              applyReceipt(questionId, payload.kind, !on, repaint);
            },
          },
          on ? copy.undo : copy.do
        )
      )
    );
    return box;
  }

  function applyReceipt(questionId, kind, next, repaint) {
    // 这两条都是刷题页/错题本里用户在用的那两条老路，不另开一套写入口
    if (kind === 'mastered') QF.store.setMastered(questionId, next);
    else QF.store.toggleFlag(questionId);

    repaint();
    ui.toast(
      kind === 'mastered'
        ? next
          ? '已标记掌握'
          : '已取消标记'
        : next
          ? '已加入收藏夹'
          : '已移出收藏夹',
      'info',
      1500
    );
  }

  /* ------------------------------------------------------------ 零件 */

  /**
   * 一串零件 → DOM。
   *
   * 这一层就是"零件化"的全部意义所在：同一段回答里长出的是不同的东西
   * （它在想什么、它查了什么、它引了哪几行、它说了什么），
   * 前端按类型各画各的，而不是拿一段字符串去猜。
   */
  function partsNode(parts) {
    var box = h('div.chatmsg__parts');
    (parts || []).forEach(function (part) {
      var node = partNode(part);
      if (node) box.appendChild(node);
    });
    return box;
  }

  function partNode(part) {
    var type = part && part.type;
    if (type === 'text') return QF.md.render(String(part.text || ''));
    if (type === 'think') return thinkNode(part);
    if (type === 'tool_call') return toolNode(part);
    if (type === 'card') return questionCard(part);
    if (type === 'action') return actionNode(part);
    if (type === 'file') return fileNode(part);
    if (type === 'demo') return demoCard(part);
    if (type === 'citation') return citationNode(part);
    if (type === 'summary') return h('div.chatmsg__note', { text: part.text || '' });
    if (type === 'error') return h('div.chatmsg__why', { text: part.message || '出错了' });
    return null;
  }

  /** 推理：折起来。它是过程，不是结论 —— 想看的人点开，不想看的人不被它挤走。 */
  function thinkNode(part) {
    return h(
      'details.chatmsg__think',
      null,
      h('summary', { text: '思考过程' }),
      h('div.md', { html: QF.md.renderToString(String(part.text || '')) })
    );
  }

  /** 工具调用：一行摘要 + 可展开的结果。调用中与调用完是同一条，只是结果填进来了。 */
  function toolNode(part) {
    var head = h(
      'div.chatmsg__toolhead',
      null,
      h('span.chatmsg__toolname', { text: part.name || '工具' }),
      h('span.chatmsg__toolargs', { text: compactArgs(part.args) }),
      part.ms ? h('span.chatmsg__toolms', { text: part.ms + ' ms' }) : null,
      part.ok === false ? h('span.chatmsg__tag.is-bad', { text: '失败' }) : null
    );
    var tail = part.output
      ? h(
          'details.chatmsg__toolout',
          null,
          h('summary', { text: '结果' }),
          h('pre.chatmsg__toolpre', { text: part.output })
        )
      : h('div.chatmsg__toolwait', { text: '调用中…' });
    return h('div.chatmsg__tool' + (part.ok === false ? '.is-bad' : ''), null, head, tail);
  }

  /**
   * 引用：材料 + 行区间，**点开就是原文那几行**。
   *
   * 三件刻意的事：
   *
   * * **点开才拉**：折叠着只占一行，不预取（一条消息最多挂六处引用）。
   * * 拉的是**模型当时读的那几行** —— 服务端同一个 `read_lines`，
   *   所以"它引的"与"你看到的"逐字一致。引用能当证据，靠的就是这一条。
   * * 展开状态与正文缓存在模块里（按 材料+区间）：流式重画不会把它收回去，
   *   也不会重复拉。
   */
  var citeViews = {};

  function citeView(key) {
    if (!citeViews[key]) citeViews[key] = { open: false, loading: false, error: '', lines: [] };
    return citeViews[key];
  }

  function citationNode(part) {
    var slug = String(part.slug || '');
    var start = parseInt(part.startLine, 10) || 0;
    var end = parseInt(part.endLine, 10) || start;
    var key = slug + ':' + start + '-' + end;
    var host = h('div.chatcite__host');

    function paint() {
      ui.clear(host);
      host.appendChild(citeBody(part, key, slug, start, end, paint));
    }

    paint();
    return host;
  }

  function citeBody(part, key, slug, start, end, repaint) {
    var view = citeView(key);
    var box = h('div.chatcite' + (view.open ? '.is-open' : ''));

    box.appendChild(
      h(
        'button.chatcite__head',
        {
          type: 'button',
          onClick: function () {
            toggleCite(part, key, slug, start, end, view, repaint);
          },
        },
        h('span.chatcite__caret', { text: view.open ? '▾' : '▸' }),
        h('span.chatcite__slug', { text: slug || '材料' }),
        start ? h('span.chatcite__range', { text: start + '–' + end + ' 行' }) : null,
        view.loading ? h('span.chatcite__hint', { text: '读原文…' }) : null
      )
    );

    if (part.quote) box.appendChild(h('div.chatcite__quote', { text: part.quote }));
    if (view.error) box.appendChild(h('div.chatcite__error', { text: view.error }));

    if (view.open && view.lines.length) {
      box.appendChild(
        h(
          'pre.chatcite__pre',
          null,
          view.lines.map(function (item) {
            var quote = String(part.quote || '').trim();
            var hit = quote && String(item.text || '').trim() === quote;
            return h(
              'div.chatcite__line' + (hit ? '.is-hit' : ''),
              null,
              h('span.chatcite__no', { text: String(item.line) }),
              h('span.chatcite__text', { text: item.text })
            );
          })
        )
      );
    }
    return box;
  }

  function toggleCite(part, key, slug, start, end, view, repaint) {
    view.open = !view.open;
    repaint();
    if (!view.open || view.lines.length || view.loading || !slug) return;

    view.loading = true;
    repaint();
    QF.api
      .get(
        '/knowledge/material?slug=' +
          encodeURIComponent(slug) +
          '&start=' +
          (start || 1) +
          '&end=' +
          (end || 0)
      )
      .then(function (data) {
        view.loading = false;
        view.lines = (data && data.lines) || [];
        repaint();
      })
      .catch(function (err) {
        view.loading = false;
        view.error = (err && err.message) || '读不到原文';
        repaint();
      });
  }

  function compactArgs(args) {
    var bits = [];
    Object.keys(args || {}).forEach(function (key) {
      var value = args[key];
      if (value === null || value === undefined || value === '') return;
      if (typeof value === 'object') value = JSON.stringify(value);
      bits.push(key + '=' + String(value).slice(0, 40));
    });
    var text = bits.join(' ');
    return text.length > 70 ? text.slice(0, 70) + '…' : text;
  }

  /** 助手消息的尾巴：用量、状态标签、重试按钮 */
  function decorateAssistant(row, body, m) {
    if (m.role !== 'assistant') return;
    var foot = h('div.chatmsg__foot');

    if (m.status === 'partial') {
      foot.appendChild(h('span.chatmsg__tag', { text: '已中断' }));
      foot.appendChild(retryButton(m));
    } else if (m.status === 'error') {
      foot.appendChild(h('span.chatmsg__tag.is-bad', { text: '失败' }));
      if (m.error) foot.appendChild(h('span.chatmsg__why', { text: m.error }));
      foot.appendChild(retryButton(m));
    } else if (m.status === 'streaming' && !state.busy) {
      // 打开页面时看到的「还在生成」= 上一轮没收到收尾信号（服务端会在读的时候自愈，
      // 这里是同一件事的前端一侧：给个出口，别让它静止地转圈）
      foot.appendChild(h('span.chatmsg__tag', { text: '这一轮没有收尾' }));
      foot.appendChild(retryButton(m));
    }

    if (m.status === 'ok') {
      var bits = [];
      if (m.model) bits.push(m.model);
      if (m.latencyMs) bits.push((m.latencyMs / 1000).toFixed(1) + 's');
      if (m.promptTokens || m.completionTokens) {
        bits.push(m.promptTokens + '+' + m.completionTokens + ' tokens');
      }
      if (bits.length) foot.appendChild(h('span.chatmsg__meta', { text: bits.join(' · ') }));
      // 成功的回答也能重来一次 —— 没有这个按钮，"再生成一次"这条分支就造不出来，
      // 切换器也就永远只有 1 / 1（LibreChat 在每条回答下都放了这个入口）。
      // 显示为图标：页脚里已经有一行元信息（模型 · 耗时 · token），
      // 再加三个字就显得吵；图标与「复制」是同一套，安静且认得出来。
      foot.appendChild(
        iconButton('retry', '重新回答（会另开一条分支）', function () {
          if (state.busy) return;
          regenerate(m);
        })
      );
    }

    // 复制**正文**：工具调用与引用不复制（它们复制出去没用）。
    // 放在状态分支之外 —— 中断的回答、失败的半截，同样值得能复制走。
    var prose = proseOf(m);
    if (prose) {
      foot.appendChild(
        iconButton('copy', '复制这条回答', function () {
          copyText(prose, '回答已复制');
        })
      );
    }

    if (foot.childNodes.length) body.appendChild(foot);
  }

  function retryButton(m) {
    return iconButton('retry', '重新回答', function () {
      if (state.busy) return;
      regenerate(m);
    });
  }

  /* ------------------------------------------------------------ 流式 */

  /**
   * 正在长的那条消息。
   *
   * 增量不会每个字都重排一遍 Markdown（那会又抖又费）：攒到 100ms 才刷一次。
   * 结束时再无条件刷一次 —— 最后那段没到 100ms 的字同样要落进渲染里。
   */
  function makeLive(row, body, msg) {
    var parts = [];
    var timer = null;

    function render(immediate) {
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
      if (!immediate) {
        // 每来一个字就重排一遍 Markdown 会又抖又费，攒 100ms 刷一次
        timer = setTimeout(function () {
          render(true);
        }, 100);
        return;
      }
      var stick = nearBottom();
      ui.clear(body);
      body.appendChild(partsNode(parts));
      if (state.busy) body.appendChild(h('span.chat__caret'));
      if (stick) scrollToEnd(false);
    }

    return {
      row: row,
      body: body,
      msg: msg,
      parts: function () {
        return parts;
      },
      pushText: function (chunk) {
        pushPart(parts, 'text', chunk);
        render(false);
        scrollToEnd(false);
      },
      pushThink: function (chunk) {
        pushPart(parts, 'think', chunk);
        render(false);
      },
      pushNote: function (text) {
        parts.push({ type: 'summary', text: text });
        render(true);
      },
      // 工具事件立刻刷：用户最想知道的正是"它现在在干什么"，等 100ms 就没意思了
      toolStart: function (data) {
        parts.push({
          type: 'tool_call',
          id: data.callId,
          name: data.name,
          args: data.args || {},
          output: '',
          ok: true,
          ms: 0,
        });
        render(true);
      },
      toolResult: function (data) {
        for (var i = parts.length - 1; i >= 0; i--) {
          if (parts[i].type === 'tool_call' && parts[i].id === data.callId) {
            parts[i].output = data.output || '';
            parts[i].ok = data.ok !== false;
            parts[i].ms = data.ms || 0;
            break;
          }
        }
        render(true);
      },
      // 卡片一到就立刻画：用户最想马上做的就是动手答
      pushCard: function (card) {
        parts.push({ type: 'card', kind: 'question', payload: card });
        render(true);
      },
      // 凭条也一样：它得在回答说完之前就能点（不然用户干等）
      pushAction: function (proposal) {
        parts.push({ type: 'action', kind: proposal.kind, payload: proposal });
        render(true);
      },
      pushPart: function (part) {
        parts.push(part);
        render(true);
      },
      settle: function (m) {
        if (m && m.parts && m.parts.length) parts = m.parts;
        row.dataset.id = String((m && m.id) || '');
        row.classList.remove('is-error');
        if (m && m.status === 'error') row.classList.add('is-error');
        ui.clear(body);
        body.appendChild(partsNode(parts));
        decorateAssistant(row, body, m || { role: 'assistant', status: 'partial' });
        scrollToEnd(false);
      },
      text: function () {
        return textOf(parts);
      },
    };
  }

  /** 同类相邻的增量并成一个零件（与后端 `_push_text` 同一套规矩）。 */
  function pushPart(parts, type, chunk) {
    if (parts.length && parts[parts.length - 1].type === type) {
      parts[parts.length - 1].text = String(parts[parts.length - 1].text || '') + chunk;
    } else {
      parts.push({ type: type, text: chunk });
    }
  }

  function textOf(parts) {
    var chunks = [];
    (parts || []).forEach(function (part) {
      if (part && part.type === 'text') chunks.push(String(part.text || ''));
    });
    return chunks.join('\n').trim();
  }

  /** 用户是不是贴着底看（贴着才跟着滚，否则会把人从旧消息里拽走）。 */
  function nearBottom() {
    if (!threadEl) return true;
    return threadEl.scrollHeight - threadEl.scrollTop - threadEl.clientHeight < 120;
  }

  /* ------------------------------------------------------------ 附件 */

  /**
   * 选文件 → 立刻上传 → 变成一枚待发标签。
   *
   * 为什么"先传后发"而不是跟着消息一起传：发送那条路是 SSE 流，
   * 在流里同时收文件、校验、落盘，会把"流"和"非流"搅在一起
   * （见后端 `upload_attachment` 的说明）。所以这里传完只留一个 id。
   */
  function onPickFiles() {
    var files = Array.prototype.slice.call((clipInput && clipInput.files) || []);
    if (!files.length) return;
    clipInput.value = '';
    files.slice(0, 8).forEach(function (file) {
      api
        .upload('/chat/attachments', file)
        .then(function (info) {
          state.pending.push(info);
          renderPending();
          ui.toast(
            '已附上 ' + info.name + (info.textChars ? '（抽出 ' + info.textChars + ' 字给 AI）' : ''),
            'ok',
            1800
          );
        })
        .catch(function (err) {
          ui.toast(file.name + '：' + (err.message || '上传失败'), 'error', 6000);
        });
    });
  }

  function attachmentMeta(info) {
    var kind = info.kind === 'image' ? '图片' : info.kind === 'pdf' ? 'PDF' : info.kind === 'text' ? '文本' : '文件';
    var kb = Math.max(1, Math.round((info.size || 0) / 1024));
    return kind + ' · ' + kb + 'KB' + (info.textChars ? ' · 抽出 ' + info.textChars + ' 字' : '');
  }

  function renderPending() {
    if (!pendingEl) return;
    ui.clear(pendingEl);
    if (!state.pending.length) return;
    state.pending.forEach(function (info, index) {
      pendingEl.appendChild(
        h(
          'div.chat__chip',
          null,
          h('span.chat__chipname', { text: info.name }),
          h('span.chat__chipmeta', { text: attachmentMeta(info) }),
          h('button.chat__chipdrop', {
            type: 'button',
            title: '去掉',
            text: '×',
            onClick: function () {
              state.pending.splice(index, 1);
              renderPending();
            },
          })
        )
      );
    });
  }

  function send(options) {
    var opts = options || {};
    var text = String(opts.content || '').trim();
    if (!text && !opts.replyTo) return Promise.resolve();
    if (state.busy) return Promise.resolve();

    return ensureConversation().then(function () {
      // 新消息挂在**当前这一支的末尾**（用户翻到旧分支上接着问，就该挂在那儿）；
      // 「编辑并重发」则显式指定父节点 —— 判据是「parentId 键在不在」，不是值真不真，
      // 因为编辑第一条消息时它的父节点**就是** null
      var path = activePath();
      var leaf = path.length ? path[path.length - 1].id : null;
      var hasParent = Object.prototype.hasOwnProperty.call(opts, 'parentId');
      var attachTo = hasParent ? opts.parentId : leaf;
      if (!opts.replyTo && !hasParent) delete state.picks[keyOf(leaf)];

      var local = null;
      var localId = '';
      if (text) {
        local = {
          id: 'local-' + ++LOCAL_ID,
          role: 'user',
          content: text,
          status: 'ok',
          parentId: attachTo,
        };
        localId = local.id;
        state.messages.push(local);
        if (threadEl.querySelector('.chat__intro')) ui.clear(threadEl);
        threadEl.appendChild(messageRow(local));
        inputEl.value = '';
        growInput();
        scrollToEnd(true);
      }

      state.busy = true;
      state.controller = new AbortController();
      updateComposer();
      scrollToEnd(true);

      var settled = false;

      var attachedIds = state.pending.map(function (info) {
        return info.id;
      });
      if (attachedIds.length) {
        state.pending = [];
        renderPending();
      }
      var body = opts.replyTo
        ? { replyTo: opts.replyTo }
        : { content: text, parentId: attachTo, attachments: attachedIds };
      return api
        .stream(
          '/chat/conversations/' + state.current + '/messages',
          body,
          {
            user: function (m) {
              // 服务端落库后的那份替换掉乐观节点（同一条 DOM，不闪）
              if (!local) return;
              local.id = m.id;
              local.parentId = m.parentId;
              // 新落的这条现在是它父节点下的选择 —— 编辑并重发之后，
              // 当前分支必须跟着走到新那条，而不是留着旧的那条
              state.picks[keyOf(attachTo)] = m.id;
              // 附件零件是服务端落库时挂上去的，本地的乐观节点还没有它 ——
              // 不带过来的话，消息要等下次刷新才看得到附件
              if (m.parts && m.parts.length) {
                local.parts = m.parts;
                var old = threadEl.querySelector('[data-id="' + localId + '"]');
                if (old && old.parentNode) old.parentNode.replaceChild(messageRow(local), old);
              }
              local.createdAtMs = m.createdAtMs;
              var node = threadEl.querySelector('[data-id="' + localId + '"]');
              if (node) node.dataset.id = String(m.id);
            },
            start: function (m) {
              settled = false;
              state.messages.push(m);
              var row = messageRow(m);
              threadEl.appendChild(row);
              state.live = makeLive(row, row.querySelector('.chatmsg__body'), m);
              scrollToEnd(true);
            },
            delta: function (d) {
              if (state.live && d && d.text) state.live.pushText(d.text);
            },
            think: function (d) {
              if (state.live && d && d.text) state.live.pushThink(d.text);
            },
            note: function (d) {
              if (state.live && d && d.text) state.live.pushNote(d.text);
            },
            tool: function (d) {
              if (!state.live || !d) return;
              if (d.phase === 'start') state.live.toolStart(d);
              else state.live.toolResult(d);
            },
            card: function (d) {
              if (state.live && d && d.card) state.live.pushCard(d.card);
            },
            action: function (d) {
              if (state.live && d && d.proposal) state.live.pushAction(d.proposal);
            },
            citation: function (d) {
              if (state.live && d && d.citation) state.live.pushPart(d.citation);
            },
            demo: function (d) {
              if (!state.live || !d || !d.demo) return;
              state.live.pushPart({ type: 'demo', title: d.demo.title, html: d.demo.html });
            },
            done: function (m) {
              settled = true;
              if (state.live) state.live.settle(m);
              replaceMessage(m);
              state.live = null;
            },
            error: function (e) {
              settled = true;
              if (state.live) {
                var failed = {};
                Object.keys(state.live.msg || {}).forEach(function (k) {
                  failed[k] = state.live.msg[k];
                });
                var why = (e && (e.detail || e.message)) || '生成失败';
                failed.parts = state.live.parts().concat([{ type: 'error', message: why }]);
                failed.content = state.live.text();
                failed.status = 'error';
                failed.error = why;
                state.live.settle(failed);
                replaceMessage(failed);
              }
              state.live = null;
            },
          },
          { signal: state.controller.signal }
        )
        .catch(function (err) {
          // 开流之前就被拦下的情况（没填密钥、超配额）：这时**没有**任何消息落库，
          // 所以把用户刚打的字还回输入框，让他改完设置直接重发
          if (local) {
            state.messages = state.messages.filter(function (m) {
              return m !== local;
            });
            inputEl.value = text;
            growInput();
          }
          ui.toast(err.message, 'error');
          paintThread();
          // 失败常常是因为"刚填好密钥 / 本地模型刚起来"，顺手重问一次通道状态
          refreshAiState();
        })
        .then(function () {
          state.busy = false;
          state.controller = null;
          if (!settled && state.live) {
            // 用户按了停止：前端这边先把它收成「已中断」。
            // 从 live.msg 拷一份（而不是新建对象）—— 那样才带着 id 与 parentId，
            // 否则停下之后立刻点「重新回答」会点不动（它要靠 parentId 回问那条提问）
            var live = state.live;
            var last = {};
            Object.keys(live.msg || {}).forEach(function (k) {
              last[k] = live.msg[k];
            });
            last.content = live.text();
            last.parts = live.parts();
            last.status = 'partial';
            live.settle(last);
            replaceMessage(last);
            state.live = null;
            // 并且明确告诉服务端一声：它自己感知不到客户端断开
            // （见 api/app/routers/chat.py 的「已知边界」）。失败也无所谓 ——
            // 读会话时的自愈会补上这个状态。
            if (last.id) {
              api
                .post('/chat/conversations/' + state.current + '/messages/' + last.id + '/stop')
                .catch(function () {});
            }
          }
          updateComposer();
          // 重生成 / 编辑并重发之后都要重画：新分支成了当前这一支，旧那条退到切换器后面去。
          // 不重画的话，界面会同时留着两条（它们现在是兄弟，不是一条线上的两条）
          if (opts.replyTo || hasParent) paintThread();
          return loadList();
        });
    });
  }

  function regenerate(m) {
    if (!m || !m.parentId) return;
    // 旧那条**留着**（它是树上的一个分支，随时能翻回去看）。把这一层的选择清掉，
    // 于是新答案一落地就自然成为当前这一支 —— 不需要额外告诉界面"看新的"
    delete state.picks[keyOf(m.parentId)];
    send({ replyTo: m.parentId });
  }

  function replaceMessage(m) {
    for (var i = state.messages.length - 1; i >= 0; i--) {
      if (state.messages[i] && state.messages[i].id === m.id) {
        state.messages[i] = m;
        return;
      }
    }
    state.messages.push(m);
  }

  /* ------------------------------------------------------------ 交互 */

  function updateComposer() {
    if (!sendBtn) return;
    if (state.busy) {
      sendBtn.textContent = '停止';
      sendBtn.classList.remove('btn--primary');
      hintEl.textContent = '正在生成…（点「停止」会留下已经生成的部分）';
      return;
    }

    sendBtn.textContent = '发送';
    sendBtn.classList.add('btn--primary');

    var bits = [];
    if (!state.current) bits.push('第一句话会开一条新对话');
    var mode = aiState && aiState.mode;
    if (mode === 'beta') {
      // 说清"现在是谁在回答"：内测通道答得不理想时，用户要知道换模型的办法
      bits.push((aiState.label || '内测通道') + '（想换：设置 → AI 填自己的密钥）');
    } else if (mode === 'user' && aiState.model) {
      bits.push('模型：' + aiState.model);
    }
    hintEl.textContent = bits.join(' · ');
  }

  /**
   * 读一次"现在能不能用 AI、走哪条通道"，并把结果画成一条提示。
   *
   * 这一条是给"为什么我发不出去"这个问题准备的答案：
   * 不能用的时候，不能只让用户对着一个发不出消息的输入框发呆。
   */
  function refreshAiState() {
    return api
      .get('/ai/usage')
      .then(function (data) {
        aiState = data;
        renderNotice();
        updateComposer();
      })
      .catch(function () {
        // 读不到就当未知：不画提示，也别拦着用户用
      });
  }

  function renderNotice() {
    if (!noticeEl) return;
    ui.clear(noticeEl);
    var mode = aiState && aiState.mode;
    // `mode` 的取值：user（自己的密钥）/ beta（本站内测通道）/ off（AI 关着）/ none（都不行）。
    // 内测通道能答就别挂横幅 —— 这条提示原先会把 mode='beta' 也算成"不能对话"，
    // 于是开了内测的用户一直在被劝去填密钥。
    if (!mode || mode === 'user' || mode === 'beta' || mode === 'local') return;

    var text =
      mode === 'off'
        ? '本站已关闭 AI 功能，对话暂时不可用。'
        : '还不能对话：本站内测通道未就绪，去设置里填自己的 API 密钥即可使用。';
    noticeEl.appendChild(
      h(
        'div.chat__noticebox',
        null,
        h('span.chat__noticetext', { text: text }),
        h(
          'button.btn.chat__noticebtn',
          {
            type: 'button',
            onClick: function () {
              if (QF.settings && QF.settings.open) QF.settings.open();
            },
          },
          '打开设置'
        )
      )
    );
  }

  function onSendClick() {
    if (state.busy) {
      if (state.controller) state.controller.abort();
      return;
    }
    send({ content: inputEl.value });
  }

  function onKeydown(event) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      onSendClick();
    }
  }

  function growInput() {
    inputEl.style.height = 'auto';
    inputEl.style.height = Math.min(inputEl.scrollHeight, 220) + 'px';
  }

  function scrollToEnd(force) {
    if (!threadEl) return;
    var gap = threadEl.scrollHeight - threadEl.scrollTop - threadEl.clientHeight;
    // 只在贴着底的时候跟着滚：用户往上翻着读旧消息时，不该被新字拽回去
    if (force || gap < 120) threadEl.scrollTop = threadEl.scrollHeight;
    refreshJump();
  }

  /**
   * 「回到最新」按钮：浮在输入框上沿的右下角，滚上去之后才出现。
   *
   * 为什么要有它：长回答能把对话拉得很长，往上翻几屏之后想回到底部，
   * 只能一路滚（或者去点输入框再敲字）。它是个纯导航件，
   * 所以只在**离开底部**时显形，贴着底的时候不占视线。
   */
  function refreshJump() {
    if (!jumpBtn || !threadEl) return;
    var gap = threadEl.scrollHeight - threadEl.scrollTop - threadEl.clientHeight;
    jumpBtn.classList.toggle('is-on', gap > 160);
  }

  function ensureConversation() {
    if (state.current) return Promise.resolve(state.current);
    return api.post('/chat/conversations', {}).then(function (res) {
      state.current = res.conversation.id;
      state.messages = [];
      state.list.unshift(res.conversation);
      renderAside();
      updateComposer();
      return state.current;
    });
  }

  function openConversation(id) {
    return api
      .get('/chat/conversations/' + id)
      .then(function (res) {
        state.current = id;
        state.messages = (res && res.messages) || [];
        state.picks = {}; // 默认跟最新那一支
        state.live = null;
        renderAside();
        paintThread();
        updateComposer();
      })
      .catch(function (err) {
        ui.toast(err.message, 'error');
        // 会话可能已经被删了：回到一个干净的空态，而不是停在一个报错上
        state.current = null;
        state.messages = [];
        renderAside();
        paintThread();
      });
  }

  function onNewClick() {
    if (state.busy) return;
    state.current = null;
    state.messages = [];
    state.live = null;
    renderAside();
    paintThread();
    updateComposer();
    inputEl.focus();
  }

  /** 置顶/取消置顶。本地立刻重排一次 —— 别等下一次拉列表才动。 */
  function togglePin(conv) {
    var next = !conv.pinned;
    api
      .patch('/chat/conversations/' + conv.id, { pinned: next })
      .then(function () {
        conv.pinned = next;
        state.list = state.list.slice().sort(function (a, b) {
          if (!!a.pinned !== !!b.pinned) return a.pinned ? -1 : 1;
          return (b.updatedAtMs || 0) - (a.updatedAtMs || 0);
        });
        renderAside();
        ui.toast(next ? '已置顶到这个列表最上面' : '已取消置顶', 'info', 1400);
      })
      .catch(function (err) {
        ui.toast(err.message, 'error');
      });
  }

  /** 触发一次下载。走 `<a download>`：同源带 cookie，服务端给的是 attachment 头。 */
  function download(path) {
    var link = h('a', { href: path, download: '' });
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  function openMenu(conv) {
    var field = h('input.input', { type: 'text', value: conv.title || '' });
    var exportRow = h(
      'div.field',
      null,
      h('div.field__label', { text: '导出' }),
      h(
        'div.chat__exports',
        null,
        h(
          'button.btn.btn--ghost',
          {
            type: 'button',
            onClick: function () {
              download('/api/chat/conversations/' + conv.id + '/export?format=md');
            },
          },
          'Markdown（当前分支，给人读）'
        ),
        h(
          'button.btn.btn--ghost',
          {
            type: 'button',
            onClick: function () {
              download('/api/chat/conversations/' + conv.id + '/export?format=json');
            },
          },
          'JSON（整棵树，给机器读）'
        ),
        h(
          'button.btn.btn--ghost',
          {
            type: 'button',
            onClick: function () {
              download('/api/chat/export');
            },
          },
          '全部对话（完整轨迹）'
        )
      )
    );
    var modal = ui.modal({
      title: '对话设置',
      size: 'sm',
      body: h(
        'div.form',
        null,
        h('div.field', null, h('div.field__label', { text: '标题' }), field),
        exportRow
      ),
      actions: [
        {
          label: '删除',
          kind: 'danger',
          onClick: function () {
            modal.close();
            removeConversation(conv);
          },
        },
        {
          label: '保存',
          kind: 'primary',
          onClick: function () {
            var title = String(field.value || '').trim();
            modal.close();
            if (!title || title === conv.title) return;
            api
              .patch('/chat/conversations/' + conv.id, { title: title })
              .then(function () {
                conv.title = title;
                renderAside();
              })
              .catch(function (err) {
                ui.toast(err.message, 'error');
              });
          },
        },
      ],
    });
    setTimeout(function () {
      field.focus();
      field.select();
    }, 40);
  }

  function removeConversation(conv) {
    ui.confirm('删除后无法恢复，里面的消息会一起删掉。', {
      title: '删除「' + (conv.title || '未命名对话') + '」？',
      okLabel: '删除',
      danger: true,
    }).then(function (ok) {
      if (!ok) return;
      api
        .del('/chat/conversations/' + conv.id)
        .then(function () {
          state.list = state.list.filter(function (c) {
            return c.id !== conv.id;
          });
          if (state.current === conv.id) {
            state.current = null;
            state.messages = [];
            paintThread();
            updateComposer();
          }
          renderAside();
        })
        .catch(function (err) {
          ui.toast(err.message, 'error');
        });
    });
  }

  /* ------------------------------------------------------------ 启动 */

  function boot() {
    rootEl = document.getElementById('app-root');
    if (!rootEl) return;
    if (chat.booted) return; // 幂等：boot.js 与 DOMContentLoaded 都可能触发

    ui.theme.init();
    // Esc 逐层关：演示面板 → 对话树 → 就地编辑。
    // 一律从"最上面那一层"关起，不然按一下把底下的编辑器也关了，人会愣一下
    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape') return;
      if (state.demo) {
        state.demo = null;
        renderDemoPanel();
      } else if (state.treeOpen) {
        state.treeOpen = false;
        renderBar();
        renderTree();
      } else if (state.editing) {
        state.editing = 0;
        paintThread();
      }
    });
    var conf = QF.store.settings();
    document.documentElement.style.setProperty('--font-scale', String(conf.fontScale || 1));
    document.documentElement.style.setProperty('--content-max', (conf.maxWidth || 880) + 'px');

    // 主题按钮、设置按钮、当前页图标都由 shell.js 统一接线（全站一份）
    QF.shell.mount({});

    buildSkeleton();
    renderAside();
    renderEmptyThread();
    updateComposer();
    // 顺手问一次"现在能不能用 AI"：不能用的原因要当场说清，而不是等用户打了一段字才报错
    refreshAiState();

    // 从别的页面「带着上下文」跳过来：?ask= 预填问题，?c= 直接打开某个会话
    var params = new URLSearchParams(location.search);
    var ask = params.get('ask');
    var want = params.get('c');
    if (ask) {
      inputEl.value = ask;
      growInput();
    }

    loadList().then(function () {
      var target = null;
      if (want) {
        target = state.list.filter(function (c) {
          return c.id === want;
        })[0];
      }
      if (!target && !ask && state.list.length) target = state.list[0];
      if (target) return openConversation(target.id);
      return undefined;
    }).then(function () {
      if (ask) inputEl.focus();
    });

    chat.booted = true;
  }

  var chat = { boot: boot, booted: false };
  QF.chat = chat;
})();
