/* ===========================================================================
 * api.js —— 后端接口封装
 *
 * 数据全部来自 /api，会话靠 HttpOnly Cookie；基址由构建期注入的
 * window.QF_CONFIG.apiBase 给出（默认 /api）。
 *
 * 统一在这里做两件事：拼基址、把各种失败归一成带 status 的 Error ——
 * 调用方只需要 try/catch，不用关心响应形态。
 *
 * （原先还要带 CSRF 双提交令牌、401 统一跳登录页 —— 单用户本地形态下这两件事
 *   都不存在了，见 `api/app/deps.py`。）
 * ========================================================================= */
(function () {
  'use strict';

  var QF = (window.QF = window.QF || {});

  var config = window.QF_CONFIG || {};
  var BASE = String(config.apiBase || '').replace(/\/+$/, '');

  QF.config = config;

  // 这里原先有 `unauthorizedHandler`（401 时跳登录页）与 `csrfToken()`（读 qf_csrf
  // Cookie 放进请求头）。单用户本地形态下两者都没有意义：没有登录页可跳，
  // 也没有跨站表单要防。删掉，别让后来人以为还有这套东西。

  function parseBody(response) {
    var type = response.headers.get('content-type') || '';
    if (type.indexOf('application/json') !== -1) {
      return response.json().catch(function () {
        return null;
      });
    }
    return response.text().then(function (text) {
      return { detail: text };
    });
  }

  function buildError(response, data) {
    var message =
      (data && (data.detail || data.message)) ||
      '请求失败（HTTP ' + response.status + '）';
    if (typeof message !== 'string') message = JSON.stringify(message);
    var err = new Error(message);
    err.status = response.status;
    err.data = data || null;
    return err;
  }

  /**
   * 发一个请求。
   * @param {string} method
   * @param {string} path    以 / 开头的接口路径（不含 /api 前缀）
   * @param {object} [options]
   *   body      请求体，会被 JSON 序列化
   *   allow401  401 时不触发「未授权」回调（登录页自己处理 401）
   *   signal    AbortSignal
   */
  function request(method, path, options) {
    var opts = options || {};
    var headers = {};
    if (opts.body !== undefined && !opts.form) headers['Content-Type'] = 'application/json';

    var init = {
      method: method,
      headers: headers,
      // 会话是 HttpOnly Cookie，必须带上；同源所以用 same-origin
      credentials: 'same-origin',
    };
    if (opts.form) {
      // multipart：**不要**自己设 Content-Type —— boundary 只有浏览器知道，
      // 手动设了服务端就解不出表单
      init.body = opts.form;
    } else if (opts.body !== undefined) {
      init.body = JSON.stringify(opts.body);
    }
    if (opts.signal) init.signal = opts.signal;

    return fetch(BASE + path, init).then(
      function (response) {
        if (response.status === 204) return null;
        return parseBody(response).then(function (data) {
          if (response.ok) return data;
          throw buildError(response, data);
        });
      },
      function (cause) {
        // fetch 只在网络层失败时 reject（断网、被拦截、DNS 等）
        var err = new Error('本地服务连不上，检查它是否在运行');
        err.status = 0;
        err.cause = cause;
        throw err;
      }
    );
  }

  /**
   * 拆 SSE 帧并逐条派发。
   *
   * 帧边界是空行；帧内只认 `event:` 与 `data:` 两种字段 —— 我们服务端就是这么发的
   * （见 api/app/routers/chat.py 的 `_sse`）。**刻意不实现 `id:`**：
   * 那是给 `Last-Event-ID` 断点续传用的，实现了就等于暗示支持它。
   *
   * @param {ReadableStreamDefaultReader} reader
   * @param {object} handlers 事件名 → 回调
   */
  function pump(reader, handlers) {
    var decoder = new TextDecoder('utf-8');
    var buffer = '';

    function dispatch() {
      var index;
      while ((index = buffer.indexOf('\n\n')) !== -1) {
        var block = buffer.slice(0, index);
        buffer = buffer.slice(index + 2);

        var name = '';
        var payload = null;
        block.split('\n').forEach(function (line) {
          if (line.indexOf('event: ') === 0) name = line.slice(7).trim();
          else if (line.indexOf('data: ') === 0) {
            try {
              payload = JSON.parse(line.slice(6));
            } catch (e) {
              payload = null;
            }
          }
        });

        var fn = name && handlers[name];
        if (fn) fn(payload);
      }
    }

    function read() {
      return reader.read().then(function (result) {
        if (result.done) {
          buffer += decoder.decode();
          dispatch();
          return;
        }
        buffer += decoder.decode(result.value, { stream: true });
        dispatch();
        return read();
      });
    }

    return read();
  }

  /**
   * 流式请求：读 SSE 并把每个事件交给 handlers。
   *
   * 为什么不用 `EventSource`：它发不了 POST、也带不了自定义头。
   * 用 fetch + ReadableStream 自己拆帧，图的是「可取消」与「可带凭据」。
   *
   * 与 `request()` 的差别只在成功路径上：那一个要的是整个 JSON，
   * 这一个要的是过程中陆续来的事件。失败路径完全一致（同一个错误对象）。
   *
   * @param {string} path      以 / 开头的接口路径
   * @param {object} body
   * @param {object} handlers  { user, start, delta, usage, done, error }
   * @param {object} [options] { signal }
   * @returns {Promise<void>}  流结束（或被 abort）时 resolve；被 abort 不算失败
   */
  function stream(path, body, handlers, options) {
    var opts = options || {};
    var init = {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(body || {}),
    };
    if (opts.signal) init.signal = opts.signal;

    return fetch(BASE + path, init).then(
      function (response) {
        if (!response.ok) {
          // 开流之前的失败（没填密钥、超配额、不是你的会话）仍然带着正常的状态码，
          // 所以按普通请求处理：把 detail 拿出来抛给调用方去 toast
          return parseBody(response).then(function (data) {
            throw buildError(response, data);
          });
        }
        if (!response.body || !response.body.getReader) {
          throw new Error('当前浏览器不支持流式读取，请换一个较新的浏览器。');
        }
        return pump(response.body.getReader(), handlers).catch(function (err) {
          // 用户按了停止：读取会以 AbortError 结束，这是预期路径，不是失败
          if (err && err.name === 'AbortError') return;
          throw err;
        });
      },
      function (cause) {
        if (cause && cause.name === 'AbortError') return;
        var err = new Error('本地服务连不上，检查它是否在运行');
        err.status = 0;
        err.cause = cause;
        throw err;
      }
    );
  }

  var api = {
    base: BASE,
    stream: stream,
    request: request,
    get: function (path, options) {
      return request('GET', path, options);
    },
    post: function (path, body, options) {
      return request('POST', path, { body: body, allow401: (options || {}).allow401 });
    },
    patch: function (path, body, options) {
      return request('PATCH', path, { body: body, allow401: (options || {}).allow401 });
    },
    put: function (path, body, options) {
      return request('PUT', path, { body: body, allow401: (options || {}).allow401 });
    },
    del: function (path, options) {
      return request('DELETE', path, options);
    },
    /** 上传一个文件：走 multipart（附件接口用） */
    upload: function (path, file) {
      var form = new FormData();
      form.append('file', file, file.name || 'file');
      return request('POST', path, { form: form });
    },
  };

  /* ------------------------------------------------------ 具名接口 */

  // 这里原先有一整块 `api.auth`（me / login / register / logout / changePassword）。
  // 单用户本地形态下没有账号，整块删掉 —— 见 `api/app/deps.py`。

  api.health = function () {
    return request('GET', '/health', { allow401: true });
  };

  QF.api = api;
})();
