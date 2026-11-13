/* ===========================================================================
 * scratch.js —— 碎片页：随手记一句的地方（对标 flomo）。
 *
 * ## 为什么是**一列聊天记录**
 *
 * 用户给的两张参照：版式照 QQ（最新的在最底下，输入框钉在底部，气泡靠右），
 * 单条的排法照 flomo（时间在上面、正文在下面、右上角一个"···"）。
 * 两者合起来正好是"跟自己说话"：每一条都是自己发出去的一句话，不存在收件人，
 * 所以全部靠右 —— 靠左会读成"别人说的"，而这一栏里没有别人。
 *
 * ## 左栏是标签，不是文件树
 *
 * 标签写在正文里（`#Books/札记/grammar`），这一栏只是**把它照出来**：
 * 树、条数、筛全在服务端算（`app/scratch_tags.py`），前端不写第二套解析规则。
 * 开合与拖动照抄对话页的左栏（`--scratch-aside-w`、拖右边线、⌘/Ctrl+B），
 * 理由那边写过一遍：一栏的宽窄是人的偏好，不是布局常量。
 *
 * ## 数据一律走服务端
 *
 * `/api/scratch`，这一页不做本地缓存、也不攒"待发队列"：碎片的全部价值就是
 * "记下来之后换台机器还在"，一份只活在 localStorage 里的记录等于没记。
 * 别的设备上的改动由 30 秒一次的对账 + 切回前台时补一次（见 `watch`），
 * 服务端那份则由 Turso 在后台按行同步（`app/sync.py`，加密上传）。
 * ========================================================================= */
