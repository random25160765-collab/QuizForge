/* ===========================================================================
 * api.js —— 后端接口封装
 *
 * 两种运行模式由构建期注入的 window.QF_CONFIG 决定：
 *   offline（默认）：没有 apiBase，题库由内联的 window.__QB__ 提供，
 *                    本模块基本不会被调用，但保留以便代码路径统一。
 *   online         ：所有数据来自 /api，会话靠 HttpOnly Cookie。
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
  QF.online = config.mode === 'online';

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

  var api = {
    base: BASE,
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
