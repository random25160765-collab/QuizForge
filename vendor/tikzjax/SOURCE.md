# TikZJax（离线副本）

* 整包来自 npm **`@rod2ik/tikzjax@1.6.0`** 的 `dist/`（7.1MB）：
  `tikzjax.js`（入口，认 `text/tikz`）、`run-tex.js`（引擎）、`core.dump.gz`、
  `tex.wasm.gz`、`tex_files/`（245 个宏包，**按需取** —— 里面有 `circuitikz.sty.gz`、
  `tikz-cd.sty.gz`）、`fonts/`、`fonts.css`。
* 取件方式：`curl -sSL --http1.1 -o pkg.tgz https://registry.npmjs.org/@rod2ik/tikzjax/-/tikzjax-1.6.0.tgz`
  （**必须 `--http1.1`**：本机 DNS 把外网指到 198.18.0.10 的本地代理，HTTP/2 会超时）。
  解包取 `package/dist/` 下的东西，**平铺**放进 `vendor/tikzjax/` ——
  它按**相对自己的路径**找 `run-tex.js` / `core.dump.gz` / `tex_files/`，目录结构不能改。

## 为什么不是另外两份（都试过）

* `tikzjax.com/v1/tikzjax.js` + 它 S3 上那两个哈希载荷（`.wasm` 598KB、`.gz` 9.79MB）：
  能跑，但**只支持纯 TikZ** —— `circuitikz` / `tikz-cd` / AMScd 一律内部致命错误（`unreachable`）。
  实测四块对照页：纯 TikZ ✓、其余三块 ✗。
* npm `node-tikzjax` 的载荷：格式对不上（`tex_files.tar.gz` 与浏览器包不是一代）。

## pgfplots：试过一次，已回滚（2026-09-25）

* 装法（照上面那条规矩）：CTAN `pgfplots.tds.zip` 里 `tex/generic/pgfplots/**` 的 75 个
  `.sty`/`.tex` 平铺 gzip 进 `tex_files/`（新增 44 个，另 31 个同名文件引擎本来就有）。
* 结果：**装上之后 TeX 编不出来了**（用户与我这边都是「tex 编译一直出不来」）。
  于是整批挪走、提示词跟着说回「没有 pgfplots」。真原因没查清
  （嫌疑：这份载荷与引擎的格式不是一代；或某个 `.gz` 让它卡住）。
* 再试的话别一次全放：一次只加几个文件、每加一次就用一张 `\begin{axis}` 图验一次，
  并盯着控制台里对 `tex_files/<名>.gz` 的 404（引擎取不到会去 fetch 同名文件）。

## 现状（2026-09-25 晚）

前言里现在有：`circuitikz` / `amscd` / `tikz-cd` / `pgfplots` / `CJK`，外加
`\usetikzlibrary{positioning,calc,fit,arrows.meta,matrix}`（都在 `run-tex.js` 内那一串）。

上面那条"pgfplots 已回滚"是**当时的**结论：后来查明真正的原因不是那份载荷，而是**缺 `.fdx`**
（见下面第 1 条）—— 补上之后 pgfplots 正常（`\begin{axis}` 能用）。

这五条都是**同一类事故**：模型按最自然的写法写，引擎却缺一件东西；而**缺什么都不会报错**，
表现得像卡死。所以宁可一次把常用的备齐，也别让它一个个踩。

## 中文（CJK）：装了什么、从哪来

* **宏包**：CTAN `cjk-4.8.5.tar.gz` 的 `texinput/**`（171 个 `.sty`/`.tex`/`.fd`/`.fdx`/`.enc`），
  平铺 gzip 进 `tex_files/`。
* **度量**：CTAN `gbsn00lp-20071107.tar.bz2` 里 `gbsnu` 全族 **98 个 `.tfm`**，同样平铺 gzip。
* **引擎的字体表**：`run-tex.js` 里那段 `"cmr10":"<base64>"` 是**内嵌的 tfm**。中文那族要按
  **同格式**塞进去（98 片、约 140KB base64），**还得配一张 `字符码 → Unicode` 的表**
  （`"gbsnu53":{"214":21494,…}`）—— 少那张表引擎会崩（`Cannot read properties of undefined`）。
  对应关系是实测出来的：`取` = U+53D6 → 字体 `gbsnu53`、码 **214**，即
  **`gbsnu<HH>` 的 `<LL>` 号字 ↔ 码点 `U+HHLL`**。
