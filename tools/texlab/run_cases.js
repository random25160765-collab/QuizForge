/**
 * 图表引擎的回归套件（跑法：`make cases`）。
 *
 * 为什么要有它：这两天接连撞上各种"这张图编不出来" —— 引擎限制、前言缺包、模型写错
 * 混在一起，靠一条条正则去堵是**打地鼠**（用户原话："不能一直靠正则给 llm 擦屁股"）。
 * 规矩从此定死：**每加一处兜底，必须在这里带一条用例**；没有用例的改写一律不加。
 *
 * 这一份是 `playwright-cli run-code` 的脚本体（被 `make cases` 用 `cat` 送进去），所以：
 *   * 用例由 `make cases` 拷进产物根、页面**同源**取（`/cases.json`）—— `run-code` 那个上下文里
 *     既没有 `require` 也没法跨源 fetch（实测：`ReferenceError: require is not defined`）；
 *   * 渲染走**应用**（`chat.html` 的 `QF.latex.slot/fill`）—— 于是套件覆盖的是应用真正那条路：
 *     摘前言命令、按需套 CJK、按样式键补库、中文坐标键改写、日志桥、缓存……全都在里面。
 * 因此跑之前先 `make web`（目标已依赖它），并且**应用服务（8100）在跑**。
 */
async page => {
  const APP_URL = 'http://127.0.0.1:8100/chat.html';

  /* 用例**从页面自己的源取**（`make cases` 会把 `tools/texlab/cases.json` 拷进产物根）。
   *
   * 为什么不在这儿直接读文件：`playwright-cli run-code` 那个上下文里**没有 `require`**，
   * 也没法可靠地 `fetch` 跨源的东西（实测：`ReferenceError: require is not defined`）。
   * 页面里就没有这些麻烦 —— 同源、原生 `fetch`。 */
  let data;
  try {
    data = await page.evaluate(() => fetch('/cases.json').then(r => r.json()));
  } catch (err) {
    return [
      {
        name: '(取用例失败)',
        expect: 'ok',
        got: 'fail',
        ms: 0,
        pass: false,
        why: '拿不到 /cases.json：' + String((err && err.message) || err) + ' —— 先 `make web` 并确认 8100 在跑',
      },
    ];
  }

  const rows = [];
  for (const one of data.cases) {
    // 每条用例都**重开页面**：引擎卡过一次的工位会一直卡，混着跑就没意义了（我自己栽过两次）
    await page.goto(APP_URL, { waitUntil: 'load' });
    await page.evaluate(() => new Promise(r => setTimeout(r, 900)));
    const res = await page.evaluate(async tex => {
      // 清缓存：命中缓存就量不到引擎了
      for (let i = localStorage.length - 1; i >= 0; i--) {
        const k = localStorage.key(i);
        if (k && k.indexOf('qf.latex.') === 0) localStorage.removeItem(k);
      }
      const stage = document.createElement('div');
      stage.style.cssText = 'position:fixed;left:0;top:0;z-index:2147483600';
      document.body.appendChild(stage);
      const box = document.createElement('div');
      box.innerHTML = window.QF.latex.slot(tex);
      stage.appendChild(box);
      const t0 = performance.now();
      window.QF.latex.fill(box);
      let ok = false;
      let why = '';
      while (performance.now() - t0 < 45000) {
        const svg = box.querySelector('svg');
        if (svg && !/tikzjax-broken|tikzjax-loader/.test(svg.getAttribute('class') || '')) {
          ok = true;
          break;
        }
        const txt = (box.textContent || '').replace(/\s+/g, ' ');
        if (/编不出|卡住/.test(txt)) {
          why = txt;
          break;
        }
        await new Promise(r => setTimeout(r, 30));
      }
      if (!why) why = (box.textContent || '').replace(/\s+/g, ' ');
      const ms = Math.round(performance.now() - t0);
      box.remove();
      stage.remove();
      return { ok: ok, why: why, ms: ms };
    }, one.tex);

    const got = res.ok ? 'ok' : 'fail';
    const want = one.expect;
    const needleOk = !one.needle || res.why.indexOf(one.needle) >= 0;
    rows.push({
      name: one.name,
      expect: want,
      got: got,
      ms: res.ms,
      pass: got === want && needleOk,
      why: got === want && needleOk ? '' : res.why.slice(0, 200),
    });
  }
  return rows;
}
