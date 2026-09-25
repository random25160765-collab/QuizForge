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

**另记一笔：应用听不到引擎的报错。** 引擎把 TeX 日志打在 **worker 的 console** 里，
而应用钩的是 iframe 的 console —— 于是"引擎判死"时应用只能等看门狗（90 秒）。
`whyFailed` 现在也接在看门狗那条路上，至少让"卡住"这句带上按源码能判出来的原因。

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
