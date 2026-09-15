/* ===========================================================================
 * md.js —— 受控子集的 Markdown 渲染器 + LaTeX 保护
 *
 * 为什么不用 marked / markdown-it：
 *   本机无外网，无法下载这些库；而题库 Markdown 是受控子集（标题 / 列表 /
 *   围栏代码 / 行内码 / 强调 / 链接 / 引用 / 表格 / 分割线），自己实现约 300
 *   行即可覆盖，并且能**原生解决 LaTeX 与 Markdown 的语法冲突**。
 *
 * LaTeX 冲突是怎么解决的：
 *   `$a_b$` 里的下划线会被 Markdown 当成斜体、`x^2` 的 `*` 会被当成强调。
 *   因此解析顺序被固定为：
 *     围栏代码块 → 数学公式 → 行内代码 → HTML 转义 → 行内标记 → 还原
 *   数学公式与代码在解析前就被抽成 \u0000M0\u0000 形式的占位符，
 *   完全不参与 Markdown 语法分析，最后再交给 KaTeX 渲染。
 * ========================================================================= */
(function () {
  'use strict';

  var QF = (window.QF = window.QF || {});
  var ui = QF.ui;
  var h = ui.h;

  var PLACEHOLDER = /\u0000([MIC])(\d+)\u0000/g;
  var FENCE = /^\s{0,3}(`{3,}|~{3,})\s*([\w+#.-]*)\s*$/;
  var HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
  var HR = /^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/;
  var QUOTE = /^\s{0,3}>\s?(.*)$/;
  var ULIST = /^(\s*)([-*+])\s+(.*)$/;
  var OLIST = /^(\s*)(\d+)[.)]\s+(.*)$/;
  var TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/;

  function esc(text) {
    return ui.esc(text);
  }

  /* ----------------------------------------------------------- HTML 转义 */

  /* ------------------------------------------------------- 围栏代码块抽取 */

  function extractFences(src, store) {
    var lines = src.split('\n');
    var out = [];
    var i = 0;
    while (i < lines.length) {
      var match = FENCE.exec(lines[i]);
      if (match) {
        var marker = match[1][0];
        var len = match[1].length;
        var lang = (match[2] || '').toLowerCase();
        var body = [];
        var j = i + 1;
        var closed = false;
        while (j < lines.length) {
          var closeMatch = FENCE.exec(lines[j]);
          if (closeMatch && closeMatch[1][0] === marker && closeMatch[1].length >= len) {
            closed = true;
            break;
          }
          body.push(lines[j]);
          j++;
        }
        var token = '\u0000C' + store.length + '\u0000';
        store.push({ type: 'code', lang: lang, code: body.join('\n') });
        out.push(token);
        i = closed ? j + 1 : j;
        continue;
      }
      out.push(lines[i]);
      i++;
    }
    return out.join('\n');
  }

  /* --------------------------------------------------------- 数学公式抽取 */

  function extractMath(src, store) {
    function push(tex, display) {
      var token = '\u0000M' + store.length + '\u0000';
      store.push({ type: 'math', tex: tex, display: display });
      return token;
    }

    var text = src;

    // 块级：$$ ... $$ 与 \[ ... \]（允许跨行）
    text = text.replace(/\$\$([\s\S]+?)\$\$/g, function (whole, tex) {
      return tex.trim() ? push(tex.trim(), true) : whole;
    });
    text = text.replace(/\\\[([\s\S]+?)\\\]/g, function (whole, tex) {
      return tex.trim() ? push(tex.trim(), true) : whole;
    });

    // 行内：$ ... $ 与 \( ... \)
    // 约束：单行内、内容非空、首尾不为空白。「首尾不许有空白」这条本身就挡住了
    // 「价格 $100 到 $200」这类写法 —— 匹配出来的 tex 会以空格结尾，直接放过。
    //
    // 这里曾经还有一条「纯数字不当公式」的规则（怕把 $100$ 当公式），代价是题目里
    // 老实写的 $192$、$300$、$2$ 被原样吐出来。技术题库里纯数字公式很常见
    // （规格表、参数、带宽/算力数值），货币写法反而是稀客，这条规则弊大于利。
    text = text.replace(/\$(?!\$)([^\n$]*?)\$(?!\$)/g, function (whole, tex) {
      if (!tex.trim() || /^\s|\s$/.test(tex)) return whole;
      return push(tex, false);
    });
    text = text.replace(/\\\(([\s\S]+?)\\\)/g, function (whole, tex) {
      return tex.trim() ? push(tex, false) : whole;
    });

    return text;
  }

  /* --------------------------------------------------------- 行内代码抽取 */

  function extractInlineCode(src, store) {
    // 反引号数量可变：`` `code` `` 这种写法也支持
    return src.replace(/(`+)([\s\S]*?)\1/g, function (whole, ticks, code) {
      if (!code.trim() && ticks.length === 1) return whole;
      var token = '\u0000I' + store.length + '\u0000';
      store.push({ type: 'icode', code: code.trim() });
      return token;
    });
  }

  /* ------------------------------------------------------------- 行内标记 */

  function inlineMarkup(escapedText) {
    var text = escapedText;

    // 链接 [text](url)
    text = text.replace(/\[([^\]\n]+)\]\(([^)\s]+)(?:\s+&quot;([^&]*)&quot;)?\)/g, function (whole, label, href) {
      var safe = sanitizeUrl(href);
      if (!safe) return whole;
      return '<a href="' + safe + '" target="_blank" rel="noopener noreferrer">' + label + '</a>';
    });

    // 图片 ![alt](src)
    text = text.replace(/!\[([^\]\n]*)\]\(([^)\s]+)\)/g, function (whole, alt, src) {
      var safe = sanitizeUrl(src);
      if (!safe) return whole;
      return '<img src="' + safe + '" alt="' + alt + '" loading="lazy">';
    });

    // 裸链接
    text = text.replace(/(^|[\s(])(https?:\/\/[^\s<>()]+)/g, function (whole, pre, url) {
      return pre + '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + url + '</a>';
    });

    // 强调：先 ** / __，再 * / _
    text = text.replace(/\*\*([^\n*]+?)\*\*/g, '<strong>$1</strong>');
    text = text.replace(/(^|\W)__([^\n_]+?)__(?=\W|$)/g, '$1<strong>$2</strong>');
    text = text.replace(/(^|[^*\w])\*([^\n*]+?)\*(?=[^*\w]|$)/g, '$1<em>$2</em>');
    text = text.replace(/(^|\W)_([^\n_]+?)_(?=\W|$)/g, '$1<em>$2</em>');

    // 删除线
    text = text.replace(/~~([^\n~]+?)~~/g, '<del>$1</del>');

    // 高亮 ==text==
    text = text.replace(/==([^\n=]+?)==/g, '<mark>$1</mark>');

    return text;
  }

  function sanitizeUrl(url) {
    var value = String(url || '').trim();
    if (/^(https?:|mailto:|#|\/|\.\/|\.\.\/)/i.test(value)) return esc(value);
    if (/^[\w.-]+\.(?:png|jpe?g|gif|svg|webp)$/i.test(value)) return esc(value);
    return '';
  }

  /* ----------------------------------------------------------- 还原占位符 */

  function restore(html, store, options) {
    return html.replace(PLACEHOLDER, function (whole, kind, index) {
      var item = store[Number(index)];
      if (!item) return '';
      if (kind === 'C') {
        var lang = item.lang || 'text';
        var label = lang === 'text' ? '' : lang;
        var codeHtml = QF.highlight
          ? QF.highlight.code(item.code, lang)
          : esc(item.code);
        return (
          '<div class="codeblock" data-lang="' + esc(lang) + '">' +
          '<div class="codeblock__bar"><span class="codeblock__lang">' + esc(label || 'text') + '</span>' +
          '<button type="button" class="codeblock__copy" data-copy="1">复制</button></div>' +
          '<pre class="codeblock__pre"><code>' + codeHtml + '</code></pre>' +
          '</div>'
        );
      }
      if (kind === 'I') {
        return '<code class="icode">' + esc(item.code) + '</code>';
      }
      return renderMath(item.tex, item.display, options);
    });
  }

  function renderMath(tex, display, options) {
    var opts = options || {};
    if (window.katex) {
      try {
        var html = window.katex.renderToString(tex, {
          displayMode: !!display,
          throwOnError: false,
          errorColor: '#F87171',
          strict: 'ignore',
          trust: false,
          macros: opts.macros || {},
          output: 'htmlAndMathml',
        });
        return display ? '<div class="mathblock">' + html + '</div>' : html;
      } catch (err) {
        return '<span class="math-error" title="' + esc(String(err && err.message)) + '">$' + esc(tex) + '$</span>';
      }
    }
    return display
      ? '<div class="mathblock mathblock--raw">$$' + esc(tex) + '$$</div>'
      : '<span class="mathblock--raw">$' + esc(tex) + '$</span>';
  }

  /* ------------------------------------------------------------- 块级解析 */

  function splitRow(line) {
    var trimmed = line.trim().replace(/^\|/, '').replace(/\|$/, '');
    return trimmed.split('|').map(function (cell) {
      return cell.trim();
    });
  }

  function parseBlocks(text, store, options) {
    var lines = text.split('\n');
    var nodes = [];
    var i = 0;

    function isBlank(line) {
      return !line.trim();
    }

    while (i < lines.length) {
      var line = lines[i];

      if (isBlank(line)) {
        i++;
        continue;
      }

      // 围栏代码块占位符独占一行
      var lone = /^\u0000C(\d+)\u0000$/.exec(line.trim());
      if (lone) {
        nodes.push({ type: 'rawHtml', html: restore(line.trim(), store, options) });
        i++;
        continue;
      }

      var heading = HEADING.exec(line);
      if (heading) {
        var level = heading[1].length;
        nodes.push({
          type: 'heading',
          level: level,
          text: heading[2],
        });
        i++;
        continue;
      }

      if (HR.test(line)) {
        nodes.push({ type: 'hr' });
        i++;
        continue;
      }

      // 表格
      if (line.indexOf('|') !== -1 && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
        var head = splitRow(line);
        var rows = [];
        var k = i + 2;
        while (k < lines.length && lines[k].indexOf('|') !== -1 && lines[k].trim()) {
          rows.push(splitRow(lines[k]));
          k++;
        }
        nodes.push({ type: 'table', head: head, rows: rows });
        i = k;
        continue;
      }

      // 引用块
      if (QUOTE.test(line)) {
        var quoteLines = [];
        while (i < lines.length && (QUOTE.test(lines[i]) || (lines[i].trim() && quoteLines.length))) {
          var qm = QUOTE.exec(lines[i]);
          quoteLines.push(qm ? qm[1] : lines[i]);
          i++;
        }
        nodes.push({ type: 'quote', lines: quoteLines });
        continue;
      }

      // 列表
      if (ULIST.test(line) || OLIST.test(line)) {
        var items = [];
        while (i < lines.length) {
          var um = ULIST.exec(lines[i]);
          var om = OLIST.exec(lines[i]);
          if (!um && !om) {
            // 列表项的续行（缩进）
            if (lines[i].trim() && /^\s{2,}/.test(lines[i]) && items.length) {
              items[items.length - 1].text += '\n' + lines[i].trim();
              i++;
              continue;
            }
            break;
          }
          var depth = Math.floor((um ? um[1] : om[1]).replace(/\t/g, '    ').length / 2);
          items.push({
            ordered: !!om,
            depth: depth,
            marker: om ? om[2] : null,
            text: (um ? um[3] : om[3]) || '',
          });
          i++;
        }
        nodes.push({ type: 'list', items: items });
        continue;
      }

      // 段落
      var para = [];
      while (i < lines.length && !isBlank(lines[i])) {
        if (
          HEADING.test(lines[i]) ||
          HR.test(lines[i]) ||
          QUOTE.test(lines[i]) ||
          FENCE.exec(lines[i]) ||
          /^\u0000C\d+\u0000$/.test(lines[i].trim()) ||
          ((ULIST.test(lines[i]) || OLIST.test(lines[i])) && !para.length)
        ) {
          if (para.length) break;
        }
        para.push(lines[i].trim());
        i++;
      }
      if (para.length) nodes.push({ type: 'paragraph', text: para.join('\n') });
    }

    return nodes;
  }

  /* ------------------------------------------------------------- 渲染 */

  function renderNodes(nodes, store, options) {
    var container = h('div.md');
    nodes.forEach(function (node) {
      switch (node.type) {
        case 'rawHtml':
          container.appendChild(h('div.md__raw', { html: node.html }));
          break;
        case 'heading':
          container.appendChild(
            h('h' + ui.clamp(node.level, 1, 6), { class: 'md__h md__h' + node.level,
              html: restore(inlineMarkup(esc(node.text)), store, options) })
          );
          break;
        case 'hr':
          container.appendChild(h('hr.md__hr'));
          break;
        case 'paragraph':
          container.appendChild(
            h('p.md__p', { html: restore(inlineMarkup(esc(node.text)), store, options).replace(/\n/g, '<br>') })
          );
          break;
        case 'quote':
          container.appendChild(
            h('blockquote.md__quote', {
              html: node.lines
                .map(function (l) {
                  return restore(inlineMarkup(esc(l)), store, options);
                })
                .join('<br>'),
            })
          );
          break;
        case 'table':
          container.appendChild(renderTable(node, store, options));
          break;
        case 'list':
          container.appendChild(renderList(node, store, options));
          break;
        default:
          break;
      }
    });
    return container;
  }

  function renderTableCell(text, store, options, tag) {
    return h(tag, { html: restore(inlineMarkup(esc(text)), store, options) });
  }

  function renderTable(node, store, options) {
    var head = h('thead', null, h('tr', null, node.head.map(function (cell) {
      return renderTableCell(cell, store, options, 'th');
    })));
    var body = h('tbody', null, node.rows.map(function (row) {
      var cells = [];
      for (var c = 0; c < node.head.length; c++) {
        cells.push(renderTableCell(row[c] == null ? '' : row[c], store, options, 'td'));
      }
      return h('tr', null, cells);
    }));
    return h('div.md__tablewrap', null, h('table.md__table', null, head, body));
  }

  function renderList(node, store, options) {
    var root = h(node.items[0].ordered ? 'ol' : 'ul', { class: 'md__list' });
    var stack = [{ el: root, depth: node.items[0].depth }];

    node.items.forEach(function (item) {
      while (stack.length > 1 && item.depth < stack[stack.length - 1].depth) stack.pop();
      if (item.depth > stack[stack.length - 1].depth) {
        var nested = h(item.ordered ? 'ol' : 'ul', { class: 'md__list' });
        var lastLi = stack[stack.length - 1].el.lastElementChild;
        (lastLi || stack[stack.length - 1].el).appendChild(nested);
        stack.push({ el: nested, depth: item.depth });
      }
      var text = item.text;
      var task = /^\[([ xX])\]\s+(.*)$/.exec(text);
      var li = h('li.md__li', { class: task ? 'md__li--task' : '' });
      if (task) {
        li.classList.add(task[1].toLowerCase() === 'x' ? 'is-done' : 'is-todo');
        li.appendChild(h('span.md__check'));
        text = task[2];
      }
      li.appendChild(
        h('span', {
          html: restore(inlineMarkup(esc(text)), store, options).replace(/\n/g, '<br>'),
        })
      );
      stack[stack.length - 1].el.appendChild(li);
    });

    return root;
  }

  /**
   * 把 Markdown 源码渲染成一个 .md 元素。
   * @param {string} src
   * @param {object} [options] {macros}
   * @returns {HTMLElement}
   */
  /**
   * 渲染结果缓存。
   *
   * 同一段题面在一道题的多次渲染前会反复出现（错题本切换题目、练习重渲染、
   * 大题的小问与参考答析），而 Markdown 解析 + KaTeX 渲染是这里最贵的一步。
   * 缓存以「源码字符串 → 渲染后的 innerHTML」为单位，命中时直接把 HTML 交给
   * 浏览器解析，比重新跑一遍解析器快一到两个数量级。
   */
  var htmlCache = {};
  var cacheKeys = [];
  var CACHE_LIMIT = 400;

  function cachedHtml(source) {
    if (Object.prototype.hasOwnProperty.call(htmlCache, source)) return htmlCache[source];
    return null;
  }

  function putCache(source, html) {
    if (typeof html !== 'string') return;
    htmlCache[source] = html;
    cacheKeys.push(source);
    while (cacheKeys.length > CACHE_LIMIT) {
      var oldest = cacheKeys.shift();
      delete htmlCache[oldest];
    }
  }

  function render(src, options) {
    var source = String(src == null ? '' : src);
    if (!source.trim()) return h('div.md');

    // 带自定义宏时不缓存，避免宏变化后拿到旧结果
    var cacheable = !options || !options.macros;

    if (cacheable) {
      var hit = cachedHtml(source);
      if (hit !== null) {
        var cached = h('div.md');
        cached.innerHTML = hit;
        return cached;
      }
    }

    var store = [];
    var text = extractFences(source, store);
    text = extractMath(text, store);
    text = extractInlineCode(text, store);

    var nodes = parseBlocks(text, store, options);
    var el = renderNodes(nodes, store, options);

    if (cacheable && el.children.length) putCache(source, el.innerHTML);
    return el;
  }

  /**
   * 单行内联渲染（用于选项文字等，不产生块级元素）。
   */
  function renderInlineToString(src, options) {
    var source = String(src == null ? '' : src);
    var cacheable = !options || !options.macros;
    if (cacheable) {
      var hit = cachedHtml('i\u0001' + source);
      if (hit !== null) return hit;
    }
    var store = [];
    var text = extractInlineCode(source, store);
    text = extractMath(text, store);
    var html = restore(inlineMarkup(esc(text)), store, options);
    if (cacheable) putCache('i\u0001' + source, html);
    return html;
  }

  function renderToString(src, options) {
    return render(src, options).outerHTML;
  }

  function renderInto(target, src, options) {
    ui.clear(target);
    target.appendChild(render(src, options));
    return target;
  }

  /** 去掉 Markdown 标记与 LaTeX 定界符，得到纯文本摘要（用于列表预览） */
  function plain(src, max) {
    var text = String(src == null ? '' : src);

    // 代码块整段丢弃：题面里的代码对摘要没有帮助
    text = text.replace(/```[\s\S]*?```/g, ' ').replace(/~~~[\s\S]*?~~~/g, ' ');
    // 数学公式只保留内容，去掉 $ 定界符
    text = text.replace(/\$\$([\s\S]*?)\$\$/g, ' $1 ');
    text = text.replace(/\$([^$\n]+)\$/g, ' $1 ');
    text = text.replace(/\\\(([\s\S]*?)\\\)/g, ' $1 ');
    text = text.replace(/\\\[([\s\S]*?)\\\]/g, ' $1 ');
    // 图片丢弃；链接只留可见文字
    text = text.replace(/!\[[^\]]*\]\([^)]*\)/g, ' ');
    text = text.replace(/\[([^\]]*)\]\([^)]*\)/g, '$1');
    // 表格的分隔行（|---|---|）没有信息量
    text = text.replace(/^[^\n]*\|[\s:|-]{3,}[^\n]*$/gm, ' ');
    // 行首标记
    text = text.replace(/^ {0,3}#{1,6}\s+/gm, '');
    text = text.replace(/^ {0,3}>\s?/gm, '');
    text = text.replace(/^ {0,3}[-*+]\s+/gm, '');
    text = text.replace(/^ {0,3}\d+[.)]\s+/gm, '');
    // 行内标记
    text = text.replace(/(\*\*|__)([\s\S]*?)\1/g, '$2');
    text = text.replace(/`([^`]*)`/g, '$1');
    // 表格竖线与所有换行/缩进都压成单空格，摘要变成一段连续文字
    text = text.replace(/\|/g, ' ').replace(/\s+/g, ' ').trim();

    return ui.truncate(text, max || 160);
  }

  QF.md = {
    render: render,
    renderToString: renderToString,
    renderInto: renderInto,
    renderInline: renderInlineToString,
    plain: plain,
  };
})();