(function () {
  'use strict';

  var QF = window.QF;
  var ui = QF.ui;
  var h = ui.h;
  var api = QF.api;

  var NARROW = window.matchMedia ? window.matchMedia('(max-width: 600px)') : null;
  //: 与对话页同一条断点（chat.js 的 NARROW_ASIDE）：这以下左栏改成抽屉
  var NARROW_ASIDE = window.matchMedia ? window.matchMedia('(max-width: 860px)') : null;

  var ASIDE_KEY = 'qf.scratch.aside';
  var ASIDE_W_KEY = 'qf.scratch.aside.w';
  var TAG_KEY = 'qf.scratch.tag';
  var FOLD_KEY = 'qf.scratch.folds';
  var ASIDE_W_MIN = 180;
  var ASIDE_W_MAX = 420;

  function isNarrow() {
    return !!(NARROW && NARROW.matches);
  }

  function store(key, value) {
    try {
      if (value === null) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, value);
    } catch (err) {
      /* 存不下就只在这次生效 */
    }
  }

  function recall(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (err) {
      return null;
    }
  }

  var state = {
    //: 当前这一页，**旧 → 新**（服务端就是这个顺序）
    notes: [],
    //: 当前筛选下的条数 / 全部条数
    total: 0,
    all: 0,
    //: 标签树（服务端给，父节点含子节点）
    tags: [],
    //: 选中的标签路径（'' = 全部）
    tag: recall(TAG_KEY) || '',
    //: 读回来过没有（空列表与"还没读"要分得开）
    ready: false,
    //: 上一次读失败了（要如实说"没读出来"，而不是显示"还没记过"）
    failed: false,
    //: 正在编辑的那条 id。编辑期间跳过所有重画 —— 否则编辑框会被拆掉
    editing: '',
    //: 上次读回来的指纹：定时对账只在**内容真的变了**时才重画
    sig: '',
    tagSig: '',
    //: 往 DOM 里画过一次没有
    painted: false,
    //: 左栏开着没有（窄屏默认收起，见 `applyNarrowAside`）
    asideOpen: recall(ASIDE_KEY) !== 'closed',
    asideAuto: false,
    //: 哪些标签被**收起**了（默认全展开：碎片的标签通常就几条）
    folds: readFolds(),
  };

  function readFolds() {
    try {
      var raw = JSON.parse(recall(FOLD_KEY) || '{}');
      return raw && typeof raw === 'object' ? raw : {};
    } catch (err) {
      return {};
    }
  }

  function saveFolds() {
    store(FOLD_KEY, JSON.stringify(state.folds));
  }

  var el = {
    root: null,
    fold: null,
    count: null,
    filter: null,
    body: null,
    scrim: null,
    aside: null,
    tagBox: null,
    stream: null,
    input: null,
    send: null,
  };
  var busy = false;
  var loading = false;
  //: 上一次读还没回来时又要求读一次（刚点完标签、刚记下一条）：回来后再补一次
  var pendingLoad = false;

  /* ------------------------------------------------------------ 小工具 */

  /** 32 位十六进制的随机 id。
   *
   * 为什么 id 由前端生成：发送失败重试、两个标签页各点一次，都只会落成一行
   *（服务端按 id 幂等）；随机串换台机器也不会撞号 —— 它同时是同步需要的稳定身份。
   * 有 `crypto.getRandomValues` 就用它，没有（老浏览器 / 非安全上下文）退到 Math.random。
   */
  function fragId() {
    var bytes = new Uint8Array(16);
    if (window.crypto && window.crypto.getRandomValues) {
      window.crypto.getRandomValues(bytes);
    } else {
      for (var i = 0; i < 16; i++) bytes[i] = Math.floor(Math.random() * 256);
    }
    var out = '';
    for (var j = 0; j < 16; j++) out += (bytes[j] + 256).toString(16).slice(1);
    return out;
  }

  function pad(n) {
    return n < 10 ? '0' + n : String(n);
  }

  function sameDay(a, b) {
    return (
      a.getFullYear() === b.getFullYear() &&
      a.getMonth() === b.getMonth() &&
      a.getDate() === b.getDate()
    );
  }

  /** 时间：今天只给时分，昨天给"昨天"，同年给月日，跨年才给年份。
   *
   * 与 QQ / flomo 同一个取舍：日期是**定位**用的，不是每行都要读的信息。
   */
  function fragTime(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    var now = new Date();
    var hm = pad(d.getHours()) + ':' + pad(d.getMinutes());
    if (sameDay(d, now)) return hm;
    var yesterday = new Date(now);
    yesterday.setDate(yesterday.getDate() - 1);
    if (sameDay(d, yesterday)) return '昨天 ' + hm;
    var md = d.getMonth() + 1 + '月' + d.getDate() + '日 ' + hm;
    return d.getFullYear() === now.getFullYear() ? md : d.getFullYear() + '年' + md;
  }

  /** 输入框的提示语：手机上不需要教"Shift + Enter"（没有那个键）。 */
  function placeholder() {
    return isNarrow() ? '记一句…' : '记一句…（Enter 记下，Shift + Enter 换行）';
  }

  function copyText(text) {
    var done = function () {
      ui.toast('已复制', 'ok', 1600);
    };
    var fail = function () {
      ui.toast('复制不了，手动选一下吧', 'warn', 2200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, fail);
      return;
    }
    // 非安全上下文（http 的局域网地址）里没有 clipboard API：退回老办法。
    var area = h('textarea', { style: 'position:fixed;left:-9999px;top:0' });
    area.value = text;
    document.body.appendChild(area);
    area.select();
    try {
      document.execCommand('copy') ? done() : fail();
    } catch (e) {
      fail();
    }
    area.remove();
  }

  /* ------------------------------------------------------------ 骨架 */

  /** 壳只搭一次：顶条、左栏、气泡流、输入条。
   *
   * 重画只换**内容**（标签树 / 气泡流）—— 整块重建会把输入框里正在写的字一起冲掉，
   * 而定时对账每 30 秒就来一次（见 `watch`）。
   */
  function build() {
    el.root = document.getElementById('scratch-root');
    if (!el.root) return;

    el.fold = h('button.scratch__fold', {
      type: 'button',
      draggable: 'false',
      onClick: function () {
        setAside(!state.asideOpen);
      },
    });
    el.count = h('span.scratch__count');
    el.filter = h('span.scratch__filter', { hidden: true });
    el.root.appendChild(
      h(
        'header.scratch__bar',
        null,
        el.fold,
        h('h1.scratch__title', { text: '碎片' }),
        el.filter,
        el.count
      )
    );

    el.tagBox = h('div.scratch__tags');
    el.aside = h(
      'aside.scratch__aside',
      { 'aria-label': '标签' },
      h('div.scratch__asidehead', null, h('span.scratch__asideTitle', { text: '标签' })),
      el.tagBox,
      // 右边那条线可拖（与对话页左栏同一个手柄，见 `dragAsideWidth`）
      h('div.scratch__grip', {
        title: '拖动调整宽度（双击复位）',
        'aria-hidden': 'true',
        onPointerdown: dragAsideWidth,
        onDblclick: function () {
          setAsideWidth(0, true);
        },
      })
    );

    el.stream = h('div.scratch__stream', { role: 'log', 'aria-live': 'polite' });
    el.input = h('textarea.scratch__input', {
      rows: '1',
      // Enter 记下、Shift+Enter 换行 —— 与 QQ 一致（用户给的那张图就是这个手感）。
      // 输入法选词时按的那一下 Enter 不算（`isComposing`）。
      placeholder: placeholder(),
      'aria-label': '记一条碎片',
      onInput: growInput,
      onKeydown: function (event) {
        if (event.key !== 'Enter' || event.shiftKey) return;
        if (event.isComposing || event.keyCode === 229) return;
        event.preventDefault();
        send();
      },
    });
    el.send = h('button.scratch__send', {
      type: 'button',
      title: '记下（Enter）',
      'aria-label': '记下',
      html: ui.icon('send', 16),
      onClick: send,
    });

    el.scrim = h('div.scratch__scrim', {
      onClick: function () {
        setAside(false);
      },
    });
    el.body = h(
      'div.scratch__body',
      null,
      el.scrim,
      el.aside,
      h(
        'main.scratch__main',
        null,
        h(
          'div.scratch__col',
          null,
          el.stream,
          h('div.scratch__composer', null, el.input, el.send)
        )
      )
    );
    el.root.appendChild(el.body);

    setAsideWidth(storedWidth(), false);
    applyAside();
  }

  /** 输入框跟着内容长高（最多 6 行左右，再多就自己滚）。 */
  function growInput() {
    if (!el.input) return;
    el.input.style.height = 'auto';
    el.input.style.height = Math.min(el.input.scrollHeight, 132) + 'px';
  }

  function paintCount() {
    if (!el.count) return;
    el.count.textContent = state.total ? state.total + ' 条' : '';
  }

  /** 顶条上那枚"正在看某个标签"的胶囊：左栏收起 / 手机上时，它是唯一的线索。 */
  function paintFilter() {
    if (!el.filter) return;
    ui.clear(el.filter);
    el.filter.hidden = !state.tag;
    if (!state.tag) return;
    el.filter.appendChild(
      h(
        'button.scratch__chip.is-on',
        {
          type: 'button',
          title: '不再按标签筛选',
          onClick: function () {
            setTag('');
          },
        },
        '#' + state.tag + '  ×'
      )
    );
  }

  /* ------------------------------------------------------------ 左栏：标签 */

  function setTag(path) {
    var next = String(path || '');
    if (next === state.tag) return;
    state.tag = next;
    store(TAG_KEY, next || null);
    state.sig = '';
    paintTags();
    paintFilter();
    // 先按旧名单占位（不闪"空"），新结果回来再换上
    load(false, true);
    if (NARROW_ASIDE && NARROW_ASIDE.matches) setAside(false);
    if (el.stream) el.stream.scrollTop = el.stream.scrollHeight;
  }

  function tagRow(node, depth) {
    var kids = (node.children || []).length;
    var folded = !!state.folds[node.path];
    var on = state.tag === node.path;

    var caret = h(
      'span.scratch__caret' + (kids ? '' : '.is-empty') + (folded ? '' : '.is-open'),
      {
        role: kids ? 'button' : 'presentation',
        tabindex: kids ? '0' : '-1',
        title: kids ? (folded ? '展开子标签' : '收起子标签') : '',
        onClick: function (event) {
          if (!kids) return;
          event.stopPropagation();
          state.folds[node.path] = !folded;
          saveFolds();
          paintTags();
        },
        onKeydown: function (event) {
          if (!kids) return;
          if (event.key !== 'Enter' && event.key !== ' ') return;
          event.preventDefault();
          event.stopPropagation();
          state.folds[node.path] = !folded;
          saveFolds();
          paintTags();
        },
        html: kids ? ui.icon('chevronR', 11) : '',
      }
    );

    var row = h(
      'div.scratch__tagrow' + (on ? '.is-on' : ''),
      {
        role: 'button',
        tabindex: '0',
        title: '只看 #' + node.path,
        onClick: function () {
          setTag(node.path);
        },
        onKeydown: function (event) {
          if (event.key !== 'Enter' && event.key !== ' ') return;
          event.preventDefault();
          setTag(node.path);
        },
      },
      caret,
      h('span.scratch__hash', { text: '#' }),
      h('span.scratch__tagname', { text: node.name }),
      h('span.scratch__tagcount', { text: String(node.count) })
    );
    row.style.paddingLeft = 6 + depth * 14 + 'px';
    return row;
  }

  function paintTags() {
    if (!el.tagBox) return;
    ui.clear(el.tagBox);

    // "全部"是第一行：它是这一栏的默认落点，也是"退出去"的唯一办法
    el.tagBox.appendChild(
      h(
        'div.scratch__tagrow.is-all' + (state.tag ? '' : '.is-on'),
        {
          role: 'button',
          tabindex: '0',
          title: '全部碎片',
          onClick: function () {
            setTag('');
          },
          onKeydown: function (event) {
            if (event.key !== 'Enter' && event.key !== ' ') return;
            event.preventDefault();
            setTag('');
          },
        },
        h('span.scratch__caret.is-empty'),
        // 占住"#"那一格的宽度：全部不是标签，但名字要与标签名同一条竖线
        h('span.scratch__hash.is-empty'),
        h('span.scratch__tagname', { text: '全部' }),
        h('span.scratch__tagcount', { text: String(state.all) })
      )
    );

    if (!state.tags.length) {
      // 还没读回来时不要先说"没有标签" —— 那是两回事
      if (state.ready) {
        el.tagBox.appendChild(
          h('p.scratch__taghint', { text: '在正文里写 #标签，这里就会长出来。' })
        );
      }
      return;
    }

    (function walk(nodes, depth) {
      nodes.forEach(function (node) {
        el.tagBox.appendChild(tagRow(node, depth));
        if ((node.children || []).length && !state.folds[node.path]) {
          walk(node.children, depth + 1);
        }
      });
    })(state.tags, 0);
  }

  /* ------------------------------------------------------------ 气泡流 */

  /** 只重画气泡流。`stick` = 画完滚到最底下（刚记下一条 / 第一次画）。 */
  function paint(stick) {
    if (!el.stream) return;
    // 编辑中一律不重画：那会把编辑框连同没保存的字一起拆掉。
    if (state.editing) return;

    var box = el.stream;
    var atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 64;
    ui.clear(box);

    if (!state.ready) {
      box.appendChild(h('p.scratch__hint', { text: '正在读取…' }));
    } else if (state.failed && !state.notes.length) {
      box.appendChild(
        h('p.scratch__hint', { text: '没读出来 —— 本地服务可能没响应，切回来时会再试。' })
      );
    } else if (!state.notes.length) {
      box.appendChild(
        h(
          'div.scratch__empty',
          null,
          h('p.scratch__empty-line', {
            text: state.tag ? '这个标签下还没有碎片。' : '还没记过什么。',
          }),
          h('p.scratch__hint', {
            text: state.tag
              ? '换个标签，或者点左栏的「全部」看看。'
              : '想到一句就写下来 —— 它不是待办，也不用整理。写 #标签 可以归类。',
          })
        )
      );
    } else {
      // 一条撑不满时**贴底**（QQ 那种：话在输入框上方，不是从顶上往下长）。
      // 用一个 0 高的垫片吃掉多余空间，而不是 `justify-content: flex-end` ——
      // 后者在可滚容器里有"内容超高时第一屏看不到开头"的老毛病。
      box.appendChild(h('div.frag__pad'));
      state.notes.forEach(function (note) {
        box.appendChild(fragRow(note));
      });
    }

    paintCount();
    state.painted = true;
    if (stick || atBottom) box.scrollTop = box.scrollHeight;
  }

  /** 正文里的 `#标签` 画成可以点的胶囊。
   *
   * 标签的**解析**在服务端（`app/scratch_tags.py`），这里只按它给的那几条去认原位 ——
   * 前端不写第二套正则，否则两边迟早对不上（"正文里有、点它却筛不出来"）。
   */
  function bodyNode(text, tags) {
    var pool = (tags || [])
      .slice()
      .sort(function (a, b) {
        return b.length - a.length; // 长的优先：`#A/B` 不该先被 `#A` 吃掉
      });
    var frag = document.createDocumentFragment();
    var plain = '';
    var i = 0;
    while (i < text.length) {
      var hit = '';
      if (text.charAt(i) === '#') {
        for (var k = 0; k < pool.length; k += 1) {
          var needle = '#' + pool[k];
          if (text.substr(i, needle.length) === needle) {
            hit = needle;
            break;
          }
        }
      }
      if (!hit) {
        plain += text.charAt(i);
        i += 1;
        continue;
      }
      if (plain) {
        frag.appendChild(document.createTextNode(plain));
        plain = '';
      }
      frag.appendChild(
        h(
          'span.frag__tag',
          {
            role: 'button',
            tabindex: '0',
            title: '只看 #' + hit.slice(1),
            onClick: function (event) {
              event.stopPropagation();
              setTag(hit.slice(1));
            },
            onKeydown: function (event) {
              if (event.key !== 'Enter' && event.key !== ' ') return;
              event.preventDefault();
              event.stopPropagation();
              setTag(hit.slice(1));
            },
            // 双击正文 = 编辑，胶囊上不该跟着打开编辑框
            onDblclick: function (event) {
              event.stopPropagation();
            },
          },
          hit
        )
      );
      i += hit.length;
    }
    if (plain) frag.appendChild(document.createTextNode(plain));
    return frag;
  }

  /** 一条：上面是时间 + "···"，下面是气泡。 */
  function fragRow(note) {
    var bubble = h('div.frag__bubble');
    bubble.appendChild(bodyNode(note.body, note.tags));
    var more = h(
      'button.frag__more',
      {
        type: 'button',
        title: '更多',
        'aria-label': '更多',
        onClick: function (event) {
          event.stopPropagation();
          var rect = more.getBoundingClientRect();
          ui.menu(
            [
              { label: '复制', icon: 'copy', run: function () { copyText(note.body); } },
              { label: '编辑', icon: 'pencil', run: function () { edit(note, row, bubble); } },
              {
                label: '删除',
                icon: 'trash',
                danger: true,
                run: function () { remove(note); },
              },
            ],
            // 菜单落在"···"左下方：右下角是屏幕边缘时，往右开会跑出去
            { x: rect.left - 96, y: rect.bottom + 4 }
          );
        },
      },
      '···'
    );

    var row = h(
      'div.frag',
      null,
      h('div.frag__meta', null, h('span.frag__time', { text: fragTime(note.createdAt) }), more),
      bubble
    );
    // 双击正文 = 改这一条（与清单里"点标题改名"同一个手势，不额外占一行按钮）
    bubble.addEventListener('dblclick', function () {
      edit(note, row, bubble);
    });
    return row;
  }

  /* ------------------------------------------------------------ 改 / 删 / 记 */

  /** 就地改：气泡变成一块输入区。Enter 留给换行，Ctrl/Cmd + Enter 存下。 */
  function edit(note, row, bubble) {
    if (state.editing || !row || !bubble) return;
    state.editing = note.id;
    row.classList.add('is-editing');
    ui.clear(bubble);

    var area = h('textarea.frag__edit', { rows: '3', 'aria-label': '改这一条' });
    area.value = note.body;
    var save = h('button.btn.btn--primary.btn--sm', { type: 'button' }, '保存');
    var cancel = h('button.btn.btn--ghost.btn--sm', { type: 'button' }, '取消');
    bubble.appendChild(area);
    bubble.appendChild(h('div.frag__editfoot', null, cancel, save));
    bubble.classList.add('is-editing');
    growEdit();
    area.addEventListener('input', growEdit);
    area.focus();
    area.setSelectionRange(area.value.length, area.value.length);

    function growEdit() {
      area.style.height = 'auto';
      area.style.height = Math.min(area.scrollHeight, 260) + 'px';
    }

    function close() {
      state.editing = '';
      row.classList.remove('is-editing');
      bubble.classList.remove('is-editing');
      paint(false);
      paintTags();
    }

    function commit() {
      var text = String(area.value || '').trim();
      if (!text) {
        ui.toast('空的一条不留', 'warn', 1800);
        return;
      }
      if (text === note.body) {
        close();
        return;
      }
      save.disabled = true;
      api.patch('/scratch/' + encodeURIComponent(note.id), { body: text }).then(
        function (res) {
          var fresh = (res && res.note) || null;
          if (fresh) {
            state.notes.forEach(function (one, index) {
              if (one.id === fresh.id) state.notes[index] = fresh;
            });
          }
          // 下一次对账重新对一遍（改了正文，指纹要跟着变）
          state.sig = '';
          // 标签可能改了（加了一个 #、删了一个 #）：重读一次，左栏跟着变
          state.tagSig = '';
          close();
          load(true);
        },
        function (err) {
          save.disabled = false;
          ui.toast((err && err.message) || '没改成', 'bad', 2600);
        }
      );
    }

    save.addEventListener('click', commit);
    cancel.addEventListener('click', close);
    area.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') {
        // 只关编辑，不把底下的东西也收走
        event.stopPropagation();
        close();
        return;
      }
      if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        commit();
      }
    });
  }

  function remove(note) {
    ui.confirm('删掉这条碎片？', { danger: true, okLabel: '删掉' }).then(function (yes) {
      if (!yes) return;
      api.del('/scratch/' + encodeURIComponent(note.id)).then(
        function () {
          state.notes = state.notes.filter(function (one) {
            return one.id !== note.id;
          });
          state.total = Math.max(0, state.total - 1);
          state.all = Math.max(0, state.all - 1);
          state.sig = '';
          state.tagSig = '';
          paint(false);
          paintTags();
          // 删掉最后一条带某个标签的碎片时，那支标签也该跟着消失
          load(true);
        },
        function (err) {
          ui.toast((err && err.message) || '没删掉', 'bad', 2600);
        }
      );
    });
  }

  /** 记下一条。id 先由前端生成 —— 失败重试也只落一行（见 `fragId`）。 */
  function send() {
    if (!el.input || busy) return;
    var text = String(el.input.value || '').trim();
    if (!text) return;
    busy = true;
    el.send.disabled = true;
    api.post('/scratch', { id: fragId(), body: text }).then(
      function (res) {
        busy = false;
        el.send.disabled = false;
        var note = res && res.note;
        if (note) {
          // 记下的这条可能因为当前的标签筛选而不该出现（比如筛着 #A 却写了一条 #B）：
          // 那就只更新计数，不硬塞进这一屏 —— 否则"筛着还冒出别的"会让人怀疑筛选坏了。
          var fits = !state.tag || (note.tags || []).some(function (path) {
            return path === state.tag || path.indexOf(state.tag + '/') === 0;
          });
          if (fits) {
            var known = state.notes.some(function (one) {
              return one.id === note.id;
            });
            if (!known) {
              state.notes.push(note);
              // 时间可能因为本机时钟略快而排到前面：按时间重排一次
              state.notes.sort(function (a, b) {
                return String(a.createdAt || '').localeCompare(String(b.createdAt || '')) ||
                  String(a.id).localeCompare(String(b.id));
              });
            }
            if (res.created && !known) state.total += 1;
          }
          if (res.created) state.all += 1;
          state.tagSig = ''; // 可能长出了新标签
        }
        el.input.value = '';
        growInput();
        paint(true);
        paintTags();
        el.input.focus();
        // 对一次账：新的一条可能长出新的标签（左栏的条数与树要跟上），
        // 而本地只在"它符合当前筛选"时才乐观塞进这一屏。
        load(true);
      },
      function (err) {
        busy = false;
        el.send.disabled = false;
        // 字留在框里：一次网络抖动不该把刚想好的那句吞掉
        ui.toast((err && err.message) || '没记上', 'bad', 2800);
      }
    );
  }

  /* ------------------------------------------------------------ 读 / 对账 */

  /** 读一页。`quiet` = 后台对账（失败不弹 toast）；`force` = 无视指纹重画。 */
  function load(quiet, force) {
    if (loading) {
      pendingLoad = true;
      return Promise.resolve();
    }
    loading = true;
    var url = '/scratch?limit=200';
    if (state.tag) url += '&tag=' + encodeURIComponent(state.tag);
    return api
      .get(url)
      .then(
        function (res) {
          var rows = (res && res.notes) || [];
          state.total = Number((res && res.total) || 0);
          state.all = Number((res && res.all) || rows.length) || 0;
          var tags = (res && res.tags) || [];
          var tagSig = JSON.stringify(tags);
          var sig = rows
            .map(function (one) {
              return one.id + '@' + one.updatedAt;
            })
            .join('|');
          var changed = !state.painted || sig !== state.sig || !!force;
          state.notes = rows;
          state.sig = sig;
          state.ready = true;
          state.failed = false;
          if (tagSig !== state.tagSig) {
            state.tagSig = tagSig;
            state.tags = tags;
            paintTags();
          }
          if (changed) paint(false);
          else paintCount();
        },
        function (err) {
          var first = !state.ready;
          state.ready = true;
          state.failed = true;
          if (first) paint(false);
          if (!quiet) {
            ui.toast('碎片没读出来：' + ((err && err.message) || '本地服务没响应'), 'bad', 2600);
          }
        }
      )
      .then(function () {
        loading = false;
        if (pendingLoad) {
          pendingLoad = false;
          load(true, false);
        }
      });
  }

  /** 每 30 秒对一次账：别的设备上的改动要能自己出现。
   *
   * 为什么是 30 秒而不是长连接：服务端的 Turso 同步本身按秒在跑（`autosync`），
   * 这一页不是聊天窗口，晚半分钟看到一句碎片没有代价；而重画会动滚动条，
   * 太勤反而打扰。只在页面可见、且没在编辑时问。
   */
  function watch() {
    window.setInterval(function () {
      if (document.visibilityState === 'hidden') return;
      if (!state.ready || state.editing || busy || loading) return;
      load(true);
    }, 30000);

    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState !== 'visible') return;
      if (state.editing || busy) return;
      load(true);
    });

    if (NARROW && NARROW.addEventListener) {
      NARROW.addEventListener('change', function () {
        if (el.input) el.input.placeholder = placeholder();
      });
    }
    if (NARROW_ASIDE && NARROW_ASIDE.addEventListener) {
      NARROW_ASIDE.addEventListener('change', applyNarrowAside);
    }
  }

  /* ------------------------------------------------------------ 左栏：开合与宽度 */

  /** 窄屏时**主动**收起（与对话页同一条规矩）：这是环境决定的，不写偏好。
   *  回到宽屏照旧按他存的偏好展开。 */
  function applyNarrowAside() {
    if (!el.body) return;
    if (NARROW_ASIDE && NARROW_ASIDE.matches && state.asideOpen) {
      state.asideAuto = true;
      state.asideOpen = false;
      applyAside();
    } else if (state.asideAuto && !(NARROW_ASIDE && NARROW_ASIDE.matches)) {
      state.asideAuto = false;
      state.asideOpen = true;
      applyAside();
    }
  }

  function applyAside() {
    if (!el.body) return;
    el.body.setAttribute('data-aside', state.asideOpen ? 'open' : 'closed');
    if (el.fold) {
      el.fold.innerHTML = ui.icon(state.asideOpen ? 'chevronL' : 'chevronR', 15);
      el.fold.title = (state.asideOpen ? '收起标签栏' : '展开标签栏') + '（⌘/Ctrl+B）';
      el.fold.setAttribute('aria-expanded', state.asideOpen ? 'true' : 'false');
    }
  }

  /** 开合的唯一落点（按钮与 ⌘B 都走它）。 */
  function setAside(open) {
    state.asideAuto = false;
    state.asideOpen = !!open;
    store(ASIDE_KEY, state.asideOpen ? 'open' : 'closed');
    applyAside();
  }

  function storedWidth() {
    try {
      var raw = parseInt(recall(ASIDE_W_KEY) || '', 10);
      return isFinite(raw) && raw > ASIDE_W_MIN ? raw : 0;
    } catch (err) {
      return 0;
    }
  }

  /** 宽度只落在 `--scratch-aside-w` 这一个变量上（CSS 的栅格读它）——
   *  不在 JS 里改 grid-template-columns，否则收起那几条规则就管不住它了。 */
  function setAsideWidth(px, save) {
    var root = document.documentElement;
    if (!px) {
      root.style.removeProperty('--scratch-aside-w');
    } else {
      root.style.setProperty(
        '--scratch-aside-w',
        Math.max(ASIDE_W_MIN, Math.min(ASIDE_W_MAX, Math.round(px))) + 'px'
      );
    }
    if (!save) return;
    if (px) store(ASIDE_W_KEY, String(Math.round(px)));
    else store(ASIDE_W_KEY, null);
  }

  /** 抓住那条右边线左右拖（与对话页的左栏同一个手法）。 */
  function dragAsideWidth(ev) {
    if (!el.aside) return;
    ev.preventDefault();
    var startX = ev.clientX;
    var startW = el.aside.getBoundingClientRect().width;
    var move = function (e) {
      setAsideWidth(startW + (e.clientX - startX), false);
    };
    var up = function () {
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', up);
      document.body.classList.remove('is-colresize');
      setAsideWidth(el.aside ? el.aside.getBoundingClientRect().width : 0, true);
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', up);
    document.body.classList.add('is-colresize');
  }

  // ⌘/Ctrl+B = 收起 / 展开标签栏（与对话页同一套手感）
  document.addEventListener('keydown', function (ev) {
    if (!(ev.metaKey || ev.ctrlKey) || ev.altKey || ev.shiftKey) return;
    if (ev.key !== 'b' && ev.key !== 'B') return;
    var node = document.activeElement;
    if (node && node.isContentEditable) return;
    ev.preventDefault();
    setAside(!state.asideOpen);
  });

  function boot() {
    QF.shell.mount({});
    build();
    if (!el.root) return;
    // 从顶上往下拉 = 立刻同步（与其它页同一个手势，见 pullsync.js）
    if (QF.pullSync && el.stream) QF.pullSync.attach(el.stream, { onDone: load });
    applyNarrowAside();
    paintTags();
    paintFilter();
    load();
    watch();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
