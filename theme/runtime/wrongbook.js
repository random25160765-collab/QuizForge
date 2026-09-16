/* ===========================================================================
 * wrongbook.js —— 独立错题本页面
 *
 * 与 quiz.html 同目录，在 file:// 下共用同一个 origin，因此可以直接读取
 * 刷题应用写入的 localStorage。Safari 等对 file:// 源做隔离的浏览器可以
 * 通过导入/导出 JSON 迁移数据。
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

  var rootEl = null;

  var view = {
    topics: [],
    sortBy: 'recent',
    includeMastered: false,
    keyword: '',
    selected: null,
    redoResponse: null,
    redoResult: null,
  };

  /* ------------------------------------------------------------- 数据 */

  function currentIds() {
    var ids = store.wrongIds({
      includeMastered: view.includeMastered,
      sortBy: view.sortBy,
    });
    if (view.topics.length) {
      // 选的是学科，命中范围含其全部子孙知识点
      var hit = D.expandTopics(view.topics);
      ids = ids.filter(function (id) {
        var q = D.get(id);
        return q && hit[q.topic];
      });
    }
    if (view.keyword.trim()) {
      var needle = view.keyword.trim().toLowerCase();
      ids = ids.filter(function (id) {
        var q = D.get(id);
        if (!q) return false;
        // 题面里就带主题路径与出处，够搜了；chapter/tags 已经不存在
        return [q.id, q.stem, D.topicPathNames(q.topic).join(' '), q.source]
          .join(' ')
          .toLowerCase()
          .indexOf(needle) !== -1;
      });
    }
    return ids;
  }

  /* ------------------------------------------------------------- 渲染 */

  // 首次渲染是「首屏骨架 → 内容」的交接，走一次过渡；
  // 之后的渲染都是原地更新（筛选、排序），直接画。
  var entered = false;

  function render() {
    if (!rootEl) return;
    if (entered) {
      paint();
      return;
    }
    entered = true;
    ui.viewSwap(paint);
  }

  function paint() {
    ui.clear(rootEl);
    try {
      renderNav();
      rootEl.appendChild(buildHeader());
    } catch (err) {
      rootEl.appendChild(
        h(
          'div.card.panel',
          null,
          h('h2.page__title', { style: { color: 'var(--bad)' } }, h('span', { text: '错题本渲染出错' })),
          h('pre.codeblock__pre', {
            style: {
              marginTop: '12px', padding: '12px', borderRadius: '10px',
              background: 'var(--bg3)', border: '1px solid var(--line)',
              fontFamily: 'var(--font-mono)', fontSize: '12.5px', whiteSpace: 'pre-wrap',
            },
            text: (err && err.message) || String(err),
          }),
          h(
            'div.btnrow',
            { style: { marginTop: '14px' } },
            h('button.btn.btn--primary', {
              type: 'button',
              onClick: function () {
                view.topics = [];
                view.keyword = '';
                view.includeMastered = true;
                render();
              },
            }, h('span', { text: '清除筛选并重试' }))
          )
        )
      );
      return;
    }

    var ids = currentIds();
    // 刚完成重做的题目会立刻从错题本移出。此时要保留在详情里，否则用户看不到
    // 结果对比；只有「切换筛选导致当前题不在结果集」或「从未选中」才重新选。
    var keepJustRedone = !!view.redoResult && !!view.selected;
    if (!view.selected || (!keepJustRedone && ids.indexOf(view.selected) === -1)) {
      view.selected = ids.length ? ids[0] : null;
      view.redoResult = null;
      view.redoResponse = null;
    }
    if (!view.selected) {
      rootEl.appendChild(buildEmpty());
    } else {
      var grid = h('div.wb');
      grid.appendChild(buildList(ids));
      grid.appendChild(buildDetail(view.selected));
      rootEl.appendChild(grid);
    }
    renderStatusbar(ids);
  }

  function renderNav() {
    var nav = document.getElementById('mainnav');
    if (!nav) return;
    ui.clear(nav);
    nav.appendChild(
      h(
        'a.nav-item',
        { href: 'quiz.html' },
        h('span', { html: ui.icon('play', 15) }),
        h('span', { text: '刷题' })
      )
    );
    nav.appendChild(
      h('span.nav-item.is-active', null, h('span', { html: ui.icon('book', 15) }), h('span', { text: '错题本' }))
    );
    // 「导航页」导航项已去掉：左上角 logo 就是回首页的入口
  }

  function buildHeader() {
    var all = store.wrongIds({ includeMastered: true });
    var active = store.wrongIds();
    var mastered = all.length - active.length;

    var page = h('div.page');
    page.appendChild(
      h(
        'div.page__head',
        null,
        h('h1.page__title', { text: '错题本' }),
        h('p.page__sub', {
          text: active.length + ' 道待巩固 · ' + mastered + ' 道已订正',
        })
      )
    );

    var bar = h('div.filterbar');
    bar.appendChild(chip('全部主题', view.topics.length === 0, function () {
      view.topics = [];
      render();
    }));
    // 按学科（一级）给筛选项；计数要含该学科下所有知识点上的题
    D.subjects.forEach(function (topic) {
      var hit = D.expandTopics([topic.key]);
      var count = all.filter(function (id) {
        var q = D.get(id);
        return q && hit[q.topic];
      }).length;
      if (!count) return;
      bar.appendChild(
        chip(topic.name + ' ' + count, view.topics.indexOf(topic.key) !== -1, function () {
          var list = view.topics.slice();
          var at = list.indexOf(topic.key);
          if (at === -1) list.push(topic.key);
          else list.splice(at, 1);
          view.topics = list;
          render();
        }, topic.color)
      );
    });
    bar.appendChild(h('span.filterbar__spacer'));
    bar.appendChild(
      h(
        'button.chip',
        {
          type: 'button',
          class: view.sortBy === 'recent' ? 'is-on' : '',
          onClick: function () {
            view.sortBy = 'recent';
            render();
          },
        },
        h('span', { text: '按最近错误' })
      )
    );
    bar.appendChild(
      h(
        'button.chip',
        {
          type: 'button',
          class: view.sortBy === 'wrong' ? 'is-on' : '',
          onClick: function () {
            view.sortBy = 'wrong';
            render();
          },
        },
        h('span', { text: '按错误次数' })
      )
    );
    bar.appendChild(
      h(
        'button.chip',
        {
          type: 'button',
          class: view.includeMastered ? 'is-on' : '',
          onClick: function () {
            view.includeMastered = !view.includeMastered;
            render();
          },
        },
        h('span', { text: '含已订正' })
      )
    );

    page.appendChild(bar);

    var bar2 = h('div.filterbar');
    bar2.appendChild(
      h(
        'label.searchbox',
        null,
        h('span', { html: ui.icon('search', 15) }),
        h('input', {
          type: 'search',
          placeholder: '搜索错题…',
          value: view.keyword,
          onInput: ui.debounce(function (event) {
            view.keyword = event.target.value;
            render();
          }, 250),
        })
      )
    );
    bar2.appendChild(h('span.filterbar__spacer'));
    bar2.appendChild(
      h(
        'button.btn.btn--primary',
        {
          type: 'button',
          onClick: function () {
            var ids = currentIds();
            if (!ids.length) {
              ui.toast('当前没有可重刷的错题', 'warn');
              return;
            }
            store.setJump({ ids: ids, label: '错题本重刷' });
            window.location.href = 'quiz.html';
          },
        },
        h('span', { html: ui.icon('play', 15) }),
        h('span', { text: '一键重刷当前列表' })
      )
    );
    bar2.appendChild(
      h(
        'button.btn',
        {
          type: 'button',
          onClick: function () {
            exportMarkdown();
          },
        },
        h('span', { html: ui.icon('download', 15) }),
        h('span', { text: '导出 Markdown' })
      )
    );
    bar2.appendChild(
      h(
        'button.btn',
        { type: 'button', onClick: function () { window.print(); } },
        h('span', { html: ui.icon('print', 15) }),
        h('span', { text: '打印' })
      )
    );
    page.appendChild(bar2);
    return page;
  }

  function chip(label, on, onClick, color) {
    return h(
      'button.chip',
      {
        type: 'button',
        class: on ? 'is-on' : '',
        style: color ? { '--tc': color } : null,
        onClick: onClick,
      },
      h('span', { text: label })
    );
  }

  function buildList(ids) {
    var aside = h('div.wb__aside');
    var box = h('div.card.panel', { style: { display: 'flex', flexDirection: 'column', gap: '12px', maxHeight: '100%' } });
    box.appendChild(
      h(
        'div.panel__head',
        { style: { marginBottom: '0' } },
        h('h2.panel__title', { text: '错题列表' }),
        h('span.badge.badge--mono', { text: ids.length + ' 题' })
      )
    );

    var list = h('div.wb__list');
    ids.forEach(function (id) {
      var q = D.get(id);
      if (!q) return;
      var rec = store.record(id) || {};
      list.appendChild(
        h(
          'button.wb__item',
          {
            type: 'button',
            class: view.selected === id ? 'is-active' : '',
            onClick: function () {
              view.selected = id;
              view.redoResponse = null;
              view.redoResult = null;
              render();
            },
          },
          h(
            'span.wb__itemtop',
            null,
            h('span', { text: D.topicName(q.topic) }),
            h('span', { text: '·' }),
            h('span', { text: ui.typeLabel(q.type) }),
            h('span', { style: { flex: '1 1 auto' } }),
            rec.wrong ? h('span.badge.badge--bad', { text: '错 ' + rec.wrong }) : null,
            rec.partial ? h('span.badge.badge--amber', { text: '半对 ' + rec.partial }) : null,
            rec.mastered ? h('span.badge.badge--ok', { text: '已订正' }) : null
          ),
          h('span.wb__itemtitle', { text: ui.truncate(q.stem, 90) }),
          h('span.wb__itemtop', null, h('span', { text: '最近 ' + ui.fmtRelative(rec.lastAt) }), h('span', { style: { flex: '1 1 auto' } }), h('span', { text: q.id }))
        )
      );
    });
    box.appendChild(list);
    aside.appendChild(box);
    return aside;
  }

  function buildDetail(id) {
    var q = D.get(id);
    var detail = h('div.wb__detail');
    if (!q) {
      detail.appendChild(h('div.empty', null, h('div.empty__title', { text: '题目不存在' })));
      return detail;
    }
    var rec = store.record(id) || {};
    var result = view.redoResult;

    // 大题：只读展示每一问的作答与批改结果，重做请到刷题应用里进行
    if (q.type === 'problem') {
      var storedResponses =
        rec.lastResponse && typeof rec.lastResponse === 'object' && !Array.isArray(rec.lastResponse)
          ? rec.lastResponse
          : {};
      detail.appendChild(
        qv.card(q, {
          response: storedResponses,
          locked: true,
          result: {
            status: rec.lastStatus === 'correct' ? 'correct' : rec.lastStatus === 'partial' ? 'partial' : 'wrong',
            correct: rec.lastStatus === 'correct',
            score: rec.lastScore,
            blanks: [],
            expected: '见各小问参考答案',
            expectedHtml: '',
            fromHistory: true,
          },
          problem: { readOnly: true, responses: storedResponses, partResults: rec.parts || {} },
          headerExtra: h('span.badge.badge--amber', { text: '大题 · 共 ' + (q.parts || []).length + ' 问' }),
          footer: h(
            'div.qcard__foot',
            null,
            h('span.badge', { text: '答错 ' + (rec.wrong || 0) + ' 次 · 不完整 ' + (rec.partial || 0) + ' 次' }),
            h('span.badge', { text: '最近 ' + ui.fmtTime(rec.lastAt) }),
            h('span.spacer'),
            h(
              'button.btn.btn--primary.btn--sm',
              {
                type: 'button',
                onClick: function () {
                  store.setJump({ ids: [id], label: '大题重做' });
                  window.location.href = 'quiz.html';
                },
              },
              h('span', { html: ui.icon('play', 14) }),
              h('span', { text: '到刷题应用重做' })
            ),
            rec.mastered
              ? h('button.btn.btn--sm', {
                  type: 'button',
                  onClick: function () {
                    store.setMastered(id, false);
                    render();
                  },
                }, h('span', { text: '移回待巩固' }))
              : h('button.btn.btn--sm.btn--ok', {
                  type: 'button',
                  onClick: function () {
                    store.setMastered(id, true);
                    ui.toast('已标记为已订正', 'ok');
                    render();
                  },
                }, h('span', { html: ui.icon('check', 14) }), h('span', { text: '标记已订正' }))
          ),
        })
      );
      return detail;
    }

    if (currentIds().indexOf(id) === -1) {
      detail.appendChild(
        h(
          'div.card.panel',
          {
            style: {
              marginBottom: '14px',
              borderColor: 'color-mix(in srgb, var(--ok) 42%, transparent)',
              background: 'color-mix(in srgb, var(--ok) 8%, var(--bg2))',
            },
          },
          h(
            'div.statline',
            { style: { padding: '0' } },
            h('span', { html: ui.icon('check', 15), style: { color: 'var(--ok)' } }),
            h('span', { text: '这道题已从当前的错题列表移出（最近一次作答正确）。' }),
            h('span', { style: { flex: '1 1 auto' } }),
            h('button.btn.btn--sm', {
              type: 'button',
              onClick: function () {
                view.redoResult = null;
                view.redoResponse = null;
                render();
              },
            }, h('span', { text: '看下一题' }))
          )
        )
      );
    }

    detail.appendChild(
      qv.card(q, {
        response: view.redoResult ? view.redoResponse : rec.lastResponse,
        result: result || {
          status: rec.lastStatus === 'correct' ? 'correct' : rec.lastStatus === 'partial' ? 'partial' : 'wrong',
          correct: rec.lastStatus === 'correct',
          score: rec.lastScore,
          blanks: [],
          expected: eng.expectedText(q),
          expectedHtml: eng.expectedHtml(q),
          fromHistory: true,
        },
        locked: !result && true,
        onChange: function (value) {
          view.redoResponse = value;
        },
        onSubmit: function () {
          submitRedo(q);
        },
        footer: h(
          'div.qcard__foot',
          null,
          h('span.badge.badge--mono', {
            text: '答错 ' + (rec.wrong || 0) + ' 次 · 不完整 ' + (rec.partial || 0) + ' 次',
          }),
          h('span.badge', { text: '首次 ' + ui.fmtTime(rec.firstAt) }),
          h('span.badge', { text: '最近 ' + ui.fmtTime(rec.lastAt) }),
          h('span.spacer'),
          (function () {
            if (rec.mastered) {
              return h('button.btn.btn--sm', {
                type: 'button',
                onClick: function () {
                  store.setMastered(id, false);
                  ui.toast('已移回待巩固列表', 'info');
                  render();
                },
              }, h('span', { text: '重新标记为待巩固' }));
            }
            return h('button.btn.btn--sm.btn--ok', {
              type: 'button',
              onClick: function () {
                store.setMastered(id, true);
                ui.toast('已标记为已订正', 'ok');
                render();
              },
            }, h('span', { html: ui.icon('check', 14) }), h('span', { text: '标记已订正' }));
          })()
        ),
        headerExtra: h('span.badge.badge--amber', { text: '错题本' }),
        afterAnswer: h(
          'div',
          null,
          h(
            'div.section__title',
            { style: { marginTop: '20px' } },
            h('span', { text: result ? '重做结果' : '原地重做' })
          ),
          result
            ? h(
                'div',
                null,
                buildDiff(q, view.redoResponse),
                h(
                  'div.btnrow',
                  { style: { marginTop: '14px' } },
                  h('button.btn', {
                    type: 'button',
                    onClick: function () {
                      view.redoResult = null;
                      view.redoResponse = null;
                      render();
                    },
                  }, h('span', { html: ui.icon('refresh', 15) }), h('span', { text: '再试一次' })),
                  q.type === 'short'
                    ? h('button.btn', {
                        type: 'button',
                        onClick: function () {
                          store.setSelfGrade(id, 2);
                          ui.toast('已标记为掌握', 'ok');
                          render();
                        },
                      }, h('span', { text: '记为我已掌握' }))
                    : null
                )
              )
            : h(
                'div',
                null,
                h('p.form__note', { text: '在这里重做一遍，答对后会自动从默认错题列表移出。' }),
                h(
                  'div.btnrow',
                  { style: { marginTop: '10px' } },
                  q.type === 'single'
                    ? h('span.badge', { text: '点击选项即可提交' })
                    : h('button.btn.btn--primary', {
                        // 不用 disabled：输入不会触发重渲染，disabled 会停留在旧值
                        type: 'button',
                        onClick: function () {
                          submitRedo(q);
                        },
                      }, h('span', { html: ui.icon('check', 15) }), h('span', { text: '提交重做' })),
                  h('button.btn', {
                    type: 'button',
                    onClick: function () {
                      view.redoResult = {
                        status: 'empty',
                        correct: false,
                        score: null,
                        blanks: [],
                        expected: eng.expectedText(q),
                        expectedHtml: eng.expectedHtml(q),
                      };
                      render();
                    },
                  }, h('span', { text: '直接看答案' }))
                )
              )
        ),
      })
    );

    if (result) {
      detail.appendChild(qv.explainPanel(q, { open: true }));
    } else if (rec.note) {
      detail.appendChild(
        h('div.card.panel', { style: { marginTop: '14px' } }, h('div.explain__rowtitle', { text: '我的笔记' }), md.render(rec.note))
      );
    }

    return detail;
  }

  function buildDiff(q, response) {
    return h(
      'div.diffcompare',
      null,
      h(
        'div.diffcompare__col.diffcompare__col--mine',
        null,
        h('div.diffcompare__title', { text: '我的作答' }),
        h('div.diffcompare__value', { text: qv.formatResponse(q, response) })
      ),
      h(
        'div.diffcompare__col.diffcompare__col--want',
        null,
        h('div.diffcompare__title', { text: '正确答案' }),
        h('div.diffcompare__value', { html: eng.expectedHtml(q) || '见参考答案' })
      )
    );
  }

  function submitRedo(q) {
    var response = view.redoResponse;
    if (q.type === 'single' && typeof response === 'string') {
      // 单选点击时已经写入 response
    }
    if (eng.isResponseEmpty(q, response)) {
      ui.toast('还没有作答', 'warn');
      return;
    }
    var result = eng.grade(q, response);
    view.redoResult = result;
    if (q.type !== 'short') {
      store.applyResult(q, response, result);
    } else {
      store.applyResult(q, response, {
        status: 'ungraded',
        correct: false,
        score: null,
        blanks: [],
        expected: '',
        expectedHtml: '',
      });
    }
    render();
    if (result.correct) ui.toast('重做正确，已从错题本移出', 'ok');
  }

  function buildEmpty() {
    var all = store.wrongIds({ includeMastered: true });
    if (!all.length) {
      return h(
        'div.empty',
        null,
        h('span.empty__icon', { html: ui.icon('check', 26) }),
        h('div.empty__title', { text: '错题本是空的' }),
        h('div.empty__text', {
          text: '还没有做错过的题目。去刷题应用里练习一下，答错的题会自动出现在这里。',
        }),
        h(
          'div.btnrow',
          null,
          h('a.btn.btn--primary', { href: 'quiz.html' }, h('span', { html: ui.icon('play', 15) }), h('span', { text: '去刷题' }))
        )
      );
    }
    return h(
      'div.empty',
      null,
      h('span.empty__icon', { html: ui.icon('filter', 24) }),
      h('div.empty__title', { text: '当前筛选下没有错题' }),
      h('div.empty__text', { text: '共 ' + all.length + ' 道错题，但都不符合当前筛选条件。' }),
      h(
        'div.btnrow',
        null,
        h('button.btn', {
          type: 'button',
          onClick: function () {
            view.topics = [];
            view.keyword = '';
            view.includeMastered = true;
            render();
          },
        }, h('span', { text: '清除筛选并包含已订正' }))
      )
    );
  }

  function renderStatusbar(ids) {
    // 错题本不占用底部状态栏：
    //   - 「待巩固 / 已订正」页头已经写了
    //   - 「本地数据 xx KB」属于内部构建信息，不该出现在界面上
    //   - 「返回刷题」顶栏导航里已经有了
    ui.statusbar.clear();
  }

  /* -------------------------------------------------------- 导出 */

  function exportMarkdown() {
    var ids = currentIds();
    if (!ids.length) {
      ui.toast('当前没有可导出的错题', 'warn');
      return;
    }
    var lines = [];
    lines.push('# quizforge 错题本');
    lines.push('');
    lines.push('导出时间：' + new Date().toLocaleString());
    lines.push('题目数：' + ids.length);
    lines.push('');
    lines.push('---');
    lines.push('');

    ids.forEach(function (id, index) {
      var q = D.get(id);
      var rec = store.record(id) || {};
      lines.push('## ' + (index + 1) + '. ' + q.id + '（' + D.topicName(q.topic) + ' / ' + ui.typeLabel(q.type) + '）');
      lines.push('');
      lines.push('> 答错 ' + (rec.wrong || 0) + ' 次 · 不完整 ' + (rec.partial || 0) + ' 次 · 最近 ' + ui.fmtTime(rec.lastAt));
      lines.push('');
      lines.push(q.stem);
      lines.push('');
      if (q.options && q.options.length) {
        lines.push('**选项**');
        lines.push('');
        q.options.forEach(function (option) {
          lines.push('- ' + option.key + '. ' + option.text);
        });
        lines.push('');
      }
      lines.push('**正确答案**：' + (eng.expectedText(q) || '见参考答案'));
      lines.push('');
      lines.push('**我的作答**：' + qv.formatResponse(q, rec.lastResponse));
      lines.push('');
      if (q.reference) {
        lines.push('**参考答案**');
        lines.push('');
        lines.push(q.reference);
        lines.push('');
      }
      if (q.rubric && q.rubric.length) {
        lines.push('**评分要点**');
        lines.push('');
        q.rubric.forEach(function (item) {
          lines.push('- ' + item);
        });
        lines.push('');
      }
      if (q.explanation) {
        lines.push('**解析**');
        lines.push('');
        lines.push(q.explanation);
        lines.push('');
      }
      lines.push('---');
      lines.push('');
    });

    var blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'quizforge-wrongbook-' + ui.dayKey() + '.md';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () {
      URL.revokeObjectURL(url);
    }, 1000);
    ui.toast('已导出 ' + ids.length + " 道错题为 Markdown", 'ok');
  }

  /* -------------------------------------------------------- 启动 */

  function boot() {
    rootEl = document.getElementById('app-root');
    if (!rootEl) return;
    // 幂等：在线模式下 boot.js 与 DOMContentLoaded 都可能触发
    if (QF.wrongbook && QF.wrongbook.booted) return;

    ui.theme.init();
    var conf = store.settings();
    document.documentElement.style.setProperty('--font-scale', String(conf.fontScale || 1));
    document.documentElement.style.setProperty('--content-max', (conf.maxWidth || 880) + 'px');

    var themeBtn = document.getElementById('btn-theme');
    if (themeBtn) {
      themeBtn.addEventListener('click', function () {
        ui.theme.toggle();
        store.saveSettings({ theme: ui.theme.current() });
      });
    }
    var settingsBtn = document.getElementById('btn-settings');
    if (settingsBtn) {
      settingsBtn.addEventListener('click', function () {
        window.location.href = 'quiz.html#settings';
      });
    }
    var wbBtn = document.getElementById('btn-wrongbook');
    if (wbBtn) wbBtn.setAttribute('href', 'wrongbook.html');

    if (!store.available) {
      ui.toast('浏览器不允许 file:// 使用 localStorage，无法读取刷题进度。建议用 python3 -m http.server 打开。', 'warn', 9000);
    }

    render();
    QF.wrongbook.booted = true;
  }

  QF.wrongbook = { render: render, exportMarkdown: exportMarkdown, boot: boot, booted: false };

  // 离线单文件自己起；在线模式等 boot.js 走完鉴权与题库装载
  if (!QF.online) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', boot);
    } else {
      boot();
    }
  }
})();

