/* plandoc.js —— 每份计划配套的那份 Markdown 计划书（"和 coding agent 的 plan 一样"）。
 *
 * 两边各管各的：
 *   - **条目**（计划卡）是结构化的真相：勾选、改期、加删都以它为准；
 *   - **文档**是自由书写的工作文档：目标、拆解思路、进展、踩过的坑。
 *
 * 唯一的连接点是文档里 `<!-- qf:items:start -->` / `<!-- qf:items:end -->`
 * 标记之间的**活清单** —— 条目变了，后端只刷新这两个标记之间的内容，
 * 标记之外的文字一个字都不动（见 `app/plan_doc.py`）。AI 整份重写、把标记
 * 一起去掉，就是"我自己接管文档"，同步自然停止 —— 这不是故障。
 *
 * 两种来源，同一个右侧面板（与演示沙箱同一套开合方式，用户要"从右侧打开"）：
 *
 *   QF.plandoc.open(planId, title)              已落库的计划：读 / 写 /api/plans/{id}/doc
 *   QF.plandoc.openDraft(title, doc, onChange)  还没接受的提案：改的是卡片上的草稿，
 *                                               保存只回调 onChange，不碰服务器
 */
(function () {
  var ui = QF.ui;
  var h = ui.h;

  // 与后端 `plan_doc.DOC_MAX` 一致：本地先拦一次，别等一趟 400
  var DOC_MAX = 100000;

  //: 同时只开一个面板（从卡片上再点一次就是换内容，不是叠两层）
  var openPanel = null;

  function docUrl(planId) {
    return '/plans/' + encodeURIComponent(planId) + '/doc';
  }

  function sizeText(n) {
    return n + ' 字';
  }

  function stampText(iso) {
    var d = new Date(iso || '');
    if (isNaN(d.getTime())) return '';
    return (
      ' · 更新于 ' + (d.getMonth() + 1) + '月' + d.getDate() + '日 ' +
      (d.getHours() < 10 ? '0' : '') + d.getHours() + ':' +
      (d.getMinutes() < 10 ? '0' : '') + d.getMinutes()
    );
  }

  /**
   * 打开一份**已落库**计划的计划书。
   * `planId` 为空（对话里还没接受的提案）要改用 `openDraft`。
   */
  function open(planId, title) {
    if (!planId) {
      ui.toast('这份计划还没有接受 —— 接受之后才有计划书', 'warn', 2400);
      return;
    }
    mount({ planId: planId, title: title, draftMode: false });
  }

  /**
   * 打开一份**还没落库**的草稿（对话里的计划卡）。
   *
   * `doc` 是当前草稿；用户在面板里改完保存时，`onChange(新正文)` 把它交回卡片 ——
   * 卡片接受时发出去的就是这一版。不写服务器：那时候计划还不存在。
   */
  function openDraft(title, doc, onChange) {
    mount({
      planId: '',
      title: title,
      draftMode: true,
      doc: String(doc || ''),
      onChange: onChange || null,
    });
  }

  function mount(options) {
    // 已经开着一个：先收掉（不播退场，直接换内容，免得闪两下）
    if (openPanel) {
      openPanel.hardClose();
      openPanel = null;
    }

    var state = {
      planId: options.planId || '',
      draftMode: !!options.draftMode,
      title: options.title || '',
      doc: options.draftMode ? String(options.doc || '') : '',
      updatedAt: '',
      loaded: !!options.draftMode,
      failed: false,
      editing: false,
      busy: false,
    };

    var titleEl = h('span.plandoc__title', {
      text: (state.title ? state.title + ' · ' : '') + '计划书',
    });
    var closeBtn = h(
      'button.icon-btn.plandoc__close',
      { type: 'button', title: '关闭（Esc）', 'aria-label': '关闭' },
      h('span', { html: ui.icon('close', 16) })
    );

    var meta = h('span.plandoc__meta', { text: state.loaded ? sizeText(state.doc.length) : '正在打开…' });
    var editBtn = h('button.btn.btn--ghost.btn--sm', { type: 'button', onClick: startEdit }, '编辑');
    var cancelBtn = h(
      'button.btn.btn--ghost.btn--sm',
      { type: 'button', onClick: cancelEdit },
      '取消'
    );
    var saveBtn = h('button.btn.btn--primary.btn--sm', { type: 'button', onClick: save }, '保存');
    editBtn.hidden = true;
    cancelBtn.hidden = true;
    saveBtn.hidden = true;

    var bar = h(
      'div.plandoc__bar',
      null,
      meta,
      h('span.plandoc__actions', null, editBtn, cancelBtn, saveBtn)
    );

    var prose = h('div.plandoc__prose');
    var view = h('div.plandoc__view', null, prose);
    var editor = h('textarea.plandoc__editor', {
      'aria-label': '计划书正文',
    });
    editor.hidden = true;

    var hint = h('p.plandoc__hint');
    var scroll = h('div.plandoc__scroll', null, view, editor);

    var panel = h(
      'aside.plandoc__panel',
      { role: 'dialog', 'aria-modal': 'true', 'aria-label': '计划书' },
      h('header.plandoc__head', null, titleEl, closeBtn),
      bar,
      scroll,
      hint
    );
    var backdrop = h('div.plandoc__backdrop');
    var overlay = h('div.plandoc-overlay', null, backdrop, panel);

    function paint() {
      var canEdit = state.loaded && !state.failed;
      editBtn.hidden = !canEdit || state.editing;
      cancelBtn.hidden = !state.editing;
      saveBtn.hidden = !state.editing;
      saveBtn.disabled = state.busy;
      view.hidden = state.editing;
      editor.hidden = !state.editing;

      if (state.failed) {
        meta.textContent = '打不开';
        return;
      }
      if (!state.loaded) {
        meta.textContent = '正在打开…';
        hint.textContent = '';
        return;
      }
      if (state.editing) {
        meta.textContent = sizeText(editor.value.length);
        hint.textContent =
          '保存会整份替换这份文档。保留 qf:items 两个标记，「待办」里的清单才会继续跟着计划卡走。';
      } else {
        meta.textContent = sizeText(state.doc.length) + stampText(state.updatedAt);
        hint.textContent = state.draftMode
          ? '这是提案里的草稿。你可以直接改；保存后卡片上的正文一起更新，接受时以这一版为准。'
          : '条目勾选会刷新标记之间的清单；标记之外的文字，系统不动。AI 在对话里也能读写这份文档。';
      }
    }

    function fillView() {
      if (!state.doc.trim()) {
        ui.clear(prose);
        prose.appendChild(
          h('p.plandoc__empty', {
            text: state.draftMode
              ? '这份提案还没有正文 —— 点「编辑」自己写一份，或者回对话里让 AI 先写。'
              : '这份计划书还是空的 —— 点「编辑」开始写。',
          })
        );
        return;
      }
      if (QF.md && QF.md.renderInto) QF.md.renderInto(prose, state.doc);
      else prose.textContent = state.doc;
    }

    function startEdit() {
      if (!state.loaded || state.busy || state.failed) return;
      editor.value = state.doc;
      state.editing = true;
      paint();
      editor.focus();
    }

    function cancelEdit() {
      if (state.busy) return;
      state.editing = false;
      editor.value = state.doc; // 丢掉未保存的改动，回到当前那一版
      paint();
    }

    function afterSaved(text, updatedAt) {
      state.busy = false;
      state.editing = false;
      state.doc = text;
      state.updatedAt = updatedAt || '';
      state.loaded = true;
      state.failed = false;
      fillView();
      paint();
    }

    function save() {
      if (!state.loaded || state.busy || !state.editing) return;
      var text = editor.value;
      if (text.length > DOC_MAX) {
        ui.toast('计划文档最多 ' + DOC_MAX + ' 字，这份 ' + text.length + ' 字', 'warn', 2800);
        return;
      }
      if (state.draftMode) {
        afterSaved(text, '');
        if (options.onChange) options.onChange(text);
        ui.toast('已更新到卡片', 'ok', 1500);
        return;
      }
      state.busy = true;
      paint();
      QF.api.put(docUrl(state.planId), { doc: text }).then(
        function (data) {
          var saved = data && data.doc != null ? String(data.doc) : text;
          afterSaved(saved, (data && data.updatedAt) || '');
          ui.toast('计划书已保存', 'ok', 1600);
        },
        function (err) {
          state.busy = false;
          paint();
          ui.toast((err && err.message) || '没保存上', 'bad', 2800);
        }
      );
    }

    editor.addEventListener('input', function () {
      if (state.editing) meta.textContent = sizeText(editor.value.length);
    });

    function onKey(ev) {
      // Ctrl/Cmd+Enter 保存：写长文时不用把手挪到鼠标上
      if (ev.key === 'Enter' && (ev.metaKey || ev.ctrlKey) && state.editing) {
        ev.preventDefault();
        ev.stopPropagation();
        save();
        return;
      }
      if (ev.key !== 'Escape') return;
      // 面板开着的时候，Esc 只归它 —— 别再让底下的对话 / 日程也动一下
      ev.preventDefault();
      ev.stopPropagation();
      // 编辑时 Esc 先退回查看，而不是把整个面板关掉
      if (state.editing) {
        cancelEdit();
        return;
      }
      close();
    }

    function hardClose() {
      if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
      document.removeEventListener('keydown', onKey, true);
    }

    var closing = false;
    function close() {
      if (closing) return;
      closing = true;
      document.removeEventListener('keydown', onKey, true);
      if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
        hardClose();
        if (openPanel === api) openPanel = null;
        return;
      }
      overlay.classList.add('is-out');
      window.setTimeout(function () {
        hardClose();
        if (openPanel === api) openPanel = null;
      }, 200);
    }

    closeBtn.addEventListener('click', close);
    backdrop.addEventListener('click', close);
    document.addEventListener('keydown', onKey, true);
    document.body.appendChild(overlay);
    // 入场那一帧再加 is-open（与 chatdemo 同一套：挂载即播）
    requestAnimationFrame(function () {
      if (!overlay.parentNode) return;
      overlay.classList.add('is-open');
    });

    var api = { close: close, hardClose: hardClose };
    openPanel = api;

    paint();

    if (!state.draftMode) {
      QF.api.get(docUrl(state.planId)).then(
        function (data) {
          state.doc = String((data && data.doc) || '');
          state.updatedAt = (data && data.updatedAt) || '';
          state.loaded = true;
          fillView();
          paint();
        },
        function (err) {
          state.failed = true;
          hint.textContent = '';
          prose.textContent = '打不开这份计划书：' + ((err && err.message) || err);
          paint();
        }
      );
    } else {
      fillView();
      paint();
    }
  }

  /** 收掉当前打开的面板（接受计划后调用：那张草稿已经没意义了） */
  function close() {
    if (openPanel) openPanel.close();
  }

  QF.plandoc = { open: open, openDraft: openDraft, close: close, DOC_MAX: DOC_MAX };
})();
