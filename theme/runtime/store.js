/* ===========================================================================
 * store.js —— 本地持久化
 *
 * 所有数据都放在 localStorage，键名统一加 quizforge.v1. 前缀。
 * file:// 下同目录的 quiz.html 与 wrongbook.html 共用同一个 origin，
 * 因此错题本能直接读到刷题进度；跨机器迁移靠 export/import JSON。
 *
 * localStorage 不可用（隐私模式 / 某些浏览器的 file:// 限制）时
 * 自动降级为内存存储，功能不中断但刷新后丢失，并在 UI 上给出提示。
 * ========================================================================= */
(function () {
  'use strict';

  var QF = (window.QF = window.QF || {});

  var PREFIX = 'quizforge.v1.';
  var K = {
    settings: PREFIX + 'settings',
    records: PREFIX + 'records',
    days: PREFIX + 'days',
    jump: PREFIX + 'jump',
    paper: PREFIX + 'paper',
    meta: PREFIX + 'meta',
    // 设置项的写入序号（记录自己的 rev 存在记录对象里）
    settingsRev: PREFIX + 'settingsRev',
    // 待上传的作答流水（增量）
    attempts: PREFIX + 'attempts',
    // 待上传的「重新设基线」（重置 / 导入）
    resets: PREFIX + 'resets',
  };

  /* ------------------------------------------------------- 存储后端 */

  var memory = {};
  var available = (function () {
    try {
      var probe = PREFIX + '__probe__';
      window.localStorage.setItem(probe, '1');
      window.localStorage.removeItem(probe);
      return true;
    } catch (err) {
      return false;
    }
  })();

  function rawGet(key) {
    if (!available) return Object.prototype.hasOwnProperty.call(memory, key) ? memory[key] : null;
    try {
      return window.localStorage.getItem(key);
    } catch (err) {
      return null;
    }
  }

  function rawSet(key, value) {
    memory[key] = value;
    if (!available) return;
    try {
      window.localStorage.setItem(key, value);
    } catch (err) {
      if (QF.ui) QF.ui.toast('本地存储写入失败（可能已超出配额）', 'error');
    }
  }

  function rawRemove(key) {
    delete memory[key];
    if (!available) return;
    try {
      window.localStorage.removeItem(key);
    } catch (err) {
      /* ignore */
    }
  }

  function readJSON(key, fallback) {
    var raw = rawGet(key);
    if (!raw) return fallback;
    try {
      var parsed = JSON.parse(raw);
      return parsed == null ? fallback : parsed;
    } catch (err) {
      return fallback;
    }
  }

  function writeJSON(key, value) {
    rawSet(key, JSON.stringify(value));
  }

  /* ---------------------------------------------------------- 设置 */

  var DEFAULT_SETTINGS = {
    theme: 'dark',
    fontScale: 1,
    maxWidth: 880,
    shuffle: false,
    // 首页主题排序：被置顶的 topic key 会排到最前，顺序即用户自定义顺序
    pinnedTopics: [],
    // 选题篮：浏览题库时攒下的题目 id，跨刷新保留
    basket: [],
    ai: {
      enabled: false,
      baseUrl: 'https://api.openai.com/v1',
      model: 'gpt-4o-mini',
      apiKey: '',
      timeoutMs: 60000,
      temperature: 0.2,
      jsonMode: true,
      fallbackSelfRate: true,
      strictRubric: true,
    },
  };

  function deepMerge(base, patch) {
    var out = Array.isArray(base) ? base.slice() : Object.assign({}, base);
    if (!patch || typeof patch !== 'object') return out;
    Object.keys(patch).forEach(function (key) {
      var value = patch[key];
      if (value && typeof value === 'object' && !Array.isArray(value) && base && typeof base[key] === 'object' && !Array.isArray(base[key])) {
        out[key] = deepMerge(base[key], value);
      } else if (value !== undefined) {
        out[key] = value;
      }
    });
    return out;
  }

  var settingsCache = null;

  function settings() {
    if (!settingsCache) {
      var stored = readJSON(K.settings, null) || {};
      settingsCache = deepMerge(DEFAULT_SETTINGS, stored);
    }
    return settingsCache;
  }

  function saveSettings(patch) {
    settingsCache = deepMerge(settings(), patch || {});
    writeJSON(K.settings, settingsCache);
    // 本机一改就**当场**抬 rev —— 这样"服务端那份比本机新吗"才有得比。
    // 不抬的话：在没加载 boot.js 的页面（工作台 / 笔记 / 资料 / 图谱）改完主题，
    // 下次进页面照样会被服务端那份旧设置覆盖回去，表现为「改了白改」。
    bumpSettingsRev();
    markSettingsDirty();
    return settingsCache;
  }

  function resetSettings() {
    settingsCache = null;
    rawRemove(K.settings);
    return settings();
  }

  /* 批注的两个上限：条数与单条字数。
     它是"聊天时随手记"，不是知识库 —— 到了上限就该整理，而不是继续堆
     （而且每条都要跟着设置整份同步，见 QF.store.notes 的说明）。 */
  var NOTES_MAX = 500;
  var NOTE_MAX_CHARS = 4000;

  function notesAll() {
    var list = settings().notes;
    return Array.isArray(list) ? list.slice() : [];
  }

  /* ---------------------------------------------------------- 记录 */

  var recordsCache = null;

  function records() {
    if (!recordsCache) recordsCache = readJSON(K.records, {});
    return recordsCache;
  }

  function flushRecords() {
    writeJSON(K.records, records());
  }

  /* -------------------------------------------------------- 作答流水 */
  // 每次判分产生一条流水（**增量**），而不是把「我这台设备看到的累计值」推上去。
  //
  // 后者在多设备下会互相覆盖：一台设备离线作答后回传，若它的 rev 已被另一台
  // 超过，这次作答就被静默丢弃 —— 计数不增、用户毫无察觉。
  // 流水是增量，服务端按主键去重后只对**真正插入**的那些累加，相加天然正确。
  //
  // 流水落本地而不是只放内存：断网、关页面、重开都不该丢。
  var ATTEMPT_QUEUE_MAX = 500;

  function uuid() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      return window.crypto.randomUUID();
    }
    // 兜底：随机 128 位拼成 UUID v4 形状。这里只需要唯一性。
    var bytes = new Uint8Array(16);
    if (window.crypto && window.crypto.getRandomValues) {
      window.crypto.getRandomValues(bytes);
    } else {
      for (var i = 0; i < 16; i++) bytes[i] = Math.floor(Math.random() * 256);
    }
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    var hex = [];
    for (var j = 0; j < 16; j++) hex.push(('0' + bytes[j].toString(16)).slice(-2));
    return (
      hex.slice(0, 4).join('') + '-' + hex.slice(4, 6).join('') + '-' +
      hex.slice(6, 8).join('') + '-' + hex.slice(8, 10).join('') + '-' + hex.slice(10).join('')
    );
  }

  function attempts() {
    var list = readJSON(K.attempts, []);
    return Array.isArray(list) ? list : [];
  }

  /** 记一条作答流水。只在**真正判分**时调用（未作答 / 待批改不计）。 */
  function queueAttempt(question, response, status, score, now) {
    if (!question || !question.id) return;
    if (status !== 'correct' && status !== 'partial' && status !== 'wrong') return;

    var list = attempts();
    list.push({
      id: uuid(),
      questionId: question.id,
      at: now,
      // 归属哪天用**本机日期**判定，随流水一起上报。
      // 让服务端用自身时区从 at 反推的话，服务器跑 UTC 时用户凌晨作答
      // 会被算到前一天，热力图与「今日已练」就对不上了。
      day: QF.ui.dayKey(now),
      status: status,
      score: typeof score === 'number' ? score : 0,
      topicKey: question.topic || '',
      response: response === undefined ? null : response,
    });
    // 上限只是防御性的：正常情况下流水在几百毫秒内就被推走
    if (list.length > ATTEMPT_QUEUE_MAX) list = list.slice(list.length - ATTEMPT_QUEUE_MAX);
    writeJSON(K.attempts, list);
    if (QF.sync) QF.sync.attemptsDirty();
  }

  function dropAttempts(ids) {
    if (!ids || !ids.length) return;
    var gone = {};
    ids.forEach(function (id) {
      gone[id] = true;
    });
    writeJSON(
      K.attempts,
      attempts().filter(function (item) {
        return !gone[item.id];
      })
    );
  }

  /* ------------------------------------------------------ 重置基线队列 */
  // 「重置这道题」「清空全部」「导入备份」都不能靠补丁表达 ——
  // 补丁通道是增减语义，而这三件事是「直接设成某个值」。
  // 所以单独一条通道，服务端收到后直接设定计数（且**不删流水**，
  // 删了的话重发旧流水会又累加一次）。
  function pendingResets() {
    return readJSON(K.resets, {});
  }

  function queueReset(id, counters) {
    var all = pendingResets();
    all[id] = counters === undefined ? null : counters;
    writeJSON(K.resets, all);
    if (QF.sync) QF.sync.resetsDirty();
  }

  function dropResets(ids) {
    if (!ids || !ids.length) return;
    var all = pendingResets();
    ids.forEach(function (id) {
      delete all[id];
    });
    writeJSON(K.resets, all);
  }

  /* -------------------------------------------------------- 云端同步 */
  // 这三个转发函数是「写路径」与「同步队列」之间唯一的连接点。
  // 队列本身在 sync.js（离线模式不加载），所以 store.js 对外仍然是
  // 一套同步 API，89 处调用点一行都不用改。
  var REV_FIELD = '_rev';

  function markRecordDirty(id) {
    if (QF.sync && id) QF.sync.recordDirty(id);
  }

  function markDaysDirty() {
    if (QF.sync) QF.sync.daysDirty();
  }

  function markSettingsDirty() {
    if (QF.sync) QF.sync.settingsDirty();
  }

  function markDaysSeedDirty() {
    if (QF.sync) QF.sync.daysSeedDirty();
  }

  /** 一批记录里的最大 rev —— 导入时用来把地板抬到当前值之上 */
  function maxRev(table) {
    var top = 0;
    Object.keys(table || {}).forEach(function (id) {
      var rec = table[id];
      if (rec && (rec[REV_FIELD] || 0) > top) top = rec[REV_FIELD] || 0;
    });
    return top;
  }

  /**
   * 灌入服务端快照。
   *
   * 服务端为准，但**本地 rev 更大的记录保留**并标脏 —— 那是断网期间
   * 攒下的改动，还没推上去，不能被旧的服务端数据覆盖掉。
   */
  function hydrate(snapshot) {
    if (!snapshot) return false;

    var local = records();
    var remote = snapshot.records || {};
    var merged = {};
    var keepLocal = 0;

    Object.keys(remote).forEach(function (id) {
      merged[id] = remote[id];
    });
    Object.keys(local).forEach(function (id) {
      var mine = local[id];
      var theirs = merged[id];
      if (!theirs || (mine[REV_FIELD] || 0) > (theirs[REV_FIELD] || 0)) {
        merged[id] = mine;
        keepLocal += 1;
        markRecordDirty(id);
      }
    });
    recordsCache = merged;
    writeJSON(K.records, merged);

    // 每日统计要**合并**而不是覆盖：
    //   - 服务端返回空对象（新账号）时，覆盖会把本地热力图历史直接抹掉
    //   - 本机断网期间攒下的作答会让本地更大，覆盖同样会丢
    // 取较大值是安全的：流水的权威副本在服务端，本机的乐观值只多不少，
    // 流水推上去之后服务端会追平并超过它，下一次合并自然收敛。
    if (snapshot.days && typeof snapshot.days === 'object') {
      var table = days();
      Object.keys(snapshot.days).forEach(function (key) {
        table[key] = mergeDayBucket(table[key], snapshot.days[key]);
      });
      daysCache = null;
      writeJSON(K.days, table);
    }

    hydrateSettings(snapshot);

    return { records: Object.keys(merged).length, keptLocal: keepLocal };
  }

  /**
   * 只灌「设置」那一段（主题 / 工具挂载 / 资料根 …）。
   *
   * 拆出来是因为：**不加载 boot.js 的那几页**（工作台 / 笔记 / 资料 / 图谱）走不到
   * `hydrate()`，于是读不到服务端那份设置、退回写死的兜底值。症状是同一个浏览器里
   * 两副面孔：练习中心浅色、工作台深色（启动页现在正是工作台）。它们现在自己调这个。
   */
  function hydrateSettings(snapshot) {
    if (!snapshot || typeof snapshot !== 'object') return false;

    // 本机那份更新（刚改过、还没推上去）就留着，只标脏等同步器推 ——
    // 与 records 的合并规则一个道理：本地的热改动不该被服务端的旧值按回去。
    var theirsRev = typeof snapshot.settingsRev === 'number' ? snapshot.settingsRev : 0;
    if (settingsRev() > theirsRev) {
      if (snapshot.settings) markSettingsDirty();
      return false;
    }

    var got = false;
    if (snapshot.settings && typeof snapshot.settings === 'object') {
      settingsCache = deepMerge(DEFAULT_SETTINGS, snapshot.settings);
      writeJSON(K.settings, settingsCache);
      got = true;
    }

    // 这里必须取较大值，不能直接覆盖。
    // 服务端对设置是「rev 不大于已存值就拒绝」，本机 rev 一旦被覆盖变小，
    // 它之后的每一次设置变更都会被静默拒绝 —— 表现为「改了主题，刷新就变回去」。
    if (typeof snapshot.settingsRev === 'number') {
      rawSet(K.settingsRev, String(Math.max(settingsRev(), snapshot.settingsRev)));
    }
    return got;
  }

  /** 按日期合并两份每日统计：逐字段取较大值（time 与 topics 都要） */
  function mergeDayBucket(mine, theirs) {
    if (!mine) return theirs;
    if (!theirs) return mine;
    var out = {
      answers: Math.max(mine.answers || 0, theirs.answers || 0),
      correct: Math.max(mine.correct || 0, theirs.correct || 0),
    };
    var keys = Object.keys(mine.topics || {}).concat(Object.keys(theirs.topics || {}));
    if (keys.length) {
      out.topics = {};
      keys.forEach(function (key) {
        out.topics[key] = Math.max(
          (mine.topics || {})[key] || 0,
          (theirs.topics || {})[key] || 0
        );
      });
    }
    return out;
  }

  function settingsRev() {
    return Number(rawGet(K.settingsRev) || '0') || 0;
  }

  function bumpSettingsRev() {
    var next = settingsRev() + 1;
    rawSet(K.settingsRev, String(next));
    return next;
  }

  function emptyRecord(id) {
    return {
      id: id,
      attempts: 0,
      correct: 0,
      partial: 0,
      wrong: 0,
      lastAt: 0,
      lastStatus: '',
      lastScore: null,
      lastResponse: null,
      firstAt: 0,
      sm2: QF.sm2 ? QF.sm2.fresh() : null,
      flagged: false,
      mastered: false,
      note: '',
    };
  }

  function record(id) {
    return records()[id] || null;
  }

  function ensureRecord(id) {
    var all = records();
    if (!all[id]) all[id] = emptyRecord(id);
    // 记录的所有改动（作答、自评、标记、星标）都会经过这里，
    // 所以在这里登记「待同步」与递增 rev 最不容易漏。
    // rev 偶尔多涨几次没有副作用：服务端只比较大小。
    all[id][REV_FIELD] = (all[id][REV_FIELD] || 0) + 1;
    markRecordDirty(id);
    return all[id];
  }

  function patchRecord(id, patch) {
    var rec = ensureRecord(id);
    Object.assign(rec, patch);
    flushRecords();
    return rec;
  }

  /**
   * 作答后写盘。
   * @param {object} question 题库条目
   * @param {*} response 用户作答
   * @param {object} result engine.grade 的返回值
   * @param {object} [options] {review: boolean, now: number}
   */
  function applyResult(question, response, result, options) {
    var opts = options || {};
    var now = opts.now || Date.now();
    var rec = ensureRecord(question.id);
    var status = result.status;

    if (!rec.firstAt) rec.firstAt = now;
    if (status === 'empty' || status === 'ungraded') {
      // 未作答 / 待批改不计入统计，但保留最后一次作答内容
      rec.lastResponse = response;
      flushRecords();
      return rec;
    }

    rec.attempts += 1;
    rec.lastAt = now;
    rec.lastStatus = status;
    rec.lastScore = result.score;
    rec.lastResponse = response;

    if (status === 'correct') rec.correct += 1;
    else if (status === 'partial') rec.partial += 1;
    else if (status === 'wrong') rec.wrong += 1;

    // 自动更新复习调度
    if (QF.sm2) {
      var grade = opts.review && typeof opts.reviewGrade === 'number'
        ? opts.reviewGrade
        : QF.engine
        ? QF.engine.toGrade(result)
        : 0;
      rec.sm2 = QF.sm2.update(rec.sm2, grade, now);
    }

    // 「已掌握」= 最近一次作答正确 → 从错题本默认视图移除（历史记录仍保留，
    // 需要时可在错题本里切到「含已订正」查看）；答错则重新回到错题本。
    rec.mastered = status === 'correct';
    // 连对次数：掌握度算法用它给「连续做对」一点加成，答错即清零
    rec.streak = status === 'correct' ? (rec.streak || 0) + 1 : 0;

    flushRecords();
    touchDay(now, status === 'correct', question.topic);
    // 计数不再靠上传这里的累计值，而是靠这条增量流水由服务端累加
    queueAttempt(question, response, status, result.score, now);
    return rec;
  }

  function setSelfGrade(id, grade, now) {
    var rec = ensureRecord(id);
    if (QF.sm2) rec.sm2 = QF.sm2.update(rec.sm2, grade, now || Date.now());
    rec.lastAt = now || Date.now();
    rec.lastStatus = grade >= 2 ? 'correct' : grade === 1 ? 'partial' : 'wrong';
    if (grade >= 2) {
      rec.attempts += 1;
      rec.correct += 1;
    } else if (grade === 1) {
      rec.attempts += 1;
      rec.partial += 1;
    } else {
      rec.attempts += 1;
      rec.wrong += 1;
    }
    rec.streak = grade >= 2 ? (rec.streak || 0) + 1 : 0;
    flushRecords();
    var q = QF.data ? QF.data.get(id) : null;
    touchDay(now, grade >= 2, q ? q.topic : null);
    // 自评也是一次真实作答：它同样累加了计数，所以要产生流水。
    // 作答内容取记录里的 lastResponse（自评前通常由「待批改」那次提交写入）。
    queueAttempt(
      { id: id, topic: q ? q.topic : '' },
      rec.lastResponse,
      rec.lastStatus,
      grade >= 2 ? 1 : grade === 1 ? 0.5 : 0,
      now
    );
    return rec;
  }

  function toggleFlag(id) {
    var rec = ensureRecord(id);
    rec.flagged = !rec.flagged;
    flushRecords();
    return rec;
  }

  function setNote(id, note) {
    return patchRecord(id, { note: String(note || '') });
  }

  function setMastered(id, value) {
    return patchRecord(id, { mastered: !!value });
  }

  function resetRecord(id) {
    delete records()[id];
    flushRecords();
    // 本地删掉不够：服务端才是计数的权威，得让它一起清零，
    // 否则刷新一次就「复活」了
    queueReset(id, null);
  }

  function resetAll() {
    recordsCache = {};
    rawRemove(K.records);
    rawRemove(K.days);
    rawRemove(K.jump);
    rawRemove(K.paper);
    // 待上传的流水与待设的基线都作废：用户要的是「全清」，
    // 留着它们反而会在下一次同步时把刚清掉的数据又写回服务端
    rawRemove(K.attempts);
    rawRemove(K.resets);
    if (QF.sync) QF.sync.resetAll();
  }

  /* ---------------------------------------------------------- 活跃度 */

  var daysCache = null;

  /**
   * 每日活跃度。存储格式为 { 'YYYY-MM-DD': {answers, correct} }；
   * 早期版本只存了一个数字（作答次数），这里做兼容归一化。
   */
  function days() {
    if (!daysCache) {
      var raw = readJSON(K.days, {});
      var normalized = {};
      Object.keys(raw).forEach(function (key) {
        var value = raw[key];
        if (typeof value === 'number') normalized[key] = { answers: value, correct: 0 };
        else if (value && typeof value === 'object') {
          normalized[key] = { answers: Number(value.answers) || 0, correct: Number(value.correct) || 0 };
          // topics 必须原样留下 —— 热力图按主题筛选全靠它。
          // 之前这里把它丢了，于是「按主题筛热力图永远是空的」（点了没反应的观感），
          // 更糟的是 touchDay 会把 days() 写回 localStorage，丢掉的版本就此落盘、永久损坏。
          if (value.topics && typeof value.topics === 'object') {
            var topics = {};
            Object.keys(value.topics).forEach(function (k) {
              var n = Number(value.topics[k]) || 0;
              if (n > 0) topics[k] = n;
            });
            if (Object.keys(topics).length) normalized[key].topics = topics;
          }
        }
      });
      daysCache = normalized;
    }
    return daysCache;
  }

  function dayBucket(key) {
    var table = days();
    if (!table[key]) table[key] = { answers: 0, correct: 0 };
    return table[key];
  }

  /**
   * 记一次作答。correct 为 true 时同时累加当日正确数。
   * topicKey 用于让热力图能按主题筛选 —— 只存一级主题，
   * 知识点级别的日粒度统计得不出有用的图形。
   */
  function touchDay(now, correct, topicKey) {
    var bucket = dayBucket(QF.ui.dayKey(now));
    bucket.answers += 1;
    if (correct) bucket.correct += 1;
    // 这里只做**本地乐观累加**，让热力图立刻更新。
    // 不再标记待上传：每日统计的权威来源是服务端按流水累加 —— 上传绝对累计值
    // 正是跨设备少算的根因（服务端只能取较大值，两台设备各 10 题会得到 10 而不是 20）。
    if (topicKey && QF.data && QF.data.topicPath) {
      var subject = QF.data.topicPath(topicKey)[0];
      if (!bucket.topics) bucket.topics = {};
      bucket.topics[subject] = (bucket.topics[subject] || 0) + 1;
    }
    writeJSON(K.days, days());
    return bucket;
  }

  /**
   * 今天的桶 —— **只读，不建**。
   *
   * 建桶会把它写进天表，而「连续练习」原来就是靠「今天有没有桶」判断今天是否
   * 已经练过的：一个空桶足以让它以为今天练了、然后停在 0。
   * 实测的复现路径：今天没练过（只有昨天练过）时刷新是「1 天」，
   * 随便点一下触发重渲染就变「0 天」，而近 7 天的活跃天数还是 1。
   */
  function today() {
    var table = days();
    return table[QF.ui.dayKey()] || { answers: 0, correct: 0 };
  }

  /**
   * 最近 n 天的活跃度序列（按日期升序），用于热力图与趋势。
   * 传 subjectKey 时只统计该主题下的作答；老数据没有 topics 字段，
   * 按主题筛会得到 0 —— 这是可接受的降级，不额外做迁移。
   */
  function daySeries(n, subjectKey) {
    var count = n || 126;
    var table = days();
    // 主题筛选支持"一个主题或一组主题"：上层主题必须连同**子孙**一起算 ——
    // 题目标签挂在子主题上，只按精确匹配的话，点一级主题永远命中不了任何题
    // （表现为"点了没反应"；主题题目数"加起来不对"也是同一个根）。
    var keys = subjectKey ? (subjectKey.map ? subjectKey.slice() : [subjectKey]) : null;
    var out = [];
    var cursor = new Date();
    cursor.setHours(0, 0, 0, 0);
    cursor.setDate(cursor.getDate() - (count - 1));
    for (var i = 0; i < count; i++) {
      var key = QF.ui.dayKey(cursor);
      var bucket = table[key] || { answers: 0, correct: 0 };
      if (keys && keys.length) {
        // 传一个主题（或一组主题）时，把它们的答题数**加起来** ——
        // 题目标签挂在子主题上，只做精确匹配的话，点一级主题永远命中不了任何题
        var sum = 0;
        for (var k = 0; k < keys.length; k++) {
          sum += (bucket.topics && bucket.topics[keys[k]]) || 0;
        }
        out.push({ date: key, answers: sum, correct: sum });
      } else {
        out.push({ date: key, answers: bucket.answers, correct: bucket.correct });
      }
      cursor.setDate(cursor.getDate() + 1);
    }
    return out;
  }

  /**
   * 最近 n 天里每个**学科**（一级主题）各练了多少题。
   *
   * 热力图的主题筛选项取自这里，而不是取自题库题数 ——
   * 热力图画的是「我练过什么」，只有在同一个时间窗里有作答的学科才值得作为筛选项出现。
   * 此前按题库题数列，于是没刷过的学科也能点，点进去必然是空图（"显示我没做题"）。
   *
   * 键是 `touchDay` 写入的一级主题键（题目的 topicPath[0]），与 D.subjects 的 key 同源。
   */
  function activityBySubject(n) {
    var count = n || 126;
    var table = days();
    var cursor = new Date();
    cursor.setHours(0, 0, 0, 0);
    cursor.setDate(cursor.getDate() - (count - 1));
    var out = {};
    for (var i = 0; i < count; i++) {
      var bucket = table[QF.ui.dayKey(cursor)];
      if (bucket && bucket.topics) {
        Object.keys(bucket.topics).forEach(function (key) {
          out[key] = (out[key] || 0) + bucket.topics[key];
        });
      }
      cursor.setDate(cursor.getDate() + 1);
    }
    return out;
  }

  /* --------------------------------------------------------- 掌握度 */
  /**
   * 单题掌握度 0–100。设计目标：只用做题记录里已有的字段，
   * 并且对「只做过一次就答对」保持怀疑。
   *
   *   acc   = (correct + 1) / (attempts + 2)        拉普拉斯平滑的正确率
   *   ev    = attempts / (attempts + 3)             证据强度：做得少就向中间收缩
   *   fresh = 0.5 ^ (days / 30)                     30 天半衰期的记忆新鲜度
   *   base  = acc × (0.55 + 0.45 × fresh) × (0.6 + 0.4 × ev)
   *   加成  = min(streak, 3) × 3                    连续答对最多 +9
   *
   * 校准值（用于自测）：1 次答对 ≈ 50，3 次全对 ≈ 73，5 次全对 ≈ 82，
   * 5 次全对但 60 天没碰 ≈ 57，1 次答错 ≈ 23，从未作答 = 0。
   */
  var MASTERY_HALFLIFE_DAYS = 30;
  var MASTERY_BANDS = [
    { key: 'new', label: '未练' },
    { key: 'weak', label: '薄弱' },
    { key: 'fair', label: '一般' },
    { key: 'solid', label: '熟练' },
    { key: 'mastered', label: '精通' },
  ];

  function masteryOfRecord(rec, now) {
    if (!rec || !rec.attempts) return 0;
    var stamp = now || Date.now();
    var acc = (rec.correct + 1) / (rec.attempts + 2);
    var ev = rec.attempts / (rec.attempts + 3);
    var days = Math.max(0, (stamp - (rec.lastAt || stamp)) / 86400000);
    var fresh = Math.pow(0.5, days / MASTERY_HALFLIFE_DAYS);
    var base = acc * (0.55 + 0.45 * fresh) * (0.6 + 0.4 * ev);
    var bonus = Math.min(rec.streak || 0, 3) * 3;
    return Math.max(0, Math.min(100, Math.round(base * 100 + bonus)));
  }

  /** 掌握度档位：未练 / 薄弱 / 一般 / 熟练 / 精通 */
  function masteryBand(rec, now) {
    if (!rec || !rec.attempts) return 'new';
    var m = masteryOfRecord(rec, now);
    if (m < 40) return 'weak';
    if (m < 70) return 'fair';
    if (m < 90) return 'solid';
    return 'mastered';
  }

  function mastery(id) {
    return masteryOfRecord(records()[id]);
  }

  function masteryBandOf(id) {
    return masteryBand(records()[id]);
  }

  /** 最近 n 天的汇总 */
  function recentTotals(n) {
    var series = daySeries(n);
    var answers = 0;
    var correct = 0;
    series.forEach(function (item) {
      answers += item.answers;
      correct += item.correct;
    });
    var activeDays = series.filter(function (item) {
      return item.answers > 0;
    }).length;
    return { answers: answers, correct: correct, activeDays: activeDays, days: n };
  }

  function streak() {
    var table = days();
    var cursor = new Date();
    var count = 0;
    // 今天还没做过就从昨天开始算，避免「连续天数」在早上归零。
    // 判据必须是「这天有作答」，不能是「表里有这个键」——
    // 空桶（answers 为 0）也算有键，会被当成「今天练过了」，然后停在 0。
    if (!dayAnswered(table, cursor)) cursor.setDate(cursor.getDate() - 1);
    for (var i = 0; i < 1000; i++) {
      if (!dayAnswered(table, cursor)) break;
      count += 1;
      cursor.setDate(cursor.getDate() - 1);
    }
    return count;
  }

  function dayAnswered(table, date) {
    var bucket = table[QF.ui.dayKey(date)];
    return !!(bucket && bucket.answers > 0);
  }

  /* ---------------------------------------------------------- 统计 */

  function stats() {
    var all = records();
    var questionList = QF.data ? QF.data.questions : [];
    var byTopic = {};
    var byType = {};

    (QF.data ? QF.data.topics : []).forEach(function (t) {
      byTopic[t.key] = { total: 0, attempted: 0, correct: 0, wrong: 0, mastered: 0 };
    });

    var attempted = 0;
    var correctTotal = 0;
    var attemptsTotal = 0;
    var mastered = 0;
    var flagged = 0;

    questionList.forEach(function (q) {
      var rec = all[q.id];
      // 主题是棵树：一道题的进度要同时计入它所在的每一级祖先，
      // 这样学科卡片上的进度环才是「该学科全部题目」的进度。
      var path = QF.data && QF.data.topicPath ? QF.data.topicPath(q.topic) : [q.topic];
      var buckets = path.map(function (key) {
        return byTopic[key] || (byTopic[key] = { total: 0, attempted: 0, correct: 0, wrong: 0, mastered: 0 });
      });
      var typeBucket = byType[q.type] || (byType[q.type] = { total: 0, attempted: 0, correct: 0, wrong: 0, mastered: 0 });
      buckets.forEach(function (b) {
        b.total += 1;
      });
      typeBucket.total += 1;
      if (!rec || !rec.attempts) return;

      attempted += 1;
      attemptsTotal += rec.attempts;
      correctTotal += rec.correct;
      typeBucket.attempted += 1;
      typeBucket.correct += rec.correct;
      typeBucket.wrong += rec.wrong;
      buckets.forEach(function (b) {
        b.attempted += 1;
        b.correct += rec.correct;
        b.wrong += rec.wrong;
        if (rec.mastered) b.mastered += 1;
      });
      if (rec.mastered) {
        mastered += 1;
        typeBucket.mastered += 1;
      }
      if (rec.flagged) flagged += 1;
    });

    var total = questionList.length;
    return {
      total: total,
      attempted: attempted,
      untouched: total - attempted,
      mastered: mastered,
      flagged: flagged,
      attempts: attemptsTotal,
      correct: correctTotal,
      accuracy: attemptsTotal ? correctTotal / attemptsTotal : 0,
      coverage: total ? attempted / total : 0,
      byTopic: byTopic,
      byType: byType,
      streak: streak(),
      today: today(),
      recent: recentTotals(7),
      heatmap: daySeries(126),
      recommended: recommend(),
      storageBytes: sizeBytes(),
      storageAvailable: available,
    };
  }

  /** 按最近练习情况给出下一步建议：错题 > 待复习 > 未练过的主题 */
  function recommend() {
    var all = records();
    var stats0 = QF.data ? QF.data.bankStats : { byTopic: {} };
    var weakestTopic = '';
    var worst = 2;
    Object.keys(stats0.byTopic || {}).forEach(function (key) {
      var bucket = { attempts: 0, correct: 0 };
      Object.keys(all).forEach(function (id) {
        var q = QF.data.get(id);
        if (!q || q.topic !== key) return;
        bucket.attempts += all[id].attempts;
        bucket.correct += all[id].correct;
      });
      if (bucket.attempts >= 3) {
        var rate = bucket.correct / bucket.attempts;
        if (rate < worst) {
          worst = rate;
          weakestTopic = key;
        }
      }
    });
    return {
      weakestTopic: weakestTopic,
      weakestRate: weakestTopic ? worst : null,
      wrongCount: wrongIds().length,
    };
  }

  /* ---------------------------------------------------------- 视图集合 */

  function allIds() {
    return QF.data ? QF.data.questions.map(function (q) {
      return q.id;
    }) : [];
  }

  /**
   * 这道题还在题库里吗。
   *
   * 记录（`records`）会**长期保留**：题退役或下架之后进度不丢，回滚回来还能接着刷。
   * 但"数给用户看"的数字只能算现存的题 —— 否则会出现
   * 「待复习还有 6 题，点进去只有 3 题」这种自相矛盾的界面（实测撞过：
   * 题库删掉一批题之后，计数还带着那些已经取不到题目的记录）。
   */
  function inBank(id) {
    return !!(QF.data && QF.data.get && QF.data.get(id));
  }

  /**
   * 错题本集合：答错或答得「不完整」（多选漏选、填空只对一部分、AI 判 partial）
   * 的题目都算需要巩固；最近一次作答正确后自动移出默认视图。
   */
  function wrongIds(options) {
    var opts = options || {};
    var all = records();
    var ids = Object.keys(all).filter(function (id) {
      var rec = all[id];
      if (!inBank(id)) return false;
      if (!rec || (!rec.wrong && !rec.partial)) return false;
      if (!opts.includeMastered && rec.mastered) return false;
      return true;
    });
    ids.sort(function (a, b) {
      var ra = all[a];
      var rb = all[b];
      if (opts.sortBy === 'wrong') return rb.wrong - ra.wrong || (rb.lastAt || 0) - (ra.lastAt || 0);
      if (opts.sortBy === 'attempts') return rb.attempts - ra.attempts || (rb.lastAt || 0) - (ra.lastAt || 0);
      return (rb.lastAt || 0) - (ra.lastAt || 0);
    });
    return ids;
  }

  function flaggedIds() {
    var all = records();
    return Object.keys(all).filter(function (id) {
      return inBank(id) && all[id].flagged;
    });
  }

  function dueIds(now) {
    if (!QF.sm2) return [];
    // 把题库的 id 一起给过去：复习列表是按题库过滤的，计数必须同一个口径
    return QF.sm2.dueList(records(), now, allIds()).map(function (rec) {
      return rec.id;
    });
  }

  /* ------------------------------------------------------- 跳转载荷 */

  function setJump(payload) {
    writeJSON(K.jump, Object.assign({ at: Date.now() }, payload || {}));
  }

  function takeJump() {
    var payload = readJSON(K.jump, null);
    if (payload) rawRemove(K.jump);
    return payload && Date.now() - payload.at < 60000 ? payload : null;
  }

  function savePaper(paper) {
    writeJSON(K.paper, paper);
  }

  function getPaper() {
    return readJSON(K.paper, null);
  }

  function clearPaper() {
    rawRemove(K.paper);
  }

  /* ------------------------------------------------------- 导入导出 */

  function exportAll(options) {
    var opts = options || {};
    var payload = {
      app: 'quizforge',
      schema: 1,
      exportedAt: new Date().toISOString(),
      bank: {
        generatedAt: QF.data ? QF.data.generatedAt : '',
        total: QF.data ? QF.data.questions.length : 0,
      },
      settings: deepMerge({}, settings()),
      records: records(),
      days: days(),
    };
    if (!opts.includeSecrets && payload.settings.ai) {
      payload.settings.ai.apiKey = '';
    }
    return payload;
  }

  function importAll(payload, options) {
    var opts = options || {};
    if (!payload || typeof payload !== 'object') throw new Error('数据格式不正确');
    if (payload.app && payload.app !== 'quizforge') throw new Error('这不是 quizforge 的导出文件');
    if (payload.schema && payload.schema > 1) throw new Error('导出文件来自更新的版本，请升级本页面');

    var merged = { records: 0, days: 0, settings: false };

    if (payload.records && typeof payload.records === 'object') {
      var all = records();
      // 导出文件里的 _rev 可能低于服务端当前值，直接沿用的话这条补丁会被
      // 判为「过期」而拒绝（星标/笔记就悄悄丢了）。先把地板抬到本地最大值之上。
      var floorRev = maxRev(all);
      Object.keys(payload.records).forEach(function (id) {
        if (!QF.data.get(id)) return; // 题库里已不存在的题跳过
        var incoming = payload.records[id];
        if (opts.replace || !all[id]) {
          all[id] = incoming;
        } else {
          var existing = all[id];
          all[id] = Object.assign({}, incoming, {
            attempts: (existing.attempts || 0) + (incoming.attempts || 0),
            correct: (existing.correct || 0) + (incoming.correct || 0),
            partial: (existing.partial || 0) + (incoming.partial || 0),
            wrong: (existing.wrong || 0) + (incoming.wrong || 0),
            lastAt: Math.max(existing.lastAt || 0, incoming.lastAt || 0),
            firstAt: Math.min(existing.firstAt || Infinity, incoming.firstAt || Infinity) || 0,
            flagged: existing.flagged || incoming.flagged,
            mastered: incoming.mastered || existing.mastered,
            note: incoming.note || existing.note,
            sm2: incoming.lastAt >= existing.lastAt ? incoming.sm2 : existing.sm2,
          });
        }
        // 导入进来的计数没有对应流水，必须作为**新基线**推给服务端，
        // 否则它只活在本地，刷新就被服务端的权威值抹掉
        var imported = all[id];
        imported[REV_FIELD] = Math.max(imported[REV_FIELD] || 0, floorRev) + 1;
        queueReset(id, {
          attempts: imported.attempts || 0,
          correct: imported.correct || 0,
          partial: imported.partial || 0,
          wrong: imported.wrong || 0,
          firstAt: imported.firstAt || 0,
          lastAt: imported.lastAt || 0,
          lastStatus: imported.lastStatus || '',
          lastScore: imported.lastScore || 0,
          lastResponse: imported.lastResponse === undefined ? null : imported.lastResponse,
        });
        merged.records += 1;
      });
      flushRecords();
    }

    if (payload.days && typeof payload.days === 'object') {
      var table = days();
      Object.keys(payload.days).forEach(function (key) {
        table[key] = (table[key] || 0) + payload.days[key];
        merged.days += 1;
      });
      writeJSON(K.days, table);
      // 历史热力图同样没有流水支撑，走「种子」通道。
      // 服务端只接受「该日期还没有任何流水」的那些天，避免覆盖真实统计。
      markDaysSeedDirty();
    }

    if (payload.settings && opts.withSettings !== false) {
      var current = settings();
      var patch = deepMerge({}, payload.settings);
      // 不覆盖本机已填的密钥
      if (!patch.ai) patch.ai = {};
      if (!patch.ai.apiKey && current.ai.apiKey) patch.ai.apiKey = current.ai.apiKey;
      saveSettings(patch);
      merged.settings = true;
    }

    return merged;
  }

  function sizeBytes() {
    var total = 0;
    Object.keys(K).forEach(function (name) {
      var raw = rawGet(K[name]);
      if (raw) total += raw.length;
    });
    return total;
  }

  QF.store = {
    KEYS: K,
    available: available,
    settings: settings,
    // 云端同步（离线模式下 QF.sync 为 null，这些函数不会被用到）
    hydrate: hydrate,
    hydrateSettings: hydrateSettings,
    settingsRev: settingsRev,
    bumpSettingsRev: bumpSettingsRev,
    // 待上传的作答流水与重置基线，由 sync.js 取走并回执删除
    attempts: attempts,
    dropAttempts: dropAttempts,
    pendingResets: pendingResets,
    dropResets: dropResets,
    saveSettings: saveSettings,
    resetSettings: resetSettings,
    pinnedTopics: function () {
      var list = settings().pinnedTopics;
      return Array.isArray(list) ? list.slice() : [];
    },
    togglePin: function (key) {
      var list = settings().pinnedTopics || [];
      var at = list.indexOf(key);
      if (at === -1) list = list.concat([key]);
      else list = list.slice(0, at).concat(list.slice(at + 1));
      saveSettings({ pinnedTopics: list });
      return list;
    },
    /* ------------------------------------------------------------ 批注
     *
     * 为什么放在 settings 里：它已经是**会被同步的东西**（本地优先 + 跨设备按
     * rev 合并），于是批注不用另开一张表、一条接口、一套冲突规则。
     * 代价是每次同步整份带上 —— 所以下面有两个上限：一条批注就是一句话，
     * 但它是**按段落**标的，比原来的随手记密得多，所以条数给得宽一点。
     */
    notes: function () {
      return notesAll();
    },
    /** 加一条批注（或纯高亮）。
     *
     * `mid` / `start` / `end` 是"钉在哪一段上"：消息 id，加那段在**渲染后正文**里的
     * 字符区间；`quote` 是当时选中的原文 —— 万一渲染变了样，靠它还能认出标的是哪句。
     * `text` 给空串 = 只有高亮不写字（那也是批注的一种，有时你只是想标记一下）。
     */
    addMark: function (mark) {
      var one = mark || {};
      var list = notesAll();
      if (list.length >= NOTES_MAX) return null;
      var piece = {
        id: 'm' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
        text: String(one.text || '').trim().slice(0, NOTE_MAX_CHARS),
        quote: String(one.quote || '').slice(0, 600),
        mid: one.mid == null ? '' : String(one.mid),
        start: Math.max(0, parseInt(one.start, 10) || 0),
        end: Math.max(0, parseInt(one.end, 10) || 0),
        at: Date.now(),
        cid: one.cid ? String(one.cid) : '',
      };
      list.unshift(piece); // 新的在最前：批注也是往前翻的
      saveSettings({ notes: list });
      return piece;
    },
    /** 某条消息上钉着的批注（渲染高亮时按它找区间）。 */
    marksOf: function (mid) {
      var key = mid == null ? '' : String(mid);
      return notesAll().filter(function (one) {
        return String(one.mid) === key;
      });
    },
    updateNote: function (id, text) {
      var body = String(text || '').trim().slice(0, NOTE_MAX_CHARS);
      if (!body) return false;
      var list = notesAll();
      var hit = false;
      list = list.map(function (note) {
        if (note.id !== id) return note;
        hit = true;
        return { id: note.id, text: body, at: note.at, cid: note.cid || '', editedAt: Date.now() };
      });
      if (hit) saveSettings({ notes: list });
      return hit;
    },
    removeNote: function (id) {
      var list = notesAll();
      var next = list.filter(function (note) {
        return note.id !== id;
      });
      if (next.length === list.length) return false;
      saveSettings({ notes: next });
      return true;
    },
    // 选题篮落盘：攒题可以跨刷新、跨会话继续
    basket: function () {
      var list = settings().basket;
      return Array.isArray(list) ? list.slice() : [];
    },
    setBasket: function (ids) {
      var list = (ids || []).filter(function (id) {
        return typeof id === 'string' && id;
      });
      saveSettings({ basket: list });
      return list;
    },
    records: records,
    record: record,
    ensureRecord: ensureRecord,
    patchRecord: patchRecord,
    applyResult: applyResult,
    setSelfGrade: setSelfGrade,
    toggleFlag: toggleFlag,
    // 掌握度：由做题记录算出，替代「对/错」这种二值判断
    mastery: mastery,
    masteryBandOf: masteryBandOf,
    masteryOfRecord: masteryOfRecord,
    masteryBand: masteryBand,
    masteryBands: MASTERY_BANDS,
    setNote: setNote,
    setMastered: setMastered,
    resetRecord: resetRecord,
    resetAll: resetAll,
    days: days,
    touchDay: touchDay,
    today: today,
    daySeries: daySeries,
    activityBySubject: activityBySubject,
    recentTotals: recentTotals,
    streak: streak,
    stats: stats,
    allIds: allIds,
    wrongIds: wrongIds,
    flaggedIds: flaggedIds,
    dueIds: dueIds,
    setJump: setJump,
    takeJump: takeJump,
    savePaper: savePaper,
    getPaper: getPaper,
    clearPaper: clearPaper,
    exportAll: exportAll,
    importAll: importAll,
    sizeBytes: sizeBytes,
  };
})();