* **字形**：CTAN `arphic-ttf.zip` 的 `gbsn00lp.ttf` 按同一范围做子集 →
  `fonts/gbsn00lp.woff2`（1.33MB，**按需下载**，见 `fonts.css` 末尾那条前缀规则）。
  Python 沙箱另用一份 **TTF**（matplotlib 不认 woff2）：`vendor/cjk/gbsn00lp.ttf`，
  构建时进 `assets/fonts/` —— 那条路径才有 CORS（沙箱是不透明源的独立文档）。
* **那层环境**：`\usepackage{CJK}` 只是把宏包装上；真正把 UTF-8 汉字映到字形上的是**正文里**
  的 `\begin{CJK}{UTF8}{gbsn}…\end{CJK}`。所以应用在交给引擎前**自己套**
  （`theme/runtime/latex.js` 的 `withCJK`）—— 不能指望模型记得写这一层。

## "看起来像卡死"的四个坑（都实测过，照这条查）

1. **缺文件 → TeX 停在报错提示上等人按键**：既不报错也不出 dvi。线索是服务端日志里对
   `tex_files/<名>.gz` 的 404。典型：`ot1cmr.fdx`、**`omlcmm.fdx`** —— CJK 会去读**当前字族**
   的 `.fdx`，缺一个就是 `! Missing $ inserted` → `Emergency stop`。**空文件也对**，关键是别缺。
2. **引擎的 svg 带 `tikzjax-broken` 类**：那是"引擎判死"的标记，不是图。应用从前把它当成
   "还没好"，于是一路等到 90 秒看门狗、报"引擎卡住了" —— 其实几秒前就有结论了。
3. **诊断要挑有用的那段**：引擎抛的错里**嵌着整份 TeX 日志**；从头截只会截到
   `This is e-TeX…`，要挑 `! …` 那一行（`theme/runtime/latex.js` 里就是这么做的）。
4. **实验室**：`tools/texlab/index.html` —— 仓库根起
   `python3 -m http.server 8199 --directory <仓库根>`，打开 `/tools/texlab/index.html`。
   它**在引擎之前**就钩住 console，TeX 的日志与 `! 报错` 都能读到；上面几条都是在那儿定案的。
   **一次只加一个变量**，并且每次**重开页面**（工位是复用的，卡过一次的页面会一直卡）。

## pgfplots：装齐、且**同一个版本**（2026-09-26）

上一轮那句"整批装上就编不出来"的**真因不是批量**，是**缺文件**与**版本混装**：

* **缺文件**：`tikzlibrarypgfplots.surfshading.code.tex`（上游叫 `pgflibrary…`，这片引擎按
  `tikzlibrary…` 找）之类。少一个 → 引擎取到 404 → TeX 把空内容当正文读 → 报出来是
  `! Missing $ inserted` 停在 `\end{axis}` → **一个 svg 都不出** → 只能干等看门狗。
  **对策：整套铺**（`tex/generic/pgfplots/**` + `tex/latex/pgfplots/**`，627 个文件量级），
  别再一个个试 —— 缺一个与缺一套是同一个后果。
* **版本混装**：铺的时候若"已存在就跳过"，会留下两代文件（实测 **1.18.2 与 1.18.3 混住**），
  报出来是 `! Extra \else. \pgf@plotstreampoint …` —— 报在 **pgf 内核宏**上，看着像引擎坏了，
  实际是 pgfplots 自家两代文件打架。**对策：整批覆盖，一个版本。**
  实测这台：引擎内 **pgf 3.1.10a**、**pgfplots 1.18.3**（后者要的正是 ≥ 3.1.10，配得上）。

## 中文分类轴：`symbolic x coords` 里**不能**放中文（2026-09-26）

那里的键是 pgfplots **要解析**的，而中文在这台引擎上是**活动字符**（CJK 靠它映字形；
e-TeX 只有 8 位，绕不开）—— 两者冲突，报 `! Extra \else. \pgf@plotstreampoint …`。
**修不了**（编码方式的固有冲突），但绕得开，而且写法很自然：

