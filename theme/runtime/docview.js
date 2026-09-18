/* docview.js —— 一份文件该怎么看（PDF / 图片 / Markdown / Word / 幻灯片 / 纯文本）。
 *
 * 这段逻辑原先只长在资料页里（`library.js` 的 `viewPanel`），窗格要装"某个文件"
 * 就得再写一遍 —— 于是抽成这一份，两边共用：
 *
 *   资料页：自己已经取回了 `/library/view` 的返回，直接 `payload` 喂进来
 *   窗格里：给个 `{citekey, index}`，它自己去取
 *
 * 三种"看不了"的情况都照实说（旧格式 / 二进制 / 后端没转出来），并留一个下载 ——
 * 静默给一片空白是最糟的。
 */
(function () {
  var ui = QF.ui;
  var h = ui.h;
  // 注意：这里**不能**把 `QF.api` 取一次存起来。本文件登记在 RUNTIME_ORDER 里，
  // 谁先谁后不由它决定；踩过 —— 早期登记在 api.js 之前，取到的是 undefined，
  // 于是"窗格里看文件"一挂就报 `Cannot read properties of undefined`。
  // 每个请求现取现用，与加载顺序解耦。
  function apiGet(path) {
    return QF.api.get(path);
  }

  var LABEL = {
    pdf: 'PDF',
    image: '图片',
    markdown: 'Markdown',
    docx: 'Word',
    pptx: '幻灯片',
    text: '纯文本',
    legacy: '旧格式',
    binary: '二进制',
  };

  function label(kind) {
    return LABEL[kind] || kind || '';
  }

  /** 画正文（不含顶部那条栏 —— 那是各页自己的事）。 */
  function paint(box, one) {
    if (one.kind === 'pdf') {
      box.appendChild(h('iframe.docv__frame', { src: one.raw, title: one.name || 'PDF' }));
    } else if (one.kind === 'image') {
      box.appendChild(h('img.docv__image', { src: one.raw, alt: one.name || '', loading: 'lazy' }));
    } else if (one.kind === 'markdown') {
      var prose = h('div.docv__prose');
      if (QF.md && QF.md.renderInto) QF.md.renderInto(prose, one.text || '');
      else prose.textContent = one.text || '';
      box.appendChild(prose);
    } else if (one.kind === 'docx') {
      if (one.html) {
        var doc = h('div.docv__prose.docv__prose--docx');
        doc.innerHTML = one.html;          // 后端已经洗过脚本与事件属性
        box.appendChild(doc);
      } else {
        box.appendChild(h('div.docv__hint', { text: one.note || '没转出内容。' }));
      }
    } else if (one.kind === 'pptx') {
      var slides = one.slides || [];
      slides.forEach(function (slide) {
        box.appendChild(h('div.docv__slide', null,
          h('div.docv__slideno', { text: '第 ' + slide.index + ' 页' }),
          h('div.docv__slidetitle', { text: slide.title }),
          h('ul.docv__slidebody', null, (slide.lines || []).map(function (line) {
            return h('li', { text: line });
          }))));
      });
      if (!slides.length) box.appendChild(h('div.docv__hint', { text: one.note || '没解出幻灯片。' }));
    } else if (one.kind === 'text') {
      box.appendChild(h('pre.docv__text', { text: one.text || '' }));
    } else {
      box.appendChild(h('div.docv__hint', {
        text: one.note || '这个格式没有内建查看器，用「下载」看吧。',
      }));
    }
  }

  /**
   * render(host, target, opts)
   *
   *   target = { citekey, index }  要看的哪一份（index=-1 是主文件）
   *   opts.payload                 已经有 `/library/view` 的返回时直接给它，不再请求
   *   opts.head                    true 时在顶上画一条"文件名 + 类型 + 下载"
   *
   * 返回一个清理函数（窗格关标签时会把没回来的请求作废）。
   */
  function render(host, target, opts) {
    var options = opts || {};
    var box = h('div.docv');
    host.appendChild(box);

    function fill(one) {
      ui.clear(box);
      if (options.head !== false) {
        box.appendChild(h('div.docv__bar', null,
          h('span.docv__name', { text: one.name || '', title: one.path || '' }),
          h('span.docv__kind', { text: label(one.kind) }),
          one.raw ? h('a.docv__download', { href: one.raw, download: one.name || '', text: '下载' }) : null));
      }
      var body = h('div.docv__body');
      box.appendChild(body);
      paint(body, one);
    }

    if (options.payload) {
      fill(options.payload);
      return function () {};
    }

    box.appendChild(h('div.docv__hint', { text: '正在打开…' }));
    var killed = false;
    apiGet('/library/view?citekey=' + encodeURIComponent(target.citekey) +
      '&index=' + (target.index == null ? -1 : target.index))
      .then(function (one) { if (!killed) fill(one); })
      .catch(function (err) {
        if (killed) return;
        ui.clear(box);
        box.appendChild(h('div.docv__hint', { text: '打不开这份文件：' + ((err && err.message) || err) }));
      });
    return function () { killed = true; };
  }

  QF.docview = { render: render, label: label, paint: paint };
})();
