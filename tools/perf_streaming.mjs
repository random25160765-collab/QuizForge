/* ===========================================================================
 * 流式渲染压测（对话页）
 *
 * 量的是"真流式到达时，前端跟不跟得上"：把"发送消息"的请求**拦在浏览器里**，
 * 用一段自制的慢速 SSE 流喂前端 —— 不碰服务端、不落库、不花一分钱。
 *
 * 运行（先起好本地服务、构建过前端）：
 *     node tools/perf_streaming.mjs              # 默认 http://127.0.0.1:8100
 *     node tools/perf_streaming.mjs http://127.0.0.1:8100
 *     PLAYWRIGHT_CORE=/path/to/playwright-core node tools/perf_streaming.mjs
 *
 * 读三个数：
 *   totalMs     点发送 → 画完
 *   upstreamMs  上游吐完（脚本自己造的流，固定约 4.5 秒）
 *   tailMs      **拖尾**：上游吐完 → 页面画完（越小越好）
 *   rafOver100  帧间隔超过 100ms 的次数（页面被渲染任务冻结的迹象）
 *
 * 历史：2026-09-20 首次用它量出"拖尾 2s+、整个流式期间一次都不渲染"——
 * 根因是那个 100ms 的节流写成了防抖（每个 chunk 都重设计时器、永不触发）。
 *
 * 注：playwright-core 是外部依赖，脚本**不猜**它装在哪 —— 先看你有没有用
 * PLAYWRIGHT_CORE 显式指，再退回按包名解析。见 resolvePlaywright()。
 * ========================================================================= */
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);

function resolvePlaywright() {
  const tries = [
    process.env.PLAYWRIGHT_CORE,
    'playwright-core',
  ];
  for (const one of tries) {
    if (!one) continue;
    try {
      return require(one);
    } catch (e) {
      /* 换下一个 */
    }
  }
  throw new Error('找不到 playwright-core：用 PLAYWRIGHT_CORE=<目录> 指一下');
}

const { chromium } = resolvePlaywright();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const BASE = process.argv[2] || 'http://127.0.0.1:8100';

/* 150 块 / 约 1.5 万字 / 三处公式 + 一段代码块：专打渲染的痛处 */
const CHUNKS = [];
for (let i = 0; i < 150; i++) {
  let t = '';
  for (let k = 0; k < 4; k++) t += '第' + i + '段：流式渲染压测的正文，随便写点字凑长度。';
  if (i === 30) t += '公式 $E = mc^2$ 与 $\\int_0^1 x^2\\,dx$ 插在这里。';
  if (i === 75) t += '\n\n```python\nfor i in range(3):\n    print(i)\n```\n\n';
  if (i === 110) t += '行内公式 $\\alpha+\\beta=\\gamma$ 收尾。';
  CHUNKS.push(t);
}

(async () => {
  const browser = await chromium.launch();
  const page = await (await browser.newContext({ viewport: { width: 1360, height: 900 } })).newPage();
  const errs = [];
  page.on('pageerror', (e) => errs.push(e.message));

  await page.addInitScript((chunks) => {
    var USER = { id: 999001, conversationId: 'perf', parentId: null, role: 'user', content: '压测', status: 'ok', parts: [] };
    var START = { id: 999002, conversationId: 'perf', parentId: 999001, role: 'assistant', content: '', status: 'streaming', model: 'perf', parts: [] };
    var DONE = { id: 999002, conversationId: 'perf', parentId: 999001, role: 'assistant', content: '', status: 'ok', model: 'perf', latencyMs: 4500, promptTokens: 12, completionTokens: 600, parts: [] };

    // 帧间隔采样：页面被渲染任务冻住时 rAF 会停摆，最大间隔就是用户感到的"卡"
    window.__raf = [];
    (function loop(last) {
      window.requestAnimationFrame(function (now) {
        window.__raf.push(Math.round(now - last));
        if (window.__raf.length < 20000) loop(now);
      });
    })(performance.now());

    var origFetch = window.fetch;
    window.fetch = function (url, init) {
      var u = String(url);
      if (u.indexOf('/messages') >= 0 && init && String(init.method || '').toUpperCase() === 'POST') {
        var enc = new TextEncoder();
        var index = 0;
        var stream = new ReadableStream({
          start: function (controller) {
            controller.enqueue(enc.encode('event: user\ndata: ' + JSON.stringify(USER) + '\n\n'));
            controller.enqueue(enc.encode('event: start\ndata: ' + JSON.stringify(START) + '\n\n'));
            var timer = setInterval(function () {
              if (index < chunks.length) {
                controller.enqueue(enc.encode('event: delta\ndata: ' + JSON.stringify({ text: chunks[index] }) + '\n\n'));
                index += 1;
                return;
              }
              clearInterval(timer);
              window.__streamEnd = performance.now(); // 上游"吐完"的时刻（对齐用）
              controller.enqueue(enc.encode('event: done\ndata: ' + JSON.stringify(DONE) + '\n\n'));
              controller.close();
            }, 30); // 150 块 × 30ms ≈ 上游吐 4.5 秒
          },
        });
        return Promise.resolve(
          new Response(stream, { status: 200, headers: { 'content-type': 'text/event-stream' } })
        );
      }
      return origFetch.apply(window, arguments);
    };
  }, CHUNKS);

  await page.goto(BASE + '/chat.html', { waitUntil: 'domcontentloaded' });
  await sleep(4200);
  const opened = await page.evaluate(() => {
    const it = document.querySelector('.chatlist__item');
    if (it) it.click();
    return !!it;
  });
  await sleep(2200);
  if (!opened) console.log('（左栏没有会话可打开 —— 压测照跑，它不碰真实数据）');

  const t0 = await page.evaluate(() => performance.now() + 0);
  await page.fill('.chat__input', '压测：慢慢说');
  await page.click('.chat__send');

  // caret 要**先出现**再等消失（只在首个 delta 之后才出现；
  // 把"还没出现"当成"已结束"会数出一个假数字 —— 第一版就是这么错的）
  let appeared = false;
  for (let i = 0; i < 48; i++) {
    await sleep(125);
    appeared = await page.evaluate(() => !!document.querySelector('.chat__caret'));
    if (appeared) break;
  }
  let gone = false;
  for (let i = 0; i < 900; i++) {
    await sleep(100);
    gone = await page.evaluate(() => !document.querySelector('.chat__caret'));
    if (gone) break;
  }
  await sleep(400);

  const res = await page.evaluate((start) => {
    const rows = document.querySelectorAll('.chatmsg--assistant');
    const last = rows[rows.length - 1];
    const text = last ? (last.querySelector('.chatmsg__parts') || last).textContent || '' : '';
    const raf = (window.__raf || []).slice();
    raf.sort((a, x) => x - a);
    return {
      totalMs: Math.round(performance.now() - start),
      upstreamMs: window.__streamEnd ? Math.round(window.__streamEnd - start) : null,
      tailMs: window.__streamEnd ? Math.round(performance.now() - window.__streamEnd) : null,
      chars: text.replace(/\s+/g, '').length,
      rafTop: raf.slice(0, 6),
      rafOver100: (window.__raf || []).filter((x) => x > 100).length,
    };
  }, t0);

  console.log('生成期间有渲染（caret 出现过）:', appeared);
  console.log('结果:', JSON.stringify(res));
  console.log('JS 错误:', errs.length ? errs.slice(0, 2) : '（无）');
  await browser.close();
  process.exit(gone ? 0 : 1);
})().catch((e) => {
  console.log('failed:', String(e.stack || e).slice(0, 300));
  process.exit(1);
});
