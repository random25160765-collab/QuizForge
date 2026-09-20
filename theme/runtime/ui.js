/* ===========================================================================
 * ui.js —— DOM 构造、主题、提示条、弹窗、底部状态栏
 *
 * 全部挂到全局 QF 命名空间下，文件按 build.py 的 RUNTIME_ORDER 顺序拼接。
 * ========================================================================= */
(function () {
  'use strict';

  var QF = (window.QF = window.QF || {});

  /* ------------------------------------------------------------- DOM 构造 */

  function append(parent, child) {
    if (child == null || child === false || child === true) return;
    if (Array.isArray(child)) {
      child.forEach(function (c) {
        append(parent, c);
      });
      return;
    }
    if (child instanceof Node) {
      parent.appendChild(child);
      return;
    }
    parent.appendChild(document.createTextNode(String(child)));
  }

  /**
   * h('div.card', { onClick: fn }, child, child...)
   * 支持 tag 里带 .class 与 #id 简写。
   */
  function h(tag, props) {
    var m = /^([a-zA-Z][\w-]*)?((?:[.#][\w-]+)*)$/.exec(tag);
    var name = (m && m[1]) || 'div';
    var el = document.createElement(name);
    if (m && m[2]) {
      m[2].split(/(?=[.#])/).forEach(function (token) {
        if (!token) return;
        if (token[0] === '.') el.classList.add(token.slice(1));
        else if (token[0] === '#') el.id = token.slice(1);
      });
    }
    if (props) {
      Object.keys(props).forEach(function (key) {
        var value = props[key];
        if (value == null || value === false) return;
        if (key === 'class' || key === 'className') {
          String(value)
            .split(/\s+/)
            .filter(Boolean)
            .forEach(function (c) {
              el.classList.add(c);
            });
        } else if (key === 'text') {
          el.textContent = value;
        } else if (key === 'html') {
          el.innerHTML = value;
        } else if (key === 'style' && typeof value === 'object') {
          Object.assign(el.style, value);
        } else if (key === 'dataset' && typeof value === 'object') {
          Object.assign(el.dataset, value);
        } else if (key === 'value') {
          // 必须走属性赋值：textarea 的 value 不是 HTML 属性
          el.value = value;
        } else if (key.slice(0, 2) === 'on' && typeof value === 'function') {
          el.addEventListener(key.slice(2).toLowerCase(), value);
        } else {
          el.setAttribute(key, value === true ? '' : value);
        }
      });
    }
    append(el, Array.prototype.slice.call(arguments, 2));
    return el;
  }

  function clear(el) {
    while (el && el.firstChild) el.removeChild(el.firstChild);
    return el;
  }

  /**
   * 「丝滑」的视图替换：把 DOM 替换交给 View Transitions API。
   *
   * 为什么不用自己写淡入淡出：那种做法必须让画面先变淡，深色主题下那一刻
   * 就是「闪一下黑」—— 问题的成因正是它。View Transitions 反过来：
   * 浏览器把**旧画面留成快照垫在底下**，新画面在它上面淡入，
   * 全程底下都有东西，永远不会露出背景色。
   *
   * 不支持该 API 的浏览器直接同步替换：没有动画，但同样不会闪。
   */
  function viewSwap(swap) {
    if (typeof document.startViewTransition !== 'function') {
      swap();
      return;
    }
    document.startViewTransition(swap);
  }

  /**
   * 量一次「汉字墨迹相对行盒中心的偏移」，写成 --ink-shift 交给样式用。
   *
   * 为什么需要：汉字画在 em 框里，而 em 框并不落在 ascent/descent 的正中
   * （Noto / 思源黑体：ascent 1.16em、descent 0.29em，汉字只占 -0.12~0.88em），
   * 所以 flex 的「行盒居中」看着比几何中心低半像素左右 —— 圆角按钮、胶囊、
   * 导航项里的文字因此显得没坐正，而旁边按几何中心摆的图标看着就偏高了。
   *
   * 数值按当前字体量出来（换字体、换字号都跟着走），不写死：硬编码 0.5px
   * 只对一种字体正确，换成 em 框位置不同的字体（苹方之类）就反过来偏。
   */
  function calibrateInkShift() {
    try {
      var probe = document.createElement('canvas');
      // 画布要放得下整串探针字（100px 的四个汉字约 400px 宽），
      // 画布太窄只会画出头一个字，量出来的代表性和预期就差了
      probe.width = 420;
      probe.height = 200;
      var ctx = probe.getContext('2d');
      if (!ctx) return;

      var family = getComputedStyle(document.body).fontFamily || 'sans-serif';
      ctx.font = '100px ' + family;
      ctx.textBaseline = 'alphabetic';
      // 用多个字做探针：单个字的墨迹不具代表性（「国」这类全包围字明显偏高，
      // 量出来会多抬一倍），几个常用字取并集后的墨迹高度才和标签里的平均情况一致。
      ctx.fillText('正确答案', 20, 140);

      var data = ctx.getImageData(0, 0, 420, 200).data;
      var top = -1;
      var bottom = -1;
      for (var y = 0; y < 200; y++) {
        for (var x = 0; x < 420; x++) {
          if (data[(y * 420 + x) * 4 + 3] > 16) {
            if (top < 0) top = y;
            bottom = y;
          }
        }
      }
      if (top < 0) return;

      var metrics = ctx.measureText('正确答案');
      var ascent = metrics.fontBoundingBoxAscent;
      var descent = metrics.fontBoundingBoxDescent;
      if (!ascent || !descent) return;

      // 两者都相对基线量：差多少就平移多少（负值 = 往上抬）
      var boxCenter = (ascent - descent) / 2;
      var inkCenter = 140 - (top + bottom) / 2;
      var shift = (inkCenter - boxCenter) / 100;      // em
      if (Math.abs(shift) > 0.08) return;             // 量出来离谱就宁可不校正
      document.documentElement.style.setProperty('--ink-shift', shift.toFixed(4) + 'em');
    } catch (err) {
      /* 量不出来保持几何居中，不影响任何功能 */
    }
  }

  function esc(text) {
    return String(text == null ? '' : text).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function clamp(value, lo, hi) {
    return Math.min(hi, Math.max(lo, value));
  }

  function debounce(fn, wait) {
    var timer = 0;
    return function () {
      var args = arguments,
        self = this;
      clearTimeout(timer);
      timer = setTimeout(function () {
        fn.apply(self, args);
      }, wait);
    };
  }

  /* ------------------------------------------------------------- 格式化 */

  function pct(n, d) {
    if (!d) return '—';
    return ((n / d) * 100).toFixed(n / d === 1 ? 0 : 1) + '%';
  }

  function pad2(n) {
    return (n < 10 ? '0' : '') + n;
  }

  function dayKey(date) {
    var d = date ? new Date(date) : new Date();
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
  }

  function fmtTime(ts) {
    if (!ts) return '—';
    var d = new Date(ts);
    return dayKey(d) + ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }

  function fmtRelative(ts) {
    if (!ts) return '—';
    var diff = Date.now() - ts;
    var abs = Math.abs(diff);
    var future = diff < 0;
    var units = [
      [86400000 * 30, '个月'],
      [86400000, '天'],
      [3600000, '小时'],
      [60000, '分钟'],
    ];
    for (var i = 0; i < units.length; i++) {
      if (abs >= units[i][0]) {
        var n = Math.round(abs / units[i][0]);
        return future ? n + ' ' + units[i][1] + '后' : n + ' ' + units[i][1] + '前';
      }
    }
    return future ? '马上' : '刚刚';
  }

  function fmtDuration(ms) {
    var total = Math.max(0, Math.round(ms / 1000));
    var m = Math.floor(total / 60);
    var s = total % 60;
    if (m >= 60) return Math.floor(m / 60) + ':' + pad2(m % 60) + ':' + pad2(s);
    return pad2(m) + ':' + pad2(s);
  }

  function fmtBytes(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1024 / 1024).toFixed(2) + ' MB';
  }

  function truncate(text, max) {
    var limit = max || 160;
    var src = String(text == null ? '' : text);
    // 列表摘要只需要开头的几十个字，先把超长题面截断再做正则，
    // 避免为几百条列表项反复扫描整段题面（含大段公式与代码）
    var scanCap = Math.max(240, limit * 8);
    if (src.length > scanCap) src = src.slice(0, scanCap);

    var clean = src
      .replace(/\$\$[\s\S]*?\$\$/g, ' ⟨公式⟩ ')
      .replace(/\$[^$\n]*\$/g, ' ⟨公式⟩ ')
      .replace(/```[\s\S]*?```/g, ' ⟨代码⟩ ')
      .replace(/[#*`>|-]+/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
    return clean.length > limit ? clean.slice(0, limit) + '…' : clean;
  }

  /* ------------------------------------------------------------- 图标 */

  /**
   * 图标一律以 24×24 为画布，**笔画要落在画布正中**（12,12）。
   * 图标是按几何中心与文字对齐的，笔画自己偏了，看起来就是图标没对齐。
   * 量法：把 svg 挂进文档后取 getBBox()，看中心离 (12,12) 差多少 ——
   * 下面几个偏了的（shuffle / spark / refresh / download / upload）已按此校正。
   * chevron 与 search 的 ±0.5 是刻意的光学修正（箭头、放大镜手柄本来就偏），
   * play 的 +1 也是（右向三角形的视觉重心偏左，居中反而看着偏）。
   */
  var ICONS = {
    play: '<path d="M7 4.5 19 12 7 19.5Z"/>',
    check: '<path d="M4 12.5 9.5 18 20 6.5"/>',
    close: '<path d="M6 6l12 12M18 6 6 18"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    star: '<path d="M12 3.6l2.6 5.3 5.9.9-4.3 4.2 1 5.8-5.2-2.7-5.2 2.7 1-5.8-4.3-4.2 5.9-.9Z"/>',
    warn: '<path d="M12 4 2.5 20h19Z"/><path d="M12 10v4.5M12 17.4v.2"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.4v.2"/>',
    bulb: '<path d="M9 18h6M10 21h4"/><path d="M12 3a6 6 0 0 0-3.5 10.9V15h7v-1.1A6 6 0 0 0 12 3Z"/>',
    chevronL: '<path d="M14.5 6 8.5 12l6 6"/>',
    chevronR: '<path d="M9.5 6l6 6-6 6"/>',
    shuffle: '<path d="M4 7.5h3.5l3 5M4 19.5h3.5l3-5"/><path d="M14 7.5h6v0M20 7.5l-3-3M20 7.5l-3 3"/>',
    list: '<path d="M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7.5V12l3 2"/>',
    flag: '<path d="M6 21V4h12l-2.5 4.5L18 13H6"/>',
    trash: '<path d="M4 7h16M9 7V4.5h6V7M6.5 7l1 13h9l1-13"/>',
    download: '<path d="M12 4V15.5M7.5 11 12 15.5l4.5-4.5"/><path d="M4.5 20h15"/>',
    upload: '<path d="M12 15.5V4M7.5 8.5 12 4 16.5 8.5"/><path d="M4.5 20h15"/>',
    print: '<path d="M7 9V3.5h10V9"/><path d="M4.5 9h15v7h-3M7.5 16H4.5"/><path d="M7.5 13h9v7.5h-9Z"/>',
    book: '<path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H11v16H5.5A1.5 1.5 0 0 1 4 18.5Z"/><path d="M20 5.5A1.5 1.5 0 0 0 18.5 4H13v16h5.5a1.5 1.5 0 0 0 1.5-1.5Z"/>',
    cpu: '<rect x="7" y="7" width="10" height="10" rx="2"/><path d="M10 3v3M14 3v3M10 18v3M14 18v3M3 10h3M3 14h3M18 10h3M18 14h3"/>',
    spark: '<path d="M12 5l1.9 5.1 5.1 1.9-5.1 1.9L12 19l-1.9-5.1L5 12l5.1-1.9Z"/>',
    target: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r="1"/>',
    robot: '<rect x="4.5" y="7.5" width="15" height="11" rx="3"/><path d="M12 3.5V7.5M8.5 12v1.5M15.5 12v1.5M9.5 18.5v2M14.5 18.5v2"/>',
    refresh: '<path d="M20 11.5a8 8 0 1 0-2.6 6.4"/><path d="M20 5.5v6h-6"/>',
    search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
    filter: '<path d="M4 6h16M7 12h10M10 18h4"/>',
    grip: '<path d="M9 6h.01M9 12h.01M9 18h.01M15 6h.01M15 12h.01M15 18h.01"/>',
  };

  function icon(name, size) {
    var body = ICONS[name] || '';
    return (
      '<svg viewBox="0 0 24 24" width="' + (size || 16) + '" height="' + (size || 16) +
      '" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">' +
      body + '</svg>'
    );
  }

  /* ------------------------------------------------------------- Toast */

  var toastRoot = null;

  function toast(message, kind, timeout) {
    if (!toastRoot) toastRoot = document.getElementById('toast-root');
    if (!toastRoot) return;
    var iconName = kind === 'error' ? 'warn' : kind === 'ok' ? 'check' : kind === 'warn' ? 'warn' : 'info';
    var node = h(
      'div.toast',
      { class: 'toast toast--' + (kind || 'info'), role: kind === 'error' ? 'alert' : 'status' },
      h('span.toast__icon', { html: icon(iconName, 15) }),
      h('span.toast__text', { text: message })
    );
    toastRoot.appendChild(node);
    requestAnimationFrame(function () {
      node.classList.add('is-in');
    });
    setTimeout(function () {
      node.classList.remove('is-in');
      setTimeout(function () {
        if (node.parentNode) node.parentNode.removeChild(node);
      }, 240);
    }, timeout || (kind === 'error' ? 5200 : 2600));
    return node;
  }

  /* ------------------------------------------------------------- Modal */

  /**
   * 弹层的挂载点，**按需创建**。
   *
   * 原先是从页面里取 `#modal-root`，取不到就直接返回一个空壳 ——
   * 而四个页面**都没有**这个元素，于是 `ui.confirm` 永远不弹、它的 promise
   * 永远不 settle：用户看到的就是"点了删除没反应，对话删不掉"（实测复现）。
   * 挂载点这种东西不该由每个页面各自记得写 —— 缺了就补一个。
   */
  function modalRoot() {
    var root = document.getElementById('modal-root');
    if (!root) {
      root = document.createElement('div');
      root.id = 'modal-root';
      // class 不能少：定位与遮罩（position: fixed / grid / place-items: center）
      // 全挂在 `.modal-root` 上 —— 只给 id 的话它没有样式，卡片会落在 <body> 末尾，
      // 也就是"先在页面底部闪一下、再跳回中心"（实测踩过）。
      root.className = 'modal-root';
      root.hidden = true;
      document.body.appendChild(root);
    }
    return root;
  }

  function modal(options) {
    var root = modalRoot();
    var opts = options || {};
    var closable = opts.closable !== false;

    var card = h('div.modal__card', { class: opts.size ? 'modal__card--' + opts.size : '' });
    var closed = false;
    var rafId = 0;

    function close() {
      if (closed) return;
      closed = true;
      // 若在入场动画的那一帧之前就被关闭（例如程序化快速点击），
      // 必须取消 rAF，否则回调会把 is-open 又加回去，遮罩就再也关不掉了
      if (rafId) cancelAnimationFrame(rafId);
      document.removeEventListener('keydown', onKey);
      // 这一张进入"退场"：脱离流（否则它会在 grid 里占掉一行，把新弹层顶到下面 ——
      // 用户看到的就是"确认框先在下面闪一下再回到中心"），自己淡出。
      // 遮罩的透明度**不动**：可能还有别的弹层在用（菜单里点删除就是这个情形）。
      card.classList.add('is-leaving');
      setTimeout(function () {
        // **只摘掉自己这张卡**。弹层里再开一个弹层是常规操作
        // （会话菜单 → "确认删除"），而前一个的收尾定时器还在跑 ——
        // 原先它 `clear(root)` 会把后开的那个一并清掉，
        // 用户看到的就是"对话框闪一下就没了，对话也没删掉"（实测）。
        card.remove();
        if (!root.children.length) {
          root.classList.remove('is-open');
          root.hidden = true;
        }
      }, 180);
      if (opts.onClose) opts.onClose();
    }

    function onKey(event) {
      if (event.key === 'Escape' && closable) close();
    }

    if (opts.title) {
      card.appendChild(
        h(
          'header.modal__head',
          null,
          h('h3.modal__title', { text: opts.title }),
          closable
            ? h('button.icon-btn', {
                type: 'button',
                title: '关闭',
                'aria-label': '关闭',
                html: icon('close', 16),
                onClick: close,
              })
            : null
        )
      );
    }
    if (opts.body) card.appendChild(h('div.modal__body', null, opts.body));
    if (opts.actions && opts.actions.length) {
      card.appendChild(
        h(
          'footer.modal__foot',
          null,
          opts.actions.map(function (action) {
            return h(
              'button.btn',
              {
                type: 'button',
                class: 'btn--' + (action.kind || 'ghost'),
                onClick: function () {
                  if (!action.onClick || action.onClick(close) !== false) close();
                },
              },
              action.icon ? h('span', { html: icon(action.icon, 15) }) : null,
              h('span', { text: action.label })
            );
          })
        )
      );
    }

    card.addEventListener('click', function (e) {
      e.stopPropagation();
    });
    if (closable) {
      root.onclick = close;
    } else {
      root.onclick = null;
    }

    // 不清空 root：可能还有别的弹层正在收尾（见 close 的说明），清掉就把它弄没了
    root.appendChild(card);
    root.hidden = false;
    document.addEventListener('keydown', onKey);
    rafId = requestAnimationFrame(function () {
      rafId = 0;
      if (!closed) root.classList.add('is-open');
    });

    return { close: close, card: card };
  }

  function confirmDialog(message, options) {
    var opts = options || {};
    return new Promise(function (resolve) {
      modal({
        title: opts.title || '请确认',
        size: 'sm',
        body: h('p.modal__text', { text: message }),
        closable: true,
        onClose: function () {
          resolve(false);
        },
        actions: [
          { label: opts.cancelLabel || '取消', kind: 'ghost', onClick: function () { resolve(false); } },
          {
            label: opts.okLabel || '确定',
            kind: opts.danger ? 'danger' : 'primary',
            onClick: function () {
              resolve(true);
            },
          },
        ],
      });
    });
  }

  /* ------------------------------------------------------------- 主题 */

  var THEME_KEY = 'quizforge.v1.settings';

  function readStoredSettings() {
    try {
      return JSON.parse(localStorage.getItem(THEME_KEY) || '{}') || {};
    } catch (e) {
      return {};
    }
  }

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme === 'light' ? 'light' : 'dark';
  }

  var theme = {
    current: function () {
      return document.documentElement.dataset.theme || 'dark';
    },
    set: function (value) {
      applyTheme(value);
      try {
        var raw = readStoredSettings();
        raw.theme = theme.current();
        localStorage.setItem(THEME_KEY, JSON.stringify(raw));
      } catch (e) {
        /* localStorage 不可用时静默降级 */
      }
    },
    toggle: function () {
      theme.set(theme.current() === 'dark' ? 'light' : 'dark');
    },
    init: function () {
      applyTheme(readStoredSettings().theme || 'dark');
    },
  };

  /* ------------------------------------------------------------- 状态栏 */

  var statusbar = {
    set: function (items) {
      var bar = document.getElementById('statusbar');
      if (!bar) return;
      var list = (items || []).filter(Boolean);
      clear(bar);
      // 状态栏只在「有实际控件/提示」的进行中视图里出现。
      // 没有内容时整条隐藏，否则会剩下一条空栏和一道多余的边框。
      document.body.dataset.sb = list.length ? 'on' : 'off';
      list.forEach(function (item) {
        if (!item) return;
        if (item.spacer) {
          bar.appendChild(h('span.sb__spacer'));
          return;
        }
        if (item.node) {
          bar.appendChild(item.node);
          return;
        }
        if (item.kind === 'kbd' || item.kind === 'hint') {
          bar.appendChild(
            h('span.sb__hint', null, h('kbd', { text: item.key }), h('span', { text: item.label }))
          );
          return;
        }
        if (item.kind === 'stat') {
          bar.appendChild(
            h(
              'span.sb__stat',
              { class: item.tone ? 'sb__stat--' + item.tone : '' },
              h('span.sb__stat-label', { text: item.label }),
              h('b', { text: item.value })
            )
          );
          return;
        }
        if (item.kind === 'button') {
          bar.appendChild(
            h(
              'button.btn',
              {
                type: 'button',
                class: 'btn--' + (item.tone || 'ghost'),
                disabled: item.disabled || false,
                title: item.title || item.label,
                onClick: item.onClick,
              },
              item.icon ? h('span', { html: icon(item.icon, 15) }) : null,
              item.label ? h('span', { text: item.label }) : null
            )
          );
          return;
        }
        bar.appendChild(h('span.sb__text', { text: item.label }));
      });
    },
    clear: function () {
      statusbar.set([]);
    },
  };

  /* ------------------------------------------------------------- 进度条 */

  function progressBar(ratio, tone) {
    var r = clamp(ratio, 0, 1);
    return h(
      'div.progress',
      { class: tone ? 'progress--' + tone : '' },
      h('i', { style: { width: (r * 100).toFixed(1) + '%' } })
    );
  }

  function ring(ratio, options) {
    var opts = options || {};
    var r = clamp(ratio, 0, 1);
    var size = opts.size || 56;
    var stroke = opts.stroke || 5;
    var radius = (size - stroke) / 2;
    var circumference = 2 * Math.PI * radius;
    var offset = circumference * (1 - r);
    var svg =
      '<svg viewBox="0 0 ' + size + ' ' + size + '" width="' + size + '" height="' + size + '">' +
      '<circle class="ring__track" cx="' + size / 2 + '" cy="' + size / 2 + '" r="' + radius +
      '" fill="none" stroke-width="' + stroke + '"/>' +
      '<circle class="ring__fill" cx="' + size / 2 + '" cy="' + size / 2 + '" r="' + radius +
      '" fill="none" stroke-width="' + stroke + '" stroke-linecap="round" ' +
      'stroke-dasharray="' + circumference.toFixed(1) + '" stroke-dashoffset="' + offset.toFixed(1) +
      '" transform="rotate(-90 ' + size / 2 + ' ' + size / 2 + ')"/>' +
      '</svg>';
    return h(
      'div.ring',
      { style: { width: size + 'px', height: size + 'px', '--ring-color': opts.color || 'var(--pri)' } },
      h('span.ring__svg', { html: svg }),
      opts.label ? h('span.ring__label', { text: opts.label }) : null
    );
  }

  /* ------------------------------------------------------------- 标签/徽章 */

  var TYPE_LABEL_FALLBACK = { single: '单选', multi: '多选', blank: '填空', short: '简答' };

  function typeLabel(type) {
    var labels = (QF.data && QF.data.typeLabels) || TYPE_LABEL_FALLBACK;
    return labels[type] || type;
  }

  function difficultyDots(level) {
    var dots = [];
    for (var i = 1; i <= 5; i++) {
      dots.push(h('i', { class: i <= level ? 'is-on' : '' }));
    }
    return h('span.diff', { title: '难度 ' + level + '/5' }, dots);
  }

  // 启动时量一次汉字墨迹偏移（内存里的 canvas 测量，亚毫秒级）。
  // 之后所有「圆角容器里的文字」都靠 --ink-shift 落到几何中心。
  calibrateInkShift();

  /**
   * 换内容时做一次淡入淡出。
   *
   * 何时用：**用户按下某个东西、一整块内容换掉**的时候（换笔记、换对话、换条目、换标签）。
   * 何时不用：自动发生的重画（自动保存后重画状态栏、流式吐字、窗口尺寸变化）——
   * 那些一秒好几次，加了就是一直在闪。
   *
   * 做法是 CSS 淡出 → 换 → 淡入（110ms 一下），**刻意不用 View Transitions**：
   * 实测同一个文档里只要用过一次 `startViewTransition`，跨页那次（app.css 里
   * `@view-transition { navigation: auto }`，项目里早就有的那套）就会被拒，
   * 并在控制台留一条 "ViewTransition opt-in disabled"。跨页那套覆盖面更大，
   * 所以让路的是这一套。
   *
   * `host` 是要淡的那一块（默认整页内容 `#app-root`）。像笔记页那种"只换正文、
   * 左边树别动"，就把主区传进来。系统里关掉了动效的**直接换** —— 这只是顺滑，
   * 不该成为可用性的一部分。
   */
  function swap(run, host) {
    var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    var box = host || document.getElementById('app-root');
    if (reduce || !box) {
      run();
      return;
    }
    // 同一块上连着换（点得快）：后来那次说了算，别让前一个定时器把内容定格在透明里
    var token = (box.__qfSwap || 0) + 1;
    box.__qfSwap = token;
    // **两个方向都要有过渡。**
    //
    // 原先只给了"淡出"：换完内容就把 transition 清掉，于是 opacity 从 0 回到 1
    // 是**瞬间**的 —— 看上去是"淡下去、啪地出现"。这正是那句"不同对话之间的
    // 切换太生硬 / 不够丝滑"的真正来源（不是没有过渡，是只做了一半）。
    box.style.transition = 'opacity 110ms ease';
    box.style.opacity = '0';
    window.setTimeout(function () {
      if (box.__qfSwap !== token) return;
      run();
      // 换完**隔两帧**再淡回来：同一帧里改回去等于没改（不会触发过渡）
      window.requestAnimationFrame(function () {
        window.requestAnimationFrame(function () {
          if (box.__qfSwap !== token) return;
          box.style.opacity = '1'; // 这一次是**带过渡**地回来
          // 过渡跑完再撤掉内联样式，免得起止值留在元素上影响以后
          window.setTimeout(function () {
            if (box.__qfSwap !== token) return;
            box.style.transition = '';
            box.style.opacity = '';
          }, 130);
        });
      });
    }, 110);
  }


  /* ---------------------------------------------------------- 页面之间的过渡
   *
   * 点站内链接：先给 `body` 加 `data-leaving`（CSS 把内容淡出，120ms），再真的跳；
   * 新页面那边 `.view` 自己淡入（app.css）。两半加起来 ~300ms，连贯且**每次都在**。
   *
   * 为什么不用跨文档 View Transitions（原先就是这么做的）：实测它会漏 ——
   * 在笔记页打开过一篇笔记、等几秒再点活动栏换页，浏览器拒掉那次过渡并报
   * "ViewTransition opt-in disabled"；也试过"先 preventDefault、等上一个过渡收尾
   * 再跳"，更糟：程序化跳转丢掉用户激活，跨页照样被拒。用户那句"平滑过渡是
   * UI 设计的工程纪律，你没做全"说的就是这个漏。所以换成不依赖 opt-in 的做法。
   *
   * 拦的条件很保守：同源、普通左键、无修饰键、不是新窗口、不是下载、不是页内锚点。
   * 其它一律不碰（外链、右键新标签、⌘+点……）。
   */
  function leaveWithFade(href) {
    var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduce) {
      location.assign(href);
      return;
    }
    document.body.dataset.leaving = '1';
    window.setTimeout(function () {
      location.assign(href);
    }, 120);
  }

  document.addEventListener(
    'click',
    function (ev) {
      if (ev.defaultPrevented || ev.button !== 0) return;
      if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
      if (!ev.target || !ev.target.closest) return;
      var link = ev.target.closest('a[href]');
      if (!link || link.target || link.hasAttribute('download')) return;
      if (link.origin !== location.origin) return;
      var href = link.getAttribute('href') || '';
      if (!href || href.charAt(0) === '#') return;
      if (link.getAttribute('href') === location.pathname + location.search) return;
      ev.preventDefault();
      leaveWithFade(link.href);
    },
    true
  );

  QF.ui = {
    swap: swap,
    h: h,
    clear: clear,
    viewSwap: viewSwap,
    calibrateInkShift: calibrateInkShift,
    esc: esc,
    clamp: clamp,
    debounce: debounce,
    pct: pct,
    dayKey: dayKey,
    fmtTime: fmtTime,
    fmtRelative: fmtRelative,
    fmtDuration: fmtDuration,
    fmtBytes: fmtBytes,
    truncate: truncate,
    icon: icon,
    toast: toast,
    modal: modal,
    confirm: confirmDialog,
    theme: theme,
    statusbar: statusbar,
    progressBar: progressBar,
    ring: ring,
    typeLabel: typeLabel,
    difficultyDots: difficultyDots,
  };
})();
