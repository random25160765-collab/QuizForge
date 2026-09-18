/* 顶栏中部的枢纽图标组：工具挂载开关。
 *
 * 用户的话是"亮起即挂载、熄灭即独立、全部熄灭即极简模式"。
 * 三条刻意的取舍：
 *
 * * **不弹提示条**：状态就在图标上（亮着/熄灭 + 全灭时旁边出现"极简"），
 *   点一下自己会变，再弹一条"已关闭"是噪音。
 * * **以服务端为准**：每次切换都拿返回的那份状态重画（`declared` 也是服务端算的）——
 *   本地先改先亮、服务端失败再回滚那种写法，会在"看起来关了但模型还能调"时骗人。
 * * **只在对话页出现**：它管的是 AI 能碰到什么，而对话是唯一的调度枢纽。
 */
(function () {
  var QF = (window.QF = window.QF || {});
  var h = (QF.ui && QF.ui.h) || null;

  var state = { groups: [], mounted: [], minimal: false, declared: 0, failed: '' };
  var clickSeq = 0;   // 第几下点击：响应回来时若不是最新那一下，就不拿它改本地状态
  var pending = null; // 排着要发的那一份集合（点击合并进它）
  var sending = false;


  /* 细描边同源图标：viewBox 24、stroke-width 1.8，与顶栏其它图标一套。 */
  var PATHS = {
    notes: '<path d="M5 4.5h9.5L19 9v10.5H5z"/><path d="M14.2 4.6V9H19"/><path d="M8 12.5h8"/><path d="M8 16h5"/>',
    library: '<path d="M4.5 5.5h6a2 2 0 0 1 2 2v11a1.6 1.6 0 0 0-1.6-1.6H4.5z"/>' +
      '<path d="M19.5 5.5h-6a2 2 0 0 0-2 2v11a1.6 1.6 0 0 1 1.6-1.6h6.4z"/>',
    graph: '<circle cx="6.5" cy="7" r="2.4"/><circle cx="17.5" cy="7" r="2.4"/><circle cx="12" cy="17.5" r="2.4"/>' +
      '<path d="M8.6 8.4 11 15.3"/><path d="M15.4 8.4 13 15.3"/><path d="M8.9 7h6.2"/>',
    quiz: '<rect x="4.5" y="4.5" width="15" height="15" rx="2.4"/><path d="M8.4 12.2l2.3 2.3 4.9-5"/>',
    sandbox: '<rect x="3.5" y="5" width="17" height="14" rx="2.2"/><path d="M7.5 10l2.4 2.2-2.4 2.2"/><path d="M12.6 14.6h4"/>'
  };

  function host() {
    return document.getElementById('tool-hubs');
  }

  function title(one) {
    var word = one.mounted ? '亮着' : '熄灭';
    return one.label + '（' + word + '）：' + one.hint;
  }

  function draw() {
    var box = host();
    if (!box || !h) return;
    QF.ui.clear(box);
    state.groups.forEach(function (one) {
      var node = h('button.hub' + (one.mounted ? '.is-on' : ''), {
        type: 'button',
        title: title(one),
        'aria-pressed': one.mounted ? 'true' : 'false',
        'aria-label': one.label + (one.mounted ? '（已挂载）' : '（未挂载）'),
        onclick: function () {
          flip(one.key);
        }
      });
      node.innerHTML =
        '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" ' +
        'stroke-width="' + (one.mounted ? '2.1' : '1.8') + '" stroke-linecap="round" stroke-linejoin="round">' +
        (PATHS[one.key] || '') +
        '</svg>';
      box.appendChild(node);
    });
    if (state.minimal) {
      box.appendChild(
        h('span.hubs__note', { text: '极简', title: '五组都熄灭了：这一版 AI 不调用任何模块，只聊天' })
      );
    } else if (state.failed) {
      box.appendChild(h('span.hubs__note.is-bad', { text: '没存上', title: state.failed }));
    }
    box.title = '这一版 AI 能调 ' + state.declared + ' 个工具';
  }

  /** 发一份挂载集（串行：发完再看有没有新的排队）。 */
  function push() {
    if (sending || pending === null) return;
    var mine = pending;
    var shot = clickSeq;
    pending = null;
    sending = true;
    QF.api
      .post('/chat/mounts', { groups: mine })
      .then(function (data) {
        if (shot === clickSeq) apply(data);
      })
      .catch(function (err) {
        if (shot !== clickSeq) return;
        // 只在原地留一个记号（计划里写了不弹提示条），并把状态拉回服务端那一份
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

  /** 把服务端那一份装进来（它算的 `declared` 是最终权威）。 */
  function apply(data) {
    state.groups = data.groups || [];
    state.mounted = data.mounted || [];
    state.minimal = !!data.minimal;
    state.declared = (data.declared || []).length;
  }

  /** 本地先算一遍"亮着这几组能调几个工具" —— 图标与提示要立刻跟着手指走。 */
  function recount() {
    var total = 0;
    state.groups.forEach(function (one) {
      if (state.mounted.indexOf(one.key) >= 0) total += (one.tools || []).length;
    });
    state.declared = total;
  }

  function flip(key) {
    clickSeq += 1;
    var next = state.mounted.slice();
    var at = next.indexOf(key);
    if (at >= 0) next.splice(at, 1);
    else next.push(key);
    // **先亮/先灭，再发请求**。先前写的是"上一个请求没回来就别点"（busy 守卫），
    // 实测的后果是连点两下、第二下被悄悄丢掉，图标与设置从此对不上。
    // 乐观更新 + 以服务端返回为准，才是这个开关该有的样子。
    state.mounted = next;
    state.minimal = next.length === 0;
    state.failed = '';
    recount();
    draw();
    // 存盘走**串行 + 合并**：一次只发一份，途中的点击并进下一发。
    // 先前是每下都并发发一个 POST，实测踩到两件事：
    //   1. 响应乱序回来，用旧的那份 `apply` 把界面覆盖成旧的；
    //   2. 更重的一件 —— 请求**到达服务端的顺序**也不保证，最后落库的可能是较早那份，
    //      于是"界面全亮、库里少一组"（我实测就是这么留在库里的）。
    // 一份完整集合 + 串行发送，服务端的最终状态就等于最后想要的那一份。
    pending = next.slice();
    push();
    draw();
  }

  function load() {
    var box = host();
    if (!box || !QF.api) return;
    // 挂在活动栏上之后就是**全站可见**的：它管的是"AI 能碰到哪几块"，
    // 与当前在看哪个页面无关（原来那句"只在对话页出现"随顶栏一起撤了）
    QF.api
      .get('/chat/mounts')
      .then(function (data) {
        state.groups = data.groups || [];
        state.mounted = data.mounted || [];
        state.minimal = !!data.minimal;
        state.declared = (data.declared || []).length;
        draw();
      })
      .catch(function (err) {
        state.failed = (err && err.message) || '读不到挂载状态';
        draw();
      });
  }

  QF.mounts = { load: load, state: state };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', load);
  else load();
})();
