/* ===========================================================================
 * engine.js —— 判分引擎
 *
 * 输入：题目对象 + 用户作答；输出统一的结果结构，供 UI 与 store 消费。
 *
 *   { status, correct, score, points, max, blanks, expected, expectedHtml }
 *
 *   status ∈ 'correct' | 'partial' | 'wrong' | 'empty' | 'ungraded'
 *   score  ∈ 0..1（简答题为 null，交给 AI 批改或自评）
 * ========================================================================= */
(function () {
  'use strict';

  var QF = (window.QF = window.QF || {});

  /* ------------------------------------------------------------ 归一化 */

  var WRAP_QUOTES = /^[`"'“”‘’《》]+|[`"'“”‘’《》]+$/g;

  var TRAILING_PUNCT = /[；;。，,]+$/;

  function normalizeBlank(value, caseSensitive) {
    var s = String(value == null ? '' : value)
      .replace(/\u00a0/g, ' ')
      .replace(/[\r\n\t]+/g, ' ')
      .replace(/\s{2,}/g, ' ')
      .trim();
    // 反复剥离包裹的引号与结尾标点到稳定：`std::vector`; → std::vector
    for (var i = 0; i < 4; i++) {
      var before = s;
      s = s.replace(WRAP_QUOTES, '').trim();
      s = s.replace(TRAILING_PUNCT, '').trim();
      if (s === before) break;
    }
    if (!caseSensitive) s = s.toLowerCase();
    return s;
  }

  function isBlankAnswerCaseSensitive(question) {
    var flag = question.meta && question.meta.caseSensitive;
    return flag === true || flag === 'true';
  }

  /* ------------------------------------------------------- 题型判分 */

  function gradeSingle(question, response) {
    var picked = typeof response === 'string' ? response.trim().toUpperCase() : '';
    var want = String(question.answer || '').toUpperCase();
    var correct = !!picked && picked === want;
    return {
      status: picked ? (correct ? 'correct' : 'wrong') : 'empty',
      correct: correct,
      score: picked ? (correct ? 1 : 0) : null,
      blanks: [],
      expected: want,
    };
  }

  function gradeMulti(question, response) {
    var picked = (Array.isArray(response) ? response : []).map(function (v) {
      return String(v).toUpperCase();
    });
    var want = (question.answer || []).map(function (v) {
      return String(v).toUpperCase();
    });
    if (!picked.length) {
      return { status: 'empty', correct: false, score: null, blanks: [], expected: want.join(',') };
    }
    var pickedSet = {};
    picked.forEach(function (k) {
      pickedSet[k] = true;
    });
    var wantSet = {};
    want.forEach(function (k) {
      wantSet[k] = true;
    });
    var missing = want.filter(function (k) {
      return !pickedSet[k];
    });
    var extra = picked.filter(function (k) {
      return !wantSet[k];
    });

    if (!missing.length && !extra.length) {
      return { status: 'correct', correct: true, score: 1, blanks: [], expected: want.join(',') };
    }
    if (extra.length) {
      // 选到错误项：直接判错（多选题多选不倒扣，但结果记为错）
      return {
        status: 'wrong',
        correct: false,
        score: 0,
        blanks: [],
        expected: want.join(','),
        detail: { missing: missing, extra: extra },
      };
    }
    // 只选了部分正确项：部分得分
    return {
      status: 'partial',
      correct: false,
      score: want.length ? picked.length / want.length : 0,
      blanks: [],
      expected: want.join(','),
      detail: { missing: missing, extra: extra },
    };
  }

  function matchOne(value, accept, regexes, caseSensitive) {
    var raw = String(value == null ? '' : value).trim();
    var normalized = normalizeBlank(value, caseSensitive);
    for (var i = 0; i < accept.length; i++) {
      if (normalizeBlank(accept[i], caseSensitive) === normalized) return true;
    }
    for (var j = 0; j < regexes.length; j++) {
      try {
        var re = new RegExp(regexes[j], caseSensitive ? '' : 'i');
        if (re.test(raw) || re.test(normalized)) return true;
      } catch (err) {
        /* 非法正则忽略，不阻塞判分 */
      }
    }
    return false;
  }

  function gradeBlank(question, response) {
    var answers = question.answer || [];
    var caseSensitive = isBlankAnswerCaseSensitive(question);
    var given = Array.isArray(response) ? response : [response];
    var blanks = [];
    var okCount = 0;

    for (var i = 0; i < answers.length; i++) {
      var spec = answers[i] || { accept: [], regex: [] };
      var value = given[i] == null ? '' : given[i];
      var filled = String(value).trim().length > 0;
      var ok = filled && matchOne(value, spec.accept || [], spec.regex || [], caseSensitive);
      if (ok) okCount++;
      blanks.push({
        index: i,
        ok: ok,
        filled: filled,
        got: String(value),
        want: (spec.accept || []).slice(),
        regex: (spec.regex || []).slice(),
      });
    }

    var total = answers.length || 1;
    var allOk = okCount === total;
    var anyFilled = blanks.some(function (b) {
      return b.filled;
    });
    return {
      status: !anyFilled ? 'empty' : allOk ? 'correct' : okCount > 0 ? 'partial' : 'wrong',
      correct: allOk,
      score: anyFilled ? okCount / total : null,
      blanks: blanks,
      expected: blanks
        .map(function (b) {
          return b.want[0] || (b.regex[0] ? '/' + b.regex[0] + '/' : '');
        })
        .join(' ; '),
    };
  }

  /**
   * 判分主入口。
   * @param {object} question 题库条目
   * @param {*} response 单选=字符串；多选=数组；填空=数组；简答=字符串
   * @returns {object} 结果对象
   */
  function grade(question, response) {
    if (!question) {
      return { status: 'empty', correct: false, score: null, blanks: [], expected: '' };
    }
    var result;
    switch (question.type) {
      case 'single':
        result = gradeSingle(question, response);
        break;
      case 'multi':
        result = gradeMulti(question, response);
        break;
      case 'blank':
        result = gradeBlank(question, response);
        break;
      case 'short':
      case 'problem':
        // 简答 / 大题不做自动判分，但仍要区分「没作答」和「待批改」，
        // 否则成绩报告里未作答的题会被算成待自评、徽章也显示错
        result = {
          status: isResponseEmpty(question, response) ? 'empty' : 'ungraded',
          correct: false,
          score: null,
          blanks: [],
          expected: '',
          requiresAI: true,
        };
        break;
      default:
        result = { status: 'empty', correct: false, score: null, blanks: [], expected: '' };
    }
    result.expectedHtml = expectedHtml(question);
    return result;
  }

  /** 人可读的「正确答案」文本 */
  function expectedText(question) {
    switch (question.type) {
      case 'single':
        return String(question.answer || '');
      case 'multi':
        return (question.answer || []).join('、');
      case 'blank':
        return (question.answer || [])
          .map(function (spec, index) {
            var parts = (spec.accept || []).slice();
            (spec.regex || []).forEach(function (re) {
              parts.push('匹配 /' + re + '/');
            });
            return (question.answer.length > 1 ? '第 ' + (index + 1) + ' 空：' : '') + parts.join(' 或 ');
          })
          .join('；');
      case 'short':
        return '见参考答案';
      default:
        return '';
    }
  }

  function expectedHtml(question) {
    if (!QF.md) return QF.ui ? QF.ui.esc(expectedText(question)) : expectedText(question);
    return QF.md.renderInline(expectedText(question));
  }

  /** 判断作答是否为空（用于提交前校验与「未作答」统计） */
  function isResponseEmpty(question, response) {
    if (response == null) return true;
    if (question.type === 'multi') return !response.length;
    if (question.type === 'blank') {
      return !(Array.isArray(response) ? response : [response]).some(function (v) {
        return String(v == null ? '' : v).trim();
      });
    }
    if (question.type === 'problem') {
      var keys = Object.keys(response || {});
      if (!keys.length) return true;
      return !keys.some(function (k) {
        return String(response[k] == null ? '' : response[k]).trim();
      });
    }
    return !String(response).trim();
  }

  /** 大题：统计已作答的小问数 */
  function answeredParts(question, response) {
    var parts = question.parts || [];
    return parts.filter(function (part) {
      var value = response && response[part.index];
      return String(value == null ? '' : value).trim().length > 0;
    }).length;
  }

  /**
   * 大题：把小问的批改结果汇总成整题结果。
   * @param {object} question
   * @param {object} partResults {partIndex: {score, max, verdict, ...}}
   * @param {object} response 各小问作答
   */
  function aggregateParts(question, partResults, response) {
    var parts = question.parts || [];
    var scored = parts.filter(function (part) {
      return partResults && partResults[part.index];
    });
    if (!scored.length) {
      return {
        status: isResponseEmpty(question, response) ? 'empty' : 'ungraded',
        correct: false,
        score: null,
        blanks: [],
        expected: '见各小问参考答案',
        expectedHtml: '',
        fromAI: false,
        parts: partResults || {},
      };
    }
    var total = 0;
    var max = 0;
    scored.forEach(function (part) {
      var r = partResults[part.index];
      total += Number(r.score) || 0;
      max += Number(r.max) || 10;
    });
    var ratio = max ? total / max : 0;
    var verdict = ratio >= 0.85 ? 'correct' : ratio > 0.4 ? 'partial' : 'wrong';
    return {
      status: verdict,
      correct: verdict === 'correct',
      score: ratio,
      blanks: [],
      expected: '见各小问参考答案',
      expectedHtml: '',
      fromAI: true,
      ai: {
        score: Math.round(total * 10) / 10,
        max: max,
        verdict: verdict,
        matched: [],
        missing: [],
        errors: [],
        feedback:
          '大题整体得分 ' + (Math.round(total * 10) / 10) + ' / ' + max +
          '（已批改 ' + scored.length + ' / ' + parts.length + ' 个小问）',
      },
      parts: partResults,
    };
  }

  /** 判定结果转成 SM2 的四档评分（用于自动复习调度） */
  function toGrade(result) {
    if (!result) return 0;
    if (result.status === 'correct') return 2;
    if (result.status === 'partial') return 1;
    return 0;
  }

  /** 判定结果的颜色/文案元信息 */
  function describeStatus(status) {
    var map = {
      correct: { label: '回答正确', tone: 'ok', icon: 'check' },
      partial: { label: '部分正确', tone: 'warn', icon: 'warn' },
      wrong: { label: '回答错误', tone: 'bad', icon: 'close' },
      empty: { label: '未作答', tone: 'muted', icon: 'info' },
      ungraded: { label: '待批改', tone: 'amber', icon: 'robot' },
      ai_partial: { label: 'AI：部分正确', tone: 'warn', icon: 'robot' },
      ai_correct: { label: 'AI：正确', tone: 'ok', icon: 'robot' },
      ai_wrong: { label: 'AI：错误', tone: 'bad', icon: 'robot' },
    };
    return map[status] || map.empty;
  }

  QF.engine = {
    grade: grade,
    normalizeBlank: normalizeBlank,
    expectedText: expectedText,
    expectedHtml: expectedHtml,
    isResponseEmpty: isResponseEmpty,
    answeredParts: answeredParts,
    aggregateParts: aggregateParts,
    toGrade: toGrade,
    describeStatus: describeStatus,
  };
})();
