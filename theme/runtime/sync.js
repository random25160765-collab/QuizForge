/* ===========================================================================
 * sync.js —— 学习进度的云端同步队列
 *
 * 前端的 store 对外是同步 API，本地读写照旧；这里只额外做一件事：
 * 记住「哪些东西变脏了」，去抖之后批量推给服务端。
 *
 * ## 三个通道，语义不同，刻意分开
 *
 *   attempts —— **增量**。每次判分一条，带客户端生成的 id。
 *                服务端只对真正插入成功的那些累加计数与每日统计，
 *                所以「请求发出、响应丢了、重发」不会重复计数。
 *                这是跨设备正确性的核心。
 *   patches  —— 主观状态（星标/已订正/笔记/复习调度/小问结果），
 *                没有可加和的语义，按 client_rev 的 last-write-wins 合并。
 *                **计数字段从这里被服务端强制忽略**，否则一台设备的快照
 *                会覆盖另一台的增量，等于问题没解决。
 *   resets   —— 「直接设成某个值」（重置这道题 / 清空 / 导入备份）。
 *
 * ## 两种清理时机
 *
 * 标记类（patches / settings / daysSeed）在发送前就清掉，失败时再打回来 ——
 * 重发是幂等的，代价只是多一次请求。
 * 数据类（attempts / resets）**必须等成功回执才删** —— 丢了就真的丢了。
 *
 * store.js 通过 `QF.sync` 找到这个队列（加载顺序保证它先就绪）。
 * ========================================================================= */