/* 根因诊断 —— 错题本顶部补一块"根因"。
 *
 * 过去这里只列"你错过了哪些题"；现在把题号交给服务端（题 → 考点 → 前置考点），
 * 换回一条人话：先补哪个考点、一共几步。
 * 离线产物没有服务端，那就什么都不显示 —— 不能因为拿不到诊断把错题本弄坏。
 */
(function () {
  'use strict';
  var RX = /^[a-z][a-z0-9-]*-\d{4}$/;
  var lastKey = '';

  function wrongIds() {
    var out = [];
    var nodes = document.querySelectorAll('span.wb__itemtop');
    for (var i = 0; i < nodes.length; i++) {
      var tail = nodes[i].lastElementChild;
      var text = tail ? (tail.textContent || '').trim() : '';
      if (RX.test(text)) out.push(text);
    }
    return out;
  }

  function anchor() {
    var nodes = document.querySelectorAll('span.wb__itemtop');
    var first = nodes[0];
    if (!first) return null;
    var row = first.parentNode;              // 一条错题
    return row && row.parentNode ? row.parentNode : null;   // 那个列表
  }

  function show(data) {
    var list = anchor();
    if (!list || list.querySelector('.wb__rootcause')) return;
    var prereq = (data && data.prerequisites) || [];
    var wrong = (data && data.wrongConcepts) || [];
    var steps = (data && data.order) || [];
    if (!prereq.length && !wrong.length) return;

    var box = document.createElement('div');
    box.className = 'wb__rootcause';
    box.style.cssText = 'margin:10px 0;padding:10px 12px;border:1px solid var(--line);' +
      'border-radius:10px;background:var(--bg2);color:var(--fg2);font-size:12.5px;line-height:1.7';

    var head = document.createElement('div');
    head.style.cssText = 'color:var(--pri);font-weight:600;margin-bottom:4px';
    head.textContent = '根因诊断';
    box.appendChild(head);

    var text = document.createElement('div');
    if (prereq.length) {
      var names = [];
      for (var i = 0; i < Math.min(3, prereq.length); i++) names.push('「' + prereq[i].name + '」');
      text.textContent = '这些错题的根子在前置考点上：先补 ' + names.join('、') +
        (prereq.length > 3 ? ' 等 ' + prereq.length + ' 个' : '') +
        '，再回来做错题。整个复习顺序共 ' + steps.length + ' 步。';
    } else {
      text.textContent = '这些错题都落在同一个考点上（' + wrong[0].name + '），直接把这个考点再练几道即可。';
    }
    box.appendChild(text);
    list.insertBefore(box, list.firstChild);
  }

  function poll() {
    var ids = wrongIds();
    var key = ids.join(',');
    if (!ids.length || key === lastKey) return;
    lastKey = key;
    try {
      fetch('/api/graph/diagnose', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ questionIds: ids })
      }).then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { if (d) show(d); })
        .catch(function () {});
    } catch (e) { /* 离线：静默 */ }
  }

  setInterval(poll, 1500);
})();
