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
  //: **传送球**（回溯的门）：贴在正文区边上的一颗圆球，拖动可挪、点开是一列带序号的
  //: 回溯点，点一枚就传过去。一个回溯点都没有时它整个不出现（见 `renderStations`）。
  var stationsEl = null;
  var notesEl = null;
  //: 书签抽屉：装"永久标记"那一类（见 `renderMarks`）
  var marksEl = null;
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
    //: 正在被「重试」顶掉的那一条（见 `regenerate`）；没有替代在飞时为 null。
    //:
    //: 干什么用：重试一条**失败**的消息时，那一行（就一行报错）要**立刻**从屏幕上撤掉。
    //: 它跟"重新回答一条成功的回答"不是一回事 —— 成功那条有内容可读，"留在原处、
    //: 新回答在下面慢慢长"是舒服的；失败那条留着就是让人**盯着一行报错等十几秒**
    //:（用户："旧的出错信息要等到新的信息完全生成才会消失"）。
    //:
    //: 但**只从 DOM 删是不够的**：`activePath()` 的兜底是"跟着最新那一支"，而按下
    //: 重试的那一瞬间替代品还没落地 —— 中间任何一次重画都会把那行报错放回来。
    //: 所以记一笔、让**兜底**跳过它；这一轮尝试一结束就清掉（`send()` 末尾），
    //: 于是"替代品压根没生成出来"时它会**自己回来**（连同那个重试按钮 ——
    //: 不然下一次失败就没地方点了）。
    replacing: null,
    //: 对话树里**被收起**的那几支：`{ 消息 id: true }`（见 `toggleBranchFold`）。
    //:
    //: 纯界面状态：不进库、也不影响数据 —— "收起"只是**这张图上先不画它下面那一段**，
    //: 消息一条都没少，展开、翻分支、接着问都照旧。
    treeFolded: {},
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
    //: 这一版树**摆过位置没有**（也就是"适配窗口"算过没有）。见 `resetTreeView`。
    fitted: false,
    //: 缩放的**下限**。不写死：适配窗口算出来的可能远小于 0.2（158 个节点的长对话实测
    //: 0.066），写死就会出现"适应窗口能到的视图，自己滚轮滚不下去"（用户报的）。
    //: 每次适配时按算出来的 scale 更新，见 `drawTree`。上限不随它变。
    minK: 0.2,
    //: 轨迹：把当前分支单独描一条线（见 `treeTrail`）
    trail: false,
    //: 播放：按时间顺序把整棵树放一遍。`at` = 已经点到第几枚（按 id 排的序号）；
    //: `limit` = 已经长出来的最大 id（轨迹按它决定画到哪儿）
    play: { on: false, at: 0, timer: 0, speed: 1, limit: 0 },
    //: 画这一棵树时算好的模型（轨迹要按节点坐标穿线，见 `trailD`）
    trailModel: null,
  };

  /** 让**下一次**画树重新适配窗口。
   *
   * 干什么用：画树有很多种原因（打开、收起一支、删掉一支、切分支、换会话…），
   * 但**不是每一种都该重算缩放**。原先 `drawTree` 里那段适配是无条件的，于是
   * 任何一次重画都会把用户刚放大、刚拖到的位置一把扔掉 —— 表现就是
   * "我放大了删，删完页面跳回了全局视图"（用户原话）。
   *
   * 现在只有这里显式声明过"该重算"时才重算，其余情况保持当前视角。
   * 想手动回全局视图有现成入口：工具栏那个「适应窗口」。
   */
  function resetTreeView() {
    TREE.view = { x: 0, y: 0, k: 1 };
    TREE.fitted = false;
  }

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
      // 被收起的那一支**当作它没有孩子** —— 于是"自底向上量宽度"与"自顶向下摆位置"
      // 那两趟会自然跳过整棵子树，不必在布局算法里到处加判断（"收起"能一行接进现有
      // 布局，就靠这一处）。
      if (state.treeFolded[keyOf(id)]) return [];
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
            // 打开 = 重新适配一次（关掉再开，就该是"重新看这张图"）
            if (state.treeOpen) resetTreeView();
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

    // 书签抽屉的入口**不在这条工具栏上**，在对话树里（用户："把书签抽屉做到对话树里面，
    // 不要放在 chat 页面里"）。理由也顺：书签是**地图上的地点**，抽屉就是那张图的地点
    // 清单 —— 和地图待在一起才找得到，摆在 chat 顶栏上它跟"清理 / 分享"成了一类东西。

    // 分享：把这一条会话装配成**一个自带数据的网页**（对话正文 + 那棵对话树），
    // 发给别人双击就能看。装配在服务端做（实测 158 条 37ms），所以**不做**"正在打包"
    // 那一套 —— 成功与失败都由 toast 说清楚（见 onShare）。
    barEl.appendChild(
      h(
        'button.chat__asidefold.chat__sharebtn',
        {
          type: 'button',
          title: '分享这一条对话（生成一个能直接发出去的网页）',
          'aria-label': '分享',
          draggable: 'false',
          onClick: onShare,
        },
        iconNode('share', 15)
      )
    );
  }

  function renderTree() {
    if (!treeEl) return;
    ui.clear(treeEl);
    if (!state.treeOpen) {
      // 地图收起来了，抽屉跟着收（它是这张图上的一栏）。
      // 这一处是**唯一的收口**：点节点、点图钉、工具栏的 ✕、Esc 走的都是
      // `state.treeOpen = false`，谁都不必记得顺手去关抽屉。
      if (marksTimer) {
        clearTimeout(marksTimer);
        marksTimer = 0;
      }
      marksOpen = false;
      marksClosing = false;
      renderMarks();
      stopPlay();   // 地图收起来了，播放也停（计时器不能留着空转）
      return;
    }

    var close = function () {
      state.treeOpen = false;
      renderBar();
      renderTree();
    };

    var canvas = h('div.chattree__canvas');
    treeEl.appendChild(h('div.chattree__screen', null, treeBar(close), canvas));
    paintPlayUI();   // 工具栏刚挂上去，把"播放/速度"两颗键的字刷对
    drawTree(canvas);
    // 抽屉装在**画布这一层**里，不是整屏：遮罩只压地图，工具栏还留着 ——
    // 否则开着抽屉时那颗「书签」自己也被压住，点不动了。
    canvas.appendChild(marksEl);
    renderMarks();
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
    var bar = h(
      'div.chattree__bar',
      null,
      h('span.chattree__title', { text: '对话树' }),
      h('span.chattree__sub', {
        text:
          state.messages.length + ' 个节点 · 亮的是当前分支 · 点节点切过去' +
          ' · 图钉 = 标过的地点',
      }),
      dirButton,
      h(
        'button.chattree__btn',
        {
          type: 'button',
          onClick: function () {
            resetTreeView();
            renderTree();
          },
        },
        '适应窗口'
      ),
      // 轨迹：把当前分支单独描一条线（"我走过的路"）。点一下开、再点一下关。
      h(
        'button.chattree__btn.chattree__trailbtn' + (TREE.trail ? '.is-on' : ''),
        {
          type: 'button',
          title: TREE.trail
            ? '收起轨迹线'
            : '描一条轨迹：所有节点按创建时间串起来（我真正的探索旅程）',
          'aria-pressed': TREE.trail ? 'true' : 'false',
          onClick: function () {
            TREE.trail = !TREE.trail;
            renderTree();
          },
        },
        iconNode('route', 14),
        h('span', { text: '轨迹' })
      ),
      // 播放：按时间顺序把整棵树放一遍（多久一枚由旁边那颗速度键定）
      h('button.chattree__btn.chattree__playbtn', { type: 'button', onClick: togglePlay }, '▶ 播放'),
      h('button.chattree__btn.chattree__speedbtn', { type: 'button', onClick: cyclePlaySpeed }, '1×'),
      // 书签抽屉的入口：**这张地图的地点清单**（用户："书签抽屉是要做的"）。
      // 它长在地图的工具栏上，不长在 chat 页 —— 那里是地图外面。
      (function () {
        var marks = bookmarksOf(QF.store.places());
        return h(
          'button.chattree__btn.chattree__marksbtn' + (marksOpen ? '.is-on' : ''),
          {
            type: 'button',
            title: marks.length
              ? '书签抽屉 · ' + marks.length + ' 个永久标记（含别的对话里的）'
              : '书签抽屉（还没标过地方）',
            'aria-expanded': marksOpen ? 'true' : 'false',
            onClick: toggleMarks,
          },
          iconNode('bookmark', 14),
          h('span', { text: marks.length ? '书签 · ' + marks.length : '书签' })
        );
      })(),
      iconButton('close', '关闭（Esc）', close)
    );
    return bar;
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
          // 这条边属于哪个子节点：播放时"轮到谁就亮谁"（见 `markNodeShown`）
          'data-edge': String(edge.to),
        })
      );
    });

    // **轨迹**：把"我走过的那条路"单独描一遍。画在连线之上、节点之下 —— 于是它压着
    // 那几条灰线显眼，而穿过节点的部分又被节点自己盖住（看不见横穿方框的线）。
    var trail = treeTrail(model);
    if (trail) layer.appendChild(trail);

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
          (message.id === state.editing ? ' is-editing' : '') +
          // 被收起的一支：边框换成虚线（"这儿本来还有东西，是收起来了"）
          (state.treeFolded[keyOf(message.id)] ? ' is-folded' : ''),
        transform: 'translate(' + node.x + ',' + (node.y - node.h / 2) + ')',
        // 播放时按 id 找它（见 `markNodeShown`）
        'data-node': String(message.id),
      });
      // **里面再套一层**：出生动画（"长大"）要缩放，而 CSS 的 transform 会顶掉上面那个
      // 定位用的 transform 属性 —— 一碰它，节点就跳到原点去了。缩放只能落在里层。
      var ink = sv('g', { class: 'ctnode__ink' });
      group.appendChild(ink);
      ink.appendChild(
        sv('rect', { class: 'ctnode__box', x: 0, y: 0, width: TREE.w, height: node.h, rx: 12, ry: 12 })
      );
      ink.appendChild(
        sv('text', { class: 'ctnode__who', x: 12, y: 18 }, [
          document.createTextNode(message.role === 'user' ? '我' : 'AI'),
        ])
      );
      // 右上角那枚角标：分叉数（这里有几个兄弟分支）／这一支被收起时，换成"藏了多少条"
      var hidden = state.treeFolded[keyOf(message.id)] ? descendantsOf(message.id).length : 0;
      var badge = hidden ? '＋' + hidden : node.forks > 1 ? '⑂' + node.forks : '';
      if (badge) {
        ink.appendChild(
          sv(
            'text',
            {
              class: 'ctnode__fork' + (hidden ? ' is-folded' : ''),
              x: TREE.w - 34,
              y: 18,
              'text-anchor': 'end',
            },
            [document.createTextNode(badge)]
          )
        );
      }
      // 「⋯」= 这一支上能做的事（收起 / 删除）。
      // **常驻显示，不做"hover 才出现"** —— 手机上根本没有 hover，藏起来等于没有，
      // 而提这个需求的正是手机上的那个人。
      var more = sv('g', { class: 'ctnode__more' });
      // **命中区比看得见的方块大一圈**：这棵树要缩到装得下（158 个节点时 k≈0.31），
      // 一个 22px 的方块到屏幕上只剩 7px，手指根本点不准 —— 而催这个功能的正是
      // 手机上那个人。`fill: transparent`（不是 `none`）才收得到指针事件。
      more.appendChild(
        sv('rect', {
          class: 'ctnode__morehit',
          x: TREE.w - 38,
          y: -2,
          width: 36,
          height: 30,
        })
      );
      more.appendChild(
        sv('rect', {
          class: 'ctnode__morebox',
          x: TREE.w - 28,
          y: 5,
          width: 22,
          height: 18,
          rx: 5,
          ry: 5,
        })
      );
      more.appendChild(
        sv('text', { class: 'ctnode__moredots', x: TREE.w - 17, y: 18, 'text-anchor': 'middle' }, [
          document.createTextNode('⋯'),
        ])
      );
      more.addEventListener('click', function (event) {
        // 不 stop 的话会连带触发节点那个"跳到这条消息"：面板当场关掉，菜单也跟着没了
        event.stopPropagation();
        event.preventDefault();
        var box = more.getBoundingClientRect();
        chatMenuX = box.left;
        chatMenuY = box.bottom + 4;
        openNodeMenu(message);
      });
      ink.appendChild(more);
      node.lines.forEach(function (line, index) {
        ink.appendChild(
          sv('text', { class: 'ctnode__line', x: 12, y: 36 + index * 15 }, [
            document.createTextNode(line),
          ])
        );
      });
      // 模式那一行画在正文下面、框内最后一格：一眼看得出"从这一轮起换了模式"
      if (node.mode) {
        ink.appendChild(
          sv('text', { class: 'ctnode__mode', x: 12, y: 36 + node.lines.length * 15 }, [
            document.createTextNode(node.mode),
          ])
        );
      }

      // **右键 = 在这个地点上做标记**（用户："支持一下在对话树的节点上直接右键添加书签
      // 和回溯点的机制"）。所以这一条监听**每个节点都挂**，而不是"有钉子才挂"：
      // 地图上右键一处地点，该给的是"在这儿打个标记"，加与改都在同一张菜单里
      //（`openPlaceMenu` 按类别成组：没有的给"加"，有的给"写描述 / 取消"）。
      //
      // 位置标记本身**不画在这棵树里**：图钉是屏幕坐标的一层 HTML，见 `treePinsLayer`
      //（用户："太小太不明确。想象你要在一张地图上标记某个地点"—— 画在 SVG 里就得跟着
      // 地图一起缩，缩到 0.3 倍就只剩两三个像素）。左键仍是"跳到这条"。
      group.addEventListener('contextmenu', function (event) {
        event.preventDefault();
        event.stopPropagation();
        chatMenuX = event.clientX;
        chatMenuY = event.clientY;
        // **按当下重查一遍**：`message` 是建这一层时抓的快照，钉过之后它就过期了
        //（消息行尾那两枚图标踩过同一个坑，见 `placeButtons`）
        openPlaceMenu(QF.store.placesOf(message.id), refreshPlaces, [
          { kind: 'mark', m: message },
          { kind: 'back', m: message },
        ]);
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
    // **地图上的图钉**：挂在画布上的**一层 HTML**，不跟着 SVG 缩放。
    // 地点是地图坐标（`node.x/node.y`），图钉是屏幕坐标，两者在 `applyTreeView` 里换算。
    // 这么分的理由就是用户那句话（"太小太不明确。想象你要在一张地图上标记某个地点"）：
    // 图钉得**始终认得出来**，而地图可以缩到 0.3 倍去装下 158 个节点。
    host.appendChild(treePinsLayer(model));
    host.appendChild(treeLegend());
    host.appendChild(
      // 提示语要把**双指**写进去：触屏上没有滚轮，只写"滚轮缩放"等于告诉手机
      // 用户"这棵树不能放大"（原来就是这么写的，而他确实找不到怎么放大）。
      h('div.chattree__hint', { text: '双指或滚轮缩放 · 拖动平移 · 点图钉去那个地点' })
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
    TREE.bounds = { minX: minX, minY: minY, maxX: maxX, maxY: maxY };
    // **只在还没摆过的时候适配窗口**，其余情况保持用户当前的缩放与位置。
    // 这里每重画一次就重算一次的话，用户刚在图上找到的位置就没了（见 `resetTreeView`）。
    if (!TREE.fitted) {
      var scale = Math.min((width - pad * 2) / Math.max(1, maxX - minX), (height - pad * 2) / Math.max(1, maxY - minY), 1.1);
      TREE.view.k = scale;
      TREE.view.x = (width - (maxX - minX) * scale) / 2 - minX * scale;
      TREE.view.y = (height - (maxY - minY) * scale) / 2 - minY * scale;
      TREE.fitted = true;
      // 缩放下限跟着这一次适配走，再留一点余量（0.75）：能比"刚好装下"再拉远一档，
      // 好让整张图在中间、四周留白。小树时 scale 可能 >0.27，那就还是 0.2 打底。
      TREE.minK = Math.min(0.2, scale * 0.75);
    }
    applyTreeView(svg);
    // 重画之后把播放状态与轨迹重新贴上去
    //（播放的计时器活在 DOM 之外，见 `TREE.play`；轨迹的显隐规则见 `applyTrail`）
    applyPlayState();
    applyTrail();

    // ---- 手势：一根指头拖动 = 平移，两根指头 = 缩放 ----------------------
    //
    // 为什么要自己接缩放：`.chattree__svg` 上有 `touch-action: none`（平移需要它，
    // 不然一拖就把页面滚走了），而它的代价是**浏览器手势也被一起关掉，包括双指
    // 缩放**。于是这棵树在触屏上只平移、不能放大 —— 桌面那套"滚轮缩放"在手机上
    // 没有任何对应物。用户原话：分享页传到移动端，"没办法用手指放大对话树"。
    var pointers = new Map(); // pointerId -> {x,y}：双指要算**两根**各自的位置
    var pinch = null;         // 双指手势的基准（见 `startPinch`）

    /** 缩放的上下限。**下限是动态的**（`TREE.minK`，适配窗口时算出来）。
     *
     * 上限仍然是写死的 2.4：放大到那儿节点字已经很大（12px × 2.4），再大只是空转。
     * 下限不行 —— 它必须能覆盖"适配窗口"到的那个倍数，否则那一个视图就只能靠按钮去，
     * 手势到不了（用户："点击适应窗口可以跳转到更高的视图，但是自己用滚轮滚不到这么高"）。
     */
    function clampK(k) {
      return Math.max(TREE.minK || 0.2, Math.min(2.4, k));
    }

    /** 以 (px,py)（svg 局部坐标）为中心缩放 `factor` 倍。
     *
     * 滚轮与双指**共用这一条**："让那个点在原地不动"的公式只该有一份 ——
     * 视图是 `translate(x,y) scale(k)`，要让点 p 指着的内容不动，
     * 新偏移就是 `p - (p - x) * (k'/k)`。 */
    function zoomAt(px, py, factor) {
      var k = clampK(TREE.view.k * factor);
      TREE.view.x = px - (px - TREE.view.x) * (k / TREE.view.k);
      TREE.view.y = py - (py - TREE.view.y) * (k / TREE.view.k);
      TREE.view.k = k;
      applyTreeView(svg);
    }

    /** 前两根手指的距离与中点（Client 坐标）。第三根起忽略 ——
     * 多点几下不该把画面甩飞。 */
    function pinchPair() {
      var list = [];
      pointers.forEach(function (p) {
        if (list.length < 2) list.push(p);
      });
      if (list.length < 2) return null;
      return {
        d: Math.max(1, Math.hypot(list[0].x - list[1].x, list[0].y - list[1].y)),
        cx: (list[0].x + list[1].x) / 2,
        cy: (list[0].y + list[1].y) / 2
      };
    }

    function startPinch() {
      var two = pinchPair();
      if (!two) return;
      var box = svg.getBoundingClientRect();
      pinch = {
        d0: two.d,
        cx: two.cx - box.left,
        cy: two.cy - box.top,
        x0: TREE.view.x,
        y0: TREE.view.y,
        k0: TREE.view.k
      };
    }

    /** 双指实时：距离比 = 倍数，中点 = 中心。
     *
     * 按**基准**算，不按上一帧累加：累加会把每帧的取整误差攒起来，两指拉开再
     * 缩回去之后画面回不到原位。这里只用"开头中指在哪、开头视图是什么"，
     * 所以**拉开再缩回就是原样**。
     *
     * 中点自己也会动（两指一起挪），`(当前中点 - 基准中点)` 这一项就同时把
     * 手势里的平移带上了 —— 一行里既缩放又平移。 */
    function applyPinch() {
      var two = pinchPair();
      if (!pinch || !two) return;
      var box = svg.getBoundingClientRect();
      var k = clampK(pinch.k0 * (two.d / pinch.d0));
      var ratio = k / pinch.k0;
      TREE.view.x = (two.cx - box.left) - (pinch.cx - pinch.x0) * ratio;
      TREE.view.y = (two.cy - box.top) - (pinch.cy - pinch.y0) * ratio;
      TREE.view.k = k;
      applyTreeView(svg);
    }

    /** 一次滚轮走多远：**按 delta 的大小**算，不按它的符号。
     *
     * 原先是"一格固定 1.12"。鼠标滚轮一格 1.12 还算合适，但触控板一次滚动会连发十几个
     * 小 delta（3~10），每个都被当成"一格"，于是轻轻一滑就飞出老远；反过来，要从全局
     * 视图拉到最紧（0.066 那种）得滚二十几格。按比例走两边都顺（地图类应用都是这个手感）。
     *
     * `deltaMode` 要归一：Firefox 给的是"行"（deltaMode 1，deltaY 约 ±3），
     * 归一成像素才不会在它上面变得几乎不动。
     */
    function wheelFactor(deltaY, deltaMode) {
      var px = deltaMode === 1 ? deltaY * 16 : deltaMode === 2 ? deltaY * 100 : deltaY;
      return Math.max(0.5, Math.min(2, Math.exp(px * -0.0015)));
    }

    svg.addEventListener('wheel', function (event) {
      event.preventDefault();
      var box = svg.getBoundingClientRect();
      // 触控板**捏合**走的也是这一条（它发的是带 ctrlKey 的 wheel，delta 小、频率高）：
      // 比例式对它同样合适，所以不另开分支。
      zoomAt(event.clientX - box.left, event.clientY - box.top,
             wheelFactor(event.deltaY, event.deltaMode));
    });

    svg.addEventListener('pointerdown', function (event) {
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      // 第二根指头落下 = 这一手是要缩放，不是要平移。**当场把平移取消**，
      // 否则第一根指头攒下的位移会先跳一下，再开始缩放。
      if (pointers.size === 2) {
        TREE.drag = null;
        svg.classList.remove('is-panning');
        startPinch();
        return;
      }
      // 落在节点上就**不要**接管：`setPointerCapture` 会把后续指针事件全部
      // 抢到 svg 自己身上，于是节点那个 `click` 永远不触发（"点节点没反应"
      // 就是这么来的 —— 切换逻辑本身是好的，是它根本没被调到）。
      if (event.target && event.target.closest && event.target.closest('.ctnode')) return;
      TREE.drag = { x: event.clientX, y: event.clientY, vx: TREE.view.x, vy: TREE.view.y };
      svg.setPointerCapture(event.pointerId);
      svg.classList.add('is-panning');
    });

    svg.addEventListener('pointermove', function (event) {
      var rec = pointers.get(event.pointerId);
      if (rec) {
        rec.x = event.clientX;
        rec.y = event.clientY;
      }
      if (pinch) {
        applyPinch();
        return;
      }
      if (!TREE.drag) return;
      TREE.view.x = TREE.drag.vx + (event.clientX - TREE.drag.x);
      TREE.view.y = TREE.drag.vy + (event.clientY - TREE.drag.y);
      applyTreeView(svg);
    });

    var endDrag = function (event) {
      if (event && event.pointerId !== undefined) pointers.delete(event.pointerId);
      // 少于两根就结束缩放。剩下一根**不接着平移** —— 那一下会突然蹦一段；
      // 想接着拖就抬手重按（地图类应用都是这个手感）。
      if (pointers.size < 2) pinch = null;
      TREE.drag = null;
      svg.classList.remove('is-panning');
    };
    svg.addEventListener('pointerup', endDrag);
    svg.addEventListener('pointercancel', endDrag);
  }

  function applyTreeView(svg) {
    var layer = svg.querySelector('.chattree__layer');
    if (layer) {
      layer.setAttribute(
        'transform',
        'translate(' + TREE.view.x + ',' + TREE.view.y + ') scale(' + TREE.view.k + ')'
      );
    }
    // 图钉：**用户坐标 → 屏幕坐标**，在这一处换算（滚轮、拖动、适应窗口最后都落到这里）。
    // 图钉自己不缩放，所以不能放进 SVG —— 它是地图上的 POI：地点跟着地图动，
    // 图钉始终是那个大小、那个字号。
    var pins = svg.parentNode ? svg.parentNode.querySelector('.chattree__pins') : null;
    if (!pins) return;
    var k = TREE.view.k;
    var ox = TREE.view.x;
    var oy = TREE.view.y;
    // 缩到看不清字的时候（0.45 倍以下）只留图钉、收起名字：一屏几十个名字会互相压，
    // 反而谁也读不出来。鼠标停在某一枚上，它自己的名字照旧浮出来（CSS 里那两条规则）。
    pins.classList.toggle('is-tight', k < 0.45);
    // 再远一档：连钉帽也收小一点。一屏几十枚 22px 的圆会糊成一片
    //（这条长对话适配窗口后 k 只有 0.066，实测就是那么糊）。
    pins.classList.toggle('is-small', k < 0.16);
    var items = pins.querySelectorAll('.ctpin2');
    for (var i = 0; i < items.length; i++) {
      var el = items[i];
      var ax = Number(el.dataset.ax);
      var ay = Number(el.dataset.ay);
      if (!isFinite(ax) || !isFinite(ay)) continue;
      // 后一个 translate 是**相对自身**的：把尖头（元素底边中点）落在锚点上。
      // 再后一个把同一地点上的第 2、3 枚抬起来（`PIN_STEP`）—— 这一句必须在**屏幕
      // 坐标**里做：锚点会跟着地图缩放，缩到 0.066 倍时 26px 只剩 1.7px，两枚钉子
      // 又叠回去了（第一版就是这么栽的）。
      var slot = Number(el.dataset.slot) || 0;
      el.style.transform =
        'translate(' + (ax * k + ox) + 'px,' + (ay * k + oy) + 'px) translate(-50%, -100%)' +
        (slot ? ' translate(0,' + -slot * PIN_STEP + 'px)' : '');
    }
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
      if (!chosen) {
        // 兜底：跟着最新那一支。**跳过正在被重试顶掉的那条**（见 `state.replacing`）：
        // 它已经从屏幕上撤了，而替代品可能还要几秒才落地 —— 这中间不该把它放回来。
        for (var j = kids.length - 1; j >= 0; j--) {
          if (!state.replacing || String(kids[j].id) !== String(state.replacing.id)) {
            chosen = kids[j];
            break;
          }
        }
      }
      // 这一层暂时没有可显示的（刚按下重试就是这个形状）：流式那条会接在它后面
      if (!chosen) break;
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

  /** 一个节点下面的**所有后代**（不含它自己）—— "收起"与"删除"两处都要先数清楚。 */
  function descendantsOf(id) {
    var out = [];
    var frontier = childrenOf(id);
    var guard = 0;
    while (frontier.length && guard++ < 500) {
      var next = [];
      frontier.forEach(function (one) {
        out.push(one);
        next = next.concat(childrenOf(one.id));
      });
      frontier = next;
    }
    return out;
  }

  /** 收起 / 展开一支（只影响这张图，见 `state.treeFolded`）。 */
  function toggleBranchFold(id) {
    var key = keyOf(id);
    if (state.treeFolded[key]) delete state.treeFolded[key];
    else state.treeFolded[key] = true;
    renderTree();
  }

  /** 删掉一个节点，**连带它下面的整棵子树**（服务端只认这一种语义，见 `delete_message`）。
   *
   * 确认框里写的是**确数**（"连同它下面 N 条"）：用户是在树上点了一个节点，
   * 他看不见那下面挂了多少 —— 只说"确定要删吗"，他没法判断自己在删什么。
   */
  function deleteBranch(message) {
    var doomed = descendantsOf(message.id);
    var total = doomed.length + 1;
    ui.confirm(
      total > 1
        ? '连它下面那 ' + doomed.length + ' 条一起删掉。删了不能恢复。'
        : '删掉这一条。删了不能恢复。',
      { title: '删掉这一支（' + total + ' 条）？', okLabel: '删掉', danger: true }
    ).then(function (yes) {
      if (!yes) return;
      api
        .del('/chat/conversations/' + state.current + '/messages/' + message.id)
        .then(function () {
          forgetBranch(message, doomed);
        })
        .catch(function (err) {
          ui.toast('删除失败：' + ((err && err.message) || '未知原因'), 'bad');
        });
    });
  }

  /** 把这一支从**本地**的树上抹掉（删除与"分出去"共用这一段）。
   *
   * 两处要做的是同一件事：`state.messages` 里去掉，并把 `treeFolded` / `picks` 里
   * 那些指向已不存在 id 的残留清掉 —— 留着不会当场报错，但那是**听不懂的残留**，
   * 下一次撞上同一个 id 就会指向一条已经不在这棵树上的消息。
   */
  function forgetBranch(message, doomed) {
    var gone = {};
    gone[String(message.id)] = true;
    doomed.forEach(function (one) {
      gone[String(one.id)] = true;
    });
    state.messages = state.messages.filter(function (one) {
      return !gone[String(one.id)];
    });
    Object.keys(state.treeFolded).forEach(function (key) {
      if (gone[key]) delete state.treeFolded[key];
    });
    Object.keys(state.picks).forEach(function (key) {
      if (gone[String(state.picks[key])]) delete state.picks[key];
    });
    if (state.treeOpen) renderTree();
    paintThread();
  }

  /** 把这一支**搬进一条新对话**（剪枝的另一个去处：不是删掉，是分家）。
   *
   * 用户："剪枝功能（删除子树），多一个选项：彻底删除/另成一棵对话树"。
   * 两种意图到这一步才分得开：**删**是"我不想要了"，**分**是"它不该长在这一棵上"。
   * 服务端只改籍（消息 id 不动，见 `split_message`），这边跟着把这一支从树上抹掉，
   * 再把左栏刷一遍 —— 新对话就在那儿等着。
   */
  function splitBranch(message, keep) {
    var doomed = descendantsOf(message.id);
    var total = doomed.length + 1;
    ui.confirm(
      keep
        ? '把这 ' + total + ' 条复制到一条新对话里：原树一点不动，两边从此各走各的。' +
          '消息本身一条不丢（附件也跟着复制一份）。'
        : '把这 ' + total + ' 条分到一条新对话里：它会从当前这棵树上消失（其余部分不动），' +
          '消息本身一条不丢。',
      {
        title: (keep ? '复制成新对话树（' : '另成一棵对话树（') + total + ' 条）？',
        okLabel: keep ? '复制出去' : '分出去',
      }
    ).then(function (yes) {
      if (!yes) return;
      api
        .post('/chat/conversations/' + state.current + '/messages/' + message.id + '/split', {
          keep: !!keep,
        })
        .then(function (res) {
          // **剪下去**：本地这一支要跟着消失（与删除共用 `forgetBranch`）。
          // **留一份**：原树一点不动 —— 本地这条分支不改，只是左栏多出一条，
          // 所以这里**不碰** `state.messages`（碰了就成了"本地看起来也被剪了"）。
          if (!keep) forgetBranch(message, doomed);
          loadList();   // 左栏多出一条（新对话）；剪的时候当前这条的条数也变了
          var title = (res && res.conversation && res.conversation.title) || '新对话';
          ui.toast(
            keep
              ? '已复制成新对话树「' + title + '」—— 原树没动，新树在左边会话栏里'
              : '已另成一棵对话树「' + title + '」—— 在左边会话栏里',
            'info',
            3600
          );
        })
        .catch(function (err) {
          ui.toast((keep ? '复制' : '分出去') + '失败：' + ((err && err.message) || '未知原因'), 'bad');
        });
    });
  }

  /** 树节点右上角那个「⋯」：这一支上能做的两件事。 */
  function openNodeMenu(message) {
    var hidden = descendantsOf(message.id).length;
    var folded = !!state.treeFolded[keyOf(message.id)];
    var items = [];
    if (hidden) {
      items.push({
        icon: folded ? 'unfold' : 'fold',
        label: folded ? '展开这一支' : '收起这一支',
        hint: folded ? '放回 ' + hidden + ' 条' : '藏起 ' + hidden + ' 条',
        run: function () {
          toggleBranchFold(message.id);
        },
      });
    }
    // 有子树时才谈"分家"：单独一条消息另成一棵对话树没有意义。
    // 两个去处都摆出来（用户："另成一棵树，有两种选项，一种是不保留，一种是保留
    // 原树的子树"）—— 差别只在**原树留不留这一支**，菜单上就得说清是哪一种。
    if (hidden) {
      items.push({
        icon: 'branch',
        label: '另成一棵对话树',
        hint: hidden + 1 + ' 条 · 从这棵树里剪下去',
        run: function () {
          splitBranch(message, false);
        },
      });
      items.push({
        icon: 'copy',
        label: '复制成新对话树',
        hint: hidden + 1 + ' 条 · 原树保留这一支',
        run: function () {
          splitBranch(message, true);
        },
      });
    }
    items.push({
      icon: 'trash',
      danger: true,
      // 两个去处摆在一起之后，"删掉"要说清是**哪一种**删：连根拔掉、不能恢复
      label: hidden ? '彻底删掉这一支' : '彻底删掉这一条',
      hint: hidden + 1 + ' 条 · 不能恢复',
      run: function () {
        deleteBranch(message);
      },
    });
    showChatMenu(items);
  }

  var LOCAL_ID = 0;


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


  function iconNode(name, size) {
    // 踩过：折叠按钮的图标是空的（用户截到的是 `<svg viewBox=...></svg>`，里面
    // 一个 `<path>` 都没有）—— 因为 `chevronL/chevronR` 只定义在 **ui.js** 的图标表里，
    // 而这里查的是 chat.js 自己那份 `ICONS`，查不到就返回空字符串。补上，路径与
    // ui.js 那份保持一致（那边的注释说这 ±0.5 是给箭头做的光学修正）。
    var box = h('span.chat__icon');
    var px = size || 16;
    // ⚠️ 路径从 **ui.js 那张唯一的表**取（`ui.iconPath`）。原先这里查的是 chat.js
    // 自己那份 `ICONS` + 一份 `ICON_FALLBACK`，而 ui.js 里有另一张 —— 三处各一半，
    // 于是 `chevronL/R`、`bookmark/flag`、`share` 先后都因为"查错了表"变成空 svg。
    box.innerHTML =
      '<svg viewBox="0 0 24 24" width="' + px + '" height="' + px + '" fill="none" ' +
      'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' +
      ui.iconPath(name) +
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
        // 查 ui.js 那张唯一的表（删表时**漏了这一处**，运行时就 `ICONS is not defined`，
        // 被 boot 统一的提示说成"连不上服务" —— 所以合并之后要把"还有没有别处引用"搜干净）
        (ui.iconPath(name) || ui.iconPath('close')) +
        '</svg>',
    });
  }

  /* ------------------------------------------------------------ 标记 */

  /**
   * 三种标记，钉在**它的回答**的某一段上。
   *
   *   * **高亮** —— 给眼睛做记号（正文底色变了就行）
   *   * **删除线** —— "这一段不算数"（灰 + 划线）
   *   * **批注** —— 给这一段**写一句话**：话既进批注栏（总览、搜索、编辑），
   *     也作为**旁批**出现在正文右边那一栏，读到哪一句就看见哪一句
   *
   * 三者是**三件事**，别混（用户："高亮和批注是分开的两个东西"）：批注栏只收批注，
   * 点在高亮上右键给的是「取消高亮」，不是「改这条批注」。共用的是存储与同步那一条
   * 记录（`settings.notes`，按 `kind` 分开，见 `QF.store.markKind`）。
   *
   * 入口两个：在回答里**选中一段** → 右键 →「高亮这一段」/「删除线」/「批注这一段」，
   * 或者不碰鼠标，直接按 ⌘/Ctrl+⇧H / ⇧X / ⇧A。**批注从一开始就在正文右边就地写**
   *（草稿卡片直接长在该在的位置上），不经过总览面板 —— 那份面板只管翻全篇。
   *
   * 钉的是**渲染后正文里的字符区间**：正文由 `QF.md.render` 出来，同一份正文
   * 每次渲染的结果一致，所以区间是稳的（给 Markdown 源码算偏移得先理解语法）。
   * 同时存下**选中的原文**（`quote`）：万一将来渲染变了样，还能认出标的是哪句。
   *
   * 存储与原批注同源（settings，本地优先）：
   * 不新开表、不新开接口、不新增冲突规则。
   */
  var notesOpen = false;
  var notesDraft = '';
  var notesEditing = '';
  //: 面板里那条批注改之前"正文那块"多高（同上：不许比它矮）
  var notesEditMin = 0;
  //: 刚选了「批注这一段」、还没落字的那一段：{cid, mid, quote, start, end, anchor*}
  //: 它对应旁批栏里**那张正在填字的卡片**（创建也走就地，见 annotatePick）。
  var pendingMark = null;
  //: 那张草稿卡片里已经敲进去的字。存在这里而不是只留在 textarea 里：中途被重画
  //: （来了新消息、切了分支）时，敲了一半的话不该消失。
  var marginDraftText = '';
  //: 正在**就地改**的那条批注（旁批那一栏里那张卡片变成了输入框）。
  //: 与面板里的 `notesEditing` 各管一边：面板是"翻全篇"的地方，改一条不必先开它。
  var marginEditing = '';
  //: 就地改的那条现在敲成什么样了（同上：重画之后字还在）。`null` = 还没开过这条。
  var marginEditText = null;
  //: 就地改之前那张卡多高（见 `fitNoteArea`）：编辑器**不许比它矮**。
  var marginEditMin = 0;
  //: 已经为哪一条抢过焦点（`draft` / `note:<id>`）：重画会重建卡片，
  //: 不能每次都把焦点从用户手上拽回去。
  var marginFocus = '';
  //: 面板里那个搜索框（批注多了就要找）
  var markQuery = '';

  function openNotes() {
    if (marksOpen) closeMarks();   // 两张右侧浮层叠在一起只会互相挡
    notesOpen = true;
    notesEditing = '';
    renderNotes();
  }

  function closeNotes() {
    notesOpen = false;
    notesDraft = '';
    notesEditing = '';
    // 关面板**不动**正在填的那条草稿：它在正文右边那一栏里，不属于这个面板
    //（草稿是 2026-09-25 从面板搬到那里的，这条注释跟着一起改）
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

  /** 落一条批注。`pendingMark` = 新的一条；`notesEditing` = 在改已有的那条。 */
  function saveNote() {
    var text = String(notesDraft || '').trim();
    if (notesEditing) {
      if (!text) return;
      QF.store.updateNote(notesEditing, text);
      notesEditing = '';
      notesDraft = '';
      renderNotes();
      repaintKeepingScroll();   // 改一条批注是原地变形，不该把视口拽到底
      return;
    }
    if (!pendingMark) return;
    if (
      !addMarkTracked({
        kind: 'note',
        cid: pendingMark.cid,
        mid: pendingMark.mid,
        quote: pendingMark.quote,
        start: pendingMark.start,
        end: pendingMark.end,
        text: text,
      })
    ) {
      ui.toast('批注满了（500 条）—— 先删几条再加', 'warn', 3200);
      return;
    }
    // 走了面板这一条路也要把草稿状态收干净（旁批栏那张草稿卡片随之消失）
    cancelDraftState();
    notesDraft = '';
    renderNotes();
    repaintKeepingScroll();   // 同上：钉一条标记是原地变形，不该把视口拽到底
    ui.toast('已批注', 'info', 1200);
  }

  /** 选区落在哪一段上：返回 {cid, mid, quote, start, end}，不在回答正文里就是 null。
   *
   * 只认**助手消息的零件区**（`.chatmsg__parts`）：批的是"它的输出"，不批自己那几条；
   * 而且要钉在一条真有 id 的消息上 —— `local-` 开头的乐观节点服务端还没有它，
   * 钉上去刷新就失效。
   */
  function selectionMark() {
    var sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
    var range = sel.getRangeAt(0);
    var node = range.commonAncestorContainer;
    var el = node.nodeType === 1 ? node : node.parentNode;
    var host = el && el.closest ? el.closest('.chatmsg__parts') : null;
    if (!host) return null;
    var row = host.closest('.chatmsg');
    var mid = row && row.dataset ? row.dataset.id : '';
    if (!mid || String(mid).indexOf('local-') === 0) return null;
    var span = textOffsetsIn(host, range);
    if (!span || span.end <= span.start) return null;
    // 顺带把这一段**在屏幕上的位置**记下来（相对零件区）：批注要摆到正文右边那一栏，
    // 而"还没落库的那条"没有 mark 可量，只能靠按下那一刻记下的位置（见 marginItems）。
    // 用相对零件区的差而不是视口坐标：滚动与重排都不会改变它。
    var hostRect = host.getBoundingClientRect();
    var rangeRect = range.getBoundingClientRect();
    return {
      cid: state.current || '',
      mid: String(mid),
      quote: String(sel.toString() || '').slice(0, 600),
      start: span.start,
      end: span.end,
      anchorY: rangeRect.top - hostRect.top,
      anchorH: rangeRect.height,
      anchorX: rangeRect.right - hostRect.left,
    };
  }

  /** 区间在正文里的**线性字符位置**：走一遍文本节点、把长度累加起来。 */
  function textOffsetsIn(root, range) {
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var pos = 0;
    var start = -1;
    var end = -1;
    var node = walker.nextNode();
    while (node) {
      var len = node.nodeValue ? node.nodeValue.length : 0;
      if (node === range.startContainer) start = pos + range.startOffset;
      if (node === range.endContainer) end = pos + range.endOffset;
      pos += len;
      node = walker.nextNode();
    }
    if (start >= 0 && end >= 0) return { start: start, end: end };
    // 边界落在元素上（整段被选中那种）：退化成"按原文找一次"
    var text = root.textContent || '';
    var picked = String(range.toString() || '');
    var at = picked ? text.indexOf(picked) : -1;
    if (at < 0) return null;
    return { start: at, end: at + picked.length };
  }

  /** 把三种标记画到正文上，并把批注摆成**旁批**。
   *
   * 每次重画正文都要重来（重画会换掉 DOM，包好的 mark 自然也没了）。 */
  function paintMarks(bodyEl, m) {
    if (!bodyEl || !m || m.id == null || !QF.store.marksOf) return;
    var row = bodyEl.closest ? bodyEl.closest('.chatmsg') : null;
    var marks = QF.store.marksOf(m.id);
    var host = bodyEl.querySelector('.chatmsg__parts');
    if (!marks.length || !host) {
      paintMargin(row, host, []);   // 标记被删光时把旁批那一栏收掉
      return;
    }
    // **从后往前包**：先包前面那段会把后面节点的偏移顶掉。
    marks
      .slice()
      .sort(function (a, b) {
        return b.start - a.start;
      })
      .forEach(function (mark) {
        wrapOnce(host, mark);
      });
    paintMargin(row, host, marks);
  }

  /** 把 [mark.start, mark.end) 画成 `<mark class="chatmark chatmark--<kind>">`；对不上就静静跳过。
   *
   * **按文本节点逐段包**，而不是拿一个 range 一把包下去。为什么：
   * 选区跨了多个块（横跨两段、跨过一个小标题）时 `range.surroundContents` 会抛，
   * 而"抽出来再包"（`extractContents` + `insertNode`）会把**整块 `<p>` 搬进 `<mark>` 里** ——
   * 那是个块级盒子：底色糊成一大片、几张标记嵌套起来还会在行首竖出条条
   *（实测撞过：一条跨段的高亮渲染成 688×156、里面装着两个块的 `<mark>`）。
   * 逐段包则每一行各有各的一片，行内的偏移也不会互相顶掉。
   */
  function wrapOnce(host, mark) {
    var walker = document.createTreeWalker(host, NodeFilter.SHOW_TEXT, null);
    var pos = 0;
    var pieces = [];
    var node = walker.nextNode();
    while (node) {
      var len = node.nodeValue ? node.nodeValue.length : 0;
      var from = Math.max(mark.start, pos);
      var to = Math.min(mark.end, pos + len);
      if (to > from) pieces.push({ node: node, from: from - pos, to: to - pos });
      pos += len;
      node = walker.nextNode();
    }
    // **从后往前包**：包了前面那一段会把后面节点的偏移顶掉（同一条消息上有多条标记时
    // 尤其明显）。这一条在"一把包"的写法里也成立，逐段包之后仍然要守。
    for (var i = pieces.length - 1; i >= 0; i -= 1) {
      var piece = pieces[i];
      try {
        var range = document.createRange();
        range.setStart(piece.node, piece.from);
        range.setEnd(piece.node, piece.to);
        var box = document.createElement('mark');
        // 三种标记**各长各的**（底色 / 灰+划线 / 虚线下划线，见 CSS）：
        // 类名里带 kind，样式就不用去猜它是不是批注
        box.className = 'chatmark chatmark--' + QF.store.markKind(mark);
        box.dataset.mark = mark.id;
        if (mark.text) box.title = mark.text;
        range.surroundContents(box);
      } catch (err) {
        /* 某一段包不上就只是那一段没画上 —— 别把整条消息搞坏 */
      }
    }
  }

  /* ------------------------------------------------------------------ 旁批
   *
   * 批注的那句话要出现在**它标的那一行旁边**，而不是只在统一面板里看得到 ——
   * 读到哪一句、提醒就在眼前（用户："要能做到一边看输出一边看批注，就像笔记一样"）。
   *
   * 版式：给这一行加第三个格子（`.chatmsg__margin`，行上加 `.has-margin` 才出现），
   * 卡片用**绝对定位**按"它标的那一行在哪"算 `top`，所以不参与文档流、也不推挤正文；
   * 两条挤在一起时依次往下让。行宽只在**这条回答有批注**时才收窄，没批注的照旧铺满。
   *
   * 位置是版式的函数，不是一次性算完的：正文重排（窗口缩放、收起会话栏、字号变）之后
   * 要重算，所以有一条 `relayoutMargins`（由线程上的 ResizeObserver 触发）。
   */
  function notesOn(marks) {
    return (marks || []).filter(function (one) {
      return QF.store.markKind(one) === 'note' && String(one.text || '').trim();
    });
  }

  /** 就地编辑时，让输入框**至少和它原来显示的那块一样高**。
   *
   * 用户："对于长的批注，点击编辑的时候，编辑框会缩得很小。不要让它动，要保持在原形上编辑"。
   * 长的批注原来显示成好几行，一点"编辑"却换成 `rows="3"` 的小框 —— 卡片当场缩水，
   * 底下整栏跟着跳。
   *
   * 做法：进编辑**之前**先把原来那块的高度量下来（`minPx`），写成 `min-height`，
   * 而且**创建时就带上**（不是等挂进 DOM 再改）—— 这样一帧都不会闪出小框；
   * 然后再按内容自动长高：字多了更高，**不会比原来矮**。
   * 返回"按内容重算一次"的函数，挂进 DOM 之后再调（挂之前量不到折行）。
   *
   * `minPx` 为 0 = 新写的草稿（没有"原来的形状"要保）：直接不插手，保持 `rows=3` ——
   * 否则一个空框会被"按内容"缩成一行。
   */
  function fitNoteArea(area, minPx) {
    var floor = Math.max(0, Math.round(minPx || 0));
    if (!area || !floor) return null;
    area.style.minHeight = floor + 'px';
    var grow = function () {
      var cs = window.getComputedStyle(area);
      // `box-sizing: border-box` 是全局那条：`scrollHeight` 不含边框，要补上，
      // 否则有边框的那个（`.chatnotes__input`）会差 2px 就出滚动条。
      var frame = parseFloat(cs.borderTopWidth || 0) + parseFloat(cs.borderBottomWidth || 0);
      area.style.height = 'auto';
      area.style.height = Math.max(floor, area.scrollHeight + frame) + 'px';
    };
    area.addEventListener('input', grow);
    return grow;
  }

  /** 卡片里那个输入框 + 两颗图标（保存＝勾，取消＝叉）。
   *
   * **Enter 就是保存**（用户："批注框编辑的时候 enter 就是保存"）：换行改走 Shift+Enter，
   * 与对话输入框同一套手感（组字中的回车仍然是"选词"，见 `isComposing`）。Esc 是取消。
   */
  function appendComposer(card, opts) {
    var area = h('textarea.chatnote-card__input', {
      rows: '3',
      // 高度下限在**创建时**就写进去（见 `fitNoteArea`）：等挂进 DOM 再改会闪一下小框
      style: opts.minHeight ? { minHeight: Math.round(opts.minHeight) + 'px' } : null,
      value: opts.value,
      placeholder: '写一句…',
      onInput: function (event) {
        opts.onInput(event.target.value);
      },
      onKeydown: function (event) {
        if (event.key === 'Escape') {
          event.preventDefault();
          opts.onCancel();
          return;
        }
        if (event.key !== 'Enter' || event.isComposing || event.shiftKey) return;
        event.preventDefault();
        opts.onSave(area.value);
      },
    });
    card.appendChild(area);
    card.appendChild(
      h(
        'div.chatnote-card__acts',
        null,
        iconButton('check', '保存（Enter）', function () {
          opts.onSave(area.value);
        }, '.chatnote-card__act'),
        iconButton('close', '取消（Esc）', opts.onCancel, '.chatnote-card__act')
      )
    );
    var grow = fitNoteArea(area, opts.minHeight);
    // 刚开这一条时抢一次焦点、光标落在末尾 —— 写一句话不该还要先点一下输入框。
    // `marginFocus` 记住"这一次已经抢过了"：重画会重建卡片，不能每次都把焦点拽回来。
    setTimeout(function () {
      if (grow) grow();   // 挂进 DOM 了，这时量折行才准
      if (marginFocus === opts.key) return;
      marginFocus = opts.key;
      area.focus();
      area.setSelectionRange(area.value.length, area.value.length);
    }, 30);
    return area;
  }

  /** 一张旁批卡片。**填字与改字都在这里就地做**（不再跳总览面板）。
   *
   * 两态共用一张卡：`marginEditing`（改已有的那条）与 `pendingMark`（刚创建、还没落库，
   * 见 `draftCard`）。用户的两句话是这一块的全部理由："对批注进行修改的时候，不要跳出来
   * 那个总览页面，直接在右边的批注栏上就地修改"、"创建也做成就地填字"。
   * 总览面板仍然是"翻全篇"的地方（搜索、跳读、批量看）。
   */
  function marginCard(note) {
    var editing = marginEditing === note.id;
    var card = h('div.chatnote-card' + (editing ? '.is-editing' : ''), {
      dataset: { note: note.id },
    });
    if (!editing) {
      card.appendChild(
        h(
          'button.chatnote-card__view',
          {
            type: 'button',
            title: '点这里就地改',
            onClick: function () {
              // 把**这一张卡现在多高**量给它（见 `marginEditMin`）
              startMarginEdit(note.id, card);
            },
          },
          h('span.chatnote-card__text', { text: note.text })
        )
      );
      return card;
    }
    appendComposer(card, {
      key: 'note:' + note.id,
      minHeight: marginEditMin,   // 长的批注：输入框不能比原来那块矮
      value: marginEditText === null ? note.text : marginEditText,
      onInput: function (v) {
        marginEditText = v;
      },
      onSave: function (v) {
        saveMarginEdit(note.id, v);
      },
      onCancel: cancelMarginEdit,
    });
    return card;
  }

  /** 正在填字的那张**草稿**卡片：批注还没有落库，所以它没有 mark 可量。
   *
   * 名字里带 `margin` 不是啰嗦：这个文件里已经有一个 `draftCard`（模型现编的**临时题卡**，
   * 见 `partNode` 那一带）。函数声明提升之后，写在后面的那个会把前面的整个盖掉 ——
   * 实测症状是"按下 ⌘⇧A 什么也不出来，控制台里一句 `undefined` 的报错"。
   */
  function marginDraftCard() {
    var card = h('div.chatnote-card.is-draft', { dataset: { draft: '1' } });
    appendComposer(card, {
      key: 'draft',
      value: marginDraftText,
      onInput: function (v) {
        marginDraftText = v;
      },
      onSave: saveDraft,
      onCancel: cancelDraft,
    });
    return card;
  }

  function startMarginEdit(id, card) {
    // **先量下"原来那张卡"多高**：长的批注显示成好几行，一点编辑就换成三行的小框，
    // 卡片当场缩水、底下整栏跟着跳（用户："编辑框会缩得很小。不要让它动"）。
    var view = card ? card.querySelector('.chatnote-card__view') : null;
    marginEditMin = view ? Math.round(view.getBoundingClientRect().height) : 0;
    marginEditing = id;
    marginEditText = null;   // 从这一条自己的字开始（见 marginCard）
    marginFocus = '';
    notesEditing = '';       // 与面板那一份互斥：同时开两处编辑框只会让人不知道在改哪条
    cancelDraftState();      // 改一条的同时不再留着草稿
    repaintKeepingScroll();
  }

  function cancelMarginEdit() {
    marginEditing = '';
    marginEditText = null;
    marginFocus = '';
    repaintKeepingScroll();
  }

  /** 就地改完落库。**空文本不算改**：要删就去菜单里删（见右键菜单的「取消这条批注」）。 */
  function saveMarginEdit(id, text) {
    var body = String(text || '').trim();
    if (!body) {
      ui.toast('批注不能是空的 —— 不想要它就在右键菜单里删掉', 'warn', 3000);
      return;
    }
    if (!QF.store.updateNote(id, body)) return;
    marginEditing = '';
    marginEditText = null;
    marginFocus = '';
    repaintKeepingScroll();
    ui.toast('已更新批注', 'info', 1200);
  }

  /** 草稿落库 —— 创建一条批注走的就是这条路（不再经过面板）。 */
  function saveDraft(text) {
    var body = String(text || '').trim();
    if (!pendingMark) return;
    if (!body) {
      ui.toast('写一句再保存（不想写就按 Esc）', 'warn', 2600);
      return;
    }
    if (
      !addMarkTracked({
        kind: 'note',
        cid: pendingMark.cid,
        mid: pendingMark.mid,
        quote: pendingMark.quote,
        start: pendingMark.start,
        end: pendingMark.end,
        text: body,
      })
    ) {
      ui.toast('标记满了（500 条）—— 先删几条再加', 'warn', 3200);
      return;
    }
    cancelDraftState();
    repaintKeepingScroll();
    ui.toast('已批注', 'info', 1200);
  }

  function cancelDraft() {
    cancelDraftState();
    repaintKeepingScroll();
  }

  function cancelDraftState() {
    pendingMark = null;
    marginDraftText = '';
    if (marginFocus === 'draft') marginFocus = '';
  }

  /** 这一行是不是正挂着那条草稿。 */
  function draftOn(row) {
    if (!pendingMark || !row) return null;
    return String(pendingMark.mid) === String(row.dataset.id) ? pendingMark : null;
  }

  /** 这一行上要摆的东西：已有的批注（按 mark 量位置）+ 正在填字的那条（按记下的位置）。
   *
   * 排序按**锚点当前的 y**，不按 start 偏移：一条被挤到下面之后，下一帧仍按锚点重排，
   * 顺序不会因为"谁先创建"而乱。
   */
  function marginItems(host, box, notes, draft) {
    var boxRect = box.getBoundingClientRect();
    var items = notes.map(function (note) {
      var mark = host ? host.querySelector('.chatmark[data-mark="' + note.id + '"]') : null;
      var rect = mark ? mark.getBoundingClientRect() : null;
      return { key: note.id, note: note, rect: rect, top: rect ? rect.top - boxRect.top : 0 };
    });
    if (draft && host) {
      var partsRect = host.getBoundingClientRect();
      var hgt = draft.anchorH || 18;
      var top = partsRect.top + draft.anchorY - boxRect.top;
      // 还没落库的那条没有 mark 可量：拿按下那一刻记下的矩形（相对零件区）在这里还原
      items.push({
        key: '__draft',
        draft: draft,
        top: top,
        rect: {
          top: boxRect.top + top,
          bottom: boxRect.top + top + hgt,
          height: hgt,
          right: partsRect.left + draft.anchorX,
        },
      });
    }
    items.sort(function (a, b) {
      return a.top - b.top;
    });
    return items;
  }

  /** 卡片按各自那一行摆好，并从**标的文字**拉一根线到卡片。 */
  function placeMargin(host, box, notes, draft) {
    var boxRect = box.getBoundingClientRect();
    var svg = ensureLeadSvg(box);
    ui.clear(svg);
    var floor = 0;
    marginItems(host, box, notes, draft).forEach(function (item) {
      var card = item.draft
        ? box.querySelector('.chatnote-card.is-draft') || marginDraftCard()
        : box.querySelector('[data-note="' + item.key + '"]') || marginCard(item.note);
      if (!card.parentNode) box.appendChild(card);
      // `top` 相对旁批这一栏的顶边算（这一栏与正文同顶，所以两个坐标系差一个常量）
      var top = Math.round(Math.max(item.top, floor));
      card.style.top = top + 'px';
      // 线接在卡片**上沿往下一点**，不用正中间：改到一半的卡片会变高，
      // 用中点会让这根线在编辑时上下乱跑。
      if (item.rect) leadLine(svg, boxRect, item.rect, top + 13);
      floor = top + card.offsetHeight + 6;   // 挨着就往下让，别叠在一起
    });
  }

  // （`SVG_NS` 在文件开头已经声明过一次，这里直接用，不再重复声明）
  function ensureLeadSvg(box) {
    var svg = box.querySelector('.chatnote-leads');
    if (svg) return svg;
    svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', 'chatnote-leads');
    svg.setAttribute('aria-hidden', 'true');
    box.insertBefore(svg, box.firstChild);   // 连线垫在卡片底下
    return svg;
  }

  /** 一根连线：从标的文字拉到卡片左边。
   *
   * 走法：先沿**这一行的下缘那条空白**往右（`anchor.bottom` 之下已经没有字形，
   * 只剩行距），进了这一栏再拐下去接卡片 —— 直接横穿过去会从字上压过一道线。
   * 例外是"标的文字已经贴到右边界"（尾巴不足一格宽）：那种情况没有字会挡路，
   * 就从行的中间直接拉过去，看起来才像"指着那句话"。
   */
  function leadLine(svg, boxRect, anchorRect, cardY) {
    var tail = boxRect.left - anchorRect.right;
    var midY = anchorRect.top + anchorRect.height / 2 - boxRect.top;
    var y = Math.round(tail <= 26 ? midY : anchorRect.bottom - boxRect.top + 1);
    var startX = Math.round(anchorRect.right - boxRect.left + 3);
    var elbowX = -8;   // 拐点落在正文与这一栏之间那条列间距里
    var line = document.createElementNS(SVG_NS, 'polyline');
    line.setAttribute('class', 'chatnote-lead');
    line.setAttribute(
      'points',
      [startX + ',' + y, elbowX + ',' + y, elbowX + ',' + Math.round(cardY), '0,' + Math.round(cardY)].join(' ')
    );
    svg.appendChild(line);
  }

  function paintMargin(row, host, marks) {
    if (!row) return;
    var box = row.querySelector('.chatmsg__margin');
    var notes = notesOn(marks);
    var draft = draftOn(row);
    if (!notes.length && !draft) {
      row.classList.remove('has-margin');
      if (box) box.remove();
      return;
    }
    if (!box) {
      box = h('div.chatmsg__margin');
      row.appendChild(box);
    }
    row.classList.add('has-margin');
    ui.clear(box);
    placeMargin(host, box, notes, draft);
  }

  // 版式一变就重算一次卡片位置。ResizeObserver 盯线程本身：窗口缩放、收起会话栏、
  // 面板开合都会让它换尺寸，而**卡片的 top 就是这些的结果**。
  var marginFrame = 0;

  function relayoutMargins() {
    marginFrame = 0;
    if (!threadEl) return;
    Array.prototype.forEach.call(threadEl.querySelectorAll('.chatmsg.has-margin'), function (row) {
      var host = row.querySelector('.chatmsg__parts');
      var box = row.querySelector('.chatmsg__margin');
      if (!host || !box) return;
      var notes = notesOn(QF.store.marksOf(row.dataset.id));
      var draft = draftOn(row);
      if (!notes.length && !draft) {
        row.classList.remove('has-margin');
        box.remove();
        return;
      }
      placeMargin(host, box, notes, draft);
    });
  }

  function scheduleMarginRelayout() {
    if (marginFrame) return;
    marginFrame = requestAnimationFrame(relayoutMargins);
  }

  /** 右键：选中一段 → 高亮 / 删除线 / 批注；点在已有标记上 → 按**那一段上有的**给动作。
   *
   * 两条修出来的规矩（用户："我没法既加删除线又加高亮又加批注"）：
   *   * **选区优先于"点在哪"**：选中一段再右键时，光标底下**正好就是**那枚旧 `<mark>`，
   *     第一版于是只给「取消高亮」——"既高亮又删除线又批注"从这条路根本走不到；
   *   * **点了旧标记也给"加"**：缺的那几种摆出来，目标就是它自己那一段 ——
   *     否则"先高亮、再回来加删除线"要先取消、再重新划一遍，划线不该比改主意更费力。
   */
  function onMarkContextMenu(event) {
    var pick = selectionMark();
    var hit = pick || !event.target || !event.target.closest ? null : event.target.closest('.chatmark');
    if (!hit && !pick) return;   // 既没选中也没点在标记上：让浏览器自己的菜单出来
    event.preventDefault();
    chatMenuX = event.clientX;
    chatMenuY = event.clientY;

    var items = [];
    // 这一段（选区的那一段，或点中的那枚标记自己那一段）上盖着的标记
    var target = hit ? markPick(markOf(hit), hit) : pick;
    var covered = coveringMarks(target.mid, target.start, target.end);
    var has = {};
    covered.forEach(function (row) {
      has[row.kind] = true;
    });

    // **只有批注才有"改"**：高亮与删除线没有话可改（用户："高亮和批注是分开的两个
    // 东西"）。落在高亮上却给「改这条批注」，等于把一件东西说成了另一件。
    // 菜单里**不写批注的内容**（用户："右键菜单里面，不要出现批注的具体内容了"）——
    // 类别已经在标题里说了，再挂一句原文只会把菜单撑长、还得为它截断。
    var note = covered.filter(function (row) {
      return row.kind === 'note';
    })[0];
    if (note) {
      items.push({
        icon: 'pencil',
        label: '改这条批注',
        run: function () {
          startMarginEdit(note.id);   // 就地改，不跳到总览面板
        },
      });
    }
    // 这一段上有的每一种，各给一条取消
    covered.forEach(function (row) {
      items.push({
        icon: 'trash',
        danger: true,
        label: row.kind === 'strike' ? '取消删除线' : row.kind === 'note' ? '取消这条批注' : '取消高亮',
        run: function () {
          dropMark(row.id);
        },
      });
    });
    // 缺的那几种摆出来（目标 = 这一段）
    if (!has.hl) {
      items.push({
        icon: 'marker',
        label: '高亮这一段',
        hint: '⌘⇧H',
        run: function () {
          applyMark('hl', target);
        },
      });
    }
    if (!has.strike) {
      items.push({
        icon: 'strike',
        label: '加删除线',
        hint: '⌘⇧X',
        run: function () {
          applyMark('strike', target);
        },
      });
    }
    if (!has.note) {
      items.push({
        icon: 'note',
        label: '批注这一段',
        hint: '⌘⇧A',
        run: function () {
          annotatePick(target);
        },
      });
    }
    showChatMenu(items);
  }

  /** 一枚标记元素对应的那条记录。 */
  function markOf(el) {
    var id = el && el.dataset ? el.dataset.mark : '';
    return (QF.store.notes() || []).filter(function (each) {
      return each.id === id;
    })[0];
  }

  /** 某一段上**盖满了**的标记（按 kind 去重，每种一条）。
   *
   * 右键菜单靠它回答两件事："这段上还缺哪种"、以及"每种各给一条取消"。
   * 只认"整段都盖住"的：部分重叠的那些，说"取消高亮"取消掉的是哪一截讲不清。
   * 去重是因为同段上可能叠着两枚同类（先高亮一次、又从菜单里加了一次）——
   * 菜单里冒出两行一模一样的"取消高亮"只会让人猜哪个是哪个。
   */
  function coveringMarks(mid, start, end) {
    var seen = {};
    var list = [];
    (QF.store.marksOf(mid) || []).forEach(function (one) {
      if (one.start > start || one.end < end) return;
      var kind = QF.store.markKind(one);
      if (seen[kind]) return;
      seen[kind] = true;
      list.push({ kind: kind, id: one.id });
    });
    return list;
  }

  /** 撤掉一条标记，并把跟着它一起变的东西收拾干净（就地编辑态、正文、旁批栏、面板）。 */
  function dropMark(id) {
    if (!removeMarkTracked(id)) return false;
    // 正在就地改的那条被删了：别留着一个指向空条目的输入框
    if (marginEditing === id) marginEditing = '';
    repaintKeepingScroll();   // 撤销标记同样是原地变形，不该动视口
    if (notesOpen) renderNotes();
    return true;
  }

  /** 把一枚**已有的标记**当成"要操作的一段"，给 `applyMark` / `annotatePick` 当目标。
   *
   * 右键点在一枚旧标记上时用它：目标就是它自己那一段。锚点按**这一枚元素**在屏幕上的
   * 位置算 —— 批注的草稿卡片要摆到正文右边那一栏，`pendingMark` 里少了这个会飘。
   */
  function markPick(mark, el) {
    var host = el && el.closest ? el.closest('.chatmsg__parts') : null;
    var pick = {
      cid: state.current || '',
      mid: mark && mark.mid != null ? String(mark.mid) : '',
      quote: String((mark && mark.quote) || '').slice(0, 600),
      start: Math.max(0, parseInt(mark && mark.start, 10) || 0),
      end: Math.max(0, parseInt(mark && mark.end, 10) || 0),
    };
    if (host && el) {
      var hostRect = host.getBoundingClientRect();
      var rect = el.getBoundingClientRect();
      pick.anchorY = rect.top - hostRect.top;
      pick.anchorH = rect.height;
      pick.anchorX = rect.right - hostRect.left;
    }
    return pick;
  }

  /** 钉一条标记。右键菜单与快捷键**共用这一条路**（两处各写一份，迟早只改一处）。 */
  function applyMark(kind, pick) {
    if (
      !addMarkTracked({
        kind: kind,
        cid: pick.cid,
        mid: pick.mid,
        quote: pick.quote,
        start: pick.start,
        end: pick.end,
      })
    ) {
      ui.toast('标记满了（500 条）—— 先删几条再加', 'warn', 3200);
      return;
    }
    // **原地变形**，不是"发完一条消息"：`paintThread()` 默认把视口拽到最底
    //（用户："在 chatUI 上处高亮，画面会自动滚动到最下面"）。
    repaintKeepingScroll();
  }

  /** 批注这一段：**在正文右边的旁批栏里就地填字**（用户："创建也做成就地填字"）。
   *
   * 原先它开的是总览面板 —— 写一句话却要弹一整个面板，重心从"那句话"跑到了"面板"。
   * 现在只把这一段挂起来（`pendingMark`），卡片由 `paintMargin` 长在它该在的位置上，
   * 连线和它在正文里的锚点一起画出来（见 `draftCard` / `marginItems`）。
   */
  function annotatePick(pick) {
    cancelDraftState();   // 上一张草稿没写完就换了目标：以这一次为准
    pendingMark = pick;
    notesEditing = '';
    notesDraft = '';
    repaintKeepingScroll();
  }

  // 快捷键：选中它回答里的一段 → ⌘/Ctrl+⇧H 高亮、⇧X 删除线、⇧A 批注。
  //
  // 为什么要有：这条路原先只有右键一个入口，而"选好一段话、再把光标挪上去右键"是
  // 两只手交替的动作；手在键盘上的人只想要一个组合键。
  //
  // 键位的选法（工作台页同时挂着笔记编辑器 notes.js，两边的组合键会共用一页）：
  //   * `⌘⇧H`（高亮）、`⌘⇧X`（删除线）两边**同义**，留着 —— notes.js 那份只在焦点
  //     位于编辑器里时生效（`formatShortcut` 绑在编辑器上），两边不会同时命中；
  //   * 批注**不用** `⌘⇧M`：它在 notes.js 里是"公式块"，同一个键在同页指两件事，
  //     养出来的是错的肌肉记忆。用 `⇧A`（annotate）。
  //
  // 让路规则与 `⌘B` 一致，但判据落在**选区**上而不是焦点上：焦点还留在输入框里、
  // 用鼠标在回答里划一段，是最常见的一种选法，按焦点让路会把这种情形挡在外面。
  // 选区不在它的回答里（选的是自己的话、或选在别的面板里）就什么都不做 —— 抢下
  // 这个组合键却不发生任何事，比不响应更让人困惑。
  document.addEventListener('keydown', function (ev) {
    if (!(ev.metaKey || ev.ctrlKey) || !ev.shiftKey || ev.altKey) return;
    var key = String(ev.key || '').toLowerCase();
    if (key !== 'h' && key !== 'x' && key !== 'a') return;
    var pick = selectionMark();
    if (!pick) return;
    ev.preventDefault();
    if (key === 'a') annotatePick(pick);
    else applyMark(key === 'x' ? 'strike' : 'hl', pick);
  });

  /* ---------------------------------------------------------------- 撤回（⌘Z）
   *
   * 记的是**标记的增删**（高亮 / 删除线 / 批注）—— "手滑点错了"的兜底，不是历史记录
   *（上限 30 条）。改字不进这里：那种撤回是逐字的、在输入框里该由浏览器管
   *（见下面那条 keydown 的让路规则），两件事混在一起只会让人猜不到会发生什么。
   */
  var MARK_UNDO_MAX = 30;
  var markUndo = [];

  function trackMark(entry) {
    markUndo.push(entry);
    if (markUndo.length > MARK_UNDO_MAX) markUndo.shift();
  }

  function markKindWord(piece) {
    var kind = QF.store.markKind(piece);
    return kind === 'strike' ? '删除线' : kind === 'note' ? '批注' : '高亮';
  }

  /** 加一条标记并记进撤回栈 —— 三个入口（右键 / 快捷键 / 面板）共用它。 */
  function addMarkTracked(payload) {
    var piece = QF.store.addMark(payload);
    if (piece) trackMark({ op: 'add', id: piece.id });
    return piece;
  }

  /** 删一条标记并记进撤回栈（连整条一起记下来，好按原 id 放回去）。 */
  function removeMarkTracked(id) {
    var piece = (QF.store.notes() || []).filter(function (one) {
      return one.id === id;
    })[0];
    if (!piece) return false;
    var snapshot = Object.assign({}, piece);
    if (!QF.store.removeNote(id)) return false;
    trackMark({ op: 'remove', piece: snapshot });
    return true;
  }

  /** 撤回上一步：刚加的删掉、刚删的放回来（按**原来的 id**，见 `store.restoreMark`）。 */
  function undoMark() {
    var entry = markUndo.pop();
    if (!entry) {
      ui.toast('没有可撤回的标记了', 'info', 1600);
      return;
    }
    if (entry.op === 'add') {
      var live = (QF.store.notes() || []).filter(function (one) {
        return one.id === entry.id;
      })[0];
      if (!live || !QF.store.removeNote(entry.id)) {
        ui.toast('这条标记已经不在了', 'info', 1600);
        return;
      }
      if (marginEditing === entry.id) marginEditing = '';
      repaintKeepingScroll();
      ui.toast('已撤回' + markKindWord(live), 'info', 1400);
      return;
    }
    if (!QF.store.restoreMark(entry.piece)) {
      ui.toast('放不回去了（标记已经满了）', 'warn', 2400);
      return;
    }
    repaintKeepingScroll();
    ui.toast('已恢复' + markKindWord(entry.piece), 'info', 1400);
  }

  // ⌘/Ctrl+Z = 撤回上一步标记操作（用户："^Z 要能撤回刚刚的高亮/删除线"）。
  //
  // 让路规则与 ⌘B 一致：焦点在可编辑区域里时不抢 —— 在输入框里按 ⌘Z 该由浏览器
  // 撤回你刚打的字。栈空时也不抢：别把浏览器自己的默认行为挡掉却什么都不做。
  document.addEventListener('keydown', function (ev) {
    if (!(ev.metaKey || ev.ctrlKey) || ev.altKey || ev.shiftKey) return;
    if (String(ev.key || '').toLowerCase() !== 'z') return;
    var el = document.activeElement;
    if (el && (el.isContentEditable || el.tagName === 'TEXTAREA' || el.tagName === 'INPUT')) return;
    if (!markUndo.length) return;
    ev.preventDefault();
    undoMark();
  });

  /* -------------------------------------------------------- 位置标记（两类）
   *
   * 都是"这条消息这个位置"，但用处不同（用户："回溯是一个小工具，趁手，方便快速跳转。
   * 然后现在是书签，这个是永久性的标记"）：
   *
   *   back  回溯  —— 最多 5 个，从正文区边上那颗传送球里快速跳转（趁手）
   *   mark  书签  —— 数量不限，靠**地图（对话树）**上那枚丝带认（永久）
   *
   * 两者都带一句**描述**（可空）：描述是给人认的（"我当时为什么把这儿标下来"），
   * 锚点是给机器认的（cid + mid）。两类都画在**对话树上** —— 那是地图（用户原话）。
   *
   * 五处刻意：
   *   * 消息行尾两枚图标：旗（回溯）、丝带（书签）。左键打 / 取消，右键出菜单
   *    （写描述 / 取消标记）—— 打一下不用打断，想写描述再写。
   *   * 一个回溯点都没有时**那条 rail 整条不出现**；书签不上 rail，它在地图上。
   *   * 回溯满 5 个：rail 抖一下（比一句 toast 更快让人知道"满在哪儿"）。
   *   * 传送要有过程：点下去那颗点脉冲、落点闪一下（`revealMessage` 自带）；
   *     跨对话时整条线程再轻沉一下 —— 一秒钟里换了一整屏内容，人得有个着落。
   *   * 描述为空时一律显示**那一句的开头**（`quote`）—— 没写描述的也得认得出来。
   */
  var placesKey = '';

  function placeKindName(place) {
    return place && place.kind === 'mark' ? '书签' : '回溯';
  }

  /** 给人认的一行字：描述优先，没描述就退到那一句的开头。 */
  function placeLabel(place) {
    var label = String((place && place.label) || '').trim();
    if (label) return label;
    var quote = String((place && place.quote) || '').replace(/\s+/g, ' ').trim();
    return quote ? quote.slice(0, 18) + (quote.length > 18 ? '…' : '') : placeKindName(place);
  }

  /** 这条消息上的回溯点（传送球与编号只认它；书签在地图上）。 */
  function backsOf(places) {
    return (places || []).filter(function (one) {
      return one.kind !== 'mark';
    });
  }

  /* ------------------------------------------------------------ 轨迹线（我的探索旅程）
   *
   * 用户："加一个轨迹按钮，点击之后，一根细线按照时间顺序把我经过的节点平滑地串起来"。
   * "我经过的节点" 是**所有节点**，按**创建时间**（id）排 —— 不是当前分支（见 `trailD`
   * 里那段说明：那是"最终留下的路"，不是"走过的路"）。所以这条线会在图上到处窜、
   * 来回摞在一起 —— 那正是"地图上一堆连接线"。
   *
   * 每个节点取**中心点**：路线要"串起节点"，穿进框里的那一段交给节点自己盖住
   *（轨迹画在节点之下），露出来的是框与框之间的一段段线。整条是一个子路径
   *（一个 `M` + 若干 `C`）—— `stroke-dasharray` 按子路径算，自画那一套才成立。
   */
  /** 一个节点在图上的**中心点**（两种方向都是这个式子：`y` 是竖着的那条轴）。
   *
   * 取中心而不是边：路线要"串起节点"，穿进框里的那一段交给节点自己盖住
   *（轨迹画在节点之下），露出来的是框与框之间的一段段连线 —— 正是"地图上一堆连接线"。
   */
  function trailPoint(node) {
    return [node.x + TREE.w / 2, node.y];
  }

  /** 当前分支那条路线的 `d`：**按时间顺序**穿过每个经过的节点中心，平滑曲线。
   *
   * 用 Catmull-Rom 插值（转成三次贝塞尔）：它**穿过**每个控制点 —— 这条正是要的。
   * 折线太多拐角、普通贝塞尔又不过点，只有它既过点又光滑。
   *
   * `limit` = 只画到哪个 id 为止（播放用；`null` = 全部）。整条仍是一个子路径
   *（一个 `M` + 若干 `C`），虚线自画那套才成立。
   */
  function trailD(limit) {
    var model = TREE.trailModel;
    if (!model) return '';
    var at = {};
    model.nodes.forEach(function (one) {
      at[String(one.message.id)] = one;
    });
    var pts = [];
    // **所有节点**，按创建时间（id）—— 不是"当前分支"。
    //
    // 这一处改过一次，把话留清楚：第一版沿 `activePath()`（当前上下文那条线）走，
    // 用户当场否了 —— "它只指示了我目前所处的这一条线路，但实际上我在树的前后反复跳转…
    // 你要按照节点创建的时间顺序来，这个才是我真正的探索旅程"。
    // 对：树上每个节点都是那一刻真的长出来的（包括后来放弃的分支、来回跳的那些），
    // 所以 **id 顺序 = 旅程本身**；当前分支只是最终留下的那条路，不是走过的路。
    playOrder().forEach(function (m) {
      if (limit !== null && limit !== undefined && m.id > limit) return;
      var node = at[String(m.id)];
      if (node) pts.push(trailPoint(node));
    });
    if (pts.length < 2) return '';
    var round = function (v) {
      return Math.round(v * 10) / 10;
    };
    var d = 'M' + round(pts[0][0]) + ' ' + round(pts[0][1]);
    for (var i = 0; i + 1 < pts.length; i++) {
      var p0 = pts[i > 0 ? i - 1 : 0];
      var p1 = pts[i];
      var p2 = pts[i + 1];
      var p3 = pts[i + 2 < pts.length ? i + 2 : pts.length - 1];
      var c = trailControls(p0, p1, p2, p3);
      d += 'C' + round(c[0][0]) + ' ' + round(c[0][1]) + ',' +
                 round(c[1][0]) + ' ' + round(c[1][1]) + ',' +
                 round(p2[0]) + ' ' + round(p2[1]);
    }
    return d;
  }

  /** 一段的两个控制点：**向心** Catmull-Rom（α = 0.5）。
   *
   * 第一版用的是普通 Catmull-Rom（控制点取 `(p2 - p0) / 6`）—— 它在急转弯处会**过冲**：
   * 控制点伸得比节点还远，曲线冲过去再折回来，屏幕上就是一根尖刺
   *（用户截到的正是那个：一条线猛戳下去又戳回来）。
   *
   * 向心版把"节点间距的 α 次方"当作参数间隔（knot），控制点只由**相邻两段**决定，
   * 数学上保证曲面光滑且不自交（Yuksel 等，Parameterization and Applications of
   * Catmull-Rom Curves）—— 转得再急也是一个圆润的弯，不会吐尖。
   */
  function trailControls(p0, p1, p2, p3) {
    var dist = function (a, b) {
      var dx = b[0] - a[0];
      var dy = b[1] - a[1];
      return Math.sqrt(dx * dx + dy * dy);
    };
    // 间距为 0（首尾把邻居复制了一份）时给一个极小值，别让它除出 Infinity
    var knot = function (v) {
      return Math.max(Math.pow(Math.max(v, 1e-6), 0.5), 1e-4);
    };
    var t0 = 0;
    var t1 = t0 + knot(dist(p0, p1));
    var t2 = t1 + knot(dist(p1, p2));
    var t3 = t2 + knot(dist(p2, p3));
    var d10 = t1 - t0;
    var d21 = t2 - t1;
    var d32 = t3 - t2;
    var d20 = t2 - t0;
    var d31 = t3 - t1;
    var mix = function (a, ka, b, kb, c, kc) {
      return [
        a[0] * ka - b[0] * kb + c[0] * kc,
        a[1] * ka - b[1] * kb + c[1] * kc,
      ];
    };
    var sub = function (a, b) {
      return [a[0] - b[0], a[1] - b[1]];
    };
    // m1 = d21 * [ (p1-p0)/d10 - (p2-p0)/d20 + (p2-p1)/d21 ]
    var m1 = mix(sub(p1, p0), 1 / d10, sub(p2, p0), 1 / d20, sub(p2, p1), 1 / d21);
    // m2 = d21 * [ (p2-p1)/d21 - (p3-p1)/d31 + (p3-p2)/d32 ]
    var m2 = mix(sub(p2, p1), 1 / d21, sub(p3, p1), 1 / d31, sub(p3, p2), 1 / d32);
    return [
      [p1[0] + (m1[0] * d21) / 3, p1[1] + (m1[1] * d21) / 3],
      [p2[0] - (m2[0] * d21) / 3, p2[1] - (m2[1] * d21) / 3],
    ];
  }

  /** 轨迹那一层；没开就返回 null（调用方不挂）。 */
  function treeTrail(model) {
    if (!TREE.trail || !model) return null;
    TREE.trailModel = model;
    var d = trailD(TREE.play.on ? TREE.play.limit : null);
    var group = sv('g', { class: 'cttrail' });
    var glow = sv('path', { class: 'cttrail__glow', d: d });
    var line = sv('path', { class: 'cttrail__line', d: d });
    group.appendChild(glow);
    group.appendChild(line);
    // 没在播放才"自画"：先把虚线缺口顶到整条长度（等于全遮），下一帧再放开。
    // 播放中不这么干 —— 那时**长大本身就是动画**，轨迹跟着一枚一枚接上去（见 `paintTrail`）。
    if (d && !TREE.play.on) {
      try {
        var len = line.getTotalLength();
        // 线宽是**屏幕**像素（CSS 里那条 `vector-effect`），虚线长度因此也要按屏幕算：
        // 不给这个比例，缩小后整条会被"提前画完"、放大后只画出一半。
        var dash = len * (TREE.view.k || 1);
        [glow, line].forEach(function (el) {
          el.style.strokeDasharray = dash + ' ' + dash;
          el.style.strokeDashoffset = String(dash);
        });
        requestAnimationFrame(function () {
          requestAnimationFrame(function () {
            if (!line.parentNode) return;
            [glow, line].forEach(function (el) {
              el.style.strokeDashoffset = '0';
            });
            // 画完把虚线撤掉：之后无论怎么缩放它都是一条实线
            setTimeout(function () {
              [glow, line].forEach(function (el) {
                el.style.strokeDasharray = 'none';
              });
            }, 1200);
          });
        });
      } catch (err) {
        /* 量不到就整条直接显示 */
      }
    }
    return group;
  }

  /** 把已有的轨迹重画到 `limit`（播放每点一枚就调一次）。 */
  function paintTrail(limit) {
    if (!treeEl || !TREE.trail) return;
    var d = trailD(limit);
    ['.cttrail__line', '.cttrail__glow'].forEach(function (sel) {
      var el = treeEl.querySelector(sel);
      if (el) el.setAttribute('d', d || '');
    });
  }

  /** 轨迹开着时：把"自画"那套虚线让开（播放中长大的过程本身就是动画）。
   *
   * 早先这里还有一条"只显示轨迹"：轨迹开着就把节点/连线/图钉全隐掉。**已撤**
   *（用户："点击轨迹的时候，下面的节点不要消失"）—— 路线要跟树一起看才有意义：
   * 哪一段是沿着主线走的、哪一段是回头跳的，只有节点在旁边才读得出来。
   */
  function applyTrail() {
    if (!treeEl) return;
    if (!TREE.trail) return;
    var line = treeEl.querySelector('.cttrail__line');
    if (!line) return;
    // 播放中不用"自画"那套虚线（长大的过程就是动画）
    if (TREE.play.on) {
      ['.cttrail__line', '.cttrail__glow'].forEach(function (sel) {
        var el = treeEl.querySelector(sel);
        if (!el) return;
        el.style.strokeDasharray = 'none';
        el.style.strokeDashoffset = '0';
      });
    }
  }

  /* ------------------------------------------------------------ 播放（按时间长大）
   *
   * 用户："加一个播放按钮，点击之后，整棵树按照时间顺序徐徐长大。播放速度要能调整"。
   *
   * 三处讲究：
   *   * **几何不动**：节点的位置一直是算好的，长大只是"显形"而已。整棵树不会一边长
   *     一边抖（真去动布局的话，每一帧都要重排，看着就是晃）。
   *   * **出生动画落在里层**（`.ctnode__ink`）：外层 `group` 的 transform 是定位用的，
   *     CSS 的 transform 会把它顶掉 —— 一碰，节点就跳到原点去。
   *   * **状态活在 DOM 之外**（`TREE.play`）：重画（收起一支、换方向、写描述…）时只把
   *     "该显示哪些"重贴一遍，计时器照旧跑 —— 不然画一次就断一次。
   */
  var PLAY_STEP = 260;                    // 1× 时每枚之间停多久（毫秒）
  var PLAY_SPEEDS = [0.5, 1, 2, 4];       // 速度四档

  function playOrder() {
    return state.messages.slice().sort(function (a, b) {
      return a.id - b.id;                 // id 就是时间（一条一条往上加的）
    });
  }

  /** 点亮一枚节点（连带它的连线与图钉）。`born` = 要不要放"长大"那一下。 */
  function markNodeShown(mid, born) {
    if (!treeEl) return;
    var key = String(mid);
    var group = treeEl.querySelector('.ctnode[data-node="' + key + '"]');
    if (group) {
      group.classList.add('is-shown');
      if (born) group.classList.add('is-born');
    }
    var edge = treeEl.querySelector('.ctedge[data-edge="' + key + '"]');
    if (edge) edge.classList.add('is-shown');
    var pin = treeEl.querySelector('.ctpin2[data-mid="' + key + '"]');
    if (pin) {
      pin.classList.add('is-shown');
      // 书签/锚点**跟着它那一枚节点一起出场**（播放前它们是藏着的，见 `applyPlayState`），
      // 出场那一下把描述亮出来（用户："轮到它的时候，显示一下文字描述"）。
      if (born) flashPinCallout(pin);
    }
  }

  /** 轮到这一枚图钉时，把它的标签（类别 + 描述）显出来一会儿。
   *
   * 为什么需要：地图缩到 0.45 倍以下时标签本来是**收着**的（一屏几十个会互相压），
   * 而播放时人正盯着看 —— 轮到谁，谁就把名字报一下，然后再收回去。
   * 停留时长跟着播放速度走：4× 时每枚只隔 65ms，还停 2 秒就会糊成一片。
   */
  function flashPinCallout(pin) {
    if (!pin) return;
    pin.classList.add('is-callout');
    var ms = Math.max(900, 2000 / (TREE.play.speed || 1));
    setTimeout(function () {
      pin.classList.remove('is-callout');
    }, ms);
  }

  /** 把"已经点亮"的标记**全部清掉**（重新播放前必做）。
   *
   * 不清会怎样：上一遍跑完（或停播）后，`is-shown` 留在每个节点、连线、图钉上；
   * 再点播放时它们照样是亮的 —— 用户看到的就是"还没展开，书签和锚点也在"。
   * 这一条是第二遍播放才露出来的，第一遍干净，所以一开始没被发现。
   */
  function clearShown() {
    if (!treeEl) return;
    Array.prototype.forEach.call(
      treeEl.querySelectorAll('.is-shown, .is-born, .is-callout'),
      function (el) {
        el.classList.remove('is-shown');
        el.classList.remove('is-born');
        el.classList.remove('is-callout');
      }
    );
  }

  /** 重画之后重贴播放状态；没在播就只把 `is-playing` 撤掉（于是全部显形）。 */
  function applyPlayState() {
    if (!treeEl) return;
    var layer = treeEl.querySelector('.chattree__layer');
    var pins = treeEl.querySelector('.chattree__pins');
    if (layer) layer.classList.toggle('is-playing', TREE.play.on);
    if (pins) pins.classList.toggle('is-playing', TREE.play.on);
    if (!TREE.play.on) return;
    var order = playOrder();
    for (var i = 0; i < TREE.play.at && i < order.length; i++) {
      markNodeShown(order[i].id, false);
    }
  }

  /** 播放/速度两颗键的字与亮灭（建完调一次；播放中就地改，不重画整条工具栏）。 */
  function paintPlayUI() {
    if (!treeEl) return;
    var play = treeEl.querySelector('.chattree__playbtn');
    if (play) {
      play.classList.toggle('is-on', TREE.play.on);
      play.setAttribute('aria-pressed', TREE.play.on ? 'true' : 'false');
      play.title = TREE.play.on ? '停下（整棵树立刻全部显示）' : '按时间顺序把整棵树放一遍';
      ui.clear(play);
      play.appendChild(iconNode(TREE.play.on ? 'stop' : 'play', 12));
      play.appendChild(h('span', { text: TREE.play.on ? '停止' : '播放' }));
    }
    var speed = treeEl.querySelector('.chattree__speedbtn');
    if (speed) {
      speed.textContent = TREE.play.speed + '×';
      speed.title = '播放速度 ' + TREE.play.speed + '×（点一下换一档：0.5 / 1 / 2 / 4）';
    }
  }

  function playTick() {
    if (!TREE.play.on) return;
    var order = playOrder();
    if (TREE.play.at >= order.length) {
      // 长完了：撤掉"播放中"，所有节点因此全部显形（不必一枚一枚去补），按钮回到"播放"
      TREE.play.on = false;
      TREE.play.at = 0;
      TREE.play.limit = 0;
      TREE.play.timer = 0;
      applyPlayState();
      applyTrail();
      paintTrail(null);
      paintPlayUI();
      return;
    }
    var one = order[TREE.play.at];
    TREE.play.at += 1;
    TREE.play.limit = one.id;
    markNodeShown(one.id, true);
    // 轨迹与树**同时**长：这条路只画到刚长出来的那一枚为止
    if (TREE.trail) paintTrail(TREE.play.limit);
    TREE.play.timer = setTimeout(playTick, PLAY_STEP / TREE.play.speed);
  }

  function startPlay() {
    if (!treeEl || !state.treeOpen) return;
    stopPlay();
    TREE.play.on = true;
    TREE.play.at = 0;
    TREE.play.limit = 0;
    clearShown();       // 从零开始（第二遍播放时，上一遍的亮灯必须清掉）
    applyPlayState();   // `is-playing` 一挂上，还没轮到的那几枚就都隐形了
    applyTrail();       // 不再"只显示轨迹"（树要长出来），轨迹也从零开始接
    paintTrail(0);
    paintPlayUI();
    playTick();
  }

  /** 停：**整棵树立刻全部显示**（不是冻在半路）。留一棵长到一半的树，人只会发懵。 */
  function stopPlay() {
    if (TREE.play.timer) {
      clearTimeout(TREE.play.timer);
      TREE.play.timer = 0;
    }
    TREE.play.on = false;
    TREE.play.at = 0;
    TREE.play.limit = 0;
    // 整棵树立刻全部显示（`is-playing` 一撤，没点亮的也就都显形了）。
    // `is-shown` 这一类标记顺手清掉：下一次播放要从零开始，不能带着上一遍的亮灯。
    clearShown();
    applyPlayState();
    applyTrail();          // 不在播了 → 回到"只显示轨迹"（若开着）
    paintTrail(null);      // 整条补全
    paintPlayUI();
  }

  function togglePlay() {
    if (TREE.play.on) stopPlay();
    else startPlay();
  }

  function cyclePlaySpeed() {
    var at = PLAY_SPEEDS.indexOf(TREE.play.speed);
    TREE.play.speed = PLAY_SPEEDS[(at + 1) % PLAY_SPEEDS.length];
    paintPlayUI();
    // 正在播：立刻按新速度排下一拍（不然得等这一拍走完才生效，手感是"改慢了没反应"）
    if (TREE.play.on && TREE.play.timer) {
      clearTimeout(TREE.play.timer);
      TREE.play.timer = setTimeout(playTick, PLAY_STEP / TREE.play.speed);
    }
  }

  /* ------------------------------------------------------------ 传送球（回溯的门）
   *
   * 只装回溯点：它是"趁手、快跳"的那个小工具，最多 5 个、带编号；书签是永久性的，
   * 它在地图上（见 treePins），不进这颗球 —— 一颗球装不下"永久"这件事。
   *
   * **形态换过一次**（用户："把锚点的传送门从输入框上面的固定位，改成带动效的，
   * 可动的圆形传送球，默认初始位置在右边框旁边"）。原先是一条横贯输入框上方的细条：
   * 写作时那一整条横带是**要被占掉的**，而回溯点是"随时可用、但多数时候不用"的东西 ——
   * 它该挂在旁边，随时能抓，而不是压在输入区上面。
   *
   * 动效**全在球里面**（用户："把那个旋转的虚线环拿掉，动效做在球的内部，白底"）：
   * 一圈沿着内缘转的弧 + 一层轻轻呼吸的内光。球本身不动、外面一件装饰都没有 ——
   * 一颗白球浮在正文旁，转的是它自己肚子里的那点光。
   *
   * 其余两处（都不在球外）：
   *   * **拖动**：抓住它走（球心鼓一点、影子加深）。**框内随便停**，只有靠近框边
   *     才吸附（用户："不要强制贴边，让它可以在框内随意拖动，靠近框的时候自动吸附"）。
   *     所以第一版"松手一律贴最近那一侧"改掉了：拖的时候是**磁铁**（进阈值后球渐渐
   *     被牵向那条边，越近越用力，但不跳），松手才**咔哒**贴上去（那一下有过渡，滑过去）。
   *   * **传送**：点一枚 → 那枚脉冲、落点闪一下（复用 `gotoPlace`）。
   *
   * 位置按**正文区**（`.chat__main`）算，不按窗口：工作台把对话挂进窗格时，
   * 右边框是窗格的右边框。记忆在本机（`qf.chat.portal`），存的是**正文区里的比例**
   *（`{x, y}`），换个窗口大小它还在原地；默认是右边框旁、高度 42% 处。
   */
  var PORTAL_KEY = 'qf.chat.portal';
  var PORTAL_SIZE = 46;          // 球的直径（与 CSS 里的 width/height 一致）
  var PORTAL_GAP = 12;           // 默认离边框多远
  var PORTAL_EDGE = 6;           // 贴边时留的缝（吸到边上就是这个数）
  var PORTAL_SNAP = 56;          // 离框边多近算"靠近"（磁铁的阈值，只在这儿生效）
  //: 球停在哪儿：`x` / `y` 是**正文区里的比例**（0~1），所以换个窗口大小它还在原处。
  //: `x: null` = 还没拖过 → 用默认（右边框旁边）。
  var portalPos = { x: null, y: 0.42 };
  var portalOpen = false;
  var portalDrag = null;
  var portalSuppressClick = false;

  function loadPortal() {
    try {
      var saved = JSON.parse(window.localStorage.getItem(PORTAL_KEY) || 'null');
      if (!saved) return;
      var y = isFinite(saved.y) ? Math.min(0.94, Math.max(0.06, Number(saved.y))) : 0.42;
      if (isFinite(saved.x)) {
        portalPos = { x: Math.min(1, Math.max(0, Number(saved.x))), y: y };
      } else if (saved.side === 'left' || saved.side === 'right') {
        // 老记录（那时只有"贴哪一侧"）：贴左 → 0、贴右 → 1，越界的部分由夹取收拾
        portalPos = { x: saved.side === 'left' ? 0 : 1, y: y };
      }
    } catch (err) {
      /* 读不到就用默认（右边框旁边） */
    }
  }

  function savePortal() {
    try {
      window.localStorage.setItem(PORTAL_KEY, JSON.stringify(portalPos));
    } catch (err) {
      /* 存不了就算了：这一次挪的位置还在界面上 */
    }
  }

  /** 球能待的那块地方：**正文区**（左栏不算），坐标相对 `.chat` 的左上角。
   *
   * 不这么做的话，"贴左边"会贴到会话列表上（`.chat` 的左边是左栏）。 */
  function portalBox() {
    var host = stationsEl && stationsEl.parentNode;
    var main = chatEl ? chatEl.querySelector('.chat__main') : null;
    var hb = host ? host.getBoundingClientRect() : null;
    if (!hb || !hb.width) return { left: 0, top: 0, width: window.innerWidth, height: window.innerHeight };
    var mb = main ? main.getBoundingClientRect() : null;
    if (!mb || !mb.width) return { left: 0, top: 0, width: hb.width, height: hb.height };
    return { left: mb.left - hb.left, top: mb.top - hb.top, width: mb.width, height: mb.height };
  }

  function portalDock() {
    return stationsEl ? stationsEl.querySelector('.chatportal__dock') : null;
  }

  /** 把球摆到 `portalPos` 说的地方（窗口大小、左栏宽度一变就要重叫一次）。 */
  /** 球心的合法范围（正文区坐标）：贴边时留 `PORTAL_EDGE` 的缝。 */
  function portalLimits(box) {
    var half = PORTAL_SIZE / 2;
    return {
      minX: half + PORTAL_EDGE,
      maxX: Math.max(half + PORTAL_EDGE, box.width - half - PORTAL_EDGE),
      minY: half + PORTAL_EDGE,
      maxY: Math.max(half + PORTAL_EDGE, box.height - half - PORTAL_EDGE),
    };
  }

  function portalClamp(v, lo, hi) {
    return Math.min(Math.max(v, lo), hi);
  }

  /** 把球心摆到正文区坐标 (cx, cy) 上；顺带定扇子朝哪边开（哪半边就往里侧开）。 */
  function portalPaint(cx, cy) {
    var dock = portalDock();
    if (!dock) return;
    var box = portalBox();
    dock.style.transform =
      'translate3d(' + Math.round(box.left + cx) + 'px,' + Math.round(box.top + cy) + 'px,0)';
    stationsEl.dataset.side = cx > box.width / 2 ? 'right' : 'left';
  }

  function applyPortalPos() {
    if (!portalDock()) return;
    var box = portalBox();
    var lim = portalLimits(box);
    // 写进去的是**球心**（CSS 里球是挂在 dock 中心的），所以两侧都算到中心。
    // `x: null` = 从没拖过 → 默认"右边框旁边"（离边 12px，比贴边的 6px 松一点）
    var cx = portalPos.x === null || portalPos.x === undefined
      ? lim.maxX - (PORTAL_GAP - PORTAL_EDGE)
      : portalPos.x * box.width;
    var cy = portalPos.y * box.height;
    portalPaint(portalClamp(cx, lim.minX, lim.maxX), portalClamp(cy, lim.minY, lim.maxY));
  }

  function openPortal() {
    if (!stationsEl || stationsEl.hidden) return;
    portalOpen = true;
    stationsEl.classList.add('is-open');
    var ball = stationsEl.querySelector('.chatportal__ball');
    if (ball) ball.setAttribute('aria-expanded', 'true');
    document.addEventListener('mousedown', portalAway, true);
    document.addEventListener('keydown', portalKeydown, true);
  }

  function closePortal() {
    portalOpen = false;
    if (stationsEl) stationsEl.classList.remove('is-open');
    var ball = stationsEl && stationsEl.querySelector('.chatportal__ball');
    if (ball) ball.setAttribute('aria-expanded', 'false');
    document.removeEventListener('mousedown', portalAway, true);
    document.removeEventListener('keydown', portalKeydown, true);
  }

  function portalAway(event) {
    if (stationsEl && !stationsEl.contains(event.target)) closePortal();
  }

  function portalKeydown(event) {
    if (event.key === 'Escape') closePortal();
  }

  /** 点球：**只有一枚回溯点时直接传送**（为了一枚去开一个扇子是多余的）。 */
  function onPortalClick(event) {
    if (portalSuppressClick) {
      // 刚才是拖动：浏览器在 pointerup 后照样会派发 click，挡掉这一次
      portalSuppressClick = false;
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    if (portalOpen) {
      closePortal();
      return;
    }
    var backs = backsOf(QF.store.places());
    if (backs.length === 1) {
      gotoPlace(backs[0], stationsEl.querySelector('.chatportal__core'));
      return;
    }
    openPortal();
  }

  /* 拖动：跟手走，松手吸附到最近那一侧。手感三处：拖动期间**关掉过渡**（宽度/位置
   * 跟着指针一帧一帧写进去，再叠一层缓动就变成追着指针慢慢爬）、拖动时球放大一点
   * （`.is-drag`）、松手那一下让它自己滑到边上（过渡打开，见 CSS）。 */
  function startPortalDrag(event) {
    if (!stationsEl || stationsEl.hidden || event.button) return;
    closePortal();
    portalDrag = { id: event.pointerId, x0: event.clientX, y0: event.clientY, moved: false };
    try {
      stationsEl.querySelector('.chatportal__ball').setPointerCapture(event.pointerId);
    } catch (err) {
      /* 抓不住也照样能拖（后面的 move 仍然会来） */
    }
    stationsEl.classList.add('is-drag');
  }

  function movePortalDrag(event) {
    if (!portalDrag || event.pointerId !== portalDrag.id) return;
    var dx = event.clientX - portalDrag.x0;
    var dy = event.clientY - portalDrag.y0;
    // 4px 以内算"手抖"，仍按点击处理（不然轻轻一碰就变成拖动）
    if (!portalDrag.moved && Math.abs(dx) + Math.abs(dy) < 4) return;
    portalDrag.moved = true;
    if (!portalDock()) return;
    var box = portalBox();
    var lim = portalLimits(box);
    // 一律换到**正文区坐标**里算（宿主里再叠上 `box.left/top` 那一段）
    var hostRect = stationsEl.getBoundingClientRect();
    var cx = event.clientX - hostRect.left - box.left;
    var cy = event.clientY - hostRect.top - box.top;
    // **磁铁**：进入阈值后按"离得越近拉得越狠"把球牵向那条边 —— 连续、不跳。
    // 平方一下：阈值边界上几乎没力（不突兀），贴到边上才是全力。
    // 拖动过程里只是被牵着，**吸死**发生在松手那一下（见 `endPortalDrag`）。
    var pull = function (v, lo, hi) {
      var near = v - lo < PORTAL_SNAP ? lo : hi - v < PORTAL_SNAP ? hi : null;
      if (near === null) return v;
      var t = 1 - Math.abs(v - near) / PORTAL_SNAP;
      return v + (near - v) * t * t;
    };
    portalPaint(
      portalClamp(pull(cx, lim.minX, lim.maxX), lim.minX, lim.maxX),
      portalClamp(pull(cy, lim.minY, lim.maxY), lim.minY, lim.maxY)
    );
  }

  function endPortalDrag(event) {
    if (!portalDrag || (event && event.pointerId !== portalDrag.id)) return;
    var moved = portalDrag.moved;
    portalDrag = null;
    if (stationsEl) stationsEl.classList.remove('is-drag');
    if (!moved) {
      // 没挪动 → 交给 click 去当"点了一下"（键盘回车走的也是那一条）
      return;
    }
    var dock = portalDock();
    var box = portalBox();
    if (!dock) return;
    var rect = dock.getBoundingClientRect();
    var hostRect = stationsEl.getBoundingClientRect();
    var lim = portalLimits(box);
    var cx = rect.left + rect.width / 2 - hostRect.left - box.left;
    var cy = rect.top + rect.height / 2 - hostRect.top - box.top;
    // **松手才吸死**：阈值内贴到那条边上，阈值外**就地停下**（框内随便停）。
    // 这一下会走 dock 的过渡（拖动期间是关掉的），所以是"滑过去"而不是"跳过去"。
    var stick = function (v, lo, hi) {
      if (v - lo <= PORTAL_SNAP) return lo;
      if (hi - v <= PORTAL_SNAP) return hi;
      return portalClamp(v, lo, hi);
    };
    cx = stick(cx, lim.minX, lim.maxX);
    cy = stick(cy, lim.minY, lim.maxY);
    portalPos = { x: cx / Math.max(1, box.width), y: cy / Math.max(1, box.height) };
    savePortal();
    portalSuppressClick = true;
    applyPortalPos();
  }

  function renderStations() {
    if (!stationsEl) return;
    var backs = backsOf(QF.store.places());
    var key =
      String(state.current || '') +
      '|' +
      backs
        .map(function (one) {
          return one.id + ':' + one.mid + ':' + one.label;
        })
        .join(',');
    if (key === placesKey) return;   // 流式每帧都会走到这儿，内容没变就别重画
    placesKey = key;
    closePortal();                   // 重画 = 收起扇子（里面那几枚都换人了）
    ui.clear(stationsEl);
    if (!backs.length) {
      stationsEl.hidden = true;
      return;
    }
    stationsEl.hidden = false;

    var fan = h('div.chatportal__fan');
    backs.forEach(function (place, index) {
      fan.appendChild(portalSlot(place, index));
    });
    var ball = h(
      'button.chatportal__ball',
      {
        type: 'button',
        title:
          backs.length === 1
            ? '回溯 1：' + placeLabel(backs[0]) + '（点一下传送 · 拖动可挪位置）'
            : '回溯点 ' + backs.length + ' 个（点开选一枚传送 · 拖动可挪位置）',
        'aria-label': '回溯传送球',
        'aria-expanded': 'false',
        onPointerdown: startPortalDrag,
        onPointermove: movePortalDrag,
        onPointerup: endPortalDrag,
        onPointercancel: endPortalDrag,
        onClick: onPortalClick,
      },
      // 球里那两层：一层会呼吸的内光 + 一圈沿内缘转的弧（顺序即层次）
      h('span.chatportal__glow'),
      h('span.chatportal__sweep'),
      h('span.chatportal__core', null, h('span.chatportal__n', { text: String(backs.length) }))
    );
    stationsEl.appendChild(h('div.chatportal__dock', null, fan, ball));
    applyPortalPos();
  }

  /** 扇子里的一枚：序号 + 那一句的开头（与树图钉上的编号是同一套）。 */
  function portalSlot(place, index) {
    var here = String(place.cid || '') === String(state.current || '');
    var slot = h(
      'button.chatportal__slot' + (here ? '.is-here' : '.is-away'),
      {
        type: 'button',
        dataset: { station: place.id },
        // 逐枚出来（错开一点），像从球里摊开
        style: { animationDelay: Math.min(index, 6) * 34 + 'ms' },
        title:
          '回溯 ' + (index + 1) +
          (here ? '（传送过去）' : '（在别的对话里，点一下传送）') +
          '：' + placeLabel(place) +
          ' · 右键写描述',
        'aria-label': '传送：回溯 ' + (index + 1) + ' ' + placeLabel(place),
        onClick: function () {
          closePortal();
          gotoPlace(place, slot);
        },
        onContextmenu: function (event) {
          event.preventDefault();
          event.stopPropagation();
          // **必须把坐标写进去**：菜单是按 `chatMenuX/Y` 摆的，不写就落在左上角
          //（这一处漏过一次 —— 扇子里右键，菜单从屏幕角上冒出来）
          chatMenuX = event.clientX;
          chatMenuY = event.clientY;
          openPlaceMenu([place], renderStations);
        },
      },
      h('span.chatportal__num', { text: String(index + 1) }),
      h('span.chatportal__tag', { text: placeLabel(place) })
    );
    return slot;
  }

  /** 满了：让球抖一下（比一句 toast 更快让人知道"是哪儿满了"）。 */
  function shakeStations() {
    if (!stationsEl || stationsEl.hidden) return;
    stationsEl.classList.remove('is-shake');
    void stationsEl.offsetHeight;
    stationsEl.classList.add('is-shake');
    setTimeout(function () {
      if (stationsEl) stationsEl.classList.remove('is-shake');
    }, 380);
  }

  /** 消息行尾那两枚图标的状态（打过标记的要亮着）。 */
  function paintPlaceIcons() {
    if (!threadEl) return;
    Array.prototype.forEach.call(threadEl.querySelectorAll('.chatmsg'), function (row) {
      var places = QF.store.placesOf(row.dataset.id);
      Array.prototype.forEach.call(row.querySelectorAll('.chaticon[data-place]'), function (btn) {
        var kind = btn.getAttribute('data-place');
        var on = places.some(function (one) {
          return placeKind(one) === kind;
        });
        btn.classList.toggle('is-on', on);
        btn.title = (on
          ? kind === 'mark'
            ? '取消书签'
            : '取消回溯点'
          : kind === 'mark'
            ? '加书签（永久，可写描述）'
            : '打个回溯点（最多 5 个，在右边那颗传送球里传送）') + ' · 右键写描述';
      });
    });
  }

  function placeKind(place) {
    return place && place.kind === 'mark' ? 'mark' : 'back';
  }

  /** `parentId` 下面**当下正显示着**的那条回答所在的行（没有就 null）。 */
  function shownReplyRow(parentId) {
    var prow = threadEl ? threadEl.querySelector('[data-id="' + parentId + '"]') : null;
    if (!prow) return null;
    var next = prow.nextElementSibling;
    while (next && (!next.classList || !next.classList.contains('chatmsg'))) {
      next = next.nextElementSibling;
    }
    return next || null;
  }

  /** 这一条下面**已经有一条回答了**吗（用来决定用户那条的标记图标出不出）。
   *
   * 判据是「孩子里有**非用户**那一条」，不是「有孩子」：接着往下问的那一条也是孩子，
   * 而它出现的时候上面那条还没有回答。
   */
  function answered(m) {
    return (state.messages || []).some(function (one) {
      return one && String(one.parentId) === String(m.id) && one.role !== 'user';
    });
  }

  /** 消息行尾那两枚图标：旗（回溯）/ 丝带（书签）。 */
  function placeButtons(m) {
    var places = QF.store.placesOf(m.id);
    var mine = {};
    places.forEach(function (one) {
      mine[placeKind(one)] = one;
    });
    return ['back', 'mark'].map(function (kind) {
      var one = mine[kind];
      var btn = iconButton(
        kind === 'mark' ? 'bookmark' : 'flag',
        kind === 'mark' ? '加书签（永久，可写描述）' : '打个回溯点（最多 5 个）',
        function () {
          togglePlace(kind, m);
        },
        '.chatplacebtn'
      );
      btn.dataset.place = kind;
      if (one) btn.classList.add('is-on');
      // 右键 = 菜单（还没标记的给"加一个"，已标记的给"写描述 / 取消"）。
      // 打一下不打断（左键就够），想写描述再走菜单 —— 描述是可选的，别拦在打的路上。
      btn.addEventListener('contextmenu', function (event) {
        event.preventDefault();
        event.stopPropagation();
        // **按当下重查一遍**：`one` 是建这一行时抓的快照，打过之后它就过期了
        //（症状：加完书签再右键，菜单里还是"加书签"）
        var live = QF.store.placesOf(m.id).filter(function (each) {
          return placeKind(each) === kind;
        })[0];
        var box = btn.getBoundingClientRect();
        chatMenuX = box.left;
        chatMenuY = box.bottom + 4;
        openPlaceMenu(live ? [live] : [], refreshPlaces,
          live ? null : { kind: kind, m: m }
        );
      });
      return btn;
    });
  }

  /** 打 / 取消一个位置标记。 */
  function togglePlace(kind, m) {
    var places = QF.store.placesOf(m.id);
    var one = places.filter(function (each) {
      return placeKind(each) === kind;
    })[0];
    if (one) {
      QF.store.removePlace(one.id);
      ui.toast('已取消' + placeKindName(one), 'info', 1200);
    } else if (
      !QF.store.addPlace({
        kind: kind,
        cid: state.current || '',
        mid: m.id,
        quote: String(m.content || '').slice(0, 60),
      })
    ) {
      if (kind === 'mark') {
        ui.toast('书签没加上，再试一次', 'warn', 2400);
      } else {
        ui.toast('回溯最多 5 个 —— 先取消一个再打（书签不限量）', 'warn', 3200);
        shakeStations();
      }
      return;
    } else {
      ui.toast(
        kind === 'mark' ? '已加书签（右键可写描述）' : '已打回溯点（最多 5 个，在右边那颗传送球里传送）',
        'info',
        1800
      );
    }
    refreshPlaces();
  }

  /** 标记的右键菜单：**按类别成组**（加 / 写描述 / 取消），一次把两类都摆出来。
   *
   * `pending` = 这条还没标记时**可以补上的那些**：一个 `{kind, m}`，或一串（两者都给）。
   * 消息行尾那两枚图标各传一个（它管的就是那一种）；对话树节点右键传两个 ——
   * 用户："支持一下在对话树的节点上直接右键添加书签和回溯点的机制"：站在地图上，
   * 两种都该给，而不是先回对话、找到那一行、再点其中一枚丝带。
   *
   * 类别顺序**写死**（书签在前、回溯在后），不按打的时间：同一个地点上的两枚钉子，
   * 每次打开菜单都该是同一个样子。
   */
  function openPlaceMenu(places, after, pending) {
    var wants = [];
    if (pending) wants = pending.length ? pending.slice() : [pending];
    var live = places || [];
    var items = [];
    ['mark', 'back'].forEach(function (kind) {
      var isMark = kind === 'mark';
      var mine = live.filter(function (one) {
        return placeKind(one) === kind;
      });
      if (mine.length) {
        mine.forEach(function (place, index) {
          // **不带内容**（用户："右键菜单里面，不要出现批注的具体内容了"）：
          // 同类有好几条时用序号分开（"书签 2/3"），描述与原文留在地图与抽屉上。
          var name = placeKindName(place) + (mine.length > 1 ? ' ' + (index + 1) + '/' + mine.length : '');
          items.push({
            icon: 'pencil',
            label: '写描述',
            hint: name,
            run: function () {
              editPlaceLabel(place, after);
            },
          });
          items.push({
            icon: 'trash',
            danger: true,
            label: '取消' + placeKindName(place),
            hint: name,
            run: function () {
              QF.store.removePlace(place.id);
              if (after) after();
              refreshPlaces();
              ui.toast('已取消' + placeKindName(place), 'info', 1200);
            },
          });
        });
        return;
      }
      var want = wants.filter(function (one) {
        return one.kind === kind;
      })[0];
      if (!want) return;
      items.push({
        icon: isMark ? 'bookmark' : 'flag',
        label: isMark ? '加书签' : '打个回溯点',
        hint: isMark ? '永久，可写描述' : '最多 5 个，在右边那颗传送球里',
        run: function () {
          togglePlace(kind, want.m);
          if (after) after();
        },
      });
    });
    showChatMenu(items);
  }

  /** 写 / 改一句描述（用现成的那只输入弹层，原生 prompt 会冻住整页）。 */
  function editPlaceLabel(place, after) {
    askName('这条' + placeKindName(place) + '的描述', place.label || '', '保存').then(function (label) {
      if (!label) return;   // 取消、或没写字：不动
      QF.store.updatePlace(place.id, { label: label });
      if (after) after();
      refreshPlaces();
      if (state.treeOpen) renderTree();
      ui.toast('描述已保存', 'info', 1200);
    });
  }

  /** 传送过去。 */
  function gotoPlace(place, dot) {
    if (dot) {
      dot.classList.add('is-go');
      setTimeout(function () {
        dot.classList.remove('is-go');
      }, 420);
    }
    var cid = String(place.cid || '');
    var away = !!cid && cid !== String(state.current || '');
    var land = function () {
      revealMessage(place.mid);
      if (away) arriveThread();
    };
    if (!away) {
      land();
      return;
    }
    openConversation(cid)
      .then(land)
      .catch(function () {
        ui.toast('这个标记指向的对话找不到了', 'warn', 2800);
      });
  }

  /* ------------------------------------------------------------ 地图上的图钉
   *
   * 对话树就是地图（用户："这两类标记都要在对话树上体现——对话树是地图"）。
   * 第一版是画在 SVG 里的小图形：一面临时旗（8px）、一个带编号的点（13px）—— 画在
   * 节点右下角。缩到 0.3 倍（158 个节点就是那个倍数）时只剩两三个像素，
   * 用户的原话是"太小太不明确。想象你要在一张地图上标记某个地点"。
   *
   * 现在按地图的做法来：**图钉是屏幕坐标的一层 HTML**，地点是地图坐标。
   *   * 尖头**指着那个地点**（节点左上角，右上角被分叉角标与「⋯」占着）；
   *   * 旁边挂**名字**（写了描述就是描述，没写就是那一句的开头）—— 名字始终可读；
   *   * 两类各长各的：书签 = 丝带徽（主色），回溯 = 带编号的徽（琥珀）;
   *   * 缩得太小时名字先收（`.is-tight`），悬停那一枚再浮出来 —— 地图缩到一屏
   *     装满节点时，几十个名字会互相压，不如都收起来。
   *
   * 点一枚 = 去那个地点（与点节点同一条路）；右键 = 写描述 / 取消。
   */

  /** 回溯点的**全局编号**（与传送球扇子里那几枚的号是同一个）。 */
  function backIndexOf(place) {
    var all = backsOf(QF.store.places()).map(function (one) {
      return one.id;
    });
    var at = all.indexOf(place.id);
    return at >= 0 ? at + 1 : 1;
  }

  function bookmarksOf(places) {
    return (places || []).filter(function (one) {
      return placeKind(one) === 'mark';
    });
  }

  /** 同一个地点上的钉子**竖着码**：一枚一格（`PIN_STEP`），顺序固定。
   *
   * 两版都栽在同一件事上，值得记下来：
   *   * 第一版只按 slot 错开 5px/6px —— 徽章自己就有 22px，看上去就是"完全重合"
   *    （用户报的："一个点同时有书签和回溯点，它们在对话树上完全重合了"）；
   *   * 第二版改成一枚一格 26px，但那个 26 是**用户坐标**，被地图缩放一乘
   *    （0.066 倍 → 1.7px）又叠回去了。
   * 所以它是一段**屏幕像素**：锚点归地图，错开归屏幕（见 `applyTreeView`）。
   */
  var PIN_STEP = 26;

  function placeSlot(place) {
    return placeKind(place) === 'mark' ? 0 : 1;
  }

  function treePinsLayer(model) {
    var layer = h('div.chattree__pins');
    model.nodes.forEach(function (node) {
      QF.store
        .placesOf(node.message.id)
        .slice()
        // 书签在**下**（贴着地点）、回溯在上：不按打的时间排，免得同一对钉子下次换个位置
        .sort(function (a, b) {
          return placeSlot(a) - placeSlot(b);
        })
        .forEach(function (place, slot) {
          layer.appendChild(treePin(place, node, slot));
        });
    });
    return layer;
  }

  /** 一枚图钉：锚在节点左上角、尖头指过去，脑袋旁边挂名字。 */
  function treePin(place, node, slot) {
    var isMark = placeKind(place) === 'mark';
    var pin = h('button.ctpin2.ctpin2--' + (isMark ? 'mark' : 'back'), {
      type: 'button',
      // 锚点：节点左上角；同一节点上多枚时错开一点（图钉挨在一起会分不清哪个是哪个）
      // 锚点是**地点本身**（同一个地点上几枚钉子锚点相同）；错开留给屏幕坐标去做
      //（见 `applyTreeView` 里那一句 translate）—— 锚点跟着地图缩放，错开不能跟。
      dataset: {
        ax: String(node.x + 6),
        ay: String(node.y - node.h / 2),
        slot: String(slot),
        place: place.id,
        mid: String(node.message.id),   // 播放时跟着节点一起显形
      },
      title: treePinTitle([place]) + '\n点一下去这个地方',
      'aria-label': placeKindName(place) + '：' + placeLabel(place),
      // 逐枚落下，像往地图上插钉子（延迟按这一格上的次序）
      style: { animationDelay: Math.min(slot, 8) * 55 + 'ms' },
      onClick: function (event) {
        event.stopPropagation();
        // 与点节点同一条路：收起地图 → 回到对话 → 那条消息闪一下（`revealMessage` 自带）。
        // 地图在这儿是个浮层，所以"去那个地点"必然要连地图一起收掉。
        marksOpen = false;
        state.treeOpen = false;
        renderBar();
        renderTree();
        revealMessage(place.mid);
      },
      onContextmenu: function (event) {
        event.preventDefault();
        event.stopPropagation();
        chatMenuX = event.clientX;
        chatMenuY = event.clientY;
        openPlaceMenu([place], refreshPlaces);
      },
    });
    pin.appendChild(
      h(
        'span.ctpin2__badge',
        null,
        isMark
          ? iconNode('bookmark', 12)
          : h('span.ctpin2__n', { text: String(backIndexOf(place)) })
      )
    );
    // 尖头：一枚朝下的小三角，把徽章接到锚点上（有它才像"插在地图上"）
    pin.appendChild(h('span.ctpin2__leg'));
    // 名字：**写上类别**再写描述。类别靠形状与颜色也认得出，但用户要的是"明确"，
    // 那就别让他猜（`书签 · 这里是关键分叉`）。
    pin.appendChild(
      h(
        'span.ctpin2__label',
        null,
        h('span.ctpin2__kind', { text: placeKindName(place) }),
        h('span.ctpin2__text', { text: placeLabel(place) })
      )
    );
    return pin;
  }

  /** 地图角落那一小块图例 —— "图钉是什么"这件事得能自己看懂。 */
  function treeLegend() {
    var marks = bookmarksOf(QF.store.places()).length;
    var backs = backsOf(QF.store.places()).length;
    return h(
      'div.chattree__legend',
      null,
      h('span.chattree__legendtitle', { text: '图钉' }),
      h('i.chattree__legendpin.is-mark', null, iconNode('bookmark', 9)),
      h('span.chattree__legendtext', { text: '书签 · 永久（' + marks + '）' }),
      h('i.chattree__legendpin.is-back', null, h('span.ctpin2__n', { text: '1' })),
      h('span.chattree__legendtext', { text: '回溯 · 快速跳转（' + backs + '/5）' }),
      h('span.chattree__legendtext.is-dim', { text: '右键图钉可写描述' })
    );
  }

  function treePinTitle(places) {
    return places
      .map(function (one) {
        return placeKindName(one) + '：' + placeLabel(one);
      })
      .join('\n');
  }

  /** 到了：整条线程轻轻沉一下（只在跨对话传送时用 —— 同一条对话里闪落点就够了）。 */
  function arriveThread() {
    if (!threadEl) return;
    threadEl.classList.remove('is-arrive');
    void threadEl.offsetHeight;   // 重排一次，连着传送两次动画也会重放
    threadEl.classList.add('is-arrive');
    setTimeout(function () {
      threadEl.classList.remove('is-arrive');
    }, 420);
  }


  /* ------------------------------------------------------------ 书签抽屉
   *
   * 装"永久标记"那一类的抽屉：按打的先后**倒序**列出全部书签（跨对话），点一条就传送过去。
   * 回溯不上这儿 —— 它是"趁手、快跳"的那 5 个，住在正文区边上那颗传送球里（用户把两者
   * 分得很清楚："回溯是一个小工具…然后现在是书签，这个是永久性的标记"）。
   *
   * 动效三段，每段都有它要解决的问题：
   *   * **进来**：背景 150ms 淡入、抽屉 200ms 从右滑入（缓出曲线，像被推进来而不是弹进来）；
   *     列表项再逐条浮现（每条错 26ms）—— 一次全亮像"啪"地贴上去，逐条才像翻出一叠卡片。
   *   * **出去**：先加 `is-closing` 播 160ms 反向动画，**播完再拆 DOM**。直接 remove
   *     就是"啪"地消失（用户要的"注意平滑动效"说的就是这种地方）。
   *   * **传送**：沿用 `gotoPlace` —— 点的那条脉冲、落点闪一下、跨对话时整条线程轻沉。
   *     `prefers-reduced-motion` 一开，三段全都静止。
   */
  var marksOpen = false;
  var marksClosing = false;
  var marksTimer = 0;

  /** 这条书签在哪儿（给跨对话的认路用）。 */
  function placeWhere(place) {
    var cid = String(place.cid || '');
    if (!cid || cid === String(state.current || '')) return '当前对话';
    var conv = (state.list || []).filter(function (one) {
      return String(one.id) === cid;
    })[0];
    return (conv && conv.title) || '另一条对话';
  }

  function placeTime(place) {
    var d = new Date(place.at || Date.now());
    var pad = function (n) {
      return (n < 10 ? '0' : '') + n;
    };
    return (
      d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) +
      ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes())
    );
  }

  /** 标记这一摊的界面全都刷一遍（消息行尾的图标 / rail / 地图上的图钉与图例 / 抽屉）。 */
  function refreshPlaces() {
    renderStations();
    paintPlaceIcons();
    // 树开着就重画整棵树：图钉、图例里的计数、工具栏上那颗「书签 · N」都在那儿
    // （抽屉跟着 `renderTree` 一起重画，它挂在画布那一层）。
    if (state.treeOpen) renderTree();
  }

  function toggleMarks() {
    if (marksOpen) closeMarks();
    else openMarks();
  }

  /** 工具栏那颗「书签」的亮灭。
   *
   * 单独管这一颗而不是整块重画工具栏：`renderTree` 会把画布连同**已播到一半的
   * 开合动画**一起重建 —— 关抽屉那 160ms 的滑出就没了（那是这一处唯一的讲究）。 */
  function paintMarksBtn() {
    var btn = treeEl ? treeEl.querySelector('.chattree__marksbtn') : null;
    if (!btn) return;
    btn.classList.toggle('is-on', marksOpen);
    btn.setAttribute('aria-expanded', marksOpen ? 'true' : 'false');
  }

  function openMarks() {
    if (!state.treeOpen) return;   // 抽屉只在树上：它是那张地图的地点清单
    if (marksTimer) {
      clearTimeout(marksTimer);
      marksTimer = 0;
    }
    marksOpen = true;
    marksClosing = false;
    renderMarks();
    paintMarksBtn();
  }

  function closeMarks() {
    if (!marksOpen || marksClosing) return;
    var back = marksEl ? marksEl.querySelector('.chatmarks__backdrop') : null;
    var sheet = marksEl ? marksEl.querySelector('.chatmarks__sheet') : null;
    if (!back) {
      marksOpen = false;
      marksClosing = false;
      renderMarks();
      renderBar();
      return;
    }
    marksClosing = true;
    paintMarksBtn();   // 立刻不亮：按下去就有回应，别等那 160ms 播完
    back.classList.add('is-closing');
    if (sheet) sheet.classList.add('is-closing');
    // **先播完再拆**：动画 160ms，留一点余量
    marksTimer = setTimeout(function () {
      marksTimer = 0;
      marksOpen = false;
      marksClosing = false;
      renderMarks();
    }, 180);
  }

  /** Esc：先关抽屉，**不让路给底下那层**（不然一下把整张地图也关了）。
   *
   * 注册用的是捕获阶段（`true`），启动时那条"逐层关"的 Esc 挂在冒泡阶段 ——
   * 捕获先到，这里 stop 一下，底下的树就收不到这一下。 */
  function marksKeydown(ev) {
    if (ev.key !== 'Escape') return;
    ev.preventDefault();
    ev.stopPropagation();
    closeMarks();
  }

  function renderMarks() {
    if (!marksEl) return;
    ui.clear(marksEl);
    document.removeEventListener('keydown', marksKeydown, true);
    if (!marksOpen) return;
    document.addEventListener('keydown', marksKeydown, true);

    var places = QF.store
      .places()
      .filter(function (one) {
        return placeKind(one) === 'mark';
      })
      .reverse();   // 新的在前：刚标下来的最想马上就看见

    var list = h('div.chatmarks__list');
    if (!places.length) {
      list.appendChild(
        h('div.chatmarks__empty', {
          text:
            '这张图上还没有图钉。回到对话里，在消息尾部点那枚丝带（或右键 →「加书签」）——' +
            '标过的地方就会变成地图上的一枚图钉，同时列在这里。书签是永久的，数量不限。',
        })
      );
    }
    places.forEach(function (place, index) {
      list.appendChild(markItem(place, index));
    });

    marksEl.appendChild(
      h(
        'div.chatmarks__backdrop',
        {
          onClick: function (event) {
            if (event.target === event.currentTarget) closeMarks();
          },
        },
        h(
          'div.chatmarks__sheet',
          null,
          h(
            'div.chatmarks__head',
            null,
            iconNode('bookmark', 15),
            h('span.chatnotes__title', { text: '书签' }),
            h('span.chatnotes__count', { text: places.length ? places.length + ' 个' : '' }),
            h('span.chatmarks__lead', { text: '永久标记 · 点一条传送' }),
            iconButton('close', '关闭（Esc）', closeMarks)
          ),
          list
        )
      )
    );
  }

  /** 抽屉里的一条：丝带 + 描述（没描述就退到那一句的开头）+ 在哪儿 + 两颗小动作。 */
  function markItem(place, index) {
    var item = h('div.chatmarkitem', {
      dataset: { place: place.id },
      title: '传送到这一条',
      // 逐条浮现：错开一点，像翻出一叠卡片（`both` + delay = 没轮到它时先隐形）
      style: { animationDelay: Math.min(index, 12) * 26 + 'ms' },
      onClick: function (event) {
        if (event.target && event.target.closest && event.target.closest('.chatmarkitem__acts')) return;
        // 抽屉挂在树上：传送 = 收起地图（不然回来了树还盖在上面），再跳那个地点
        marksOpen = false;
        state.treeOpen = false;
        renderBar();
        renderTree();
        gotoPlace(place, null);
      },
      onContextmenu: function (event) {
        event.preventDefault();
        event.stopPropagation();
        chatMenuX = event.clientX;
        chatMenuY = event.clientY;
        openPlaceMenu([place], refreshPlaces);
      },
    });
    item.appendChild(h('span.chatmarkitem__ribbon', null, iconNode('bookmark', 13)));
    item.appendChild(
      h(
        'div.chatmarkitem__body',
        null,
        h('div.chatmarkitem__text', { text: placeLabel(place) }),
        h('div.chatmarkitem__meta', { text: placeWhere(place) + ' · ' + placeTime(place) })
      )
    );
    item.appendChild(
      h(
        'div.chatmarkitem__acts',
        null,
        iconButton(
          'pencil',
          '写描述',
          function (event) {
            event.stopPropagation();
            editPlaceLabel(place, refreshPlaces);
          },
          '.chatmarkitem__act'
        ),
        iconButton(
          'close',
          '取消这个书签',
          function (event) {
            event.stopPropagation();
            QF.store.removePlace(place.id);
            refreshPlaces();
            ui.toast('已取消书签', 'info', 1200);
          },
          '.chatmarkitem__act'
        )
      )
    );
    return item;
  }

  /** 一条批注头上的时间与"在哪儿"（显示态与编辑态都要有）。 */
  function noteMetaNode(note) {
    return h(
      'div.chatnotes__meta',
      null,
      h('span.chatnotes__time', { text: noteTime(note) }),
      note.cid && note.cid === state.current
        ? h('span.chatnotes__tag', { text: '本次对话' })
        : note.cid
          ? h('span.chatnotes__tag', { text: '另一段对话' })
          : null
    );
  }

  function noteNode(note) {
    var box = h('div.chatnotes__item');

    if (notesEditing === note.id) {
      // **在原形上编辑**（用户："对于长的批注，点击编辑的时候，编辑框会缩得很小。
      // 不要让它动，要保持在原形上编辑"）：时间与引文照旧留着 —— 只看得到输入框的话，
      // 改到一半会忘了自己改的是哪一段；顺带这一条的形状也基本不变。
      box.appendChild(noteMetaNode(note));
      if (note.quote) {
        box.appendChild(h('div.chatnotes__quote', { text: note.quote }));
      }
      var draft = h('textarea.chatnotes__input', {
        rows: '3',
        // 原来"正文那块"多高，输入框就不许比它矮（同上）
        style: notesEditMin ? { minHeight: notesEditMin + 'px' } : null,
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
            return;
          }
          if (event.key !== 'Enter' || event.isComposing || event.shiftKey) return;
          event.preventDefault();
          saveNote();
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

    box.appendChild(noteMetaNode(note));
    if (note.quote) {
      box.appendChild(h('div.chatnotes__quote', { text: note.quote }));
    }
    box.appendChild(
      h('div.chatnotes__text' + (note.text ? '' : '.is-bare'), {
        text: note.text || '（只有高亮，没写字）',
      })
    );
    box.appendChild(
      h(
        'div.chatnotes__acts',
        null,
        note.mid
          ? h(
              'button.chatnotes__act.is-pri',
              {
                type: 'button',
                onClick: function () {
                  closeNotes();
                  revealMessage(note.mid);
                },
              },
              '跳到这句话'
            )
          : null,
        h(
          'button.chatnotes__act',
          {
            type: 'button',
            onClick: function () {
              copyText(note.text || note.quote, '批注已复制');
            },
          },
          '复制'
        ),
        h(
          'button.chatnotes__act',
          {
            type: 'button',
            onClick: function () {
              // 量下"正文那块"现在多高，交给输入框当高度下限（见 `fitNoteArea`）
              var text = box.querySelector('.chatnotes__text');
              notesEditMin = text ? Math.round(text.getBoundingClientRect().height) : 0;
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
              if (removeMarkTracked(note.id)) {
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

    // 批注栏**只装批注**：高亮与删除线是"给正文做记号"，不进这份清单
    //（用户："高亮和批注是分开的两个东西"）。它们要撤，就在正文上右键撤。
    var notes = (QF.store.notes() || []).filter(function (one) {
      return QF.store.markKind(one) === 'note';
    });
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
      placeholder: '写一句…（Enter 保存，Shift+Enter 换行）',
      value: notesDraft,
      onInput: function (event) {
        notesDraft = event.target.value;
        saveBtn.disabled = !String(notesDraft || '').trim();
      },
      onKeydown: function (event) {
        if (event.key === 'Escape') {
          event.preventDefault();
          closeNotes();
          return;
        }
        // 批注框一律"Enter 就是保存"（换行走 Shift+Enter）——与旁批栏里那张同一套
        if (event.key !== 'Enter' || event.isComposing || event.shiftKey) return;
        event.preventDefault();
        saveNote();
      },
    });

    var needle = String(markQuery || '').trim().toLowerCase();
    var shown = needle
      ? notes.filter(function (one) {
          var hay = String(one.text || '') + ' ' + String(one.quote || '');
          return hay.toLowerCase().indexOf(needle) >= 0;
        })
      : notes;
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
            h('span.chatnotes__title', { text: '批注' }),
            h('span.chatnotes__count', {
              text: notes.length ? notes.length + ' 条' : '',
            }),
            iconButton('close', '关闭（Esc）', closeNotes)
          ),
          h(
            'div.chatnotes__searchrow',
            null,
            h('input.input.chatnotes__search', {
              type: 'search',
              placeholder: '搜批注与原文…',
              value: markQuery,
              onInput: function (event) {
                markQuery = event.target.value;
                renderNotes();
                var again = notesEl.querySelector('.chatnotes__search');
                if (again) {
                  again.focus();
                  again.setSelectionRange(again.value.length, again.value.length);
                }
              },
            })
          ),
          h(
            'div.chatnotes__compose',
            null,
            pendingMark ? h('div.chatnotes__quote', { text: pendingMark.quote }) : null,
            pendingMark || notesEditing
              ? [draft, saveBtn]
              : h('div.chatnotes__howto', {
                  text:
                    '选中它的回答里的一段，右键 →「批注这一段」（或按 ⌘/Ctrl+⇧A）—— ' +
                    '话就写在正文右边那一栏里，不经过这个面板；这里只做总览与搜索。' +
                    '高亮与删除线是给正文做记号，也不进这份清单。',
                })
          ),
          shown.length
            ? h('div.chatnotes__list', null, shown.map(noteNode))
            : h('div.chatnotes__empty', {
                text: notes.length
                  ? '没有匹配的批注。'
                  : '还没有批注。在它的回答里选中一段话，右键 →「批注这一段」（或按 ⌘/Ctrl+⇧A）' +
                    '—— 话在正文右边**就地**写，读到哪句就在哪句旁边。',
              })
        )
      )
    );
    if (pendingMark || notesEditing) draft.focus();
    // 面板里那条正在改的：挂进 DOM 之后按"原来的高度 + 内容"撑开（见 `fitNoteArea`）
    if (notesEditing) {
      var grow = fitNoteArea(notesEl.querySelector('.chatnotes__item .chatnotes__input'), notesEditMin);
      if (grow) grow();
    }
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
    // 传送球上次被拖到哪儿（默认：右边框旁边）
    loadPortal();

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
    // 传送球：先建出来、默认藏起来（有回溯点时 `renderStations` 会放开）。
    // **是个常驻浮件**，一会儿挂到 `.chat` 上（不是建在输入区里）：它要贴着正文区的
    // 边浮着，位置按正文区尺寸算（见 `applyPortalPos`）。
    stationsEl = h('div.chatportal', {
      hidden: true,
      role: 'group',
      'aria-label': '回溯传送球',
    });
    threadEl = h('div.chat__thread', { role: 'log', 'aria-live': 'polite' });
    // 滚上去就露出「回到最新」（见 refreshJump）
    // 在回答里选中一段 → 右键 → 三种标记（见 onMarkContextMenu）
    threadEl.addEventListener('contextmenu', onMarkContextMenu);
    // 旁批卡片的 top 是"它标的那一行在哪"的函数，而那是**版式的结果**：线程一换
    // 尺寸（窗口缩放、收起会话栏、面板开合）就得重算一次（见 relayoutMargins）。
    // 盯线程自己而不是 window：工作台把这一套挂进窗格时，窗口尺寸根本不变。
    if (window.ResizeObserver) {
      new ResizeObserver(scheduleMarginRelayout).observe(threadEl);
    }
    threadEl.addEventListener('scroll', function () {
      // 用户**自己**滚动时才更新跟随状态。程序化的 `scrollTop = scrollHeight`
      // 也会走到这里，但那时本来就贴着底，判定为真、状态不变。
      stickBottom = nearBottom();
      refreshJump();
    });
    inputEl = h('textarea.chat__input', {
      rows: '1',
      // `/` 那件事写进 placeholder：不写的话没人知道有这个东西（一句就够，别做教程）
      placeholder: '问点什么，或者贴一段材料…（打 / 召唤一个流程）',
      onInput: function (event) {
        growInput(event);
        paintSkillPick();
      },
      onKeydown: onKeydown,
    });
    // 输入框上方那份 `/` 清单（空的时候不占位置，见 CSS 的 `.chatskill:not(.is-open)`）
    skillPickEl = h('div.chatskill', { role: 'listbox', 'aria-label': '流程' });
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
    notesEl = h('div.chatnotes', { role: 'dialog', 'aria-label': '批注' });
    // 书签抽屉：**挂在对话树里**（每次 `renderTree` 塞进画布那一层）。所以这里只建一个
    // 常驻容器 —— 它在两棵树之间搬来搬去，但节点本身不重建，抽屉的滚动位置因此活得下来。
    marksEl = h('div.chatmarks', { role: 'dialog', 'aria-label': '书签' });
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
              // `/` 那份清单挂在输入框**上方**（它是"正在挑"，不是第三条药丸）。
              skillPickEl,
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
                iconButton('note', '批注', function () {
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
                    title: deepTitle() + '（右键调思考强度）',
                    'aria-label': '深度思考：' + (state.deepThink ? '开' : '关') + '（右键调强度）',
                    'aria-pressed': state.deepThink ? 'true' : 'false',
                    onClick: toggleDeep,
                    // 强度在这颗药丸的右键菜单里 —— 与它管的是同一件事
                    onContextMenu: openThinkMenu,
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
        // 传送球挂在 `.chat` 上（与那几个浮层同级）：它是浮件，不进输入区。
        stationsEl,
        treeEl,
        demoEl,
        notesEl,
        problemEl
      )
    );
    // `.chat` 是刚建出来的（对话树 / 批注 / 大题那几个浮层是它的**兄弟**，
    // 不能塞进它里面），所以挂完再取引用、落上"会话栏开没开"—— 第一帧就对，不会闪。
    chatEl = rootEl.querySelector('.chat');
    // 传送球的位置是**按正文区算**的：正文区一改尺寸（收起左栏、拖宽度、窗口缩放）
    // 就得重摆一次。盯正文区自己而不是窗口 —— 收起左栏时窗口尺寸根本没变。
    var portalMain = chatEl.querySelector('.chat__main');
    if (portalMain && window.ResizeObserver) {
      new ResizeObserver(function () {
        applyPortalPos();
      }).observe(portalMain);
    }
    window.addEventListener('resize', function () {
      applyPortalPos();
    });
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
    // 用户先把"分组树"改成了三个区（"分组其实就是文件夹归档的逻辑"），后来又
    // 明确要回文件管理那套（"归档实际上是文件管理式的"）—— 所以：**归档区仍然画
    // 文件夹树、仍能拖放**，未归档区才是按时间平铺。两件事合起来是现在这个样子。
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
    // 每一行都得有图标：菜单里"有的行有、有的行空着"最扎眼（用户报过这一处）
    var items = [
      { icon: 'folderUp', label: '移到最外层', run: function () { batchMove(''); } },
    ];
    (state.folders || []).forEach(function (path) {
      items.push({
        icon: 'folder',
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

  /** 点在菜单外面 → 走开。用 `mousedown` 而不是 `click`/`contextmenu`。
   *
   * 这一步的时机是整块菜单的要害：**关旧菜单必须早于开新菜单**。`contextmenu` 同时
   * 是"关"与"开" —— 右键时 threadEl 的监听先把新菜单画出来，事件再冒泡到 document，
   * 挂在 document 上的那条"关菜单"就把**刚画出来的这张**撤掉了，菜单只闪一瞬。
   * 原先是 `{once:true}` + `setTimeout` 那么写的，症状正是"标注/高亮一次之后，
   * 再选任何文字都没有选择栏了"——而且它每开一次菜单都会重新武装，等于永久失效。
   * `mousedown` 比 `contextmenu` 先到，于是关旧的、随后再开新的，两件事互不打扰。
   *
   * 与 `sidetree.js` / `mounts.js` 那几个小菜单同一套写法。
   */
  function closeChatMenuAway(ev) {
    var menu = document.getElementById('chat-menu');
    if (menu && !menu.contains(ev.target)) closeChatMenu();
  }

  function closeChatMenuKey(ev) {
    if (ev.key !== 'Escape') return;
    // **菜单是"最上面那一层"**：Esc 只该关它，不该顺手把底下的对话树也关掉
    //（启动时那条"逐层关"的 Esc 挂在冒泡阶段，这里拦一下它就收不到了；与批注抽屉同一处讲究）
    ev.stopPropagation();
    closeChatMenu();
  }

  function closeChatMenu() {
    var found = document.getElementById('chat-menu');
    if (found) found.remove();
    document.removeEventListener('mousedown', closeChatMenuAway, true);
    document.removeEventListener('keydown', closeChatMenuKey, true);
  }


  /** 一个小弹出菜单（会话行 / 分组行右键用）。
   *
   * 每条 = 图标 + 文字 +（可选）右边的快捷键：用户要的是"重命名，置顶，删除，
   * 都是图标 + 文字，删除做成红色"。 */
  function showChatMenu(items) {
    closeChatMenu();
    var menu = h('div.chattree__menu', { id: 'chat-menu', role: 'menu' });
    items.forEach(function (item) {
      // `item.node`：这一项不是一行按钮，而是**任意节点**（思考强度那根拖动条
      // 就是这么塞进来的 —— 它不是"点一下"的东西）。
      if (item.node) {
        menu.appendChild(item.node);
        return;
      }
      var row = h('button.chattree__menuitem' + (item.danger ? '.is-danger' : ''), {
        type: 'button',
        role: 'menuitem',
        html:
          '<span class="chattree__menuico">' +
          // 同样查 ui.js 那张唯一的表（原先它单独查 `MENU_ICONS`，
          // `bookmark`/`flag` 不在那张表里，于是那两行是空的）
          (item.icon && ui.iconPath(item.icon)
            ? '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" ' +
              'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' +
              ui.iconPath(item.icon) +
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
    // 推到下一拍再装：开菜单这一下自己也会派发 mousedown，同拍装会把这次点击
    // 当成"点在外面"（与 sidetree.js / mounts.js 同一个讲究）。
    setTimeout(function () {
      document.addEventListener('mousedown', closeChatMenuAway, true);
      document.addEventListener('keydown', closeChatMenuKey, true);
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
    // 药丸要在 `renderBar()` **之后**补：那一行是 `renderBar` 重建的，
    // 插早了会被它连锅端掉（这颗药丸是动态插进那一行的，见 `paintSkillPills`）。
    paintSkillPills();
    renderTree();
    if (!state.messages.length) {
      renderEmptyThread();
      return;
    }
    ui.clear(threadEl);
    activePath().forEach(function (m) {
      threadEl.appendChild(messageRow(m));
    });
    // 旁批的位置与连线要**等这些行都进了 DOM** 才算得准：`messageRow` 里那次 paintMarks
    // 跑在这一行挂上去之前，量到的 rect 全是 0（实测：连线退化成 `3,0 …`）。
    // 推到下一帧算一次，位置与线一次到位 —— 不靠 ResizeObserver 顺手兜底。
    scheduleMarginRelayout();
    // 回溯条跟着一次重画走：切对话、打标记之后"哪些在这里"要跟着变
    //（内容没变时它自己会跳过，见 placesKey）
    renderStations();
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
          }),
          // 我自己的提问同样可以打标记（"这个位置我要回来"与谁说的无关）：
          // 旗 = 回溯（趁手），丝带 = 书签（永久）。
          /**
           * 但**回答还没到的那一条先不给**（用户："用户发消息，agent 未回复的时候，
           * 书签和锚点的图标竟然存在"）：位置标记是"我要回到这儿"，而这儿还没有可回看的
           * 内容；更要紧的是发送那一刻界面上就跳出一排图标，看着像已经答完了。
           * 回答一到，`start` 那一步会把这两枚补上（见 `send` 里"回答到了"那段）。
           */
          answered(m) ? placeButtons(m) : null,
          // 用 `/` 装上的 skill 记在**这条消息**上（点一下可取下）—— 与记号同一类：
          // "从这儿往下都该这样"，只是它管的是"怎么陪他"，不是"标记哪儿"。
          // 一串（可能同时挂着流程 + 口吻 + 纪律），`h` 会把数组摊平。
          skillChips(m)
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
    // 标记要**等这一行挂好之后**才画：旁批是往行的第三个格子里放的，
    // 而 `body.closest('.chatmsg')` 在 body 还没进 DOM 时是 null —— 这一步原先排在
    // 建 row 之前（紧跟着 partsNode），于是旁批那一栏永远建不出来。
    // 重画会换掉 DOM，所以每次重画正文都得来一遍。
    if (!isUser) paintMarks(body, m);
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
    bootMs: 0,     // 环境准备总共花了多少
    pyMs: 0,       // 其中解释器
    packMs: 0,     // 其中依赖
    warmMs: 0,     // 其中绘图预热（Agg + pyplot 首个 import）
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
        // 运行时还没落到本机（首启那 76M 还在后台取）→ **不要建壳**：建了它
        // `indexURL` 是空的，壳永远起不来；而 `shell.el` 一旦占住就不会再建，
        // 真正要跑 Python 时就会一直"运行中…"。这种情况留给懒启动那条原路。
        if (data.ready === false) return;
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

  /**
   * 空闲就先把壳建起来 —— 用户原话："启动以后 pyodide 啊，latex 这些都自动启动准备
   * 好，尽量让用户进入对话的时候无感知。"
   *
   * 这里改的是**时机**，不是机制：壳还是那一个（`shellBoot` 里"建一次常驻"的设计不变），
   * 只是把 `loadPyodide` 那几秒挪到用户读上一条消息的时候，而不是他按下"运行"之后。
   *
   * 三条克制：
   * * 计费 / 2G 网络不预下（那 60M 不该在人没要的时候花他的流量）；
   * * 空闲才做，最多等 5 秒（它比 LaTeX 那份重，让得久一点）；
   * * **运行时没就绪就不建**（`shellBoot` 里看 `data.ready`）—— 建了也起不来。
   */
  function shellWarm() {
    if (shell.el) return;
    var conn = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
    if (conn && (conn.saveData || /(^|-)2g$/.test(conn.effectiveType || ''))) return;
    shellBoot();
  }

  /**
   * 壳报"就绪"时打**一行**日志。为什么要有：面板上不显示壳的内部账（那是壳的事，
   * 不是脚本的输出 —— 见 `qfReady` 那段的注释），但"这次到底省了多少"得有个地方
   * 能看见。一行 `[qf]` 前缀的 info，刷新页面时在控制台能读到。
   */
  function shellLog() {
    var s = shell;
    var secs = function (ms) {
      return ms ? (ms / 1000).toFixed(1) + 's' : '—';
    };
    try {
      window.console.info(
        '[qf] Python 环境已备好：' +
          secs(s.bootMs) +
          '（解释器 ' +
          secs(s.pyMs) +
          ' · 依赖 ' +
          secs(s.packMs) +
          ' · 绘图预热 ' +
          secs(s.warmMs) +
          '）—— 用户第一次运行不必再等'
      );
    } catch (err) {
      /* 打不出来不影响任何事 */
    }
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
      shell.pyMs = Number(data.pyMs || 0);
      shell.packMs = Number(data.packMs || 0);
      shell.warmMs = Number(data.warmMs || 0);
      // 依赖没装上要**说一声**：悄悄降级的代价是"看起来装了、其实 import 失败"
      //（壳里的注释就是这么写的，但这一条以前一直没人显示）。
      if (data.warning) ui.toast(String(data.warning), 'warn');
      shellLog(); // 一行日志：这次环境准备花了多久（省掉的正是这一段）
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

  /* ---- 沙箱面板的展开 / 收起动效 ----------------------------------- */

  /**
   * 展开、收起都要有动效（用户："沙箱的展开和收起要有动效，现在很突兀"）。
   *
   * 原生 `<details>` 是一下子开合的，没有中间态；`::details-content` 那条路
   * 浏览器还太新（仓库里没有先例，不想为一个面板押上兼容性）。所以拦下 summary
   * 的点击，把高度自己演一遍：从"只剩摘要那一条"长到全高（收起时反过来），
   * 演完再把 `open` 定下来、把行内样式清掉 —— **必须清掉**：留着的高度会把这块
   * 钉死，里面的代码块换行、窗口变宽时它就错位了。
   *
   * 系统开了"减少动效"的直接放行，交给浏览器立刻到位。
   */
  var FOLD_MS = 180;

  function foldBox(box, want) {
    if (box.dataset.folding === '1') return; // 上一次还没演完，别叠着来
    var kids = [];
    for (var i = 0; i < box.children.length; i++) {
      if (box.children[i].tagName !== 'SUMMARY') kids.push(box.children[i]);
    }
    if (!kids.length) {
      box.open = want;
      return;
    }
    var sum = box.querySelector('summary');
    var headH = sum ? sum.getBoundingClientRect().height : 0;
    box.dataset.folding = '1';
    box.style.overflow = 'hidden';
    box.style.transition = 'height ' + FOLD_MS + 'ms var(--ease)';
    if (want) {
      box.open = true; // 先开：不开的话量不到里面有多高
      var full = box.scrollHeight;
      box.style.height = headH + 'px';
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          box.style.height = full + 'px';
        });
      });
    } else {
      box.style.height = box.getBoundingClientRect().height + 'px';
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          box.style.height = headH + 'px';
        });
      });
    }
    var settled = false;
    var done = function () {
      if (settled) return;
      settled = true;
      box.style.transition = '';
      box.style.height = '';
      box.style.overflow = '';
      delete box.dataset.folding;
      if (!want) box.open = false;
    };
    box.addEventListener('transitionend', done);
    // 兜底：前后高度恰好一样时不会触发 transitionend，那就没人来收尾了
    window.setTimeout(done, FOLD_MS + 80);
  }

  document.addEventListener(
    'click',
    function (ev) {
      var sum = ev.target && ev.target.closest ? ev.target.closest('summary') : null;
      if (!sum) return;
      var box = sum.parentElement;
      if (!box || box.tagName !== 'DETAILS') return;
      // 只管沙箱那两块（脚本块 / 结果块）：别处的 details 不归这里管
      if (!box.classList.contains('pyrun__codewrap') && !box.classList.contains('pyrun__outwrap')) {
        return;
      }
      if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
      ev.preventDefault(); // 拦下默认的"啪"一下，换成上面的演法
      foldBox(box, !box.open);
    },
    true
  );

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

    // 关掉也要有动效（与打开对称）。这里必须先播完再卸 —— 直接 `state.demo = null`
    // 是整块消失，一帧到位，"突兀"就是这么来的（见 chat.css 那段 .chatdemo 的说明）。
    // 减少动效的人不等这一下：那时候"等 180ms 什么都看不见"比不演更难受。
    var close = function () {
      var back = demoEl.querySelector('.chatdemo__backdrop');
      if (!back || window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
        state.demo = null;
        renderDemoPanel();
        return;
      }
      back.classList.add('is-out');
      window.setTimeout(function () {
        state.demo = null;
        renderDemoPanel();
      }, 200);
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
    // 药丸只有开/关，强度有四档：**关就是关**（否则"药丸关了还在想"最气人），
    // 再打开时回到记着的那一档（缺省 normal）。走 applyThink 是为了把落档**也存下来** ——
    // 只改内存的话，下次开页面会从 localStorage 读回旧档，于是"药丸是灭的、强度还是
    // 使劲"（实测复现过）。
    applyThink(
      state.deepThink ? (state.think && state.think !== 'off' ? state.think : 'normal') : 'off',
      { quiet: true }
    );
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

    // 位置标记：旗（回溯，趁手）/ 丝带（书签，永久）。放在这一行图标的最右端 ——
    // 与复制/重答是同一类"对这条消息做的事"，也都在对话树那张地图上有对应物。
    //
    // **但它要等这一条落库**。两道门都要过：
    //
    //   * `local-N`：用户那条的乐观节点，还没有 id。位置标记存的是 `mid`，此时点下去
    //     会把 `local-3` 这种临时名记进标记里，等落库换了真 id，那枚标记就永远指不回
    //     任何消息（用户："用户发消息，agent 未回复的时候，书签和锚点的图标竟然存在"）。
    //   * `streaming`：**正在长的那一条**。它的 `start` 事件带的就是真 id，只挡 `local-`
    //     挡不住它 —— 实测（伪造的慢流）症状是：回答行一出现就挂着这两枚，等第一个字
    //     到了再消失，也就是"消息一发出去就先闪一下，等到 llm 开始思考才消失"。
    //
    // 落库那一步会重画这一行（`replaceMessage` → `decorateAssistant`），图标自然就出来了。
    if (m.id && String(m.id).indexOf('local-') !== 0 && m.status !== 'streaming') {
      placeButtons(m).forEach(function (btn) {
        foot.appendChild(btn);
      });
    }
    // skill 标记：挂在**哪条消息**上就在哪一行显示（点一下取下）—— 它可能挂在用户那条，
    // 也可能挂在这一条（用 `/` 之后紧接着发的那一句在哪条上，就记在哪条上）。
    skillChips(m).forEach(function (chip) {
      foot.appendChild(chip);
    });

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
    var caret = null;
    //: 每个零件对应的 DOM 节点（与 parts 一一对应）。流式里绝大多数帧**只动最后一块**，
    //: 有了这张对照表就不必整棵重建 —— 一次重建的代价是"所有块重跑 Markdown + KaTeX"，
    //: 而它在流式里要做几十上百次。实测（150 块 / 1.5 万字）：上游 4.6 秒吐完，
    //: 页面还要再花 2 秒才画完，那段尾巴就是这些白干的重建。
    var nodes = [];

    /* ---- 平滑吐字：把「到达」和「显示」拆开 -------------------------------
     *
     * 上游是**一段一段到**的：一个 SSE delta 可能是一整句话，而到达的间隔（几十
     * 到几百毫秒）跟人看的节奏毫无关系。以前是收到就铺上去、靠节流重画，于是
     * 文字"一小段一小段往外蹦"（用户原话："还是一小段一小段出来"）。
     *
     * 现在收到的先进 `queue`，由一个按帧跑的循环匀速往外吐：
     *
     *   * **每帧都动** —— 60Hz 的步进，眼睛看到的就是连续，像水；
     *   * **落后越多吐得越快**（落后量的 1/CATCHUP）—— 所以**永远追得上**：
     *     上游一次吐 500 字，也就多花几帧，不会把整段回答拖在最后
     *     （"攒着最后一次性画完"是老毛病，这次不能换个形式犯回去）；
     *   * 队列吐空就停 —— 不空转（上游可能几百毫秒没动静）。
     *
     * 队列按 `{type, text}` 保序：正文与思考交替到达时，先后顺序不能乱
     *（乱了的话"思考 → 正文"会变成"正文夹在思考中间"）。
     */
    var queue = []; // 还没显示出去的 [{type, text}, …]
    var pending = 0; // queue 里的总字数（省得每帧遍历一遍才知道还剩多少）
    var frame = 0; // 挂着的 requestAnimationFrame id（0 = 没在跑）
    //: 每帧吐掉"落后量"的几分之一。分母越小追得越急。
    var CATCHUP = 4;
    //: 两次重画之间**至少**隔多久。**16ms = 一帧** —— 也就是允许每帧都画。
    //: 实测（会话里那段 1683 字的回答）整块 Markdown 重画只要 **0.1ms**，真正贵的是
    //: 写 DOM 与随之而来的布局；原先这里是 33ms，那是还没量过成本时拍的保守值 ——
    //: 量完发现"每帧画"完全付得起。做这个闸只为"块大到几十毫秒时退开"。
    var PAINT_MIN_MS = 16;
    //: 但**也不能比原来还卡**：固定 100ms 是改之前的值，自适应选出来的间隔不许超它。
    var PAINT_MAX_MS = 100;
    var lastPaintMs = 0; // 上一次重画实际花了多久（自适应就靠这个数）
    var lastPaintAt = 0; // 上一次重画的时刻（与 `paintGap()` 一起决定这一帧画不画）
    // ---- 尾巴渐隐（样式见 chat.css 的 `.chatmsg__flowing`）----------------
    var flowHost = null; // 现在挂着渐隐的那个元素
    var flowTail = 0; // 当前带子宽度（px，越宽尾巴越软）
    var flowWant = 0; // 目标宽度
    var flowIdleAt = 0; // 最后一次吐字的时刻
    //: 盖住多高（px）。**别贪大**：这一层盖在正文最后一行上，盖住半行就成了
    //: "整行一洗一清"，那比原先的"几个字蹦出来"还难看。
    //: 12px ≈ 14.5px 字号那一行里、基线以下到字身下缘的那一点。
    var FLOW_TAIL_PX = 12;
    //: 停手多久就把带子收回去（毫秒）。220 是"比一次眨眼短、但确实算停了"：
    //: 远大于一帧（不会每帧开合着闪），又短到模型一停下来雾就散。
    var FLOW_HOLD_MS = 220;
    //: 要不要缓动。`prefers-reduced-motion` 下直接到位 —— 全库同一条规矩。
    var FLOW_EASE = !(
      window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches
    );

    /**
     * 把 `parts` 画到 DOM 上。**什么时候画由调用方决定**（见 `tick` 里那道时间闸）。
     *
     * 这里**没有自己的计时器** —— 那是踩出来的：流式那条路每秒要画几十次，而
     * *计时器与帧不对齐*会白白丢掉帧。实测（无头 Chromium，rAF 约 43fps）：
     * `setTimeout(33ms)` 落在两帧中间，于是退化成"每两帧画一次"，只有 20 次/秒。
     * 所以"画"这个动作放进 rAF 回调里（`tick` 本来就是），要不要画由
     * `paintGap()` 那个时间闸判断 —— 一个时钟、一道闸，不再有第二种节奏。
     */
    function render() {
      var stick = nearBottom();
      // 量这次重画花了多久 —— 下一次隔多久就按它定（见 `paintGap`）
      var startedAt = nowMs();
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
      // 尾巴渐隐跟着最后一块正文走（渐隐本身由 `tick` 撑开，这里只管它在哪一块上）
      moveFlow();
      // 光标钉在末尾：节点**复用**（原先每帧新建一个，会闪）
      if (state.busy) {
        if (!caret) caret = h('span.chat__caret');
        body.appendChild(caret);
      } else {
        caret = null;
      }
      if (stick) scrollToEnd(false);
      lastPaintMs = nowMs() - startedAt;
    }

    /** 现在几点（毫秒）。用 `performance.now` 而不是 `Date.now`：单次重画常常只有
     * 几毫秒，1ms 分辨率的时钟量出来一片 0，自适应就成了瞎调。 */
    function nowMs() {
      return window.performance && window.performance.now
        ? window.performance.now()
        : Date.now();
    }

    /** 下一次重画隔多久：**跟着上一次实际花的时间走**。
     *
     * 重画是整块 Markdown 重跑（含 KaTeX），成本随块长与公式数涨。固定 100ms 两头
     * 不讨好：小块明明可以更顺，长块却可能一次就画掉几十毫秒。这里按"画一次最多
     * 占一半时间"定下一帧的间隔，夹在 `[PAINT_MIN_MS, PAINT_MAX_MS]` 之间 ——
     * 自适应，但**不许比改之前的 100ms 更卡**。
     */
    function paintGap() {
      if (!lastPaintMs) return PAINT_MIN_MS;
      return Math.max(PAINT_MIN_MS, Math.min(PAINT_MAX_MS, lastPaintMs * 2));
    }

    /** 正在流的那一块里、**装正文的那个元素** —— 尾巴渐隐打在它身上。
     *
     * 为什么不打在最外层：思考块的最外层是 `<details>`，上面还压着 `<summary>`
     *（"思考过程"那四个字），一起罩住的话连标题都会跟着忽明忽暗。
     *
     * 也只在最后一块是 text / think 时才算数 —— 工具卡、题卡是**零件**，
     * 它们不该有"正在流出来"的尾巴。
     */
    function streamHost() {
      var last = parts.length ? parts[parts.length - 1] : null;
      if (!last || (last.type !== 'text' && last.type !== 'think')) return null;
      var node = nodes[parts.length - 1];
      if (!node) return null;
      if (last.type === 'think') return node.querySelector('.md');
      return node.classList && node.classList.contains('md') ? node : null;
    }

    /** 把渐隐挪到当前该在的那一块上。 */
    function moveFlow() {
      var host = streamHost();
      if (host === flowHost) return;
      // 换块了就把旧的摘干净 —— 否则每插一个零件，前面那些块会各留一道糊着的底边
      if (flowHost) {
        flowHost.classList.remove('chatmsg__flowing');
        flowHost.style.removeProperty('--qf-tail');
      }
      flowHost = host;
      if (host) {
        host.classList.add('chatmsg__flowing');
        host.style.setProperty('--qf-tail', flowTail.toFixed(1) + 'px');
      }
    }

    /** 带子走一步缓动，返回"还在动吗" —— 动就得继续挂帧（收一半停下会僵住）。 */
    function stepFlow() {
      flowWant = flowIdleAt && nowMs() - flowIdleAt < FLOW_HOLD_MS ? FLOW_TAIL_PX : 0;
      var gap = flowWant - flowTail;
      if (Math.abs(gap) < 0.4) {
        // 到位了：**把最后那一点补上之后就不再写样式** —— 每帧写一次样式，
        // 那一层就会每帧重画一次遮罩，而画面在几百分之一像素上没有区别。
        if (flowTail !== flowWant) {
          flowTail = flowWant;
          if (flowHost) flowHost.style.setProperty('--qf-tail', flowTail.toFixed(1) + 'px');
        }
        return false;
      }
      flowTail = FLOW_EASE ? flowTail + gap * 0.22 : flowWant;
      if (flowHost) flowHost.style.setProperty('--qf-tail', flowTail.toFixed(1) + 'px');
      return true;
    }

    /** 收到一段文字：**不直接显示**，先进队列（理由见上面那段注释）。 */
    function enqueue(type, text) {
      if (!text) return;
      var last = queue.length ? queue[queue.length - 1] : null;
      if (last && last.type === type) last.text += text;
      else queue.push({ type: type, text: text });
      pending += text.length;
      if (!frame) frame = window.requestAnimationFrame(tick);
    }

    /** 按帧往外吐。 */
    function tick() {
      frame = 0;
      // 落后越多吐得越快：这样上游一次给一大段，也只是多花几帧就追平，
      // 不会让"显示"落在"生成"后面越欠越多（那正是要避免的"攒到最后一次性画"）。
      var step = 1 + Math.floor(pending / CATCHUP);
      var revealed = false;
      while (step > 0 && queue.length) {
        var head = queue[0];
        var take = Math.min(step, head.text.length);
        pushPart(parts, head.type, head.text.slice(0, take));
        head.text = head.text.slice(take);
        pending -= take;
        step -= take;
        if (!head.text) queue.shift();
        revealed = true;
      }
      // 有字刚出来 → 尾巴软一下；停手超过 `FLOW_HOLD_MS` 就自己收回去
      //（模型在"想"的时候，那几个字不该一直糊着）。
      // **记在这里而不是 `render` 里**：重画会被时间闸跳过，
      // 而"刚才到底有没有新字"只有吐字这一侧知道。
      if (revealed) flowIdleAt = nowMs();

      // **就在这一帧里画**（已经在 rAF 回调里了，不必也不该再排计时器）。
      // 要不要画由 `paintGap()` 那道闸定：渲染便宜时就是每帧一画（60 次/秒），
      // 块大到一次几十毫秒时它会自己退开 —— 但**不许比改之前的 100ms 更卡**。
      // 没吐出新字就**不画**：队列吐空之后还会为"收带子"多跑十几帧，
      // 那些帧上 DOM 一个字都没变，重画纯属白干。
      if (revealed) {
        var now = nowMs();
        if (now - lastPaintAt >= paintGap()) {
          render();
          lastPaintAt = now;
        }
      }
      var flowing = stepFlow();
      // **这里不滚**：改变布局的是重画，不是吐字 —— 而重画之后 `render` 自己会滚。
      // 在这儿再滚一次等于每帧多摸一次 `scrollHeight`（强制布局），
      // 而它带来的可见差别是零（DOM 还没变，滚了也一样）。原先 `pushText` 里
      // 那个"合并到下一帧"的滚动就是为此存在的，现在随重画走，不需要了。
      //
      // **"还开着"和"还在收"都算**，两个都要，少一个就出上面那种僵住：
      //   * 只看队列 —— 最后一个字吐完那帧带子已经到位（`flowing` 是 false），
      //     帧就停了，220ms 后没有任何一帧来收它；
      //   * 只看 `flowing` —— 同样漏掉"还没到期"的那 220ms。
      var cooling = flowIdleAt && nowMs() - flowIdleAt < FLOW_HOLD_MS;
      if (queue.length || cooling || flowing) {
        frame = window.requestAnimationFrame(tick);
      }
    }

    /** 把队列里剩下的**一次吐完**。
     *
     * 收尾路径必须调它：`settle` 之后 `parts` 可能被服务端那份整个替换（它才是
     * 权威），而**那条路不一定带着 `parts`** —— 不先把队列补完，最后那一截
     * 会凭空少掉（少的是回答的结尾，最难被发现的那种丢内容）。
     */
    function flush() {
      if (frame) {
        window.cancelAnimationFrame(frame);
        frame = 0;
      }
      // 带子直接收到位：`settle` 马上会把整棵 DOM 重建（新节点上没有渐隐），
      // 再让帧循环多跑十几帧去收一个已经不在页面上的元素，纯属白干。
      flowTail = 0;
      flowWant = 0;
      flowIdleAt = 0;
      while (queue.length) {
        var head = queue.shift();
        pending -= head.text.length;
        pushPart(parts, head.type, head.text);
      }
      pending = 0;
    }

    return {
      row: row,
      body: body,
      msg: msg,
      parts: function () {
        // 调它的都是**收尾路径**（错误 / 中止，见 `send`）—— 先把没吐完的补上，
        // 否则交给上层的是一份少了结尾的 parts（错误提示会长在半句话后面）。
        flush();
        return parts;
      },
      pushText: function (chunk) {
        // **不直接显示**：进队列，由 `tick` 按帧匀速吐出来（见上面那段注释）。
        // 滚动也跟着"显示"走（在 `tick` 里），不再跟着"到达"走 —— 否则一大段
        // 一次到达时，会先滚到底、再慢慢把内容吐出来，看着像页面在往下跳。
        enqueue('text', chunk);
      },
      pushThink: function (chunk) {
        enqueue('think', chunk);
      },
      pushNote: function (text) {
        parts.push({ type: 'summary', text: text });
        render();
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
        render();
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
        render();
      },
      // 卡片一到就立刻画：用户最想马上做的就是动手答
      pushCard: function (card) {
        parts.push({ type: 'card', kind: 'question', payload: card });
        render();
      },
      // 凭条也一样：它得在回答说完之前就能点（不然用户干等）
      pushAction: function (proposal) {
        parts.push({ type: 'action', kind: proposal.kind, payload: proposal });
        render();
      },
      pushPart: function (part) {
        parts.push(part);
        render();
      },
      settle: function (m) {
        // **先把队列补完**：`parts` 接下来可能被服务端那份整个替换（它才是权威），
        // 但那条路不一定带着 `parts` —— 不补的话，还没吐出来的那一截会凭空少掉。
        flush();
        if (m && m.parts && m.parts.length) parts = m.parts;
        // 收尾了：下面整棵重建，不用再等吐字循环 —— `flush()` 已经把它停掉
        //（它顺手 cancelAnimationFrame），不会再有帧往这份 parts 上追加。
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
        flush(); // 同 `parts()`：收尾路径上不能漏掉还没吐出来的那截
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

      // 他刚用 `/` 挑的那个流程：**在发请求之前**记下来，所以**这一轮就带上**。
      // 记在**这条链的末端**（也就是新消息将要挂的那个父节点）—— 服务端是
      // "沿链取最近的那条记录"，从新消息往下都算挂着，这一轮自然也在内
      //（见 `app/skills.py` 的 `resolve`）。链末端为空（这条对话还是空的）= 记
      // `mid` 为空的那条，含义就是"整条对话"。
      if (pendingSkill) {
        QF.store.attachSkill({
          cid: state.current || '',
          mid: attachTo == null ? '' : String(attachTo),
          key: pendingSkill,
        });
        pendingSkill = '';
        paintSkillPick();
        // 那枚标记长在**那条消息**上，而它此刻已经在屏幕上了 —— 不重画就看不到，
        // 于是"装上了没有"要靠刷新页面才知道（第一版就是这样）。
        repaintKeepingScroll();
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
      // 思考强度：带上**等级**（`off`/`low`/`normal`/`high`，见 `gateway.thinking_params`）。
      // 药丸那颗（开/关）与它对齐：`off` 就是关，其余都是开。
      state.think =
        state.think ||
        (function () {
          try {
            return window.localStorage.getItem('qf.chat.think') || '';
          } catch (err) {
            return '';
          }
        })() ||
        (state.deepThink ? 'normal' : 'off');
      body.thinking = state.think;
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
              // **回答到了**：用户那一条的标记图标该出来了（它们是"有回答才给"的，
              // 见 messageRow 里那段）。只补这两颗，不重画整行 —— 重画会把刚长出来的
              // 回答旁边的滚动位置也一起动掉。
              if (m && m.parentId != null) {
                var asked = state.messages.filter(function (one) {
                  return String(one.id) === String(m.parentId);
                })[0];
                var prow = threadEl.querySelector('[data-id="' + m.parentId + '"]');
                var pacts = prow ? prow.querySelector('.chatmsg__useractions') : null;
                if (asked && pacts && !pacts.querySelector('.chatplacebtn')) {
                  placeButtons(asked).forEach(function (btn) {
                    pacts.appendChild(btn);
                  });
                }
              }
              // **新回答一出现，就把它记成"这一层选的那一条"**。
              //
              // 重新回答时尤其关键：`regenerate` 先把这一层的选择清掉了，不在这里
              // 补回来，收尾的 `paintThread()` 会按"第一个孩子"重画 —— 界面上就是
              // "新回答闪一下，又变回旧回答"（用户："要在新的内容出现之后旧的才消失"）。
              if (m && m.parentId) state.picks[keyOf(m.parentId)] = m.id;
              var row = messageRow(m);
              /**
               * **旧回答让位**（用户："重新生成 agent 回复的时候，旧的回复要等到新的
               * 回复生成完毕才会消失"）。
               *
               * 这里原来是"旧的留在原处、新的 append 到下面，等流结束才收成一条线" ——
               * 那是照上一轮的话（"要在新的内容出现之后旧的才消失"）做的，但它做过头了：
               * 一次重新生成要等十几秒，这十几秒里屏幕上**并排两条回答**（新的还在长），
               * 哪条算数看不出来。
               *
               * 现在：新的那条**一出现就在原地顶掉旧的**（旧的仍在 `‹ ›` 里，随时切回来），
               * 顺带把旧回答往下挂的那一段（它的后代行）一起收走 —— 它们属于旧分支。
               */
              var was = m && m.parentId != null ? shownReplyRow(m.parentId) : null;
              if (was) {
                var host = was.parentNode;
                host.replaceChild(row, was);
                var next = row.nextElementSibling;
                while (next) {
                  var after = next.nextElementSibling;
                  if (!next.classList || !next.classList.contains('chatmsg')) break;
                  next.remove();
                  next = after;
                }
              } else {
                threadEl.appendChild(row);
              }
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
          // 这一轮尝试结束了，`state.replacing` 的使命到此为止 —— **必须赶在下面那次
          // 重画之前清掉**：那条被顶掉的报错回不回来，就看重画时它还挡不挡路。
          //   * 替代品已经在链上（生成成功，或者又失败了一条**新的**）→ 它照旧不露面；
          //   * 压根没生成出来（连接就没建起来那类）→ 它连着重试按钮一起回来，
          //     否则下一次失败用户就没地方点了。
          state.replacing = null;
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
    // 但**失败/中断的那条，按下重试就从屏幕上撤掉**。
    //
    // 收尾时那次重画本来就会把它收走（见 `send()` 末尾那句 `paintThread()`，
    // "旧那条退到切换器后面去"）—— 这里只是把那一刻**从"新回答生成完"提前到
    // "按下重试"**。中间那段等待是十几秒，而失败那条只有一行报错，留着纯是噪音。
    //
    // 成功的那条**不撤**：它还有内容可读，一边读旧的、一边等新的，比空着强。
    //
    // 分支没丢：树里还在（`is-error` 那个节点），切进切换器照样能翻到它
    //（`pickBranch` / `revealMessage` 是明确的选择，优先级高于 `state.replacing`）。
    if (m.status && m.status !== 'ok') {
      state.replacing = m;
      var row = threadEl ? threadEl.querySelector('[data-id="' + m.id + '"]') : null;
      if (row && row.parentNode) row.parentNode.removeChild(row);
    }
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

  /* ------------------------------------------------------------ 流程（skill）
   *
   * `docs/对话树.md` §十二 的三种形态里，**流程**做成 `/` 命令 —— 它是动作：有开始、
   * 有走完，而且**讲的人是用户**。口吻 / 纪律是状态（装 / 取、挂在节点上、沿树继承），
   * 要的是另一套机制，留给下一步。
   *
   * 界面只干一件事：把他挑的那条写进记录（`QF.store.attachSkill`，存法与记号同源）。
   * "现在挂着什么"由**服务端**按当下的链算（`app/skills.py` 的 `resolve`）——
   * 链会随切分支/编辑重发而变，本地算一份必然过期。
   */
  var pendingSkill = '';   // 这次发出去时要挂上的那条（从挑中到发送之间）
  var skillPickEl = null;  // 输入框上方那份清单
  var skillPickAt = 0;     // 清单里高亮到第几条（键盘上下走）

  //: 形态的中文（`app/skills.py` 的 kind → 界面上那两个字）。只用来**标出来给他看**：
  //: "我装的到底是流程还是口吻"这件事，界面不说清，他就得靠猜。
  var SKILL_KIND_WORD = { flow: '流程', tone: '口吻', discipline: '纪律' };

  //: 形态各自的小图标（药丸上跟在形态那两个字前面）。图标本身在 `theme/runtime/ui.js`
  //: 的图标表里，与「深度思考」那颗原子同一路描边、同一个尺寸档。
  var SKILL_KIND_ICON = { flow: 'steps', tone: 'voice', discipline: 'ban' };

  function skillCatalog() {
    var st = (QF.mounts && QF.mounts.state) || {};
    return Array.isArray(st.skills) ? st.skills : [];
  }

  /** 冒号这一层：`/` 是**命令**，不只有 skill（用户："`/` 是命令，不只有 skill"）。
   *
   * 现在只登记了一条命令 `skill` —— 别的命令**先不设计**（用户："命令先不要设计"），
   * 所以这张表就一条：它下面挂着他那个文件夹里的十来条 skill。 */
  // 只有 skill 一条了：思考强度**不再放命令里**（用户："那个 thinking 强度的调整，
  // 还是算了不要放在 command 里吧。做成右键「深度思考」那个药丸跳出来的菜单"）。
  // 它本来就与那颗药丸是同一件事的两面 —— 药丸管"带不带思考"，菜单管"带多强"。
  var COMMANDS = [{ key: 'skill', label: 'skill', hint: '装一个 skill：流程 / 口吻 / 纪律' }];

  //: 四档的说明。**口径要和后端那几句提示词对得上**（见 `chat.py` 的 `_THINKING_LINES`）——
  //: 这里写"遇到分叉才多想一步"，后端就得是同一句话；两边不一致，他选的东西就不是他以为的。
  var THINK_LEVELS = [
    { key: 'off', label: '不想', hint: '直接给答案，也不展开推理（简单问题用它）' },
    { key: 'low', label: '省着点', hint: '先给答案，遇到分叉才多想一步' },
    { key: 'normal', label: '常规', hint: '默认：该怎么想怎么想' },
    { key: 'high', label: '使劲', hint: '前提、边界、反例都过一遍' },
  ];

  function _skillMatch(one, word) {
    if (!word) return true;
    return (
      String(one.key || '').toLowerCase().indexOf(word) === 0 ||
      String(one.label || '').indexOf(word) === 0 ||
      String(one.hint || '').toLowerCase().indexOf(word) >= 0
    );
  }

  /** 输入框里已经写了哪条命令（空 = 还在第一层，或者写的是一个 skill 名）。 */
  function pickCommand() {
    var text = String((inputEl && inputEl.value) || '');
    var hit = /^\/\s*([a-z][a-z0-9-]*)\b/i.exec(text);
    if (!hit) return '';
    var key = hit[1].toLowerCase();
    return COMMANDS.some(function (one) {
      return one.key === key;
    })
      ? key
      : '';
  }

  /** 清单里要显示什么。命令与 skill 混在同一层里，各带各的标签 ——
   *  两条路都留着：`/skill` 是正式入口，直接打 `/费` 也认（那个名字够具体，
   *  没必要逼他先选一次命令）。 */
  function pickItems() {
    if (!inputEl) return [];
    var text = String(inputEl.value || '');
    if (text.charAt(0) !== '/') return [];
    var typed = text.slice(1);
    var word = typed.trim().toLowerCase();
    // 第二层：每条命令有自己的子项（现在只有 `skill` → 那一堆）
    var cmd = pickCommand();
    if (cmd === 'skill') {
      var rest = word.replace(/^\/?\s*skill\b/, '').trim();
      return skillCatalog()
        .filter(function (one) {
          return _skillMatch(one, rest);
        })
        .map(function (one) {
          return { type: 'skill', one: one };
        });
    }
    var out = COMMANDS.filter(function (one) {
      // 认 key 也认中文名（他打 `/思`，想要的显然是「思考」那条命令）
      return !word || one.key.indexOf(word) === 0 || one.label.indexOf(word) >= 0;
    }).map(function (one) {
      return { type: 'command', one: one };
    });
    if (word) {
      // 直接打 skill 名：与命令并排显示（他打 `/费`，想要的显然是费曼）
      skillCatalog()
        .filter(function (one) {
          return _skillMatch(one, word);
        })
        .forEach(function (one) {
          out.push({ type: 'skill', one: one });
        });
    }
    return out;
  }

  function paintSkillPick() {
    if (!skillPickEl) return;
    var list = pickItems();
    ui.clear(skillPickEl);
    if (!list.length) {
      skillPickEl.classList.remove('is-open');
      return;
    }
    if (skillPickAt >= list.length) skillPickAt = 0;
    list.forEach(function (item, index) {
      var one = item.one;
      var isCmd = item.type === 'command';
      skillPickEl.appendChild(
        h(
          'button.chatskill__item' + (index === skillPickAt ? '.is-on' : ''),
          {
            type: 'button',
            onClick: function () {
              chooseItem(item);
            },
          },
          h('span.chatskill__kind' + (isCmd ? '.is-cmd' : ''), {
            text: isCmd ? '命令' : SKILL_KIND_WORD[one.kind] || '',
          }),
          // 名字**不带斜杠**（用户："command 的选项，不要带 / 了"）：
          // 斜杠是"我在输入框里打什么"，不是这条东西的名字。
          h('span.chatskill__name', { text: one.label }),
          h('span.chatskill__hint', { text: one.hint })
        )
      );
    });
    skillPickEl.classList.add('is-open');
  }

  /**
   * 思考强度的拖动条 —— 右键那颗「深度思考」药丸弹出来（见 openThinkMenu）。
   *
   * 为什么是拖动条：这四档是**一路上坡**的一条线（不想 → 省着点 → 常规 → 使劲），
   * 摆成列表就像四个互不相干的开关。为什么搬到这里：它跟那颗药丸本来就是同一件事
   * 的两面 —— 药丸管"这一轮带不带思考"，这里管"带多强"。
   */
  function thinkSliderNode() {
    var index = 0;
    var current = state.think || (state.deepThink ? 'normal' : 'off');
    THINK_LEVELS.forEach(function (one, i) {
      if (one.key === current) index = i;
    });
    var now = h('span.chatthink__now', { text: THINK_LEVELS[index].label });
    var bar = h('input.chatthink__bar', {
      type: 'range',
      min: '0',
      max: String(THINK_LEVELS.length - 1),
      step: '1',
      value: String(index),
      'aria-label': '思考强度',
      onInput: function (ev) {
        // 拖动时**实时**生效（还没松手，药丸就跟着走了）……
        var one = THINK_LEVELS[Number(ev.target.value)] || THINK_LEVELS[0];
        applyThink(one.key, { quiet: true });
        now.textContent = one.label;
        now.parentNode.setAttribute('title', one.hint);
        paintFill(ev.target.value);
      },
      onChange: function (ev) {
        // ……松手才算"定下来"：提示一句，菜单收起来
        var one = THINK_LEVELS[Number(ev.target.value)] || THINK_LEVELS[0];
        applyThink(one.key);
        closeChatMenu();
      },
      onKeyDown: function (ev) {
        if (ev.key === 'Escape') {
          ev.stopPropagation();
          closeChatMenu();
        }
      },
    });
    // 已选那一段得自己画（Chromium 的 range 不会），进度写进 CSS 变量交给渐变
    var paintFill = function (value) {
      bar.style.setProperty('--fill', (Number(value) / (THINK_LEVELS.length - 1)) * 100 + '%');
    };
    paintFill(index);
    window.setTimeout(function () {
      if (bar.focus) bar.focus();
    }, 0);
    return h(
      'div.chatthink',
      {},
      h('div.chatthink__row', { title: THINK_LEVELS[index].hint }, now),
      bar,
      h(
        'div.chatthink__scale',
        {},
        THINK_LEVELS.map(function (one) {
          return h('span.chatthink__tick', { text: one.label });
        })
      )
    );
  }

  /** 右键「深度思考」那颗药丸：弹出强度菜单（一根拖动条）。 */
  function openThinkMenu(ev) {
    if (ev) {
      ev.preventDefault();
      chatMenuX = ev.clientX;
      chatMenuY = ev.clientY;
    }
    showChatMenu([{ node: thinkSliderNode() }]);
  }

  /**
   * 落档：记在本地（与那颗「深度思考」药丸同一个存法），顺手把药丸对齐 ——
   * 药丸只有开/关，`off` 之外都是"开"；不对齐就会出现"选了不想、药丸还亮着"，
   * 那是同一个状态的两种显示，不一致比不做还坏。
   */
  function applyThink(key, options) {
    var one = null;
    THINK_LEVELS.forEach(function (each) {
      if (each.key === key) one = each;
    });
    if (!one) return;
    state.think = one.key;
    state.deepThink = one.key !== 'off';
    try {
      window.localStorage.setItem('qf.chat.think', one.key);
    } catch (err) {
      /* 存不下只影响下次开页面，不该拦住这一次 */
    }
    var deepPill = document.querySelector('.chat__deep:not(.chatskill__open)');
    if (deepPill) {
      deepPill.classList.toggle('is-on', !!state.deepThink);
      deepPill.setAttribute('aria-pressed', state.deepThink ? 'true' : 'false');
    }
    updateComposer();
    if (!options || !options.quiet) {
      ui.toast('思考强度：' + one.label + ' —— ' + one.hint, 'info', 2600);
    }
  }

  function chooseItem(item) {
    if (item.type === 'command') {
      // 进第二层：把 `skill` 写进输入框（`/skill` 这三个字本身就是他的输入）
      inputEl.value = '/' + item.one.key + ' ';
      growInput();
      skillPickAt = 0;
      paintSkillPick();
      return;
    }
    chooseSkill(item.one);
  }

  /** 挑中一条：记在 `pendingSkill` 上，把输入框里那截 `/xxx` 撤掉（等他接着提问）。 */
  function chooseSkill(one) {
    pendingSkill = one.key;
    inputEl.value = '';
    growInput();
    skillPickAt = 0;
    paintSkillPick();
    inputEl.focus();
    ui.toast('/' + one.label + ' 已装上 —— ' + one.hint, 'info', 2600);
  }

  /** 这条链上现在正生效的（与 `app/skills.py` 的 `resolve` 同一套语义：沿链取最近、
   *  每个 key 各算各的）。**只用来画那一排药丸** —— 真正生效由服务端按当下的链算。 */
  function activeSkills() {
    var cid = String(state.current || '');
    var records = QF.store.skills() || [];
    var mids = [''].concat((activePath() || []).map(function (m) {
      return String(m.id);
    }));
    var decided = {};
    mids.forEach(function (mid) {
      records.forEach(function (one) {
        if (String(one.cid || '') !== cid) return;
        if (String(one.mid || '') !== mid) return;
        decided[one.key] = one.on !== false;
      });
    });
    return Object.keys(decided)
      .filter(function (k) {
        return decided[k];
      })
      .map(function (k) {
        return (
          skillCatalog().filter(function (one) {
            return one.key === k;
          })[0] || { key: k, label: k, kind: '', hint: '' }
        );
      });
  }

  /** 输入区里与 skill 有关的两件东西（用户："常驻药丸要做，这个是状态的指示和取消药丸。
   *  skill 另外做一个药丸放在模式旁边。"）：
   *
   *   * **`skill` 药丸**：紧挨着模式药丸，点一下把 `/skill` 填进输入框（= 打开清单）；
   *   * **状态药丸**：这条链上正生效的每一条一枚，各带 × —— 既是"现在挂着什么"的指示，
   *     也是**取下**的地方（不必再滚回那条消息去点它）。
   *
   * 都动态插进补药丸那一行（`.chat__tools`）：那一行是别处建的，这里只往里插；
   * 找不到就什么都不做 —— 布局变了顶多少两颗药丸，不该把输入区弄崩。
   */
  function paintSkillPills() {
    var tools = document.querySelector('.chat__tools');
    if (!tools) return;

    var btn = tools.querySelector('.chatskill__open');
    if (!btn) {
      btn = h(
        'button.chat__deep.chatskill__open',
        {
          type: 'button',
          title: '装一个 skill（也可以直接在输入框里打 /）',
          onClick: function () {
            inputEl.value = '/skill ';
            growInput();
            skillPickAt = 0;
            paintSkillPick();
            inputEl.focus();
          },
        },
        // 与「深度思考」那一颗**同一套**：图标 + 文字，同一个尺寸档、同一路描边
        // （用户："那个 skill 药丸，同样的，图标+文字，对齐。"）
        iconNode('skill', 14),
        h('span.chat__deeptext', { text: 'skill' })
      );
      var modes = tools.querySelector('.chat__modes');
      if (modes && modes.parentNode) modes.parentNode.insertBefore(btn, modes.nextSibling);
      else tools.appendChild(btn);
    }

    var bar = tools.querySelector('.chatskill__states');
    if (!bar) {
      bar = h('div.chatskill__states');
      tools.appendChild(bar);
    }
    ui.clear(bar);
    var live = activeSkills();
    live.forEach(function (one) {
      bar.appendChild(
        h(
          'button.chatskill__state',
          {
            type: 'button',
            title: '正在生效：「/' + one.label + '」—— 点 × 取下',
            onClick: function () {
              var path = activePath() || [];
              var leaf = path.length ? path[path.length - 1] : null;
              QF.store.attachSkill({
                cid: state.current || '',
                mid: leaf ? String(leaf.id) : '',
                key: one.key,
                on: false,
              });
              paintSkillPills();
              repaintKeepingScroll();
              ui.toast('已取下 /' + one.label, 'info', 1600);
            },
          },
          // 状态药丸也是图标 + 文字：图标说明**哪一类**（流程/口吻/纪律），
          // 文字说明是哪一条 —— 一排药丸里靠这个一眼分得开。
          iconNode(SKILL_KIND_ICON[one.kind] || 'skill', 12),
          h('span.chatskill__statetext', {
            text: (SKILL_KIND_WORD[one.kind] || '') + ' ' + one.label,
          }),
          h('span.chatskill__x', { text: '×' })
        )
      );
    });
    bar.classList.toggle('is-on', live.length > 0);
  }

  /** 这条消息上挂着的标记（点一下 = 取下 / 再装回去）。没有就返回 []。
   *
   * **返回的是一串**：三种形态可以并存（用户："流程/口吻/纪律实际上是可以兼容的"），
   * 同一条消息上完全可能同时挂着"费曼"和"大白话"。 */
  function skillChips(m) {
    return (QF.store.skillsOn(m && m.id) || []).map(function (live) {
      var on = live.on !== false;
      var one = skillCatalog().filter(function (each) {
        return each.key === live.key;
      })[0];
      var label = one ? one.label : live.key;
      var kind = one && one.kind ? SKILL_KIND_WORD[one.kind] || '' : '';
      var btn = h(
        'button.chatskill__chip' + (on ? '.is-on' : ''),
        { type: 'button', dataset: { skill: live.key } },
        (on ? kind + ' · ' : '已取下 · ') + label
      );
      var paint = function (now) {
        btn.classList.toggle('is-on', now);
        btn.textContent = (now ? kind + ' · ' : '已取下 · ') + label;
        btn.title = now
          ? '正在生效：「/' + label + '」—— 点一下取下'
          : '这一条已经取下了（点一下再装回去）';
      };
      paint(on);
      btn.addEventListener('click', function () {
        var next = !btn.classList.contains('is-on');
        QF.store.attachSkill({
          cid: state.current || '',
          mid: m.id == null ? '' : String(m.id),
          key: live.key,
          on: next,
        });
        // **就地改这一枚**，不整屏重画：位置标记那边重画是对的（它改的是正文），
        // 而这里改的只是这一枚的亮灭 —— 整屏重画会在某些路径上把标记反复重建
        //（实测：点一下炸出几百枚同样的标记）。
        paint(next);
        if (notesOpen) renderNotes();
        ui.toast((next ? '又装上了 /' : '已取下 /') + label, 'info', 1600);
      });
      return btn;
    });
  }

  function onKeydown(event) {
    // 清单开着时，键盘先归它：上下走、回车挑中、Esc 收起（都**不**发送）
    var list = pickItems();
    if (list.length) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        skillPickAt = (skillPickAt + (event.key === 'ArrowDown' ? 1 : list.length - 1)) % list.length;
        paintSkillPick();
        return;
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        inputEl.value = '';
        growInput();
        paintSkillPick();
        return;
      }
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        chooseItem(list[skillPickAt] || list[0]);
        return;
      }
    }
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
        state.replacing = null; // 换会话了，上一轮"被顶掉的那条"与这里无关
        state.treeFolded = {}; // 收起状态也是按会话算的，换个对话就不该还收着
        resetTreeView(); // 换了会话就是另一张图，下次打开重新适配
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

  /** 当前这条会话在列表里的那一条（分享要标题，列表里没有就现造一个薄的）。 */
  function currentConv() {
    var found = null;
    state.list.forEach(function (one) {
      if (one && one.id === state.current) found = one;
    });
    if (found) return found;
    return state.current ? { id: state.current, title: '' } : null;
  }

  /**
   * 分享当前会话：服务端装配成**一个自带数据的网页**，浏览器存成文件。
   *
   * 为什么是"存一个文件"而不是"复制一个链接"：那一页是**自包含**的 ——
   * 没有后端可指，装配好的文件本身就是那份内容。发出去对方双击即可，
   * 不需要装任何东西（装配见 `app/share.py`，那里也写着它跟"离线形态已淘汰"
   * 那条决定的关系：否掉的是第二种应用形态，不是内联本身）。
   *
   * **为什么走 `fetch` 而不是直接 `<a download>`**：装配在服务端做，
   * 失败时返回的是**一条 JSON 报错**，而 `<a download>` 会把它当成文件存下来 ——
   * 用户拿到一个叫 `chat-xxxx.html` 的报错文件，还会以为是自己那边的问题
   *（装配自检的失败就是这种：见 `app/share.py` 的 `ShareBuildError`）。
   * 先看一眼状态码，错了当面说。
   *
   * 不做"正在打包"那套：实测 158 条消息装配 **37ms**，塞一个转圈只会闪一下。
   */
  function onShare() {
    var conv = currentConv();
    if (!conv) return;
    if (!state.messages.length) {
      ui.toast('这条对话还没有内容，没什么可分享的', 'warn');
      return;
    }
    var name = 'chat-' + String(conv.id).slice(0, 8) + '.html';
    fetch(QF.api.base + '/chat/conversations/' + conv.id + '/export?format=html', {
      credentials: 'same-origin',
    })
      .then(function (res) {
        if (res.ok) return res.blob();
        return res
          .json()
          .catch(function () {
            return {};
          })
          .then(function (data) {
            throw new Error((data && data.detail) || '导出失败（' + res.status + '）');
          });
      })
      .then(function (blob) {
        var url = URL.createObjectURL(blob);
        var link = h('a', { href: url, download: name });
        document.body.appendChild(link);
        link.click();
        link.remove();
        // 立刻回收会让某些浏览器来不及取那份 blob，延后一拍
        window.setTimeout(function () {
          URL.revokeObjectURL(url);
        }, 1000);
        ui.toast('分享页已生成：' + name + '（' + (blob.size / 1048576).toFixed(1) + ' MB，直接发出去即可）');
      })
      .catch(function (err) {
        ui.toast('没能生成分享页：' + ((err && err.message) || '未知原因'), 'warn');
      });
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
              download('/api/chat/conversations/' + conv.id + '/export?format=html');
            },
          },
          'HTML（单页分享，能直接发出去）'
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

  /* 启动预热：空闲（或最多 5 秒后）先把 Python 壳建起来。与 latex.js 那份同源 ——
   * 这里只管"什么时候"，"要不要"由 shellWarm 自己判（网络、就绪、重复调用）。 */
  if (typeof window.requestIdleCallback === 'function') {
    window.requestIdleCallback(shellWarm, { timeout: 5000 });
  } else {
    window.setTimeout(shellWarm, 2500);
  }

  var chat = { boot: boot, booted: false, mount: mount };
  QF.chat = chat;
  /**
   * 壳的账本（`bootMs` = 环境准备总时长）。挂出来是为了让"预热省了多少"能量，
   * 而不是像 LaTeX 那次只能证明"机制在跑"。
   *
   * **名字是 `QF.pyrun`，不是 `QF.shell`** —— 后者是布局层（`theme/runtime/shell.js`，
   * 带 `mount`）的地盘。我第一版就叫 `QF.shell`，于是把它**覆盖掉**了：`QF.chat.boot()`
   * 一开口就是 `QF.shell.mount(...)`，直接 `TypeError` —— 整个应用**从那一刻起打不开**
   * （用户："不是，是我现在根本就进不去 quizforge 里面"）。教训：`QF.*` 是共享命名空间，
   * 占一个新名字前先 `grep "QF\."` 看一眼。
   */
  QF.pyrun = {
    warm: shellWarm,
    state: function () {
      return {
        started: !!shell.el,
        ready: shell.ready,
        // `bootMs` = 环境准备总时长（解释器 + 依赖 + 绘图预热），其余是分项
        bootMs: Math.round(shell.bootMs || 0),
        pyMs: Math.round(shell.pyMs || 0),
        packMs: Math.round(shell.packMs || 0),
        warmMs: Math.round(shell.warmMs || 0),
        info: shell.info,
      };
    },
  };
})();