```latex
symbolic x coords={A, B, C},
xticklabels={取指, 译码, 执行},   % 中文放这儿：它是"排版"出去的，不参与解析
```

数据点相应写 `(A,2)`。实测这样出图正常（实验室与应用都验过）。提示词里已写明
（`api/app/routers/chat.py`），失败文案里也会指出（`latex.js` 的 `whyFailed`）。

**另记一笔：应用听不到引擎的报错，根因不是 Worker（这句以前写错了，2026-09-26 纠正）。**

从前这里写的是"引擎把 TeX 日志打在 **worker 的 console** 里"。**不对**：整个仓库里
**没有任何 `new Worker`** —— 引擎根本不用 Worker。真相是：

* 引擎确实把 `! …` 打在 console 里（证据：playwright 的 `console-*.log` 里躺着
  `! Package pgfkeys Error: I do not know the key '/tikz/state'`）。
* 而应用的钩子挂在**错的 window 上**：`ensureFrame()` 只在 iframe 刚建好时挂一次
  （那时还是 `about:blank`），可 `fresh()` 是给同一个 iframe 设 **`srcdoc`** —— 那是
  **一次导航**，新文档 + **新 window 对象**，旧 patch 跟着旧 window 一起没了。
  于是 `lastError` 永远是空，用户看到"（引擎没报原因）"，而 `settle` 里那道
  "TeX 已经停死就立刻结账"（认 `! Emergency stop` / `End of file on the terminal`）
  **永远不触发** → 一张编不出来的图**干等 90 秒看门狗**（用户原话："平均出一张图要几分钟"）。

修法：把钩子抽成 `hookConsole(win)`，并在 **iframe 的 `load` 事件**上重挂（每次导航都重挂）。
`whyFailed` 仍然接在看门狗那条路上，作为读不到日志时的兜底。

## "平均出一张图要几分钟" —— 真因与修法（2026-09-26，接上一条）

上面那条查清了"应用听不到引擎的报错"。接着做了三件，缺一件都不成：

* **日志桥**：在工位文档里、**引擎脚本之前**注入一小段，把 `console` 各档 `postMessage` 给应用
  （`latex.js` 的 `LOG_BRIDGE` + 应用侧的 `message` 监听）。为什么非要在**引擎之前**：引擎
  （压缩过的包）多半在加载时就把 `console.warn` 取进了变量，**事后替换 `console.warn` 对它无效**
  —— 实测：钩子挂上了，`! Package pgfkeys Error …` 照样读不到。
* **别只留最后一条**：引擎是**一次一条**地报，后一条会把前一条**覆盖** —— 那条关键的
  `! Emergency stop.` 就是这么丢的。现在逐条**当场判**（`sawStop`）+ 一个 4000 字的滚动缓冲。
* **多认一种"停死"**：引擎会自己宣告 `TikZJax: TeX did not produce input.dvi.`，而且**放在日志
  最前面**（实测：收到的那条 4000 字日志，开头就是它）。把它也当成"立刻结账"的信号。

**效果（应用里实测）**：一张编不出来的图（少一个分号）从 **92.9 秒 → 6.6 秒**结账，
文案里也第一次带上了引擎原话。用户那句"平均出一张图要几分钟"就是这么来的 —— 一张坏图 90 秒，
两张就是三分钟。

**代价说清**：日志被截到 4000 字（一张图的 preamble 日志就能吃掉大半），所以文案里未必看得到
那句 `! …` —— 那部分靠 `whyFailed` 兜。

## 样式库：`[state]` 这类写法（2026-09-26）

用户的自动机状态转移图挂在 `\node[state]` 上：`state` 是 **`automata`** 库的样式，模型没写
`\usetikzlibrary{automata}`，TeX 报 `! Package pgfkeys Error: I do not know the key '/tikz/state'`，
整张图没有。两个方向都堵上了：

* 前言**预载**了常用的样式库（automata、shapes.*、decorations.*、patterns、chains、quotes、
  angles、intersections 等约 20 个）；
* `latex.js` 的 `rewriteUnsupported()` 还会**按用到的键自动补一行** `\usetikzlibrary{…}`
  （键→库的表就在那儿，`state`/`diamond`/`pattern`/`decoration`/`rectangle split` …）。

