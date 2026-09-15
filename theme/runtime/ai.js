/* ===========================================================================
 * ai.js —— 简答题 AI 批改（浏览器直连 OpenAI 兼容接口）
 *
 * 设计取舍：
 *   * 纯离线页面无法调用模型，AI 批改必然需要联网，因此这块是"可选增强"，
 *     失败时一律降级为「显示参考答案 + 自评打分」，不影响刷题主流程。
 *   * 密钥只存在本机 localStorage，绝不写进构建产物 HTML。
 *   * 要求模型返回结构化 JSON；解析做了三层容错（直接 parse → 提取花括号块
 *     → 退化成正则抽取分数与文本）。
 *   * 部分供应商不支持 response_format=json_object，遇到 400 会自动去掉该
 *     参数重试一次。
 * ========================================================================= */
(function () {
  'use strict';

  var QF = (window.QF = window.QF || {});

  var DEFAULT_AI = {
    enabled: false,
    baseUrl: 'https://api.openai.com/v1',
    model: 'gpt-4o-mini',
    apiKey: '',
    timeoutMs: 60000,
    temperature: 0.2,
    jsonMode: true,
    fallbackSelfRate: true,
    strictRubric: true,
  };

  function cfg(override) {
    var base = QF.store ? QF.store.settings().ai : DEFAULT_AI;
    return Object.assign({}, DEFAULT_AI, base || {}, override || {});
  }

  /* ------------------------------------------------------------ 地址 */

  function endpoint(url) {
    var base = String(url || '').trim().replace(/\/+$/, '');
    if (!base) throw new Error('未配置接口地址');
    if (/\/chat\/completions$/.test(base)) return base;
    if (/\/v\d+(\.\d+)?$/.test(base)) return base + '/chat/completions';
    return base + '/v1/chat/completions';
  }

  /* ------------------------------------------------------------ 提示词 */

  function buildMessages(question, userAnswer, options) {
    var opts = options || {};
    var rubric = (question.rubric || []).filter(Boolean);
    var rubricBlock = rubric.length
      ? rubric
          .map(function (item, index) {
            return index + 1 + '. ' + item;
          })
          .join('\n')
      : '（本题未提供评分要点，请依据参考答案自行拟定要点后再评分）';

    var system = [
      '你是一位严谨的计算机科学助教，负责批改学生的简答题。',
      '评分必须依据题目给出的「评分要点」，不要因为表述风格扣分，只判断知识点是否命中。',
      '只输出一个 JSON 对象，不要输出任何解释文字，不要用 Markdown 代码块包裹。',
      '',
      'JSON 字段：',
      '{',
      '  "score": 0 到 10 的数字，按命中要点的比例给分,',
      '  "verdict": "correct" | "partial" | "wrong",',
      '  "matched": ["学生已准确命中的评分要点"],',
      '  "missing": ["遗漏或表述有误的评分要点"],',
      '  "errors": ["学生答案中的事实性错误，没有则给空数组"],',
      '  "feedback": "面向学生的中文讲评，2 到 5 句，指出问题并给出改进方向",',
      '  "improved": "一个更规范的参考答案，可用 Markdown 与 LaTeX"',
      '}',
      '',
      '判定规则：',
      '- 全部要点命中且无事实错误 → "correct"',
      '- 命中部分要点 → "partial"',
      '- 几乎未命中，或存在关键事实错误 → "wrong"',
      '- 学生答案为空或答非所问 → score 0，"wrong"',
    ].join('\n');

    var parts = [];
    parts.push('【题目信息】');
    parts.push('主题：' + (QF.data ? QF.data.topicName(question.topic) : question.topic));
    if (question.chapter) parts.push('章节：' + question.chapter);
    if (question.difficulty) parts.push('难度：' + question.difficulty + '/5');
    parts.push('');
    parts.push('【题干】');
    parts.push(question.stem || '');
    parts.push('');
    parts.push('【评分要点】');
    parts.push(rubricBlock);
    parts.push('');
    parts.push('【参考答案】');
    parts.push(question.reference || '（未提供）');
    parts.push('');
    parts.push('【学生作答】');
    parts.push(String(userAnswer == null ? '' : userAnswer).trim() || '（学生未作答）');
    parts.push('');
    parts.push('请严格按上述 JSON 结构输出批改结果。');

    return [
      { role: 'system', content: system },
      { role: 'user', content: parts.join('\n') },
    ];
  }

  /* ------------------------------------------------------------ 解析 */

  function stripFences(text) {
    return String(text || '')
      .replace(/^\s*```(?:json|JSON)?\s*/m, '')
      .replace(/\s*```\s*$/m, '')
      .trim();
  }

  function extractJson(text) {
    var cleaned = stripFences(text);
    try {
      return JSON.parse(cleaned);
    } catch (err) {
      /* 继续尝试 */
    }
    var start = cleaned.indexOf('{');
    if (start === -1) return null;
    var depth = 0;
    var inString = false;
    var escaped = false;
    for (var i = start; i < cleaned.length; i++) {
      var ch = cleaned[i];
      if (inString) {
        if (escaped) escaped = false;
        else if (ch === '\\') escaped = true;
        else if (ch === '"') inString = false;
        continue;
      }
      if (ch === '"') inString = true;
      else if (ch === '{') depth++;
      else if (ch === '}') {
        depth--;
        if (!depth) {
          try {
            return JSON.parse(cleaned.slice(start, i + 1));
          } catch (err2) {
            return null;
          }
        }
      }
    }
    return null;
  }

  function toArray(value) {
    if (!value) return [];
    if (Array.isArray(value)) {
      return value
        .map(function (item) {
          return typeof item === 'string' ? item.trim() : JSON.stringify(item);
        })
        .filter(Boolean);
    }
    if (typeof value === 'string') {
      return value
        .split(/\s*[;；\n]\s*/)
        .map(function (s) {
          return s.trim();
        })
        .filter(Boolean);
    }
    return [String(value)];
  }

  function normalize(raw, question) {
    var data = raw && typeof raw === 'object' ? raw : {};
    var score = typeof data.score === 'number' ? data.score : parseFloat(data.score);
    if (!isFinite(score)) {
      var match = /(\d+(?:\.\d+)?)\s*(?:\/\s*10)?/.exec(String(data.feedback || data.raw || ''));
      score = match ? parseFloat(match[1]) : 0;
    }
    score = Math.max(0, Math.min(10, score));

    var verdict = String(data.verdict || '').toLowerCase();
    if (['correct', 'partial', 'wrong'].indexOf(verdict) === -1) {
      verdict = score >= 8.5 ? 'correct' : score > 0 ? 'partial' : 'wrong';
    }

    return {
      score: Math.round(score * 10) / 10,
      max: 10,
      verdict: verdict,
      matched: toArray(data.matched),
      missing: toArray(data.missing),
      errors: toArray(data.errors),
      feedback: String(data.feedback || data.comment || '').trim() || '（模型未给出讲评）',
      improved: String(data.improved || '').trim(),
      raw: raw,
    };
  }

  function parseResult(text, question) {
    var json = extractJson(text);
    return normalize(json || { feedback: stripFences(text), raw: null }, question);
  }

  /* ------------------------------------------------------------ 请求 */

  /**
   * 把供应商的响应规范化成 `{content, model, usage}`。
   *
   * 离线（浏览器直连）与在线（服务端代理）两条路径共用这一份 ——
   * 服务端刻意原样返回供应商响应，就是为了让这里不必分叉。
   */
  function normalizeResponse(raw, conf) {
    var payload = raw;
    if (typeof raw === 'string') {
      try {
        payload = JSON.parse(raw);
      } catch (err) {
        throw new Error('接口返回的不是合法 JSON：' + String(raw).slice(0, 200));
      }
    }
    payload = payload || {};

    var choice = (payload.choices || [])[0] || {};
    var content = (choice.message || {}).content;
    if (Array.isArray(content)) {
      content = content
        .map(function (part) {
          return typeof part === 'string' ? part : part.text || '';
        })
        .join('');
    }
    if (!content) {
      throw new Error('模型没有返回内容' + (payload.error ? '：' + payload.error.message : ''));
    }
    return { content: content, usage: payload.usage || null, model: payload.model || conf.model };
  }

  /**
   * 服务端代理路径（在线模式）。
   *
   * 密钥只存在于服务端环境变量，浏览器侧既拿不到也不需要它；
   * 顺带绕开了「供应商不允许跨域」这个在浏览器里根本无解的问题。
   */
  function requestViaServer(body, conf) {
    return QF.api
      .post('/ai/grade', body, { timeoutMs: conf.timeoutMs || 60000 })
      .then(function (data) {
        return normalizeResponse(data, conf);
      })
      .catch(function (err) {
        var status = err && err.status;
        // 服务端的 503 文案已经写清了「去哪儿填密钥」，直接透传比前端另写一句好：
        // 前端猜不到是没填密钥、还是开关没打开。
        if (status === 503) {
          throw new Error((err && err.message) || '尚未启用 AI 批改，请到设置里填入你自己的 API 密钥。');
        }
        if (status === 429) {
          throw new Error(err.message || '今日 AI 调用已达上限。');
        }
        if (status === 504) {
          throw new Error('AI 服务响应超时，稍后重试，或改用「自己判断」。');
        }
        if (status === 401) {
          throw new Error('登录状态已失效，请重新登录后再用 AI 批改。');
        }
        if (status === 404) {
          throw new Error('接口地址或模型名不对：请检查设置里的「接口地址」和「模型名」。');
        }
        throw err;
      });
  }

  function request(body, options) {
    var opts = options || {};
    var conf = cfg(opts);

    // 在线模式一律走服务端：密钥不下发到浏览器
    if (QF.online && QF.api) return requestViaServer(body, conf);

    var url = endpoint(conf.baseUrl || opts.baseUrl);

    var headers = { 'Content-Type': 'application/json' };
    var key = (opts.apiKey != null ? opts.apiKey : conf.apiKey || '').trim();
    if (key) headers.Authorization = 'Bearer ' + key;

    var controller = typeof AbortController === 'function' ? new AbortController() : null;
    var timer = controller
      ? setTimeout(function () {
          controller.abort();
        }, conf.timeoutMs || 60000)
      : 0;

    var init = {
      method: 'POST',
      headers: headers,
      body: JSON.stringify(body),
    };
    if (controller) init.signal = controller.signal;

    return fetch(url, init)
      .then(function (response) {
        return response.text().then(function (text) {
          if (!response.ok) {
            var detail = '';
            try {
              var errJson = JSON.parse(text);
              detail = (errJson.error && (errJson.error.message || errJson.error.type)) || text.slice(0, 300);
            } catch (err) {
              detail = text.slice(0, 300);
            }
            var error = new Error('HTTP ' + response.status + '：' + detail);
            error.status = response.status;
            throw error;
          }
          return normalizeResponse(text, conf);
        });
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') {
          throw new Error('请求超时（' + Math.round((conf.timeoutMs || 60000) / 1000) + ' 秒），可在设置里调大超时时间');
        }
        if (err instanceof TypeError || /Failed to fetch|NetworkError|load failed/i.test(String(err && err.message))) {
          throw new Error(
            '无法连接到接口。常见原因：接口地址写错、本机网络不通、或该服务不允许浏览器跨域调用（CORS）。'
          );
        }
        throw err;
      })
      .finally(function () {
        if (timer) clearTimeout(timer);
      });
  }

  /* ------------------------------------------------------------ 对外 */

  function buildBody(messages, conf, withJsonMode) {
    var body = {
      model: conf.model,
      messages: messages,
      temperature: typeof conf.temperature === 'number' ? conf.temperature : 0.2,
      stream: false,
    };
    if (withJsonMode) body.response_format = { type: 'json_object' };
    return body;
  }

  /**
   * 批改一道简答题。
   * @returns {Promise<object>} normalize 后的结果
   */
  function grade(question, userAnswer, options) {
    var conf = cfg(options);
    if (!conf.model) return Promise.reject(new Error('未配置模型名'));
    var messages = buildMessages(question, userAnswer, options);
    var useJson = conf.jsonMode !== false;

    return request(buildBody(messages, conf, useJson), options).catch(function (err) {
      // 供应商不支持 response_format 时去掉重试一次
      if (useJson && err && err.status === 400) {
        return request(buildBody(messages, conf, false), options);
      }
      throw err;
    }).then(function (response) {
      var result = parseResult(response.content, question);
      result.usage = response.usage;
      result.model = response.model;
      return result;
    });
  }

  /** 连通性测试：发一条极短的请求，返回耗时与模型回显 */
  function ping(options) {
    var conf = cfg(options);

    // 在线模式：服务端的 /api/ai/ping 直接返回同一组字段
    // （ok / latencyMs / model / sample / message / url），设置面板不必分叉
    if (QF.online && QF.api) {
      return QF.api.get('/ai/ping').catch(function (err) {
        return { ok: false, latencyMs: 0, message: (err && err.message) || '连通性测试失败', serverSide: true };
      });
    }

    var messages = [{ role: 'user', content: '只回复两个字：可用' }];
    var started = Date.now();
    return request(
      { model: conf.model, messages: messages, temperature: 0, max_tokens: 16, stream: false },
      options
    )
      .then(function (response) {
        return { ok: true, latencyMs: Date.now() - started, model: response.model, sample: response.content.trim(), url: endpoint(conf.baseUrl) };
      })
      .catch(function (err) {
        return { ok: false, latencyMs: Date.now() - started, message: err.message, url: endpoint(conf.baseUrl) };
      });
  }

  /** 把 AI 结果映射成判定状态，便于 store 统一落盘 */
  function verdictToStatus(verdict) {
    if (verdict === 'correct') return 'ai_correct';
    if (verdict === 'partial') return 'ai_partial';
    return 'ai_wrong';
  }

  /** AI 结果 → engine 风格的结果对象（用于复用状态条 UI 与统一落盘） */
  function toResult(aiResult, question) {
    var score = aiResult.max ? aiResult.score / aiResult.max : 0;
    var status = aiResult.verdict === 'correct' ? 'correct' : aiResult.verdict === 'partial' ? 'partial' : 'wrong';
    return {
      status: status,
      fromAI: true,
      correct: status === 'correct',
      score: score,
      blanks: [],
      expected: QF.engine ? QF.engine.expectedText(question) : '',
      expectedHtml: QF.engine ? QF.engine.expectedHtml(question) : '',
      ai: aiResult,
    };
  }

  QF.ai = {
    defaults: DEFAULT_AI,
    config: cfg,
    endpoint: endpoint,
    buildMessages: buildMessages,
    grade: grade,
    ping: ping,
    parseResult: parseResult,
    toResult: toResult,
    verdictToStatus: verdictToStatus,
  };
})();
