/* 对话页输入框左边那颗**模式药丸**：这一版对话是什么模式。
 *
 * 从"五个挂载开关"改成"三个模式"（用户定的）：工具与访问权限是模式的底层元素，
 * 不同的组合构成不同的模式，prompt 也跟着不同 —— 所以**界面只让他选模式**，
 * 组与权限档是零件，不再摆到他面前。
 *
 *   极简  不挂任何工具，只聊天
 *   查询  只查不改：翻笔记、读资料原文、走知识图谱（这一档只放 read）
 *   学习  全功能：外加题库、推题与批改、沙箱演示
 *
 * 长相也改过一版：原来是输入框**上方**一条分段控件，占掉整整一行；
 * 现在是输入框左边一颗扁药丸，点开弹选择栏（用户："这三个按钮合并到下面的输入框
 * 左侧，做成一个扁的长的药丸，点击会有一个选择栏。它占用的一整块地方都给上面吧"）。
 *
 * 三条沿用下来的取舍（都是踩过坑的）：
 *
 * * **以服务端为准**：每次切换都拿返回的那份状态重画（`declared` 也是服务端算的）——
 *   本地先改先亮、服务端失败再回滚那种写法，会在"看起来关了但模型还能调"时骗人。
 * * **串行 + 合并**：连点时一次只发一份，途中的点击并进下一发。并发发会让**到达
 *   服务端的顺序**不保证，最后落库的可能是较早那份（我实测在库里留下过这种状态）。
 * * **只在对话页出现**：它管的是 AI 能碰到什么，而对话是唯一的调度枢纽。
 */
