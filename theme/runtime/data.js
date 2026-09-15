/* ===========================================================================
 * data.js —— 题库索引与筛选
 *
 * 题库在构建期被内联成 window.__QB__，这里把它整理成便于查询的结构。
 * 全流程零 fetch，因此 file:// 双击打开也能正常工作。
 * ========================================================================= */
(function () {
  'use strict';

  var QF = (window.QF = window.QF || {});

  /* ---------------------------------------------------------------- 容器
   * 这些容器**一律就地清空/填充，绝不整体替换**：
   * app.js / qview.js / wrongbook.js 在模块加载时就拿到了数组与对象的引用，
   * 一旦 install() 之后换成新对象，它们手里的旧引用会静默变成空数据 ——
   * 这类问题不会报错，只会让界面莫名其妙地空掉。
   */
  var meta = {};
  var questions = [];
  var byId = {};
  var topics = [];
  var topicMap = {};
  var subjects = [];
  var groups = [];
  var groupMap = {};
  var subtreeCache = {};
  var statsCache = { total: 0, byTopic: {}, byType: {}, byDifficulty: {} };
  var installed = false;

  function clearObject(target) {
    Object.keys(target).forEach(function (key) {
      delete target[key];
    });
  }

  /**
   * 装载题库。离线模式在模块末尾用 window.__QB__ 自动调用一次；
   * 在线模式由 boot.js 拿到 GET /api/bank 的结果后调用。
   */
  function install(bank) {
    var source = bank || {};

    clearObject(meta);
    Object.assign(meta, source.meta || {});

    questions.length = 0;
    (source.questions || []).forEach(function (q) {
      questions.push(q);
    });

    clearObject(byId);
    questions.forEach(function (q) {
      byId[q.id] = q;
    });

    topics.length = 0;
    clearObject(topicMap);
    (meta.topics || []).forEach(function (t) {
      topics.push(t);
      topicMap[t.key] = t;
    });

    subjects.length = 0;
    topics.forEach(function (t) {
      if (t.depth === 1) subjects.push(t);
    });

    groups.length = 0;
    clearObject(groupMap);
    (meta.groups || []).forEach(function (g) {
      groups.push(g);
      groupMap[g.key] = g;
    });

    // 主题树变了，缓存的子孙集合必须作废
    clearObject(subtreeCache);

    installed = true;
    recomputeBankStats();
    return api;
  }

  /* ------------------------------------------------------------ 主题树 */
  // 三级：学科(depth 1) → 单元(depth 2) → 知识点(depth 3)。
  // 题目挂任意一层；按上层筛选要包含其全部子孙，所以每个节点都带 descendants
  // （构建期算好），这里缓存成数组避免每次筛选重新拼接。

  /** 选中某节点时真正命中的 topic 集合：自己 + 全部子孙 */
  function subtree(key) {
    if (!subtreeCache[key]) {
      var node = topicMap[key];
      subtreeCache[key] = [key].concat(node && node.descendants ? node.descendants : []);
    }
    return subtreeCache[key];
  }

  /**
   * 把选中的节点摊成「实际命中的 topic 集合」。
   *
   * 只取最深的那些：祖先节点保留在选择里是为了让筛选条显示完整的路径
   * （点了主题再点单元，主题那颗 chip 仍然亮着），但匹配时它不该参与 ——
   * 否则就变成两段范围取并集，点单元等于没点。
   */
  function expandTopics(keys) {
    var list = (keys || []).slice();
    var deepest = list.filter(function (key) {
      return !list.some(function (other) {
        return other !== key && subtree(key).indexOf(other) !== -1;
      });
    });
    var set = {};
    deepest.forEach(function (key) {
      subtree(key).forEach(function (k) {
        set[k] = true;
      });
    });
    return set;
  }

  /** 取某节点的直接子节点；传空则返回全部学科 */
  function childrenOf(key) {
    var list = key
      ? (topicMap[key] ? topicMap[key].children : [])
      : subjects.map(function (t) {
          return t.key;
        });
    return (list || [])
      .map(function (k) {
        return topicMap[k];
      })
      .filter(Boolean);
  }

  function topicPath(key) {
    var node = topicMap[key];
    return node && node.path ? node.path.slice() : [key];
  }

  function topicPathNames(key) {
    var node = topicMap[key];
    return node && node.pathNames ? node.pathNames.slice() : [key];
  }

  function topicDepth(key) {
    var node = topicMap[key];
    return node ? node.depth : 0;
  }

  /** 卡片上显示的分类：知识点优先，退化到该节点自身的名字 */
  function topicLabel(key) {
    var node = topicMap[key];
    return node ? node.name : key;
  }


  var DEFAULT_TYPE_LABELS = { single: '单选', multi: '多选', blank: '填空', short: '简答' };

  /* -------------------------------------------------------------- 查询 */

  function topicName(key) {
    return (topicMap[key] && topicMap[key].name) || key;
  }

  function topicColor(key) {
    return (topicMap[key] && topicMap[key].color) || '#2DD4BF';
  }

  function groupName(key) {
    return (groupMap[key] && groupMap[key].name) || key;
  }

  /**
   * 按条件筛选题目。
   * @param {object} filter
   *   topics: string[]        主题 key 白名单（空 = 不限）
   *   tags: string[]          tag 白名单（命中任一即可）
   *   types: string[]         题型白名单
   *   difficulty: number[]    难度白名单
   *   keyword: string         题面关键词（大小写不敏感，匹配题面/选项/标签/出处）
   *   ids: string[]           直接指定题目 id（优先级最高）
   */
  function filter(criteria) {
    var c = criteria || {};
    var pool = questions;

    if (c.ids && c.ids.length) {
      return c.ids
        .map(function (id) {
          return byId[id];
        })
        .filter(Boolean);
    }

    if (c.topics && c.topics.length) {
      // 选中「单元」不该漏掉它下面挂到知识点上的题，所以按子孙集合匹配
      var hitTopics = expandTopics(c.topics);
      pool = pool.filter(function (q) {
        return !!hitTopics[q.topic];
      });
    }
    if (c.types && c.types.length) {
      pool = pool.filter(function (q) {
        return c.types.indexOf(q.type) !== -1;
      });
    }
    if (c.difficulty && c.difficulty.length) {
      pool = pool.filter(function (q) {
        return c.difficulty.indexOf(q.difficulty) !== -1;
      });
    }
    if (c.keyword && c.keyword.trim()) {
      var needle = c.keyword.trim().toLowerCase();
      pool = pool.filter(function (q) {
        // 主题路径一起参与搜索，这样搜「迭代器」能覆盖该知识点下的题；
        // chapter / tags 已随主题树退出，不再拼进搜索文本（否则会拼进 "undefined"）
        var haystack = [q.id, q.stem, q.source, topicPathNames(q.topic).join(' ')]
          .concat((q.options || []).map(function (o) {
            return o.text;
          }))
          .join(' ')
          .toLowerCase();
        return haystack.indexOf(needle) !== -1;
      });
    }
    return pool.slice();
  }

  /* -------------------------------------------------------------- 统计 */

  function stats() {
    var byTopic = {};
    var byType = {};
    var byDifficulty = {};
    topics.forEach(function (t) {
      byTopic[t.key] = 0;
    });
    questions.forEach(function (q) {
      // 计数沿 path 累加到每级祖先：学科的题数 = 其下所有知识点之和
      topicPath(q.topic).forEach(function (key) {
        byTopic[key] = (byTopic[key] || 0) + 1;
      });
      byType[q.type] = (byType[q.type] || 0) + 1;
      byDifficulty[q.difficulty] = (byDifficulty[q.difficulty] || 0) + 1;
    });
    return { total: questions.length, byTopic: byTopic, byType: byType, byDifficulty: byDifficulty };
  }

  function byIds(ids) {
    return (ids || [])
      .map(function (id) {
        return byId[id];
      })
      .filter(Boolean);
  }

  /**
   * 题库统计：以构建期写入的 meta.stats 为准，并用本地重新统计的结果补齐
   * 缺失字段。这样即使构建脚本新增/遗漏了某个维度，前端也不会因为读到
   * undefined 而崩掉（曾经就因为缺 byDifficulty 导致筛选条渲染报错）。
   *
   * 结果缓存在 statsCache：筛选条渲染时会按节点读它，不能每次重算。
   */
  function recomputeBankStats() {
    var local = stats();
    var provided = meta.stats || {};
    statsCache.total = typeof provided.total === 'number' ? provided.total : local.total;
    statsCache.byTopic = provided.byTopic || local.byTopic;
    statsCache.byType = provided.byType || local.byType;
    statsCache.byDifficulty = provided.byDifficulty || local.byDifficulty;
    return statsCache;
  }

  var api = (QF.data = {
    meta: meta,
    questions: questions,
    byId: byId,
    topics: topics,
    topicMap: topicMap,
    groups: groups,
    // 以下都是 install() 之后才确定的派生值，必须用 getter：
    // 一次性取值会在 install 之后停留在旧数据（甚至空数据）上
    get typeLabels() {
      return meta.typeLabels || DEFAULT_TYPE_LABELS;
    },
    get generatedAt() {
      return meta.generatedAt || '';
    },
    get bankStats() {
      return statsCache;
    },
    get installed() {
      return installed;
    },
    install: install,
    get: function (id) {
      return byId[id] || null;
    },
    byIds: byIds,
    topicName: topicName,
    topicColor: topicColor,
    groupName: groupName,
    filter: filter,
    stats: stats,
    // 主题树
    subjects: subjects,
    subtree: subtree,
    expandTopics: expandTopics,
    childrenOf: childrenOf,
    topicPath: topicPath,
    topicPathNames: topicPathNames,
    topicDepth: topicDepth,
    topicLabel: topicLabel,
    typeLabel: function (type) {
      return (meta.typeLabels || DEFAULT_TYPE_LABELS)[type] || type;
    },
  });

  // 离线单文件：题库已经内联在页面里，模块加载即完成装载。
  // 在线模式没有 window.__QB__，等 boot.js 拿到 /api/bank 之后再 install。
  if (window.__QB__) install(window.__QB__);
})();
