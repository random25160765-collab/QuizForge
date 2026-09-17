/* ===========================================================================
 * quizforge 自测（无需浏览器）
 *
 * 用最小 DOM 垫片在 Node 里加载**真实的运行时模块**，验证：
 *   LaTeX 占位保护、Markdown 子集渲染、语法高亮、四种题型的判分、
 *   SM2 调度、localStorage 持久化与导入导出、以及全量题目的渲染与判分。
 *
 * 运行（需要一份题库 JSON）：
 *     QF_BANK_JSON=/path/to/bank.json node tools/selftest.mjs
 * 或：
 *     make test（会自己从库物化并导出一份）
 * ========================================================================= */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

/* ----------------------------------------------------------- DOM 垫片 */

function escText(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

class Node_ {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.attrs = {};
    this._class = new Set();
    this.style = {};
    this.dataset = {};
    this._text = null;
    this._html = null;
    this.value = '';
    this.listeners = {};
  }
  get classList() {
    const s = this._class;
    return {
      add: (...cs) => cs.forEach((c) => String(c).split(/\s+/).filter(Boolean).forEach((x) => s.add(x))),
      remove: (...cs) => cs.forEach((c) => s.delete(c)),
      contains: (c) => s.has(c),
      toggle: (c, force) => {
        const on = force === undefined ? !s.has(c) : !!force;
        if (on) s.add(c);
        else s.delete(c);
        return on;
      },
    };
  }
  set className(v) { this._class = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return [...this._class].join(' '); }
  setAttribute(k, v) { if (k === 'class') this.className = v; else this.attrs[k] = v; }
  getAttribute(k) { return k === 'class' ? this.className : this.attrs[k]; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; }
  insertBefore(n, ref) {
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(n); else this.children.splice(i, 0, n);
    n.parentNode = this; return n;
  }
  removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); return c; }
  get firstChild() { return this.children[0] || null; }
  get childNodes() { return this.children; }
  get firstElementChild() { return this.children.find((c) => c instanceof Node_) || null; }
  get lastElementChild() { return [...this.children].reverse().find((c) => c instanceof Node_) || null; }
  get textContent() {
    if (this._text !== null) return this._text;
    return this.children.map((c) => c.textContent || '').join('');
  }
  set textContent(v) { this._text = String(v); this.children = []; }
  get innerHTML() { return this._html !== null ? this._html : this.children.map((c) => c.outerHTML).join(''); }
  set innerHTML(v) { this._html = String(v); this.children = []; }
  get outerHTML() {
    if (this.tagName === '#TEXT') return escText(this._text || '');
    const attrs = [];
    if (this.className) attrs.push(`class="${this.className}"`);
    for (const [k, v] of Object.entries(this.attrs)) attrs.push(`${k}="${v}"`);
    const tag = this.tagName.toLowerCase();
    return `<${tag}${attrs.length ? ' ' + attrs.join(' ') : ''}>${this.innerHTML}</${tag}>`;
  }
  querySelector() { return null; }
  closest() { return null; }
  focus() {}
  click() {}
}

const textNode = (t) => { const n = new Node_('#TEXT'); n._text = String(t); return n; };

globalThis.Node = Node_;
globalThis.window = globalThis;
globalThis.document = {
  createElement: (tag) => new Node_(tag),
  createTextNode: (t) => textNode(t),
  addEventListener() {},
  getElementById() { return null; },
  querySelector() { return null; },
  documentElement: new Node_('html'),
  readyState: 'complete',
  execCommand() { return true; },
  body: new Node_('body'),
};
globalThis.requestAnimationFrame = (f) => setTimeout(f, 0);
globalThis.setTimeout = setTimeout;
globalThis.clearTimeout = clearTimeout;

const mem = new Map();
globalThis.localStorage = {
  getItem: (k) => (mem.has(k) ? mem.get(k) : null),
  setItem: (k, v) => mem.set(k, String(v)),
  removeItem: (k) => mem.delete(k),
};

/* 题库从环境变量给的路径读 —— 由 `make test` 从库物化并导出。
   以前读 dist/data.json：那是离线构建的产物，离线形态已淘汰。
   刻意不给默认值：直接跑这个脚本时必须显式指定，否则很容易拿一份
   上一次留下的旧题库跑出一片绿，而库里其实已经变了。 */