(function () {
  var QF = (window.QF = window.QF || {});
  var h = (QF.ui && QF.ui.h) || null;

  var state = {
    mode: '',
    modeLabel: '',
    modes: [],
    access: [],
    groups: [],
    mounted: [],
    minimal: false,
    declared: 0,
    failed: ''
  };
  var clickSeq = 0;   // 第几下点击：响应回来时若不是最新那一下，就不拿它改本地状态
  var pending = null; // 排着要发的那个模式（点击合并进它）
  var sending = false;
  var pickEl = null;  // 弹出来的那条选择栏（没弹时为 null）

  /* 服务端没给 `modes` 时的兜底（比如静态快照）：不让药丸空着。 */
  var FALLBACK = [
    { key: 'minimal', label: '极简', hint: '不挂任何工具，只聊天' },
    { key: 'query', label: '查询', hint: '只查不改：翻笔记、读资料原文、走知识图谱' },
    { key: 'study', label: '学习', hint: '全功能：外加题库、推题与批改、沙箱演示' }
  ];

  /* 细描边同源图标：viewBox 24、stroke-width 1.8，与顶栏其它图标一套。
   * `custom` 是给"对不上任何预设"的老配置用的（服务端会回 mode=custom）。 */
  var PATHS = {
    minimal: '<path d="M5 5.5h14v9.5h-8l-4 3.6V15H5z"/>',
    query: '<circle cx="10.6" cy="10.6" r="5.6"/><path d="M14.7 14.7 19.6 19.6"/>',
    study:
      '<path d="M5 4.5h9.5L19 9v10.5H5z"/><path d="M14.2 4.6V9H19"/><path d="M8 13h8"/><path d="M8 16.5h5"/>',
    custom: '<path d="M5 8h14"/><path d="M5 16h14"/><circle cx="9.4" cy="8" r="2.1"/><circle cx="14.6" cy="16" r="2.1"/>'
  };

  /* 一直会用到的小箭头（画出来的，不用字符 —— 字符依赖字体，之前踩过） */
  var CARET =
    '<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" ' +
    'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9.5 12 15.5l6-6"/></svg>';

  function icon(key, size, width) {
    return (
      '<svg viewBox="0 0 24 24" width="' + size + '" height="' + size + '" fill="none" ' +
      'stroke="currentColor" stroke-width="' + (width || 1.8) + '" stroke-linecap="round" ' +
      'stroke-linejoin="round">' + (PATHS[key] || PATHS.custom) + '</svg>'
    );
  }

  function host() {
    // 只有对话页有这个槽 —— 别的页面拿不到宿主，整个模块静默不画（不报错）
    return document.getElementById('chat-modes');
  }

  function list() {
    return state.modes && state.modes.length ? state.modes : FALLBACK;
  }

  function current() {
    var key = state.mode;
    var all = list();
    for (var i = 0; i < all.length; i += 1) {
      if (all[i].key === key) return all[i];
    }
    return null;
  }

  function label() {
    var one = current();
    if (one) return one.label;
    return state.mode === 'custom' ? '自定义' : state.modeLabel || '模式';
  }

  function hint() {
    var one = current();
    if (one && one.hint) return one.label + '：' + one.hint;
    if (state.mode === 'custom') return '这一版用的不是三个预设之一；点开换一个即可归位';
    return label();
  }

  /* ---------------------------------------------------------------- 药丸 */

  function draw() {
    var box = host();
    if (!box || !h) return;
    QF.ui.clear(box);

    var pill = h('button.modepill' + (state.failed ? '.is-bad' : ''), {
      type: 'button',
      // 模式色靠这个属性认（三个模式三种颜色，见 chat.css 里那三条映射）
      'data-mode': state.mode || 'custom',
      'aria-haspopup': 'listbox',
      'aria-expanded': pickEl ? 'true' : 'false',
      title: hint() + '（点开换一个）',
      onclick: function (ev) {
        ev.stopPropagation();
        if (pickEl) closePick();
        else openPick();
      },
      html:
        icon(state.mode, 13, 1.9) +
        '<span class="modepill__label"></span>' +
        '<span class="modepill__caret">' + CARET + '</span>'
    });
    pill.querySelector('.modepill__label').textContent = label();
    if (state.failed) pill.title = '没存上：' + state.failed + '（点开可以再试）';
    box.appendChild(pill);
    box.title = '这一版 AI 能调 ' + state.declared + ' 个工具';

    // 输入框这一侧的颜色指示：把模式挂在整条 `.chat__box` 上，**发送键**因此
    // 跟着变色（用户："信息准备框的这边你可以用这个发送按钮的颜色来做指示"）。
    // 消息框右上角那盏灯在 chat.js 那边画 —— 它认的是**每条输入自己**的模式。
    var boxEl = document.querySelector('.chat__box');
    if (boxEl) {
      boxEl.setAttribute('data-mode', state.mode || 'custom');
      boxEl.title = '这一版能调 ' + state.declared + ' 个工具';
    }

    // 选择栏开着的时候状态变了（比如服务端那份回来了）：跟着重画，勾要跟着走
    if (pickEl) openPick();
  }

  /* ------------------------------------------------------------ 选择栏 */

  function closePick() {
    if (!pickEl) return;
    var box = pickEl;
    pickEl = null; // 逻辑上**立刻**就算关了（键盘、连点都不会撞上它）
    document.removeEventListener('mousedown', onAway, true);
    document.removeEventListener('keydown', onKey, true);
    var pill = host() && host().querySelector('.modepill');
    if (pill) pill.setAttribute('aria-expanded', 'false');
    // **收起来要有动效**（用户："展开和回收没有做动效"）：先挂 `.is-out` 让它淡出，
    // 过渡跑完再摘节点。这段时间它 `pointer-events: none`（见 CSS），不挡下面的点击。
    box.classList.add('is-out');
    setTimeout(function () {
      if (box.parentNode) box.parentNode.removeChild(box);
    }, 170);
  }

  function onAway(ev) {
    if (!pickEl) return;
    // 点**药丸本身**不算"走开" —— 它有自己的开合逻辑（见上面那颗 pill 的 onclick）。
    // 不排掉它的话，同一次点击会走两遍：这里是 mousedown 且**捕获**（比 click 早），
    // 先把选择栏关掉；随后药丸的 onclick 看到 `pickEl` 已是 null，于是又 openPick()
    // —— 表现就是"点一下跳出来，再点一下收不回去"（用户报的正是这个）。
    var anchor = host() && host().querySelector('.modepill');
    if (anchor && anchor.contains(ev.target)) return;
    if (!pickEl.contains(ev.target)) closePick();
  }

  function onKey(ev) {
    if (ev.key === 'Escape') closePick();
  }

  function openPick() {
    closePick();
    var anchor = host() && host().querySelector('.modepill');
    if (!anchor || !h) return;

    var box = h('div.modepick', { role: 'listbox', 'aria-label': '换一个模式' });
    list().forEach(function (one) {
      var on = one.key === state.mode;
      var row = h('button.modepick__row' + (on ? '.is-on' : ''), {
        type: 'button',
        role: 'option',
        'data-mode': one.key,
        'aria-selected': on ? 'true' : 'false',
        html:
          '<span class="modepick__ico">' + icon(one.key, 15, on ? 2.1 : 1.8) + '</span>' +
          '<span class="modepick__main"><span class="modepick__name"></span>' +
          '<span class="modepick__hint"></span></span>' +
          '<span class="modepick__meta"></span>',
        onclick: function () {
          closePick();
          pick(one.key);
        }
      });
      row.querySelector('.modepick__name').textContent = one.label;
      row.querySelector('.modepick__hint').textContent = one.hint || '';
      row.querySelector('.modepick__meta').textContent =
        typeof one.tools === 'number' ? one.tools + ' 个工具' : '';
      box.appendChild(row);
    });
    if (state.failed) {
      box.appendChild(h('div.modepick__err', { text: '上次没存上：' + state.failed }));
    }

    document.body.appendChild(box);
    // 贴着药丸、**向上**弹：它在屏幕最下面，往下弹会被窗口截掉
    var r = anchor.getBoundingClientRect();
    box.style.left = Math.round(r.left) + 'px';
    box.style.bottom = Math.round(window.innerHeight - r.top + 6) + 'px';
    pickEl = box;
    anchor.setAttribute('aria-expanded', 'true');
    // **展开也要有动效**：先以"收起态"进 DOM（见 CSS 里 `.modepick` 的初值），
    // 逼一次布局再挂 `.is-in` —— 不这样的话浏览器会把两个状态合并成一次计算，
    // 过渡那一帧根本不跑，看起来就是"啪"地一下出现。
    void box.offsetHeight;
    box.classList.add('is-in');
    // 同一拍里就装监听会把"点开这一下"自己也当成走开，所以推到下一拍
    setTimeout(function () {
      document.addEventListener('mousedown', onAway, true);
      document.addEventListener('keydown', onKey, true);
    }, 0);
  }

  /* ------------------------------------------------------------ 落库 */

  /** 发一个模式（串行：发完再看有没有新的排队）。 */
  function push() {
    if (sending || pending === null) return;
    var mine = pending;
    var shot = clickSeq;
    pending = null;
    sending = true;
    QF.api
      .post('/chat/mounts', { mode: mine })
      .then(function (data) {
        if (shot === clickSeq) apply(data);
      })
      .catch(function (err) {
        if (shot !== clickSeq) return;
        // 只在原地留一个记号（不弹提示条），并把状态拉回服务端那一份
        state.failed = (err && err.message) || '保存失败';
        return QF.api.get('/chat/mounts').then(apply).catch(function () {});
      })
      .then(function () {
        sending = false;
        if (pending !== null) {
          push();
          return;
        }
        if (shot === clickSeq) draw();
      });
  }

  /** 把**服务端给的**三个颜色注入成 CSS 变量。
   *
   * 用户："注意不要硬编码颜色 —— 未来如果我想改颜色呢？那应该是一键式的。"
   * 所以 CSS 里一个色号都不写，只认 `var(--mode-*)`；要换颜色就改 `mounts.py` 里那份
   * `Mode.color`（或设置里的 `mode_colors`）—— 这里会自动跟着变。
   */
  function paintPalette() {
    var root = document.documentElement;
    list().forEach(function (one) {
      if (one && one.key && one.color) {
        root.style.setProperty('--mode-' + one.key, String(one.color));
      }
    });
  }

  /** 吆喝一声"模式变了"：别处（对话页正在编辑的那条：灯 + 「发送」键）靠它就地跟上。
   *
   * 为什么用 `document` 上的自定义事件而不是回调表：与 `shell.js` 里那条
   * `qf:data-changed` 是同一套写法 —— 谁要听谁听，两边不互相持有。
   */
  function announce() {
    try {
      document.dispatchEvent(new CustomEvent('qf:mode', { detail: { mode: state.mode || '' } }));
    } catch (err) {
      /* 没有 CustomEvent 的老浏览器就算了：只是颜色不跟着变，功能不受影响 */
    }
  }

  /** 把服务端那一份装进来（它算的 `declared` 是最终权威）。 */
  function apply(data) {
    state.mode = data.mode || '';
    state.modeLabel = data.modeLabel || '';
    state.modes = data.modes || [];
    state.access = data.access || [];
    state.groups = data.groups || [];
    state.mounted = data.mounted || [];
    state.minimal = !!data.minimal;
    state.declared = (data.declared || []).length;
    // 颜色跟着一起来：色号只存在服务端那一处
    paintPalette();
    announce();
  }

  /** 本地先算一遍"这个模式能调几个工具" —— 药丸与选择栏要立刻跟着手指走。 */
  function recount(key) {
    var all = list();
    for (var i = 0; i < all.length; i += 1) {
      if (all[i].key === key && typeof all[i].tools === 'number') {
        state.declared = all[i].tools;
        return;
      }
    }
  }

  function pick(key) {
    if (key === state.mode) return;
    clickSeq += 1;
    // **先亮，再发请求**。先前写的是"上一个请求没回来就别点"（busy 守卫），
    // 实测的后果是连点两下、第二下被悄悄丢掉，界面与设置从此对不上。
    // 乐观更新 + 以服务端返回为准，才是这个开关该有的样子。
    state.mode = key;
    state.minimal = key === 'minimal';
    state.failed = '';
    recount(key);
    draw();
    // 乐观更新这一刻就吆喝：正在编辑的那条要**立刻**跟着变色，
    // 不能等服务端回包（用户："我在编辑输入状态下频繁改变模式，
    // 右上角的灯和下面的发送不会跟着一起改变颜色"）。
    announce();
    pending = key;
    push();
    draw();
  }

  var loading = false;

  function load() {
    if (!QF.api || loading) return;
    // **不要因为"宿主还没建出来"就退出**：外壳先 mount，对话页的骨架（含这颗药丸的槽）
    // 是 mount 之后才建的。原先在这里直接 return，于是状态一次都没取回来，
    // 整条栏空着（实测：容器在、子节点 0、标题写着"能调 0 个工具"）。
    // 画不画由 `draw()` 自己看有没有宿主决定。
    loading = true;
    QF.api
      .get('/chat/mounts')
      .then(function (data) {
        apply(data);
        draw();
      })
      .catch(function (err) {
        state.failed = (err && err.message) || '读不到模式状态';
        draw();
      });
  }

  // `render` 给"宿主刚建好"的调用方用（对话页骨架建完就调一次）
  QF.mounts = { render: draw, load: load, state: state };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', load);
  else load();
})();
