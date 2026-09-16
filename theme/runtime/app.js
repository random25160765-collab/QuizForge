/* ===========================================================================
 * app.js —— 刷题应用主程序
 *
 * 四个视图：工作台 / 练习 / 组卷 / 复习，加上设置弹窗。
 * 状态全部在内存 + localStorage，没有任何服务端依赖。
 * ========================================================================= */
(function () {
  'use strict';

  var QF = window.QF;
  var ui = QF.ui;
  var h = ui.h;
  var md = QF.md;
  var D = QF.data;
  var store = QF.store;
  var eng = QF.engine;
  var qv = QF.qview;
  var sm2 = QF.sm2;
  var ai = QF.ai;

  var rootEl = null;

  var state = {
    view: 'home',
    filters: { topics: [], status: [], starred: false, types: [], difficulty: [], keyword: '' },
    // 练习页两种互斥状态：true = 选题态（浏览题库 + 选题篮），
    // false = 做题态（只有题目卡片）。两者不再同屏出现。
    picking: true,
    // 选题篮：浏览题库时挑出来的题目 id，攒够了再一次性开始练习。
    // 从 localStorage 读回，刷新页面不会丢。
    basket: store.basket(),
    // 选题态一次渲染多少道题（题量大时避免一次渲染上千道）
    browseLimit: 20,
    // 首页热力图按主题筛选（null = 全部）
    heatTopic: null,
    session: null,
    sessionStats: null,
    exam: null,
    examTick: null,
    review: null,
    aiBusy: {},
  };

  /* ------------------------------------------------------------- 工具 */

  function shuffle(list) {
    var arr = list.slice();
    for (var i = arr.length - 1; i > 0; i--) {
      var j = Math.floor(Math.random() * (i + 1));
      var tmp = arr[i];
      arr[i] = arr[j];
      arr[j] = tmp;
    }
    return arr;
  }

  function question(id) {
    return D.get(id);
  }

  function filteredIds() {
    var ids = D.filter(state.filters).map(function (q) {
      return q.id;
    });
    var status = state.filters.status;
    if (status.length) {
      ids = ids.filter(function (id) {
        return status.indexOf(store.masteryBandOf(id)) !== -1;
      });
    }
    if (state.filters.starred) {
      ids = ids.filter(function (id) {
        var rec = store.record(id);
        return !!(rec && rec.flagged);
      });
    }
    return ids;
  }

  /* --------------------------------------------------- 掌握度与收藏 */
  // 「标签」这类人工分类在主题树之后已经没有位置了；学生真正需要的是
  // 「这道题我掌握到什么程度」，而它可以从做题记录算出来（见 store.mastery）。
  // 掌握度比错题本更准：错题本只有「对/错」两个状态，而掌握度会随着
  // 做对的次数上升、随着长时间不碰而回落。

  function bandLabel(key) {
    var label = key;
    store.masteryBands.forEach(function (band) {
      if (band.key === key) label = band.label;
    });
    return label;
  }

  function isStarred(id) {
    var rec = store.record(id);
    return !!(rec && rec.flagged);
  }

  function toggleStar(id) {
    var rec = store.toggleFlag(id);
    ui.toast(rec && rec.flagged ? '已加入收藏夹' : '已移出收藏夹', 'info', 1400);
    render();
  }

  function currentQuestion() {
    if (!state.session) return null;
    return question(state.session.ids[state.session.index]) || null;
  }

  // 上一帧渲染的是哪个视图：用来区分「切页」与「原地重绘」。
  // 只有切页才挂入场动效 —— 筛选、判分、收藏这些原地重绘每点一下都动会很吵。
  var renderedView = null;

  function render() {
    if (!rootEl) return;
    var entering = renderedView !== state.view;
    renderedView = state.view;
    if (entering) ui.viewSwap(paintView);
    else paintView();
  }

  /** 真正把当前视图画进 DOM。切页时由 ui.viewSwap 包一层过渡，其余场合直接画。 */
  function paintView() {
    ui.clear(rootEl);
    document.documentElement.dataset.view = state.view;
    try {
      renderNav();
      switch (state.view) {
        case 'practice':
          rootEl.appendChild(viewPractice());
          break;
        case 'paper':
          rootEl.appendChild(viewPaper());
          break;
        case 'review':
          rootEl.appendChild(viewReview());
          break;
        default:
          rootEl.appendChild(viewHome());
      }
    } catch (err) {
      renderCrash(err);
    }
    try {
      renderStatusbar();
    } catch (err) {
      // 状态栏出问题不应连累主视图
      ui.statusbar.clear();
    }
  }

  /** 视图渲染失败时的兜底界面：把错误暴露出来而不是留一片空白 */
  function renderCrash(err) {
    var message = (err && err.message) || String(err);
    var stack = (err && err.stack) || '';
    rootEl.appendChild(
      h(
        'div.card.panel',
        { style: { borderColor: 'color-mix(in srgb, var(--bad) 45%, transparent)' } },
        h(
          'h2.page__title',
          { style: { color: 'var(--bad)' } },
          h('span', { html: ui.icon('warn', 20) }),
          h('span', { text: ' 界面渲染出错' })
        ),
        h('p.form__note', {
          style: { marginTop: '10px' },
          text: '当前视图「' + state.view + '」渲染失败。这通常是题库数据缺字段或题目内容格式异常导致的。',
        }),
        h('pre.codeblock__pre', {
          style: {
            marginTop: '12px', padding: '12px', borderRadius: '10px',
            background: 'var(--bg3)', border: '1px solid var(--line)',
            fontFamily: 'var(--font-mono)', fontSize: '12.5px', whiteSpace: 'pre-wrap',
          },
          text: message + (stack ? '\n\n' + stack.split('\n').slice(0, 6).join('\n') : ''),
        }),
        h(
          'div.btnrow',
          { style: { marginTop: '14px' } },
          h('button.btn.btn--primary', {
            type: 'button',
            onClick: function () {
              state.session = null;
              state.exam = null;
              state.review = null;
              state.view = 'home';
              render();
            },
          }, h('span', { text: '回到工作台' })),
          h('button.btn', {
            type: 'button',
            onClick: function () {
              render();
            },
          }, h('span', { html: ui.icon('refresh', 15) }), h('span', { text: '重试' }))
        )
      )
    );
  }

  function go(view) {
    state.view = view;
    // 在线模式下把视图同步到地址栏，刷新与前进后退才有意义；
    // 离线单文件没有 router.js，这里自然跳过
    if (QF.router) QF.router.push(view);
    render();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  /** 由路由反向驱动：只切视图、不再回写地址，避免与 history 打架 */
  function setViewFromRoute(view) {
    if (state.view === view) return;
    state.view = view;
    render();
  }

  /* ------------------------------------------------------------- 导航 */

  function renderNav() {
    var nav = document.getElementById('mainnav');
    if (!nav) return;
    ui.clear(nav);

    var dueCount = store.dueIds().length;

    var items = [
      { key: 'home', label: '工作台', icon: 'cpu' },
      { key: 'practice', label: '练习', icon: 'play' },
      { key: 'paper', label: '组卷', icon: 'list' },
      { key: 'review', label: '复习', icon: 'refresh', badge: dueCount },
    ];

    items.forEach(function (item) {
      nav.appendChild(
        h(
          'button.nav-item',
          {
            type: 'button',
            class: state.view === item.key ? 'is-active' : '',
            onClick: function () {
              // 点「练习」回到选题态，但不销毁进行中的会话：
              // 选题页会给一张「继续上次练习」，误点不会丢进度。
              if (item.key === 'practice') state.picking = true;
              if (item.key === 'review') state.review = null;
              go(item.key);
            },
          },
          // 图标用偶数尺寸（见 app.css 里那条说明）：15px 会让图标落在半像素上
          h('span', { html: ui.icon(item.icon, 16) }),
          h('span', { text: item.label }),
          item.badge ? h('span.nav-item__badge', { text: String(item.badge) }) : null
        )
      );
    });

    // 错题本不单列导航项：顶栏右侧有常驻图标入口。
    // 题库规模也不再挂在顶栏 —— 它属于「题库」这个面板自己的信息
    // （面板头已经写着「共 N 题」），钉在顶栏只是每个页面都重复一遍。
  }

  /* ----------------------------------------------------------- 状态栏 */

  function renderStatusbar() {
    var items = [];

    // 只在「做题态」显示答题控件：选题态虽然也可能带着一个会话，
    // 但屏幕上并没有正在作答的题，挂上进度条和上一题/下一题只会莫名。
    if (state.view === 'practice' && state.session && !state.picking) {
      var s = state.session;
      items.push({ kind: 'stat', label: '进度', value: s.index + 1 + ' / ' + s.ids.length });
      items.push({ kind: 'stat', label: '本次正确', value: String(sessionCorrect()), tone: 'ok' });
      items.push({ kind: 'stat', label: '本次错误', value: String(sessionWrong()), tone: 'bad' });
      items.push({ kind: 'button', label: '上一题', icon: 'chevronL', disabled: s.index === 0, onClick: function () { move(-1); } });
      items.push({ kind: 'button', label: '下一题', icon: 'chevronR', disabled: s.index >= s.ids.length - 1, onClick: function () { move(1); } });
      items.push({ spacer: true });
      items.push({ kind: 'hint', key: '1-9', label: '选择' });
      items.push({ kind: 'hint', key: 'Enter', label: '提交 / 下一题' });
      items.push({ kind: 'hint', key: '←/→', label: '切题' });
      // 练习不报时：用时在右侧「本次练习」里够了，底部再挂一个跑秒只是噪音。
      // 组卷（考试）才需要时限，那里的计时保留。
    } else if (state.view === 'paper' && state.exam && state.exam.report) {
      // 报告页不应再出现「交卷 / 退出」这类作答控件
      var rep = state.exam.report;
      items.push({ kind: 'stat', label: '准确率', value: ui.pct(rep.right, state.exam.ids.length), tone: rep.accuracy >= 0.8 ? 'ok' : 'amber' });
      items.push({ kind: 'stat', label: '用时', value: ui.fmtDuration(rep.usedMs) });
      items.push({ kind: 'stat', label: '需复盘', value: String(state.exam.ids.length - rep.right) });
      items.push({ spacer: true });
      items.push({ kind: 'button', label: '再组一份', icon: 'refresh', onClick: function () { state.exam = null; render(); } });
      items.push({ kind: 'button', label: '打印', icon: 'print', onClick: function () { window.print(); } });
      items.push({ kind: 'button', label: '工作台', icon: 'cpu', onClick: function () { state.exam = null; go('home'); } });
    } else if (state.view === 'paper' && state.exam) {
      items.push({ kind: 'stat', label: '已答', value: examAnswered() + ' / ' + state.exam.ids.length });
      items.push({ kind: 'stat', label: '标记', value: String(Object.keys(state.exam.flags || {}).length), tone: 'amber' });
      items.push({ spacer: true });
      items.push({ kind: 'button', label: '交卷', icon: 'check', tone: 'primary', onClick: function () { submitExam(); } });
      items.push({ kind: 'button', label: '退出', icon: 'close', onClick: function () { quitExam(); } });
    } else if (state.view === 'review' && state.review) {
      var r = state.review;
      items.push({ kind: 'stat', label: '剩余', value: String(r.ids.length - r.index) });
      items.push({ kind: 'stat', label: '已复习', value: String(r.done) });
      items.push({ spacer: true });
      items.push({ kind: 'hint', key: 'Space', label: '显示答案' });
      items.push({ kind: 'hint', key: '1-4', label: '自评档位' });
      items.push({ kind: 'button', label: '结束复习', icon: 'close', onClick: function () { state.review = null; go('home'); } });
    } else if (!store.available) {
      // 非进行中的视图（工作台 / 组卷 / 未开始的练习）不再往状态栏塞统计数字：
      // 页面顶部的统计卡已经表达了同样的信息，重复一遍只是噪音。
      // 只有「本地存储不可用」这种异常才值得占用底部这条。
      items.push({ kind: 'stat', label: '注意', value: '当前环境不支持本地存储', tone: 'bad' });
    }

    ui.statusbar.set(items);
  }

  function sessionCorrect() {
    return countSession('correct');
  }

  function sessionWrong() {
    return countSession('wrong');
  }

  function countSession(kind) {
    if (!state.session) return 0;
    return Object.keys(state.session.results).filter(function (id) {
      var r = state.session.results[id];
      return kind === 'correct' ? r.correct : r.status === 'wrong' || r.status === 'partial';
    }).length;
  }

  /* ========================================================== 工作台 */

  function viewHome() {
    var stats = store.stats();
    var overview = sm2.overview(store.records(), store.allIds());
    var page = h('div.page');

    page.appendChild(todayStrip(stats, overview));

    page.appendChild(
      h(
        'div.modegrid',
        null,
        modecard('play', '练习', {
          meta: ['未练过', overview.untouched + ' 题', '待复习', overview.dueToday + ' 题'],
          onClick: function () {
            go('practice');
          },
        }),
        modecard('list', '组卷', {
          variant: 'paper',
          meta: ['题库', stats.total + ' 题'],
          onClick: function () {
            go('paper');
          },
        }),
        modecard('refresh', '复习', {
          variant: 'review',
          meta: ['今日到期', overview.dueToday + ' 题', '在学', overview.learning + ' 题'],
          onClick: function () {
            go('review');
          },
        })
      )
    );

    page.appendChild(h('div.section__title', null, h('span', { text: '刷题记录' }), h('span', { class: 'section__note', text: '最近 18 周' })));
    // 按主题筛选热力图（GitHub 的贡献图也能按仓库筛）
    // 按主题筛热力图时要把**子孙主题**一起算上（data.js 已经为每个节点备好了
    // key + descendants 的清单）。此前只传了主题键本身，而题目标签挂在子主题上，
    // 于是点一级主题（Tenstorrent 这类）热力图一动不动。
    var heatSeries = state.heatTopic
      ? store.daySeries(126, (D.subtree && D.subtree(state.heatTopic)) || [state.heatTopic])
      : stats.heatmap;
    page.appendChild(
      h(
        'div.card.panel',
        null,
        heatTopicRow(),
        h(
          'div.heatwrap',
          null,
          h('div', null, heatmap(heatSeries), heatmapLegend()),
          heatSideStats(heatSeries)
        )
      )
    );

    page.appendChild(
      // 只保留「右键置顶」这个不明显的操作，点击卡片是显然的，不必写出来
      h('div.section__title', null, h('span', { text: '主题' }), h('span', { class: 'section__note', text: '右键置顶' }))
    );
    page.appendChild(topicGrid(stats));

    var rec = stats.recommended || {};
    if (rec.weakestTopic || rec.wrongCount) {
      var weakest = D.topicMap[rec.weakestTopic] || {};
      var row = h('div.card.panel', { style: { marginTop: '22px' } });
      row.appendChild(
        h(
          'div.statline',
          { style: { padding: '0 0 10px' } },
          h('span', { html: ui.icon('target', 15) }),
          h(
            'span',
            {
              text: rec.weakestTopic
                ? '正确率最低的主题是「' + (weakest.name || rec.weakestTopic) + '」'
                : '有 ' + rec.wrongCount + ' 道题需要巩固'
            }
          ),
          rec.weakestTopic && rec.weakestRate != null
            ? h('b', { text: ui.pct(rec.weakestRate * 100, 100) })
            : null
        )
      );
      row.appendChild(
        h(
          'div.btnrow',
          null,
          rec.weakestTopic
            ? h('button.btn.btn--primary.btn--sm', {
                type: 'button',
                onClick: function () {
                  state.filters = { topics: [rec.weakestTopic], status: [], starred: false, types: [], difficulty: [], keyword: '' };
                  state.session = null;
                  go('practice');
                },
              }, h('span', { text: '强化这个主题' }))
            : null,
          rec.wrongCount
            ? h('button.btn.btn--sm', {
                type: 'button',
                onClick: function () {
                  startPractice({ scope: 'wrong' });
                },
              }, h('span', { text: '只做错题（' + rec.wrongCount + '）' }))
            : null
        )
      );
      page.appendChild(row);
    }

    return page;
  }

  /** 今日一行摘要：当天战况 + 连续天数 + 待复习 */
  function todayStrip(stats, overview) {
    var today = stats.today || { answers: 0, correct: 0 };
    var recent = stats.recent || { answers: 0, correct: 0, activeDays: 0 };
    var rate = today.answers ? today.correct / today.answers : 0;
    var recentRate = recent.answers ? recent.correct / recent.answers : 0;

    function item(label, valueHtml, sub, tone) {
      return h(
        'div.today__item',
        null,
        h('div.today__label', { text: label }),
        h('div.today__value', { class: tone ? 'is-' + tone : '', html: valueHtml }),
        h('div.today__sub', { text: sub || '' })
      );
    }

    return h(
      'div.today',
      null,
      item('今日已做', ui.esc(String(today.answers)) + '<small>题</small>', '正确 ' + today.correct + ' 题'),
      item('今日正确率', today.answers ? ui.esc(ui.pct(today.correct, today.answers)) : '—', today.answers ? '' : '还没开始'),
      item('连续练习', ui.esc(String(stats.streak)) + '<small>天</small>', '近 7 天练了 ' + recent.activeDays + ' 天'),
      item('近 7 天', ui.esc(String(recent.answers)) + '<small>题</small>', recent.answers ? '正确率 ' + ui.pct(recent.correct, recent.answers) : '—'),
      item('待复习', ui.esc(String(overview.dueToday)) + '<small>题</small>', overview.dueToday ? '今天到期' : '暂无到期', overview.dueToday ? 'amber' : ''),
      item('题库', ui.esc(String(stats.total)) + '<small>题</small>', '已练 ' + ui.pct(stats.coverage, 0))
    );
  }

  /** GitHub 风格的贡献热力图 */
  function heatmap(series) {
    var cells = (series || []).slice();
    if (!cells.length) return h('div');

    // 左侧补空到周日，保证每列是一个自然周
    var firstDow = new Date(cells[0].date + 'T00:00:00').getDay();
    var padded = [];
    for (var p = 0; p < firstDow; p++) padded.push(null);
    padded = padded.concat(cells);

    var max = 1;
    cells.forEach(function (c) {
      if (c.answers > max) max = c.answers;
    });
    function level(n) {
      if (!n) return 0;
      if (n >= max) return 4;
      var ratio = n / max;
      if (ratio <= 0.25) return 1;
      if (ratio <= 0.5) return 2;
      if (ratio <= 0.75) return 3;
      return 4;
    }

    var grid = h('div.heat__grid');
    var monthRow = h('div.heat__months');
    var lastMonth = -1;

    for (var i = 0; i < padded.length; i += 7) {
      var week = padded.slice(i, i + 7);
      var col = h('div.heat__col');
      var labelMonth = -1;
      week.forEach(function (cell) {
        if (!cell) {
          col.appendChild(h('i.heat__cell', { class: 'is-empty' }));
          return;
        }
        var month = Number(cell.date.slice(5, 7));
        if (labelMonth === -1) labelMonth = month;
        col.appendChild(
          h('i.heat__cell', {
            class: 'lvl-' + level(cell.answers),
            title:
              cell.date +
              '：' +
              (cell.answers ? cell.answers + ' 题，正确 ' + cell.correct : '未练习'),
          })
        );
      });
      grid.appendChild(col);

      var headMonth = week[0] ? Number(week[0].date.slice(5, 7)) : labelMonth;
      if (headMonth !== lastMonth) {
        monthRow.appendChild(h('span.heat__month', { text: headMonth + '月' }));
        lastMonth = headMonth;
      } else {
        monthRow.appendChild(h('span.heat__month'));
      }
    }

    // 左侧星期标签：GitHub 只标周一/周三/周五，全标会挤成一团
    var dowList = h('div.heat__dowlist');
    ['一', '二', '三', '四', '五', '六', '日'].forEach(function (name, index) {
      dowList.appendChild(
        h('span.heat__dowcell', { text: index < 5 && index % 2 === 0 ? '周' + name : '' })
      );
    });

    return h(
      'div.heat',
      null,
      h(
        'div.heat__body',
        null,
        h('div.heat__dowcol', null, h('div.heat__dowhead'), dowList),
        h('div.heat__main', null, monthRow, grid)
      )
    );
  }

  function heatmapLegend() {
    var wrap = h('div.heat__legend');
    wrap.appendChild(h('span', { text: '少' }));
    for (var i = 0; i <= 4; i++) wrap.appendChild(h('i.heat__cell', { class: 'lvl-' + i }));
    wrap.appendChild(h('span', { text: '多' }));
    return wrap;
  }

  /** 热力图右侧的累计统计 */
  /** 热力图上方：按主题筛选。GitHub 的贡献图也能按仓库筛，同一套思路。 */
  function heatTopicRow() {
    var row = h('div.filterbar', { style: { marginBottom: '14px' } });
    row.appendChild(h('span.filterbar__label', { text: '主题' }));
    var group = h('div.filterbar__group');

    function chip(key, label) {
      group.appendChild(
        h(
          'button.chip',
          {
            type: 'button',
            class: (state.heatTopic || '') === key ? 'is-on' : '',
            style: key ? { '--tc': D.topicColor(key) } : null,
            onClick: function () {
              state.heatTopic = key || null;
              render();
            },
          },
          h('span', { text: label })
        )
      );
    }
    chip('', '全部');
    // 只列有题的学科；当前选中的那个即使被筛没了也要留着，否则取消不掉
    pruneEmpty(
      D.subjects.map(function (t) {
        return { key: t.key, label: t.name, count: D.bankStats.byTopic[t.key] || 0 };
      }),
      state.heatTopic ? [state.heatTopic] : []
    ).forEach(function (item) {
      chip(item.key, item.label);
    });
    row.appendChild(group);
    return row;
  }

  /**
   * 热力图右侧统计。全部从传进来的序列算，这样按主题筛选时数字会跟着走
   * —— 否则图变了、旁边的数字还是全局的，读起来是错的。
   */
  function heatSideStats(series) {
    var list = series || [];
    var answers = 0;
    var correct = 0;
    list.forEach(function (d) {
      answers += d.answers;
      correct += d.correct;
    });
    var active = list.filter(function (d) {
      return d.answers > 0;
    }).length;
    var last7 = list.slice(-7).reduce(function (sum, d) {
      return sum + d.answers;
    }, 0);
    var best = list.reduce(function (acc, d) {
      return d.answers > acc.answers ? d : acc;
    }, { date: '—', answers: 0 });

    function row(label, value) {
      return h('div.heat__siderow', null, h('span', { text: label }), h('b', { text: value }));
    }

    return h(
      'div.heat__side',
      null,
      row('作答', answers + ' 次'),
      row('正确率', answers ? ui.pct(correct, answers) : '—'),
      row('有练习的天数', active + ' / ' + list.length + ' 天'),
      row('近 7 天', last7 + ' 题'),
      row('单日最多', best.answers ? best.answers + ' 题（' + best.date.slice(5) + '）' : '—')
    );
  }

  /** 主题卡片：不分组，置顶的排在前面 */
  function topicGrid(stats) {
    var pinned = store.pinnedTopics();
    // 首页按「学科」（一级）铺卡片；单元与知识点进到练习页再选。
    // stats.byTopic 已在 store 里沿主题树累加，所以这里的数字是该学科的总和。
    var topics = D.subjects
      // 一道题都没有的学科不铺卡片 —— 与筛选器同一条规则。
      // 但用户手动置顶的除外：置顶是他自己说的「我要看这个」，不该被自动藏掉。
      .filter(function (t) {
        if (pinned.indexOf(t.key) !== -1) return true;
        return ((stats.byTopic[t.key] || {}).total || 0) > 0;
      })
      .sort(function (a, b) {
        var pa = pinned.indexOf(a.key);
        var pb = pinned.indexOf(b.key);
        if (pa !== -1 && pb !== -1) return pa - pb;
        if (pa !== -1) return -1;
        if (pb !== -1) return 1;
        return a.order - b.order;
      });

    var grid = h('div.topicgrid');
    topics.forEach(function (topic) {
      var bucket = stats.byTopic[topic.key] || { total: 0, attempted: 0, correct: 0, mastered: 0 };
      var rate = bucket.attempted ? bucket.correct / bucket.attempted : 0;
      var isPinned = pinned.indexOf(topic.key) !== -1;

      grid.appendChild(
        h(
          'button.topiccard',
          {
            type: 'button',
            class: isPinned ? 'is-pinned' : '',
            style: { '--tc': topic.color },
            onClick: function () {
              state.filters = { topics: [topic.key], status: [], starred: false, types: [], difficulty: [], keyword: '' };
              state.session = null;
              go('practice');
            },
            onContextmenu: function (event) {
              event.preventDefault();
              store.togglePin(topic.key);
              ui.toast(isPinned ? '已取消置顶' : '已置顶「' + topic.name + '」', 'info', 1600);
              render();
            },
          },
          ui.ring(rate, {
            size: 40,
            stroke: 4,
            color: topic.color,
            label: bucket.attempted ? Math.round(rate * 100) + '%' : '—',
          }),
          h(
            'span.topiccard__body',
            null,
            // 不再放彩色小圆点：左侧进度环已经用主题色表达了同一件事
            h('span.topiccard__name', null, h('span', { text: topic.name })),
            h('span.topiccard__meta', {
              text:
                (topic.children || []).length + ' 个单元 · ' +
                bucket.total + ' 题 · 已练 ' + bucket.attempted +
                (bucket.mastered ? ' · 掌握 ' + bucket.mastered : ''),
            })
          ),
          isPinned ? h('span.topiccard__pin', { html: ui.icon('flag', 13) }) : null
        )
      );
    });
    return grid;
  }

  function statcard(label, value, unit, hint, tone) {
    return h(
      'div.statcard',
      null,
      h('div.statcard__label', { text: label }),
      h(
        'div.statcard__value',
        { class: tone === 'amber' ? 'is-amber' : '', html: ui.esc(value) + (unit ? '<small>' + ui.esc(unit) + '</small>' : '') }
      ),
      h('div.statcard__hint', { text: hint || '' })
    );
  }

  function modecard(iconName, title, options) {
    var opts = options || {};
    return h(
      'button.modecard',
      {
        type: 'button',
        class: opts.variant ? 'modecard--' + opts.variant : '',
        onClick: opts.onClick,
      },
      h('span.modecard__icon', { html: ui.icon(iconName, 20) }),
      h('div.modecard__title', { text: title }),
      opts.meta
        ? h(
            'div.modecard__meta',
            null,
            opts.meta.map(function (item, index) {
              return h('span', { html: index % 2 === 1 ? '<b>' + ui.esc(item) + '</b>' : ui.esc(item) });
            })
          )
        : null
    );
  }

  /* ========================================================== 练习模式 */

  /**
   * 练习页入口：选题与做题是两件不同的事，这里彻底分开。
   *   选题态 —— 只有筛选条件和题目列表，看不到题目内容；
   *   做题态 —— 只有当前会话的题目卡片，出现任何筛选控件都算干扰。
   * 以前两者同屏：一边做题一边挂着筛选条，点「随机」会当场重建会话丢掉进度，
   * 改了筛选条件题目却不变，谁也说不清当前这题的集合到底是哪一批。
   */
  function viewPractice() {
    var page = h('div.page');
    if (state.picking || !state.session || !state.session.ids.length) return viewPicker(page);
    return viewPracticeSession(page);
  }

  /* ------------------------------------------------------------ 选题态 */

  function viewPicker(page) {
    var ids = filteredIds();

    var grid = h('div.exam-grid');
    var main = h('div');
    main.appendChild(buildPickerPanel());

    if (!ids.length) {
      main.appendChild(
        emptyState('search', '没有符合条件的题目', '放宽主题、题型或难度筛选，或清空关键词。', [
          { label: '清除筛选', onClick: function () { resetFilters(); render(); } },
        ])
      );
    } else {
      main.appendChild(buildBrowseList(ids));
    }

    grid.appendChild(main);
    grid.appendChild(
      h('div.exam-aside', null, buildBasketPanel(), buildResumePanel())
    );
    page.appendChild(grid);
    return page;
  }

  /** 改选题篮：写回 localStorage 并重渲染 */
  function setBasket(ids) {
    state.basket = store.setBasket(ids);
    render();
  }

  function basketIds() {
    return state.basket.filter(function (id) {
      return !!question(id);
    });
  }

  /** 上次没做完的会话：给一张「继续」，免得重新选题就把进度扔了 */
  function buildResumePanel() {
    var s = state.session;
    if (!s || !s.ids.length) return null;
    var done = Object.keys(s.results).length;
    if (done >= s.ids.length) return null;

    var box = h('div.card.panel');
    box.appendChild(h('div.panel__head', null, h('h2.panel__title', { text: '未完成' })));
    box.appendChild(
      h(
        'div.statline',
        { style: { padding: '0 0 10px' } },
        h('span', { text: s.label }),
        h('span', { style: { flex: '1 1 auto' } }),
        h('b', { text: done + ' / ' + s.ids.length })
      )
    );
    box.appendChild(
      h(
        'div.btnrow',
        null,
        h('button.btn.btn--sm.btn--primary', {
          type: 'button',
          onClick: function () {
            state.picking = false;
            render();
          },
        }, h('span', { html: ui.icon('play', 14) }), h('span', { text: '继续' })),
        h('button.btn.btn--sm', {
          type: 'button',
          onClick: function () {
            state.session = null;
            render();
          },
        }, h('span', { text: '放弃' }))
      )
    );
    return box;
  }

  /** 选题篮：攒好的题，够了再一次性开始练习 */
  function buildBasketPanel() {
    var ids = basketIds();
    var box = h('div.card.panel');
    box.appendChild(
      h(
        'div.panel__head',
        null,
        h('h2.panel__title', { text: '选题篮' }),
        h('span.badge.badge--mono', { text: ids.length + ' 题' })
      )
    );

    if (!ids.length) {
      box.appendChild(
        h('p.form__note', {
          style: { margin: '0' },
          text: '在左侧题库里「加入选题篮」，或一次「全部加入」，攒够了再开始。',
        })
      );
      return box;
    }

    var list = h('div.basketlist');
    ids.forEach(function (id, index) {
      var q = question(id);
      list.appendChild(
        h(
          'div.basketlist__item',
          null,
          h('span.basketlist__n', { text: String(index + 1) }),
          h('span.basketlist__text', { text: md.plain(q.stem, 44) }),
          h('button.basketlist__x', {
            type: 'button',
            title: '移出选题篮',
            onClick: function () {
              setBasket(state.basket.filter(function (x) {
                return x !== id;
              }));
            },
          }, h('span', { html: ui.icon('close', 13) }))
        )
      );
    });
    box.appendChild(list);

    box.appendChild(
      h(
        'div.btnrow',
        { style: { marginTop: '12px' } },
        h('button.btn.btn--sm.btn--primary', {
          type: 'button',
          onClick: function () {
            startPractice({ scope: 'ids', ids: ids, label: '选题篮' });
          },
        }, h('span', { html: ui.icon('play', 14) }), h('span', { text: '开始练习（' + ids.length + '）' })),
        h('button.btn.btn--sm', {
          type: 'button',
          onClick: function () {
            setBasket([]);
          },
        }, h('span', { text: '清空' }))
      )
    );
    return box;
  }

  /**
   * 题库浏览：一道题一张卡，题面、选项、答案都能当场看完。
   * 卡片上刻意没有任何「点了就跑」的热区 —— 想练某题用卡上的按钮，
   * 想练一批用右侧选题篮。以前点一行就直接进答题界面还带上计时，
   * 只是因为想看一眼题面，体验很突兀。
   */
  function buildBrowseList(ids) {
    var box = h('div.card.panel');
    box.appendChild(
      h(
        'div.panel__head',
        null,
        h('h2.panel__title', { text: '题库' }),
        h('span.badge.badge--mono', { text: '共 ' + ids.length + ' 题' }),
        h('span', { style: { flex: '1 1 auto' } }),
        h('button.btn.btn--sm.btn--ghost', {
          type: 'button',
          onClick: function () {
            setBasket(ids);
          },
        }, h('span', { text: '全部加入选题篮' }))
      )
    );

    var shown = Math.min(ids.length, state.browseLimit);
    for (var i = 0; i < shown; i += 1) {
      var q = question(ids[i]);
      if (q) box.appendChild(browseCard(q, i));
    }

    if (ids.length > shown) {
      box.appendChild(
        h(
          'div.btnrow',
          { style: { marginTop: '14px' } },
          h('button.btn', {
            type: 'button',
            onClick: function () {
              state.browseLimit += 20;
              render();
            },
          }, h('span', { text: '再显示 20 题（还有 ' + (ids.length - shown) + ' 题）' }))
        )
      );
    }
    return box;
  }

  function browseCard(q, index) {
    var rec = store.record(q.id) || {};
    var band = store.masteryBand(rec);
    var score = store.masteryOfRecord(rec);

    var inBasket = state.basket.indexOf(q.id) !== -1;
    var card = h('div.browsecard', { class: inBasket ? 'is-picked' : '' });

    card.appendChild(
      qv.metaRow(q, {
        extra: h(
          'span.browsecard__info',
          null,
          h('span.browsecard__n', { text: '第 ' + (index + 1) + ' 题' }),
          h('span.browsecard__mastery', {
            class: 'is-' + band,
            // 没练过的题不给数字，免得「0 分」看起来像做错过
            text: band === 'new' ? '未练' : bandLabel(band) + ' ' + score,
          }),
          h('button.browsecard__star', {
            type: 'button',
            class: rec.flagged ? 'is-on' : '',
            title: rec.flagged ? '移出收藏夹' : '加入收藏夹',
            'aria-pressed': rec.flagged ? 'true' : 'false',
            html: ui.icon('star', 15),
            onClick: function (event) {
              event.stopPropagation();
              toggleStar(q.id);
            },
          })
        ),
      })
    );
    card.appendChild(qv.stem(q));

    // 大题各问的答案槽：{ part, slot }，展开答案时才填
    var partSlots = [];
    if (q.type === 'single' || q.type === 'multi') {
      card.appendChild(qv.optionsReadOnly(q));
    } else if (q.type === 'problem') {
      // 大题：按小问把题面铺开，只读。每问的题面下面留一个答案槽（先空着，
      // 展开时才填）—— 以前是把各问答案在卡片底部堆成一坨：六问大题有六千多
      // 像素高，想核对第 1 问的答案，得先把第 2~6 问的题面整段滚过去。
      var parts = h('div.parts');
      (q.parts || []).forEach(function (part, partIndex) {
        var partSlot = h('div.part__refblock');
        partSlots.push({ part: part, slot: partSlot });
        parts.appendChild(
          h(
            'div.part.is-open',
            null,
            h(
              'div.part__head',
              { style: { cursor: 'default' } },
              h('span.part__no', { text: '第 ' + (part.index || partIndex + 1) + ' 问' }),
              h('span.part__title', { text: part.title || '' })
            ),
            h(
              'div.part__body',
              null,
              h('div.part__stem', null, md.render(part.stem || '')),
              partSlot
            )
          )
        );
      });
      card.appendChild(parts);
    }

    // 答案默认不构建：整库题目的解析都含 KaTeX，全量渲染很贵。
    // 大题没有底部答案区（答案在小问下面），只有别的题型才有这个盒子。
    var answerBox = partSlots.length ? null : h('div.browsecard__answer');
    var answerLoaded = false;
    // 文案要与它开合的范围一致：面板里既有正确答案（参考答案 / 评分要点）也有解析
    var answerLabel = h('span', { text: '显示答案与解析' });
    var answerBtn = h(
      'button.btn.btn--sm.btn--ghost',
      {
        type: 'button',
        onClick: function () {
          var open = !card.classList.contains('is-answer-open');
          if (open && !answerLoaded) {
            if (answerBox) answerBox.appendChild(qv.explainPanel(q, { open: true, toggle: false }));
            else partSlots.forEach(function (item) { fillPartRef(item.slot, item.part); });
            answerLoaded = true;
          }
          card.classList.toggle('is-answer-open', open);
          answerLabel.textContent = open ? '收起答案与解析' : '显示答案与解析';
        },
      },
      h('span', { html: ui.icon('bulb', 14) }),
      answerLabel
    );

    card.appendChild(
      h(
        'div.browsecard__foot',
        null,
        answerBtn,
        inBasket
          ? h('button.btn.btn--sm.is-active', {
              type: 'button',
              title: '点击移出选题篮',
              onClick: function () {
                setBasket(state.basket.filter(function (x) {
                  return x !== q.id;
                }));
              },
            }, h('span', { html: ui.icon('check', 14) }), h('span', { text: '已在选题篮' }))
          : h('button.btn.btn--sm', {
              type: 'button',
              onClick: function () {
                setBasket(state.basket.concat([q.id]));
              },
            }, h('span', { html: ui.icon('plus', 14) }), h('span', { text: '加入选题篮' })),
        h('span', { style: { flex: '1 1 auto' } }),
        h('button.btn.btn--sm', {
          type: 'button',
          title: '只练这一题',
          onClick: function () {
            startPractice({ scope: 'ids', ids: [q.id], label: '单题练习' });
          },
        }, h('span', { html: ui.icon('play', 14) }), h('span', { text: '单题练习' }))
      )
    );
    if (answerBox) card.appendChild(answerBox);
    return card;
  }

  /**
   * 大题：把某一问的参考答案与评分要点填进它自己的槽位。
   * 由题卡上的「显示答案与解析」一次性填满所有小问（首次展开时才动手，
   * 六问的解析全量渲染要 30ms 量级，折叠状态下没必要先付这笔钱）。
   */
  function fillPartRef(slot, part) {
    var wrap = h('div');
    if (part.reference) {
      wrap.appendChild(
        h('div.explain__row', null, h('div.explain__rowtitle', { text: '参考答案' }), h('div.explain__ref', null, md.render(part.reference)))
      );
    }
    if (part.rubric && part.rubric.length) {
      wrap.appendChild(
        h(
          'div.explain__row',
          null,
          h('div.explain__rowtitle', { text: '评分要点' }),
          h('ul.rubriclist', null, part.rubric.map(function (item) {
            return h('li', { html: md.renderInline(item) });
          }))
        )
      );
    }
    // 有的小问只给要点、不给参考答案（或反过来），两者都没有就留空槽
    if (!wrap.children.length) return;
    slot.appendChild(wrap);
  }

  function viewPracticeSession(page) {
    var s = state.session;
    var q = currentQuestion();
    if (!q) {
      state.session = null;
      return viewPractice();
    }
    var response = s.responses[q.id];
    var result = s.results[q.id] || null;
    var rec = store.record(q.id) || {};

    var afterAnswer = null;
    if (result && q.type !== 'problem') {
      // 大题的解答已按小问展开，不再重复一个空的解析抽屉
      afterAnswer = qv.explainPanel(q, { open: true });
      if (result.ai) afterAnswer.insertBefore(qv.aiResultPanel(result.ai, q), afterAnswer.firstChild);
      if (q.type === 'short' && !result.ai) {
        afterAnswer.appendChild(
          qv.selfRate(function (grade) {
            store.setSelfGrade(q.id, grade);
            ui.toast('已记录自评：' + ['没答对', '部分正确', '答对了'][grade], 'info');
            move(1);
          }, { title: '自评（用于安排复习）' })
        );
      }
    }

    var card = qv.card(q, {
      response: response,
      result: result,
      locked: !!result,
      autoFocus: !result && (q.type === 'blank' || q.type === 'short') && !response,
      onChange: function (value) {
        s.responses[q.id] = value;
        if (q.type === 'single' && !s.results[q.id]) {
          submitCurrent();
        } else if (q.type === 'multi') {
          // 多选需要立刻反映勾选状态
          render();
        }
        // 填空 / 简答不重渲染：重渲染会替换 input 元素，导致每敲一个字
        // 就丢失输入焦点与光标位置。作答内容已存进 session，提交时读取。
      },
      onSubmit: function () { submitCurrent(); },
      actions: q.type === 'short' ? shortActions(q, result) : null,
      headerExtra: h(
        'span',
        null,
        rec.flagged ? h('span.badge.badge--amber', { text: '已标记' }) : null,
        rec.wrong ? h('span.badge.badge--bad', { text: '错过 ' + rec.wrong + ' 次' }) : null
      ),
      problem: q.type === 'problem' ? problemOptions(q, s) : null,
      footer: buildCardFooter(q, result),
      afterAnswer: afterAnswer,
    });

    var grid = h('div.exam-grid');
    grid.appendChild(h('div', null, card));
    grid.appendChild(h('div.exam-aside', null, buildQNav(), buildSessionSummary()));
    page.appendChild(grid);
    return page;
  }

  function shortActions(q, result) {
    var conf = ai.config();
    var busy = state.aiBusy[q.id];
    if (result) return null;
    if (busy) {
      return h('span.ai-loading', null, h('span.spinner'), h('span', { text: 'AI 正在批改…' }));
    }
    if (!conf.enabled || !conf.baseUrl) {
      return h('span.badge', { text: '未启用 AI 批改，提交后按参考答案自评' });
    }
    return h(
      'button.btn.btn--sm',
      {
        type: 'button',
        onClick: function (event) {
          event.stopPropagation();
          submitCurrent(true);
        },
      },
      h('span', { html: ui.icon('robot', 14) }),
      h('span', { text: '提交并 AI 批改' })
    );
  }

  function buildCardFooter(q, result) {
    var foot = h('div.qcard__foot');
    var flags = store.record(q.id) || {};

    if (q.type === 'problem') {
      // 大题没有「整题提交」这一动作：每个小问各自作答与批改
      foot.appendChild(
        h('span.badge', {
          text:
            (q.parts || []).length + ' 个小问 · 已批改 ' +
            Object.keys((state.session.partResults && state.session.partResults[q.id]) || {}).length + ' 个',
        })
      );
      foot.appendChild(h('span.spacer'));
      foot.appendChild(
        h('button.btn.btn--primary', {
          type: 'button',
          disabled: state.session.index >= state.session.ids.length - 1,
          onClick: function () { move(1); },
        }, h('span', { text: '下一题' }), h('span', { html: ui.icon('chevronR', 15) }))
      );
      return foot;
    }

    if (!result) {
      if (q.type !== 'single') {
        // 这里刻意不用 disabled 快照：填空 / 简答的输入不会触发重渲染，
        // 渲染时算出来的 disabled 会一直是旧值，按钮会永久卡在禁用状态。
        // 空作答由 submitCurrent() 统一拦截并给出提示。
        foot.appendChild(
          h('button.btn.btn--primary', {
            type: 'button',
            onClick: function () { submitCurrent(); },
          }, h('span', { html: ui.icon('check', 15) }), h('span', { text: '提交' }))
        );
      } else {
        foot.appendChild(h('span.badge', { text: '点击选项即判定' }));
      }
      if (q.type === 'short') {
        foot.appendChild(
          h('button.btn.btn--ghost', {
            type: 'button',
            onClick: function () {
              state.session.results[q.id] = {
                status: 'empty',
                correct: false,
                score: null,
                blanks: [],
                expected: '',
                expectedHtml: eng.expectedHtml(q),
                skipped: true,
              };
              render();
            },
          }, h('span', { text: '不会，直接看答案' }))
        );
      }
    } else {
      foot.appendChild(
        h('button.btn', {
          type: 'button',
          onClick: function () {
            delete state.session.results[q.id];
            state.session.responses[q.id] = q.type === 'multi' ? [] : q.type === 'blank' ? [] : '';
            render();
          },
        }, h('span', { html: ui.icon('refresh', 15) }), h('span', { text: '重做这题' }))
      );
      foot.appendChild(
        h('button.btn', {
          type: 'button',
          class: flags.flagged ? 'is-active' : '',
          onClick: function () {
            store.toggleFlag(q.id);
            render();
          },
        }, h('span', { html: ui.icon('flag', 15) }), h('span', { text: flags.flagged ? '取消标记' : '标记' }))
      );
      foot.appendChild(h('span.spacer'));
      foot.appendChild(
        h('button.btn.btn--primary', {
          type: 'button',
          disabled: state.session.index >= state.session.ids.length - 1,
          onClick: function () { move(1); },
        }, h('span', { text: '下一题' }), h('span', { html: ui.icon('chevronR', 15) }))
      );
    }
    if (foot.childNodes.length === 0) return null;
    return foot;
  }

  function buildQNav() {
    var s = state.session;
    var box = h('div.card.panel');
    box.appendChild(h('div.panel__head', null, h('h2.panel__title', { text: '题号' })));
    var nav = h('div.qnav');
    s.ids.forEach(function (id, index) {
      var result = s.results[id];
      var cls = '';
      if (index === s.index) cls += ' is-current';
      else if (result) cls += result.correct ? ' is-right' : ' is-wrong';
      nav.appendChild(
        h('button.qnav__item', {
          type: 'button',
          class: cls,
          title: id,
          text: String(index + 1),
          onClick: function () {
            s.index = index;
            render();
          },
        })
      );
    });
    box.appendChild(nav);
    return box;
  }

  function buildSessionSummary() {
    var s = state.session;
    var answered = Object.keys(s.results).length;
    var correct = sessionCorrect();
    var box = h('div.card.panel');
    box.appendChild(
      h(
        'div.panel__head',
        null,
        h('h2.panel__title', { text: '本次练习' }),
        h('span.badge', { text: s.label })
      )
    );
    box.appendChild(progressRow('作答进度', answered / Math.max(1, s.ids.length), answered + ' / ' + s.ids.length));
    box.appendChild(
      progressRow('正确率', answered ? correct / answered : 0, answered ? ui.pct(correct, answered) : '—', 'ok')
    );
    box.appendChild(
      h('div.statline', null, h('span', { text: '用时' }), h('b', { text: ui.fmtDuration(Date.now() - s.started) }))
    );
    box.appendChild(
      h(
        'div.btnrow',
        { style: { marginTop: '12px' } },
        h('button.btn.btn--sm', {
          type: 'button',
          onClick: function () {
            // 只是回到选题态，会话留着，选题页会给「继续上次练习」
            state.picking = true;
            render();
          },
        }, h('span', { text: '重新选题' })),
        h('button.btn.btn--sm', {
          type: 'button',
          onClick: function () {
            state.session = null;
            state.picking = true;
            render();
          },
        }, h('span', { text: '结束本次练习' }))
      )
    );
    return box;
  }

  function progressRow(label, ratio, value, tone) {
    return h(
      'div',
      { style: { marginBottom: '12px' } },
      h(
        'div.statline',
        { style: { padding: '0 0 6px' } },
        h('span', { text: label }),
        h('span', { style: { flex: '1 1 auto' } }),
        h('b', { text: value })
      ),
      ui.progressBar(ratio, tone)
    );
  }

  function resetFilters() {
    state.filters = { topics: [], status: [], starred: false, types: [], difficulty: [], keyword: '' };
  }

  /** 选题面板：筛选条件 + 快捷范围。只在选题态出现，任何时候都不会与题目卡片同屏。 */
  function buildPickerPanel() {
    var panel = h('div.card.panel', { style: { marginBottom: '14px' } });
    panel.appendChild(
      h(
        'div.panel__head',
        null,
        h('h2.panel__title', { text: '筛选范围' }),
        h('button.btn.btn--sm.btn--ghost', {
          type: 'button',
          onClick: function () {
            resetFilters();
            render();
          },
        }, h('span', { text: '清空' }))
      )
    );

    // 快捷范围：不是筛选条件，而是直接换一批题开始，所以放在筛选条件之前。
    // 间距用 .filterbar 的默认值，和下面每一行筛选器保持一致：这里原来是 4px，
    // 两行贴在一起，「主题」会被误读成是在给上面那排按钮打标签。
    var dueCount = store.dueIds().length;
    var wrongCount = store.wrongIds().length;
    panel.appendChild(
      h(
        'div.filterbar',
        null,
        h('span.filterbar__label', { text: '快捷开始' }),
        h(
          'div.filterbar__group',
          null,
          h('button.btn.btn--sm', {
            type: 'button',
            title: '打乱当前筛选的题目顺序',
            onClick: function () { startPractice({ scope: 'filter', shuffle: true }); },
          }, h('span', { html: ui.icon('shuffle', 14) }), h('span', { text: '随机顺序' })),
          dueCount
            ? h('button.btn.btn--sm', {
                type: 'button',
                onClick: function () { startPractice({ scope: 'due' }); },
              }, h('span', { html: ui.icon('refresh', 14) }), h('span', { text: '到期 ' + dueCount }))
            : null,
          wrongCount
            ? h('button.btn.btn--sm', {
                type: 'button',
                onClick: function () { startPractice({ scope: 'wrong' }); },
              }, h('span', { html: ui.icon('close', 14) }), h('span', { text: '错题 ' + wrongCount }))
            : null
        )
      )
    );

    panel.appendChild(buildTopicSelector(filterAccessor('topics')));
    addRow(panel, chipRow('题型', typeChipItems(), filterAccessor('types')));
    addRow(panel, chipRow('难度', difficultyChipItems(), filterAccessor('difficulty')));
    // 掌握度由做题记录算出（含遗忘回落），比错题本的「对/错」二值判断准
    addRow(panel, chipRow('掌握度', masteryChipItems(), filterAccessor('status')));
    panel.appendChild(starRow());

    panel.appendChild(
      h(
        'div.filterbar',
        { style: { marginBottom: '0', marginTop: '6px' } },
        h(
          'label.searchbox',
          null,
          h('span', { html: ui.icon('search', 15) }),
          h('input', {
            type: 'search',
            placeholder: '搜索题面 / 主题 / 出处…',
            value: state.filters.keyword,
            onInput: ui.debounce(function (event) {
              state.filters.keyword = event.target.value;
              render();
            }, 250),
          })
        ),
        h('span.filterbar__spacer'),
        h('span.badge.badge--mono', { text: '命中 ' + filteredIds().length + ' 题' })
      )
    );

    // 勾选不立刻重开会话，改完再一次性按新筛选开始
    panel.appendChild(
      h(
        'div.btnrow',
        { style: { marginTop: '14px' } },
        h('button.btn.btn--primary', {
          type: 'button',
          onClick: function () {
            startPractice({ scope: 'filter' });
          },
        }, h('span', { html: ui.icon('play', 15) }), h('span', { text: '从头开始（' + filteredIds().length + ' 题）' }))
      )
    );

    return panel;
  }

  function subjectChipItems() {
    return D.subjects.map(function (t) {
      return { key: t.key, label: t.name, count: D.bankStats.byTopic[t.key] || 0, color: t.color };
    });
  }

  function masteryChipItems() {
    var counts = {};
    D.questions.forEach(function (q) {
      var band = store.masteryBandOf(q.id);
      counts[band] = (counts[band] || 0) + 1;
    });
    // 数量为 0 的档位由 pruneEmpty 统一丢掉（见该函数）
    return store.masteryBands.map(function (band) {
      return { key: band.key, label: band.label, count: counts[band.key] || 0 };
    });
  }

  /**
   * 丢掉「一个题都筛不到」的候选项。
   *
   * 只列真正有内容的项 —— 与 GitHub 的语言筛选同一个思路。
   * 一个点下去必然空结果的按钮，除了让人怀疑自己点错了没有别的用。
   *
   * 但**已选中的必须留下**：否则筛到一半它变成空的时候，
   * 用户连取消都点不到了。
   */
  function pruneEmpty(items, picked) {
    var selected = picked || [];
    return items.filter(function (item) {
      // 没有 count 的候选项（纯动作项）不参与裁剪
      if (typeof item.count !== 'number') return true;
      return item.count > 0 || selected.indexOf(item.key) !== -1;
    });
  }

  /**
   * 挂一「行」筛选器。
   *
   * 行构造器在候选项被裁光时返回 null —— 留一个光秃秃的标签
   * （比如「知识点」下面什么都没有）比整行不显示更让人困惑。
   */
  function addRow(host, row) {
    if (row) host.appendChild(row);
    return host;
  }

  /** 收藏夹开关：星标是人工置的，和算出来的掌握度分开成两行 */
  function starRow() {
    var n = 0;
    D.questions.forEach(function (q) {
      if (isStarred(q.id)) n += 1;
    });
    var row = h('div.filterbar');
    row.appendChild(h('span.filterbar__label', { text: '收藏夹' }));
    row.appendChild(
      h(
        'div.filterbar__group',
        null,
        h(
          'button.chip',
          {
            type: 'button',
            class: state.filters.starred ? 'is-on' : '',
            onClick: function () {
              state.filters.starred = !state.filters.starred;
              render();
            },
          },
          h('span', { text: '只看星标' }),
          h('span.chip__count', { text: String(n) })
        )
      )
    );
    return row;
  }

  function topicChipOf(node) {
    return {
      key: node.key,
      label: node.name,
      count: D.bankStats.byTopic[node.key] || 0,
      color: node.color,
    };
  }

  /** 按主题树顺序排序一批节点（D.topics 已是深度优先顺序） */
  function sortByTree(nodes) {
    return nodes.slice().sort(function (a, b) {
      return D.topics.indexOf(a) - D.topics.indexOf(b);
    });
  }

  /** 主题筛选条：点选时把祖先/子孙摘掉，见下方注释 */
  function topicRow(label, items, accessor) {
    var kept = pruneEmpty(items, accessor.get());
    if (!kept.length) return null;
    var row = h('div.filterbar');
    row.appendChild(h('span.filterbar__label', { text: label }));
    var group = h('div.filterbar__group');
    kept.forEach(function (item) {
      var on = accessor.get().indexOf(item.key) !== -1;
      group.appendChild(
        h(
          'button.chip',
          {
            type: 'button',
            class: on ? 'is-on' : '',
            style: item.color ? { '--tc': item.color } : null,
            onClick: function () {
              var list = accessor.get().slice();
              var at = list.indexOf(item.key);
              if (at === -1) {
                // 选父级时摘掉它的子孙（回到整块范围）；
                // 选子级时保留祖先 —— 它只是路径提示，匹配时会被 expandTopics 忽略，
                // 所以「主题仍亮着 + 范围真的收窄」两件事可以同时成立。
                var sub = D.subtree(item.key);
                list = list.filter(function (k) {
                  return sub.indexOf(k) === -1;
                });
                list.push(item.key);
              } else {
                list.splice(at, 1);
              }
              accessor.set(list);
              render();
            },
          },
          h('span', { text: item.label }),
          item.count != null ? h('span.chip__count', { text: String(item.count) }) : null
        )
      );
    });
    row.appendChild(group);
    return row;
  }

  /**
   * 三级主题选择器：学科 → 单元 → 知识点。
   * 下一层只在上一层选过之后才出现 —— 80 个知识点全铺开是没法看的。
   */
  function buildTopicSelector(accessor) {
    var sel = accessor.get();
    var box = h('div');

    addRow(box, topicRow('主题', subjectChipItems(), accessor));
    if (!sel.length) return box;

    // 已选节点的「学科根」，用来决定单元层的候选范围
    var roots = [];
    sel.forEach(function (key) {
      var path = D.topicPath(key);
      if (path.length && roots.indexOf(path[0]) === -1) roots.push(path[0]);
    });

    var unitMap = {};
    roots.forEach(function (root) {
      D.childrenOf(root).forEach(function (node) {
        unitMap[node.key] = node;
      });
    });
    // 已选知识点所在的单元也要留在候选里，否则选完知识点就摸不到兄弟节点
    sel.forEach(function (key) {
      if (D.topicDepth(key) === 3) {
        var parent = D.topicMap[D.topicMap[key].parent];
        if (parent) unitMap[parent.key] = parent;
      }
    });

    var units = sortByTree(
      Object.keys(unitMap).map(function (k) {
        return unitMap[k];
      })
    );
    if (!units.length) return box;

    addRow(box, topicRow('单元', units.map(topicChipOf), accessor));

    // 知识点：只列「已选单元」下面的
    var leafMap = {};
    sel.forEach(function (key) {
      if (D.topicDepth(key) !== 2) return;
      D.childrenOf(key).forEach(function (node) {
        leafMap[node.key] = node;
      });
    });
    var leaves = sortByTree(
      Object.keys(leafMap).map(function (k) {
        return leafMap[k];
      })
    );
    if (leaves.length) {
      addRow(box, topicRow('知识点', leaves.map(topicChipOf), accessor));
    }

    return box;
  }

  function typeChipItems() {
    return Object.keys(D.typeLabels).map(function (key) {
      return { key: key, label: D.typeLabels[key], count: D.bankStats.byType[key] || 0 };
    });
  }

  function difficultyChipItems() {
    return [1, 2, 3, 4, 5].map(function (level) {
      return { key: level, label: level + ' 星', count: D.bankStats.byDifficulty[level] || 0 };
    });
  }

  /** 让 chipRow 既能操作 state.filters，也能操作 paperConfig */
  function filterAccessor(field) {
    return {
      get: function () {
        return state.filters[field];
      },
      set: function (list) {
        state.filters[field] = list;
      },
    };
  }

  function configAccessor(container, field) {
    return {
      get: function () {
        return container[field] || [];
      },
      set: function (list) {
        container[field] = list;
      },
    };
  }

  function chipRow(label, items, accessor) {
    var kept = pruneEmpty(items, accessor.get());
    if (!kept.length) return null;
    var row = h('div.filterbar');
    row.appendChild(h('span.filterbar__label', { text: label }));
    var group = h('div.filterbar__group');
    kept.forEach(function (item) {
      var on = accessor.get().indexOf(item.key) !== -1;
      group.appendChild(
        h(
          'button.chip',
          {
            type: 'button',
            class: on ? 'is-on' : '',
            style: item.color ? { '--tc': item.color } : null,
            onClick: function () {
              var list = accessor.get().slice();
              var at = list.indexOf(item.key);
              if (at === -1) list.push(item.key);
              else list.splice(at, 1);
              accessor.set(list);
              render();
            },
          },
          h('span', { text: item.label }),
          item.count != null ? h('span.chip__count', { text: String(item.count) }) : null
        )
      );
    });
    row.appendChild(group);
    return row;
  }

  /* --------------------------------------------------------- 会话控制 */

  /** 按范围构造一个练习会话；没有可用题目时返回 null（不渲染、不提示） */
  function buildSession(options) {
    var opts = options || {};
    var ids;
    var label;

    if (opts.scope === 'wrong') {
      ids = store.wrongIds();
      label = '错题练习';
    } else if (opts.scope === 'due') {
      ids = store.dueIds();
      label = '到期复习';
    } else if (opts.scope === 'ids') {
      ids = (opts.ids || []).slice();
      label = opts.label || '定向练习';
    } else {
      ids = filteredIds();
      label = opts.label || '练习';
    }

    if (opts.shuffle) ids = shuffle(ids);
    if (opts.limit && ids.length > opts.limit) ids = ids.slice(0, opts.limit);
    if (!ids.length) return null;

    return {
      ids: ids,
      index: 0,
      responses: {},
      results: {},
      // 大题（problem）按小问分别存作答与批改结果
      partResponses: {},
      partResults: {},
      partBusy: {},
      started: Date.now(),
      label: label,
      scope: opts.scope || 'filter',
    };
  }

  /** 由用户操作触发的开始 / 重开练习 */
  function startPractice(options) {
    var opts = options || {};
    var session = buildSession(opts);
    if (!session) {
      ui.toast('没有可练习的题目', 'warn');
      return;
    }
    // 从选题列表点某一行开始：定位到那一题，顺序仍是整个筛选结果，
    // 所以「上一题 / 下一题」在列表里前后都走得通。
    if (opts.startId) {
      var at = session.ids.indexOf(opts.startId);
      if (at > 0) session.index = at;
    }
    state.session = session;
    state.picking = false;
    if (state.view !== 'practice') go('practice');
    else render();
  }

  function move(delta) {
    if (!state.session) return;
    var next = state.session.index + delta;
    if (next < 0 || next >= state.session.ids.length) {
      if (next >= state.session.ids.length) ui.toast('已经是最后一题', 'info');
      return;
    }
    state.session.index = next;
    render();
  }

  function submitCurrent(withAI) {
    var s = state.session;
    if (!s) return;
    var q = currentQuestion();
    if (!q || s.results[q.id]) return;

    // 大题按小问交互，没有整题提交
    if (q.type === 'problem') return;

    var response = s.responses[q.id];
    if (q.type === 'short') {
      handleShortSubmit(q, response, withAI);
      return;
    }
    if (eng.isResponseEmpty(q, response)) {
      ui.toast('还没有作答', 'warn');
      return;
    }

    var result = eng.grade(q, response);
    s.results[q.id] = result;
    store.applyResult(q, response, result);
    render();

    if (result.correct) ui.toast('回答正确', 'ok', 1500);
    else if (result.status === 'partial') ui.toast('部分正确', 'warn', 1800);
  }

  function handleShortSubmit(q, response, withAI) {
    var conf = ai.config();
    var s = state.session;

    if (!withAI || !conf.enabled || !conf.baseUrl) {
      var selfResult = {
        status: 'ungraded',
        correct: false,
        score: null,
        blanks: [],
        expected: '见参考答案',
        expectedHtml: eng.expectedHtml(q),
      };
      s.results[q.id] = selfResult;
      store.applyResult(q, response, selfResult);
      render();
      return;
    }

    if (eng.isResponseEmpty(q, response)) {
      ui.toast('还没有作答', 'warn');
      return;
    }

    state.aiBusy[q.id] = true;
    render();

    ai.grade(q, response)
      .then(function (aiResult) {
        delete state.aiBusy[q.id];
        var result = ai.toResult(aiResult, q);
        s.results[q.id] = result;
        store.applyResult(q, response, result);
        render();
        ui.toast('AI 批改完成：' + aiResult.score + ' / ' + aiResult.max, 'ok');
      })
      .catch(function (err) {
        delete state.aiBusy[q.id];
        var fallback = {
          status: 'ungraded',
          correct: false,
          score: null,
          blanks: [],
          expected: '见参考答案',
          expectedHtml: eng.expectedHtml(q),
          aiError: err.message,
        };
        s.results[q.id] = fallback;
        store.applyResult(q, response, fallback);
        render();
        ui.toast('AI 批改失败：' + err.message, 'error', 7000);
      });
  }

  /* ------------------------------------------------------- 大题小问批改 */

  /** 把某道大题当前的小问结果汇总后落盘 */
  function commitProblem(question) {
    var s = state.session;
    var responses = (s.partResponses && s.partResponses[question.id]) || {};
    var partResults = (s.partResults && s.partResults[question.id]) || {};
    var aggregate = eng.aggregateParts(question, partResults, responses);
    s.results[question.id] = aggregate;
    store.applyResult(question, responses, aggregate);
    // 小问级的批改结果也落盘，错题本才能还原每一问的讲评
    store.patchRecord(question.id, { parts: partResults });
    return aggregate;
  }

  /** 把一个小问包装成 short 题，交给通用的 AI 批改链路 */
  function partAsQuestion(question, partIndex) {
    var part = (question.parts || []).filter(function (p) {
      return p.index === partIndex;
    })[0];
    if (!part) return null;
    return {
      id: question.id + '#' + partIndex,
      type: 'short',
      topic: question.topic,
      chapter: question.chapter,
      difficulty: question.difficulty,
      stem: part.stem,
      reference: part.reference,
      rubric: part.rubric,
    };
  }

  function runPartGrade(question, partIndex) {
    var s = state.session;
    var text = String(((s.partResponses[question.id] || {})[partIndex]) || '').trim();
    if (!text) return Promise.reject(new Error('这一问还没有作答'));
    var sub = partAsQuestion(question, partIndex);
    if (!sub) return Promise.reject(new Error('找不到第 ' + partIndex + ' 问'));
    return ai.grade(sub, text);
  }

  function gradeOnePart(question, partIndex) {
    var conf = ai.config();
    if (!conf.enabled || !conf.baseUrl) {
      ui.toast('需要先在设置里启用 AI 批改', 'warn');
      return;
    }
    var s = state.session;
    s.partBusy[question.id] = s.partBusy[question.id] || {};
    if (s.partBusy[question.id][partIndex]) return;
    s.partBusy[question.id][partIndex] = true;
    render();

    runPartGrade(question, partIndex)
      .then(function (res) {
        delete s.partBusy[question.id][partIndex];
        s.partResults[question.id] = s.partResults[question.id] || {};
        s.partResults[question.id][partIndex] = res;
        commitProblem(question);
        render();
        ui.toast('第 ' + partIndex + ' 问：' + res.score + ' / ' + res.max, 'ok');
      })
      .catch(function (err) {
        if (s.partBusy[question.id]) delete s.partBusy[question.id][partIndex];
        render();
        ui.toast('第 ' + partIndex + ' 问批改失败：' + err.message, 'error', 7000);
      });
  }

  /** 依次批改所有「已作答且未批改」的小问，避免并发打满接口 */
  function gradeAllParts(question) {
    var conf = ai.config();
    if (!conf.enabled || !conf.baseUrl) {
      ui.toast('需要先在设置里启用 AI 批改', 'warn');
      return;
    }
    var s = state.session;
    var responses = s.partResponses[question.id] || {};
    var results = s.partResults[question.id] || {};
    var pending = (question.parts || [])
      .map(function (p) {
        return p.index;
      })
      .filter(function (idx) {
        return String(responses[idx] || '').trim().length > 0 && !results[idx];
      });
    if (!pending.length) {
      ui.toast('没有需要批改的小问', 'info');
      return;
    }

    s.partBusy[question.id] = {};
    pending.forEach(function (idx) {
      s.partBusy[question.id][idx] = true;
    });
    render();

    var done = 0;
    var failed = 0;
    var total = pending.length;
    var cursor = 0;

    function step() {
      if (cursor >= pending.length) {
        s.partBusy[question.id] = {};
        commitProblem(question);
        render();
        ui.toast(
          '批改完成：成功 ' + done + ' 个小问' + (failed ? '，失败 ' + failed + ' 个' : ''),
          failed ? 'warn' : 'ok',
          failed ? 6000 : 2600
        );
        return;
      }
      var idx = pending[cursor];
      runPartGrade(question, idx)
        .then(function (res) {
          delete s.partBusy[question.id][idx];
          s.partResults[question.id] = s.partResults[question.id] || {};
          s.partResults[question.id][idx] = res;
          done += 1;
        })
        .catch(function (err) {
          delete s.partBusy[question.id][idx];
          failed += 1;
          if (failed === 1) ui.toast('第 ' + idx + ' 问批改失败：' + err.message, 'error', 6000);
        })
        .then(function () {
          cursor += 1;
          render();
          setTimeout(step, 120);
        });
    }
    step();
    return total;
  }

  function selfRatePart(question, partIndex, grade) {
    var s = state.session;
    var score = grade === 2 ? 10 : grade === 1 ? 5 : 0;
    s.partResults[question.id] = s.partResults[question.id] || {};
    s.partResults[question.id][partIndex] = {
      score: score,
      max: 10,
      verdict: grade === 2 ? 'correct' : grade === 1 ? 'partial' : 'wrong',
      matched: [],
      missing: [],
      errors: [],
      feedback: '自评：' + ['没答对', '部分正确', '答对了'][grade],
      selfRated: true,
    };
    commitProblem(question);
    render();
    ui.toast('第 ' + partIndex + ' 问已自评', 'info', 1500);
  }

  /** 组装大题的渲染参数 */
  function problemOptions(question, container) {
    var conf = ai.config();
    return {
      responses: container.partResponses[question.id] || {},
      partResults: (container.partResults || {})[question.id] || {},
      busy: (container.partBusy || {})[question.id] || {},
      aiEnabled: !!(conf.enabled && conf.baseUrl),
      onChange: function (partIndex, value) {
        container.partResponses[question.id] = container.partResponses[question.id] || {};
        container.partResponses[question.id][partIndex] = value;
      },
      onGrade: container.aiGrading === false
        ? null
        : function (partIndex) {
            gradeOnePart(question, partIndex);
          },
      onGradeAll: container.aiGrading === false
        ? null
        : function () {
            gradeAllParts(question);
          },
      onSelfRate: container.aiGrading === false
        ? null
        : function (partIndex, grade) {
            selfRatePart(question, partIndex, grade);
          },
      onTogglePart: function () {},
    };
  }

  /* ========================================================== 组卷模式 */

  var paperConfig = {
    topics: [],
    types: [],
    difficulty: [],
    count: 20,
    minutes: 30,
    shuffle: true,
  };

  function viewPaper() {
    if (state.exam && state.exam.report) return viewReport();
    if (state.exam) return viewExam();
    return viewPaperConfig();
  }

  function viewPaperConfig() {
    var page = h('div.page');
    page.appendChild(
      h(
        'div.page__head',
        null,
        h('h1.page__title', { text: '组卷' })
      )
    );

    var draft = store.getPaper();
    if (draft && draft.expiresAt && draft.expiresAt > Date.now()) {
      page.appendChild(
        h(
          'div.card.panel',
          { style: { marginBottom: '16px' } },
          h(
            'div.panel__head',
            null,
            h('h2.panel__title', { text: '有一份未完成的试卷' }),
            h('button.btn.btn--sm', {
              type: 'button',
              onClick: function () {
                store.clearPaper();
                render();
              },
            }, h('span', { text: '丢弃' }))
          ),
          h('p.form__note', {
            text: '开始于 ' + ui.fmtTime(draft.startedAt) + '，共 ' + (draft.ids || []).length + ' 题。',
          }),
          h(
            'div.btnrow',
            { style: { marginTop: '12px' } },
            h('button.btn.btn--primary', {
              type: 'button',
              onClick: function () {
                state.exam = normaliseExam(draft);
                startExamTimer();
                render();
              },
            }, h('span', { text: '继续作答' }))
          )
        )
      );
    }

    var panel = h('div.card.panel');
    panel.appendChild(h('div.panel__head', null, h('h2.panel__title', { text: '试卷设置' })));
    panel.appendChild(buildTopicSelector(configAccessor(paperConfig, 'topics')));
    addRow(panel, chipRow('题型', typeChipItems(), configAccessor(paperConfig, 'types')));
    addRow(panel, chipRow('难度', difficultyChipItems(), configAccessor(paperConfig, 'difficulty')));

    var countInput;
    var minuteInput;
    panel.appendChild(
      h(
        'div.form__grid',
        { style: { marginTop: '16px' } },
        h(
          'label.field',
          null,
          h('span.field__label', { text: '题量：' }),
          countInput = h('input.input', {
            type: 'number',
            min: '1',
            max: '200',
            value: String(paperConfig.count),
            onInput: function (event) {
              paperConfig.count = ui.clamp(parseInt(event.target.value, 10) || 1, 1, 200);
            },
          }),
          h('span.field__hint', { text: '可用题目会按主题尽量均衡抽取' })
        ),
        h(
          'label.field',
          null,
          h('span.field__label', { text: '时长（分钟）：' }),
          minuteInput = h('input.input', {
            type: 'number',
            min: '1',
            max: '600',
            value: String(paperConfig.minutes),
            onInput: function (event) {
              paperConfig.minutes = ui.clamp(parseInt(event.target.value, 10) || 1, 1, 600);
            },
          }),
          h('span.field__hint', { text: '时间到会自动交卷' })
        )
      )
    );

    var preview = buildPaperPreview();
    panel.appendChild(h('div', { style: { marginTop: '18px' } }, preview));
    panel.appendChild(
      h(
        'div.btnrow',
        { style: { marginTop: '18px' } },
        h('button.btn.btn--primary.btn--lg', {
          type: 'button',
          onClick: function () { buildExam(); },
        }, h('span', { html: ui.icon('play', 16) }), h('span', { text: '生成试卷并开始' }))
      )
    );

    page.appendChild(panel);
    return page;
  }

  function paperPool() {
    return D.filter({ topics: paperConfig.topics, types: paperConfig.types, difficulty: paperConfig.difficulty });
  }

  function pickBalanced(pool, count) {
    var byTopic = {};
    pool.forEach(function (q) {
      (byTopic[q.topic] = byTopic[q.topic] || []).push(q.id);
    });
    var keys = Object.keys(byTopic);
    if (paperConfig.shuffle) {
      keys.forEach(function (key) {
        byTopic[key] = shuffle(byTopic[key]);
      });
    }
    var out = [];
    var round = 0;
    while (out.length < count && keys.length) {
      var added = false;
      for (var i = 0; i < keys.length && out.length < count; i++) {
        var bucket = byTopic[keys[i]];
        if (bucket.length > round) {
          out.push(bucket[round]);
          added = true;
        }
      }
      if (!added) break;
      round += 1;
    }
    return out;
  }

  function buildPaperPreview() {
    var pool = paperPool();
    var ids = pickBalanced(pool, paperConfig.count);
    var counts = {};
    ids.forEach(function (id) {
      var q = question(id);
      // 按一级主题聚合。直接按题目所在的 topic 分组会散出十几二十个
      // 知识点胶囊（等于把主题树拍平成一堆标签），既难读也没意义。
      if (q) {
        var subject = D.topicPath(q.topic)[0];
        counts[subject] = (counts[subject] || 0) + 1;
      }
    });
    var keys = Object.keys(counts);
    var bar = h('div.compose-bar');
    var legend = h('div.compose-legend');
    keys.forEach(function (key) {
      var color = D.topicColor(key);
      var ratio = ids.length ? counts[key] / ids.length : 0;
      bar.appendChild(h('i.compose-bar__seg', { style: { width: (ratio * 100).toFixed(1) + '%', '--seg': color } }));
      legend.appendChild(
        h('span', { style: { '--seg': color } }, h('i'), h('span', { text: D.topicName(key) + ' ' + counts[key] }))
      );
    });
    if (!keys.length) {
      bar.appendChild(h('i.compose-bar__seg', { style: { width: '100%', background: 'var(--line)' } }));
      legend.appendChild(h('span', { text: '当前筛选下没有可用题目' }));
    }
    return h(
      'div',
      null,
      h(
        'div.statline',
        null,
        h('span', { text: '命中题库' }),
        h('b', { text: pool.length + ' 题' }),
        h('span', { text: '· 本次抽取' }),
        h('b', { text: ids.length + ' 题' })
      ),
      bar,
      legend
    );
  }

  function buildExam() {
    var pool = paperPool();
    if (!pool.length) {
      ui.toast('当前筛选下没有可用题目', 'warn');
      return;
    }
    var ids = pickBalanced(pool, paperConfig.count);
    var exam = {
      ids: ids,
      responses: {},
      // 大题的小问作答（考试中不做 AI 批改，交卷后到练习/错题本里批改）
      partResponses: {},
      partResults: {},
      partBusy: {},
      aiGrading: false,
      flags: {},
      index: 0,
      startedAt: Date.now(),
      minutes: paperConfig.minutes,
      expiresAt: Date.now() + paperConfig.minutes * 60000,
      report: null,
    };
    state.exam = exam;
    store.savePaper({
      ids: exam.ids,
      responses: exam.responses,
      flags: exam.flags,
      startedAt: exam.startedAt,
      minutes: exam.minutes,
      expiresAt: exam.expiresAt,
    });
    startExamTimer();
    render();
  }

  /** 取出某题在试卷里的作答（大题走小问作答） */
  function examResponse(exam, qid) {
    var q = question(qid);
    if (q && q.type === 'problem') return exam.partResponses[qid] || {};
    return exam.responses[qid];
  }

  function examAnswered() {
    var exam = state.exam;
    if (!exam) return 0;
    return exam.ids.filter(function (id) {
      return !eng.isResponseEmpty(question(id), examResponse(exam, id));
    }).length;
  }

  function startExamTimer() {
    stopExamTimer();
    state.examTick = setInterval(function () {
      var exam = state.exam;
      if (!exam || exam.report) return;
      var remain = exam.expiresAt - Date.now();
      if (remain <= 0) {
        stopExamTimer();
        ui.toast('时间到，自动交卷', 'warn');
        submitExam();
        return;
      }
      var clock = document.getElementById('exam-clock');
      if (clock) {
        clock.textContent = ui.fmtDuration(remain);
        clock.className =
          'timerbar__clock' + (remain <= 30000 ? ' is-danger' : remain <= 120000 ? ' is-warn' : '');
      }
    }, 500);
  }

  function stopExamTimer() {
    if (state.examTick) {
      clearInterval(state.examTick);
      state.examTick = null;
    }
  }

  /** 补齐从 localStorage 恢复的草稿里可能缺失的字段 */
  function normaliseExam(draft) {
    var exam = draft || {};
    exam.responses = exam.responses || {};
    exam.partResponses = exam.partResponses || {};
    exam.partResults = exam.partResults || {};
    exam.partBusy = {};
    exam.flags = exam.flags || {};
    exam.aiGrading = false;
    exam.index = 0;
    exam.report = exam.report || null;
    return exam;
  }

  function viewExam() {
    var exam = state.exam;
    var page = h('div.page');
    var remain = Math.max(0, exam.expiresAt - Date.now());
    var clock = h('span.timerbar__clock', {
      id: 'exam-clock',
      class: remain <= 30000 ? 'is-danger' : remain <= 120000 ? 'is-warn' : '',
      text: ui.fmtDuration(remain),
    });
    page.appendChild(
      h(
        'div.timerbar',
        null,
        h('span', { html: ui.icon('clock', 18) }),
        clock,
        h('span.badge.badge--mono', { text: exam.ids.length + ' 题' }),
        h('span.badge', { text: '已答 ' + examAnswered() + ' / ' + exam.ids.length }),
        h('span.spacer'),
        h('button.btn.btn--ghost.btn--sm', {
          type: 'button',
          onClick: function () { quitExam(); },
        }, h('span', { text: '退出' })),
        h('button.btn.btn--primary', {
          type: 'button',
          onClick: function () { submitExam(); },
        }, h('span', { html: ui.icon('check', 15) }), h('span', { text: '交卷' }))
      )
    );

    var q = question(exam.ids[exam.index]);
    if (!q) {
      page.appendChild(emptyState('warn', '题目不存在', '试卷数据可能已损坏，请重新组卷。'));
      return page;
    }
    var response = exam.responses[q.id];
    var flagged = !!exam.flags[q.id];

    var card = qv.card(q, {
      response: response,
      problem: q.type === 'problem' ? problemOptions(q, exam) : null,
      onChange: function (value) {
        exam.responses[q.id] = value;
        persistExamDebounced();
        // 只有多选需要即时重渲染；填空 / 简答保持 DOM 不动以免丢失焦点
        if (q.type === 'multi') render();
      },
      headerExtra: h(
        'button.icon-btn',
        {
          type: 'button',
          class: flagged ? 'is-on' : '',
          title: '标记本题',
          html: ui.icon('flag', 16),
          onClick: function () {
            exam.flags[q.id] = !exam.flags[q.id];
            persistExam();
            render();
          },
        }
      ),
      footer: h(
        'div.qcard__foot',
        null,
        h('button.btn', {
          type: 'button',
          disabled: exam.index === 0,
          onClick: function () { exam.index -= 1; render(); },
        }, h('span', { html: ui.icon('chevronL', 15) }), h('span', { text: '上一题' })),
        h('button.btn.btn--primary', {
          type: 'button',
          disabled: exam.index >= exam.ids.length - 1,
          onClick: function () { exam.index += 1; render(); },
        }, h('span', { text: '下一题' }), h('span', { html: ui.icon('chevronR', 15) })),
        h('span.spacer'),
        h('span.badge', { text: '第 ' + (exam.index + 1) + ' / ' + exam.ids.length + ' 题' })
      ),
    });

    var aside = h('div.exam-aside');
    var navBox = h('div.card.panel');
    navBox.appendChild(h('div.panel__head', null, h('h2.panel__title', { text: '答题卡' })));
    var nav = h('div.qnav');
    exam.ids.forEach(function (id, index) {
      var item = question(id);
      var answered = item ? !eng.isResponseEmpty(item, examResponse(exam, id)) : false;
      var cls = index === exam.index ? ' is-current' : answered ? ' is-right' : '';
      nav.appendChild(
        h('button.qnav__item', {
          type: 'button',
          class: cls,
          text: String(index + 1),
          title: (exam.flags[id] ? '已标记 · ' : '') + id,
          onClick: function () {
            exam.index = index;
            render();
          },
        })
      );
    });
    navBox.appendChild(nav);
    navBox.appendChild(
      h(
        'div.btnrow',
        { style: { marginTop: '14px' } },
        h('button.btn.btn--sm', {
          type: 'button',
          onClick: function () {
            var next = exam.ids.findIndex(function (id) {
              return eng.isResponseEmpty(question(id), exam.responses[id]);
            });
            if (next === -1) ui.toast('所有题目都已作答', 'ok');
            else {
              exam.index = next;
              render();
            }
          },
        }, h('span', { text: '跳到第一道未答题' }))
      )
    );
    aside.appendChild(navBox);

    var grid = h('div.exam-grid');
    grid.appendChild(h('div', null, card));
    grid.appendChild(aside);
    page.appendChild(grid);
    return page;
  }

  function persistExam() {
    var exam = state.exam;
    if (!exam) return;
    store.savePaper({
      ids: exam.ids,
      responses: exam.responses,
      partResponses: exam.partResponses || {},
      flags: exam.flags,
      startedAt: exam.startedAt,
      minutes: exam.minutes,
      expiresAt: exam.expiresAt,
    });
  }

  /** 连续输入（简答/填空）时不要每个字符都写一次 localStorage */
  var persistExamDebounced = ui.debounce(persistExam, 500);

  function quitExam() {
    ui.confirm('退出后本次试卷会保留，可以在「组卷」页继续作答。确定退出吗？', { okLabel: '退出' }).then(function (ok) {
      if (!ok) return;
      stopExamTimer();
      persistExam();
      state.exam = null;
      go('home');
    });
  }

  function submitExam() {
    var exam = state.exam;
    if (!exam) return;
    var unanswered = exam.ids.length - examAnswered();

    function finish() {
      stopExamTimer();
      var results = {};
      var right = 0;
      var partial = 0;
      var wrong = 0;
      var ungraded = 0;
      var scores = [];

      exam.ids.forEach(function (id) {
        var q = question(id);
        var response = examResponse(exam, id);
        var r = eng.grade(q, response);
        var answered = !eng.isResponseEmpty(q, response);
        results[id] = r;
        if (r.status === 'correct') right += 1;
        else if (r.status === 'partial') partial += 1;
        else if (r.status === 'wrong') wrong += 1;
        // 简答 / 大题未作答时 grade 也会返回 ungraded，不能重复计入「待自评」
        else if (r.status === 'ungraded' && answered) ungraded += 1;
        if (typeof r.score === 'number') scores.push(r.score);
        if (answered) store.applyResult(q, response, r);
      });

      var total = exam.ids.length || 1;
      var scoreSum = scores.reduce(function (a, b) { return a + b; }, 0);
      exam.report = {
        results: results,
        right: right,
        partial: partial,
        wrong: wrong,
        ungraded: ungraded,
        scored: scores.length,
        score: scoreSum,
        maxScore: scores.length,
        ratio: exam.ids.length ? (right + partial * 0.5) / total : 0,
        accuracy: exam.ids.length ? right / total : 0,
        usedMs: Date.now() - exam.startedAt,
        unanswered: unanswered,
      };
      store.clearPaper();
      render();
    }

    if (unanswered > 0) {
      ui.confirm('还有 ' + unanswered + ' 道题没有作答，仍然交卷吗？', { okLabel: '交卷', danger: false }).then(function (ok) {
        if (ok) finish();
      });
    } else {
      finish();
    }
  }

  function viewReport() {
    var exam = state.exam;
    var rep = exam.report;
    var page = h('div.page');
    page.appendChild(
      h(
        'div.page__head',
        null,
        h('h1.page__title', { text: '成绩报告' }),
        h('p.page__sub', { text: '用时 ' + ui.fmtDuration(rep.usedMs) + ' · 共 ' + exam.ids.length + ' 题' })
      )
    );

    page.appendChild(
      h(
        'div.card.report__hero',
        null,
        ui.ring(rep.ratio, { size: 96, stroke: 8, label: Math.round(rep.ratio * 100) + '%' }),
        h(
          'div.report__meta',
          null,
          h('div', { html: '<b>' + rep.right + '</b> 题完全正确' }),
          h('div', { html: '<b>' + rep.partial + '</b> 题部分正确' }),
          h('div', { html: '<b>' + rep.wrong + '</b> 题错误' }),
          rep.ungraded ? h('div', { html: '<b>' + rep.ungraded + '</b> 题简答待自评' }) : null,
          rep.unanswered ? h('div', { html: '<b>' + rep.unanswered + '</b> 题未作答' }) : null
        ),
        h('span.spacer', { style: { flex: '1 1 auto' } }),
        h(
          'div',
          null,
          h('div.report__score', { html: rep.accuracy ? (rep.accuracy * 100).toFixed(0) + '<small>% 准确率</small>' : '—' })
        )
      )
    );

    // 分主题正确率
    var byTopic = {};
    exam.ids.forEach(function (id) {
      var q = question(id);
      if (!q) return;
      var bucket = byTopic[q.topic] || (byTopic[q.topic] = { total: 0, right: 0, partial: 0 });
      bucket.total += 1;
      var r = rep.results[id];
      if (r && r.status === 'correct') bucket.right += 1;
      else if (r && r.status === 'partial') bucket.partial += 1;
    });

    page.appendChild(h('div.section__title', { text: '分主题表现' }));
    page.appendChild(
      h(
        'div.report__grid',
        null,
        Object.keys(byTopic).map(function (key) {
          var bucket = byTopic[key];
          var rate = bucket.total ? (bucket.right + bucket.partial * 0.5) / bucket.total : 0;
          return h(
            'div.topicrate',
            { style: { '--tc': D.topicColor(key) } },
            h(
              'div.topicrate__head',
              null,
              h('span', { text: D.topicName(key) }),
              h('span.topicrate__val', { text: bucket.right + ' / ' + bucket.total })
            ),
            ui.progressBar(rate, rate >= 0.8 ? 'ok' : rate >= 0.5 ? 'amber' : 'bad')
          );
        })
      )
    );

    // 错题清单
    var wrongIds = exam.ids.filter(function (id) {
      var r = rep.results[id];
      return r && !r.correct;
    });
    page.appendChild(h('div.section__title', { text: '需要复盘的题目（' + wrongIds.length + '）' }));
    if (!wrongIds.length) {
      page.appendChild(emptyState('check', '全部答对', '这份试卷没有需要复盘的题目。'));
    } else {
      var STATUS_BADGE = {
        wrong: { text: '错', cls: 'badge--bad' },
        partial: { text: '部分正确', cls: 'badge--amber' },
        ungraded: { text: '待自评', cls: 'badge--amber' },
        empty: { text: '未作答', cls: '' },
      };
      var list = h('div.reportlist');
      wrongIds.forEach(function (id) {
        var q = question(id);
        var r = rep.results[id] || {};
        var badge = STATUS_BADGE[r.status] || STATUS_BADGE.empty;
        list.appendChild(
          h(
            'button.reportrow',
            {
              type: 'button',
              onClick: function () {
                state.exam = null;
                // 统一走 startPractice：它会把 partResponses 等字段补齐，并切到做题态
                startPractice({ scope: 'ids', ids: wrongIds, label: '试卷错题复盘', startId: id });
              },
            },
            h('span.reportrow__idx', { text: id }),
            h('span.reportrow__text', { text: q ? md.plain(q.stem, 90) : '（题目已不在题库中）' }),
            h('span.reportrow__tag', null, h('span.badge.badge--topic', {
              text: q ? D.topicName(q.topic) : '—',
              style: q ? { '--tc': D.topicColor(q.topic) } : null,
            })),
            h('span.reportrow__tag', null, h('span.badge', { class: badge.cls, text: badge.text }))
          )
        );
      });
      page.appendChild(list);
    }

    page.appendChild(
      h(
        'div.btnrow',
        { style: { marginTop: '22px' } },
        h('button.btn.btn--primary', {
          type: 'button',
          onClick: function () {
            state.exam = null;
            startPractice({ scope: 'ids', ids: wrongIds, label: '试卷错题复盘' });
          },
          disabled: !wrongIds.length,
        }, h('span', { html: ui.icon('play', 15) }), h('span', { text: '只练这些错题' })),
        h('button.btn', {
          type: 'button',
          onClick: function () {
            state.exam = null;
            render();
          },
        }, h('span', { html: ui.icon('refresh', 15) }), h('span', { text: '再组一份' })),
        h('button.btn.btn--ghost', {
          type: 'button',
          onClick: function () { window.print(); },
        }, h('span', { html: ui.icon('print', 15) }), h('span', { text: '打印报告' }))
      )
    );

    return page;
  }

  /* ========================================================== 复习模式 */

  function viewReview() {
    var page = h('div.page');
    var overview = sm2.overview(store.records(), store.allIds());

    page.appendChild(
      h(
        'div.page__head',
        null,
        h('h1.page__title', { text: '复习' })
      )
    );

    if (state.review) return viewReviewSession(page);

    page.appendChild(
      h(
        'div.statgrid',
        null,
        statcard('今日到期', String(overview.dueToday), '题', overview.dueToday ? '建议今天完成' : '暂无到期', overview.dueToday ? 'amber' : ''),
        statcard('未来 7 天', String(overview.week), '题', '提前预习也可以'),
        statcard('在学题目', String(overview.learning), '题', '已排入复习计划'),
        statcard('未开始', String(overview.untouched), '题', '还没接触过的题目')
      )
    );

    var dueIds = store.dueIds();
    page.appendChild(
      h(
        'div.card.panel',
        { style: { marginTop: '20px' } },
        h('div.panel__head', null, h('h2.panel__title', { text: dueIds.length ? '开始复习' : '暂时没有到期的题目' })),
        h('p.form__note', {
          text: dueIds.length
            ? '将按到期时间从早到晚依次推送 ' + dueIds.length + ' 道题。'
            : '可以去练习模式新增一些题目，系统会自动把它们排入复习计划。',
        }),
        h(
          'div.btnrow',
          { style: { marginTop: '14px' } },
          h('button.btn.btn--primary.btn--lg', {
            type: 'button',
            disabled: !dueIds.length,
            onClick: startReview,
          }, h('span', { html: ui.icon('refresh', 16) }), h('span', { text: '开始复习' + (dueIds.length ? '（' + dueIds.length + ' 题）' : '') })),
          h('button.btn', {
            type: 'button',
            onClick: function () {
              var recent = store.wrongIds().slice(0, 20);
              if (!recent.length) {
                ui.toast('还没有错题可以复习', 'warn');
                return;
              }
              startPractice({ scope: 'ids', ids: recent, label: '错题强化' });
            },
          }, h('span', { html: ui.icon('close', 15) }), h('span', { text: '用错题热身' }))
        )
      )
    );

    // 复习日历（未来 14 天）
    page.appendChild(h('div.section__title', { text: '近期复习负载' }));
    var buckets = new Array(14).fill(0);
    var now = Date.now();
    Object.keys(store.records()).forEach(function (id) {
      var rec = store.records()[id];
      if (!rec.sm2 || rec.mastered) return;
      var delta = Math.floor((rec.sm2.due - now) / sm2.DAY);
      if (delta >= 0 && delta < 14) buckets[delta] += 1;
      if (delta < 0) buckets[0] += 1;
    });
    var maxCount = Math.max.apply(null, buckets.concat([1]));
    var chart = h('div', { style: { display: 'grid', gap: '6px', gridTemplateColumns: 'repeat(14, 1fr)', alignItems: 'end', height: '130px' } });
    buckets.forEach(function (count, index) {
      var ratio = count / maxCount;
      chart.appendChild(
        h(
          'div',
          {
            title: (index === 0 ? '今天' : index + ' 天后') + '：' + count + ' 题',
            style: {
              display: 'flex', flexDirection: 'column', justifyContent: 'flex-end', alignItems: 'center',
              height: '100%', gap: '6px',
            },
          },
          h('span', { style: { fontFamily: 'var(--font-mono)', fontSize: '11px', color: 'var(--fg3)' }, text: count ? String(count) : '' }),
          h('i', {
            style: {
              display: 'block', width: '100%',
              height: Math.max(3, ratio * 78) + 'px',
              borderRadius: '5px 5px 2px 2px',
              background: index === 0 ? 'var(--amber)' : 'linear-gradient(180deg, var(--pri), var(--pri-2))',
              opacity: count ? 1 : 0.16,
            },
          }),
          h('span', { style: { fontSize: '10.5px', color: 'var(--fg3)' }, text: index === 0 ? '今天' : '+' + index })
        )
      );
    });
    page.appendChild(h('div.card.panel', null, chart));

    return page;
  }

  function startReview() {
    var ids = store.dueIds();
    if (!ids.length) {
      ui.toast('没有到期的题目', 'info');
      return;
    }
    state.review = { ids: ids, index: 0, flipped: false, done: 0, grades: {} };
    render();
  }

  function viewReviewSession(page) {
    var rev = state.review;
    var q = question(rev.ids[rev.index]);

    if (!q) {
      rev.index = 0;
      rev.ids = rev.ids.filter(function (id) { return !!question(id); });
      if (!rev.ids.length) {
        state.review = null;
        return viewReview();
      }
      q = question(rev.ids[rev.index]);
    }

    if (rev.index >= rev.ids.length) {
      page.appendChild(
        h(
          'div.card.panel',
          null,
          h('h2.panel__title', { text: '本轮复习完成' }),
          h('p.form__note', { text: '共复习 ' + rev.done + ' 道题。复习记录已经保存，下次到期时会自动出现。' }),
          h(
            'div.btnrow',
            { style: { marginTop: '14px' } },
            h('button.btn.btn--primary', {
              type: 'button',
              onClick: function () {
                state.review = null;
                go('home');
              },
            }, h('span', { text: '回到工作台' })),
            h('button.btn', {
              type: 'button',
              disabled: !store.dueIds().length,
              onClick: startReview,
            }, h('span', { text: '继续复习下一批' }))
          )
        )
      );
      return page;
    }

    var rec = store.record(q.id);
    var stage = h('div.review-stage');
    var inner = h('div.flipcard__inner');
    var front = h('div.flipcard__face', null, qv.card(q, {
      response: rev.responses ? rev.responses[q.id] : undefined,
      onChange: function (value) {
        rev.responses = rev.responses || {};
        rev.responses[q.id] = value;
      },
      headerExtra: h(
        'span',
        null,
        h('span.badge', { text: '第 ' + (rev.index + 1) + ' / ' + rev.ids.length + ' 题' }),
        rec && rec.sm2 && rec.sm2.reps ? h('span.badge.badge--mono', { text: '已复习 ' + rec.sm2.reps + ' 次' }) : null,
        rec && rec.sm2 && rec.sm2.lapses ? h('span.badge.badge--bad', { text: '遗忘 ' + rec.sm2.lapses + ' 次' }) : null
      ),
      footer: h(
        'div.qcard__foot',
        null,
        h('button.btn.btn--primary', {
          type: 'button',
          onClick: function () {
            rev.flipped = true;
            render();
          },
        }, h('span', { text: '显示答案' })),
        h('span.spacer'),
        h('span.badge', { text: '自评后自动安排下次复习' })
      ),
    }));

    var card = h('div.flipcard', { class: rev.flipped ? 'is-flipped' : '' });
    if (rev.flipped) {
      var back = h(
        'div.flipcard__face.flipcard__face--back',
        null,
        qv.explainPanel(q, { open: true })
      );
      inner.appendChild(front);
      inner.appendChild(back);
    } else {
      inner.appendChild(front);
    }
    card.appendChild(inner);
    stage.appendChild(card);
    page.appendChild(stage);

    if (rev.flipped) {
      var bar = h(
        'div.gradebar',
        null,
        sm2.grades.map(function (grade, index) {
          var preview = sm2.preview(rec && rec.sm2, index);
          return h(
            'button.gradebtn',
            {
              type: 'button',
              class: 'gradebtn--' + grade.tone,
              onClick: function () {
                store.setSelfGrade(q.id, index);
                rev.grades[q.id] = index;
                rev.done += 1;
                rev.index += 1;
                rev.flipped = false;
                render();
              },
              onMouseenter: function (event) {
                var due = event.currentTarget.querySelector('.gradebtn__due');
                if (due) due.textContent = preview.text;
              },
              onMouseleave: function (event) {
                var due = event.currentTarget.querySelector('.gradebtn__due');
                if (due) due.textContent = grade.hint;
              },
            },
            h('span.gradebtn__label', { text: grade.label }),
            h('span.gradebtn__due', { text: grade.hint }),
            h('span.gradebtn__due', { text: '→ ' + preview.text })
          );
        })
      );
      page.appendChild(
        h(
          'div.card.panel',
          { style: { marginTop: '18px' } },
          h('div.section__title', { style: { marginBottom: '4px' } }, h('span', { text: '这道题你答得怎么样？' })),
          bar,
          h(
            'div.btnrow',
            { style: { marginTop: '14px' } },
            h('span.sb__hint', null, h('kbd', { text: '1' }), h('span', { text: '重来' })),
            h('span.sb__hint', null, h('kbd', { text: '2' }), h('span', { text: '困难' })),
            h('span.sb__hint', null, h('kbd', { text: '3' }), h('span', { text: '一般' })),
            h('span.sb__hint', null, h('kbd', { text: '4' }), h('span', { text: '简单' }))
          )
        )
      );
    }

    return page;
  }

  /* ======================================================== 设置弹窗 */

  function openSettings() {
    var conf = store.settings();
    var aiConf = conf.ai;
    var resultBox = h('div');

    function field(label, hint, node) {
      return h('label.field', null, h('span.field__label', { text: label }), node, hint ? h('span.field__hint', { text: hint }) : null);
    }

    function switchRow(title, desc, value, onToggle) {
      var sw = h('button.switch', {
        type: 'button',
        class: value ? 'is-on' : '',
        role: 'switch',
        'aria-checked': value ? 'true' : 'false',
        onClick: function () {
          var next = !sw.classList.contains('is-on');
          sw.classList.toggle('is-on', next);
          sw.setAttribute('aria-checked', next ? 'true' : 'false');
          onToggle(next);
        },
      });
      return h('div.switchrow', null, h('div.switchrow__text', null, h('div.switchrow__title', { text: title }), desc ? h('div.switchrow__desc', { text: desc }) : null), sw);
    }

    var apiKeyInput = h('input.input.input--mono', {
      type: 'password',
      autocomplete: 'off',
      placeholder: 'sk-…（只保存在本机 localStorage）',
      value: aiConf.apiKey || '',
      onInput: function (event) {
        store.saveSettings({ ai: { apiKey: event.target.value.trim() } });
      },
    });
    var baseUrlInput = h('input.input.input--mono', {
      type: 'text',
      spellcheck: 'false',
      value: aiConf.baseUrl || '',
      onInput: function (event) {
        store.saveSettings({ ai: { baseUrl: event.target.value.trim() } });
      },
    });
    var modelInput = h('input.input.input--mono', {
      type: 'text',
      spellcheck: 'false',
      value: aiConf.model || '',
      onInput: function (event) {
        store.saveSettings({ ai: { model: event.target.value.trim() } });
      },
    });

    // **每个用户用自己的密钥**：这三项在两种形态下都要能填。
    // 在线版填好之后存在自己的账号里，换设备不用重填（服务端只是代你转发，
    // 因为不少模型供应商不允许浏览器直连）。
    // 唯一的例外是「本机导出产物内置了密钥」——那种情况下再让你填一遍没有意义。
    var injected = store.injectedAi;
    var builtIn = !!(injected && String(injected.apiKey || '').trim());
    var online = !!(QF.online && QF.api);
    var connectionBlock = builtIn
      ? h(
          'div',
          { style: { marginTop: '12px' } },
          field(
            '接口与密钥',
            '已随本次构建内置，无需在此填写。更换接口请修改本机配置后重新构建。',
            h(
              'div.statline',
              {
                style: {
                  padding: '9px 12px',
                  border: '1px solid var(--line)',
                  borderRadius: '10px',
                  background: 'var(--bg2)',
                  fontFamily: 'var(--font-mono)',
                  fontSize: '12.5px',
                },
              },
              h('span', { html: ui.icon('check', 15), style: { color: 'var(--ok)' } }),
              h('span', { text: '已内置' }),
              h('span', { style: { flex: '1 1 auto' } }),
              h('span', {
                style: { color: 'var(--fg2)' },
                text: String(aiConf.baseUrl || '') + ' · ' + String(aiConf.model || ''),
              })
            )
          )
        )
      : h(
          'div',
          null,
          h(
            'div.form__grid',
            { style: { marginTop: '12px' } },
            field('接口地址（OpenAI 兼容）', '例如 https://api.deepseek.com/v1', baseUrlInput),
            field('模型名', '例如 deepseek-chat / gpt-4o-mini', modelInput)
          ),
          h(
            'div',
            { style: { marginTop: '12px' } },
            field(
              'API 密钥',
              online
                ? '你自己的密钥，保存在你的账号里 —— 换设备不用重填。本站不提供共享密钥，AI 批改的用量记在你的账上。'
                : '仅存在本机浏览器中，不会写入构建产物；导出的数据也不含密钥。',
              apiKeyInput
            )
          )
        );

    var aiForm = h(
      'div.form__section',
      null,
      h('div.form__sectiontitle', { text: 'AI 批改（简答题）' }),
      switchRow('启用 AI 批改', '关闭时简答题提交后直接显示参考答案，由你自己判断对错。', aiConf.enabled, function (value) {
        store.saveSettings({ ai: { enabled: value } });
      }),
      connectionBlock,
      switchRow('要求模型返回 JSON', '若供应商不支持 response_format=json_object，会自动去掉该参数重试。', aiConf.jsonMode !== false, function (value) {
        store.saveSettings({ ai: { jsonMode: value } });
      }),
      switchRow('失败时降级为自评', 'AI 不可用时仍然可以按参考答案自评并排入复习计划。', aiConf.fallbackSelfRate !== false, function (value) {
        store.saveSettings({ ai: { fallbackSelfRate: value } });
      }),
      h(
        'div.btnrow',
        { style: { marginTop: '12px' } },
        h('button.btn', {
          type: 'button',
          onClick: function () {
            var btn = this;
            ui.clear(resultBox);
            resultBox.appendChild(h('div.statline', null, h('span.spinner'), h('span', { text: '正在测试连接…' })));
            ai.ping().then(function (res) {
              ui.clear(resultBox);
              if (res.ok) {
                resultBox.appendChild(
                  h('div.statline', { style: { color: 'var(--ok)' } },
                    h('span', { html: ui.icon('check', 15) }),
                    h('span', { text: '连接成功 · ' + res.latencyMs + ' ms · 模型 ' + res.model + ' · 回显「' + res.sample + '」' }))
                );
              } else {
                resultBox.appendChild(
                  h('div.statline', { style: { color: 'var(--bad)' } },
                    h('span', { html: ui.icon('warn', 15) }),
                    h('span', { text: res.message }))
                );
              }
              resultBox.appendChild(h('div.staturl', { text: '请求地址：' + res.url }));
            });
            window.setTimeout(function () { btn.blur(); }, 0);
          },
        }, h('span', { html: ui.icon('spark', 15) }), h('span', { text: '测试连接' }))
      ),
      resultBox,
      h('p.form__note', {
        style: { marginTop: '12px' },
        html:
          '提示：浏览器直连模型接口需要该服务允许跨域（CORS），并且密钥会出现在页面请求中——' +
          '仅在你自己信任的本机环境使用。<br>若接口不允许跨域，可改用本地服务（如 <code>ollama</code>，' +
          '地址填 <code>http://localhost:11434/v1</code>）并通过 <code>OLLAMA_ORIGINS=*</code> 放开跨域。',
      })
    );

    var fontScale = h('input.slider', {
      type: 'range',
      min: '0.85',
      max: '1.35',
      step: '0.05',
      value: String(conf.fontScale || 1),
      onInput: function (event) {
        var value = parseFloat(event.target.value);
        store.saveSettings({ fontScale: value });
        document.documentElement.style.setProperty('--font-scale', String(value));
      },
    });
    var maxWidth = h('input.slider', {
      type: 'range',
      min: '680',
      max: '1200',
      step: '20',
      value: String(conf.maxWidth || 880),
      onInput: function (event) {
        var value = parseInt(event.target.value, 10);
        store.saveSettings({ maxWidth: value });
        document.documentElement.style.setProperty('--content-max', value + 'px');
      },
    });

    var appearanceForm = h(
      'div.form__section',
      null,
      h('div.form__sectiontitle', { text: '外观' }),
      switchRow('浅色主题', '长时间阅读公式与代码时，可切换到低强度浅色配色。', conf.theme === 'light', function (value) {
        ui.theme.set(value ? 'light' : 'dark');
      }),
      field('字号缩放', null, fontScale),
      field('内容最大宽度（像素）', null, maxWidth)
    );

    var stats = store.stats();
    var dataForm = h(
      'div.form__section',
      null,
      h('div.form__sectiontitle', { text: '数据' }),
      h('div.statline', null, h('span', { text: '本地数据体积' }), h('b', { text: ui.fmtBytes(stats.storageBytes) })),
      h('div.statline', null, h('span', { text: '存储状态' }), h('b', { text: store.available ? 'localStorage 可用' : '不可用（内存模式，刷新会丢失）' })),
      h('div.statline', null, h('span', { text: '题库生成时间' }), h('b', { text: D.generatedAt || '未知' })),
      h(
        'div.btnrow',
        { style: { marginTop: '12px' } },
        h('button.btn', {
          type: 'button',
          onClick: function () {
            downloadJSON('quizforge-progress-' + ui.dayKey() + '.json', store.exportAll());
            ui.toast('已导出进度数据', 'ok');
          },
        }, h('span', { html: ui.icon('download', 15) }), h('span', { text: '导出全部数据' })),
        h('button.btn', {
          type: 'button',
          onClick: function () {
            pickFile(function (text) {
              try {
                var payload = JSON.parse(text);
                var merged = store.importAll(payload);
                ui.toast('已导入 ' + merged.records + ' 条记录', 'ok');
                render();
              } catch (err) {
                ui.toast('导入失败：' + err.message, 'error');
              }
            });
          },
        }, h('span', { html: ui.icon('upload', 15) }), h('span', { text: '导入数据' })),
        h('button.btn.btn--danger', {
          type: 'button',
          onClick: function () {
            ui.confirm('将清空本机所有作答记录、错题与复习计划，且无法恢复。确定继续吗？', {
              okLabel: '清空',
              danger: true,
            }).then(function (ok) {
              if (!ok) return;
              store.resetAll();
              ui.toast('已清空本机数据', 'ok');
              go('home');
            });
          },
        }, h('span', { html: ui.icon('trash', 15) }), h('span', { text: '清空进度' }))
      )
    );

    ui.modal({
      title: '设置',
      size: 'lg',
      body: h('div.form', null, aiForm, appearanceForm, dataForm),
      actions: [{ label: '完成', kind: 'primary' }],
    });
  }

  function downloadJSON(filename, payload) {
    var blob = new Blob([JSON.stringify(payload, null, 1)], { type: 'application/json' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () {
      URL.revokeObjectURL(url);
    }, 1000);
  }

  function pickFile(onLoad) {
    var input = document.createElement('input');
    input.type = 'file';
    input.accept = '.json,application/json';
    input.onchange = function () {
      var file = input.files && input.files[0];
      if (!file) return;
      var reader = new FileReader();
      reader.onload = function () {
        onLoad(String(reader.result));
      };
      reader.onerror = function () {
        ui.toast('读取文件失败', 'error');
      };
      reader.readAsText(file);
    };
    input.click();
  }

  /* ========================================================== 空状态 */

  function emptyState(iconName, title, text, actions) {
    return h(
      'div.empty',
      null,
      h('span.empty__icon', { html: ui.icon(iconName, 24) }),
      h('div.empty__title', { text: title }),
      text ? h('div.empty__text', { text: text }) : null,
      actions && actions.length
        ? h(
            'div.btnrow',
            null,
            actions.map(function (action) {
              return h('button.btn', { type: 'button', onClick: action.onClick }, h('span', { text: action.label }));
            })
          )
        : null
    );
  }

  /* ========================================================== 快捷键 */

  function isTyping(target) {
    if (!target) return false;
    var tag = target.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.isContentEditable;
  }

  function bindKeys() {
    document.addEventListener('keydown', function (event) {
      if (event.metaKey || event.ctrlKey || event.altKey) return;

      var modalRoot = document.getElementById('modal-root');
      if (modalRoot && !modalRoot.hidden) return;

      // 同样只在做题态接管键盘：选题态下方向键应该正常滚动题库
      if (state.view === 'practice' && state.session && !state.picking) {
        var q = currentQuestion();
        if (!q) return;
        var typing = isTyping(event.target);

        if (event.key === 'ArrowRight') {
          if (!typing) { event.preventDefault(); move(1); }
          return;
        }
        if (event.key === 'ArrowLeft') {
          if (!typing) { event.preventDefault(); move(-1); }
          return;
        }
        if (event.key === 'Enter') {
          if (typing && q.type === 'short') return; // short 用 ⌘+Enter
          event.preventDefault();
          if (state.session.results[q.id]) move(1);
          else if (q.type !== 'single') submitCurrent();
          return;
        }
        if ((q.type === 'single' || q.type === 'multi') && /^[1-9]$/.test(event.key) && !typing) {
          var index = parseInt(event.key, 10) - 1;
          var option = (q.options || [])[index];
          if (!option) return;
          event.preventDefault();
          if (state.session.results[q.id]) return;
          if (q.type === 'single') {
            state.session.responses[q.id] = option.key;
            submitCurrent();
          } else {
            var current = (state.session.responses[q.id] || []).slice();
            var at = current.indexOf(option.key);
            if (at === -1) current.push(option.key);
            else current.splice(at, 1);
            current.sort();
            state.session.responses[q.id] = current;
            render();
          }
        }
        return;
      }

      if (state.view === 'review' && state.review) {
        if (event.key === ' ' && !state.review.flipped) {
          event.preventDefault();
          state.review.flipped = true;
          render();
          return;
        }
        if (state.review.flipped && /^[1-4]$/.test(event.key)) {
          event.preventDefault();
          var grade = parseInt(event.key, 10) - 1;
          var qq = question(state.review.ids[state.review.index]);
          if (!qq) return;
          store.setSelfGrade(qq.id, grade);
          state.review.done += 1;
          state.review.index += 1;
          state.review.flipped = false;
          render();
        }
      }
    });
  }

  function bindShell() {
    var themeBtn = document.getElementById('btn-theme');
    if (themeBtn) {
      themeBtn.addEventListener('click', function () {
        ui.theme.toggle();
        store.saveSettings({ theme: ui.theme.current() });
      });
    }
    var settingsBtn = document.getElementById('btn-settings');
    if (settingsBtn) settingsBtn.addEventListener('click', openSettings);

    bindSyncNote();
  }

  /**
   * 顶栏的同步异常提示。
   *
   * **只在真的不对劲时出现**：连续失败 2 次以上，或离线且还有待上传。
   * 正常同步保持完全静默 —— 每答一题都闪一下「已同步」只会变成噪音，
   * 而跨设备时用户真正需要知道的只有「出问题了、还没存上」。
   */
  /* ------------------------------------------------------- 设置：来源页返回
   *
   * 设置面板只做在刷题页里，所以从错题本/图谱点「设置」是跳过来的。
   * 关掉面板之后如果不做处理，人就留在刷题页的默认视图（工作台）——
   * 用户看到的是"点设置然后退出，莫名其妙回到了工作台"。
   * 这里在**跨页进入**时才生效（来源页写在 sessionStorage 里），
   * 本页自己打开设置的行为不变。
   */
  function watchSettingsReturn() {
    var from = null;
    try {
      from = window.sessionStorage.getItem('quizforge.settings.from');
    } catch (err) {
      return; // 隐私模式禁 sessionStorage：退化成旧行为，不做返回
    }
    if (!from) return;
    var root = document.getElementById('modal-root');
    if (!root || !window.MutationObserver) return;
    if (root.hidden) {
      // 面板还没打开，等一下再看
      window.setTimeout(watchSettingsReturn, 200);
      return;
    }
    var observer = new MutationObserver(function () {
      if (!root.hidden) return;
      observer.disconnect();
      try {
        window.sessionStorage.removeItem('quizforge.settings.from');
      } catch (err) { /* 忽略 */ }
      window.location.href = from;
    });
    observer.observe(root, { attributes: true, attributeFilter: ['hidden'] });
  }

  function bindSyncNote() {
    var note = document.getElementById('sync-note');
    if (!note || !QF.sync) return;

    note.addEventListener('click', function () {
      note.textContent = '正在重试…';
      QF.sync.flush().then(function () {
        renderSyncNote();
      });
    });

    QF.sync.subscribe(function () {
      renderSyncNote();
    });
    renderSyncNote();
  }

  function renderSyncNote() {
    var note = document.getElementById('sync-note');
    if (!note || !QF.sync) return;

    var snapshot = QF.sync.status();
    var stuckOffline = !snapshot.online && snapshot.pending > 0;
    var failing = snapshot.failures >= 2;

    if (!stuckOffline && !failing) {
      note.hidden = true;
      note.textContent = '';
      return;
    }

    note.hidden = false;
    note.textContent = stuckOffline
      ? '离线 · ' + snapshot.pending + ' 项待同步'
      : '同步失败 · 点击重试';
  }

  /* ============================================================= 启动 */

  function applyStoredAppearance() {
    var conf = store.settings();
    document.documentElement.style.setProperty('--font-scale', String(conf.fontScale || 1));
    document.documentElement.style.setProperty('--content-max', (conf.maxWidth || 880) + 'px');
  }

  function boot() {
    rootEl = document.getElementById('app-root');
    if (!rootEl) return;
    // 幂等：路由启动与编排器都可能触发，重复执行会绑定两次键盘事件
    if (QF.app.booted) return;
    QF.app.booted = true;

    ui.theme.init();
    store.saveSettings({ theme: ui.theme.current() });
    applyStoredAppearance();
    bindShell();
    bindKeys();

    if (!store.available) {
      ui.toast('本机浏览器不允许 file:// 使用 localStorage，进度将只保存在内存中。建议用 python3 -m http.server 打开。', 'warn', 9000);
    }

    var jump = store.takeJump();
    if (jump && jump.ids && jump.ids.length) {
      var valid = jump.ids.filter(function (id) {
        return !!question(id);
      });
      if (valid.length) {
        state.session = buildSession({ scope: 'ids', ids: valid, label: jump.label || '定向练习' });
        // 从错题本「一键重刷」跳进来是直接做题，不能停在选题态
        state.picking = false;
        state.view = 'practice';
      }
    }

    render();

    if (window.location.hash === '#settings') {
      openSettings();
      watchSettingsReturn();
    }
  }

  /**
   * 切回前台时拉一次服务端进度 —— 「手机练完、切到笔记本就能看到」靠它。
   *
   * 正在做题时**只灌数据、不重渲染**：题面、已填的作答、当前在第几题都在
   * state 里，重渲染会打断作答（填空输入会丢光标、选到一半会跳走）。
   * 数据照样更新了，下次渲染自然生效。
   */
  function refreshFromServer() {
    if (!QF.online || !QF.boot || !QF.boot.refreshProgress) return Promise.resolve(false);
    return QF.boot
      .refreshProgress()
      .then(function (info) {
        var working = !state.picking && !!(state.session && state.session.ids && state.session.ids.length);
        var examing = !!state.exam;
        if (!working && !examing) render();
        return info || false;
      })
      .catch(function () {
        return false;
      });
  }

  QF.app = {
    go: go,
    render: render,
    startPractice: startPractice,
    openSettings: openSettings,
    // 在线模式下由 boot.js 调用 boot()：它要先完成鉴权与题库装载
    boot: boot,
    booted: false,
    setView: setViewFromRoute,
    currentView: function () {
      return state.view;
    },
    refreshFromServer: refreshFromServer,
    state: state,
  };

  // 离线单文件没有启动编排器，自己起；
  // 在线模式必须等 boot.js（鉴权 → 题库 → 进度）走完再渲染
  if (!QF.online) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', boot);
    } else {
      boot();
    }
  }
})();