const bankPath = process.env.QF_BANK_JSON;
if (!bankPath) {
  console.error('缺少 QF_BANK_JSON：请给一份题库 JSON 路径（`make test` 会自动准备）');
  process.exit(2);
}
const bank = JSON.parse(fs.readFileSync(bankPath, 'utf8'));

/* KaTeX 桩：把 tex 原样保留，便于断言 LaTeX 未被 Markdown 破坏 */
globalThis.katex = {
  renderToString(tex, opts) {
    return `<span class="KATEX" data-display="${!!(opts && opts.displayMode)}">${escText(tex)}</span>`;
  },
};

/* ------------------------------------------------------------- 加载 */

const RUNTIME = ['ui.js', 'data.js', 'md.js', 'highlight.js', 'engine.js', 'store.js', 'sm2.js'];
for (const f of RUNTIME) {
  const code = fs.readFileSync(path.join(ROOT, 'theme/runtime', f), 'utf8');
  // 用间接 eval 保证在全局作用域执行，模块内 var QF 挂到 window 上
  (0, eval)(code.replace(/^'use strict';$/m, ''));
}

const QF = globalThis.QF;
const { engine, md, highlight, sm2, store, data } = QF;

// 题库装载：真实路径是 boot.js 拿到 GET /api/bank 之后调用 install()。
// 这里调同一个入口 —— 自测不复制那条逻辑，只模拟它的调用时机。
data.install(bank);

/* 取样自题库，断言里不钉死具体学科与题号：
   题库内容会变（早期凑数的学科已被删除），钉死会让自测跟着内容一起红。 */
const SAMPLE_SUBJECT = data.subjects[0];
const SAMPLE_LEAF =
  data.topics.find((t) => t.leaf && t.path[0] === SAMPLE_SUBJECT.key) || data.topics[0];
const SAMPLE_UNIT =
  data.topics.find((t) => t.path.length === 2 && t.path[0] === SAMPLE_SUBJECT.key) || SAMPLE_LEAF;
const SAMPLE_SINGLE =
  data.questions.find((q) => q.type === 'single' && (q.options || []).length >= 2) || data.questions[0];
const SUBJECT_COUNT = data.subjects.length;
const SUBJECT_TOTAL = data.bankStats.byTopic[SAMPLE_SUBJECT.key];
const SAMPLE_ID = SAMPLE_SINGLE.id;

let pass = 0;
const failures = [];
function ok(name, cond, extra) {
  if (cond) { pass += 1; return; }
  failures.push(name + (extra ? ' :: ' + extra : ''));
}
function eq(name, actual, expected) {
  ok(name, JSON.stringify(actual) === JSON.stringify(expected), `got ${JSON.stringify(actual)} want ${JSON.stringify(expected)}`);
}

console.log('加载模块:', RUNTIME.join(', '));
console.log('题库:', data.questions.length, '题 ·', data.topics.length, '主题\n');

/* ------------------------------------------------- 1. LaTeX 保护与 Markdown */

const latexCases = [
  ['$a_b$', 'a_b'],
  ['$x^2 + y_1$', 'x^2 + y_1'],
  ['公式 $O(n\\log n)$ 结束', 'O(n\\log n)'],
  ['$\\frac{a}{b}$ 与 $\\sum_{i=1}^{n} i$', '\\frac{a}{b}'],
];
for (const [src, wantTex] of latexCases) {
  const html = md.renderInline(src);
  ok(`LaTeX 原样送达 KaTeX: ${src}`, html.includes(`>${escText(wantTex)}<`), html.slice(0, 200));
  ok(`LaTeX 未被 Markdown 破坏: ${src}`, !/<em>|<\/em>/.test(html), html.slice(0, 200));
}

const disp = md.render('行前\n\n$$\\sum_{i=1}^{n} i^2$$\n\n行后');
ok('块级公式渲染为 display', disp.outerHTML.includes('data-display="true"'));
ok('块级公式包裹 mathblock', disp.outerHTML.includes('mathblock'));