实测：那张图从"报错"变成 **605ms 出图**（模型一个字没写库）。

## 图里的矩阵：`amsmath` 进前言（2026-09-26）

用户那两张 Jordan 分解图（节点里写 `$J=\begin{pmatrix}…\end{pmatrix}$`）编不出来。
实验室里报的是 **`! Misplaced alignment tab character &`** —— 根因**不是那两张图的写法**，
而是**引擎前言里没有 `amsmath`**：`pmatrix` / `bmatrix` / `matrix` / `cases` / `aligned`
全在它里面；缺了它，`&` 就成了"对齐符之外的裸字符"。

* 前言原件（`run-tex.js`）装的是 `circuitikz` / `amscd` / `tikz-cd` / `pgfplots` / `CJK`
  + `\usetikzlibrary{positioning,calc,fit,arrows.meta,matrix}` —— **没有 amsmath**。
  注意别被 `amscd` 骗了：那是引擎**格式里**那份，不代表 amsmath 在。
* **真因是"文件在、前言没挂"**：`amsmath.sty` 其实**早就在** `tex_files/` 里了 —— 提交
  `56b2507` 就带着它（`amssymb`/`amsfonts` 也在），同族那四个也一直躺在目录里（只是当时
  还是**未跟踪**状态，靠 `make web` 整目录拷贝才进的产物，现在已入库）。缺的**从来只是
  前言里那句 `\usepackage{amsmath}`** —— 所以引擎一次都没加载过它们。
  这与 tikz-feynhand 那条是**同一个教训**：**铺了文件 ≠ 装上了**，得进前言才生效。
* 这次的活儿因此只剩两件：前言**最前面**加一句 `\usepackage{amsmath}`（它要早于别的包），
  以及把那 5 个文件入库。以后若要重铺同族文件，从 **TeX Live 的成品包**取
  （`/CTAN/systems/texlive/tlnet/archive/amsmath.tar.xz`）—— CTAN 的目录包只有
  `.dtx`/`.ins`，本机没有 TeX，生不成 `.sty`。
* 回归验过（实验室）：两张原话 ✓、pgfplots 三曲线 ✓、tikz-cd ✓、中文图 ✓、circuitikz ✓。
* 两边分工别混：**正文里的矩阵走 KaTeX**（瞬间，见上面那条）；**图里节点里的矩阵**才需要
  amsmath。用户这两张是后者。

## 静默预装与冷启动（2026-09-26）

用户："把要用的宏包都给它静默预装好，不要再出现类似问题了！！冷启动延迟给它藏起来"。

**宏包口径**

* 前言现在装的是：`amsmath` `amssymb` `bm` `mathtools` `circuitikz` `amscd` `tikz-cd`
  `pgfplots` `CJK`，加 `\usetikzlibrary{positioning,calc,fit,arrows.meta,matrix}`。
  **故意不预载更多 TikZ 库**：库文件齐了之后 `\usetikzlibrary{…}` 本来就能成，预载只是把
  成本加到**每一批**的编译上 —— 不划算。
* 库文件**按名字对齐过** pgf 的完整清单（85 个名字）：只差 `luamath`（要 Lua）与
  `tikzexternalshared`（内部用），两个在这儿都用不上 —— 也就是说模型想用哪个库，文件都在。
  本机没有 TeX，成品 `.sty` 一律从 **TeX Live 包**里取
  (`/CTAN/systems/texlive/tlnet/archive/<包>.tar.xz`)；CTAN 的目录包只有 `.dtx`/`.ins`。
* 实测（实验室逐条）：`shapes.geometric`、`decorations.pathreplacing`、`patterns`、`topaths`、
  `\usepgfplotslibrary{fillbetween}`、`\mathbb`、`\bm`、`\coloneqq` 全 ✓；前言成本也量过 ——
  基线极小图与逐项**分不出差别**（都是 2.5s 量级，那是实验室页面自身的开销）。
* **兜住一种"整死一张图"的写法**：模型偶尔会自己写 `\usepackage{…}`，TeX 报
  `! LaTeX Error: Can be used only in preamble.`，整张图没了（实测确认）。现在
  `latex.js` 的 `stripPreamble()` 会在编译前把那几行（`\usepackage` / `\documentclass` /
  `\begin{document}` / `\end{document}`）摘掉：真要前言里没有的包，摘掉后会以
  "未定义的控制序列"报出来 —— 那才是准确信号。实测：带 `\usepackage{amsmath}` 的图现在正常出图 ✓。

