/* library.js —— 资料页（2026-11-08）：云端资料的管理台。
 *
 * 三块：
 *   ① 流水线   —— 一条横线串起解析 / 切片 / 向量 / 完成（切片即正文，不另算一步）
 *   ② 资料全貌 —— 矩形树图（squarified），面积 = 行数，颜色 = 类别
 *   ③ 任务队列 —— 上传的文件逐份处理，可暂停整条流水线、取消单条、失败重试
 *
 * 数据：`GET /api/library/overview`（KPI + 流水线 + 云端）、`GET /api/library/treemap`、
 * `GET /api/library/jobs`。写入：`POST /jobs/*`、`POST /push`、`DELETE /materials/{slug}`。
 *
 * 不引入任何图表库 —— 树图那一百来行 squarify 比引一个 d3 划算，而且配色/字体
 * 跟着主题走。
 */
(function () {
  'use strict';

  var api = QF.api;
  var ui = QF.ui;

  var state = {
    overview: null,
    treemap: null,
    jobs: null,
    uploadDir: '',
    picked: '',
    // 默认列列表：矩阵树是"看一眼全貌"的东西，日常是逐份处理（用户 2026-11-08 要求）
    view: 'list',
    paused: false,
    timer: null,
  };

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function fmtBytes(bytes) {
    var n = Number(bytes) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
    if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
    return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB';
  }

  function fmtCount(n) {
    var v = Number(n) || 0;
    return v >= 10000 ? (v / 1000).toFixed(1) + 'k' : String(v);
  }

  function fmtWhen(seconds) {
    var at = Number(seconds) || 0;
    if (!at) return '—';
    var d = new Date(at * 1000);
    var pad = function (x) { return x < 10 ? '0' + x : String(x); };
    return pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  /* ------------------------------------------------------------ KPI */

  function renderKpi(data) {
    var host = document.getElementById('lib-kpi');
    if (!host) return;
    host.textContent = '';
    var local = data.local || {};
    var cloud = data.cloud || {};

    [
      { label: '材料', value: fmtCount(local.materials), unit: '份', hint: '库里的材料数' },
      { label: '切片', value: fmtCount(local.slices), unit: '片', hint: '切分后的段落数' },
      { label: '图', value: fmtCount(local.figures), unit: '张', hint: '材料里的插图' },
      // **不显示向量**：它是纯派生数据，只在本机（`make embed` 重算），云端那份包里
      // 没有 —— 摆在「云端」旁边一排，等于暗示"云端也有这么多向量"。要看本机向量
      // 条数走 `make ops`；流水线里的「向量」那一步仍在，那是本机执行状态。
      { label: '云端', value: fmtCount(cloud.materials), unit: '份', cloud: true,
        hint: '云端包：' + (cloud.reachable ? fmtBytes(cloud.bytes) + ' · ' + fmtWhen(cloud.pushedAt) : '连不上') }
    ].forEach(function (c) {
      var row = el('div', 'libfact' + (c.cloud ? ' is-cloud' : ''));
      row.appendChild(el('i', 'libfact__dot'));
      row.appendChild(el('span', 'libfact__label', c.label));
      var val = el('span', 'libfact__value', c.value);
      if (c.unit) val.appendChild(el('span', 'libfact__unit', c.unit));
      row.appendChild(val);
      row.title = c.hint || '';
      host.appendChild(row);
    });
  }

  /* ------------------------------------------------------------ 流水线（O=O=O=O） */

  function renderFlow(data) {
    var host = document.getElementById('lib-flow');
    if (!host) return;
    host.textContent = '';

    var jobs = (data && data.jobs) || [];
    var current = null;
    for (var i = 0; i < jobs.length; i++) {
      if (jobs[i].status === 'running') { current = jobs[i]; break; }
    }

    // 空闲时**照样画这条流水线**，只是全灰 —— 不写"空闲…"那类字（页面上已经有
    // 三处重复的"空"了：右上角、这里、队列里）。
    // 「正文」不是一步：切片那一步就把正文一并截好写进切片了（见 api/app/ingest.py）
    var seq = ['parse', 'slice', 'embed', 'done'];
    var labels = { parse: '解析', slice: '切片', embed: '向量', done: '完成' };
    var at = -1;
    if (current) {
      for (var j = 0; j < seq.length; j++) {
        if (seq[j] === (current.stage || 'parse')) { at = j; break; }
      }
      if (at < 0) at = 0;
    }

    // 一条横线串四步：圆点落在线上，名字挂在圆点底下。
    // 走完的部分单独覆一层，宽度 = 当前节点在线上 的位置（0 / 33 / 66 / 100）。
    var stops = [0, 33, 66, 100];
    var rail = el('div', 'libflow__rail');
    var done = el('i', 'libflow__done');
    if (at > 0) done.style.width = stops[at] + '%';
    rail.appendChild(done);
    host.appendChild(rail);

    var spots = ['12%', '37.3%', '62.7%', '88%'];
    seq.forEach(function (key, index) {
      var box = el('div', 'libstep' + (index < at ? ' is-done' : index === at ? ' is-active' : ''));
      box.style.left = spots[index];
      box.appendChild(el('span', 'libstep__dot', index < at ? '✓' : String(index + 1)));
      box.appendChild(el('div', 'libstep__label', labels[key]));
      host.appendChild(box);
    });

    if (current) host.appendChild(el('div', 'libflow__now', '正在处理：' + current.rel));
  }

  /* ------------------------------------------------------------ 矩形树图 */

  /** squarified treemap：把 items 铺进 (x,y,w,h)，面积与 value 成正比。 */
  function squarify(items, x, y, w, h) {
    var out = [];
    var rest = items.filter(function (i) { return i.value > 0; });
    if (!rest.length || w <= 0 || h <= 0) return out;

    var total = rest.reduce(function (s, i) { return s + i.value; }, 0);
    var scale = (w * h) / total;
    var rx = x, ry = y, rw = w, rh = h;

    function worst(row, rowSum, side) {
      var areas = row.map(function (i) { return i.value * scale; });
      var sum = rowSum * scale;
      var max = Math.max.apply(null, areas);
      var min = Math.min.apply(null, areas);
      var s2 = sum * sum;
      return Math.max((side * side * max) / s2, s2 / (side * side * min));
    }

    while (rest.length) {
      var side = Math.min(rw, rh);
      var row = [rest[0]];
      var rowSum = rest[0].value;
      var score = worst(row, rowSum, side);

      while (row.length < rest.length) {
        var cand = rest[row.length];
        var nextSum = rowSum + cand.value;
        var nextScore = worst(row.concat([cand]), nextSum, side);
        if (nextScore > score) break;
        row.push(cand);
        rowSum = nextSum;
        score = nextScore;
      }

      var area = rowSum * scale;
      if (rw >= rh) {
        var colW = area / rh;
        var oy = ry;
        row.forEach(function (item) {
          var hh = (item.value / rowSum) * rh;
          out.push({ item: item, rect: [rx, oy, colW, hh] });
          oy += hh;
        });
        rx += colW;
        rw -= colW;
      } else {
        var rowH = area / rw;
        var ox = rx;
        row.forEach(function (item) {
          var ww = (item.value / rowSum) * rw;
          out.push({ item: item, rect: [ox, ry, ww, rowH] });
          ox += ww;
        });
        ry += rowH;
        rh -= rowH;
      }
      rest = rest.slice(row.length);
    }
    return out;
  }

  /* 类别色：不做色相均分的彩虹（八类以上就是一圈荧光），而是一小组压过饱和度
   * 的色。相邻两类仍分得开，白字也还看得清。同一类只给同一色相，靠亮度分深浅。 */
  var PALETTE = [
    [214, 30],  // 石板蓝
    [160, 26],  // 鼠尾草绿
    [20, 34],   // 陶土
    [276, 24],  // 藕紫
    [192, 28],  // 黛青
    [44, 30],   // 赭黄
    [352, 30],  // 砖红
    [96, 22],   // 橄榄
  ];

  function hueOf(index) {
    return PALETTE[index % PALETTE.length];
  }

  function groupColor(index) {
    var p = hueOf(index);
    return 'hsl(' + p[0] + ', ' + p[1] + '%, 46%)';
  }

  /** 同类里第 i / n 份：亮度 46% → 34%，同一组内深浅成阶，组外仍是各组的色相。 */
  function cellColor(index, i, n) {
    var p = hueOf(index);
    var t = n <= 1 ? 0 : i / (n - 1);
    return 'hsl(' + p[0] + ', ' + Math.min(44, p[1] + 6) + '%, ' + (46 - t * 12) + '%)';
  }

  function renderTree(data) {
    var host = document.getElementById('lib-tree');
    var legend = document.getElementById('lib-legend');
    if (!host || !legend) return;
    // 切到列表视图时树图是隐藏的：此时 clientWidth 为 0，画了也是空的，别动它。
    if (host.hidden || !host.clientWidth) return;
    host.textContent = '';
    legend.textContent = '';

    var groups = (data && data.groups) || [];
    if (!groups.length) {
      host.appendChild(el('div', 'libtree__empty', '还没有材料 —— 拖一个文件进来'));
      return;
    }

    var w = host.clientWidth;
    var h = host.clientHeight;

    // 第一层：类别。面积 = 该类总行数；没有行数的材料各算 1，免得整类缩没。
    var blocks = groups.map(function (group, gi) {
      var mats = (group.materials || []).slice();
      var items = mats.map(function (m) {
        return { value: Math.max(1, Number(m.lines) || 1), data: m };
      });
      items.sort(function (a, b) { return b.value - a.value; });
      var sum = items.reduce(function (s, item) { return s + item.value; }, 0);
      return {
        value: Math.max(1, Number(group.lines) || sum || 1),
        subject: group.subject,
        count: mats.length,
        items: items,
        gi: gi,
        color: groupColor(gi),
      };
    });
    blocks.sort(function (a, b) { return b.value - a.value; });

    // 第二层：类别块里再铺材料。类名占顶上 16px 一条窄带，材料铺在剩下的地方。
    squarify(blocks, 0, 0, w, h).forEach(function (blockCell) {
      var r = blockCell.rect;
      var block = blockCell.item;
      if (r[2] < 2 || r[3] < 2) return;

      var head = r[2] >= 88 && r[3] >= 46 ? 13 : 0;
      if (head) {
        var bar = el('div', 'libtree__group');
        bar.style.left = r[0] + 'px';
        bar.style.top = r[1] + 'px';
        bar.style.width = r[2] + 'px';
        bar.style.height = head + 'px';
        var dot = el('i', 'libtree__dot');
        dot.style.background = block.color;
        bar.appendChild(dot);
        bar.appendChild(el('span', 'libtree__subject', block.subject));
        if (r[2] > 190) bar.appendChild(el('span', 'libtree__count', block.count + ' 份'));
        host.appendChild(bar);
      }

      squarify(block.items, r[0], r[1] + head, r[2], r[3] - head).forEach(function (cell, ci) {
        var rect = cell.rect;
        var m = cell.item.data;
        if (rect[2] < 2 || rect[3] < 2) return;

        var node = el('div', 'libtree__cell');
        node.style.left = rect[0] + 'px';
        node.style.top = rect[1] + 'px';
        node.style.width = rect[2] + 'px';
        node.style.height = rect[3] + 'px';
        node.style.background = cellColor(block.gi, ci, block.items.length);
        node.title = m.title + '\n' + block.subject + ' · ' + m.lines + ' 行 · ' +
          m.slices + ' 片 · ' + m.vectors + ' 向量' + (m.figures ? ' · ' + m.figures + ' 图' : '');

        if (rect[2] > 46 && rect[3] > 19) {
          node.appendChild(el('span', 'libtree__name', m.title));
        }
        if (rect[2] > 58 && rect[3] > 36) {
          node.appendChild(el('span', 'libtree__badge', fmtCount(m.lines) + ' 行'));
        }
        if (state.picked === m.slug) node.classList.add('is-picked');
        node.addEventListener('click', function () { pick(m.slug); });
        node.addEventListener('contextmenu', function (ev) { openMaterialMenu(m, ev); });
        host.appendChild(node);
      });
    });

    groups.forEach(function (group, gi) {
      var item = el('span');
      var dot = el('i');
      dot.style.background = groupColor(gi);
      item.appendChild(dot);
      item.appendChild(document.createTextNode(group.subject + '（' + group.materials.length + '）'));
      legend.appendChild(item);
    });
  }

  /** 选中的那一份：就地长在看板标题行里，没选就整条不出现
   *（底数左栏已经说过，再说一遍就是冗余）。 */
  function renderPick(data) {
    var host = document.getElementById('lib-pick');
    if (!host) return;
    host.textContent = '';

    var groups = (data && data.groups) || [];
    var found = null;
    groups.forEach(function (group) {
      (group.materials || []).forEach(function (m) {
        if (m.slug === state.picked) found = { group: group.subject, m: m };
      });
    });

    if (!found) {
      host.hidden = true;
      return;
    }

    host.hidden = false;
    var m = found.m;
    host.appendChild(el('span', 'libpicked__name', m.title));
    host.appendChild(el('span', 'libpicked__meta',
      found.group + ' · ' + fmtCount(m.lines) + ' 行 · ' + m.slices + ' 片 · ' +
      m.vectors + ' 向量' + (m.figures ? ' · ' + m.figures + ' 图' : '')));
  }

  /** 选中某一份（空串 = 什么都不选）：树图、列表、详情条一起重画，三处高亮永远一致。 */
  function select(slug) {
    state.picked = slug;
    if (!state.treemap) return;
    renderTree(state.treemap);
    renderList(state.treemap);
    renderPick(state.treemap);
  }

  /** 左键：选中 / 取消选中。 */
  function pick(slug) {
    select(state.picked === slug ? '' : slug);
  }

  /** 把一份材料从库里删掉（右键菜单里的那一条）。
   *
   * 删除的入口只在右键菜单里（用户 2026-11-08："现在删除是在顶上点击删除的
   * 那个文字，改成在列表/矩形框里右键菜单选中"）—— 一份材料的动作挂在它自己
   * 身上，而不是挂在上面某条选中信息里。 */
  function removeMaterial(m) {
    ui.confirm('原文件保留；云端那份要再点「同步到云端」才更新。', {
      title: '把《' + m.title + '》从库里删掉？',
      okLabel: '删除',
      danger: true,
    }).then(function (ok) {
      if (!ok) return;
      api.del('/library/materials/' + encodeURIComponent(m.slug)).then(function (res) {
        ui.toast('已删除：' + m.slug + '（' + res.slices + ' 片）', 'good', 2400);
        state.picked = '';
        load();
      }).catch(function (err) {
        ui.toast('删不掉：' + ((err && err.message) || '未知原因'), 'bad', 3200);
      });
    });
  }

  /** 材料（列表行 / 矩形框）的右键菜单。右键同时把它选中，于是标题行里
   * 写着要删的是哪一份 —— 别看错了删错。 */
  function openMaterialMenu(m, ev) {
    ev.preventDefault();
    if (state.picked !== m.slug) select(m.slug);
    ui.menu(
      [{ label: '删除', icon: 'trash', danger: true, run: function () { removeMaterial(m); } }],
      { x: ev.clientX, y: ev.clientY }
    );
  }

  /* ------------------------------------------------------------ 列表视图 */

  function renderList(data) {
    var host = document.getElementById('lib-list');
    if (!host) return;
    host.textContent = '';

    var groups = (data && data.groups) || [];
    if (!groups.length) {
      host.appendChild(el('div', 'liblist__empty', '还没有材料 —— 拖一个文件进来'));
      return;
    }

    groups.forEach(function (group, gi) {
      var mats = group.materials || [];
      var block = el('div', 'liblist__group');

      var head = el('div', 'liblist__head');
      var dot = el('i');
      dot.style.background = groupColor(gi);
      head.appendChild(dot);
      head.appendChild(el('span', null, group.subject));
      head.appendChild(el('b', null, mats.length + ' 份 · ' + fmtCount(group.lines) + ' 行'));
      block.appendChild(head);

      mats.forEach(function (m, mi) {
        var row = el('div', 'liblist__row');
        var chip = el('i', 'liblist__dot');
        chip.style.background = cellColor(gi, mi, mats.length);
        row.appendChild(chip);
        row.appendChild(el('span', 'liblist__title', m.title));
        row.appendChild(el('span', 'liblist__num', fmtCount(m.lines) + ' 行'));
        row.appendChild(el('span', 'liblist__num', fmtCount(m.slices) + ' 片'));
        row.appendChild(el('span', 'liblist__num', fmtCount(m.vectors) + ' 向量'));
        row.title = m.title + '（' + m.slug + '）\n' + m.lines + ' 行 · ' + m.slices + ' 片 · ' +
          m.withText + ' 片带正文 · ' + m.vectors + ' 向量' +
          (m.figures ? ' · ' + m.figures + ' 图' : '');
        if (state.picked === m.slug) row.classList.add('is-picked');
        row.addEventListener('click', function () { pick(m.slug); });
        row.addEventListener('contextmenu', function (ev) { openMaterialMenu(m, ev); });
        block.appendChild(row);
      });

      host.appendChild(block);
    });
  }

  /* ------------------------------------------------------------ 视图切换 */

  function applyView() {
    var tree = document.getElementById('lib-tree');
    var legend = document.getElementById('lib-legend');
    var list = document.getElementById('lib-list');
    var isTree = state.view === 'tree';

    if (tree) tree.hidden = !isTree;
    if (legend) legend.hidden = !isTree;
    if (list) list.hidden = isTree;

    Array.prototype.forEach.call(
      document.querySelectorAll('#lib-views button'),
      function (btn) { btn.classList.toggle('is-on', btn.getAttribute('data-view') === state.view); }
    );

    if (!state.treemap) return;
    if (isTree) renderTree(state.treemap);
    else renderList(state.treemap);
  }

  function bindViews() {
    var host = document.getElementById('lib-views');
    if (!host) return;
    host.addEventListener('click', function (ev) {
      var btn = ev.target && ev.target.closest ? ev.target.closest('button[data-view]') : null;
      if (!btn) return;
      var view = btn.getAttribute('data-view');
      if (view === state.view) return;
      state.view = view;
      applyView();
    });
  }

  /* ------------------------------------------------------------ 任务队列 */

  function renderQueue(data) {
    var host = document.getElementById('lib-queue');
    if (!host) return;
    host.textContent = '';

    var jobs = (data && data.jobs) || [];
    var counts = (data && data.counts) || {};

    var toggle = document.getElementById('lib-toggle');
    if (toggle) {
      var paused = !!(data && data.paused);
      // 无框图标键：三角 = 继续，两道竖线 = 暂停；文字进 title
      toggle.innerHTML = ui.icon(paused ? 'play' : 'pause', 14);
      toggle.title = paused ? '继续流水线' : '暂停流水线';
      toggle.setAttribute('aria-label', toggle.title);
    }
    state.paused = !!(data && data.paused);

    if (!jobs.length) return;   // 空就空着，不再写一句"队列是空的"

    jobs.forEach(function (job) {
      var row = el('div', 'libjob is-' + job.status);
      row.appendChild(el('span', 'libjob__name', job.rel));
      var stageText = job.status === 'done' ? '完成'
        : job.status === 'failed' ? '失败'
        : job.status === 'canceled' ? '已取消'
        : (job.stageLabel || '排队中');
      row.appendChild(el('span', 'libjob__stage', stageText));
      row.title = job.message || '';

      if (job.status === 'running' || job.status === 'queued') {
        var cancel = el('button', 'libjob__btn', '取消');
        cancel.type = 'button';
        cancel.addEventListener('click', function () {
          api.post('/library/jobs/' + job.id + '/cancel', {}).then(load).catch(function () {});
        });
        row.appendChild(cancel);
      } else if (job.status === 'failed' || job.status === 'canceled') {
        var retry = el('button', 'libjob__btn', '重试');
        retry.type = 'button';
        retry.addEventListener('click', function () {
          api.post('/library/jobs/' + job.id + '/retry', {}).then(load).catch(function () {});
        });
        row.appendChild(retry);
      }
      host.appendChild(row);
    });
  }

  /* ------------------------------------------------------------ 载入 */

  function load() {
    return Promise.all([
      api.get('/library/overview').catch(function () { return null; }),
      api.get('/library/treemap').catch(function () { return null; }),
      api.get('/library/jobs').catch(function () { return null; })
    ]).then(function (res) {
      if (res[0]) {
        state.overview = res[0];
        renderKpi(res[0]);
      }
      if (res[1]) {
        state.treemap = res[1];
        renderTree(res[1]);
        renderList(res[1]);
        renderPick(res[1]);
      }
      if (res[2]) {
        state.jobs = res[2];
        renderFlow(res[2]);      // 流水线只看"此刻在处理哪一份"
        renderQueue(res[2]);
      }
    });
  }

  /* ------------------------------------------------------------ 上传 */

  function uploadOne(file) {
    return api.upload('/library/upload', file, { dir: state.uploadDir });
  }

  function uploadFiles(files) {
    if (!files || !files.length) return;
    if (!state.uploadDir) {
      ui.toast('还没挂资料根：先在设置里填一个资料目录', 'bad');
      return;
    }
    var list = Array.prototype.slice.call(files);
    var chain = Promise.resolve();
    list.forEach(function (file) {
      chain = chain.then(function () {
        return uploadOne(file).catch(function (err) {
          ui.toast(file.name + ' 上传失败：' + ((err && err.message) || ''), 'bad', 3000);
        });
      });
    });
    chain.then(function () {
      ui.toast(list.length + ' 个文件已入队', 'good', 1800);
      load();
    });
  }

  function bindUpload() {
    var drop = document.getElementById('lib-drop');
    var input = document.getElementById('lib-file');
    if (!drop || !input) return;

    drop.addEventListener('click', function () { input.click(); });
    drop.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); input.click(); }
    });
    input.addEventListener('change', function () {
      uploadFiles(input.files);
      input.value = '';
    });
    ['dragenter', 'dragover'].forEach(function (name) {
      drop.addEventListener(name, function (ev) {
        ev.preventDefault();
        drop.classList.add('is-over');
      });
    });
    ['dragleave', 'drop'].forEach(function (name) {
      drop.addEventListener(name, function (ev) {
        ev.preventDefault();
        drop.classList.remove('is-over');
      });
    });
    drop.addEventListener('drop', function (ev) {
      uploadFiles(ev.dataTransfer && ev.dataTransfer.files);
    });
  }

  /* ------------------------------------------------------------ 左栏（收起 / 拖宽 / 窄屏抽屉）
   *
   * 与对话页 `.chat__aside` 同一套手感：宽度只写 `--lib-aside-w`（CSS 两处栅格都读它），
   * 收起是"列宽收到 0 + 整栏 translateX(-100%)"，窄屏自动收起且**不记**本机
   *（那是环境决定的，窗口宽回来要自动展开）。
   */

  var ASIDE_KEY = 'qf.library.aside';
  var ASIDE_W_KEY = 'qf.library.aside.w';
  var ASIDE_W_MIN = 240;
  var ASIDE_W_MAX = 520;
  var narrow = window.matchMedia('(max-width: 900px)');
  var splitEl = null;
  var asideOpen = true;
  var autoFolded = false;   //: 这次收起是环境决定的，不是人按的

  function applyAside() {
    if (!splitEl) return;
    splitEl.setAttribute('data-aside', asideOpen ? 'open' : 'closed');
    var fold = document.getElementById('lib-fold');
    if (!fold) return;
    fold.innerHTML = ui.icon(asideOpen ? 'chevronL' : 'chevronR', 15);
    fold.title = (asideOpen ? '收起左栏' : '展开左栏') + '（⌘/Ctrl+B）';
    fold.setAttribute('aria-label', asideOpen ? '收起左栏' : '展开左栏');
    fold.setAttribute('aria-expanded', asideOpen ? 'true' : 'false');
  }

  /** 左栏开合的**唯一落点**：按钮与边缘手势都走它（只认目标态，不 toggle —
   * 连着划两下时 toggle 会跟状态赛跑）。 */
  function setAside(open) {
    autoFolded = false;   // 人（或手指）动了就以这个为准：从此不再"自动展开"
    asideOpen = !!open;
    try {
      window.localStorage.setItem(ASIDE_KEY, asideOpen ? 'open' : 'closed');
    } catch (err) {
      /* 存不下就只在这次生效 */
    }
    applyAside();
  }

  function toggleAside() {
    setAside(!asideOpen);
  }

  function asideWidthNow() {
    try {
      var raw = parseInt(window.localStorage.getItem(ASIDE_W_KEY) || '', 10);
      return isFinite(raw) && raw > ASIDE_W_MIN ? raw : 0;
    } catch (err) {
      return 0;
    }
  }

  /** 设宽度。`px = 0` = 复位（回到 CSS 默认值）；拖动中不落库，松手才写。 */
  function setAsideWidth(px, save) {
    var root = document.documentElement;
    if (!px) {
      root.style.removeProperty('--lib-aside-w');
    } else {
      root.style.setProperty(
        '--lib-aside-w',
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

  function dragAsideWidth(ev) {
    ev.preventDefault();
    var startX = ev.clientX;
    var aside = splitEl && splitEl.querySelector('.libaside');
    var startW = aside ? aside.getBoundingClientRect().width : 268;

    var move = function (e) {
      setAsideWidth(startW + (e.clientX - startX), false);
    };
    var up = function () {
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', up);
      document.body.classList.remove('is-colresize');
      var el = splitEl && splitEl.querySelector('.libaside');
      setAsideWidth(el ? el.getBoundingClientRect().width : 0, true);
      // 拖动过程中树图故意不重绘（每帧重建 188 个方块会抖），松手补一次
      if (state.treemap && state.view === 'tree') renderTree(state.treemap);
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', up);
    document.body.classList.add('is-colresize');
  }

  function bindAside() {
    splitEl = document.querySelector('.libsplit');
    if (!splitEl) return;

    var saved = null;
    try { saved = window.localStorage.getItem(ASIDE_KEY); } catch (err) { /* 无痕模式 */ }
    if (saved === 'closed' || saved === 'open') asideOpen = saved === 'open';
    if (narrow.matches) {
      asideOpen = false;
      autoFolded = true;   // 不写本机：宽回来要自动展开
    }

    applyAside();
    setAsideWidth(asideWidthNow(), false);

    var fold = document.getElementById('lib-fold');
    if (fold) fold.addEventListener('click', toggleAside);

    var grip = document.getElementById('lib-grip');
    if (grip) grip.addEventListener('pointerdown', dragAsideWidth);

    var onChange = function () {
      if (narrow.matches && asideOpen) {
        autoFolded = true;
        asideOpen = false;
        applyAside();
      } else if (!narrow.matches && autoFolded) {
        autoFolded = false;
        asideOpen = true;
        applyAside();
      }
    };
    if (narrow.addEventListener) narrow.addEventListener('change', onChange);
    else if (narrow.addListener) narrow.addListener(onChange);

    // 边缘手势：左边缘往右划展开、在抽屉上往左划收回（与对话页同一套，见 swipe.js）
    if (QF.swipe) {
      QF.swipe.bind({
        edge: 'left',
        // 与对话页同一口径：**不按宽度筛**，平板横屏也能划（用户 2026-11-10）。
        // 宽屏里左栏是常驻列，`closed` 时整列滑走（见 library.css 的 data-aside 那几条）。
        enabled: function () { return true; },
        isOpen: function () { return asideOpen; },
        set: setAside,
      });
    }

    // ⌘/Ctrl+B：与对话页同一个快捷键，焦点在可编辑区里时不抢
    document.addEventListener('keydown', function (ev) {
      if (!(ev.metaKey || ev.ctrlKey) || ev.altKey || ev.shiftKey) return;
      if (ev.key !== 'b' && ev.key !== 'B') return;
      var el = document.activeElement;
      if (el && el.isContentEditable) return;
      ev.preventDefault();
      toggleAside();
    });
  }

  /* ------------------------------------------------------------ 顶部按钮 */

  function bindActions() {
    var toggle = document.getElementById('lib-toggle');
    if (toggle) {
      toggle.addEventListener('click', function () {
        var path = state.paused ? '/library/jobs/resume' : '/library/jobs/pause';
        // 不弹提示：图标自己在 ‖ / ▶ 之间翻面，就是反馈；右下角再飘一句是重复
        api.post(path, {}).then(load).catch(function (err) {
          ui.toast('操作失败：' + ((err && err.message) || ''), 'bad');
        });
      });
    }

    // 两个动作都是图标键：图标只有一处来源（ui.js 的 ICONS），这里不另画 svg
    var refresh = document.getElementById('lib-refresh');
    if (refresh) {
      refresh.innerHTML = ui.icon('refresh', 16);
      refresh.addEventListener('click', function () { load(); });
    }

    var push = document.getElementById('lib-push');
    if (push) {
      push.innerHTML = ui.icon('upload', 16);
      push.addEventListener('click', function () {
        push.disabled = true;
        push.classList.add('is-busy');   // 转着的上传图标就是"正在同步"，不再改文案
        api.post('/library/push', {}).then(function (out) {
          ui.toast('已同步：' + out.materials + ' 份 · ' + out.slices + ' 片 · ' +
            (out.textBlocks || 0) + ' 个正文块（' + fmtBytes(out.bytes) + '）', 'good', 2800);
          return load();
        }).catch(function (err) {
          ui.toast('同步失败：' + ((err && err.message) || ''), 'bad', 3400);
        }).then(function () {
          push.disabled = false;
          push.classList.remove('is-busy');
        });
      });
    }
  }

  /* ------------------------------------------------------------ 启动 */

  function boot() {
    if (!document.querySelector('.libsplit')) return;
    QF.shell.mount({});
    bindAside();
    bindUpload();
    bindActions();
    bindViews();
    applyView();
    // 资料看板顶上往下拉 = 立刻同步（自动节拍已放宽到 30 秒，见 pullsync.js）
    if (QF.pullSync) QF.pullSync.attach(document.querySelector('.libmain'), { onDone: load });

    api.get('/library/roots').then(function (res) {
      var roots = (res && res.roots) || [];
      state.uploadDir = roots[0] || '';
      if (!state.uploadDir) {
        var drop = document.getElementById('lib-drop');
        var text = drop && drop.querySelector('.libdrop__text');
        if (text) text.textContent = '还没挂资料根 —— 先去设置里填一个目录';
      }
    }).catch(function () {});

    load();

    // 队列在跑的时候每 2 秒刷一次（只在有活动任务时轮询，空闲不打扰）
    state.timer = setInterval(function () {
      var counts = (state.jobs && state.jobs.counts) || {};
      if (counts.running || counts.queued) {
        api.get('/library/jobs').then(function (data) {
          state.jobs = data;
          renderFlow(data);
          renderQueue(data);
          if (!data.counts.running && !data.counts.queued) load();
        }).catch(function () {});
      }
    }, 2000);

    // 树图按容器实际尺寸铺方块。盯着容器自己（而不是 window）：首帧布局、切视图、
    // 侧栏变宽都会让它变，window 的 resize 只覆盖其中一种。
    var treeHost = document.getElementById('lib-tree');
    if (treeHost && window.ResizeObserver) {
      var lastWidth = treeHost.clientWidth;
      new ResizeObserver(function () {
        var width = treeHost.clientWidth;
        if (!width || width === lastWidth) return;
        lastWidth = width;
        // 拖左栏时每帧重建整棵树会抖；松手那一下由 dragAsideWidth 统一补画
        if (document.body.classList.contains('is-colresize')) return;
        if (state.treemap && state.view === 'tree') renderTree(state.treemap);
      }).observe(treeHost);
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
