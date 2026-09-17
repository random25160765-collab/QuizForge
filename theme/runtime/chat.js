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
  var noticeEl = null;
  var inputEl = null;
  var sendBtn = null;
  var hintEl = null;

  /** `/api/ai/usage` 的结果：走哪条通道、能不能用（决定提示怎么写） */
  var aiState = null;

  var state = {
    list: [], // 会话列表（服务端给的，含条数与预览）
    current: null, // 当前会话 id
    messages: [], // **整棵树**（一条不落），当前分支是按 parentId 算出来的
    picks: {}, // 用户在某个分叉上选过哪一支：{ 父节点 key: 子消息 id }
    busy: false, // 正在流式
    controller: null, // AbortController：停止按钮用它
    live: null, // 正在长的那条
    query: '', // 搜索框里的字（≥2 字就把左栏换成搜索结果）
    results: [], // 搜索结果
  };

  var searchTimer = null;

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

  /* ------------------------------------------------------------ 骨架 */

  function buildSkeleton() {
    rootEl.textContent = '';

    // 左栏只建一次：搜索框如果跟着列表一起重画，打字打到一半就会丢焦点
    asideEl = h('aside.chat__aside');
    asideHeadEl = h('div.chat__asidehead');
    listEl = h('div.chat__list');
    asideEl.appendChild(asideHeadEl);
    asideEl.appendChild(listEl);
    threadEl = h('div.chat__thread', { role: 'log', 'aria-live': 'polite' });
    inputEl = h('textarea.chat__input', {
      rows: '1',
      placeholder: '问点什么，或者贴一段材料…（Enter 发送，Shift+Enter 换行）',
      onInput: growInput,
      onKeydown: onKeydown,
    });
    sendBtn = h('button.btn.btn--primary.chat__send', { type: 'button', onClick: onSendClick }, '发送');
    hintEl = h('div.chat__hint');
    noticeEl = h('div.chat__notice');
    rootEl.appendChild(
      h(
        'div.chat',
        null,
        asideEl,
        h(
          'section.chat__main',
          null,
          threadEl,
          noticeEl,
          h('div.chat__composer', null, h('div.chat__box', null, inputEl, sendBtn), hintEl)
        )
      )
    );
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

    var list = h('div.chatlist');
    state.list.forEach(function (conv) {
      var item = h(
        'div.chatlist__item' + (conv.id === state.current ? '.is-on' : ''),
        {
          onClick: function () {
            if (conv.id !== state.current) openConversation(conv.id);
          },
        },
        h('div.chatlist__title', { text: conv.title || '未命名对话' }),
        h(
          'div.chatlist__meta',
          null,
          h('span', { text: ui.fmtRelative(conv.updatedAtMs) }),
          conv.messageCount ? h('span', { text: '· ' + conv.messageCount + ' 条' }) : null
        ),
        conv.preview ? h('div.chatlist__preview', { text: conv.preview }) : null,
        h('button.chatlist__more', {
          type: 'button',
          title: '重命名 / 删除',
          'aria-label': '重命名或删除',
          onClick: function (event) {
            event.stopPropagation(); // 别顺带把会话也切了
            openMenu(conv);
          },
        }, '⋯')
      );
      list.appendChild(item);
    });
    listEl.appendChild(list);
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

    state.picks = {};
    for (var i = 0; i < chain.length - 1; i++) {
      state.picks[keyOf(chain[i].id)] = chain[i + 1].id;
    }
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
        renderAside();
      })
      .catch(function (err) {
        ui.toast(err.message, 'error');
      });
  }

  /* ------------------------------------------------------------ 主区 */

  function renderEmptyThread() {
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

  function paintThread() {
    if (!state.messages.length) {
      renderEmptyThread();
      return;
    }
    ui.clear(threadEl);
    activePath().forEach(function (m) {
      threadEl.appendChild(messageRow(m));
    });
    scrollToEnd(true);
  }

  /** `‹ 2 / 3 ›`：同一个父节点下的几个分支，翻着看（LibreChat 的 SiblingSwitch）。 */
  function siblingSwitch(m, siblings) {
    var index = 0;
    siblings.forEach(function (item, i) {
      if (item.id === m.id) index = i;
    });
    var prev = h('button.chatmsg__sib', {
      type: 'button',
      title: '上一个分支',
      disabled: index === 0,
      onClick: function () {
        pickBranch(m.parentId, siblings[index - 1]);
      },
    });
    prev.textContent = '‹';
    var next = h('button.chatmsg__sib', {
      type: 'button',
      title: '下一个分支',
      disabled: index === siblings.length - 1,
      onClick: function () {
        pickBranch(m.parentId, siblings[index + 1]);
      },
    });
    next.textContent = '›';
    return h(
      'div.chatmsg__siblings',
      null,
      prev,
      h('span.chatmsg__sibcount', { text: index + 1 + ' / ' + siblings.length }),
      next
    );
  }

  function messageRow(m) {
    var isUser = m.role === 'user';
    var body = h('div.chatmsg__body');

    // 有兄弟就显示切换器：没有它，"重新回答"过的旧分支就永远够不着了
    var siblings = siblingsOf(m);
    if (siblings.length > 1) body.appendChild(siblingSwitch(m, siblings));

    if (isUser) {
      body.appendChild(h('div.chatmsg__text', { text: m.content }));
    } else {
      // 正文是投影，零件才是真相：旧消息没有 parts 时按正文兜一个
      body.appendChild(
        partsNode(m.parts && m.parts.length ? m.parts : [{ type: 'text', text: m.content || '' }])
      );
    }
    var row = h(
      'div.chatmsg' + (isUser ? '.chatmsg--user' : '.chatmsg--assistant') + (m.status === 'error' ? '.is-error' : ''),
      { dataset: { id: String(m.id || '') } },
      h('div.chatmsg__who', { text: isUser ? '我' : 'AI' }),
      body
    );
    decorateAssistant(row, body, m);
    return row;
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

    var needsAI = QF.engine.grade(question, emptyResponse(question)).requiresAI;
    var submit = h(
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
      draft.busy ? '批改中…' : needsAI ? '提交（AI 批改）' : '提交'
    );
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
          '让 AI 讲讲这道题'
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
    return String(response === null || response === undefined ? '' : response).trim();
  }

  /**
   * 「让 AI 讲讲这道题」。
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
    if (type === 'text') return QF.md.render(String(part.text || ''));
    if (type === 'think') return thinkNode(part);
    if (type === 'tool_call') return toolNode(part);
    if (type === 'card') return questionCard(part);
    if (type === 'action') return actionNode(part);
    if (type === 'citation') return citationNode(part);
    if (type === 'summary') return h('div.chatmsg__note', { text: part.text || '' });
    if (type === 'error') return h('div.chatmsg__why', { text: part.message || '出错了' });
    return null;
  }

  /** 推理：折起来。它是过程，不是结论 —— 想看的人点开，不想看的人不被它挤走。 */
  function thinkNode(part) {
    return h(
      'details.chatmsg__think',
      null,
      h('summary', { text: '思考过程' }),
      h('div.md', { html: QF.md.renderToString(String(part.text || '')) })
    );
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
      // 切换器也就永远只有 1 / 1（LibreChat 在每条回答下都放了这个入口）
      foot.appendChild(
        h('button.chatmsg__action', {
          type: 'button',
          text: '重新回答',
          onClick: function () {
            if (state.busy) return;
            regenerate(m);
          },
        })
      );
    }

    if (foot.childNodes.length) body.appendChild(foot);
  }

  function retryButton(m) {
    return h('button.chatmsg__retry', {
      type: 'button',
      text: '重新回答',
      onClick: function () {
        if (state.busy) return;
        regenerate(m);
      },
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

    function render(immediate) {
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
      if (!immediate) {
        // 每来一个字就重排一遍 Markdown 会又抖又费，攒 100ms 刷一次
        timer = setTimeout(function () {
          render(true);
        }, 100);
        return;
      }
      var stick = nearBottom();
      ui.clear(body);
      body.appendChild(partsNode(parts));
      if (state.busy) body.appendChild(h('span.chat__caret'));
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
        scrollToEnd(false);
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
    return threadEl.scrollHeight - threadEl.scrollTop - threadEl.clientHeight < 120;
  }

  function send(options) {
    var opts = options || {};
    var text = String(opts.content || '').trim();
    if (!text && !opts.replyTo) return Promise.resolve();
    if (state.busy) return Promise.resolve();

    return ensureConversation().then(function () {
      // 新消息挂在**当前这一支的末尾**（用户翻到旧分支上接着问，就该挂在那儿）
      var path = activePath();
      var leaf = path.length ? path[path.length - 1].id : null;
      if (!opts.replyTo) delete state.picks[keyOf(leaf)];

      var local = null;
      var localId = '';
      if (text) {
        local = {
          id: 'local-' + ++LOCAL_ID,
          role: 'user',
          content: text,
          status: 'ok',
          parentId: leaf,
        };
        localId = local.id;
        state.messages.push(local);
        if (threadEl.querySelector('.chat__intro')) ui.clear(threadEl);
        threadEl.appendChild(messageRow(local));
        inputEl.value = '';
        growInput();
        scrollToEnd(true);
      }

      state.busy = true;
      state.controller = new AbortController();
      updateComposer();
      scrollToEnd(true);

      var settled = false;

      var body = opts.replyTo
        ? { replyTo: opts.replyTo }
        : { content: text, parentId: leaf };
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
              local.createdAtMs = m.createdAtMs;
              var node = threadEl.querySelector('[data-id="' + localId + '"]');
              if (node) node.dataset.id = String(m.id);
            },
            start: function (m) {
              settled = false;
              state.messages.push(m);
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
              if (state.live && d && d.text) state.live.pushNote(d.text);
            },
            tool: function (d) {
              if (!state.live || !d) return;
              if (d.phase === 'start') state.live.toolStart(d);
              else state.live.toolResult(d);
            },
            card: function (d) {
              if (state.live && d && d.card) state.live.pushCard(d.card);
            },
            action: function (d) {
              if (state.live && d && d.proposal) state.live.pushAction(d.proposal);
            },
            citation: function (d) {
              if (state.live && d && d.citation) state.live.pushPart(d.citation);
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
          // 开流之前就被拦下的情况（没填密钥、超配额）：这时**没有**任何消息落库，
          // 所以把用户刚打的字还回输入框，让他改完设置直接重发
          if (local) {
            state.messages = state.messages.filter(function (m) {
              return m !== local;
            });
            inputEl.value = text;
            growInput();
          }
          ui.toast(err.message, 'error');
          paintThread();
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
          // 重生成之后要重画一遍：新分支成了当前这一支，旧那条退到切换器后面去。
          // 不重画的话，界面会同时留着两条（它们现在是兄弟，不是一条线上的两条）
          if (opts.replyTo) paintThread();
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
      sendBtn.textContent = '停止';
      sendBtn.classList.remove('btn--primary');
      hintEl.textContent = '正在生成…（点「停止」会留下已经生成的部分）';
      return;
    }

    sendBtn.textContent = '发送';
    sendBtn.classList.add('btn--primary');

    var bits = [];
    if (!state.current) bits.push('第一句话会开一条新对话');
    var mode = aiState && aiState.mode;
    if (mode === 'beta') {
      // 说清"现在是谁在回答"：内测通道答得不理想时，用户要知道换模型的办法
      bits.push((aiState.label || '内测通道') + '（想换：设置 → AI 填自己的密钥）');
    } else if (mode === 'user' && aiState.model) {
      bits.push('模型：' + aiState.model);
    }
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
    if (!mode || mode === 'user' || mode === 'local') return;

    var text =
      mode === 'off'
        ? '本站已关闭 AI 功能，对话暂时不可用。'
        : '还不能对话：去设置里填自己的 API 密钥，或让站长开启内测通道。';
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
    var gap = threadEl.scrollHeight - threadEl.scrollTop - threadEl.clientHeight;
    // 只在贴着底的时候跟着滚：用户往上翻着读旧消息时，不该被新字拽回去
    if (force || gap < 120) threadEl.scrollTop = threadEl.scrollHeight;
  }

  function ensureConversation() {
    if (state.current) return Promise.resolve(state.current);
    return api.post('/chat/conversations', {}).then(function (res) {
      state.current = res.conversation.id;
      state.messages = [];
      state.list.unshift(res.conversation);
      renderAside();
      updateComposer();
      return state.current;
    });
  }

  function openConversation(id) {
    return api
      .get('/chat/conversations/' + id)
      .then(function (res) {
        state.current = id;
        state.messages = (res && res.messages) || [];
        state.picks = {}; // 默认跟最新那一支
        state.live = null;
        renderAside();
        paintThread();
        updateComposer();
      })
      .catch(function (err) {
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
    state.current = null;
    state.messages = [];
    state.live = null;
    renderAside();
    paintThread();
    updateComposer();
    inputEl.focus();
  }

  function openMenu(conv) {
    var field = h('input.input', { type: 'text', value: conv.title || '' });
    var modal = ui.modal({
      title: '对话设置',
      size: 'sm',
      body: h('div.form', null, h('div.field', null, h('div.field__label', { text: '标题' }), field)),
      actions: [
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
    var conf = QF.store.settings();
    document.documentElement.style.setProperty('--font-scale', String(conf.fontScale || 1));
    document.documentElement.style.setProperty('--content-max', (conf.maxWidth || 880) + 'px');

    // 主题按钮、设置按钮、当前页图标都由 shell.js 统一接线（全站一份）
    QF.shell.mount({});

    buildSkeleton();
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

  var chat = { boot: boot, booted: false };
  QF.chat = chat;
})();