const codeMd = md.render('```cpp\nstd::vector<int> v;  // 注释\n```');
ok('围栏代码块渲染为 codeblock', codeMd.outerHTML.includes('codeblock'));
ok('代码块内出现高亮 token', codeMd.outerHTML.includes('tok-'));
ok('代码块内 ## 不被当作小节（渲染层面）', !codeMd.outerHTML.includes('md__h'));

const listMd = md.render('- 第一项\n- 第二项\n  - 嵌套项');
ok('无序列表渲染', listMd.outerHTML.includes('<ul') && listMd.outerHTML.includes('<li'));

const tableMd = md.render('| A | B |\n| --- | --- |\n| 1 | 2 |');
ok('表格渲染', tableMd.outerHTML.includes('<table') && tableMd.outerHTML.includes('<th'));

const escaped = md.renderInline('<script>alert(1)</script>');
ok('HTML 注入被转义', escaped.includes('&lt;script&gt;') && !escaped.includes('<script>'));

const inlineCode = md.renderInline('用 `v.push_back(1)` 试试');
ok('行内代码渲染为 icode', inlineCode.includes('class="icode"'));
ok('行内代码内的 $ 不当作公式', md.renderInline('`$x$`').includes('$x$'));

const quoteMd = md.render('> 引用一行');
ok('引用块渲染', quoteMd.outerHTML.includes('blockquote'));

/* ----------------------------------------------------- 2. 语法高亮 */

const hlSamples = {
  cpp: 'template <typename T> class Foo { std::vector<T> v; };',
  cuda: '__global__ void k(float* p) { int i = blockIdx.x * blockDim.x + threadIdx.x; }',
  ptx: 'ld.global.f32 %f1, [%rd5];\nmov.u32 %r2, %ctaid.x;',
  rust: 'fn main() { let mut v: Vec<i32> = vec![1, 2]; println!("{v:?}"); }',
  python: 'class A:\n    def f(self) -> int:\n        return len(self.x)  # note',
  bash: 'for f in *.md; do echo "$f"; done',
};
for (const [lang, src] of Object.entries(hlSamples)) {
  const html = highlight.code(src, lang);
  ok(`高亮 ${lang} 产出 token`, html.includes('tok-'), html.slice(0, 120));
  ok(`高亮 ${lang} 输出无裸 <`, !/<(?!\/?span)/.test(html), html.slice(0, 200));
  ok(`高亮 ${lang} 保留原文（去标签后一致）`,
    html.replace(/<\/?span[^>]*>/g, '').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&') === src,
    JSON.stringify(html.replace(/<\/?span[^>]*>/g, '')));
}
ok('PTX 寄存器被识别', highlight.code('mov.u32 %r2, %ctaid.x;', 'ptx').includes('tok-reg'));
ok('未知语言退化为转义', highlight.code('a < b', 'nosuchlang') === 'a &lt; b');

/* ------------------------------------------------------- 3. 判分引擎 */

const qSingle = { id: 't1', type: 'single', answer: 'B', options: [{ key: 'A', text: 'a' }, { key: 'B', text: 'b' }] };
eq('single 正确', engine.grade(qSingle, 'B').status, 'correct');
eq('single 错误', engine.grade(qSingle, 'A').status, 'wrong');
eq('single 空答', engine.grade(qSingle, '').status, 'empty');

const qMulti = { id: 't2', type: 'multi', answer: ['A', 'C'] };
eq('multi 全对', engine.grade(qMulti, ['A', 'C']).status, 'correct');
eq('multi 漏选为 partial', engine.grade(qMulti, ['A']).status, 'partial');
eq('multi 多选为 wrong', engine.grade(qMulti, ['A', 'C', 'D']).status, 'wrong');
ok('multi 漏选给部分分', Math.abs(engine.grade(qMulti, ['A']).score - 0.5) < 1e-9);
eq('multi 空答', engine.grade(qMulti, []).status, 'empty');

