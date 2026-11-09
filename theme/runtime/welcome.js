/* welcome.js —— 新设备第一次打开时的那组引导：AI → 同步 → 数据密码。
 *
 * 为什么要有它（2026-11-09 用户）：
 * > 现在移动端还是靠用户自己填设置，自己复制 json。我认为需要做新用户的一组引导页面。
 *
 * 新设备此前有两个坑都栽在"配置不齐"上：没有 AI 密钥时对话页打不开；
 * 没有同步口令时对话只剩标题（消息在解密那一步被拦下，见 sync.py 的 PRIVATE_TABLES）。
 * 所以这三步就是"能用"的最小集合。
 *
 * **没有跳过**（用户 2026-11-09：不可以跳过引导，必须要先输入）。
 * 从前那句「以后再说，直接进入」是个洞：填了第一步就能走，口令那一步永远补不上，
 * 而缺口令的后果要到第一次读旧对话时才炸。现在每步的关键项都填了才放行。
 *
 * 版式（2026-11-09 重做）：一列、无卡片，每屏只有「题 / 一句话 / 控件」。
 * 步骤是三道小横线，不再把三个标题写成"1 · AI 2 · 同步 3 · 数据密码"重复一遍。
 *
 * 全部复用现有接口，没有新的后端：
 *   AI / Turso → 写进设置（`store.saveSettings`，输入即存）
 *   数据密码   → `POST /sync/passphrase`（只落本机 key 文件）
 *   完成       → `PUT /settings` 整份落盘 + 本机标记 `onboarding.done`
 * `onboarding` 在 LOCAL_ONLY 里：每台设备各做一次，不跟着同步走。
 */