**冷启动怎么藏的**：`prewarm()` 原来只在页面空闲时跑（最多 3 秒后）。现在**三处触发**：
用户第一次 `pointerdown`/`keydown`、聊天那边**发出消息**那一刻（`chat.js` 的 `onSendClick`）、
以及原来的空闲时机。三处幂等，也都在计费/2G 网络下自己退出。
实测：**"发出消息"到引擎备好 = 3.5 秒** —— 而模型的思考时间就是这个量级，于是第一张图不再吃
那 5.4 秒的冷启动。

**两个被证伪的猜想**（记下来，免得下次再追）：

* "工位放在屏幕外（`left:-10000px`）会被浏览器降频" —— 从外面把工位挪进视口（透明）再量，
  同一张图 0.58s vs 0.59s，**没有差别**。
* "每张图都套 `\begin{CJK}` 是那多出来的 1.6 秒" —— `withCJK` 本来就**只在含中文时才套**
  (`if (!CJK_CHAR.test(text)) return text;`)，ASCII 图根本没套。

**仍未解释的一处**（照实记）：同样一张极小图，在应用的热工位上量到 **2.2 秒**，在实验室量到
**151ms**；而这个差异有时又消失（同一天另一次量到 **0.6 秒/张**）。引擎在应用那个上下文里
有时会多花约 1.5 秒，原因尚未定位。缓存命中时是 **1ms**，所以**重复出现的图不受影响**。

## 两条"就地改对"的兜底（2026-09-26）

用户报三张图有故障。逐条进实验室查完，结论是：

* **#1 已经好了** —— 那张 tikz-cd 拉回图（`\arrow[dr, phantom, "\lrcorner" very near start]`）
  现在正常出图。原因是 `\lrcorner` 属于 **`amssymb`**，而它在这一轮才进的前言（见上一节）。
* **#2 `shader=interp`**：真故障。这台引擎的驱动不支持函数式着色，实测原文
  `! Package pgfplots Error: Sorry, surface shading (shader=interp) is NOT available for the
  selected driver`，整张图没有。
* **#3 中文的分类轴键**：真故障。`symbolic x coords={取指, …}` 里的键 pgfplots 要**解析**，
  而中文是活动字符，实测 `! Extra \else. \pgf@plotstreampoint …`（报在 pgf 内核宏上）。

处理方式改成**就地改对**（都在 `latex.js` 的 `rewriteUnsupported()` 里，改前先看有没有必要，
没必要的图一个字符都不动）：

* `shader=interp` → **摘掉**。之后 `surf` 用默认的面片着色，曲面照旧出来。**代价说清楚**：
  图与"按高度上色"那句说明不再对应 —— 所以提示词里仍然明写"不许写 `shader=interp`"，
  这里只是兜底。
* 中文键 → 换成 `k1, k2, …`，数据点里的键跟着换，中文挪进 `xticklabels`（**排版**出去、
  不参与解析）。实测出图正常，刻度与图例上的中文一个不少。

实测（应用里逐条、原话）：①5900ms ✓ ②10100ms ✓（26×26 = 676 次求值，代价就是这么来的 ——
提示词里已补上这个数）③2600ms ✓（图上文字：取指译码执行访存写回…阶段周期基线优化后 ✓）。

**另外修掉一个"沉默的 bug"**：`whyFailed()` 里那条 tikz-cd 分支写成了
`/\\begin\\s*\\{\\s*(tikzcd|CD)\\s*\\}/` —— 正则里的 `\\s` 是"一个反斜杠后跟字母 s"，
所以它**永远匹配不到**，用户那段拉回图拿到的是最笼统的兜底文案（模型与人都看不出所以然）。
改成单反斜杠后 `node -e` 验过：旧正则 false、新正则 true。

**教训**：`whyFailed()` 里写正则一律**单反斜杠**（`/\\begin\s*\{/` 这种形式），别被
JS 字符串的转义习惯带跑；另外"文案说对了"不等于"问题解决了" —— 能就地改对的就别只写说明。