const qBlank = { id: 't3', type: 'blank', answer: [{ accept: ['noexcept'], regex: [] }, { accept: [], regex: ['^有效.{0,8}$'] }] };
eq('blank 全对', engine.grade(qBlank, ['noexcept', '有效但未指定']).status, 'correct');
eq('blank 部分对', engine.grade(qBlank, ['noexcept', '错的东西']).status, 'partial');
eq('blank 全错', engine.grade(qBlank, ['x', 'y']).status, 'wrong');
eq('blank 空答', engine.grade(qBlank, ['', '']).status, 'empty');
eq('blank 大小写敏感（默认）', engine.grade(qBlank, ['noexcept2', '有效']).status, 'partial');
eq('blank 正则匹配生效', engine.grade({ id: 't3b', type: 'blank', answer: [{ accept: [], regex: ['^\\d+$'] }] }, ['42']).status, 'correct');
eq('blank 去引号/分号', engine.grade({ id: 't3c', type: 'blank', answer: [{ accept: ['std::vector'], regex: [] }] }, [' `std::vector`; ']).status, 'correct');
eq('blank caseSensitive=false 生效',
  engine.grade({ id: 't3d', type: 'blank', answer: [{ accept: ['special'], regex: [] }], meta: { caseSensitive: false } }, ['Special']).status,
  'correct');
eq('short 不走自动判分', engine.grade({ id: 't4', type: 'short', rubric: [] }, '随便写').status, 'ungraded');
eq('toGrade: 正确→2', engine.toGrade({ status: 'correct' }), 2);
eq('toGrade: 部分→1', engine.toGrade({ status: 'partial' }), 1);
eq('toGrade: 错误→0', engine.toGrade({ status: 'wrong' }), 0);
ok('isResponseEmpty 识别空白', engine.isResponseEmpty(qBlank, ['', '  ']) === true);
ok('isResponseEmpty 识别非空', engine.isResponseEmpty(qBlank, ['', 'x']) === false);

/* --------------------------------------------------------- 4. SM2 */

let s = sm2.fresh(Date.now());
const s0 = s;
s = sm2.update(s, 2);
eq('SM2 good 首答 interval=1', s.interval, 1);
eq('SM2 不改动入参', s0.interval, 0);
s = sm2.update(s, 2);
eq('SM2 good 第二次 interval=6', s.interval, 6);
s = sm2.update(s, 2);
ok('SM2 good 第三次 interval 按 ef 放大', s.interval > 6, String(s.interval));
const before = s.interval;
const reset = sm2.update(s, 0);
eq('SM2 again 重置 reps', reset.reps, 0);
eq('SM2 again 累加 lapses', reset.lapses, 1);
ok('SM2 again 间隔大幅缩短', reset.interval < before);
ok('SM2 ef 下限 1.3', sm2.update(sm2.fresh(), 0).ef >= 1.3);
ok('SM2 ef 上限 2.8', sm2.update(sm2.fresh(), 3).ef <= 2.8);
ok('humanInterval 可读', sm2.humanInterval(1).includes('天'), sm2.humanInterval(1));

/* ------------------------------------------------- 5. store 持久化 */

const stats0 = store.stats();
ok('store 初始 total 等于题库规模', stats0.total === data.questions.length, String(stats0.total));
eq('store 初始无错题', store.wrongIds().length, 0);

const wrongKey = SAMPLE_SINGLE.options.map((o) => o.key).find((k) => k !== SAMPLE_SINGLE.answer);
store.applyResult(SAMPLE_SINGLE, wrongKey, engine.grade(SAMPLE_SINGLE, wrongKey));
eq('答错后进入错题本', store.wrongIds(), [SAMPLE_ID]);
eq('记录累加 wrong', store.record(SAMPLE_ID).wrong, 1);
ok('答错后已排入复习计划', store.record(SAMPLE_ID).sm2.due > 0);

store.applyResult(SAMPLE_SINGLE, SAMPLE_SINGLE.answer, engine.grade(SAMPLE_SINGLE, SAMPLE_SINGLE.answer));
eq('答对后移出错题本（mastered）', store.wrongIds().length, 0);
eq('含已订正仍能看到', store.wrongIds({ includeMastered: true }), [SAMPLE_ID]);
ok('记录累加 correct', store.record(SAMPLE_ID).correct === 1);

const due = store.dueIds();
ok('dueIds 返回数组', Array.isArray(due));
const exported = store.exportAll();
ok('导出不含 API Key', exported.settings.ai.apiKey === '');
ok('导出含 schema 版本', exported.schema === 1);
ok('统计数据正确', store.stats().attempts === 2, String(store.stats().attempts));

