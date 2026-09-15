/* ===========================================================================
 * qview.js —— 题目的公共渲染组件
 *
 * app.js（刷题应用）与 wrongbook.js（错题本）共用同一套题目渲染逻辑，
 * 保证两个页面里题面、选项、判定条、解析抽屉的表现完全一致。
 * ========================================================================= */
(function () {
  'use strict';

  var QF = (window.QF = window.QF || {});
  var ui = QF.ui;
  var h = ui.h;
  var md = QF.md;

  /* --------------------------------------------------------- 元信息徽章 */

  function metaRow(question, options) {
    var opts = options || {};
    var names = QF.data.topicPathNames(question.topic);
    var color = QF.data.topicColor(question.topic);
    var row = h('div.qcard__head');

    row.appendChild(
      h('span.badge.badge--type', { text: ui.typeLabel(question.type) })
    );
    // 分类按主题树展示：学科用主题色，其下的单元 / 知识点接在后面。
    // 不再显示 chapter 与 tags —— 层级已经承担了分类，标签是冗余的第二套体系。
    row.appendChild(
      h('span.badge.badge--topic', { text: names[0], style: { '--tc': color } })
    );
    if (names.length > 1) {
      row.appendChild(h('span.badge.badge--path', { text: names.slice(1).join(' · ') }));
    }
    row.appendChild(ui.difficultyDots(question.difficulty));

    row.appendChild(h('span.spacer'));
    if (opts.extra) row.appendChild(opts.extra);
    row.appendChild(h('span.qcard__id', { text: question.id }));
    return row;
  }

  /* ------------------------------------------------------------- 题面 */

  function stem(question) {
    var box = h('div.qcard__stem');
    box.appendChild(md.render(question.stem || ''));
    if (question.hint) {
      box.appendChild(
        h(
          'div.explain',
          null,
          h(
            'div.explain__head',
            {
              onClick: function (e) {
                e.currentTarget.parentNode.classList.toggle('is-open');
              },
            },
            h('span', { html: ui.icon('bulb', 14) }),
            h('span', { text: '提示' }),
            h('span.spacer'),
            h('span.explain__chev', { html: ui.icon('chevronR', 14) })
          ),
          h('div.explain__body', null, md.render(question.hint))
        )
      );
    }
    return box;
  }

  /* ------------------------------------------------------------- 作答区 */

  function answerArea(question, options) {
    var opts = options || {};
    var response = opts.response;
    var result = opts.result || null;
    var locked = !!opts.locked;

    switch (question.type) {
      case 'single':
        return singleArea(question, response, result, locked, opts);
      case 'multi':
        return multiArea(question, response, result, locked, opts);
      case 'blank':
        return blankArea(question, response, result, locked, opts);
      case 'short':
        return shortArea(question, response, result, locked, opts);
      case 'problem':
        return problemArea(question, opts.problem || {});
      default:
        return h('div');
    }
  }

  function optionRow(question, option, options) {
    var opts = options || {};
    var key = option.key;
    var picked = opts.picked;
    var result = opts.result;
    var locked = opts.locked;

    var cls = 'option';
    var mark = null;

    if (result) {
      var isCorrect = opts.isCorrectKey(key);
      if (isCorrect) cls += ' is-correct';
      if (picked && !isCorrect) cls += ' is-wrong';
      if (picked && isCorrect) cls += ' is-picked';
      if (isCorrect && !picked) cls += ' is-missed';
      if (isCorrect) mark = h('span.option__mark', { html: ui.icon('check', 15) });
      else if (picked) mark = h('span.option__mark', { html: ui.icon('close', 15) });
    } else if (picked) {
      cls += ' is-picked';
    }
    if (locked) cls += ' is-locked';

    var node = h(
      'button',
      {
        type: 'button',
        class: cls,
        disabled: locked || false,
        'aria-pressed': picked ? 'true' : 'false',
        onClick: locked || !opts.onPick
          ? null
          : function () {
              opts.onPick(key);
            },
      },
      h('span.option__key', { text: key }),
      h('span.option__text', { html: md.renderInline(option.text) }),
      mark
    );
    return node;
  }

  /**
   * 只读选项：题库浏览（选题态）用。复用作答时的选项外观，
   * 但 locked 让它们不可点、不浮起，也不暴露正确答案。
   */
  function optionsReadOnly(question) {
    var box = h('div.options', { role: 'group' });
    (question.options || []).forEach(function (option) {
      box.appendChild(
        optionRow(question, option, {
          picked: false,
          result: null,
          locked: true,
          isCorrectKey: function () {
            return false;
          },
          onPick: null,
        })
      );
    });
    return box;
  }

  function singleArea(question, response, result, locked, opts) {
    var box = h('div.options', { role: 'radiogroup' });
    var correctKeys = result ? [String(question.answer)] : [];
    (question.options || []).forEach(function (option) {
      box.appendChild(
        optionRow(question, option, {
          picked: response === option.key,
          result: result,
          locked: locked,
          isCorrectKey: function (key) {
            return correctKeys.indexOf(key) !== -1;
          },
          onPick: opts.onChange
            ? function (key) {
                opts.onChange(key);
              }
            : null,
        })
      );
    });
    return box;
  }

  function multiArea(question, response, result, locked, opts) {
    var picked = Array.isArray(response) ? response.slice() : [];
    var box = h('div.options', { role: 'group' });
    var correctKeys = result ? (question.answer || []).map(String) : [];
    (question.options || []).forEach(function (option) {
      box.appendChild(
        optionRow(question, option, {
          picked: picked.indexOf(option.key) !== -1,
          result: result,
          locked: locked,
          isCorrectKey: function (key) {
            return correctKeys.indexOf(key) !== -1;
          },
          onPick: opts.onChange
            ? function (key) {
                var next = picked.slice();
                var at = next.indexOf(key);
                if (at === -1) next.push(key);
                else next.splice(at, 1);
                next.sort();
                opts.onChange(next);
              }
            : null,
        })
      );
    });
    if (!result) {
      box.appendChild(
        h('div.qcard__foot', null, h('span.badge.badge--amber', { text: '多选题：需要选中全部正确项' }))
      );
    }
    return box;
  }

  function blankArea(question, response, result, locked, opts) {
    var count = (question.answer || []).length || 1;
    var values = Array.isArray(response) ? response.slice() : [];
    while (values.length < count) values.push('');
    var isCode = question.blankMode === 'code';
    var rows = question.blankLines > 0 ? question.blankLines : 6;
    var box = h('div.blanks', { class: isCode ? 'blanks--code' : '' });

    // 作答过程中不会整体重渲染（否则输入焦点会丢），因此这里必须从 DOM
    // 实时汇总各空的当前值，而不能用渲染时的快照数组——否则多空场景下
    // 后一次输入会用旧快照覆盖掉前面已经填好的空。
    function collect() {
      var fields = box.querySelectorAll('.blank__field');
      var next = [];
      for (var k = 0; k < fields.length; k++) next.push(fields[k].value);
      return next;
    }

    for (var i = 0; i < count; i++) {
      (function (index) {
        var blankResult = result && result.blanks ? result.blanks[index] : null;
        var row = h('div.blank', {
          class: (isCode ? 'blank--code ' : '') + (blankResult ? (blankResult.ok ? 'is-ok' : 'is-bad') : ''),
        });

        row.appendChild(
          h('label.blank__label', {
            for: 'blank-' + index,
            text: count > 1 ? '第 ' + (index + 1) + ' 空' : isCode ? '代码' : '答案',
          })
        );

        var common = {
          id: 'blank-' + index,
          autocomplete: 'off',
          spellcheck: 'false',
          value: values[index] || '',
          disabled: locked || false,
          onInput: function () {
            if (opts.onChange) opts.onChange(collect());
          },
        };

        var field;
        if (isCode) {
          field = h('textarea.blank__field.blank__code', {
            id: common.id,
            rows: rows,
            autocomplete: 'off',
            spellcheck: 'false',
            value: common.value,
            disabled: common.disabled,
            placeholder: '在此写出代码，可用 ⌘/Ctrl + Enter 提交…',
            onInput: common.onInput,
            onKeydown: function (event) {
              // 代码填空里回车用于换行；用 ⌘/Ctrl+Enter 提交
              if ((event.metaKey || event.ctrlKey) && event.key === 'Enter' && opts.onSubmit && !locked) {
                event.preventDefault();
                opts.onSubmit();
              }
            },
          });
        } else {
          field = h('input.blank__field.blank__input', {
            id: common.id,
            type: 'text',
            autocomplete: 'off',
            spellcheck: 'false',
            value: common.value,
            disabled: common.disabled,
            placeholder: '在此输入…',
            onInput: common.onInput,
            onKeydown: function (event) {
              if (event.key === 'Enter' && opts.onSubmit && !locked) {
                event.preventDefault();
                opts.onSubmit();
              }
            },
          });
        }
        row.appendChild(field);

        if (blankResult && !blankResult.ok) {
          var wants = (blankResult.want || []).slice(0, 2);
          if (!wants.length && blankResult.regex && blankResult.regex.length) {
            wants = ['/' + blankResult.regex[0] + '/'];
          }
          row.appendChild(
            h('span.blank__want', { class: 'blank__want--below', text: '应为 ' + (wants.join(' / ') || '（见解析）') })
          );
        }

        box.appendChild(row);
        if (index === 0 && opts.autofocus && !locked) {
          setTimeout(function () {
            field.focus();
          }, 40);
        }
      })(i);
    }
    return box;
  }

  function shortArea(question, response, result, locked, opts) {
    var box = h('div.shortinput');
    var textarea = h('textarea', {
      placeholder: '用自己的话写清楚关键点即可，AI 批改会依据评分要点给分…',
      value: response || '',
      disabled: locked || false,
      spellcheck: 'false',
      onInput: function (event) {
        if (opts.onChange) opts.onChange(event.target.value);
        var counter = box.querySelector('.shortinput__count');
        if (counter) counter.textContent = event.target.value.length + ' 字';
      },
      onKeydown: function (event) {
        if ((event.metaKey || event.ctrlKey) && event.key === 'Enter' && opts.onSubmit && !locked) {
          event.preventDefault();
          opts.onSubmit();
        }
      },
    });
    box.appendChild(textarea);
    box.appendChild(
      h(
        'div.shortinput__foot',
        null,
        h('span.shortinput__count', { text: String(response || '').length + ' 字' }),
        h('span.spacer'),
        locked ? null : h('span.sb__hint', null, h('kbd', { text: '⌘/Ctrl + Enter' }), h('span', { text: '提交' })),
        opts.actions || null
      )
    );
    if (opts.autofocus && !locked) {
      setTimeout(function () {
        textarea.focus();
      }, 40);
    }
    return box;
  }

  /* --------------------------------------------------------- 大题（多小问） */

  /**
   * 法式大题：公共题面 + 若干小问，每问独立作答与批改。
   * @param {object} question
   * @param {object} opts
   *   responses   {partIndex: 文本}
   *   partResults {partIndex: {score,max,verdict,matched,missing,feedback,...}}
   *   onChange(partIndex, value)
   *   onGrade(partIndex)      单问批改
   *   onGradeAll()            批改全部已答小问
   *   onSelfRate(partIndex, grade)
   *   aiEnabled  boolean
   *   busy       Set/object，标记正在批改的小问
   *   openParts  {partIndex: bool}
   *   onTogglePart(partIndex)
   */
  function problemArea(question, opts) {
    var parts = question.parts || [];
    var responses = opts.responses || {};
    var results = opts.partResults || {};
    var busy = opts.busy || {};
    var aiEnabled = !!opts.aiEnabled;

    var box = h('div.problem');

    // 小问导航：一眼看到哪些已作答、哪些已批改
    var done = 0;
    var graded = 0;
    var toc = h('div.problem__toc');
    var chips = {}; // partIndex -> {chip, state}
    parts.forEach(function (part) {
      var filled = String(responses[part.index] == null ? '' : responses[part.index]).trim().length > 0;
      var ok = !!results[part.index];
      if (filled) done += 1;
      if (ok) graded += 1;
      var label = h('span', { text: filled ? '已答' : '未答' });
      var chip = h(
        'button.problem__tocitem',
        {
          type: 'button',
          class: ok ? 'is-graded' : filled ? 'is-done' : '',
          title: part.title,
          onClick: function () {
            if (opts.onTogglePart) opts.onTogglePart(part.index, true);
            var node = box.querySelector('#part-' + part.index);
            if (node && node.scrollIntoView) node.scrollIntoView({ block: 'start', behavior: 'smooth' });
          },
        },
        h('span', { text: '第 ' + part.index + ' 问' }),
        label,
        ok ? h('span', { text: '· ' + results[part.index].score + '/' + results[part.index].max }) : null
      );
      chips[part.index] = { chip: chip, label: label, graded: ok };
      toc.appendChild(chip);
    });
    toc.appendChild(
      h('span', { style: { flex: '1 1 auto' } }),
      h('span.badge.badge--mono', { text: '已答 ' + done + ' / ' + parts.length + ' · 已批改 ' + graded })
    );

    var footer = h('div.part__actions');
    footer.appendChild(
      h('span.form__note', { text: '每个小问独立作答，答完可以单独批改，也可以一次批改全部已答小问。' })
    );
    footer.appendChild(h('span.spacer'));
    if (opts.onGradeAll) {
      footer.appendChild(
        h(
          'button.btn.btn--sm',
          {
            type: 'button',
            disabled: !done || !aiEnabled,
            title: aiEnabled ? '' : '需要先在设置里启用 AI 批改',
            onClick: function () {
              opts.onGradeAll();
            },
          },
          h('span', { html: ui.icon('robot', 14) }),
          h('span', { text: '批改全部已答小问' })
        )
      );
    }
    box.appendChild(toc);
    box.appendChild(footer);

    var list = h('div.parts');
    parts.forEach(function (part) {
      var value = responses[part.index] == null ? '' : responses[part.index];
      var result = results[part.index] || null;
      var isOpen = opts.openParts ? opts.openParts[part.index] !== false : true;
      var isBusy = !!busy[part.index];

      var node = h('div.part', {
        id: 'part-' + part.index,
        class: (isOpen ? 'is-open ' : '') + (result ? 'is-graded' : ''),
      });

      node.appendChild(
        h(
          'div.part__head',
          {
            onClick: function (event) {
              event.currentTarget.parentNode.classList.toggle('is-open');
            },
          },
          h('span.part__no', { text: '第 ' + part.index + ' 问' }),
          h('span.part__title', { text: part.title }),
          result ? h('span.part__score', { text: result.score + ' / ' + result.max }) : null,
          h('span.part__chev', { html: ui.icon('chevronR', 14) })
        )
      );

      var body = h('div.part__body');
      /**
       * 解答与评分要点面板：交互模式与只读模式共用。
       * 内容是**首次展开时才构建**的 —— 六问大题的参考答析全量解析要 30ms 量级，
       * 而默认是折叠状态，没必要在切题时先付这笔钱。
       */
      function buildRef() {
        var opened = !!result;
        var built = false;
        var refBody = h('div.explain__body');
        var ref = h(
          'div.explain.part__ref',
          {
            class: opened ? 'is-open' : '',
            onClick: function (event) {
              // 只处理点在标题行上的情况
              if (!event.target.closest || !event.target.closest('.explain__head')) return;
              var nowOpen = ref.classList.toggle('is-open');
              if (nowOpen && !built) {
                built = true;
                refBody.appendChild(buildRefBody());
              }
            },
          },
          h(
            'div.explain__head',
            null,
            h('span', { html: ui.icon('book', 14) }),
            h('span', { text: '解答与评分要点' }),
            h('span.spacer'),
            h('span.explain__chev', { html: ui.icon('chevronR', 14) })
          )
        );

        function buildRefBody() {
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
          return wrap;
        }

        if (opened) {
          built = true;
          refBody.appendChild(buildRefBody());
        }
        ref.appendChild(refBody);
        return ref;
      }
      body.appendChild(h('div.part__stem', null, md.render(part.stem || '')));

      if (part.hint) {
        body.appendChild(
          h(
            'div.explain',
            null,
            h(
              'div.explain__head',
              { onClick: function (event) { event.currentTarget.parentNode.classList.toggle('is-open'); } },
              h('span', { html: ui.icon('bulb', 14) }),
              h('span', { text: '提示' }),
              h('span.spacer'),
              h('span.explain__chev', { html: ui.icon('chevronR', 14) })
            ),
            h('div.explain__body', null, md.render(part.hint))
          )
        );
      }

      if (opts.readOnly) {
        // 只读模式（错题本 / 复盘）：直接展示当时写的内容，不提供输入框
        body.appendChild(
          h(
            'div.part__answer',
            null,
            h(
              'div.diffcompare__col',
              { class: 'diffcompare__col--mine' },
              h('div.diffcompare__title', { text: '我的作答' }),
              h('div.diffcompare__value', {
                style: { whiteSpace: 'pre-wrap' },
                text: String(value).trim() || '（未作答）',
              })
            )
          )
        );
        body.appendChild(buildRef());
        if (result) body.appendChild(aiResultPanel(result, question, { compact: true }));
        node.classList.add('is-open');
        node.appendChild(body);
        list.appendChild(node);
        return;
      }

      var ta = h('textarea', {
        id: 'part-input-' + part.index,
        placeholder: '写下这一问的推导 / 结论 / 伪代码…',
        spellcheck: 'false',
        disabled: !!opts.locked || isBusy,
        value: value,
        onInput: function (event) {
          if (opts.onChange) opts.onChange(part.index, event.target.value);
          // 就地更新导航与字数，避免整体重渲染导致输入框失焦
          var filled = event.target.value.trim().length > 0;
          var meta = chips[part.index];
          if (meta) {
            meta.label.textContent = filled ? '已答' : '未答';
            if (!meta.graded) meta.chip.classList.toggle('is-done', filled);
          }
          var counter = node.querySelector('.part__actions .badge--mono');
          if (counter) counter.textContent = event.target.value.length + ' 字';
        },
        onKeydown: function (event) {
          if ((event.metaKey || event.ctrlKey) && event.key === 'Enter' && opts.onGrade && !opts.locked) {
            event.preventDefault();
            opts.onGrade(part.index);
          }
        },
      });
      body.appendChild(h('div.part__answer', null, ta));

      var actions = h('div.part__actions');
      if (isBusy) {
        actions.appendChild(h('span.ai-loading', null, h('span.spinner'), h('span', { text: 'AI 正在批改这一问…' })));
      } else {
        if (opts.onGrade) {
          actions.appendChild(
            h(
              'button.btn.btn--sm' + (aiEnabled ? '.btn--primary' : ''),
              {
                type: 'button',
                disabled: opts.locked || !aiEnabled,
                title: aiEnabled ? '' : '需要先在设置里启用 AI 批改',
                onClick: function () {
                  opts.onGrade(part.index);
                },
              },
              h('span', { html: ui.icon('robot', 14) }),
              h('span', { text: 'AI 批改这一问' })
            )
          );
        }
        actions.appendChild(
          h(
            'button.btn.btn--sm.btn--ghost',
            {
              type: 'button',
              onClick: function () {
                var panel = node.querySelector('.part__ref');
                if (panel) panel.classList.toggle('is-open');
              },
            },
            h('span', { html: ui.icon('book', 14) }),
            h('span', { text: '参考答析与评分要点' })
          )
        );
        actions.appendChild(h('span.spacer'));
        actions.appendChild(
          h('span.badge.badge--mono', {
            text: String(value).length + ' 字',
          })
        );
      }
      body.appendChild(actions);

      // 参考答案 / 评分要点（默认折叠，避免作答前看到答案）
      body.appendChild(buildRef());

      if (result) {
        body.appendChild(aiResultPanel(result, question, { compact: true }));
        if (opts.onSelfRate) {
          body.appendChild(
            selfRate(function (grade) {
              opts.onSelfRate(part.index, grade);
            }, { title: '自评这一问' })
          );
        }
      }

      node.appendChild(body);
      list.appendChild(node);
    });

    box.appendChild(list);
    return box;
  }

  /* ------------------------------------------------------------- 判定条 */

  function verdictBar(result, question, options) {
    var opts = options || {};
    var meta = QF.engine.describeStatus(result.status);
    var tone = { ok: 'ok', warn: 'warn', bad: 'bad', muted: 'muted', amber: 'warn', info: 'muted' }[meta.tone] || 'muted';

    var body = h('div.verdict__body');
    body.appendChild(h('div.verdict__title', { text: (result.fromAI ? 'AI：' : '') + meta.label }));

    if (result.status === 'partial') {
      var got = (result.blanks || []).filter(function (b) {
        return b.ok;
      }).length;
      if (result.blanks && result.blanks.length) {
        body.appendChild(h('div.verdict__text', { text: '答对 ' + got + ' / ' + result.blanks.length + ' 个空。' }));
      } else {
        body.appendChild(h('div.verdict__text', { text: '多选题漏选，部分得分。' }));
      }
    } else if (result.status === 'empty') {
      body.appendChild(h('div.verdict__text', { text: '这道题还没有作答。' }));
    } else if (result.status === 'wrong') {
      body.appendChild(h('div.verdict__text', { text: '再看看解析，注意关键细节。' }));
    }

    if (QF.engine.expectedText(question) && question.type !== 'short') {
      body.appendChild(
        h(
          'div.verdict__expected',
          { html: '正确答案：<b>' + QF.engine.expectedHtml(question) + '</b>' }
        )
      );
    }

    var node = h(
      'div.verdict',
      { class: 'verdict--' + tone },
      h('span.verdict__icon', { html: ui.icon(meta.icon, 15) }),
      body
    );

    if (typeof result.score === 'number' && question.type !== 'short') {
      node.appendChild(
        h('span.verdict__score', { text: Math.round(result.score * 100) + '%' })
      );
    }
    if (opts.extra) node.appendChild(opts.extra);
    return node;
  }

  /* ------------------------------------------------------------- 解析 */

  function explainPanel(question, options) {
    var opts = options || {};
    var open = opts.open !== false;
    // 头部默认是可点的开合开关。浏览态的题卡自己有一个「显示答案与解析」按钮，
    // 那里传 toggle: false —— 一块内容不能挂两个开关：两边各记一份开合状态，
    // 收起一个之后另一个还以为自己是展开的，点回去只剩标题栏，内容再也不出来。
    var togglable = opts.toggle !== false;

    var body = h('div.explain__body');

    if (question.type === 'short') {
      if (question.reference) {
        body.appendChild(h('div.explain__row', null, h('div.explain__rowtitle', { text: '参考答案' }), h('div.explain__ref', null, md.render(question.reference))));
      }
      if (question.rubric && question.rubric.length) {
        body.appendChild(
          h(
            'div.explain__row',
            null,
            h('div.explain__rowtitle', { text: '评分要点' }),
            h(
              'ul.rubriclist',
              null,
              question.rubric.map(function (item) {
                return h('li', { html: md.renderInline(item) });
              })
            )
          )
        );
      }
    } else if (QF.engine.expectedText(question)) {
      body.appendChild(
        h(
          'div.explain__row',
          null,
          h('div.explain__rowtitle', { text: '正确答案' }),
          h('div.explain__ref', { html: md.renderInline(QF.engine.expectedText(question)) })
        )
      );
    }

    if (question.explanation) {
      body.appendChild(
        h('div.explain__row', null, h('div.explain__rowtitle', { text: '解析' }), md.render(question.explanation))
      );
    }

    // 只展示对学习者有意义的信息；题目的内部 id 与源文件路径不在这里暴露
    if (question.source) {
      body.appendChild(
        h(
          'div.explain__row',
          null,
          h('div.explain__rowtitle', { text: '出处' }),
          h('div.form__note', { text: question.source })
        )
      );
    }

    var head;
    if (togglable) {
      // 文案跟着状态走。之前是构建时算一次的固定值：展开态点一下收起，
      // 头一行仍写着「收起解析」，方向正好反了。
      var headLabel = h('span', { text: open ? '收起解析' : '展开解析' });
      head = h(
        'div.explain__head',
        {
          onClick: function (event) {
            var isOpen = event.currentTarget.parentNode.classList.toggle('is-open');
            headLabel.textContent = isOpen ? '收起解析' : '展开解析';
          },
        },
        h('span', { html: ui.icon('book', 14) }),
        headLabel,
        h('span.spacer'),
        h('span.explain__chev', { html: ui.icon('chevronR', 14) })
      );
    } else {
      // 静态标题：面板里装的是「正确答案 / 参考答案 + 解析」，标题就照实写
      head = h(
        'div.explain__head.explain__head--static',
        null,
        h('span', { html: ui.icon('book', 14) }),
        h('span', { text: '答案与解析' })
      );
    }

    var panel = h('div.explain', { class: open ? 'is-open' : '' }, head, body);
    return panel;
  }

  /* ------------------------------------------------------- AI 批改结果 */

  function aiResultPanel(aiResult, question, options) {
    var opts = options || {};
    var node = h('div.airesult', { class: opts.compact ? 'airesult--compact' : '' });
    var head = h(
      'div.airesult__head',
      null,
      h('span.airesult__badge', { html: ui.icon('robot', 15) + '<span>AI 批改</span>' }),
      h('span.spacer'),
      h('span.airesult__score', { html: aiResult.score + '<small> / ' + aiResult.max + '</small>' })
    );
    node.appendChild(head);

    var verdictText = { correct: '作答正确', partial: '部分正确', wrong: '需要订正' }[aiResult.verdict] || '已批改';
    node.appendChild(h('div.verdict__text', { text: verdictText }));

    var cols = [];
    if (aiResult.matched && aiResult.matched.length) {
      cols.push(
        h(
          'div.airesult__col.airesult__col--ok',
          null,
          h('div.airesult__coltitle', { text: '已命中要点' }),
          h('ul.airesult__list', null, aiResult.matched.map(function (item) {
            return h('li', { text: item });
          }))
        )
      );
    }
    if (aiResult.missing && aiResult.missing.length) {
      cols.push(
        h(
          'div.airesult__col.airesult__col--bad',
          null,
          h('div.airesult__coltitle', { text: '遗漏 / 有误' }),
          h('ul.airesult__list', null, aiResult.missing.map(function (item) {
            return h('li', { text: item });
          }))
        )
      );
    }
    if (aiResult.errors && aiResult.errors.length) {
      cols.push(
        h(
          'div.airesult__col.airesult__col--bad',
          null,
          h('div.airesult__coltitle', { text: '事实性错误' }),
          h('ul.airesult__list', null, aiResult.errors.map(function (item) {
            return h('li', { text: item });
          }))
        )
      );
    }
    if (cols.length) node.appendChild(h('div.airesult__cols', null, cols));

    if (aiResult.feedback) {
      node.appendChild(h('div.airesult__feedback', null, md.render(aiResult.feedback)));
    }
    if (aiResult.improved) {
      node.appendChild(
        h('div.explain__row', null, h('div.explain__rowtitle', { text: '更规范的参考答案' }), md.render(aiResult.improved))
      );
    }
    if (aiResult.model) {
      node.appendChild(
        h('div.airesult__meta', {
          text: aiResult.model + (aiResult.usage ? ' · ' + (aiResult.usage.total_tokens || 0) + ' tokens' : ''),
        })
      );
    }
    return node;
  }

  /* ---------------------------------------------------------- 自评打分 */

  function selfRate(onPick, options) {
    var opts = options || {};
    var items = [
      { grade: 0, label: '没答对', tone: 'again', hint: '要点基本没覆盖' },
      { grade: 1, label: '部分正确', tone: 'hard', hint: '命中部分要点' },
      { grade: 2, label: '答对了', tone: 'good', hint: '要点齐全' },
    ];
    var bar = h(
      'div.gradebar',
      { style: { gridTemplateColumns: 'repeat(3, 1fr)', marginTop: '0' } },
      items.map(function (item) {
        return h(
          'button.gradebtn',
          {
            type: 'button',
            class: 'gradebtn--' + item.tone,
            onClick: function () {
              onPick(item.grade);
            },
          },
          h('span.gradebtn__label', { text: item.label }),
          h('span.gradebtn__due', { text: item.hint })
        );
      })
    );
    return h(
      'div.explain__row',
      null,
      h('div.explain__rowtitle', { text: opts.title || '自评' }),
      bar
    );
  }

  /* ------------------------------------------------------- 作答展示 */

  function formatResponse(question, response) {
    if (response == null || response === '') return '（未作答）';
    switch (question.type) {
      case 'single':
        return String(response);
      case 'multi':
        return (Array.isArray(response) ? response : []).join('、') || '（未作答）';
      case 'problem': {
        var lines = (question.parts || []).map(function (part) {
          var text = String((response && response[part.index]) || '').trim();
          return '【第 ' + part.index + ' 问】' + (text || '（未作答）');
        });
        return lines.join('\n\n') || '（未作答）';
      }
      case 'blank': {
        var values = Array.isArray(response) ? response : [response];
        return values
          .map(function (value, index) {
            var shown = String(value == null ? '' : value).trim();
            return (values.length > 1 ? '第 ' + (index + 1) + ' 空：' : '') + (shown || '（空）');
          })
          .join('\n');
      }
      default:
        return String(response);
    }
  }

  /* ------------------------------------------------------------ 整卡 */

  /**
   * 渲染一道题的完整卡片。
   * @param {object} question
   * @param {object} options
   *   headerExtra  顶部右侧附加元素
   *   footer       底部操作区元素
   *   response     当前作答
   *   result       判定结果（有值即进入「已判定」状态）
   *   locked       是否禁止继续作答
   *   autoFocus    加载后自动聚焦输入
   *   onChange     作答变化回调
   *   onSubmit     提交回调
   *   actions      简答题输入区内的附加按钮
   *   afterAnswer  作答区下方附加内容（解析、AI 结果等）
   */
  function card(question, options) {
    var opts = options || {};
    var node = h('div.qcard');
    node.appendChild(metaRow(question, { extra: opts.headerExtra }));
    node.appendChild(stem(question));
    node.appendChild(
      answerArea(question, {
        response: opts.response,
        result: opts.result,
        locked: opts.locked,
        onChange: opts.onChange,
        onSubmit: opts.onSubmit,
        actions: opts.actions,
        autofocus: opts.autoFocus,
        problem: opts.problem,
      })
    );
    if (opts.result) node.appendChild(verdictBar(opts.result, question, { extra: opts.verdictExtra }));
    if (opts.afterAnswer) node.appendChild(opts.afterAnswer);
    if (opts.footer) node.appendChild(opts.footer);
    return node;
  }

  /* --------------------------------------------------- 代码复制委托 */

  document.addEventListener('click', function (event) {
    var button = event.target && event.target.closest ? event.target.closest('[data-copy]') : null;
    if (!button) return;
    var block = button.closest('.codeblock');
    var codeEl = block ? block.querySelector('code') : null;
    if (!codeEl) return;
    var text = codeEl.textContent;
    event.preventDefault();

    function done() {
      button.textContent = '已复制';
      button.classList.add('is-done');
      setTimeout(function () {
        button.textContent = '复制';
        button.classList.remove('is-done');
      }, 1400);
    }

    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(function () {
        fallbackCopy(text, done);
      });
    } else {
      fallbackCopy(text, done);
    }
  });

  function fallbackCopy(text, done) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', 'readonly');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand('copy');
      done();
    } catch (err) {
      ui.toast('复制失败，请手动选择代码', 'warn');
    }
    document.body.removeChild(ta);
  }

  QF.qview = {
    metaRow: metaRow,
    stem: stem,
    answerArea: answerArea,
    optionsReadOnly: optionsReadOnly,
    verdictBar: verdictBar,
    explainPanel: explainPanel,
    aiResultPanel: aiResultPanel,
    selfRate: selfRate,
    formatResponse: formatResponse,
    card: card,
  };
})();
