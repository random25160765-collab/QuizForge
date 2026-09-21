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

  /* ---- 2. 开页面 ------------------------------------------------------ */

  if (QF.config) QF.config.mode = 'share';
  document.documentElement.setAttribute('data-share', '1');

  QF.chat.boot();
})();