store.toggleFlag(SAMPLE_ID);
eq('标记生效', store.flaggedIds(), [SAMPLE_ID]);

/* --------------------------------------- 6. 全量题目：渲染 + 用正确答案判分 */

let rendered = 0;
let gradedCorrect = 0;
const notProbeable = [];
for (const q of data.questions) {
  let html;
  try {
    html = md.render(q.stem).outerHTML;
    rendered += 1;
  } catch (err) {
    failures.push(`渲染题面失败 ${q.id}: ${err.message}`);
    continue;
  }
  if (q.explanation) md.render(q.explanation);
  if (q.reference) md.render(q.reference);
  (q.options || []).forEach((o) => md.renderInline(o.text));

  let r = null;
  let probeable = true;
  if (q.type === 'single') r = engine.grade(q, q.answer);
  else if (q.type === 'multi') r = engine.grade(q, q.answer.slice());
  else if (q.type === 'blank') {
    // 某个空只有正则、没有字面答案时，这里**造不出**标准答案来喂它 ——
    // 任何探测值都会判 partial。所以这类题不进「标准答案判分通过」的统计，
    // 而是单独列出来（可自测 ≠ 已验证），末尾打印数量与题号。
    probeable = q.answer.every((b) => (b.accept || []).length > 0);
    if (probeable) {
      r = engine.grade(q, q.answer.map((b) => b.accept[0]));
    } else {
      notProbeable.push(q.id);
      const rr = engine.grade(q, q.answer.map((b) => (b.accept || [])[0] || 'x'));
      ok(`正则空可判分 ${q.id}`, (rr.blanks || []).every((b) => b.want.length || b.regex.length));
    }
  } else r = engine.grade(q, 'x');
  if (r && (r.status === 'correct' || r.status === 'ungraded')) gradedCorrect += 1;
  else if (probeable) failures.push(`用标准答案判分未通过 ${q.id}: ${r && r.status}`);

  ok(`题干非空 ${q.id}`, html.trim().length > 0);
}
eq('全部题面可渲染', rendered, data.questions.length);
eq('可逐字验证的题标准答案判分通过', gradedCorrect, data.questions.length - notProbeable.length);
if (notProbeable.length) {
  console.log(`\n注意：${notProbeable.length} 道题的填空只有正则、没有字面答案，"标准答案判分"没覆盖到它们：`);
  console.log('  ' + notProbeable.join(', '));
}

// 单选/多选的干扰项必须判错（防止答案写错但恰好通过）
for (const q of data.questions) {
  if (q.type === 'single') {
    const other = q.options.find((o) => o.key !== q.answer);
    if (other) ok(`干扰项判错 ${q.id}`, engine.grade(q, other.key).correct === false);
  }
  if (q.type === 'multi') {
    ok(`多选少选不得满分 ${q.id}`, engine.grade(q, q.answer.slice(0, 1)).correct === false);
  }
}

/* -------------------------------------------------- 7. data 筛选 */

ok('按学科筛选会包含全部子孙',
  data.filter({ topics: [SAMPLE_SUBJECT.key] }).every((q) => data.topicPath(q.topic)[0] === SAMPLE_SUBJECT.key));
ok('按学科筛选不为空', data.filter({ topics: [SAMPLE_SUBJECT.key] }).length >= 1);
ok('按知识点筛选只命中该知识点',
  data.filter({ topics: [SAMPLE_LEAF.key] }).every((q) => q.topic === SAMPLE_LEAF.key));
ok('按单元筛选会包含其知识点', data.filter({ topics: [SAMPLE_UNIT.key] }).length >= 1);
ok('按题型筛选', data.filter({ types: ['short'] }).every((q) => q.type === 'short'));
ok('关键词筛选（题面）', data.filter({ keyword: SAMPLE_SUBJECT.name }).length >= 1);
ok('关键词能命中主题路径', data.filter({ keyword: SAMPLE_LEAF.name.slice(0, 4) }).length >= 1);
ok('关键词搜不到时返回空', data.filter({ keyword: 'zzz-不存在的词-zzz' }).length === 0);
ok('IDs 筛选优先', data.filter({ ids: [SAMPLE_ID] }).length === 1);
ok('按难度筛选', data.filter({ difficulty: [SAMPLE_SINGLE.difficulty] }).every((q) => q.difficulty === SAMPLE_SINGLE.difficulty));