(function () {
  'use strict';

  var QF = (window.QF = window.QF || {});

  var DEBOUNCE_MS = 600;      // 合并连续操作（连着答几题只发一次）
  var RETRY_BASE_MS = 4000;   // 失败退避基数
  var RETRY_MAX_MS = 60000;
  // 只有这些字段走补丁通道。故意列成白名单而不是「排除计数」：
  // 新增字段时若忘了登记，症状是「本地对了、刷新就没了」，比多传一个字段难查得多。
  var PATCH_KEYS = ['sm2', 'note', 'streak', 'mastered', 'flagged', 'parts', 'lastResponse'];

  var state = {
    records: {},        // id → true
    settings: false,
    daysSeed: false,
    hasAttempts: false,
    hasResets: false,
    timer: 0,
    inflight: false,
    failures: 0,
    lastError: '',
    lastSyncAt: 0,
  };

  var listeners = [];

  /* ------------------------------------------------------------ 对外 */

  function pendingCount() {
    return (
      Object.keys(state.records).length +
      (state.settings ? 1 : 0) +
      (state.daysSeed ? 1 : 0) +
      (state.hasAttempts ? 1 : 0) +
      (state.hasResets ? 1 : 0)
    );
  }

  function status() {
    return {
      pending: pendingCount(),
      inflight: state.inflight,
      failures: state.failures,
      lastError: state.lastError,
      lastSyncAt: state.lastSyncAt,
      online: navigator.onLine !== false,
    };
  }

  function recordDirty(id) {
    if (!id) return;
    state.records[id] = true;
    schedule();
  }

  function settingsDirty() {
    state.settings = true;
    schedule();
  }

  /** 本机还有**没推上去**的设置改动吗？
   *
   * 给 `store.hydrateSettings` 用，判"服务端那份该不该盖掉本机这份"。
   * 少了它就会丢东西（实测 2026-09-26）：本机 rev 落后服务端时（另一处把计数推高了、
   * 或本地存储被清过），加载时会把**本机刚写、还没推上去**的设置整份换成服务端的旧副本 ——
   * 用户看到的正是"批注打完回车，过一会儿就没了"。 */
  function settingsPending() {
    return !!state.settings;
  }

  function daysSeedDirty() {
    state.daysSeed = true;
    schedule();
  }

  function attemptsDirty() {
    state.hasAttempts = true;
    schedule();
  }

  function resetsDirty() {
    state.hasResets = true;
    schedule();
  }

  function schedule(delay) {
    if (state.timer) clearTimeout(state.timer);
    state.timer = setTimeout(flush, delay == null ? DEBOUNCE_MS : delay);
  }

  /** 立即推送（关页前、切到前台、网络恢复、用户手动重试时用）。 */
  function flush() {
    state.timer = 0;
    if (state.inflight) return Promise.resolve(null);
    if (!pendingCount()) return Promise.resolve(null);
    if (!QF.api || !QF.api.post) return Promise.resolve(null);

    if (navigator.onLine === false) {
      state.lastError = '离线中，稍后自动重试';
      schedule(RETRY_BASE_MS);
      notify();
      return Promise.resolve(null);
    }

    var body = buildPayload();
    if (!body) return Promise.resolve(null);

    state.inflight = true;
    notify();

    return QF.api
      .post('/progress/sync', body)
      .then(function (res) {
        state.inflight = false;
        state.failures = 0;
        state.lastError = '';
        state.lastSyncAt = Date.now();

        // 数据类：拿到成功回执才删
        QF.store.dropAttempts(body.attemptIds);
        QF.store.dropResets(body.resetIds);
        applyRejected(res);
        // **问一句"那份设置收下没有"**：服务端在 rev 不大于已存值时会保留旧副本，
        // 而 200 照样是 200 —— 不核对的话，本机会以为自己推成功了，
        // 下一次加载就把刚写的批注换成服务端那份旧的（实测就是这么丢的）。
        if (body.sentSettings && QF.store.noteSettingsPush) {
          QF.store.noteSettingsPush(res && res.settingsAccepted, res && res.settingsRev);
        }

        notify();
        // 期间可能又攒了新的，接着推
        if (pendingCount()) schedule(0);
        return res;
      })
      .catch(function (err) {
        state.inflight = false;
        state.failures += 1;
        state.lastError = (err && err.message) || '同步失败';

        // 标记类：发送前清掉了，这里打回来等下一轮。
        // 数据类不用管 —— 它们还在本地队列里，本来就没删。
        body.attemptIds.forEach(function (id) {
          state.records[id] = true;
        });
        if (body.sentSettings) state.settings = true;
        if (body.sentDaysSeed) state.daysSeed = true;
        // 这两个标记必须按队列的实际内容恢复。
        // 漏掉的话，当失败时只剩流水待上传（没有记录补丁）时 pendingCount() 会是 0，
        // flush 直接早退 —— 那些作答要等到用户下次答别的题才会被顺带推上去。
        state.hasAttempts = QF.store.attempts().length > 0;
        state.hasResets = Object.keys(QF.store.pendingResets()).length > 0;

        schedule(backoff());
        notify();
        return null;
      });
  }

  function buildPayload() {
    var store = QF.store;

    var attemptList = store.attempts();
    var resetMap = store.pendingResets();
    var recordIds = Object.keys(state.records);

    var body = {
      attempts: attemptList,
      attemptIds: attemptList.map(function (item) {
        return item.id;
      }),
      patches: {},
      resets: resetMap,
      resetIds: Object.keys(resetMap),
      sentSettings: false,
      sentDaysSeed: false,
    };

    recordIds.forEach(function (id) {
      var rec = store.records()[id];
      if (!rec) return;
      var patch = { _rev: rec._rev || 0 };
      PATCH_KEYS.forEach(function (key) {
        if (rec[key] !== undefined) patch[key] = rec[key];
      });
      body.patches[id] = patch;
    });

    if (state.settings) {
      body.settings = store.settings();
      body.settingsRev = store.bumpSettingsRev();
      body.sentSettings = true;
    }
    if (state.daysSeed) {
      body.daysSeed = store.days();
      body.sentDaysSeed = true;
    }

    var hasData =
      attemptList.length || body.resetIds.length || Object.keys(body.patches).length ||
      body.sentSettings || body.sentDaysSeed;

    if (!hasData) {
      // 只有空标记，没什么可发：清掉，免得每 600ms 空转一次
      state.records = {};
      state.settings = false;
      state.daysSeed = false;
      state.hasAttempts = false;
      state.hasResets = false;
      return null;
    }

    // 标记类先清；期间新产生的写入会重新标脏，不会丢
    state.records = {};
    state.settings = false;
    state.daysSeed = false;
    state.hasAttempts = false;
    state.hasResets = false;

    return body;
  }

  function backoff() {
    return Math.min(RETRY_BASE_MS * Math.pow(2, Math.min(state.failures - 1, 4)), RETRY_MAX_MS);
  }

  /**
   * 服务端拒绝了过期的补丁：用它回传的权威值覆盖本地。
   *
   * 不这么做的话，用户在设备 B 上把星标打开、界面显示已打开，
   * 但服务端仍是旧值 —— 刷新之后星标又灭了，属于「静默失败」。
   */
  function applyRejected(res) {
    var rejected = (res && res.patchesRejected) || {};
    var ids = Object.keys(rejected);
    if (!ids.length) return;

    var all = QF.store.records();
    var touched = false;
    ids.forEach(function (id) {
      if (!all[id]) return;
      // 计数与补丁一起换掉：服务端那份是权威
      all[id] = Object.assign({}, all[id], rejected[id]);
      touched = true;
    });
    if (touched && QF.app && QF.app.booted && QF.app.render) QF.app.render();
  }

  /* ------------------------------------------------------------ 清空 */

  /**
   * 全清。单独一条通道而不是塞进 resets：
   * 「清空」会连流水一起删掉（幂等键也就没意义了），语义与「设基线」不同。
   */
  function resetAll() {
    if (!QF.api || !QF.api.post) return Promise.resolve(null);
    return QF.api
      .post('/progress/reset', {})
      .then(function () {
        state.records = {};
        state.hasAttempts = false;
        state.hasResets = false;
        notify();
        return true;
      })
      .catch(function (err) {
        state.lastError = (err && err.message) || '清空失败';
        notify();
        return false;
      });
  }

  /* ------------------------------------------------------------ 订阅 */

  function subscribe(fn) {
    listeners.push(fn);
    return function () {
      listeners = listeners.filter(function (item) {
        return item !== fn;
      });
    };
  }

  function notify() {
    var snapshot = status();
    listeners.forEach(function (fn) {
      try {
        fn(snapshot);
      } catch (err) {
        /* 订阅者自己的错误不该影响同步 */
      }
    });
  }

  /* ------------------------------------------------------------ 生命周期 */

  window.addEventListener('online', function () {
    state.failures = 0;
    flush();
  });

  // 切回前台：先把本机攒的推上去，再拉一次快照。
  // 后者是「手机练完、切到笔记本就能看到」成立的关键。
  // 拉快照由 app.js 负责，因为只有它知道「正在做题」时不能打断。
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState !== 'visible') return;
    flush().then(function () {
      if (QF.app && QF.app.refreshFromServer) QF.app.refreshFromServer();
    });
  });

  // 关页前尽力推一次。fetch 可能来不及，所以不依赖它 ——
  // 流水都留在本地队列里，下次启动会接着推
  window.addEventListener('pagehide', function () {
    flush();
  });

  QF.sync = {
    recordDirty: recordDirty,
    settingsDirty: settingsDirty,
    settingsPending: settingsPending,
    daysSeedDirty: daysSeedDirty,
    attemptsDirty: attemptsDirty,
    resetsDirty: resetsDirty,
    flush: flush,
    resetAll: resetAll,
    status: status,
    subscribe: subscribe,
    /** 启动时把上次没推成功的重新标脏 */
    resume: function () {
      if (QF.store.attempts().length) state.hasAttempts = true;
      if (Object.keys(QF.store.pendingResets()).length) state.hasResets = true;
      if (state.hasAttempts || state.hasResets) schedule(0);
    },
  };
})();
