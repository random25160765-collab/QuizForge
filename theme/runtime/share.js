/* share.js —— 单页分享页的引导（**只被打进分享页**，在线那一份里没有它）。
 *
 * 分享页是一件**只读产物**：一条会话 + 它的对话树。没有后端，所以这里只做两件事：
 *
 *   1. **把接口层换掉** —— `chat.js` 以为自己还在跟服务端说话，其实数据来自内联的
 *      `window.__QF_SHARE__`。它怎么调 `/chat/conversations/{id}`，这里就怎么接；
 *      **`chat.js` 一行不改**（它下次改了，分享页跟着改）。
 *   2. **开页面** —— 不走 `boot.js`：那一条会先拉题库（`/bank`），空库直接中止在
 *      "题库还没有题目"那一屏，而分享页根本没有题库这回事。
 *
 * **会话不用手动打开**：`chat.js` 的 boot 本来就"列表非空就开第一条"
 *（见它 `loadList().then(...)` 那一段），而这里喂的列表**只有这一条**。
 *
 * 只读是**硬**的：写类接口一律抛错，由页面自己的错误处理说出来
 *（发消息失败会 toast）—— 比"点了没反应"诚实。
 */
(function () {
  'use strict';

  var QF = window.QF;
  var DATA = window.__QF_SHARE__ || {};
  var conv = DATA.conversation || {};
  var messages = DATA.messages || [];

  // 演示（`render_demo` 那些页面）靠**应用自己的源**去取运行时（React / d3 / Babel / QFKit…）。
  // 分享页是 `file://` 打开的，那里 `location.origin` 的字符串是 `"null"`，依赖一个都加载不到 ——
  // 于是"所有的 demo 都看不到也无法运行"（2026-09-29 用户报障）。装配它的那个应用地址由服务端
  // 带进来（`payload_of` 的 `origin`），交给 `shareOrigin()` 用（见 chat.js：CSP 与 `__ORIGIN__`
  // 替换都走它）。应用可达时演示就能动；不可达就只剩卡片与已存的输出。
  if (DATA.origin && QF.config) QF.config.shareOrigin = String(DATA.origin);

  var READ_ONLY = '这是分享页，只读 —— 想接着问，去打开你本机那一份。';

  function ok(payload) {
    return Promise.resolve(payload);
  }

  function no() {
    return Promise.reject(new Error(READ_ONLY));
  }

  /* ---- 1. 接口层：换成"从内联数据读" ---------------------------------- */

  // 只接**页面真的会调的那几个路径**，其余一律拒绝。理由：那些失败页面自己都有兜底
  //（比如 `/ai/usage` 读不到就当"未知"，不画提示也不拦人），硬造一个假值反而会
  // 让别处以为"服务端说了它没有"，那是另一种错。
  //
  // 注意是**换方法、不是换对象**：`chat.js` 在加载时就把 `QF.api` 存进了自己的
  // 局部变量（`var api = QF.api`），换对象它看不见，换方法才看得见。
  QF.api.get = function (path) {
    var p = String(path || '');
    if (/^\/chat\/conversations\/[^/]+$/.test(p)) {
      return ok({ conversation: conv, messages: messages });
    }
    if (p === '/chat/conversations') {
      // 列表里**只有这一条** —— 于是 boot 那句"列表非空就开第一条"正好打开它
      return ok({
        conversations: [
          {
            id: conv.id,
            title: conv.title || '',
            folder: conv.folder || '',
            pinned: false,
            archived: false,
            createdAt: conv.createdAt || '',
            updatedAt: conv.updatedAt || '',
            messageCount: messages.length
          }
        ],
        folders: []
      });
    }
    if (p === '/chat/mounts') {
      // 理论上用不到；给一份空的，别让页面以为"能力探测失败"
      return ok({ mode: 'share', modes: [], groups: [], labels: {} });
    }
    return no();
  };

  ['post', 'patch', 'put', 'del'].forEach(function (verb) {
    QF.api[verb] = no;
  });
  QF.api.stream = no;

  /* ---- 2. 我的痕迹（圈点勾画 / 批注 / 书签）--------------------------- */
  //
  // 这些笔住在"设置"里（`QF.store` 的 `notes` / `places`，见 store.js 的
  // `addMark` / `addPlace`）：在线页靠 `boot.js` 拉 `GET /api/progress` 灌进去，
  // 而分享页**没有 boot.js**（按设计，见文件头第 2 条）。于是从前的分享页里，
  // 用户自己划过的每一笔都看不见 —— 正文没有高亮、没有旁批，消息行尾那两枚
  // 图钉（回溯 / 书签）也永远是灰的（2026-09-28 用户报障：
  // "导出的对话网页里面，没有我的圈点勾画/批注/书签"）。
  //
  // 数据是装配时**按这条会话过滤好**、随 `__QF_SHARE__` 一起进来的
  //（服务端 `settings_store.marks_of`）。**渲染那边一行都不用改**：正文的
  // `paintMarks` 与行尾的 `placeButtons` 本来就问 `QF.store`，有数据就会画出来。
  //
  // `settingsRev` 传 0：分享页是只读产物，`hydrateSettings` 比 rev 是为了护住
  // "本机还没推上去的改动"，这里没有那种东西。
  // **先忘掉本机那份**：`file://` 这一个 origin 是所有导出页共用的，上一次打开分享页时
  // 留下的 `settingsDirty`（关页面 → `saveReadAnchor` → 写设置）会让下面那次 hydrate
  // **整份不灌** —— 于是批注栏 / 书签抽屉全空，而对话树照画，看着就像"批注没被导出"
  //（2026-09-29 用户报障，实测复现）。分享页是只读产物，本机那份与它无关。
  if (QF.store && QF.store.forgetLocalSettings) QF.store.forgetLocalSettings();

  if (DATA.settings && QF.store && QF.store.hydrateSettings) {
    QF.store.hydrateSettings({ settings: DATA.settings, settingsRev: 0 });
  }

  /* ---- 2b. 显示**哪一支**：作者当时在读的那条分支 ---------------------- */
  //
  // 页面显示哪一支本来有一部分是**内存里的选择**（`state.picks`，见 chat.js 的
  // `activePath`）—— 分享页是新开的一份、`picks` 是空的，只按"最新那一支"渲染。
  // 而他若切到较早的分支上读过（划记号的往往正是那一条），导出页显示的就是**另一条**，
  // 看起来就是"我的圈点勾画一支都不在"（2026-09-28 用户报障：
  // "导出的网页还是没有任何标签和批注"）。
  //
  // `leaf` 是服务端带过来的"末端消息 id"（见 `app/share.py` 的 `payload_of`），
  // `reveal()` 会把沿路每一层的选择都设成它 —— 于是渲染的就是同一支。
  //
  // 时机不必讲究：`boot()` 是异步取数据，`reveal()` 在消息还没到时**先记下**，
  // 等正文落地那一步再消费（见 chat.js 的 `pendingReveal`）。
  if (DATA.leaf && QF.chat && QF.chat.reveal) QF.chat.reveal(String(DATA.leaf));

  /* ---- 3. 开页面 ------------------------------------------------------ */

  if (QF.config) QF.config.mode = 'share';
  document.documentElement.setAttribute('data-share', '1');

  QF.chat.boot();
})();