/* 主题树 */
ok('至少有一个学科', data.subjects.length >= 1);
ok('每个学科都有子节点', data.subjects.every((s) => s.children.length > 0));
ok('节点的题数已沿树累加',
  data.bankStats.byTopic[SAMPLE_SUBJECT.key] === data.filter({ topics: [SAMPLE_SUBJECT.key] }).length);
ok('每个节点都有题数（没有题也是 0）', data.topics.every((t) => typeof data.bankStats.byTopic[t.key] === 'number'));
// 题可以挂任意一层，所以「叶子题数之和 == 学科题数」只在题全挂在叶子上时成立。
// 真正的不变量是定义式的：节点的题数 = 挂在其下（含子孙）的题目数。
ok('单元题数 = 挂在其下（含子孙）的题目数',
  data.bankStats.byTopic[SAMPLE_UNIT.key] ===
    data.questions.filter((q) => data.topicPath(q.topic).includes(SAMPLE_UNIT.key)).length);
ok('descendants 不含自己', data.topics.every((t) => t.descendants.indexOf(t.key) === -1));
ok('topics 顺序：父节点排在其所有子孙之前',
  data.topics.every((t, i) => t.path.slice(0, -1).every(
    (ancestor) => data.topics.findIndex((x) => x.key === ancestor) < i)));
ok('pathNames 与 path 等长', data.topics.every((t) => t.pathNames.length === t.path.length));
ok('筛选返回新数组', data.filter({}) !== data.questions);

/* -------------------------------------------------- install 语义 */

// 上面按 boot.js 的调用时机 install 过一次
ok('装载后题库可用', data.installed === true && data.questions.length > 0);

const questionsRef = data.questions;
const byIdRef = data.byId;
const topicMapRef = data.topicMap;
const topicsRef = data.topics;
const originalCount = data.questions.length;

// install 必须就地清空/填充：app.js / qview.js 持有的是这些容器的引用，
// 整体替换会让它们静默指向旧数据（界面莫名空掉，且不会报错）
data.install({ meta: { topics: [], groups: [], stats: {} }, questions: [] });
ok('install 保持 questions 引用不变', questionsRef === data.questions);
ok('install 保持 byId 引用不变', byIdRef === data.byId);
ok('install 保持 topicMap 引用不变', topicMapRef === data.topicMap);
ok('install 保持 topics 引用不变', topicsRef === data.topics);
ok('install 清空题目', data.questions.length === 0);
ok('install 清空索引', Object.keys(data.byId).length === 0);
ok('install 后 bankStats 归零', data.bankStats.total === 0 && Object.keys(data.bankStats.byTopic).length === 0);
ok('install 后主题树为空', data.subjects.length === 0);

data.install(bank);
ok('重新 install 恢复题目', data.questions.length === originalCount);
ok('重新 install 恢复主题树',
  data.subjects.length === SUBJECT_COUNT && data.topics.length === bank.meta.topics.length);
ok('重新 install 恢复统计',
  data.bankStats.total === originalCount && data.bankStats.byTopic[SAMPLE_SUBJECT.key] === SUBJECT_TOTAL);
ok('派生值走 getter（install 后跟着变）', data.typeLabels.single === '单选' && data.generatedAt === bank.meta.generatedAt);

/* -------------------------------------------------------- 掌握度算法 */

const DAY = 86400000;
const NOW = Date.now();
const rec = (attempts, correct, extra) =>
  Object.assign({ attempts, correct, wrong: attempts - correct, streak: correct, lastAt: NOW }, extra || {});

ok('未作答掌握度为 0', store.masteryOfRecord(null) === 0 && store.masteryOfRecord({}) === 0);
eq('未作答档位', store.masteryBand({}), 'new');

