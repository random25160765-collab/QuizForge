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
  //: 是否"跟着新内容往下滚"。**只有用户自己往上看时才关掉**（见 `scrollToEnd`
  //: 与那条 scroll 监听）—— 他滚回贴底处再自动打开。发消息、换会话这类动作
  //: 走 `scrollToEnd(true)`，会强制恢复跟随。
  var stickBottom = true;
  var noticeEl = null;
  var jumpBtn = null;
  var notesEl = null;
  var problemEl = null;
  var problemBtn = null; // 工具栏那个「大题」按钮：开合要反映在它身上
  var inputEl = null;
  var sendBtn = null;
  var deepBtn = null; // 输入框里那颗「深度思考」药丸：切换要反映在它身上
  var hintEl = null;
  var barEl = null;
  var chatEl = null;
  //: 会话管理栏开着没有。`open | closed` —— 收起来之后正文独占整宽，
  //: 与笔记页的文件面板、资料页的两栏是同一套做法（状态记本机）。
  var asideOpen = true;
  var treeEl = null;
  var demoEl = null;
  var clipInput = null;
  var pendingEl = null;
  var pickEl = null; // 左栏头部那条批量操作条（多选时出现；见 renderPickBar）

  /** `/api/ai/usage` 的结果：走哪条通道、能不能用（决定提示怎么写） */
  var aiState = null;

  var state = {
    folders: [],        // 分组（后端一次给全，左栏那棵树要用）
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
    showThink: false, // 是否展开思考（顶栏那颗思考按钮；见 loadThink）
    // 「深度思考」：这一条消息要不要让模型先思考再答（输入框里那颗药丸）。
    // **默认开** —— 默认模型是 `deepseek-flash`，思考是它的常态；关掉时后端会给
    // 上游显式传 `thinking: disabled`（见 gateway.thinking_params）。
    deepThink: true,
    // 归档区的**文件管理**：多选与排序（见 renderZones 的"归档"段）
    picked: {}, // { 会话 id: true }
    lastPickId: 0, // Shift 范围选的锚点（上一次点的是哪条）
    visibleIds: [], // 上一次画出来的行顺序 —— 范围选要在"可见顺序"里取区间
    sortMode: 'time', // 归档区排序：time | name | count（存本机）
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
    w: 268, // 节点宽（每行按实测宽度切，见 treeLines）
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
  /* ------------------------------------------------- 模式（能力挂载）的变化 */

  /** 这条消息当时挂载了哪几组工具。**老消息没有这个字段** → 空数组 = 不知道。 */
  function modeKeys(message) {
    return Array.isArray(message.mounts) ? message.mounts.slice().sort() : [];
  }

  /** 组名：`asr` 这种 key 是给程序看的，树里要写人话（取自挂载那排开关的同一份数据）。
   *  顺序也**按那排开关的次序**排 —— 按字母排会读成"图谱 + 资料 + 笔记"，
   *  与用户眼前那排图标的顺序对不上。 */
  /** 这条消息是哪个模式（药丸/信号灯的颜色靠它）。
   *
   * 输入锚定模式：消息上记着"那一刻挂了哪几组"，拿去与模式表对一下就知道是哪个。
   * 对不上任何预设 = 自定义（颜色落到中性灰）；老消息没记过 = 极简那一档。
   */
  /** 此刻的模式键（`QF.mounts` 是权威，见 mounts.js）。 */
  function currentMode() {
    var st = (QF.mounts && QF.mounts.state) || {};
    return st.mode || 'minimal';
  }

  /** 当前模式对应哪几组（本地新消息照着记一份，形状与服务端落库那份一致）。 */
  function currentModeGroups() {
    var st = (QF.mounts && QF.mounts.state) || {};
    var table = st.modes || [];
    for (var i = 0; i < table.length; i++) {
      if (table[i].key === st.mode) return (table[i].groups || []).slice();
    }
    return [];
  }

  function modeKeyOf(message) {
    var st = (QF.mounts && QF.mounts.state) || {};
    // **刚发出去的那条还没有 mounts**：服务端是落库那一刻才记的快照，而屏幕上这条
    // 是本地先画出来的（`local = {id, role, content, status, parentId}`）。这时要用
    // **当前模式**，不能落回 `minimal` —— 否则在查询模式下发一条，灯是灰的
    //（用户："我在查询模式下发消息，输入框右上角的灯没变"）。
    // 同一个函数还管着对话树的节点边框，所以这里一改，那边也就跟着对了。
    if (!message || !Array.isArray(message.mounts)) return st.mode || 'minimal';
    var want = modeKeys(message);
    var table = st.modes || [];
    for (var i = 0; i < table.length; i++) {
      var groups = (table[i].groups || []).slice().sort();
      if (groups.length === want.length && groups.join(',') === want.join(',')) {
        return table[i].key;
      }
    }
    return want.length ? 'custom' : 'minimal';
  }

  function modeLabel(keys) {
    if (!keys.length) return '极简（不挂工具）';
    var groups = (QF.mounts && QF.mounts.state && QF.mounts.state.groups) || [];
    var byKey = {};
    var order = [];
    groups.forEach(function (one) {
      byKey[one.key] = one.label;
      order.push(one.key);
    });
    return keys
      .slice()
      .sort(function (a, b) {
        return order.indexOf(a) - order.indexOf(b);
      })
      .map(function (key) {
        return byKey[key] || key;
      })
      .join(' + ');
  }

  /**
   * 这一条是不是"模式变了"的那一条。
   *
   * 判据是**与前一条用户消息比**：一轮对话用的是按下发送那一刻的挂载集，
   * 所以模式的变化只可能发生在用户消息上（助手消息继承它父节点那一轮）。
   * 挂了什么、改了几次，回看时全靠它 —— 同一句话，挂了资料库和只有极简模式，
   * 答案的口径完全不同。
   *
   * 第一次记录（前一条没有这个字段）也算"变了"：那是这套记录的开始，值得标出来。
   */
  function modeChanged(message) {
    if (message.role !== 'user' || !Array.isArray(message.mounts)) return false;
    var prev = null;
    state.messages.forEach(function (one) {
      if (one.role !== 'user' || !one.id || one.id >= message.id) return;
      if (!prev || one.id > prev.id) prev = one;
    });
    if (!prev || !Array.isArray(prev.mounts)) return true;
    return modeKeys(prev).join(',') !== modeKeys(message).join(',');
  }

  /** 这一条节点上要写的那行模式说明（没变化就是空串） */
  function modeNote(message) {
    return modeChanged(message) ? '◆ 模式：' + modeLabel(modeKeys(message)) : '';
  }

  function treeLines(message) {
    var text = String(message.content || '').replace(/\s+/g, ' ').trim();
    if (!text) {
      var kinds = (message.parts || []).map(function (part) {
        return part.type === 'card' ? '题卡' : part.type === 'file' ? '附件' : part.type === 'demo' ? '演示' : part.type === 'tool_call' ? '工具' : part.type === 'action' ? '凭条' : null;
      }).filter(Boolean);
      text = kinds.length ? '（' + kinds.join(' · ') + '）' : message.status === 'error' ? '（失败）' : '（空）';
    }
    // 按**整行的实测宽度**切，不按字数 —— 字数治不了：中英混排、标点、
    // 系统字体各占多少宽都不一样，前两版都栽在这儿（用户两次都看到越界）。
    // 量不了（极老的浏览器）就退回按 11px 估一个保守值。
    var font = '11px -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif';
    var limit = TREE.w - 40; // 左右各留 12px 内边距 + 右边一点余量
    var widthOf = (function () {
      var probe = null;
      try {
        probe = document.createElement('canvas').getContext('2d');
        probe.font = font;
      } catch (err) {
        probe = null;
      }
      return function (line) {
        if (!probe) return line.length * 11;
        return probe.measureText(line).width;
      };
    })();

    var lines = [];
    var rest = text;
    while (rest && lines.length < 3) {
      var take = 1;
      while (take < rest.length && widthOf(rest.slice(0, take + 1)) <= limit) take++;
      lines.push(rest.slice(0, take));
      rest = rest.slice(take);
    }
    if (rest && lines.length) {
      // 还有剩的：最后一行收成省略号（省略号本身也要放得下）
      var last = lines[lines.length - 1];
      while (last.length > 1 && widthOf(last + '…') > limit) last = last.slice(0, -1);
      lines[lines.length - 1] = last + '…';
    }
    return lines.length ? lines : ['（空）'];
  }

  function treeNodeHeight(message) {
    // 模式那一行也算进高度：不算的话它会被压在节点框外面（或者被裁掉）
    return 30 + treeLines(message).length * 15 + (modeNote(message) ? 16 : 0);
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
        mode: modeNote(message),
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
    // 纵向时从节点**底边**出发。高度取不到就现算一次 ——
    // 拿不到就会算出 NaN 的起点 y，线画不出来（用户说的"纵向连线是断的"）。
    var ah = a.h || (a.message ? treeNodeHeight(a.message) : 0);
    if (TREE.dir === 'h') {
      var x1 = a.x + TREE.w;
      var y1 = a.y;
      var x2 = b.x;
      var y2 = b.y;
      var mid = (x2 - x1) / 2;
      return 'M' + x1 + ' ' + y1 + 'C' + (x1 + mid) + ' ' + y1 + ',' + (x2 - mid) + ' ' + y2 + ',' + x2 + ' ' + y2;
    }
    // 子节点也要高度（`bh`）——上面那句只补了父节点，纵向时这里会 ReferenceError
    var bh = b.h || (b.message ? treeNodeHeight(b.message) : 0);
    var vy1 = a.y + ah / 2;
    var vx1 = a.x + TREE.w / 2;
    var vy2 = b.y - bh / 2;
    var vx2 = b.x + TREE.w / 2;
    var vmid = (vy2 - vy1) / 2;
    return 'M' + vx1 + ' ' + vy1 + 'C' + vx1 + ' ' + (vy1 + vmid) + ',' + vx2 + ' ' + (vy2 - vmid) + ',' + vx2 + ' ' + vy2;
  }

  /** 主区顶上那个入口：显示这棵树有多大、几处分叉。 */
  /** 会话管理栏的开合。收/展两个朝向之间**转过去**，与笔记页那颗同一套。 */
  function toggleAside() {
    asideOpen = !asideOpen;
    if (chatEl) chatEl.setAttribute('data-aside', asideOpen ? 'open' : 'closed');
    try {
      window.localStorage.setItem('qf.chat.aside', asideOpen ? 'open' : 'closed');
    } catch (err) {
      /* 存不下就只在这次生效 */
    }
    renderBar();
  }

  /* ------------------------------------------------------ 会话栏宽度（可拖） */

  //: 拖过之后的宽度记在本机。`0` = 没拖过 → 用 CSS 里的默认值（主页面 248 / 窗格 264）。
  var ASIDE_W_KEY = 'qf.chat.aside.w';
  var ASIDE_W_MIN = 180;
  var ASIDE_W_MAX = 520;

  function asideWidthNow() {
    try {
      var raw = parseInt(window.localStorage.getItem(ASIDE_W_KEY) || '', 10);
      return isFinite(raw) && raw > ASIDE_W_MIN ? raw : 0;
    } catch (err) {
      return 0;
    }
  }

  /** 设宽度。`px = 0` = 复位（回到 CSS 默认值）。
   *
   * 宽度只落在 `--chat-aside-w` 这一个变量上（CSS 里两处栅格都读它）——
   * 不在 JS 里直接改 grid-template-columns，那样收起/展开那几条规则就管不住它了。
   * 拖动过程中**不落库**，松手才写本机（每动一下写一次没必要）。
   */
  function setAsideWidth(px, save) {
    var root = document.documentElement;
    if (!px) {
      root.style.removeProperty('--chat-aside-w');
    } else {
      root.style.setProperty(
        '--chat-aside-w',
        Math.max(ASIDE_W_MIN, Math.min(ASIDE_W_MAX, Math.round(px))) + 'px'
      );
    }
    if (!save) return;
    try {
      if (px) window.localStorage.setItem(ASIDE_W_KEY, String(Math.round(px)));
      else window.localStorage.removeItem(ASIDE_W_KEY);
    } catch (err) {
      /* 存不下就只在这次生效 */
    }
  }

  /** 抓住那条右边线左右拖。 */
  function dragAsideWidth(ev) {
    ev.preventDefault();
    var startX = ev.clientX;
    var startW = asideEl ? asideEl.getBoundingClientRect().width : 0;
    var move = function (e) {
      setAsideWidth(startW + (e.clientX - startX), false);
    };
    var up = function () {
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', up);
      document.body.classList.remove('is-colresize');
      setAsideWidth(asideEl ? asideEl.getBoundingClientRect().width : 0, true);
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', up);
    // 拖的时候整页都用拖拽光标（不然移出手柄就变回箭头，像断了一样）
    document.body.classList.add('is-colresize');
  }

  // ⌘/Ctrl+B = 收起 / 展开会话栏（与编辑器、VS Code 同一套手感）。
  // 用户："这个东西的收起展开没有做 ^B 绑定。"
  //
  // 让路规则：焦点在**可编辑区域**里时不抢（contenteditable 将来若有自己的 ⌘B ——
  // 加粗之类 —— 该归它）。输入框/文本域不算可编辑区（这一页它们没有 ⌘B 的用法）。
  document.addEventListener('keydown', function (ev) {
    if (!(ev.metaKey || ev.ctrlKey) || ev.altKey || ev.shiftKey) return;
    if (ev.key !== 'b' && ev.key !== 'B') return;
    var el = document.activeElement;
    if (el && el.isContentEditable) return;
    ev.preventDefault();
    toggleAside();
  });

  function renderBar() {
    if (!barEl) return;
    ui.clear(barEl);

    // 会话栏的开合放在**最左边**：它管的就是左边那一栏（与笔记页的文件面板同一处位置）。
    barEl.appendChild(
      h(
        'button.chat__asidefold' + (asideOpen ? '.is-on' : ''),
        {
          type: 'button',
          title: (asideOpen ? '收起会话栏' : '展开会话栏') + '（⌘/Ctrl+B）',
          'aria-label': asideOpen ? '收起会话栏' : '展开会话栏',
          'aria-expanded': asideOpen ? 'true' : 'false',
          // 从这一颗上按下拖动，会把**整栏**拖成幽灵（用户："我拖动这个东西，
          // 竟然把这一整块都拖动了"）。按钮不该参与任何拖拽。
          draggable: 'false',
          onDragstart: function (ev) {
            ev.preventDefault();
            ev.stopPropagation();
          },
          onClick: toggleAside,
        },
        // 图标当**子节点**塞进去。踩过：`iconNode()` 返回的是元素，喂给 `html:` 会被
        // 转成字符串，按钮里就显示出 `[object HTMLSpanElement]`（用户截到过这个）。
        // `html:` 只吃字符串（`ui.icon(...)` 那种）。
        iconNode(asideOpen ? 'chevronL' : 'chevronR', 15)
      )
    );

    // 思考开关：管思考折不折。默认收起（思考是过程，不是答案），
    // 但**正文空着时会自动展开**（见 thinkNode）—— 有的模型把答案整段塞进推理通道，
    // 那时不展开就等于这条回复没有内容（用户："模型之前都没有输出思维链，是被吞了？"）。
    loadThink();
    barEl.appendChild(
      h(
        'button.chat__asidefold.chat__thinkbtn' + (state.showThink ? '.is-on' : ''),
        {
          type: 'button',
          title: state.showThink ? '收起思考过程' : '展开思考过程',
          'aria-label': state.showThink ? '收起思考过程' : '展开思考过程',
          'aria-pressed': state.showThink ? 'true' : 'false',
          draggable: 'false',
          onClick: toggleThink,
        },
        iconNode('bulb', 15)
      )
    );

    var counts = {};
    state.messages.forEach(function (message) {
      var key = keyOf(message.parentId);
      counts[key] = (counts[key] || 0) + 1;
    });
    var forks = Object.keys(counts).filter(function (key) {
      return counts[key] > 1;
    }).length;

    var label =
      '对话树 · ' + state.messages.length + ' 个节点' + (forks ? ' · ' + forks + ' 处分叉' : '');
    barEl.appendChild(
      h(
        'button.chat__treebtn' + (state.treeOpen ? '.is-on' : ''),
        {
          type: 'button',
          title: label + '',
          'aria-label': label,
          onClick: function () {
            state.treeOpen = !state.treeOpen;
            if (state.treeOpen) TREE.view = { x: 0, y: 0, k: 1 };
            renderBar();
            renderTree();
          },
        },
        // 形状认得出是"树"，信息（几个节点、几处分叉）做成角标 ——
        // 原先那一长串文字把工具栏挤得只剩它一个。
        iconNode('tree', 15),
        state.messages.length
          ? h('span.chat__treebadge', { text: String(state.messages.length) })
          : null,
        forks ? h('span.chat__treefork', { text: '⑂' + forks }) : null
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
    return h(
      'div.chattree__bar',
      null,
      h('span.chattree__title', { text: '对话树' }),
      h('span.chattree__sub', {
        text: state.messages.length + ' 个节点 · 亮的是当前分支 · 点节点切过去',
      }),
      dirButton,
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
    // 网格**画在 SVG 里**（挂在 layer 上）：它跟着 transform 一起缩放与平移，
    // 于是滚轮、拖动、适应窗口全都自动对齐 —— 用 CSS 背景的话得手工去追 view，
    // 一漏一处就跟不上（用户报的"滚轮缩放，背景没跟着缩放"）。
    svg.appendChild(
      sv('defs', null, [
        sv('pattern', { id: 'qfTreeGrid', width: 22, height: 22, patternUnits: 'userSpaceOnUse' }, [
          sv('path', { class: 'ctgrid__line', d: 'M22 0H0V22' }),
        ]),
      ])
    );
    layer.appendChild(
      sv('rect', { class: 'ctgrid', x: -20000, y: -20000, width: 40000, height: 40000, fill: 'url(#qfTreeGrid)' })
    );

    // 先画连线，再画节点（节点压在线上）
    model.edges.forEach(function (edge) {
      layer.appendChild(
        sv('path', {
          class: 'ctedge' + (edge.onPath ? ' is-onpath' : ''),
          d: treePath(edge),
        })
      );
    });

    // id → 消息：输出的模式要从**它的输入**上取（"输入锚定模式"那条规则）
    var byId = {};
    model.nodes.forEach(function (one) {
      byId[String(one.message.id)] = one.message;
    });
    model.nodes.forEach(function (node) {
      var message = node.message;
      // 边框色 = 这一条用的模式；输入是实线、输出是同一色的虚线
      var anchor = message;
      if (message.role !== 'user' && message.parent_id != null) {
        anchor = byId[String(message.parent_id)] || message;
      }
      var group = sv('g', {
        class:
          'ctnode' +
          (node.onPath ? ' is-onpath' : '') +
          (message.role === 'user' ? ' is-user' : ' is-out') +
          ' mode-' +
          modeKeyOf(anchor) +
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
      // 模式那一行画在正文下面、框内最后一格：一眼看得出"从这一轮起换了模式"
      if (node.mode) {
        group.appendChild(
          sv('text', { class: 'ctnode__mode', x: 12, y: 36 + node.lines.length * 15 }, [
            document.createTextNode(node.mode),
          ])
        );
      }

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
      // 落在节点上就**不要**接管：`setPointerCapture` 会把后续指针事件全部
      // 抢到 svg 自己身上，于是节点那个 `click` 永远不触发（"点节点没反应"
      // 就是这么来的 —— 切换逻辑本身是好的，是它根本没被调到）。
      if (event.target && event.target.closest && event.target.closest('.ctnode')) return;
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
    problem:
      '<path d="M4.5 5.5h9"/><path d="M4.5 10h6.5"/><path d="M4.5 14.5h5"/>' +
      '<path d="M13 20l6.2-6.2a1.9 1.9 0 0 0-2.7-2.7L10.3 17.3V20z"/>',
    close: '<path d="M6 6l12 12M18 6 6 18"/>',
    // 描边纸飞机：与旁边几个图标同一路风格（实心那块在 17px 上太笨重）
    send:
      '<path d="M21.2 3.3 2.9 10.4a.7.7 0 0 0 .05 1.3l6.6 2.4 2.4 6.6a.7.7 0 0 0 1.3.05z"/>' +
      '<path d="M21.2 3.3 9.6 14.1"/>',
    stop: '<rect x="7" y="7" width="10" height="10" rx="2.4" fill="currentColor" stroke="none"/>',
    tree:
      '<path d="M6 4v16"/><path d="M6 11.5h4.5a3 3 0 0 0 3-3V6"/>' +
      '<path d="M6 12.5h4.5a3 3 0 0 1 3 3V18"/>' +
      '<circle cx="17.5" cy="5" r="2.2"/><circle cx="17.5" cy="19" r="2.2"/>',
    // 「深度思考」的图标：两条交叉的轨道 + 中心 —— 官方那颗药丸上的同款意象，
    // 描边、与旁边几个图标同一路风格。
    atom:
      '<circle cx="12" cy="12" r="2.1"/>' +
      '<ellipse cx="12" cy="12" rx="9" ry="4" transform="rotate(45 12 12)"/>' +
      '<ellipse cx="12" cy="12" rx="9" ry="4" transform="rotate(-45 12 12)"/>',
    // 排序（归档区标题上那颗）：长短三条线 + 下箭头
    sort:
      '<path d="M4 7h11"/><path d="M4 12h7"/><path d="M4 17h4"/>' +
      '<path d="M17 7.5v9"/><path d="m14.5 14 2.5 2.5 2.5-2.5"/>',
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

  /**
   * 品牌那枚 logo（图形与 `shell.html` 里回首页的那个**同一个** —— 改一处要一起改）。
   *
   * 用它替掉"AI"两个字：署名用图形更省地方，也让"这谁说的"与品牌对得上。
   * 只取图形、不带那个渐变方块 —— 每条消息旁边都挂一块彩色方块太重了。
   */
  function logoMark() {
    var box = h('span.chatmsg__logo', { 'aria-label': 'QuizForge', title: 'QuizForge' });
    box.innerHTML =
      '<svg viewBox="0 0 32 32" width="15" height="15" fill="none" stroke="currentColor" ' +
      'stroke-width="2.8" stroke-linecap="round">' +
      '<circle cx="14.4" cy="14.4" r="8.4"/><path d="M20.6 20.6 L26.4 26.4"/></svg>';
    return box;
  }

  /**
   * 网格背景跟着缩放与平移走。
   *
   * 网格是画布上的 CSS 背景，而缩放/平移只改了 SVG 的 transform ——
   * 于是"地面"在图上滑走（用户的说法是"滚轮缩放，背景没跟着缩放"）。
   * 背景尺寸按 k 放、位置按 view 平移，两者才是一套。
   */
  /** 内联 SVG 节点（图标表里的一个名字）。 */
  /** 本页要用的那两颗箭头只在 ui.js 的图标表里，chat.js 自己这份没有 ——
   *  查不到就吐出空 `<svg>`（折叠按钮因此"没有图标"，用户截到过）。这里补齐，
   *  路径与 ui.js 那份一致（那边的 ±0.5 是给箭头做的光学修正）。 */
  var ICON_FALLBACK = {
    chevronL: '<path d="M14.5 6 8.5 12l6 6"/>',
    chevronR: '<path d="M9.5 6l6 6-6 6"/>',
    bulb:
      '<path d="M9.2 18h5.6"/><path d="M10.2 21h3.6"/>' +
      '<path d="M12 3a6 6 0 0 0-3.4 10.9c.6.5 1 1.2 1.2 2.1h4.4c.2-.9.6-1.6 1.2-2.1A6 6 0 0 0 12 3z"/>',
  };

  function iconNode(name, size) {
    // 踩过：折叠按钮的图标是空的（用户截到的是 `<svg viewBox=...></svg>`，里面
    // 一个 `<path>` 都没有）—— 因为 `chevronL/chevronR` 只定义在 **ui.js** 的图标表里，
    // 而这里查的是 chat.js 自己那份 `ICONS`，查不到就返回空字符串。补上，路径与
    // ui.js 那份保持一致（那边的注释说这 ±0.5 是给箭头做的光学修正）。
    var box = h('span.chat__icon');
    var px = size || 16;
    box.innerHTML =
      '<svg viewBox="0 0 24 24" width="' + px + '" height="' + px + '" fill="none" ' +
      'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' +
      (ICONS[name] || ICON_FALLBACK[name] || '') +
      '</svg>';
    return box;
  }

  /**
   * 发送键的两态。
   *
   * 图标按钮照样要能表达状态：跑着一轮时是方块（点了就停），平时是箭头。
   * 文案放 title/aria-label —— 图标省的是宽度，不是意思。
   */
  function paintSend(busy) {
    if (!sendBtn) return;
    ui.clear(sendBtn);
    sendBtn.appendChild(iconNode(busy ? 'stop' : 'send', 17));
    sendBtn.title = busy ? '停止' : '发送';
    sendBtn.setAttribute('aria-label', busy ? '停止' : '发送');
    if (busy) sendBtn.classList.remove('btn--primary');
    else sendBtn.classList.add('btn--primary');
    sendBtn.classList.toggle('is-stop', !!busy); // 停止态不参与"上提"那点动效
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

  /* ------------------------------------------------------------ 大题（子窗口） */

  /**
   * 大题走的是**另一条路**，因为它是另一种东西。
   *
   * 题卡承载的是"可点选、可填空"的题：判分确定，前端自己就能算。大题是多问、
   * 要写推导或代码、答案是一段论证 —— 判分只能由一个**子代理**来（它有自己的
   * 提示词、自己的上下文、收窄过的工具集，见 `routers/problem.py`）。
   *
   * 这个子窗口就是它的工作台：题目在上、分问的作答框在中、批改流在下。
   * 它和主对话是**两套上下文** —— 子代理看不见聊天记录，只看得见这道题与这次的作答。
   */
  var problemOpen = false;
  var problemData = null; // { problem, attempts }
  var problemDrafts = {}; // index → 作答
  var problemBusy = false;
  var problemFeedback = '';
  var problemRecord = '';
  // 子代理的过程（思考 + 工具调用前那半句）单独攒着，渲染成可展开的一条 ——
  // 用户要的是批语，但"它是怎么得出结论的"要能查得到（原先直接丢掉了）。
  var problemThought = '';
  var problemThinkOpen = false; // 过程区展不展开由用户决定，重画时要留住
  var problemFeedEl = null;

  function openProblem() {
    problemOpen = true;
    // 工具栏那个按钮要显示"开着的"—— 否则面板浮在中间、按钮却毫无变化，
    // 用户会以为它只是个入口，看不出当前状态。
    if (problemBtn) problemBtn.classList.add('is-on');
    problemFeedback = '';
    problemRecord = '';
    problemData = null;
    renderProblem();
    loadProblem();
  }

  function closeProblem() {
    problemOpen = false;
    if (problemBtn) problemBtn.classList.remove('is-on');
    problemData = null;
    problemDrafts = {};
    problemFeedback = '';
    problemRecord = '';
    renderProblem();
  }

  function loadProblem() {
    api
      .get('/problem/next')
      .then(function (data) {
        if (!problemOpen) return;
        problemData = data;
        problemDrafts = {};
        problemFeedback = '';
        problemRecord = '';
        renderProblem();
        if (!data.problem) ui.toast(data.note || '这个范围里没有大题', 'warn', 2600);
      })
      .catch(function (err) {
        if (problemOpen) ui.toast((err && err.message) || '拿不到题', 'error', 4000);
      });
  }

  function submitProblem() {
    if (problemBusy || !problemData || !problemData.problem) return;
    var answers = (problemData.problem.questions || [])
      .map(function (sub) {
        return { index: sub.index, text: String(problemDrafts[sub.index] || '').trim() };
      })
      .filter(function (item) {
        return item.text;
      });
    if (!answers.length) {
      ui.toast('至少答一问再交', 'warn');
      return;
    }

    problemBusy = true;
    problemFeedback = '';
    problemRecord = '';
    renderProblem();
    paintProblemFeedback();

    api.stream(
      '/problem/solve',
      // conversationId 是给"批改结果回主对话"用的（见后端 `_append_grade_note`）：
      // 没有它，子代理的结论只活在这个小窗口里，主 agent 下一轮对它一无所知。
      { problemId: problemData.problem.id, answers: answers, conversationId: state.current || '' },
      {
        delta: function (data) {
          problemFeedback += (data && data.text) || '';
          scheduleProblemPaint();
        },
        // 子代理的过程收进可展开的"批改过程"，不丢（原先直接丢掉：
        // 用户看不到它查了什么、怎么得出结论的），也不占批语的位置。
        think: function (data) {
          problemThought += (data && data.text) || '';
          paintProblemFeedback();
        },
        tool: function (data) {
          if (!data || data.phase !== 'start') return;
          // 工具调用说明"上面那段话只是铺垫"：连同这一行一起移进过程区，
          // 批语区留下的才是真正的批改结论。
          var pending = String(problemFeedback || '').trim();
          if (pending) problemThought += (problemThought ? '\n\n' : '') + pending;
          problemFeedback = '';
          problemThought += (problemThought ? '\n' : '') + '· 查了 ' + data.name + '…';
          paintProblemFeedback();
        },
        error: function (data) {
          problemFeedback += '\n\n**批改出错**：' + ((data && data.text) || '未知原因') + '\n';
          paintProblemFeedback();
        },
        record: function (data) {
          if (!data) return;
          problemRecord =
            '已记入答题记录：第 ' + data.attempts + ' 次 · ' +
            (data.lastStatus === 'correct'
              ? '全对'
              : data.lastStatus === 'partial'
                ? '部分对'
                : data.lastStatus === 'wrong'
                  ? '不对'
                  : '已记录') +
            (data.lastScore === null || data.lastScore === undefined
              ? ''
              : ' · ' + data.lastScore);
          paintProblemFeedback();
        },
        done: function () {
          problemBusy = false;
          renderProblem();
          paintProblemFeedback();
          // 批完自动开口：批改记事已由后端写进这条对话（`problem._append_grade_note`），
          // 这里让主 agent 就着它讲两句。走的是"接一轮"（不新增用户消息），
          // 否则库里会多出一条不是用户说的"继续"。
          send({ continueTurn: true });
        },
      }
    ).catch(function (err) {
      problemBusy = false;
      problemFeedback += '\n\n**批改失败**：' + ((err && err.message) || '未知原因');
      renderProblem();
      paintProblemFeedback();
    });
  }

  /**
   * 合并重画。
   *
   * 过程是逐段流出来的，原先每来一段就整块重画一次（清空 + 重渲染 Markdown +
   * 重建 details），于是布局反复重排、看起来一跳一跳。现在每帧最多画一次 ——
   * 文字仍然"活"地往外长，但不再抖。
   */
  var problemPaintQueued = false;

  function scheduleProblemPaint() {
    if (problemPaintQueued) return;
    problemPaintQueued = true;
    window.requestAnimationFrame(function () {
      problemPaintQueued = false;
      paintProblemFeedback();
    });
  }

  function paintProblemFeedback() {
    if (!problemFeedEl) return;
    // 重画要保持滚动位置：贴底时继续跟（新内容往下长），
    // 用户往上翻看时就别把他拽回去。
    var box = problemFeedEl.parentNode;
    var stick = box ? box.scrollHeight - box.scrollTop - box.clientHeight < 40 : false;
    var keepTop = box ? box.scrollTop : 0;
    ui.clear(problemFeedEl);
    if (!problemFeedback && !problemRecord && !problemBusy && !problemThought) return;
    problemFeedEl.appendChild(
      h('div.chatproblem__feedlabel', { text: problemBusy ? '正在批改…' : '批改' })
    );
    if (problemThought) {
      var details = h(
        'details.chatproblem__think',
        null,
        h('summary', { text: '批改过程' }),
        // 同上：`render` 的元素自带 `.md`，不要再包一层
        QF.md.render(problemThought)
      );
      details.open = !!problemThinkOpen; // 重画很频繁（每来一段文字就重画），展开状态得留住
      details.addEventListener('toggle', function () {
        problemThinkOpen = details.open;
      });
      problemFeedEl.appendChild(details);
    }
    if (problemFeedback) problemFeedEl.appendChild(QF.md.render(problemFeedback));
    if (problemRecord) {
      problemFeedEl.appendChild(h('div.chatproblem__record', { text: problemRecord }));
    }
    if (box) {
      if (stick) box.scrollTop = box.scrollHeight;
      else box.scrollTop = keepTop;
    }
  }

  function renderProblem() {
    if (!problemEl) return;
    ui.clear(problemEl);
    if (!problemOpen) return;

    var problem = problemData && problemData.problem;
    var list = problem ? problem.questions || [] : [];
    // 已答几问：可以只做一部分（每问留空就是跳过），按钮上得说清这次会交几问
    var answeredCount = list.filter(function (sub) {
      return String(problemDrafts[sub.index] || '').trim();
    }).length;
    problemFeedEl = h('div.chatproblem__feed');

    var body = h('div.chatproblem__body');
    if (!problem) {
      body.appendChild(
        h('div.chatproblem__empty', {
          text: problemData
            ? problemData.note || '这个范围里没有大题'
            : '正在取题…',
        })
      );
    } else {
      body.appendChild(
        h('div.chatproblem__head', null,
          h('span.chatproblem__title', { text: problem.title || '大题' }),
          problem.layer ? h('span.chatproblem__tag', { text: problem.layer }) : null,
          problem.wing ? h('span.chatproblem__tag', { text: problem.wing }) : null,
          problem.difficulty ? h('span.chatproblem__tag', { text: '难度 ' + problem.difficulty }) : null,
          problemData.attempts
            ? h('span.chatproblem__tag', { text: '做过 ' + problemData.attempts + ' 次' })
            : null
        )
      );
      if (problem.stem) body.appendChild(h('div.chatproblem__stem', null, QF.md.render(problem.stem)));
      list.forEach(function (sub) {
        body.appendChild(
          h('div.chatproblem__sub', null,
            h('div.chatproblem__subtitle', { text: '第 ' + sub.index + ' 问｜' + (sub.title || '').replace(/^第 \d+ 问｜/, '') }),
            sub.stem ? h('div.chatproblem__substem', null, QF.md.render(sub.stem)) : null,
            sub.hint ? h('div.chatproblem__hint', { text: '提示：' + sub.hint }) : null,
            h('textarea.chatproblem__answer', {
              rows: '4',
              placeholder: '写你的推导 / 计算 / 代码思路…（这一问不答就留空）',
              value: problemDrafts[sub.index] || '',
              onInput: (function (index) {
                return function (event) {
                  problemDrafts[index] = event.target.value;
                };
              })(sub.index),
            })
          )
        );
      });
    }
    body.appendChild(problemFeedEl);

    problemEl.appendChild(
      h(
        'div.chatproblem__backdrop',
        {
          onClick: function (event) {
            if (event.target === event.currentTarget) closeProblem();
          },
        },
        h(
          'div.chatproblem__sheet',
          null,
          h(
            'div.chatproblem__bar',
            null,
            h('span.chatproblem__barTitle', { text: '大题' }),
            h('span.chatproblem__barNote', { text: '批改由一个独立子代理做，它看不到聊天记录' }),
            // 「不做这道题」：原先只有"换一道"和右上角的叉，而换一道是**换题**不是退出 ——
            // 用户想表达的是"这道我不做、也别记我账上"（关掉不批，随时能再开）。
            h('button.chatproblem__act', {
              type: 'button',
              text: '不做这道题',
              title: '关掉，不批也不记成绩（工具栏那个按钮随时能再打开）',
              onClick: closeProblem,
            }),
            h(
              'button.chatproblem__act',
              {
                type: 'button',
                onClick: function () {
                  if (problemBusy) return;
                  loadProblem();
                },
              },
              '换一道'
            ),
            iconButton('close', '关闭（Esc）', closeProblem)
          ),
          body,
          h(
            'div.chatproblem__foot',
            null,
            h(
              'button.btn.btn--primary.chatproblem__submit',
              {
                type: 'button',
                disabled: problemBusy || !problem,
                onClick: submitProblem,
              },
              problemBusy ? '批改中…' : '提交批改'
            ),
            h('span.chatproblem__footnote', {
              text: '批改结果会写进答题记录（掌握度与间隔重复跟着它走）',
            })
          )
        )
      )
    );
    paintProblemFeedback();
  }

  /* ------------------------------------------------------------ 骨架 */

  function buildSkeleton() {
    rootEl.textContent = '';

    // 「深度思考」的状态要在**建那颗药丸之前**读到（它决定药丸出生时是亮是灭）。
    // 放在 renderBar 里读就晚了：药丸是这里建的，refresh 之后会先按默认值画一遍。
    loadDeep();
    // 归档区的排序方式也要在第一帧之前读到（左栏首屏就按它排）
    loadSort();

    // 左栏只建一次：搜索框如果跟着列表一起重画，打字打到一半就会丢焦点
    asideEl = h('aside.chat__aside');
    asideHeadEl = h('div.chat__asidehead');
    listEl = h('div.chat__list');
    // 批量操作条：多选时才挂进头部（见 renderPickBar），平时不占地方
    pickEl = h('div.chatlist__pick');
    asideEl.appendChild(asideHeadEl);
    asideEl.appendChild(listEl);
    // 右边那条线**可拖**（用户："这个地方的右边线没法左右拖动"）。手柄就压在那条
    // 1px 边框上：平时不见、悬停或拖动时才亮一道主色，观感与原来那条线一致。
    asideEl.appendChild(
      h('div.chat__grip', {
        title: '拖动调整宽度（双击复位）',
        'aria-hidden': 'true',
        onPointerdown: dragAsideWidth,
        onDblclick: function () {
          setAsideWidth(0, true);
        },
      })
    );
    threadEl = h('div.chat__thread', { role: 'log', 'aria-live': 'polite' });
    // 滚上去就露出「回到最新」（见 refreshJump）
    threadEl.addEventListener('scroll', function () {
      // 用户**自己**滚动时才更新跟随状态。程序化的 `scrollTop = scrollHeight`
      // 也会走到这里，但那时本来就贴着底，判定为真、状态不变。
      stickBottom = nearBottom();
      refreshJump();
    });
    inputEl = h('textarea.chat__input', {
      rows: '1',
      placeholder: '问点什么，或者贴一段材料…',
      onInput: growInput,
      onKeydown: onKeydown,
    });
    // 图标按钮：正文只放一个箭头，"发送/停止"两态换图标（见 `paintSend`）。
    // 文案进 title 与 aria-label —— 看得出、也读得出。
    sendBtn = h(
      'button.btn.btn--primary.chat__send',
      { type: 'button', title: '发送', 'aria-label': '发送', onClick: onSendClick },
      iconNode('send', 19)
    );
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
    problemEl = h('div.chatproblem', { role: 'dialog', 'aria-label': '大题' });
    try {
      var savedAside = window.localStorage.getItem('qf.chat.aside');
      if (savedAside === 'closed' || savedAside === 'open') asideOpen = savedAside === 'open';
    } catch (err) {
      /* 读不到就用默认（开着） */
    }
    // 拖过的宽度也要恢复（没拖过就是 0 → 什么都不设，CSS 的默认值生效）
    setAsideWidth(asideWidthNow(), false);
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
              // **传统两行**（用户："按钮多了，改成传统的两行。按钮放下面，输入放上面"）：
              // 第一行只有输入框，占满整宽；第二行才是图标、药丸与发送键。
              // 先前是一行到底，按钮一多，输入框就被挤成一小段。
              inputEl,
              // 输入框这一侧不再挂信号灯了：用户要的是**消息框**右上角那盏
              //（"我说的是发出来的消息框的右上角——因为每次发送的模式都不一样"）。
              // 这边只把模式色交给整条框（发送键跟着变色，见 mounts.js 与 CSS）。
              h(
                'div.chat__tools',
                null,
                iconButton('clip', '附件', function () {
                  if (clipInput) clipInput.click();
                }),
                iconButton('note', '便签', function () {
                  if (notesOpen) closeNotes();
                  else openNotes();
                }),
                // 「大题」按钮**已删**（用户："这个按钮我觉得可以删掉了"）。
                // 题库里的大题本来就该和临时大题走同一条路：推成一张题卡、在本页
                // 作答、答完直接交给**本页这个模型**（见 `askAboutCard`）。
                // 那条"独立子窗口 + 专职子代理"的路（`.chatproblem` / `problem.py`）
                // 因此不再有入口：下面的 `problemBtn` / `problemOpen` 留着不删，
                // 万一要回去；`problemBtn` 现在恒为 null。
                clipInput,
                // 「深度思考」（参考 DeepSeek 官网那颗药丸）：管**这一条消息**要不要
                // 让模型先思考再答。默认开 —— 默认模型 `deepseek-flash` 的常态就是思考；
                // 关掉时后端显式传 `thinking: disabled`（更快、更省，也不产出思维链）。
                // 它与顶栏那颗 `.chat__thinkbtn` 不是一回事：那颗管"折叠块展不展开"，
                // 这颗管"请求里带不带思考"（见 `gateway.thinking_params`）。
                //
                // 位置：**在模式药丸左边**（用户："深度思考和模式这两个药丸换个位置"）。
                (deepBtn = h(
                  'button.chat__deep' + (state.deepThink ? '.is-on' : ''),
                  {
                    type: 'button',
                    title: deepTitle(),
                    'aria-label': '深度思考：' + (state.deepThink ? '开' : '关'),
                    'aria-pressed': state.deepThink ? 'true' : 'false',
                    onClick: toggleDeep,
                  },
                  iconNode('atom', 14),
                  h('span.chat__deeptext', { text: '深度思考' })
                )),
                // 模式药丸：三个模式收成一颗（内容由 mounts.js 画）。
                // 它从"输入框紧左边"挪到了**第二行的深度思考右边**（同一句用户要求）。
                h('div.chat__modes', {
                  id: 'chat-modes',
                  role: 'group',
                  'aria-label': '这一版对话的模式',
                }),
                sendBtn
              )
            ),
            hintEl
          )
        ),
        treeEl,
        demoEl,
        notesEl,
        problemEl
      )
    );
    // `.chat` 是刚建出来的（对话树 / 便签 / 大题那几个浮层是它的**兄弟**，
    // 不能塞进它里面），所以挂完再取引用、落上"会话栏开没开"—— 第一帧就对，不会闪。
    chatEl = rootEl.querySelector('.chat');
    if (chatEl) chatEl.setAttribute('data-aside', asideOpen ? 'open' : 'closed');
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

    renderPickBar();
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

    // **三个区**：置顶 / 归档 / 未归档（见 renderZones）。
    // 这里原先是"分组树 + 拖放 + 文件夹右键菜单"。用户改主意了：不要树、
    // 不要那套层级 —— "分组其实就是文件夹归档的逻辑"，归档这一层就够。
    // 那套实现（chatTree / walkChatTree / folderRow / openFolderMenu）留在文件里
    // 但**没有接线**：接口与数据都没删，想接回来随时可以。
    var keepTop = listEl.scrollTop;
    var list = h('div.chatlist');
    renderZones(list);
    listEl.appendChild(list);
    listEl.scrollTop = keepTop;
  }

  /* --------------------------------------------- 左栏三个区（置顶 / 归档 / 未归档）

   * 用户定的规矩（原话）：
   *   * "分成三个区域：置顶，归档和未归档，用一条线分割就行了"；
   *   * "分组其实就是文件夹归档的逻辑" —— 不要多层文件夹，归档就是那一层；
   *   * "未归档的对话，按照时间顺序排列" —— 里面按 今天 / 昨天 / 7 天内 / 更早
   *     挂小标题（截图里那一套）；
   *   * "置顶就是收藏夹，有没有归档都可以置顶"；
   *   * "对话置顶后位置不变，在置顶处加副本" —— 所以置顶区是**副本**：那条会话
   *     在归档 / 未归档里照旧按时间排（与"搬走"是两回事，别把它从原处删掉）；
   *   * "新建的对话，默认在未归档里" —— 服务端 `archived` 默认 false，天然如此。
   */

  /** 一条会话落在哪个时间桶（未归档区的小标题）。 */
  function bucketOf(ms) {
    if (!ms) return '更早';
    var day = function (t) {
      return new Date(t.getFullYear(), t.getMonth(), t.getDate()).getTime();
    };
    var diff = (day(new Date()) - day(new Date(ms))) / 86400000;
    if (diff <= 0) return '今天';
    if (diff === 1) return '昨天';
    if (diff < 7) return '7 天内';
    return '更早';
  }

  function renderZones(host) {
    // 每次重画都重置"可见顺序"：Shift 范围选要在**眼前这一列**里取区间
    state.visibleIds = [];
    var byTime = function (a, b) {
      return (b.updatedAtMs || 0) - (a.updatedAtMs || 0);
    };
    var all = (state.list || []).slice().sort(byTime);
    // 注意这三个筛选是**独立**的：置顶那条同时还会出现在归档或未归档里 ——
    // 这正是"位置不变、置顶处加副本"。
    var pinned = all.filter(function (c) {
      return !!c.pinned;
    });
    var archived = all.filter(function (c) {
      return !!c.archived;
    });
    var open = all.filter(function (c) {
      return !c.archived;
    });

    // 置顶（收藏夹）：**有才画** —— 空着不该占一行标题
    if (pinned.length) {
      var top = h('div.chatzone');
      top.appendChild(zoneHead('置顶'));
      pinned.forEach(function (c) {
        top.appendChild(convRow(c, 0));
      });
      host.appendChild(top);
    }

    // **归档：文件管理式**（用户："归档实际上是文件管理式的，用户可以像管理文件
    // 一样管理对话。归档和未归档实际上是走不走文件管理的区别"）。
    //
    // 所以这一区里是文件夹树：能建文件夹、能把对话拖进去/拖出来、能多选批量、
    // 能按时间/名称/条数排序。它**总是画出来**（哪怕还空着）—— 不然"建第一个
    // 文件夹"的入口就没有了。
    var box = h('div.chatzone.chatzone--files');
    var filesHead = zoneHead('归档', true);
    // **拖到「归档」标题上 = 移出所有文件夹**（回到根）。没有这个落点的话，
    // 拖进文件夹的对话就"只进不出"了 —— 拖放既然是双向的，落点也得双向。
    filesHead.addEventListener('dragover', function (ev) {
      if (!dragConv) return;
      ev.preventDefault();
      filesHead.classList.add('is-drop');
    });
    filesHead.addEventListener('dragleave', function () {
      filesHead.classList.remove('is-drop');
    });
    filesHead.addEventListener('drop', function (ev) {
      if (!dragConv) return;
      ev.preventDefault();
      filesHead.classList.remove('is-drop');
      var cid = dragConv;
      dragConv = null;
      moveConvTo(cid, '');
    });
    box.appendChild(filesHead);
    walkChatTree(chatTree(archived), 0, box);
    host.appendChild(box);

    // 未归档：**不走文件管理**，按时间平铺。它不带区标题 —— "今天 / 昨天"就是标题
    if (open.length) {
      var rest = h('div.chatzone');
      var last = '';
      open.forEach(function (c) {
        var bucket = bucketOf(c.updatedAtMs);
        if (bucket !== last) {
          rest.appendChild(h('div.chatzone__bucket', { text: bucket }));
          last = bucket;
        }
        rest.appendChild(convRow(c, 0));
      });
      host.appendChild(rest);
    }
  }

  /** 区标题那一行。`withTools` 时右边带上这一区的工具（归档区：排序按钮）。 */
  function zoneHead(text, withTools) {
    var box = h('div.chatzone__head', null, h('span.chatzone__headtext', { text: text }));
    if (withTools) {
      box.appendChild(
        h(
          'button.chatzone__headbtn',
          {
            type: 'button',
            title: '排序方式：按时间 / 按名称 / 按条数',
            'aria-label': '排序方式',
            onClick: function (ev) {
              ev.stopPropagation();
              openSortMenu(ev);
            },
          },
          iconNode('sort', 13)
        )
      );
    }
    return box;
  }

  function setSort(mode) {
    state.sortMode = mode;
    try {
      window.localStorage.setItem('qf.chat.sort', mode);
    } catch (err) {
      /* 存不下就只活这一页 */
    }
    renderAside();
  }

  /** 归档区的排序菜单。当前那项在文字里标出来（不做勾选的记号）。 */
  function openSortMenu(ev) {
    if (ev) {
      chatMenuX = ev.clientX;
      chatMenuY = ev.clientY;
    }
    function mark(mode, text) {
      return state.sortMode === mode ? text + '（当前）' : text;
    }
    showChatMenu([
      { label: mark('time', '按时间'), run: function () { setSort('time'); } },
      { label: mark('name', '按名称'), run: function () { setSort('name'); } },
      { label: mark('count', '按条数'), run: function () { setSort('count'); } },
    ]);
  }

  /* ------------------------------------------------------------ 树（分组 + 会话）

   * 与资源页那棵树对齐的是**逻辑**：折叠状态持久化（存"折起来的那些"，默认全展开）、
   * 缩进表达层级、拖一个会话到分组上就挪进去、分组自己有一套右键菜单。
   */

  //: 折起来的分组（路径集合）。存"折起来的"而不是"展开的"：空集合 = 全展开，
  //  新建的分组天然是展开的，不用额外登记（资源树同一个讲究）。
  var foldedFolders = {};
  var FOLD_KEY = 'qf.chat.folded';

  function readFolded() {
    try {
      var raw = window.localStorage.getItem(FOLD_KEY);
      var parsed = raw ? JSON.parse(raw) : null;
      foldedFolders = parsed && typeof parsed === 'object' ? parsed : {};
    } catch (err) {
      foldedFolders = {};
    }
  }

  function saveFolded() {
    try {
      window.localStorage.setItem(FOLD_KEY, JSON.stringify(foldedFolders));
    } catch (err) {
      /* 存不下就算了：折叠状态丢了不影响用 */
    }
  }

  /** 把"文件夹 + 会话"摊成一棵树；`convs` 传哪批，树上就只挂哪批。
   *
   *  现在只有**归档区**用它（文件管理式）：传的是归档过的会话那一批。
   *  未归档区不走文件管理，是时间平铺（见 renderZones）。 */
  function chatTree(convs) {
    var root = { path: '', name: '', children: {}, leaves: [] };
    function ensure(path) {
      if (!path) return root;
      var node = root;
      path.split('/').forEach(function (_part, i) {
        var parts = path.split('/');
        var here = parts.slice(0, i + 1).join('/');
        if (!node.children[parts[i]]) {
          node.children[parts[i]] = { path: here, name: parts[i], children: {}, leaves: [] };
        }
        node = node.children[parts[i]];
      });
      return node;
    }
    (state.folders || []).forEach(function (path) {
      ensure(path);
    });
    (convs || []).forEach(function (conv) {
      ensure(conv.folder || '').leaves.push(conv);
    });
    return root;
  }

  function countLeaves(node) {
    var n = node.leaves.length;
    Object.keys(node.children).forEach(function (key) {
      n += countLeaves(node.children[key]);
    });
    return n;
  }

  function walkChatTree(node, depth, host) {
    // 树顶那一条"＋ 新建分组"。分组的新建/改名/删除本来就有（接口、菜单都在），
    // 但入口只有"在列表空白处右键" —— 用户找不到（原话："对话模块没有做新建/删除
    // 文件夹之类的逻辑"）。给一条看得见的。
    if (depth === 0) host.appendChild(newFolderRow());
    Object.keys(node.children)
      .sort(function (a, b) {
        return a.localeCompare(b, 'zh');
      })
      .forEach(function (key) {
        var child = node.children[key];
        var folded = !!foldedFolders[child.path];
        host.appendChild(folderRow(child, depth, folded));
        // **子节点总是渲染**（收起时也留着）：收起靠 CSS 把高度收到 0，
        // 展开/收起才有过渡可做。原先这里 `if (!folded)` 才建子节点 ——
        // 一跳到位，没有任何东西可以动画（用户："文件夹展开和收起、图标变化，
        // 全没有动效"）。见 CSS 的 `.chattree__kids`。
        var kids = h('div.chattree__kids');
        var inner = h('div.chattree__kidsinner');
        kids.appendChild(inner);
        walkChatTree(child, depth + 1, inner);
        host.appendChild(kids);
      });
    // 叶子按**当前的排序方式**排（文件夹自己始终按名字 —— 上面那个 localeCompare）
    sortConvs(node.leaves).forEach(function (conv) {
      host.appendChild(convRow(conv, depth));
    });
  }

  /** 分组行：展开/收起按钮 + 名字 + 条数。
   *
   *  箭头原先只是个 `<span>`（12px 宽、9px 的字、`--fg3` 的灰），只有一行 CSS 在转 ——
   *  用户看不出它是控件（原话："收起/展开按钮又没有做"）。折叠逻辑本身是通的（实测
   *  点一下 12 行 → 9 行），缺的是**它像不像一个按钮**。现在它是真的 `<button>`：
   *  18px 点击区、悬停底、`aria-expanded`、可聚焦（Enter/Space 原生可用）；
   *  整行点击照旧折叠（老习惯保留）。 */
  function folderRow(node, depth, folded) {
    /** 把"收起 / 展开"落在 DOM 上：切 class、更新箭头的语义 —— **不重画**。 */
    function applyFold(foldedNow) {
      row.classList.toggle('is-folded', foldedNow);
      var caret = row.querySelector('.chattree__caret');
      if (caret) {
        caret.setAttribute('aria-expanded', foldedNow ? 'false' : 'true');
        caret.setAttribute('aria-label', (foldedNow ? '展开 ' : '收起 ') + (node.name || ''));
        caret.title = foldedNow ? '展开这一组' : '收起这一组';
      }
    }

    function toggle() {
      var foldedNow = !foldedFolders[node.path];
      if (foldedNow) foldedFolders[node.path] = true;
      else delete foldedFolders[node.path];
      saveFolded();
      // **就地切，不 `renderAside()`**：整栏重画会把节点换成新的，高度过渡与
      // 箭头旋转就都失去了起止两端（看起来还是跳变）。节点活着，CSS 才动得起来。
      applyFold(foldedNow);
    }
    var row = h(
      'div.chattree__dir' + (folded ? '.is-folded' : ''),
      {
        title: node.path,
        draggable: 'true',
        // 缩进用 margin-left：**与对话行同一套**（对话行本来就是 marginLeft）。
        // 原先这里用 padding-left，两种缩进一叠就对不齐 —— 见 .chat__aside 那套几何。
        style: { marginLeft: depth * 16 + 'px' },
        onDragstart: function (ev) {
          dragFolder = node.path;
          dragConv = null;
          if (ev.dataTransfer) {
            ev.dataTransfer.effectAllowed = 'move';
            ev.dataTransfer.setData('text/qf-folder', node.path);
          }
        },
        onDragend: function () {
          dragFolder = null;
        },
        onClick: function (ev) {
          // 点的是箭头按钮时它自己会 toggle 并 stopPropagation，这里别再折一次
          if (ev.target && ev.target.closest && ev.target.closest('.chattree__caret')) return;
          toggle();
        },
        onContextmenu: function (ev) {
          ev.preventDefault();
          ev.stopPropagation();
          openFolderMenu(node, ev);
        },
        // 拖一个会话到这一行 = 挪进这个分组（资源树里拖到目录上同一个动作）
        onDragover: function (ev) {
          if (!dragConv && !dragFolder) return;
          ev.preventDefault();
          if (dragFolder === node.path) return;              // 拖到自己身上
          if (dragFolder && node.path.indexOf(dragFolder + '/') === 0) return;  // 拖进自己里面
          row.classList.add('is-drop');
        },
        onDragleave: function () {
          row.classList.remove('is-drop');
        },
        onDrop: function (ev) {
          ev.preventDefault();
          row.classList.remove('is-drop');
          var cid = dragConv;
          var folder = dragFolder;
          dragConv = null;
          dragFolder = null;
          if (cid) {
            moveConvTo(cid, node.path);
            return;
          }
          if (folder && folder !== node.path && node.path.indexOf(folder + '/') !== 0) {
            moveFolderTo(folder, node.path);
          }
        },
      },
      h(
        'button.chattree__caret',
        {
          type: 'button',
          title: folded ? '展开这一组' : '收起这一组',
          'aria-label': (folded ? '展开' : '收起') + ' ' + node.name,
          'aria-expanded': folded ? 'false' : 'true',
          onClick: function (ev) {
            ev.stopPropagation();
            toggle();
          },
        },
        (function () {
          // 用画出来的箭头，不用字符 ▸：字符依赖字体，用户那边根本没显示出来
          //（"刚刚那个按钮没有图标"）。静态字符串，无用户数据。旋转仍由
          // `.chattree__dir:not(.is-folded) .chattree__caret` 那条 CSS 负责。
          var mark = document.createElement('span');
          mark.className = 'chattree__caretmark';
          mark.innerHTML =
            '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor"' +
            ' stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round">' +
            '<path d="M9.5 6.5 15 12l-5.5 5.5"></path></svg>';
          return mark;
        })()
      ),
      h('span.chattree__label', { text: node.name }),
      h('span.chattree__count', { text: String(countLeaves(node)) }),
      // 每行一个 ⋯（与对话行的 ⋯ 同一个位置感）：菜单里是"在里面新对话 /
      // 新建子分组 / 重命名 / 删除"。原先只能右键这一行，等于看不见。
      h(
        'button.chattree__more',
        {
          type: 'button',
          title: '在这个分组里新建 / 重命名 / 删除',
          'aria-label': '分组操作',
          onClick: function (ev) {
            ev.stopPropagation();
            openFolderMenu(node, ev);
          },
        },
        '⋯'
      )
    );
    return row;
  }

  /** 树顶那一条"＋ 新建分组"（分组菜单挂在分组行上，一个分组都没有时它够不着）。 */
  function newFolderRow() {
    return h(
      'div.chattree__new',
      {
        title: '新建文件夹（在归档区空白处右键也可以）',
        onClick: function (ev) {
          openFolderMenu({ path: '', name: '最外层' }, ev);
        },
      },
      h('span.chattree__newplus', { text: '＋' }),
      h('span.chattree__newlabel', { text: '新建文件夹' })
    );
  }

  /** 一条会话（沿用原来的三行小卡，只是按层级缩进）。 */
  function convRow(conv, depth) {
    // 记下"它画在哪一行"：Shift 范围选要在**可见顺序**里取区间（见 pickRangeTo）
    state.visibleIds.push(conv.id);
    var item = h(
      'div.chatlist__item' +
        (conv.id === state.current ? '.is-on' : '') +
        (conv.pinned ? '.is-pinned' : '') +
        (state.picked[conv.id] ? '.is-picked' : ''),
      {
        draggable: 'true',
        // 可聚焦 + 带着 id：F2 改名要靠"焦点在哪一行"认领（见那份 keydown）
        tabindex: '0',
        'data-conv': conv.id,
        title: conv.title || '未命名对话',
        style: { marginLeft: depth * 16 + 'px' },
        onClick: function (ev) {
          // **文件管理式的多选**（归档区那套）：⌘/Ctrl 点一下是"选中这一条"
          // （不进这条会话），Shift 点是"从上次点的那条一直选到这一条" ——
          // 与文件管理器一致。普通点击照旧是打开。
          if (ev && (ev.metaKey || ev.ctrlKey)) {
            ev.preventDefault();
            togglePick(conv.id);
            return;
          }
          if (ev && ev.shiftKey && state.lastPickId) {
            ev.preventDefault();
            pickRangeTo(conv.id);
            return;
          }
          if (conv.id !== state.current) openConversation(conv.id);
        },
        onContextmenu: function (ev) {
          ev.preventDefault();
          ev.stopPropagation();
          openConversationMenu(conv, ev);
        },
        onDragstart: function (ev) {
          dragConv = conv.id;
          if (ev.dataTransfer) {
            ev.dataTransfer.effectAllowed = 'move';
            ev.dataTransfer.setData('text/qf-conv', conv.id);
          }
        },
        onDragend: function () {
          dragConv = null;
        },
      },
      h('div.chatlist__title', { text: conv.title || '未命名对话' }),
      h(
        'div.chatlist__meta',
        null,
        h('span', { text: ui.fmtRelative(conv.updatedAtMs) }),
        conv.messageCount ? h('span', { text: '· ' + conv.messageCount + ' 条' }) : null
      ),
      // 预览那一行去掉了（用户："搞成最简单的列表"）：列表里只留标题 + 时间·条数，
      // 想认内容点开看就是。原来的卡片样式是"标题 / 时间·条数 / 预览"三行 + 圆角边框。
      // 置顶并进 `⋯` 菜单：一行里挂两个图标按钮（一个还只在悬停时出现）太挤，
      // 而它们本来就是同一类操作 —— 对这条会话做什么。
      h('button.chatlist__more', {
        type: 'button',
        title: '重命名 / 置顶 / 删除 / 导出',
        'aria-label': '更多',
        onClick: function (event) {
          event.stopPropagation(); // 别顺带把会话也切了
          openMenu(conv);
        },
      }, '⋯')
    );
    return item;
  }

  //: 正在拖的东西（会话 id / 分组路径）。HTML5 拖拽的 dataTransfer 在 dragover 里
  //  读不到（安全限制），只能自己记。两者互斥：拖会话时清掉分组，反之亦然。
  var dragConv = null;
  var dragFolder = null;

  /** 拖一个分组到另一个分组上 = 改层级（整个子树跟着走，后端已支持）。 */
  function moveFolderTo(from, into) {
    var name = from.split('/').pop();
    api
      .patch('/chat/folders', { path: from, to: (into ? into + '/' : '') + name })
      .then(function () {
        toast('已把「' + name + '」移到「' + (into || '最外层') + '」', 'ok');
        loadList();
      })
      .catch(function (err) {
        toast('移动失败：' + ((err && err.message) || '未知原因'), 'bad');
      });
  }

  /** 一句提示。chat 这一页没有 `toast` 这个名字（那是笔记页的），
   *  我的菜单回调里直接写了 `toast(...)` → 一提示就抛 `toast is not defined`，
   *  连带后面的动作也不执行（实测：删分组点了一点动静都没有）。这里补一个小壳。 */
  function toast(text, kind) {
    if (ui && typeof ui.toast === 'function') return ui.toast(text, kind);
    console.warn('[chat]', text);
  }

  function moveConvTo(cid, folder) {
    // **拖进文件夹 = 归档**：文件夹是归档区的东西（"归档是文件管理式的"）。
    // 一条未归档的对话被拖进文件夹，它就该出现在归档区里 —— 不然会同时"没归档"
    // 又"待在归档区的文件夹里"，两个区的规矩打架。移回最外层（空路径）不改
    // 归档状态：它仍在归档区，只是回到根。
    var patch = { folder: folder };
    if (folder) patch.archived = true;
    api
      .patch('/chat/conversations/' + cid, patch)
      .then(function () {
        // 拖进一个**收起着**的文件夹：把它展开 —— 不然"东西进去了但看不见"
        if (folder && foldedFolders[folder]) {
          delete foldedFolders[folder];
          saveFolded();
        }
        toast('已移动' + (folder ? '到「' + folder + '」' : '到最外层'), 'ok');
        loadList();
      })
      .catch(function (err) {
        toast('移动失败：' + ((err && err.message) || '未知原因'), 'bad');
      });
  }

  /* ------------------------------------------------------------------ 多选
   *
   * 归档区是**文件管理式**的（用户："用户可以像管理文件一样管理对话"），多选
   * 照文件管理器的习惯来：⌘/Ctrl 点一下切换一条，Shift 点从锚点选到这一条；
   * 一旦有选中，左栏头部就出现批量操作条（归档 / 移出归档 / 移动 / 删除）。
   */

  function togglePick(id) {
    if (state.picked[id]) delete state.picked[id];
    else state.picked[id] = true;
    state.lastPickId = id;
    renderAside();
  }

  /** Shift 范围选：在**上一次画出来的行顺序**里，从锚点选到这一条。 */
  function pickRangeTo(id) {
    var order = state.visibleIds || [];
    var a = order.indexOf(state.lastPickId);
    var b = order.indexOf(id);
    if (a < 0 || b < 0) {
      togglePick(id);
      return;
    }
    var from = Math.min(a, b);
    var to = Math.max(a, b);
    for (var i = from; i <= to; i++) state.picked[order[i]] = true;
    renderAside();
  }

  function pickedIds() {
    return Object.keys(state.picked);
  }

  function clearPicked() {
    state.picked = {};
    state.lastPickId = 0;
    renderAside();
  }

  /** 批量改（归档 / 移出 / 移动都走它）：逐条 PATCH —— 条数不多，不为它造接口。 */
  function batchPatch(patch, doneText) {
    var ids = pickedIds();
    if (!ids.length) return;
    Promise.all(
      ids.map(function (id) {
        return api.patch('/chat/conversations/' + id, patch);
      })
    )
      .then(function () {
        toast(doneText.replace('{n}', String(ids.length)), 'ok');
        state.picked = {};
        state.lastPickId = 0;
        loadList();
        renderAside();
      })
      .catch(function (err) {
        toast('批量操作失败：' + ((err && err.message) || '未知原因'), 'bad');
      });
  }

  function batchDelete() {
    var ids = pickedIds();
    if (!ids.length) return;
    Promise.all(
      ids.map(function (id) {
        return api.del('/chat/conversations/' + id);
      })
    )
      .then(function () {
        toast('已删除 ' + ids.length + ' 条', 'ok');
        state.picked = {};
        state.lastPickId = 0;
        loadList();
        renderAside();
      })
      .catch(function (err) {
        toast('删除失败：' + ((err && err.message) || '未知原因'), 'bad');
      });
  }

  /** 批量移动（`folder` 为空 = 移回最外层）。移进文件夹同时归档，与拖放同一条规矩。 */
  function batchMove(folder) {
    var patch = folder ? { folder: folder, archived: true } : { folder: '' };
    batchPatch(patch, folder ? '已移动 {n} 条到「' + folder + '」' : '已移动 {n} 条到最外层');
  }

  /** 「移动…」菜单：把所有文件夹列出来（外加"最外层"）。 */
  function openMoveMenu(ev) {
    if (ev) {
      chatMenuX = ev.clientX;
      chatMenuY = ev.clientY;
    }
    var items = [{ label: '移到最外层', run: function () { batchMove(''); } }];
    (state.folders || []).forEach(function (path) {
      items.push({
        label: '移到「' + path + '」',
        run: function () {
          batchMove(path);
        },
      });
    });
    showChatMenu(items);
  }

  /** 选中若干条之后，左栏头部那条批量操作条（没选中就收起来）。 */
  function renderPickBar() {
    if (!pickEl || !asideHeadEl) return;
    var ids = pickedIds();
    if (!ids.length) {
      if (pickEl.parentNode) pickEl.parentNode.removeChild(pickEl);
      return;
    }
    ui.clear(pickEl);
    pickEl.appendChild(h('span.chatlist__pickcount', { text: '已选 ' + ids.length + ' 条' }));
    pickEl.appendChild(pickBtn('归档', function () { batchPatch({ archived: true }, '已归档 {n} 条'); }));
    pickEl.appendChild(pickBtn('移出归档', function () { batchPatch({ archived: false }, '已移出归档 {n} 条'); }));
    pickEl.appendChild(pickBtn('移动…', function (ev) { openMoveMenu(ev); }));
    pickEl.appendChild(pickBtn('删除', function () { batchDelete(); }, true));
    pickEl.appendChild(pickBtn('完成', function () { clearPicked(); }));
    if (!pickEl.parentNode) asideHeadEl.appendChild(pickEl);
  }

  function pickBtn(text, onClick, danger) {
    return h(
      'button.chatlist__pickbtn' + (danger ? '.is-danger' : ''),
      { type: 'button', onClick: onClick },
      text
    );
  }

  /** 问一个名字（自搭弹层：原生 `prompt` 会冻住整页，样式也跟应用不搭）。 */
  function askName(title, value, okLabel) {
    return new Promise(function (resolve) {
      var input = h('input.chattree__input', { type: 'text', value: value || '', spellcheck: 'false' });
      var done = false;
      var panel = null;
      function finish(ok) {
        if (done) return;
        done = true;
        var text = (input.value || '').trim();
        if (panel) panel.close();
        resolve(ok && text ? text : '');
      }
      input.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter') {
          ev.preventDefault();
          finish(true);
        } else if (ev.key === 'Escape') {
          ev.preventDefault();
          finish(false);
        }
        ev.stopPropagation();
      });
      panel = ui.modal({
        title: title,
        size: 'sm',
        body: h(
          'div.chattree__ask',
          null,
          input,
          h(
            'div.chattree__actions',
            null,
            h('button.btn', { type: 'button', text: '取消', onClick: function () { finish(false); } }),
            h('button.btn.btn--primary', { type: 'button', text: okLabel || '确定', onClick: function () { finish(true); } })
          )
        ),
      });
      setTimeout(function () {
        input.focus();
        input.select();
      }, 40);
    });
  }

  //: 右键弹出菜单的坐标（`contextmenu` 事件里记下来）
  var chatMenuX = 0;
  var chatMenuY = 0;

  function closeChatMenu() {
    var found = document.getElementById('chat-menu');
    if (found) found.remove();
  }

  /** 菜单里那几个图标（细描边，与顶栏一套）。 */
  var MENU_ICONS = {
    rename: '<path d="M4.5 19.5h4L19 9a2.1 2.1 0 0 0-3-3L5.5 16.5z"/><path d="M14.8 7.2l2 2"/>',
    pin: '<path d="M9 4.5h6l-.8 5.2 3.3 3.3H6.5l3.3-3.3z"/><path d="M12 13v6.5"/>',
    unpin: '<path d="M9 4.5h6l-.8 5.2 3.3 3.3H6.5l3.3-3.3z"/><path d="M12 13v6.5"/><path d="M4.5 4.5l15 15"/>',
    trash: '<path d="M5 7h14"/><path d="M9.5 7V5h5v2"/><path d="M7 7l1 12h8l1-12"/>',
    // 归档 = 一个带盖的盒子（与"删除"那只垃圾桶要一眼分得开）
    archive: '<path d="M4.5 8.5h15V19h-15z"/><path d="M3.5 5h17v3.5h-17z"/><path d="M10 12.5h4"/>',
    unarchive: '<path d="M4.5 8.5h15V19h-15z"/><path d="M3.5 5h17v3.5h-17z"/><path d="M12 16.5v-4"/><path d="M10 14.5l2-2 2 2"/>',
  };

  /** 一个小弹出菜单（会话行 / 分组行右键用）。
   *
   * 每条 = 图标 + 文字 +（可选）右边的快捷键：用户要的是"重命名，置顶，删除，
   * 都是图标 + 文字，删除做成红色"。 */
  function showChatMenu(items) {
    closeChatMenu();
    var menu = h('div.chattree__menu', { id: 'chat-menu', role: 'menu' });
    items.forEach(function (item) {
      var row = h('button.chattree__menuitem' + (item.danger ? '.is-danger' : ''), {
        type: 'button',
        role: 'menuitem',
        html:
          '<span class="chattree__menuico">' +
          (item.icon && MENU_ICONS[item.icon]
            ? '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" ' +
              'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' +
              MENU_ICONS[item.icon] +
              '</svg>'
            : '') +
          '</span><span class="chattree__menutext"></span>' +
          (item.hint ? '<span class="chattree__menuhint"></span>' : ''),
        onClick: function () {
          closeChatMenu();
          item.run();
        },
      });
      row.querySelector('.chattree__menutext').textContent = item.label;
      if (item.hint) row.querySelector('.chattree__menuhint').textContent = item.hint;
      menu.appendChild(row);
    });
    document.body.appendChild(menu);
    var box = menu.getBoundingClientRect();
    menu.style.left = Math.min(chatMenuX, window.innerWidth - box.width - 8) + 'px';
    menu.style.top = Math.min(chatMenuY, window.innerHeight - box.height - 8) + 'px';
    setTimeout(function () {
      document.addEventListener('click', closeChatMenu, { once: true });
      document.addEventListener('contextmenu', closeChatMenu, { once: true });
    }, 0);
  }

  /** 分组行上的右键菜单。 */
  function openFolderMenu(node, ev) {
    if (ev) {
      chatMenuX = ev.clientX;
      chatMenuY = ev.clientY;
    }
    var atRoot = !node.path;
    var items = [
      {
        label: '在里面新对话',
        run: function () {
          api
            .post('/chat/conversations', { folder: node.path })
            .then(function () {
              loadList();
            })
            .catch(function (err) {
              toast('新建失败：' + ((err && err.message) || '未知原因'), 'bad');
            });
        },
      },
      {
        label: '新建子文件夹',
        run: function () {
          askName('新建子分组', '', '创建').then(function (name) {
            if (!name) return;
            api
              .post('/chat/folders', { path: node.path + '/' + name })
              .then(function () {
                delete foldedFolders[node.path];      // 建了就展开父级，不然它藏起来了
                saveFolded();
                loadList();
              })
              .catch(function (err) {
                toast('新建失败：' + ((err && err.message) || '未知原因'), 'bad');
              });
          });
        },
      },
      {
        label: '重命名…',
        run: function () {
          askName('重命名分组', node.name, '改名').then(function (name) {
            if (!name) return;
            var parent = node.path.split('/').slice(0, -1).join('/');
            api
              .patch('/chat/folders', { path: node.path, to: (parent ? parent + '/' : '') + name })
              .then(function () {
                loadList();
              })
              .catch(function (err) {
                toast('改名失败：' + ((err && err.message) || '未知原因'), 'bad');
              });
          });
        },
      },
      {
        label: '删除（空的分组才能删）',
        danger: true,
        run: function () {
          api
            .del('/chat/folders?path=' + encodeURIComponent(node.path))
            .then(function () {
              toast('已删除分组', 'ok');
              loadList();
            })
            .catch(function (err) {
              toast((err && err.message) || '删不掉：里面还有东西', 'bad');
            });
        },
      },
    ];
    if (atRoot) {
      // 最外层：给的是"新建分组"。没有这一条就没法建**第一个**分组
      //（分组菜单挂在分组行上，而一开始一个分组都没有）。
      items = [
        {
          label: '新建文件夹',
          run: function () {
            askName('新建分组', '', '创建').then(function (name) {
              if (!name) return;
              api
                .post('/chat/folders', { path: name })
                .then(function () {
                  loadList();
                })
                .catch(function (err) {
                  toast('新建失败：' + ((err && err.message) || '未知原因'), 'bad');
                });
            });
          },
        },
      ];
    }
    showChatMenu(items);
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
        // 分组一起存：左栏那棵树是"目录 + 会话"一起才画得出来的
        state.folders = (res && res.folders) || [];
        readFolded();
        renderAside();
        if (!wantedOnce) {
          wantedOnce = 1;
          var want = '';
          try { want = new URLSearchParams(location.search).get('c') || ''; } catch (e) { want = ''; }
          if (want && state.list.some(function (one) { return one.id === want; })) openConversation(want);
        }
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

  //: 这一轮重画**不要跳到最底**（见 repaintKeepingScroll）。
  var keepScroll = false;

  /** 重画一次线程，但**保持用户眼前的位置**。
   *
   * `paintThread` 默认 `scrollToEnd(true)` —— 那是给"发完消息 / 流式输出"用的。
   * 编辑、取消编辑这类**原地变形**的动作不该动视口：实测点「编辑信息 → 取消」，
   * 界面会往下滑一段（就是被这一句拽到底的）。原本贴着底的话仍然贴底。
   */
  function repaintKeepingScroll() {
    if (!threadEl) return;
    var top = threadEl.scrollTop;
    var atEnd = threadEl.scrollHeight - threadEl.scrollTop - threadEl.clientHeight < 4;
    keepScroll = true;
    paintThread();
    keepScroll = false;
    if (atEnd) {
      // 本来贴着底：那重画完也贴底（`false` 不算数 —— 清空那一瞬间 scrollTop 已被夹到 0）
      scrollToEnd(true);
      return;
    }
    // **必须先把平滑滚动关掉**：`.chat__thread` 上有 `scroll-behavior: smooth`，
    // 直接赋 scrollTop 会走动画，而紧接着 fitArea 改布局会把这次动画打断 ——
    // 结果就是"恢复了却又被拉回顶部"（实测跳动 -98px）。
    threadEl.style.scrollBehavior = 'auto';
    threadEl.scrollTop = top;
    // 下一帧再恢复平滑：这一次赋值已经即时生效了
    requestAnimationFrame(function () {
      threadEl.style.scrollBehavior = '';
    });
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
    if (keepScroll) refreshJump();
    else scrollToEnd(true);
  }

  /** `‹ 2 / 3 ›`：同一个父节点下的几个分支，翻着看（LibreChat 的 SiblingSwitch）。 */
  function siblingSwitch(m, siblings) {
    var index = 0;
    siblings.forEach(function (item, i) {
      if (item.id === m.id) index = i;
    });
    // 箭头用**画出来的图形**，不用 `‹` / `›` 这两个字形：字形的光学位置随字体变，
    // 跟中间那串数字永远差一点点（用户："没有对齐"）；画出来的图形由 flex 几何居中，
    // 与数字的行盒是同一条中线。图标与折叠按钮那对同源（见 ICON_FALLBACK）。
    var prev = h(
      'button.chatmsg__sib',
      {
        type: 'button',
        title: '上一个分支',
        disabled: index === 0,
        onClick: function () {
          pickBranch(m.parentId, siblings[index - 1]);
        },
      },
      iconNode('chevronL', 13)
    );
    var next = h(
      'button.chatmsg__sib',
      {
        type: 'button',
        title: '下一个分支',
        disabled: index === siblings.length - 1,
        onClick: function () {
          pickBranch(m.parentId, siblings[index + 1]);
        },
      },
      iconNode('chevronR', 13)
    );
    return h(
      'div.chatmsg__siblings',
      null,
      prev,
      h('span.chatmsg__sibcount', { text: index + 1 + ' / ' + siblings.length }),
      next
    );
  }

  /** 右上角那颗模式色点。
   *
   * 非编辑态挂在气泡上、颜色 = **这一条自己的**模式（`modeKeyOf(m)`）；
   * 编辑态挂进编辑框、颜色 = **当前模式**（这条正要按新模式重发，
   * 灯必须说的是"你现在会用什么发"）。所以它收的是模式键与组名，不自己猜。
   */
  function ledNode(modeKey, groups) {
    return h('span.chatmsg__led', {
      'data-mode': modeKey,
      title: '这一条用的模式：' + modeLabel(groups),
    });
  }

  function messageRow(m) {
    var isUser = m.role === 'user';
    var body = h('div.chatmsg__body');
    //: 气泡下面那一行小图标（复制 / 编辑并重发）。只有用户消息有 ——
    //  见下面 `actions = …` 与建 `row` 时它作为第三个子节点。
    var actions = null;

    // 有兄弟就显示切换器：没有它，"重新回答"过的旧分支就永远够不着了。
    //
    // **放哪**：挪到"消息框下面那一行"里、与右端那组图标同一行垂直居中
    //（用户："这个东西就可以放到消息框下面了，和最右边的图标居中对齐"）——
    // 用户消息挂 `useractions`（复制/编辑那一行），助手消息挂 `foot`（复制/重答那行）。
    // 原先它挂在 `.chatmsg__body` 里、气泡**上面**，占着一段地方还老被当成标题。
    //
    // **编辑态不建它**（用户："编辑模式这个东西应直接消失"）：那会儿正在改这一句，
    // 切分支没有意义 —— 而且它原来就顶在编辑框上面，看着像框的一部分。
    var siblings = siblingsOf(m);
    var sib = siblings.length > 1 && state.editing !== m.id ? siblingSwitch(m, siblings) : null;

    if (isUser) {
      if (state.editing === m.id) {
        body.appendChild(userEditor(m));
      } else {
        body.appendChild(h('div.chatmsg__text', { text: m.content }));
        // 用户消息也有零件 —— 附件就挂在这一侧（助手那一侧的零件走 partsNode）
        (m.parts || []).forEach(function (part) {
          if (part && part.type === 'file') body.appendChild(fileNode(part));
        });
        // 这两颗**不在气泡里**了：挪到气泡下面、靠右（用户："这两个按钮拿出来，
        // 放到下面，大概是这个样式"——参考图里它们是气泡右下方的一对小图标）。
        // 交给下面那个 grid（`grid-column: 2` + `justify-self: end`）摆位，
        // 所以这里只是把它建出来、挂到**行**上而不是挂到气泡里。
        actions = h(
          'div.chatmsg__useractions',
          null,
          // 分支切换在最左端（没有兄弟时是 null，`h` 会跳过它）——
          // 与右边的复制/编辑同一行、垂直居中（行的 `align-items: center` 管着）
          sib,
          iconButton('copy', '复制我这条', function () {
            copyText(m.content, '已复制我这条');
          }),
          iconButton('pencil', '编辑并重发', function () {
            editMessage(m);
          })
        );
      }
    } else {
      // 正文是投影，零件才是真相：旧消息没有 parts 时按正文兜一个
      body.appendChild(
        partsNode(m.parts && m.parts.length ? m.parts : [{ type: 'text', text: m.content || '' }])
      );
    }
    // **消息框右上角那盏信号灯**：这一条输入用的是哪个模式。
    // 用户："我说的是发出来的消息框的右上角 —— 因为每次发送的模式都不一样。"
    // 输入锚定模式（它下面所有输出都是这一个），所以灯只打在**用户那一条**上。
    // 它替掉了原先那句"本轮模式：笔记、资料、图谱……"：长句换成一颗色点。
    //
    // **编辑态它进编辑框**（见 `userEditor`）：挂在 `body` 上时参照系是**气泡**那层，
    // 而进编辑时气泡的内距被撤掉了（`.is-editing .chatmsg__body { padding: 0 }`），
    // 灯就飘到框外面去（用户："信号灯也到外面去了"）。挂在编辑框自己身上最稳。
    if (isUser && state.editing !== m.id) {
      body.appendChild(ledNode(modeKeyOf(m), modeKeys(m)));
    }
    var row = h(
      'div.chatmsg' +
        (isUser ? '.chatmsg--user' : '.chatmsg--assistant') +
        (m.status === 'error' ? '.is-error' : '') +
        // 正在编辑这一条：气泡那层要撤掉，只留编辑框自己那层（见 CSS 里的 .is-editing）
        (m.id && state.editing === m.id ? '.is-editing' : ''),
      { dataset: { id: String(m.id || '') } },
      // 署名：用户是"我"，AI 那侧用品牌那枚 logo（与左上角回首页的是同一个图形）
      h('div.chatmsg__who', null, isUser ? '我' : logoMark()),
      body,
      // 气泡下面那一行小图标（只有用户消息有）。行是 grid，它占第 2 列、靠右 ——
      // 于是正好落在气泡右下角（见 .chatmsg__useractions 的 grid-column/justify-self）。
      actions
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
    // `data-mode`：编辑框自己也认一档模式色（与 `.chat__box` 同一套写法）——
    // 里面那颗灯、下面那颗「发送」都吃这上面的 `--mode-color`。
    // 用**当前**模式而不是这条的老模式：重发会按现在这个模式走，
    // 用户"在编辑状态里频繁改模式"时，这两个地方要一路跟着变。
    var box = h('div.chatmsg__edit', { 'data-mode': currentMode() });
    // 信号灯也搬进来（编辑框自己的右上角，`.chatmsg__edit` 是 relative）——
    // 见 messageRow 里那段说明：挂在气泡那层会飘到框外。
    box.appendChild(ledNode(currentMode(), currentModeGroups()));
    var area = h('textarea.chatmsg__editarea', {
      rows: '1',
      // 打字时继续跟着长（进入时那一次定高在 editMessage 里做）
      onInput: function () {
        fitArea(area);
      },
    });
    area.value = m.content || '';

    function close() {
      state.editing = 0;
      // 取消/发送都是**原地变形**：别把视口拽到最底（用户："点击编辑信息，然后再取消，
      // 界面会往下滑动一段"——就是 paintThread 里那句 scrollToEnd 干的）
      repaintKeepingScroll();
    }

    function submit() {
      var text = String(area.value || '').trim();
      // **正文没改也照样重发**：这是"同一句话再问一遍"的正常用法 ——
      // 它会落成同一个父节点下的兄弟分支，旧那条连同它的回答原样留着，
      // 翻回来随时能看。原先这里写着 `text === m.content` 就 `close()`，
      // 于是用户改完又改回去、或者就想重问一遍时，按钮看起来"没反应"
      //（用户："同样的消息不支持重发，必须要改一改"）。
      if (!text || state.busy) {
        close();
        return;
      }
      close();
      // parentId 显式给出来（可能是 null）：第一条消息就在根上，
      // 不显式说的话服务端会把它挂到会话末尾去。
      // `replaceId` 告诉发送那边"这是就地替换哪一行"——否则新那条会先
      // 挂到线程末尾，等生成完再搬上来（用户："最新的会先出现在下面"）。
      send({
        content: text,
        parentId: m.parentId === undefined ? null : m.parentId,
        replaceId: m.id,
      });
    }

    // 键盘与主输入框一致：**Enter 发送、Shift+Enter 换行**。
    // 这里原先一个键都没接，编辑时想发出去只能去点按钮
    //（用户："用户更改信息的时候，没法 enter 直接发送。换行走 shift+enter"）。
    // `isComposing` 那道是给中文输入法留的：组字过程中的回车是"选词"，不是发送。
    area.addEventListener('keydown', function (ev) {
      if (ev.key !== 'Enter' || ev.shiftKey || ev.isComposing) return;
      ev.preventDefault();
      submit();
    });

    box.appendChild(area);
    box.appendChild(
      h(
        'div.chatmsg__editfoot',
        null,
        // 顺序照那张参考图：左边留出提示的位置（`flex: 1` 占住），右边先是「取消」、
        // **最右**是主色的「发送」。原先发送在最左，与参考图相反。
        h('span.chatmsg__edithint', { text: '' }),
        // 两个**药丸**：发送（btn--primary 就是主色那颗）与取消（ghost 描边）。
        h('button.btn.btn--ghost.chatmsg__editcancel', { type: 'button', onClick: close }, '取消'),
        h(
          'button.btn.btn--primary.chatmsg__editsave',
          {
            type: 'button',
            // 与输入框那颗"发送"同一套写法：点它、或者 Enter，走同一个 submit()
            onClick: submit,
          },
          '发送'
        )
      )
    );
    return box;
  }

  /* 模式变了（mounts.js 吆喝的那一声）：正在编辑的那条**就地**跟上。
   *
   * 灯与「发送」键都是模式色 —— 切一下就该变色，用户的原话：
   * "我在编辑输入状态下频繁改变模式，右上角的灯和下面的'发送'不会跟着一起改变颜色"。
   *
   * 为什么不 `repaintKeepingScroll()` 重画：重画会把编辑框整个换掉，光标位置、
   * 已经打的字、中文输入法的中间状态全丢。这两样纯粹是样式，改属性就够。
   */
  document.addEventListener('qf:mode', function () {
    var box = threadEl && threadEl.querySelector('.chatmsg__edit');
    if (!box) return;
    var mode = currentMode();
    box.setAttribute('data-mode', mode);
    var led = box.querySelector('.chatmsg__led');
    if (led) {
      led.setAttribute('data-mode', mode);
      led.title = '这一条用的模式：' + modeLabel(currentModeGroups());
    }
  });

  function editMessage(m) {
    if (state.busy || !m || m.role !== 'user') return;
    state.editing = m.id;
    // 进编辑也是原地变形：保持视口位置（否则点一下铅笔，界面就往下滑一段）
    repaintKeepingScroll();
    var area = threadEl.querySelector('.chatmsg__editarea');
    if (area) {
      // **进来就按内容定高**：一句"你好！"不该占四行的高度（见 fitArea 的说明）
      fitArea(area);
      area.focus();
      area.setSelectionRange(area.value.length, area.value.length);
    }
  }

  /** 编辑框跟着内容长（与输入框那颗 `growInput` 是同一套手法）。
   *
   * 用户："一进入编辑状态的时候，编辑框的高度根据用户早先一次输入内容的高度做合适的
   * 变化（现在是一行也很大空余）。" —— 原先 CSS 里写死 `min-height: 64px`，
   * 一句"你好！"也占着四行的高度。现在进来先按 scrollHeight 定高，之后打字继续跟。
   * 上限 320px，超过就内部滚动（`overflow-y: auto`，见 CSS）。
   */
  function fitArea(el) {
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 320) + 'px';
  }

  /* ------------------------------------------------------------ 附件与演示 */

  /**
   * 附件：图片给缩略图（双击放大），别的给一个链接 + 元数据。
   *
   * 图**不再**写"AI 看不到图像内容"那种提示：能读图的模型会真的收到图像
   * （见 `ai_gateway.model_reads_images`），读不了的模型那边由服务端把原因
   * 说进上下文里 —— 界面上不该再摆一句已经不成立的话。
   */
  function fileNode(part) {
    var url = '/api/chat/attachments/' + encodeURIComponent(String(part.attachmentId || ''));
    var box = h('div.chatfile');

    if (part.kind === 'image') {
      box.appendChild(
        h('img.chatfile__img', {
          src: url,
          alt: part.name || '附件',
          loading: 'lazy',
          title: '双击放大',
          onDblclick: function () {
            zoomImage(url, part.name);
          },
        })
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
    return box;
  }

  /**
   * 双击图片放大。
   *
   * 消息里的图是缩略图（宽度被限住，否则一张截图就占满整屏），想看细节只能点开 ——
   * 不然用户得先另存到本地再打开，那是把活儿推给他。
   * 覆盖层按需创建、关掉即销毁（不给页面常驻一个空壳）。
   */
  var lightbox = null;

  function zoomImage(url, name) {
    if (lightbox || !url) return;
    var box = h(
      'div.chatzoom',
      {
        role: 'dialog',
        'aria-label': (name || '图片') + '（放大）',
        onClick: function (event) {
          // 点图片以外的空白处关闭；点在图上不关（常常要看细节）
          if (event.target === event.currentTarget) closeZoom();
        },
      },
      h('img.chatzoom__img', { src: url, alt: name || '图片' }),
      h(
        'div.chatzoom__bar',
        null,
        h('span.chatzoom__name', { text: name || '图片' }),
        h(
          'button.chatzoom__act',
          {
            type: 'button',
            onClick: function () {
              window.open(url, '_blank', 'noopener');
            },
          },
          '新标签页打开'
        ),
        h(
          'button.chatzoom__act',
          { type: 'button', onClick: closeZoom },
          '关闭（Esc）'
        )
      )
    );
    box.__esc = function (event) {
      if (event.key === 'Escape') closeZoom();
    };
    document.addEventListener('keydown', box.__esc);
    document.body.appendChild(box);
    lightbox = box;
  }

  function closeZoom() {
    if (!lightbox) return;
    document.removeEventListener('keydown', lightbox.__esc);
    lightbox.remove();
    lightbox = null;
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
   * 一次 Python 运行：**像一条命令**。
   *
   * `$ python` + 这轮想干什么（title），下面直接是输出；脚本折在「脚本」里
   *（要回看就展开，平时不占地方）。失败时整块偏红，跟终端里的报错一个意思 ——
   * 不再有「打开演示」「在右侧面板里打开」「已回填给模型」那些话。
   */
  /* --------------------------------------------------- 常驻 Python 运行壳 */

  /**
   * 一个隐藏的 iframe：**启动一次**，之后反复收代码执行。
   *
   * 为什么不再"一段脚本一个壳"：Pyodide 每次都要重新下载 wasm、初始化解释器、
   * 再 import numpy/scipy —— 实测换一段脚本就得重来一秒多（用户："跑 python 脚本
   * 非常慢，你研究一下"）。常驻之后，那笔钱只在**第一次真的要用 Python 时**付一次，
   * 后面每次运行只剩执行本身。
   *
   * 懒启动：页面一打开就下 60MB 运行时，对不跑 Python 的人不公平。
   */
  var shell = {
    el: null,      // 那个 iframe（建好之前是 null）
    ready: false,  // 它报过 qfReady 没有
    info: '',      // 环境自述（"Python 3.12 · numpy 1.26.4 · scipy …"）
    bootMs: 0,
    packMs: 0,
    // 注：这里原先还有一个 `said` 标志（"环境自述只在第一次运行的输出开头报一次"）——
    // 那种把壳的内部账塞进脚本输出的做法已经删掉了（见 qfReady / qfRun 两处注释），
    // 标志也就没用了。
    queue: [],     // 就绪前排着的活
    asked: {},     // 已经派过活的 runId（零件重画时别重复派）
  };

  function shellBoot() {
    if (shell.el) return;
    api
      .get('/chat/sandbox')
      .then(function (data) {
        if (!data || !data.html) return;
        var el = h('iframe.pyrun__shell', {
          sandbox: 'allow-scripts',
          referrerpolicy: 'no-referrer',
          title: 'python 运行壳',
          srcdoc: withCsp(data.html),
        });
        // 挂在 body 上（不在消息里）：它是**整页共用**的一个东西
        document.body.appendChild(el);
        shell.el = el;
        shellFlush();
      })
      .catch(function () {
        // 取不到壳：那块零件会一直显示"运行中…" —— 不静默假装跑过。
      });
  }

  function shellFlush() {
    if (!shell.ready || !shell.el || !shell.el.contentWindow) return;
    while (shell.queue.length) {
      shell.el.contentWindow.postMessage(shell.queue.shift(), '*');
    }
  }

  /** 把一段脚本交给常驻壳（同一个 runId 只派一次 —— 零件会重画好几次）。 */
  function shellRequest(part) {
    var id = String((part && part.runId) || '');
    if (!id || shell.asked[id]) return;
    shell.asked[id] = true;
    shell.queue.push({
      qfRun: id,
      qfCode: String((part && part.code) || ''),
      // 点名要装的包（壳里 `qfPackages`）；壳另外还会按 import 现装别的，
      // 所以这一项通常是空的 —— 只有动态导入（`__import__`）时才用得上
      qfPackages: (part && part.packages) || [],
    });
    shellBoot();
    shellFlush();
  }

  /** 输出到了：把屏幕上那一块**就地**改成结果。
   *
   * 为什么不整块重画（`paintThread()`）：流式过程中重画会把正在写字的那个节点踢掉。
   * 所以直接找那一块（`data-run` 认领）改它的输出区。
   */
  /** **运行结果那一块：输出 + 图，一起收得起也展得开。**
   *
   * 用户："现在输出是收不起来的。图也是输出，也要可以收起来。"
   * 所以它们是**一块** —— 折一次，两样一起收（图往往才是最占地方的那个）。
   * 先前只在"输出超过 24 行"时才给折叠壳，十几行的输出就无壳可折，正是
   * "收不起来"的来源。
   *
   * 默认展开（内容为主），只有很长的输出才默认收起。用原生 `<details>`：
   * 展开状态、键盘、无障碍都是现成的。
   */
  function resultNode(text, images) {
    var lines = String(text || '').split('\n').length;
    var figs = figsNode(images);
    var body = h('div.pyrun__result');
    body.appendChild(h('pre.pyrun__out', { text: text }));
    if (figs) body.appendChild(figs);

    var tail = '结果' + (lines > 1 ? '（' + lines + ' 行' : '（');
    tail += figs ? (lines > 1 ? ' · 含图）' : '含图）') : '）';
    var box = h('details.pyrun__outwrap', lines > 60 ? null : { open: true });
    box.appendChild(h('summary.pyrun__outsum', { text: tail + ' · 点这里收起 / 展开' }));
    box.appendChild(body);
    return box;
  }

  /** 这一轮画出来的图（matplotlib 那种）——贴在输出下面。
   *
   * 图片是壳收走的（脚本不用 savefig），以 base64 跟着回传；这里只负责摆出来。
   * **默认显示得小**（用户："图正常出，但是太大了（目前是整个屏幕），默认情况调小一点"）——
   * 尺寸在 CSS 里限住（限宽 + 限高），双击看大图（与附件那套同一个 `zoomImage`）。
   */
  function figsNode(images) {
    if (!images || !images.length) return null;
    var box = h('div.pyrun__figs');
    images.forEach(function (b64) {
      var url = 'data:image/png;base64,' + String(b64 || '');
      box.appendChild(
        h('img.pyrun__fig', {
          src: url,
          alt: '运行产出的图',
          loading: 'lazy',
          title: '双击看大图',
          onDblclick: function () {
            zoomImage(url, '运行产出的图');
          },
        })
      );
    });
    return box;
  }

  function paintRunInPlace(runId, run) {
    var nodes = document.querySelectorAll('.pyrun[data-run]');
    for (var i = 0; i < nodes.length; i += 1) {
      if (nodes[i].getAttribute('data-run') !== String(runId)) continue;
      var text = run.text || '（没有输出）';
      var wait = nodes[i].querySelector('.pyrun__wait');
      var done = nodes[i].querySelector('.pyrun__result');
      if (done) {
        // 结果块已经在：只更新里面的文本与图，**不重建** ——
        // 重建会把用户已经点开的折叠状态抖掉
        var out = done.querySelector('.pyrun__out');
        if (out) out.textContent = text;
        if (run.images && run.images.length && !done.querySelector('.pyrun__figs')) {
          var more = figsNode(run.images);
          if (more) done.appendChild(more);
        }
      } else if (wait && wait.parentNode) {
        // "运行中…"换成**整个结果块**（输出 + 图，一起收得起 —— 见 resultNode）
        wait.parentNode.replaceChild(resultNode(text, run.images), wait);
      }
      nodes[i].classList.toggle('is-bad', run.ok === false);
    }
  }

  function pythonRun(part) {
    // 先把这一页收藏的认回来（`demoRuns`）：输出可能**先于**这次绘制到达
    //（壳常驻之后第二次运行只要几毫秒，零件还没画出来结果就回来了）。
    var run = part.run || demoRunOf(part.runId) || null;
    var bad = run && run.ok === false;
    var rows = [
      h(
        'div.pyrun__bar',
        null,
        h('span.pyrun__prompt', { text: '$ python' }),
        h('span.pyrun__title', { text: part.title || '' })
      ),
    ];
    if (part.code) {
      rows.push(
        h(
          'details.pyrun__codewrap',
          null,
          h('summary.pyrun__summary', { text: '脚本' }),
          h('pre.pyrun__code', { text: part.code })
        )
      );
    }
    // 输出与图是**一块**（`resultNode`）：一起收、一起展 —— 图往往才是最占地方的那个
    rows.push(
      run && run.text
        ? resultNode(run.text, run.images)
        : h('div.pyrun__wait', { text: '运行中…' })
    );
    // 执行分两条路：
    //   * 新零件（只带 runId + 脚本原文）→ 交给**常驻壳**，它跑完 postMessage 回来；
    //   * 老零件（带 html，那会儿是一段脚本一个壳）→ 仍然就地起一个隐藏 iframe，
    //     否则那些消息里的脚本再也不会跑第二次（它自己的输出可能没存下来）。
    // 两条路都把 iframe 藏在 DOM 里（见 .pyrun__runner / .pyrun__shell 的样式）。
    if (part.code && !part.html) {
      shellRequest(part);
    } else if (part.html) {
      rows.push(
        h('iframe.pyrun__runner', {
          sandbox: 'allow-scripts',
          referrerpolicy: 'no-referrer',
          title: part.title || 'python',
          srcdoc: withCsp(part.html || ''),
        })
      );
    }
    // `data-run`：输出回来时要**就地认出这一块**（见 paintRunInPlace）
    return h('div.pyrun' + (bad ? '.is-bad' : ''), { 'data-run': part.runId || '' }, rows);
  }

  /**
   * 演示在消息里只是一张**卡片**：标题 + 说明 + 打开按钮。
   *
   * 真正跑它的是右侧那个整屏高的面板 —— 嵌在消息流里又窄又矮，
   * 稍微像样一点的可视化都会被框住（那是第一版的问题）。
   */
  function demoCard(part) {
    // `run_python` 不是「演示」，它是**跑一段脚本拿输出** —— 长相该像执行一条命令，
    // 而不是一张要打开面板的卡片（用户原话：你回想一下你执行命令的时候是怎么做的？
    // 做成那样的展示形式就够了）。判据是服务端给的 `kind`（见 parts.demo_part）。
    // 回退判据：`kind` 是后加的字段，库里那些**旧消息**没带它 —— 但 python 那份的
    // html 是 Pyodide 运行壳，认这个就能让旧消息也立刻换成长相（不必等它重跑）。
    if (part.kind === 'python' || /pyodide/i.test(part.html || '')) return pythonRun(part);
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
      }),
      // 跑出来的输出直接摆在这儿 —— 不必点开面板才知道它跑成什么样。
      // 它同时也是回填给服务端的那一份（见 message 监听里的回填）。
      part.run && part.run.text
        ? h(
            'div.chatdemo__inline' + (part.run.ok === false ? '.is-bad' : ''),
            null,
            h('div.chatdemo__inlinelabel', {
              text: part.run.ok === false ? '沙箱报错（已回填给模型）' : '沙箱输出（已回填给模型）',
            }),
            h('pre.chatdemo__inlinepre', { text: part.run.text })
          )
        : null
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

  /** 哪个消息里挂着这次运行的零件（回填要用它的 id）。 */
  function findRunMessage(runId) {
    for (var i = 0; i < state.messages.length; i++) {
      var message = state.messages[i];
      var parts = message.parts || [];
      for (var j = 0; j < parts.length; j++) {
        var part = parts[j];
        if (part && part.type === 'demo' && part.runId === runId) {
          return { part: part, mid: message.id, cid: state.current };
        }
      }
    }
    return null;
  }

  // 沙箱页面（Python 那种）跑完会 postMessage 回来：{qfRun: runId, ok, text}
  window.addEventListener('message', function (event) {
    var data = event.data;
    if (!data) return;
    if (data.qfReady) {
      // 常驻壳报"就绪"：把排队那几段脚本放出去（它们可能在壳启动之前就来了）。
      // 环境自述与启动耗时是**壳自己的账**，记下来备用，但**不往任何输出里塞** ——
      // 脚本的输出必须是它自己 print 的东西（用户："python 沙箱的输出里面怎么会有
      // 这个？""这个东西也不该出现在输入里"）。
      shell.ready = true;
      shell.info = String(data.info || '');
      shell.bootMs = Number(data.bootMs || 0);
      shell.packMs = Number(data.packMs || 0);
      // 依赖没装上要**说一声**：悄悄降级的代价是"看起来装了、其实 import 失败"
      //（壳里的注释就是这么写的，但这一条以前一直没人显示）。
      if (data.warning) ui.toast(String(data.warning), 'warn');
      shellFlush();
      return;
    }
    if (!data.qfRun) return;
    // 原样收下：`text` 里只有脚本自己的输出，`ms` 是壳算的执行耗时（面板若不显示就只是记着），
    // `images` 是壳收走的图（base64）——**这一项漏过一次**：忘了放进 run，
    // 结果是面板上不出图、上报给服务端的也一直是空。
    var run = {
      ok: data.ok !== false,
      text: String(data.text || ''),
      ms: Number(data.ms || 0),
      images: data.images || [],
    };
    demoRuns[data.qfRun] = run;

    if (state.demo && state.demo.runId === data.qfRun) {
      state.demo.run = run;
      paintDemoRun();
    }

    // **一律按 runId 上报**（不只往那条消息里填）。跑脚本通常发生在**流式过程中**，
    // 那时消息还没落库、前端拿不到 message id —— 只走消息那条路会把输出丢掉：
    // 块永远停在"运行中…"，而库里那次运行其实**有**输出（实测查库确认过）。
    // 服务端在那儿有个"等输出"的会合点（app/runs.py），这条一发，那一轮就能接着说结论。
    api
      .post('/chat/runs/' + data.qfRun, {
        ok: run.ok,
        text: run.text,
        ms: run.ms,
        // 图也一并交回：这样库里那条运行自带图 —— 刷新 / 换设备都还在
        images: run.images || [],
      })
      .catch(function () {});

    // 屏幕上那一块也当场改掉
    paintRunInPlace(data.qfRun, run);

    // 顺手**回填给服务端**：那条消息因此自己带着输出 —— 界面直接显示，
    // 下一轮模型也能读到。不必让人按什么按钮转述（那是把他当传话筒）。
    var host = findRunMessage(data.qfRun);
    if (!host || host.part.run) return;
    host.part.run = run;
    // 线程里长出输出块。**流式过程中不能整块重画** —— 那会把正在写字的那个
    // 节点踢掉（render 是往 state.live 的节点上追加的）。这种情况留给
    // 这一轮结束时的重画，它自然会带上输出。
    if (!state.busy) paintThread();
    api
      .post('/chat/conversations/' + host.cid + '/messages/' + host.mid + '/run', {
        runId: data.qfRun,
        ok: run.ok,
        text: run.text,
      })
      .catch(function () {
        // 回填失败只意味着"模型这一轮看不到它"，界面照常显示
      });
  });

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
          text: (run.ok ? '沙箱输出' : '沙箱报错') + ' · 已回填给模型',
        }),
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
        // 大题：`{小问号: 他写的那一段}` —— 与 `engine.isResponseEmpty` 认的形状一致
        subs: {},
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
  /**
   * 临时题卡：模型**现编**的一道题。
   *
   * 与题库题卡（`questionCard`）刻意分开，因为身份不同：那道题在题库里有 id、
   * 有答题记录、判分靠本地题库；这道题只有一个草稿 id，答案就在卡片上，
   * 用户按「存进我的题单」它才成为题单里的一道（`uq-…`）。
   *
   * 判分在本地算（single / multi / blank 都是确定的），所以不等网络 ——
   * 这也是"临时题"该有的手感：答完立刻知道对错。
   */
  function draftCard(part) {
    var payload = part.payload || {};
    var name = String(payload.type || 'single');
    var kinds = { single: '单选', multi: '多选', blank: '填空', short: '简答', problem: '大题' };
    var picked = {};
    var typed = '';
    var subs = {};
    var checked = false;
    var savedId = '';
    var host = h('div.chatcard__host');

    function want() {
      return String(payload.answer || '')
        .toUpperCase()
        .split(',')
        .map(function (item) {
          return item.trim();
        })
        .filter(Boolean);
    }

    /** 归一化：多余空格与标点不该判错（用户写的是意思，不是格式）。 */
    function flat(text) {
      return String(text || '')
        .toLowerCase()
        .replace(/\s+/g, '')
        .replace(/[。，,．.；;：:！!？?、"'（）()「」【】]/g, '');
    }

    function verdict() {
      if (name === 'single' || name === 'multi') {
        var got = Object.keys(picked)
          .filter(function (key) {
            return picked[key];
          })
          .sort()
          .join(',');
        if (!got) return null;
        return got === want().sort().join(',');
      }
      if (name === 'blank') {
        var value = flat(typed);
        if (!value) return null;
        var accepted = [payload.answer || ''].concat(payload.accepts || []);
        return accepted.some(function (item) {
          return flat(item) === value;
        });
      }
      return null;
    }

    function save() {
      if (savedId) return;
      QF.api
        .post('/my/questions', {
          payload: {
            type: name,
            stem: payload.stem || '',
            options: payload.options || undefined,
            answer: payload.answer || '',
            accepts: payload.accepts,
            explanation: payload.explanation || '',
            hint: payload.hint || '',
            questions: payload.questions,
            layer: payload.layer || '',
            wing: payload.wing || '',
            topic: payload.topic || '',
            difficulty: payload.difficulty || 3,
          },
          pointKey: payload.pointKey || '',
        })
        .then(function (data) {
          savedId = ((data || {}).question || {}).id || 'ok';
          ui.toast('已存进我的题单', 'info', 1600);
          paint();
        })
        .catch(function (err) {
          ui.toast((err && err.message) || '没存进去，稍后再试', 'error', 3000);
        });
    }

    function paint() {
      ui.clear(host);
      var box = h('div.chatcard.chatcard--draft');

      box.appendChild(
        h(
          'div.chatcard__head',
          null,
          h('span.chatcard__id', { text: '现编 · ' + (kinds[name] || '题') }),
          h('span.chatcard__tag.is-draft', { text: savedId ? '已在题单' : '临时题' }),
          // 带上"层/翼"两个字：光写「应用 · 应用」看不出是哪两个维度，
          // 而这两列的取值本来就重名（层有"应用"、翼也有"应用"）。
          payload.layer
            ? h('span.chatcard__tag', { text: payload.layer + '层', title: '认知层：识记 / 理解 / 应用 / 迁移' })
            : null,
          payload.wing
            ? h('span.chatcard__tag', { text: payload.wing + '翼', title: '难度翼：基础 / 应用 / 综合 / 创新' })
            : null,
          payload.difficulty ? h('span.chatcard__tag', { text: '难度 ' + payload.difficulty }) : null
        )
      );
      box.appendChild(h('div.chatcard__stem', null, QF.md.render(payload.stem || '')));

      if (name === 'problem') {
        (payload.questions || []).forEach(function (sub) {
          var area = h('textarea.chatcard__text', {
            rows: 3,
            placeholder: '写你的推导…',
            onInput: function (event) {
              subs[sub.index] = event.target.value;
            },
          });
          area.value = String(subs[sub.index] || '');
          box.appendChild(
            h(
              'div.chatcard__body',
              null,
              h('div.chatcard__subtitle', { text: sub.title || '第 ' + sub.index + ' 问' }),
              sub.stem ? h('div.chatcard__substem', { html: QF.md.renderInline(sub.stem) }) : null,
              area,
              checked && sub.reference
                ? h('div.chatcard__answer', null, h('div.chatcard__explain', { html: QF.md.renderToString(sub.reference) }))
                : null
            )
          );
        });
      } else if (name === 'single' || name === 'multi') {
        (payload.options || []).forEach(function (option) {
          box.appendChild(
            h(
              'button.chatcard__opt' + (picked[option.key] ? '.is-on' : ''),
              {
                type: 'button',
                onClick: function () {
                  if (name === 'single') {
                    picked = {};
                    picked[option.key] = true;
                  } else {
                    picked[option.key] = !picked[option.key];
                  }
                  checked = false;
                  paint();
                },
              },
              h('span.chatcard__key', { text: option.key }),
              h('span.chatcard__opttext', { html: QF.md.renderInline(option.text || '') })
            )
          );
        });
      } else {
        var input =
          name === 'blank'
            ? h('input.chatcard__blank', {
                placeholder: '填答案…',
                onInput: function (event) {
                  typed = event.target.value;
                },
              })
            : h('textarea.chatcard__text', {
                rows: 3,
                placeholder: '用自己的话答…',
                onInput: function (event) {
                  typed = event.target.value;
                },
              });
        input.value = typed;
        box.appendChild(h('div.chatcard__body', null, input));
      }

      // 判定：选择题与填空能算；简答/大题没有确定答案，只能给参考、自己比
      if (checked && (name === 'single' || name === 'multi' || name === 'blank')) {
        var ok = verdict();
        box.appendChild(
          h(
            'div.chatcard__verdict' + (ok ? '.is-ok' : '.is-bad'),
            null,
            h('span.chatcard__verdicttext', {
              text: ok ? '对了' : ok === false ? '不对 —— 看下面的答案' : '（还没作答）',
            })
          )
        );
      }

      if (checked) {
        if (payload.answer) {
          box.appendChild(h('div.chatcard__answer', null, h('div.chatcard__answerlabel', { text: '答案：' + payload.answer })));
        }
        if (payload.explanation) {
          box.appendChild(h('div.chatcard__explain', { html: QF.md.renderToString(payload.explanation) }));
        }
      }

      box.appendChild(
        h(
          'div.chatcard__actions',
          null,
          h('button.chatcard__submit', {
            type: 'button',
            text: checked ? '收起答案' : name === 'short' || name === 'problem' ? '看参考答案' : '对答案',
            onClick: function () {
              checked = !checked;
              paint();
            },
          }),
          h('button.chatcard__again' + (savedId ? '.is-done' : ''), {
            type: 'button',
            text: savedId ? '已在题单' : '存进我的题单',
            disabled: !!savedId,
            onClick: save,
          }),
          // 答完之后那颗「发送」：把题面与每一问的作答发到本页对话里。
          // 用户的原话："临时大题回答完之后，点击发送把作答情况发给当页的这个 llm"。
          h('button.chatcard__submit', {
            type: 'button',
            // 文案里不强调"AI"（用户："不要强调（AI 批改）"）—— 它做的事就是
            // 把作答发到本页对话里，接着那句话说下去。
            text: '发送',
            title: '把这道题和你的作答发到本页对话里，让它批改、接着讲',
            onClick: function () {
              askDraftAbout(payload, subs);
            },
          }),
          h('button.chatcard__again', {
            type: 'button',
            text: '再改一版',
            onClick: function () {
              prefillComposer('把上面那道题改一下：');
            },
          })
        )
      );

      host.appendChild(box);
    }

    paint();
    return host;
  }

  /**
   * 把一道**现编的题**与他的作答发给本页的模型。
   *
   * 和 `askAboutCard` 是同一件事的两半：那个说"这道题（题号）我答对了…"，
   * 靠题号让模型自己去取题面与答案（省上下文）；这里没有题号可给 ——
   * 题是现编的、题库里没有 —— 所以把**题面与每一问的作答**一起带上。
   *
   * 为什么走对话而不是 `/ai/grade`：那条是独立端点（自己一套提示词、看不见
   * 本页上文）；这条路就是在对话里接着说 —— 模型知道你前面在聊什么，
   * 批完还能顺着讲。用户要的就是这个（"发给当页的这个 llm"）。
   */
  function askDraftAbout(payload, subs) {
    if (state.busy) {
      ui.toast('正在生成，这条说完再发', 'warn');
      return;
    }
    var mine = [];
    (payload.questions || []).forEach(function (sub) {
      var text = String((subs || {})[sub.index] || '').trim();
      if (text) mine.push((sub.title || '第 ' + sub.index + ' 问') + '：' + text);
    });
    if (!mine.length) {
      ui.toast('还没作答呢', 'warn');
      return;
    }
    send({
      content:
        '我刚做了一道' +
        (payload.type === 'problem' ? '大题' : '题') +
        '，题面是：\n' +
        String(payload.stem || '').trim() +
        '\n\n我的作答：\n' +
        mine.join('\n') +
        '\n\n请批改：每一问哪里对、哪里缺、下一步该补什么。',
    });
  }

  /** 往输入框里放一句话（"再改一版"这类入口用），光标停在末尾。 */
  function prefillComposer(prefix) {
    var input = document.querySelector('.chat__input');
    if (!input) return;
    var existing = String(input.value || '').trim();
    input.value = existing || prefix;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.focus();
    if (input.setSelectionRange) input.setSelectionRange(input.value.length, input.value.length);
  }

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
    } else if (kind === 'problem') {
      // 大题：**每问一个输入区**（与临时题那条分支同一套写法）。
      // 题库题的题对象上是 `parts`（payload 里就是这个名字）、现编题卡上是
      // `questions` —— 两种叫法都认，渲染出来是同一样东西。
      // 每问的 reference / rubric 不在这里显示：它们**不在卡里**
      //（见后端 `_card_payload` 剥答案字段的说明），要看就点提交。
      (question.parts || question.questions || []).forEach(function (sub) {
        var area = h('textarea.chatcard__text', {
          rows: '3',
          placeholder: '就这一问写你的推导…',
          onInput: (function (at) {
            return function (event) {
              draft.subs[at] = event.target.value;
            };
          })(sub.index),
        });
        area.value = draft.subs[sub.index] || '';
        box.appendChild(
          h(
            'div.chatcard__body',
            null,
            h('div.chatcard__subtitle', { text: sub.title || '第 ' + sub.index + ' 问' }),
            sub.stem ? h('div.chatcard__substem', { html: QF.md.renderInline(sub.stem) }) : null,
            area
          )
        );
      });
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

    // **客观题一颗、主观题一颗 —— 不是两颗。**
    //
    // 原先主观题上是"提交（AI 批改）"（走 `/ai/grade` 独立端点），旁边还有一颗
    // "发送给 AI"（走本页对话）—— 两件几乎同一件事，摆在同一张卡上就是重复
    //（用户："这个地方有两个重复的按钮"）。
    //
    // 现在按**题型**切开：客观题本地就能判，留「提交」；主观题要模型来判，
    // 那就直接发到本页对话 —— 模型看得见上文，批完还能顺着讲，判定由它自己
    // 用 `record_problem_grade` 写进答题记录（与临时大题同一条路）。
    // 文案里也不再强调"AI 批改"（用户："不要强调（AI 批改）"）。
    var needsAI = QF.engine.grade(question, emptyResponse(question)).requiresAI;
    var submit;
    if (needsAI) {
      submit = h('button.btn.btn--primary.chatcard__submit', {
        type: 'button',
        text: '发送',
        title: '把这道题和你的作答发到本页对话里，让它批改、接着讲',
        onClick: function () {
          var response = readResponse(question, draft);
          if (QF.engine.isResponseEmpty(question, response)) {
            ui.toast('还没作答呢', 'warn');
            return;
          }
          askAboutCard(question, response, { status: 'ungraded' });
        },
      });
    } else {
      submit = h(
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
        draft.busy ? '批改中…' : '提交'
      );
    }
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
    // 大题：`{小问号: 作答}`（与 `engine.isResponseEmpty` 认的形状一致）
    if (question.type === 'problem') return draft.subs;
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
          '讲讲这道题'
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
    // 大题：`{小问号: 作答}` —— 按小问号排好人话再拼（对象直接 String() 会变成
    // `[object Object]`，发给模型就成了乱码）
    if (response && typeof response === 'object') {
      return Object.keys(response)
        .map(function (key) {
          return { key: key, value: String(response[key] == null ? '' : response[key]).trim() };
        })
        .filter(function (item) {
          return item.value;
        })
        .sort(function (a, b) {
          return Number(a.key) - Number(b.key);
        })
        .map(function (item) {
          return '第 ' + item.key + ' 问：' + item.value;
        })
        .join('；');
    }
    return String(response === null || response === undefined ? '' : response).trim();
  }

  /**
   * 「讲讲这道题」。
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
    if (type === 'text') {
      // **正文永远是正文**：服务端已经按通道分好了（`content` → text 零件，
      // `reasoning_content` → think 零件），这里不再做任何"按位置猜"的降级。
      // （原来这一行是 `part.process ? thinkNode(part) : …`：紧跟工具调用的正文
      // 会被折成"过程"块，用户的原话是"正文被吞到思考里面了"。现在连库里已经
      // 存下的老标记也一起不认 —— 刷新一下，那些被折走的正文回到正文里。）
      return QF.md.render(String(part.text || ''));
    }
    if (type === 'think') return thinkNode(part);
    if (type === 'tool_call') return toolNode(part);
    if (type === 'card') return questionCard(part);
    if (type === 'draft') return draftCard(part);
    if (type === 'action') return actionNode(part);
    if (type === 'file') return fileNode(part);
    if (type === 'demo') return demoCard(part);
    if (type === 'citation') return citationNode(part);
    if (type === 'summary') return h('div.chatmsg__note', { text: part.text || '' });
    if (type === 'error') return h('div.chatmsg__why', { text: part.message || '出错了' });
    return null;
  }

  /**
   * 思考：折起来。它是过程，不是结论 —— 想看的人点开，不想看的人不被它挤走。
   *
   * 这里**只**装 `think` 零件（模型的推理通道，服务端按 `reasoning_content`
   * 单独发的）。正文零件不再往这里来 —— 原来那段"紧跟工具调用的话算铺垫、
   * 折进过程"的猜测（`demoteProcess`）已删：模型自己早就用通道分好了
   * （思维链是英文的推理，正文是对用户说的话），不用我们再按位置猜。
   */
  function thinkNode(part) {
    var node = h(
      'details.chatmsg__think',
      null,
      h('summary', { text: part.label || '思考过程' }),
      // 直接放 `render` 的元素（它自带 `.md`）。原来这里是
      // `h('div.md', { html: QF.md.renderToString(…) })` —— 而 `renderToString`
      // 就是 `render(…).outerHTML`，字符串**本身已经是一个 `.md`**：两层一叠，
      // 内层那个 `.md` 有它自己的字号与前景色，把外层的"灰 + 小"整个顶掉
      //（用户："这个思考内容样式没变啊"）。
      QF.md.render(String(part.text || ''))
    );
    if (state.showThink) {
      node.open = true;
      return node;
    }
    // 正文为空就自动展开：有的模型把**答案**整段走推理通道，正文于是空着 ——
    // 不展开的话这条回复看起来"什么都没有"（用户质疑"输出被吞了"就是这个）。
    // 延后一拍：要等这条消息的其它零件都挂上，才看得出正文到底有没有。
    setTimeout(function () {
      if (node.open) return;
      var msg = node.closest ? node.closest('.chatmsg') : null;
      if (!msg) return;
      var body = msg.querySelector('.chatmsg__parts > .md');
      var shown = body ? (body.textContent || '').trim().length : 0;
      // 只在**事实**上展开：这条回复一个字都没有，那"过程"就是它唯一的内容。
      // 不猜"过程是不是答案"（原先那条"过程比正文长 4 倍就展开"是启发式，
      // 用户否掉了："不要搞启发式"）。
      if (shown === 0) node.open = true;
    }, 0);
    return node;
  }

  /** 思考开关的持久化 + 应用（把已经画出来的那些"过程"一起跟着开/合）。 */
  function applyThink() {
    try {
      window.localStorage.setItem('qf-think', state.showThink ? '1' : '0');
    } catch (err) {
      /* 存不下就算了：只影响下次进来时的默认值 */
    }
    Array.prototype.forEach.call(document.querySelectorAll('.chatmsg__think'), function (el) {
      el.open = !!state.showThink;
    });
  }

  function loadThink() {
    try {
      state.showThink = window.localStorage.getItem('qf-think') === '1';
    } catch (err) {
      state.showThink = false;
    }
  }

  /** 那颗「深度思考」药丸的悬浮说明：把"开/关各意味着什么"说全。 */
  function deepTitle() {
    return state.deepThink
      ? '深度思考：开 —— 模型先想再答（更慢、更花 token）。点一下关掉'
      : '深度思考：关 —— 直接作答（更快、更省）。点一下打开';
  }

  /**
   * 「深度思考」：**请求侧**的那个开关（显示侧是顶栏那颗 `thinkbtn`，两回事）。
   *
   * 状态只活在本机（localStorage），随每条发送的消息带给服务端；服务端不落库
   * —— 它是一次调用的运行参数（`thinking: enabled/disabled`），不是这条消息的属性。
   */
  function loadDeep() {
    try {
      // **默认开**：只有明确存过 '0' 才算关（默认模型 deepseek-flash 的常态就是思考）
      state.deepThink = window.localStorage.getItem('qf.chat.deep') !== '0';
    } catch (err) {
      state.deepThink = true;
    }
  }

  /** 归档区的排序方式（存本机；只有归档区用它 —— 未归档区永远是时间序）。 */
  function loadSort() {
    try {
      var mode = window.localStorage.getItem('qf.chat.sort');
      if (mode === 'name' || mode === 'count' || mode === 'time') state.sortMode = mode;
    } catch (err) {
      /* 读不到就用默认（时间） */
    }
  }

  /** 按当前排序方式排一批会话（`sortMode` 的三种取值）。 */
  function sortConvs(list) {
    var arr = (list || []).slice();
    if (state.sortMode === 'name') {
      arr.sort(function (a, b) {
        return String(a.title || '').localeCompare(String(b.title || ''), 'zh');
      });
    } else if (state.sortMode === 'count') {
      arr.sort(function (a, b) {
        return (b.messageCount || 0) - (a.messageCount || 0);
      });
    } else {
      arr.sort(function (a, b) {
        return (b.updatedAtMs || 0) - (a.updatedAtMs || 0);
      });
    }
    return arr;
  }

  function toggleDeep() {
    state.deepThink = !state.deepThink;
    try {
      window.localStorage.setItem('qf.chat.deep', state.deepThink ? '1' : '0');
    } catch (err) {
      /* 存不下就只活这一页 */
    }
    if (!deepBtn) return;
    // 只动这一颗的样子（不发请求、不重画对话）—— 切开关不该有别的动静
    deepBtn.classList.toggle('is-on', state.deepThink);
    deepBtn.setAttribute('aria-pressed', state.deepThink ? 'true' : 'false');
    deepBtn.title = deepTitle();
  }

  function toggleThink() {
    state.showThink = !state.showThink;
    applyThink();
    renderBar();
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

    // 助手这条的分支切换（"重新回答"出来的第几版/共几版）同样搬到脚注行里、
    // 放在最左端 —— 与右端的复制/重答同一行垂直居中（用户对用户消息那侧的要求，
    // 这一侧照做；原先它也挂在气泡上面）。
    var asib = siblingsOf(m);
    if (asib.length > 1) foot.appendChild(siblingSwitch(m, asib));

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
    var caret = null;
    var scrollQueued = false;
    //: 每个零件对应的 DOM 节点（与 parts 一一对应）。流式里绝大多数帧**只动最后一块**，
    //: 有了这张对照表就不必整棵重建 —— 一次重建的代价是"所有块重跑 Markdown + KaTeX"，
    //: 而它在流式里要做几十上百次。实测（150 块 / 1.5 万字）：上游 4.6 秒吐完，
    //: 页面还要再花 2 秒才画完，那段尾巴就是这些白干的重建。
    var nodes = [];

    function render(immediate) {
      if (immediate) {
        if (timer) {
          clearTimeout(timer);
          timer = null;
        }
      } else {
        // 每来一个字就重排一遍 Markdown 会又抖又费，攒 100ms 刷一次 ——
        // 但这是**节流**，不是防抖：**已经在排队的那一帧不许被新字推迟**。
        // 原来每来一个 chunk 都 `clearTimeout` 重设，而 chunk 间隔（几十毫秒）
        // 永远小于 100ms → 计时器永远不触发 → 整个流式期间一次都不渲染，
        // 只在收尾时画一遍 —— 用户看到的就是"基本一次生成好然后输出"。
        if (!timer) {
          timer = setTimeout(function () {
            timer = null;
            render(true);
          }, 100);
        }
        return;
      }
      var stick = nearBottom();
      var last = parts.length ? parts[parts.length - 1] : null;
      // **只换最后一块**：流式里被追加的只有 text/think 两种，前面那些块（工具卡、
      // 引用…）一个字都没变。判据不满足（新增了块、或类型不是这两种）就整棵重建。
      var patched = false;
      if (
        last &&
        (last.type === 'text' || last.type === 'think') &&
        nodes.length === parts.length &&
        nodes[parts.length - 1] &&
        nodes[parts.length - 1].parentNode
      ) {
        var slot = nodes[parts.length - 1];
        var fresh = partNode(last);
        if (fresh) {
          if (fresh.tagName === slot.tagName) {
            // **留着节点、只换里面的内容**。这里原来是 `replaceChild`（连节点
            // 一起换）—— 对思考块是致命的：`<details>` 一换，展开状态就没了，
            // 而 `thinkNode` 又"正文为空就自动展开"，于是"收起 → 展开"每
            // 100ms 重演一遍 = 频闪（用户："思考过程在流式输出的过程中，会频闪"）。
            // 实测基线：4.5 秒里这个块被整体替换 21 次。
            //
            // `open` 是 details 自己的属性、不在 innerHTML 里 —— 换内容不动它，
            // 用户的展开状态、浏览器已滚到的位置都跟着留住。
            if (last.type === 'think') {
              // 思考块**再精准一格：只换里面那段渲染**，`<summary>` 一动不动。
              // 为什么 summary 也不能跟着重建：用户点它展开时，mousedown 与
              // mouseup 要落在同一个节点上才算一次 click —— 每 100ms 换一个
              // summary，点击就永远差半拍，表现是"流式里点不开"（实测如此）。
              var oldBody = slot.querySelector('.md');
              var newBody = fresh.querySelector('.md');
              if (oldBody && newBody) oldBody.innerHTML = newBody.innerHTML;
            } else {
              slot.innerHTML = fresh.innerHTML;
            }
          } else {
            slot.parentNode.replaceChild(fresh, slot);
            nodes[parts.length - 1] = fresh;
          }
          patched = true;
        }
      }
      if (!patched) {
        ui.clear(body);
        var box = h('div.chatmsg__parts');
        nodes = [];
        parts.forEach(function (part) {
          var node = partNode(part);
          nodes.push(node);
          if (node) box.appendChild(node);
        });
        body.appendChild(box);
        caret = null;
      }
      // 光标钉在末尾：节点**复用**（原先每帧新建一个，会闪）
      if (state.busy) {
        if (!caret) caret = h('span.chat__caret');
        body.appendChild(caret);
      } else {
        caret = null;
      }
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
        // 滚动合并到下一帧：每个字都摸一次 scrollHeight 会强制布局，
        // 高频流式时这一下比渲染本身还费
        if (!scrollQueued) {
          scrollQueued = true;
          window.requestAnimationFrame(function () {
            scrollQueued = false;
            scrollToEnd(false);
          });
        }
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
        // 收尾了：挂着的节流帧别再来一次（它算的是收尾前的样子）
        if (timer) {
          clearTimeout(timer);
          timer = null;
        }
        nodes = [];
        caret = null;
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
    // 24px：**真贴底**才算在底部。原先这里是 120 —— 那是"自动跟随"的容差，
    // 被顺手拿来当"在不在底部"用，于是"贴底"的范围大得离谱：滚轮往上拉一格
    // （40~100px）都还在这条线内，用户被当成"他还在底部"。
    return threadEl.scrollHeight - threadEl.scrollTop - threadEl.clientHeight < 24;
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

  //: 一条消息最多多少字 —— 与 `api/app/routers/chat.py` 的 `MAX_CONTENT` 同一个数。
  //: 前端先看一眼，是为了**说一声**：超了要明着告诉用户，不能悄悄砍掉后半截。
  //:（上一次就是这么丢的：稿子的后半没了、界面一声不吭，用户的原话是
  //: "显然输入被偷偷截断了"。）
  var MAX_SEND_CHARS = 200000;

  function send(options) {
    var opts = options || {};
    var text = String(opts.content || '').trim();
    if (text.length > MAX_SEND_CHARS) {
      toast(
        '这条 ' + text.length + ' 字，超过上限：只发前 ' + MAX_SEND_CHARS + ' 字（后面的没发出去）',
        'warn'
      );
      text = text.slice(0, MAX_SEND_CHARS);
    }
    // `continueTurn` 是"接一轮"：没有正文也不算空发（它只是让 agent 就着已有历史说话）
    if (!text && !opts.replyTo && !opts.continueTurn) return Promise.resolve();
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
          // 这一条**发出去时用的是哪个模式**：服务端落库时也会记同一份快照
          //（`Message.mounts`），这里先记上，屏幕上那颗灯才不会在落库前空着
          //（灯的颜色、树节点的边框都从它推 —— 见 modeKeyOf）。
          mounts: currentModeGroups(),
        };
        localId = local.id;
        state.messages.push(local);
        // 「编辑并重发」是**就地替换**：新那条的层就是被改那条的层（同一个父节点下的
        // 兄弟），所以直接换掉它那一行 —— 旧分支的后续（它的回答）一起撤掉，等重画时
        // 它们本来就退到 `‹ ›` 后面去了。先前不管哪种情况都 appendChild 到末尾，
        // 于是"新的先出现在下面、生成完整条又搬上去、上面那条消失"（用户原话）。
        var replaced = opts.replaceId
          ? threadEl.querySelector('[data-id="' + opts.replaceId + '"]')
          : null;
        if (replaced && replaced.parentNode) {
          var host = replaced.parentNode;
          host.replaceChild(messageRow(local), replaced);
          // 只撤**这一行之后的消息行**（它们是被改那条的后代，属于旧分支），
          // 碰到非消息元素就停 —— 绝不动这条以前的内容。
          var node = host.querySelector('[data-id="' + localId + '"]');
          while (node) {
            var after = node.nextElementSibling;
            if (!after || !after.classList || !after.classList.contains('chatmsg')) break;
            after.remove();
          }
        } else {
          if (threadEl.querySelector('.chat__intro')) ui.clear(threadEl);
          threadEl.appendChild(messageRow(local));
        }
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
        : opts.continueTurn
          ? { continue: true } // 不新增用户消息，只接一轮（见后端 post_message 的说明）
          : { content: text, parentId: attachTo, attachments: attachedIds };
      // 每一条都带上此刻的「深度思考」开关：重新生成、接一轮同样该遵守它
      body.thinking = state.deepThink;
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
              // **新回答一出现，就把它记成"这一层选的那一条"**。
              //
              // 重新回答时尤其关键：`regenerate` 先把这一层的选择清掉了，不在这里
              // 补回来，收尾的 `paintThread()` 会按"第一个孩子"重画 —— 界面上就是
              // "新回答闪一下，又变回旧回答"（用户："要在新的内容出现之后旧的才消失"）。
              //
              // 注意**只记选择、不重画**：旧的留在原处，新的在它下面长；直到流结束
              // 才由 `paintThread()` 收成一条线 —— 那正是"新的出现之后旧的才消失"。
              if (m && m.parentId) state.picks[keyOf(m.parentId)] = m.id;
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
              // 这类 `note` 是**系统提示**（比如"这个模型不支持工具调用，已改为直接
              // 回答"），不是模型说的话 —— 不该长在回复正文里，用户看到只会觉得
              // 莫名其妙。放到输入框上方那条安静的位置（`.chat__notice`，空着时
              // 自动隐藏），而且**同一句只显示一次**：同一件事每轮都发的话，
              // 长对话里就成了每轮刷一遍。
              //
              // 注："更早的 N 条消息没有带进来"那条**已经在服务端删掉了**
              //（见 agent_loop.py）—— 用户的原话是"直接把这个告知删掉，不要再留"。
              // 这个处理器留着，是给上面那类真正有用的提示用的。
              if (!d || !d.text) return;
              var box = document.querySelector('.chat__notice');
              if (!box || box.textContent === d.text) return;
              box.textContent = d.text;
            },
            tool: function (d) {
              if (!state.live || !d) return;
              if (d.phase === 'start') state.live.toolStart(d);
              else state.live.toolResult(d);
            },
            card: function (d) {
              if (state.live && d && d.card) state.live.pushCard(d.card);
            },
            draft: function (d) {
              if (state.live && d && d.draft) state.live.pushPart({ type: 'draft', payload: d.draft });
            },
            action: function (d) {
              if (state.live && d && d.proposal) state.live.pushAction(d.proposal);
            },
            citation: function (d) {
              if (state.live && d && d.citation) state.live.pushPart(d.citation);
            },
            demo: function (d) {
              if (!state.live || !d || !d.demo) return;
              // 跑 Python 的那条：服务端会把 kind / code / runId 一起发过来（见
              // chat.py 里那个 payload），照着长出来的零件才能被认成命令块、
              // 才能把脚本交给常驻壳。演示那条只有 html。
              state.live.pushPart({
                type: 'demo',
                kind: d.demo.kind || '',
                title: d.demo.title,
                html: d.demo.html || '',
                runId: d.demo.runId || '',
                code: d.demo.code || '',
                // 点名要装的包（`packages` 参数，动态导入那种写法）——转发给壳
                packages: d.demo.packages || [],
              });
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
          // 走到这里有两种完全不同的失败，**不能一样处理**：
          //
          //   * **服务端从没确认过这条**（`user` 事件没到，`local.id` 还叫 `local-N`）：
          //     库里根本没有它，界面上那条是假的。撤掉它、把用户打的字**还回输入框**，
          //     他改完设置（或等本地模型起来）直接重发。
          //   * **服务端已经确认过**（id 已经换成真 id，然后流才断的）：它**已经在库里**了。
          //     这时千万别撤 —— 撤走之后他重发一次就是**两条**，而且刷新一下那条又冒出来。
          //     只报一句"回复断了"，他自己点重试（`regenerate` 不会新增用户消息）。
          //     原先这里不分情况一律撤掉 + 还字，已落库的那种就是这么被冤枉的。
          var arrived = !(local && String(local.id || '').indexOf('local-') === 0);
          if (local && !arrived) {
            state.messages = state.messages.filter(function (m) {
              return m !== local;
            });
            inputEl.value = text;
            growInput();
            paintThread();
          }
          ui.toast(
            arrived ? '回复断了，但这条已经发出去（刷新后它还在，点重试即可）' : err.message,
            'error'
          );
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
      paintSend(true);
      // **生成中这一行留空**（`.chat__hint:empty` 是 `display: none`，就不占位置）。
      // 这里原来写着「正在生成…（点「停止」会留下已经生成的部分）」—— 生成的时候
      // 它把输入区推高、把正文挤掉，用户："占据一部分空间——清掉"。
      //
      // 生成中有两处更直白的表达，不需要这一行说明：发送键变成了「停止」方块，
      // 正文正在一个字一个字长出来。
      hintEl.textContent = '';
      return;
    }

    paintSend(false);

    // 这一行只说**当下这一刻**的事（正在生成、第一句话会开新对话），
    // 走哪条通道、用的哪个模型搬到设置里去了 —— 用户："内测通道的提示放到设置那边去"。
    // 它也不再常驻：`.chat__hint:empty` 不占位（"这个地方不占位置"）。
    var bits = [];
    if (!state.current) bits.push('第一句话会开一条新对话');
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
    if (force) {
      // 发消息、换会话、开一条回答这类动作：重新贴底并恢复跟随
      stickBottom = true;
      threadEl.scrollTop = threadEl.scrollHeight;
      refreshJump();
      return;
    }
    // **跟不跟，看用户意图，不看此刻离底多远。**
    //
    // 原来这里是"离底 < 120px 就跟着滚"—— 这个判据在流式里必然出事：滚轮一格
    // 约 40~100px，用户往上拉一格，gap 还在容差里，于是被每 100ms 一次的自动
    // 滚动拽回底部；再拉一格，又被拽回去 —— 表现就是"想上拉看上面的内容，
    // 会被思考内容一直往下拽"（用户原话）。而且 `render` 是先按旧布局判定跟随、
    // 更新完 DOM 再在这里重算 gap，这一拍内容长了多少会直接改变结果，
    // 行为随字数飘（实测：一格 40px 必被拽回，一格 80px 有时刚好跨过 120）。
    //
    // 现在只看 `stickBottom`：他往上翻过就停手，直到自己滚回贴底处
    //（那条 scroll 监听会把状态改回来）。
    if (stickBottom) threadEl.scrollTop = threadEl.scrollHeight;
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
    return api.post('/chat/conversations', { folder: state.newFolder || '' }).then(function (res) {
      state.current = res.conversation.id;
      state.messages = [];
      state.list.unshift(res.conversation);
      renderAside();
      updateComposer();
      return state.current;
    });
  }

  // 从资源树点一条会话过来时带着 `?c=<id>`；只认一次，免得后面每次刷新列表都跳回去
  var wantedOnce = 0;

  /**
   * 只把左栏的"当前这条"切一下 —— 不为这一下整块重建整个列表。
   *
   * 打开会话时用它代替 `renderAside()`：列表内容一条都没变，变的只有"哪条是当前"。
   * 整块重建（`ui.clear(listEl)` + 重画）会让左栏闪一下，并把悬停态与滚动位置
   * 一起丢掉（用户："从一个对话转到另一个对话的切换过程不够丝滑"）。
   */
  function markCurrent() {
    if (!listEl) return;
    Array.prototype.forEach.call(listEl.querySelectorAll('.chatlist__item'), function (el) {
      el.classList.toggle('is-on', el.getAttribute('data-conv') === String(state.current));
    });
  }

  function openConversation(id) {
    // 大题面板是**某一道题**，不是页面级的常驻物：不关掉的话，换一条对话它还杵在那儿
    // （用户反馈："换了一个对话还是出现"）。
    if (problemOpen) closeProblem();
    // **点下去就先把"当前"切过来**：左栏高亮立刻跟手，不等服务端一个来回。
    // 原先要等数据回来、`renderAside()` 跑完才亮 —— 那一下"点了没反应"的迟滞
    // 正是"不丝滑"的一半。
    state.current = id;
    markCurrent();
    return api
      .get('/chat/conversations/' + id)
      .then(function (res) {
        // 等待期间他又点了别的：这次结果作废 —— 否则先回来的旧响应会把新的顶掉
        if (state.current !== id) return;
        state.messages = (res && res.messages) || [];
        state.picks = {}; // 默认跟最新那一支
        state.live = null;
        // 这里**不再调 renderAside()**：左栏内容一条没变（变的只有"哪条是当前"，
        // 上面已经切过）。整块重建的代价见 `markCurrent` 的说明。
        ui.swap(function () {
          paintThread();
          updateComposer();
        }, threadEl.parentElement || threadEl);
      })
      .catch(function (err) {
        if (state.current !== id) return;
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
    // 记住"当前在哪一格"：新对话落在**同一个分组**里。
    // 不然在某个分组里连着干活时，每开一条都要回头自己拖进去一次。
    var cur = (state.list || []).filter(function (one) {
      return one.id === state.current;
    })[0];
    state.newFolder = cur ? cur.folder || '' : '';
    state.current = null;
    state.messages = [];
    state.live = null;
    renderAside();
    paintThread();
    updateComposer();
    inputEl.focus();
  }

  /** 置顶/取消置顶。
   *
   * 置顶区是**副本**：这条会话仍留在归档/未归档里按时间排（用户："对话置顶后
   * 位置不变，在置顶处加副本"）。所以这里只翻一个布尔、重画一次 ——
   * 不再像原先那样"按 pinned 重排整个列表"（那是"搬走"的写法）。
   */
  function togglePin(conv) {
    var next = !conv.pinned;
    api
      .patch('/chat/conversations/' + conv.id, { pinned: next })
      .then(function () {
        conv.pinned = next;
        renderAside();
        ui.toast(next ? '已在置顶区加了一份（原位置不动）' : '已取消置顶', 'info', 1600);
      })
      .catch(function (err) {
        ui.toast(err.message, 'error');
      });
  }

  /** 归档 / 取消归档（"文件夹"那层逻辑，只有一层）。
   *
   * 与置顶**互不干涉**（用户："有没有归档都可以置顶"）：归档一条置顶过的对话，
   * 它在置顶区那份副本照旧在，只是原位置从"未归档"挪进"归档"。
   */
  function toggleArchive(conv) {
    var next = !conv.archived;
    api
      .patch('/chat/conversations/' + conv.id, { archived: next })
      .then(function () {
        conv.archived = next;
        renderAside();
        ui.toast(next ? '已归档' : '已移出归档', 'info', 1400);
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

  /** 打开"重命名"对话框。F2 与右键菜单都走这里 —— 只留一条路，别两份。 */
  function renameConversation(conv) {
    if (!conv) {
      toast('先点一下要改名的那条对话（或者光标停在它上面按 F2）', 'warn');
      return;
    }
    askName('重命名对话', conv.title || '', '保存').then(function (name) {
      if (!name || name === conv.title) return;
      api
        .patch('/chat/conversations/' + conv.id, { title: name })
        .then(function () {
          conv.title = name;
          renderAside();
          toast('已改名为「' + name + '」', 'ok');
        })
        .catch(function (err) {
          toast('改名失败：' + ((err && err.message) || '未知原因'), 'bad');
        });
    });
  }

  /** 会话行的右键菜单：重命名 / 置顶 / 删除（图标 + 文字，删除是红的）。
   *
   * 原先这一行**压根没有右键**（容器那条监听只认空白处与分组行），F2 也没有 ——
   * 用户的原话："对话右键/F2 没法改名字"。置顶/删除原先藏在 `⋯` 的弹层里
   *（那个弹层还在，导出也留它那儿）。 */
  function openConversationMenu(conv, ev) {
    if (ev) {
      chatMenuX = ev.clientX;
      chatMenuY = ev.clientY;
    }
    showChatMenu([
      {
        label: '重命名',
        icon: 'rename',
        hint: 'F2',
        run: function () {
          renameConversation(conv);
        },
      },
      {
        label: conv.pinned ? '取消置顶' : '置顶',
        icon: conv.pinned ? 'unpin' : 'pin',
        run: function () {
          togglePin(conv);
        },
      },
      {
        // 归档 = 左栏那一层"文件夹"（用户："分组其实就是文件夹归档的逻辑"）。
        // 放在置顶与删除中间：它是"收拾"，删除才是"丢掉"。
        label: conv.archived ? '取消归档' : '归档',
        icon: conv.archived ? 'unarchive' : 'archive',
        run: function () {
          toggleArchive(conv);
        },
      },
      {
        label: '删除',
        icon: 'trash',
        danger: true,
        run: function () {
          removeConversation(conv);
        },
      },
    ]);
  }

  // F2 = 重命名。先看焦点在哪一行（点过它就有焦点），没有就用手上打开着的那个。
  document.addEventListener('keydown', function (ev) {
    if (ev.key !== 'F2') return;
    ev.preventDefault();
    var node =
      document.activeElement && document.activeElement.closest
        ? document.activeElement.closest('.chatlist__item')
        : null;
    var id = node ? String(node.getAttribute('data-conv') || '') : '';
    var found = null;
    (state.list || []).forEach(function (one) {
      if (found) return;
      if ((id && one.id === id) || (!id && one.id === state.current)) found = one;
    });
    renameConversation(found);
  });

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
          label: conv.pinned ? '取消置顶' : '置顶',
          onClick: function () {
            togglePin(conv);
          },
        },
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
    // 能力选择栏（工具挂载）由 mounts.js 画进 `#chat-modes`；骨架建好就让它画一次
    if (QF.mounts && QF.mounts.render) QF.mounts.render();
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

  /** 把这一整套对话界面渲染进**任意宿主**（工作台的窗格里用）。
   *
   *  为什么能这么简单：`rootEl` 本来就是可变的（页面上是 `#app-root`），
   *  建骨架与后续渲染全都写在它里面 —— 换一个宿主就等于"这一页搬到那一格"。
   *  用户的原话是："我在资源页面点击对话的时候，就应该直接把对话给我看，
   *  而不是跳转到专门的对话页面。"
   *
   *  `opts.id` 打开某条会话；`opts.fresh` 直接开一条新的（左栏那个 ＋）。
   */
  function mount(host, opts) {
    if (!host) return null;
    opts = opts || {};
    rootEl = host;
    rootEl.textContent = '';
    buildSkeleton();
    if (QF.mounts && QF.mounts.render) QF.mounts.render();
    renderAside();
    renderEmptyThread();
    updateComposer();
    refreshAiState();
    var opened = loadList().then(function () {
      if (opts.fresh) return onNewClick();
      var want = opts.id;
      var target = want
        ? state.list.filter(function (c) {
            return c.id === want;
          })[0]
        : null;
      if (!target && state.list.length) target = state.list[0];
      if (target) return openConversation(target.id);
      return undefined;
    });
    return function cleanup() {
      // 收摊：把 rootEl 指向还给页面，免得下一次挂载挂到已经不在的节点上
      rootEl = document.getElementById('app-root');
      opened.catch(function () {
        /* 还没跑完就被关掉了：什么都不用做 */
      });
    };
  }

  var chat = { boot: boot, booted: false, mount: mount };
  QF.chat = chat;
})();
