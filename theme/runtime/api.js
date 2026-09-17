/* ===========================================================================
 * api.js —— 后端接口封装
 *
 * 数据全部来自 /api，会话靠 HttpOnly Cookie；基址由构建期注入的
 * window.QF_CONFIG.apiBase 给出（默认 /api）。
 *
 * 统一在这里做三件事：拼基址、带 CSRF 头、把各种失败归一成
 * 带 status 的 Error —— 调用方只需要 try/catch，不用关心响应形态。
 * ========================================================================= */
(function () {
  'use strict';

  var QF = (window.QF = window.QF || {});

  var config = window.QF_CONFIG || {};
  var BASE = String(config.apiBase || '').replace(/\/+$/, '');

  QF.config = config;

  /** 未授权时的回调（由 boot.js 注入，默认跳登录页） */
  var unauthorizedHandler = null;

  function onUnauthorized(handler) {
    unauthorizedHandler = handler;
  }

  function csrfToken() {
    var match = document.cookie.match(/(?:^|;\s*)qf_csrf=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : '';
  }

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
    if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
    // 写请求必须带双提交令牌，服务端会与 Cookie 比对
    if (method !== 'GET' && method !== 'HEAD') headers['X-CSRF-Token'] = csrfToken();

    var init = {
      method: method,
      headers: headers,
      // 会话是 HttpOnly Cookie，必须带上；同源所以用 same-origin
      credentials: 'same-origin',
    };
    if (opts.body !== undefined) init.body = JSON.stringify(opts.body);
    if (opts.signal) init.signal = opts.signal;

    return fetch(BASE + path, init).then(
      function (response) {
        if (response.status === 204) return null;
        return parseBody(response).then(function (data) {
          if (response.ok) return data;
          if (response.status === 401 && !opts.allow401) {
            if (unauthorizedHandler) unauthorizedHandler();
          }
          throw buildError(response, data);
        });
      },
      function (cause) {
        // fetch 只在网络层失败时 reject（断网、被拦截、DNS 等）
        var err = new Error('网络不可用，请检查连接后重试');
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
   * 为什么不用 `EventSource`：它发不了 POST、带不了 CSRF 头、也带不了自定义头。
   * 用 fetch + ReadableStream 自己拆帧，图的是「可取消」与「可带凭据」。
   *
   * 与 `request()` 的差别只在成功路径上：那一个要的是整个 JSON，
   * 这一个要的是过程中陆续来的事件。失败路径完全一致（错误对象、401 回调）。
   *
   * @param {string} path      以 / 开头的接口路径
   * @param {object} body
   * @param {object} handlers  { user, start, delta, usage, done, error }
   * @param {object} [options] { signal, allow401 }
   * @returns {Promise<void>}  流结束（或被 abort）时 resolve；被 abort 不算失败
   */
  function stream(path, body, handlers, options) {
    var opts = options || {};
    var init = {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
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
            if (response.status === 401 && !opts.allow401) {
              if (unauthorizedHandler) unauthorizedHandler();
            }
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
        var err = new Error('网络不可用，请检查连接后重试');
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
    onUnauthorized: onUnauthorized,
    csrfToken: csrfToken,
  };

  /* ------------------------------------------------------ 具名接口 */

  api.auth = {
    /**
     * 探测当前登录状态。
     * 401 在这个接口上是**正常语义**（就是没登录），所以转成 null 返回，
     * 而不是抛异常 —— 否则每个调用点都要写一遍「401 不算错」。
     */
    me: function () {
      return request('GET', '/auth/me', { allow401: true }).catch(function (err) {
        if (err.status === 401) return null;
        throw err;
      });
    },
    login: function (email, password) {
      return request('POST', '/auth/login', {
        body: { email: email, password: password },
        allow401: true,
      });
    },
    register: function (email, password, displayName) {
      return request('POST', '/auth/register', {
        body: { email: email, password: password, displayName: displayName || '' },
        allow401: true,
      });
    },
    logout: function () {
      return request('POST', '/auth/logout', { allow401: true });
    },
    changePassword: function (currentPassword, newPassword) {
      return request('POST', '/auth/password', {
        body: { currentPassword: currentPassword, newPassword: newPassword },
      });
    },
  };

  api.health = function () {
    return request('GET', '/health', { allow401: true });
  };

  QF.api = api;
})();