const m1 = store.masteryOfRecord(rec(1, 1));
const m3 = store.masteryOfRecord(rec(3, 3));
const m5 = store.masteryOfRecord(rec(5, 5));
ok('1 次答对 ≈ 50', m1 >= 45 && m1 <= 55, m1);
ok('3 次全对 ≈ 73', m3 >= 68 && m3 <= 78, m3);
ok('5 次全对 ≈ 82', m5 >= 77 && m5 <= 87, m5);
ok('做得越多掌握度越高', m1 < m3 && m3 < m5);

const stale = store.masteryOfRecord(rec(5, 5, { lastAt: NOW - 60 * DAY }));
ok('60 天没碰会回落（≈57）', stale < m5 && stale >= 50 && stale <= 65, stale);

const wrong1 = store.masteryOfRecord(rec(1, 0));
ok('1 次答错 ≈ 23', wrong1 >= 15 && wrong1 <= 32, wrong1);
ok('答错低于答对', wrong1 < m1);

ok('连对给加成', store.masteryOfRecord(rec(5, 5, { streak: 3 })) > store.masteryOfRecord(rec(5, 5, { streak: 0 })));

// 档位边界：薄弱 <40 / 一般 40–69 / 熟练 70–89 / 精通 ≥90
eq('1 次答对落在「一般」', store.masteryBand(rec(1, 1)), 'fair');
eq('1 次答错落在「薄弱」', store.masteryBand(rec(1, 0)), 'weak');
ok('5 次全对落在「熟练」', store.masteryBand(rec(5, 5)) === 'solid', store.masteryBand(rec(5, 5)));
ok('档位数量为 5', store.masteryBands.length === 5);
ok('掌握度不超过 100', store.masteryOfRecord(rec(50, 50, { streak: 9 })) <= 100);

/* ------------------------------- 掌握度：与后端共享的校准夹具 */

// api/app/mastery.py 与 store.masteryOfRecord 必须给出完全一致的结果。
// 不一致时界面上会同时出现「已掌握」和「薄弱」两个互相矛盾的结论 ——
// 这类问题不会报错，只会让人觉得数据坏了。
// 任一侧改了公式而没同步，下面的断言就会红。
const masteryFixtures = JSON.parse(
  fs.readFileSync(path.join(ROOT, 'meta/mastery-fixtures.json'), 'utf8')
);
const toRecord = (input) => ({
  attempts: input.attempts,
  correct: input.correct,
  streak: input.streak,
  lastAt: input.lastAt,
});

masteryFixtures.cases.forEach((c) => {
  const input = c.input;
  eq(`夹具「${c.name}」分数`, store.masteryOfRecord(toRecord(input), input.now), c.expectScore);
  eq(`夹具「${c.name}」档位`, store.masteryBand(toRecord(input), input.now), c.expectBand);
});

ok(
  '夹具覆盖全部 5 个档位',
  new Set(masteryFixtures.cases.map((c) => c.expectBand)).size === 5,
  [...new Set(masteryFixtures.cases.map((c) => c.expectBand))].join(',')
);
// 这条专门盯取整：JS 的 Math.round 向上，Python 的 round() 是银行家舍入。
// 夹具里那两个「原始分恰好 .5」的用例就是为它准备的。
eq('.5 向上取整（32.5 → 33）', store.masteryOfRecord({ attempts: 6, correct: 2, streak: 0, lastAt: masteryFixtures.now }, masteryFixtures.now), 33);
eq('.5 向上取整（41.5 → 42）', store.masteryOfRecord({ attempts: 6, correct: 2, streak: 3, lastAt: masteryFixtures.now }, masteryFixtures.now), 42);
ok('夹具时间基准固定（不依赖运行时刻）', typeof masteryFixtures.now === 'number' && masteryFixtures.now > 0);

/* ------------------------------- 跨设备：作答流水与快照合并口径 */

// 计数的权威来源改成了「逐次作答流水」（服务端只对新插入的累加），
// 不再上传本地累计值。这里盯住前端侧最容易悄悄坏掉的两件事：
//   1. 判分到底有没有产出流水、该产出的路径有没有漏
//   2. 用服务端快照合并时会不会把本地数据冲掉
store.resetAll();

const FLOW_NOW = 1757900000000;
const FLOW_DAY = QF.ui.dayKey(FLOW_NOW);
const flowTopic = SAMPLE_SINGLE.topic;
const flowQ = { id: SAMPLE_ID, topic: flowTopic };