(function () {
  var ui = QF.ui;
  var h = ui.h;
  var api = QF.api;
  var store = QF.store;

  /* 三步的题面。`sub` 只说"为什么"——"填什么"由字段标签自己讲，不重复一遍。 */
  var STEPS = [
    { title: '模型', sub: '对话与批改靠它；密钥只存在这台设备。' },
    { title: '同步', sub: '两台设备共用同一份数据。' },
    { title: '数据密码', sub: '端到端加密的钥匙；旧设备就填同一份。' },
  ];

  var root = null;
  var step = 0;
  /* 第三步的口令：`passValue` 是真正要落的那一份，`passMade` 只记"是我们生成的"
   * —— 只有生成的那份才摊开显示（手粘的那份不该突然从圆点变成明文）。 */
  var passValue = '';
  var passMade = false;
  var busy = false;
  /* 本机（`config/snapshot.key`）已经有了口令 —— 旧设备升级会走到这个引导，
   * 不能逼人家把手上的口令再输一遍（输错一次就把钥匙换掉了）。 */
  var hasKey = false;
  var nextBtn = null;
  /* 本机已有口令时，「换一份」点了才露出输入框 */
  var editing = false;

  function conf() {
    return store.settings() || {};
  }

  function aiConf() {
    return conf().ai || {};
  }

  function tursoConf() {
    return conf().turso || {};
  }

  function field(label, control) {
    return h('label.wl__field', null,
      h('span.wl__label', { text: label }),
      control);
  }

  function textInput(placeholder, value, set) {
    return h('input.input.input--mono', {
      type: 'text',
      spellcheck: 'false',
      placeholder: placeholder,
      value: value || '',
      // 输入即存（老做法），顺手把「下一步」的可用状态拨对 —— 只改按钮，
      // 不整页重画，否则每敲一个字光标都会跳回开头
      onInput: function (event) {
        set(event.target.value.trim());
        syncNext();
      },
    });
  }

  function secretInput(placeholder, value, set) {
    return h('input.input.input--mono', {
      type: 'password',
      autocomplete: 'off',
      placeholder: placeholder,
      value: value || '',
      onInput: function (event) {
        set(event.target.value.trim());
        syncNext();
      },
    });
  }

  function randomPass() {
    var bytes = new Uint8Array(24);
    var cryptoObj = window.crypto || window.msCrypto;
    cryptoObj.getRandomValues(bytes);
    var out = '';
    for (var i = 0; i < bytes.length; i += 1) {
      out += bytes[i].toString(16).padStart(2, '0');
    }
    return out;
  }

  /* 从剪贴板读一把密钥。也认整份设置（设置页「复制设置」那份 JSON 粘过来，
   * AI / Turso / 口令一次带走）—— 换设备时这才是最省事的一条路。
   *
   * 两个坑都在这儿处理了：Android 的 WebView 里 `navigator.clipboard` 拿不到
   * （要安全上下文 + 权限回调，2026-11-09 实测），所以打包版走原生 `QFNative`；
   * 浏览器那边的 `readText()` 必须由用户手势触发 —— 这个按钮的 onClick 就是手势。 */
  function readClipboard() {
    var native = window.QFNative && window.QFNative.readClipboard ? window.QFNative : null;
    var reading;
    if (native) {
      reading = Promise.resolve(String(native.readClipboard() || ''));
    } else if (navigator.clipboard && navigator.clipboard.readText) {
      reading = navigator.clipboard.readText();
    } else {
      reading = Promise.reject(new Error('这个浏览器读不了剪贴板'));
    }

    reading.then(function (text) {
      var raw = String(text || '').trim();
      if (!raw) throw new Error('剪贴板是空的');

      var data = null;
      try { data = JSON.parse(raw); } catch (err) { data = null; }
      if (data && typeof data === 'object' && (data.ai || data.turso)) {
        var pass = String(data.syncPassphrase || '').trim();
        delete data.syncPassphrase;
        store.saveSettings(data);
        if (pass) {
          passValue = pass;
          passMade = false;
        }
        render();
        ui.toast('已从剪贴板填入设置', 'ok');
        return;
      }

      // 不是整份设置，就当它是一把密钥（粘贴时常带引号或换行）
      var key = raw.replace(/^["']|["']$/g, '').replace(/\s+/g, '');
      if (!key) throw new Error('剪贴板里没有密钥');
      store.saveSettings({ ai: { apiKey: key } });
      render();
      ui.toast('已从剪贴板填入密钥', 'ok');
    }).catch(function (err) {
      ui.toast('没读到：' + ((err && err.message) || err), 'error');
    });
  }

  function copyText(text) {
    var native = window.QFNative;
    if (native && native.writeClipboard) {
      native.writeClipboard(text);
      ui.toast('已复制', 'ok');
      return;
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        ui.toast('已复制', 'ok');
      }, function () {
        ui.toast('复制失败：浏览器不给剪贴板权限', 'error');
      });
      return;
    }
    ui.toast('这个浏览器没有剪贴板接口', 'error');
  }

  /* ---------------------------------------------------------------- 三步 */

  function aiPanel() {
    var ai = aiConf();
    return [
      h('div.wl__group', null,
        field('密钥', secretInput('sk-…', ai.apiKey,
          function (value) { store.saveSettings({ ai: { apiKey: value } }); })),
        h('button.wl__link', {
          type: 'button',
          onClick: readClipboard,
        }, '从剪贴板读取')),
      field('接口地址', textInput('https://api.deepseek.com/v1', ai.baseUrl,
        function (value) { store.saveSettings({ ai: { baseUrl: value } }); })),
      field('模型', textInput('deepseek-chat', ai.model,
        function (value) { store.saveSettings({ ai: { model: value } }); })),
    ];
  }

  function syncPanel() {
    var turso = tursoConf();
    return [
      field('Turso 地址', textInput('libsql://…', turso.url,
        function (value) { store.saveSettings({ turso: { url: value } }); })),
      field('令牌', secretInput('令牌', turso.token,
        function (value) { store.saveSettings({ turso: { token: value } }); })),
    ];
  }

  function passPanel() {
    // 本机已经有口令：不逼人重输，给一个「换一份」的口子
    if (hasKey && !editing && !passMade) {
      return [
        h('button.btn.btn--ghost.wl__self', {
          type: 'button',
          onClick: function () {
            editing = true;
            render();
          },
        }, '换一份口令'),
      ];
    }

    // 我们生成的那一份：摊开显示，能复制，也能再换一个
    if (passMade) {
      return [
        h('input.input.input--mono', {
          type: 'text',
          readOnly: true,
          value: passValue,
          'aria-label': '数据密码',
        }),
        h('div.wl__row', null,
          h('button.btn.btn--ghost', {
            type: 'button',
            onClick: function () { copyText(passValue); },
          }, '复制'),
          h('button.btn.btn--ghost', {
            type: 'button',
            onClick: function () {
              passValue = randomPass();
              render();
            },
          }, '换一个')),
        h('p.wl__note', { text: '只显示这一次：丢了它，旧对话就读不出来。' }),
      ];
    }

    // 手粘的那一份
    return [
      field('与旧设备同一份', secretInput('口令', passValue,
        function (value) { passValue = value; })),
      h('button.btn.btn--ghost.wl__self', {
        type: 'button',
        onClick: function () {
          passValue = randomPass();
          passMade = true;
          render();
        },
      }, '或生成一份新的'),
    ];
  }

  var PANELS = [aiPanel, syncPanel, passPanel];

  /* 这一步算不算填完了。判据只看「能不能用」的那一项：
   *   AI   → 密钥（地址与模型有默认值，不填也是对的）
   *   同步 → 地址与令牌成对（只有一半等于没配）
   *   口令 → 粘来的、现场生成的、或本机早就有的，有一样就行 */
  function ready() {
    if (step === 0) return !!String(aiConf().apiKey || '').trim();
    if (step === 1) {
      return !!String(tursoConf().url || '').trim() && !!String(tursoConf().token || '').trim();
    }
    return hasKey || !!String(passValue || '').trim();
  }

  /* 输入时只拨按钮，不重画整页（重画会把光标顶到输入框开头）。 */
  function syncNext() {
    if (nextBtn) nextBtn.disabled = busy || !ready();
  }

  /* 三道小横线：走过的暗一点，当前这道是主题色。不写步骤名 —— 标题就是它。 */
  function stepsBar() {
    return h('ol.wl__steps', { 'aria-label': '第 ' + (step + 1) + ' 步，共 ' + STEPS.length + ' 步' },
      STEPS.map(function (item, index) {
        // 标签名后面必须跟 `.` —— 写成 `'li' + 'wl__step'` 会造出一个
        // 叫 `liw__step` 的未知元素（旧代码就是这样，所以那排"步骤"从来没上过样式）
        var cls = 'li.wl__step'
          + (index === step ? '.is-on' : '')
          + (index < step ? '.is-done' : '');
        return h(cls);
      }));
  }

  function foot() {
    var back = step > 0
      ? h('button.btn.btn--ghost', {
        type: 'button',
        onClick: function () {
          step -= 1;
          render();
        },
      }, '上一步')
      : null;
    nextBtn = h('button.btn.btn--primary', {
      type: 'button',
      disabled: busy || !ready(),
      onClick: advance,
    }, step === STEPS.length - 1 ? '完成并进入' : '下一步');
    return h('div.wl__foot', null, back, h('span.wl__grow'), nextBtn);
  }

  function advance() {
    if (busy || !ready()) return;
    if (step < STEPS.length - 1) {
      step += 1;
      render();
      return;
    }
    finish();
  }

  /* 完成：先落口令（如果有），再整份落设置，最后写本机标记并进入主页。
   * 顺序不能反：`run_full` 是后台线程，先有口令再触发同步才能一次拉对。 */
  function finish() {
    if (busy) return;
    busy = true;
    render();

    // 没输入只可能是一种情况：本机本来就有一份口令（见 ready 里的 hasKey）。
    // 那时**不要**拿空串去覆盖它 —— 其余情况 ready() 已经把人拦在上一步了。
    var pass = String(passValue || '').trim();
    var job = pass ? api.post('/sync/passphrase', { passphrase: pass }) : Promise.resolve();
    job.then(function () {
      return api.put('/settings', { settings: store.fileSettings(), replace: true });
    }).then(function () {
      store.saveSettings({ onboarding: { done: true, at: Date.now() } });
      window.location.replace('workbench.html');
    }).catch(function (err) {
      busy = false;
      render();
      ui.toast('没保存成功：' + ((err && err.message) || err), 'error');
    });
  }

  function render() {
    ui.clear(root);
    var meta = STEPS[step];
    var sub = step === 2 && hasKey && !editing && !passMade
      ? '这台设备已经有口令，直接完成即可。'
      : meta.sub;
    root.appendChild(h('div.wl', null,
      stepsBar(),
      h('h1.wl__title', { text: meta.title }),
      h('p.wl__sub', { text: sub }),
      h('div.wl__form', null, PANELS[step]()),
      foot()));
  }

  function boot() {
    root = document.getElementById('app-root');
    if (!root) return;
    // 引导是一条路走到底：不给活动栏/底栏（那些入口会把人弹回这一页）
    QF.shell.mount({ bare: true });
    render();

    // 两件事并行，落地后重画一次：
    //   ① 服务端那份设置 —— 新机或清过缓存时 localStorage 是空的，但密钥与
    //      Turso 往往还在服务端。不灌进来就会逼人把手上已有的东西重敲一遍。
    //   ② 本机有没有口令（旧设备 / 重装但 key 还在）—— 只读状态，不动文件。
    var pulled = QF.shell.pullSettings ? QF.shell.pullSettings() : Promise.resolve(true);
    var keyState = api.get('/sync/status').then(function (status) {
      hasKey = !!(status && status.keyReady);
    }).catch(function () { /* 读不到就当没有：那一步会要求真的输入 */ });
    Promise.all([pulled, keyState]).then(function () { render(); });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