eq('初始没有待上传流水', store.attempts().length, 0);

store.applyResult(flowQ, 'A', { status: 'wrong', score: 0 }, { now: FLOW_NOW });
eq('判分产出 1 条流水', store.attempts().length, 1);

const flow = store.attempts()[0];
ok('流水带客户端生成的 id', typeof flow.id === 'string' && flow.id.length >= 32, String(flow.id));
eq('流水状态与判分一致', flow.status, 'wrong');
eq('流水按本机日期归属', flow.day, FLOW_DAY);
eq('流水记录主题快照', flow.topicKey, flowTopic);

// 「待批改 / 未作答」不计入统计，也就不该产生流水
store.applyResult({ id: 'flow-2', topic: flowTopic }, 'x', { status: 'ungraded', score: 0 }, { now: FLOW_NOW });
eq('待批改不产生流水', store.attempts().length, 1);

// 自评同样累加了计数，所以它也是一次真实作答
store.setSelfGrade('flow-3', 2, FLOW_NOW + 1000);
eq('自评产生流水', store.attempts().length, 2);

// 流水按「成功回执」逐条删除，不误删还没确认的
store.dropAttempts([flow.id]);
eq('回执后只剩未确认的那条', store.attempts().length, 1);

// 重置走「设基线」通道：补丁是增减语义，表达不了「直接清零」
store.resetRecord(SAMPLE_ID);
eq('重置进入基线队列', store.pendingResets()[SAMPLE_ID], null);

// 每日统计的合并口径：取较大值，不能覆盖。
// 直接覆盖的后果是新账号（服务端返回空对象）一登录就把本地热力图历史抹掉。
ok('作答后本地有当日桶', !!store.days()[FLOW_DAY], JSON.stringify(store.days()[FLOW_DAY]));
store.hydrate({ records: {}, days: {}, settingsRev: 9 });
ok('空快照不清空本地热力图', !!store.days()[FLOW_DAY], '空 days 把本地历史冲掉了');

store.hydrate({ records: {}, days: { [FLOW_DAY]: { answers: 99, correct: 99 } }, settingsRev: 0 });
eq('服务端更大的值合并进来', store.days()[FLOW_DAY].answers, 99);
store.hydrate({ records: {}, days: { [FLOW_DAY]: { answers: 1, correct: 0 } }, settingsRev: 0 });
eq('服务端更小的值不覆盖本地', store.days()[FLOW_DAY].answers, 99);

// settingsRev 必须取较大值。被覆盖变小的话，本机之后的每一次设置变更
// 都会被服务端判为「过期 rev」而静默拒绝 ——
// 症状是「改了主题，刷新又变回去」。
store.saveSettings({ theme: 'light' });
ok('settingsRev 抬到不低于服务端', store.settingsRev() >= 9, String(store.settingsRev()));
const raisedRev = store.settingsRev();
store.hydrate({ records: {}, days: {}, settingsRev: 3 });
eq('更小的 settingsRev 不会把本机时钟压回去', store.settingsRev(), raisedRev);

// 快照里的 records 仍是前端认识的形状（服务端照这个形状组装）
store.hydrate({
  records: { 'c-0009': { id: 'c-0009', attempts: 4, correct: 3, partial: 0, wrong: 1, flagged: true, _rev: 6 } },
  days: {},
  settingsRev: 0,
});
eq('服务端记录被灌进本地', store.record('c-0009').attempts, 4);
eq('补丁字段一并生效', store.record('c-0009').flagged, true);

// 复原：后面的断言不应受这里影响（resetAll 也会清掉流水与基线队列）
store.resetAll();
eq('清空后流水队列也为空', store.attempts().length, 0);
eq('清空后基线队列也为空', Object.keys(store.pendingResets()).length, 0);

/* -------------------------------------------------------- 汇总 */

console.log(`\n通过 ${pass} 项断言`);
if (failures.length) {
  console.log(`失败 ${failures.length} 项：`);
  failures.forEach((f) => console.log('  ✗ ' + f));
  process.exit(1);
}
console.log('全部通过 ✓');
