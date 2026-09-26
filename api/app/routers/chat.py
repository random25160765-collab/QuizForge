"""对话 —— 学习前台的会话与消息。

## 这一版有什么、还缺什么

**有了**：消息是零件（`parts`：正文 / 推理 / 工具调用 / 出处 / 题卡位），
模型能调**只读工具**（检索知识空间、取出处、看已有题、看掌握度、看到期复习），
上下文按预算从最新往前截（见 `agent_loop`）。

**还缺**（都是下一刀）：
* **写操作工具**（改掌握度、推题入队）与**题卡** —— 写操作要配"先确认再执行"，
  题卡的渲染与作答要在前端落地（`card` 零件位已经留好）。
* **RAG**：把材料原文按需喂进来。切片一片 1800–3300 token，预算机制就是为了它先建的。
* **附件**：贴一张截图进来问（b.md 里"截一段材料"的实际形态）。

## SSE 事件协议（我们自己的，不是供应商的）

    event: user     data: {...消息}         刚落库的用户消息（前端用它替掉乐观节点）
    event: start    data: {...消息}         助手占位消息（status=streaming）
    event: delta    data: {"text": "..."}   增量文本
    event: usage    data: {...token 数}     上游给了才有
    event: done     data: {...消息}         落库后的最终消息
    event: error    data: {"message","detail","retryable"}

不让前端去解析供应商的格式，换供应商时前端一行都不用改。这与 `routers/ai.py`
「返回体不做二次包装」看似相反，其实是同一类取舍的两个方向：批改是**透传**
（前端早就有解析器了），对话是**自定协议**（前端只解析一次，且必须能被我们改）。

## 为什么中间态要反复落库

一次生成十几秒。用户按了停止、关了页面、网络断了 —— 如果只在结束时写库，
这十几秒的产出就没了，用户看到的是白屏，还会怀疑是自己点错了。
所以：占位行先落库（`streaming`），**收到一块就攒一攒写一次**（`SPILL_SECONDS`），
结束时改成 `ok` / `partial` / `error`，被中断但有产出也照样留下。

## 一个已知的、故意留着没修的边界

客户端断开时，这条路由**感知不到**（同步生成器跑在线程池里，读不到
`request.is_disconnected()`）。后果有两层：

* 已经生成的内容不会丢 —— 靠上面那条「边收边写」，加上读会话时的 `_heal_stale`
  把状态补成 `partial`。
* 但**上游不会立刻停**：供应商还会把话说完，那部分 token 白花了。

要真正省下这笔 token，得把这条路由改成 `async def`，用
`await request.is_disconnected()` 在块与块之间检查、主动关掉上游连接
（同时把库操作挪进 `anyio.to_thread`）。那是一次独立的重构，
而它换来的只是「省钱」，所以先记在这里，不与内核一起做。
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm.attributes import flag_modified

from .. import agent_loop, share, skills as skill_lib, tools
from .. import ai_gateway as gateway
from .. import attachments as attach
from .. import parts as msgparts
from .. import mounts
from .. import runs
from .. import websearch
from ..config import get_settings
from ..db import as_json
from ..deps import DbSession
from ..models import Attachment, Conversation, ConversationFolder, Message

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.get("/mounts")
def mounts_state(db: DbSession) -> dict:
    """顶栏那组开关现在的状态：每组各亮着没有，以及**这一版真声明了哪些工具**。

    `declared` 是从 `tools.specs()` 真算出来的，不是另抄一份 ——
    "图标亮着"与"模型看得到几个工具"必须能对得上，而这是唯一能证明它俩一致的办法
    （改完在界面上量一下 `declared` 的条数，就知道开关有没有真的接上）。

    `skills` 是 `/` 那份清单（`app/skills.py`）—— 它跟着这一份一起发，是为了让输入框那个
    选择器**不用自己抄一份名字**：加了新 skill，界面自动就有（抄成两份必然漂，这条吃过）。
    """
    out = mounts.describe(db)
    out["skills"] = skill_lib.catalog()
    return out


@router.post("/mounts")
def mounts_save(body: dict, db: DbSession) -> dict:
    """换模式，或（自定义时）存一份挂载集。

    * `{"mode": "query"}` —— 三个模式之一：组与权限档都由模式表决定（首选这条路）；
    * `{"groups": [...]}` —— 自定义：只存"哪几组"，权限档保持四档全放（老行为）。

    空列表仍然合法，等于"一条工具都不声明"。
    """
    mode = body.get("mode")
    if isinstance(mode, str) and mode.strip():
        try:
            mounts.write_mode(db, mode)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return mounts.describe(db)
    groups = body.get("groups")
    if not isinstance(groups, list):
        raise HTTPException(status_code=400, detail="groups 得是个列表")
    try:
        mounts.write(db, [str(part) for part in groups])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return mounts.describe(db)


@router.get("/websearch")
def web_search_state(db: DbSession) -> dict:
    """联网搜索配好了没有、走哪条路。

    设置面板要这一条：界面本地只知道**用户自己填的**那份，而实际生效的可能来自
    内测通道（见 `websearch.resolve`）—— 只说"没填密钥"，会让人对着一个其实能用的
    功能找问题。**不回密钥**。

    路名是 `/websearch` 而不是 `/search`：后者已经归"在消息正文里搜一段字"了
    （见下面的 `search_messages`）。同名两条路由不会报错，只会有一条**永远进不去** ——
    实测踩到：加上去的那一刻，搜消息的两个用例就开始 KeyError。
    """
    return websearch.state(db)


@router.get("/sandbox")
def sandbox_shell() -> dict:
    """常驻 Python 运行壳的那份 HTML（前端拿去做隐藏 iframe 的 `srcdoc`）。

    为什么由服务端给：壳里要填 Pyodide 的 indexURL 和预载包名单 —— 那是 `tools.py`
    才知道的事实（本机缓存路径、支持哪几个包），前端不该再抄一份。

    口径改过一次（2026-09-25）：从前是"前端**第一次真要跑 Python 时**才去建它"，
    理由是"页面一打开就下 60MB 运行时，对不跑 Python 的人不公平"。用户的原话是
    "启动以后 pyodide 啊，latex 这些都自动启动准备好，尽量让用户进入对话的时候无感知"
    —— 于是前端改成**空闲时就建**（见 chat.js 的 shellWarm）：解释器初始化那几秒
    发生在用户还在读上一条消息的时候，而不是他按下"运行"之后。前端那边仍然会
    跳过计费 / 2G 网络，所以"不公平"那一条并没有丢。

    `ready` = 运行时是否已经在**本机**（`heavy_deps` 那 76M 缓存好了没有）。
    前端拿到 `False` 时**不许**建壳：壳里 `loadPyodide({indexURL: ''})` 会起不来，
    而壳一旦建过就不会再建，真正要跑 Python 时就一直是"运行中…"。首启那个把
    运行时取回来的后台线程还在跑的时候会命中这种情况 —— 那时候留原来的懒启动路。
    """
    from .. import heavy_deps  # noqa: PLC0415

    # `shell` 是这份壳页面的**运行时指纹**：宿主建壳时记下它，之后与 `/api/build` 报的
    # 那份对账 —— 对不上说明壳模板改过了（改 shell 模板不动 `make web` 的构建戳，
    # 只盯构建戳的话，已经打开的页面会一直用旧壳，实测踩过：沙箱装了中文字体，
    # 而页面里的旧壳"一个中文字形都没有"）。
    return {
        "html": tool_registry.shell_page(),
        "ready": heavy_deps.is_ready(),
        "shell": tool_registry.shell_stamp(),
    }

# 提示词按"这一版挂了哪几组工具"拼：基座 + 每组一份片段 + 未挂组的"别提"清单。
#
# 为什么非要按模式分（原来是一个常量）：极简模式下它照样命令模型去调
# `search_knowledge` / `push_question`（工具早被拿走了），人设里那句"服务一个正在啃
# AI 加速器的学习者"还会让模型张口就猜"你最近在看 NOC、circular buffer 吧" ——
# 用户原话："它根本就不应该知道我在干什么"。所以两条硬规则：
#   1) **没挂的能力不进提示词**（连"工具"这个词都不出现）；2) 极简模式不交代应用领域。
VOICE = (
    "**所有输出一律用中文**，直接讲清机制与因果，"
    "必要时用 Markdown 与 LaTeX。"
    "**公式与图走两条完全不同的路 —— 这里最容易错**。"
    "**公式**（含矩阵、方程组、分段函数、多行对齐：`pmatrix`/`bmatrix`/`vmatrix`/`matrix`/"
    "`cases`/`aligned`/`array`，以及 `\\cdots`/`\\vdots`/`\\ddots`/`\\substack`/`\\overbrace`）"
    "直接写在正文的 `$$…$$` 里，由 **KaTeX** 排 —— **立刻**出来，不排队也不编译。"
    "**别为了排公式去画图**（实测：五张矩阵图各花两秒多，而同样五张矩阵交给 KaTeX 是瞬间的事）。"
    "矩阵只有一条要点：**写在数学环境里**（列间 `&`、行间 `\\\\`）；`\\bordermatrix` 没有，"
    "要给矩阵加行列标签就用 `array` 配 `\\overbrace`/`\\underbrace`。"
    # 2026-09-26：用户那两张 Jordan 分解图挂在这儿 —— 节点里写 pmatrix，而引擎前言当时
    # **没有 amsmath**（报 `! Misplaced alignment tab character &`）。现在前言里装上了
    # （`amsmath` 放在最前），所以这句是实话，也就该说一声：免得它因为"以前编不出来"而绕。
    "**图里的节点也可以写矩阵**（`pmatrix`/`bmatrix`/`cases`/`aligned` 都认，引擎前言里有 "
    "`amsmath`）—— 所以分块结构图那种，直接在 TikZ 节点里写 `$J=\\begin{pmatrix}…\\end{pmatrix}$` 就行。"
    "**只有真正的图形/图表**（TikZ 示意图、pgfplots 坐标图、circuitikz、tikz-cd）"
    "才走**真的 TeX 引擎**（TikZJax）—— 那是**每张 2 秒起**的编译："
    "`\\draw`、`\\node`、`child`、`matrix`、样式指令都认；"
    "**pgfplots 在**（`\\begin{axis}`、`\\addplot`/`\\addplot3`、`legend`、`grid`、`ymode=log`、"
    "`view={…}{…}`、误差棒、极坐标那些都认）：坐标轴图、函数图、曲面图**直接写 pgfplots**；"
    "要能动的交互演示才用 `render_demo` —— 但**图不要放进去**：那里的公式由 **KaTeX** 排，"
    "**不认 TikZ 与 pgfplots**（实测：'再来几张 tex 图' 那次，它把三张 "
    "`\\begin{tikzpicture}` 包进 `QFKit.Math` 交上来，三张图全成了源码 —— 现在工具会"
    "直接拒掉那种 html）。**图一律写在正文的 `$$` 里**，那才是真 TeX 引擎编的。"
    "前言里已经 `\\usepackage{pgfplots}`，"
    "你不必（也不能）自己写 `\\usepackage`。"
    # 相对定位（`below=of` / `right=… of`）要 `positioning` 库，而它从前**没装** ——
    # 模型按最自然的写法写，得到的却是 `! Package PGF Math Error: Unknown function 'of'`
    # （实测：用户那张流程图和矩阵图就死在这儿）。现在前言里装了它，这里说一声，免得
    # 模型因为"以前编不出来"而绕着手写坐标。
    "**相对定位是认的**（`node[below=of a]`、`right=1cm of b` 都行，`positioning` 已在）；"
    # tikz-feynhand 的文件**在** vendor 里，但它得靠 `\usepackage` 进前言 —— 前言是引擎的、
    # 模型改不了，所以结论不变（手画），但原因要说对（从前写的是"没有"，那是假话）。
    "**费曼图还是用 TikZ 手画**：tikz-feynhand 装了文件也没用，它得进前言才生效。"
    "**有一条绝对不许写**：`shader=interp` —— 这台引擎的 pgf 驱动是 `pgfsys-ximera.def`，"
    "不支持它，整张图会直接编不出来（实测原文：surface shading (shader=interp) is NOT available "
    "for the selected driver）。曲面就写 `\\addplot3[surf] {…};`，别加 shader。"
    # 采样数照**实测**写（2026-09-26，应用里三曲线各测一遍）：100 采样约 3 秒、
    # 200 采样跳到 6 秒、300 采样 7 秒 —— 100 与 200 之间有个断崖。从前这里写的是
    # "几百会让编译变成分钟级"，那是夸大（不是分钟级，但也确实不该开那么大）。
    # 2026-09-26：用户的自动机状态转移图挂在 `\node[state]` 上 —— `state` 是 `automata` 库的
    # 样式，模型没写 `\usetikzlibrary{automata}`，TeX 报
    # `! Package pgfkeys Error: I do not know the key '/tikz/state'`，整张图没有。
    # 现在两个方向都堵上了：前言**预载**了常用的样式库，`latex.js` 还会按用到的键**自动补**一行。
    # 这里只说一声"可以直接用"，免得它为了"保险"去写一堆用不上的库。
    "**常用样式库已经预载**（状态图 `state`/`accepting`、`diamond`/`trapezium`、`pattern`、"
    "`decoration`、`chains`、`quotes`、`angles` 这些），直接写就行；别的库照常 "
    "`\\usetikzlibrary{…}`，文件都齐。"
    "`samples` 别开大（实测：三曲线 100 采样约 3 秒，200 采样跳到 6 秒、300 采样 7 秒；"
    "**曲面是二维网格**，26 采样就是 676 次求值 —— 实测一张要 10 秒，曲面平时 15–25 足够；"
    "平面曲线的光滑度到这个量级也看不出差别）。\n"
    # 从前这里写的是"**图里不许出现中文**"（那时引擎确实没有中文字形，遇到汉字会停在交互
    # 提示上等人按键）。现在引擎侧装了 CJK 与一份简体宋体，汉字排得出来 —— 实测
    # `\node {取指 译码 写回};` 正常出图，SVG 里就是那几个汉字。所以改成实话 + 两条边界。
    "**图里可以用中文**（轴标签、图例、刻度都算）：引擎带简体宋体，"
    "汉字排得出来。三条边界：**用常见简体字**（生僻字、繁体、日文假名可能没有字形，会空着）；"
    "汉字是全角、一个字顶两个字母宽 —— 横轴标签多时注意别挤在一起；"
    # 这一条是 2026-09-26 实测出来的：中文放进 symbolic x coords 会报
    # `! Extra \else. \pgf@plotstreampoint …`（那是 pgf 内核宏，看着像引擎坏了）。
    # 原因是 CJK 把中文字符变成"活动字符"来映字形，而那里的键 pgfplots 要解析，两者冲突。
    # 修不了（e-TeX 只有 8 位），但绕得开 —— 而且写法很自然，所以写进来说清楚。
    "**唯一不能放中文的地方是 `symbolic x coords`** —— 那里的键 pgfplots 要**解析**，"
    "而中文在这台引擎上是活动字符，会把它的 `\\if…\\else` 弄乱。"
    "中文分类轴这样写：`symbolic x coords={A, B, C}` + `xticklabels={取指, 译码, 执行}`，"
    "数据点相应写 `(A,2)`（实测这样正常出图）。\n"
    "只有两点：**必须包在 `$$` 里**（裸写、"
    "或塞进代码围栏，都只会显示成源码）；宏包只有常见那一批，冷门的会编不出来，"
    "那种场合改用文字说明或沙箱演示\n"
    # 2026-09-26：用户那张"极限的通用锥"排出来只有 116x94pt（显示约 120px），字挤成一团 ——
    # tikz-cd 的默认间距就是这么小（column sep=2.5em 上下）。要能看清就得把间距写大。
    "**tikz-cd 的图尤其偏小**：默认间距排出来只有一百像素出头、标签全挤在一起 —— "
    "`column sep` 至少给到 5em、`row sep` 给到 4em，复杂图（三行以上）再往上加。"
    "两条经验：**图的大小就是你画的坐标有多大**（TikZ 1cm ≈ 38px）—— 想让它显眼就把坐标"
    "画大（`\\draw (0,0) -- (4,2);` 而不是 `-- (1,0.5);`），别去调宽度样式（会被裁掉）；"
    # 2026-09-26：三张 tikz-cd 图接连编不出来，逐条查到真因（前两轮我判断错过两次，这次是按
    # **引擎原话**定的）：
    #   * `\arrow[dd, …]` 打到**空格子** → `No shape named tikz@f@1-3-3`（tikz-cd 只给有内容的
    #     格子建节点）。两张图都是这个毛病，而且把标签里的中文换成英文**照样报**，与中文无关。
    #   * `dashed near start` → `I do not know the key '/tikz/dashed near start'`：
    #     `near start` 是**标签**的位置词，得跟在引号后面（`"v" near start`），不是箭头选项。
    "tikz-cd 两条硬规矩：①**箭头只能指向有内容的格子**（空格子没有节点，会报 "
    "`No shape named tikz@f@1-3-3` —— 要连线就先把那格填上，哪怕一个 `{}`）；"
    "②**位置词跟标签走**（`\\arrow[dd, dashed, \"v\" near start]` 对，"
    "`\\arrow[dd, \"v\", dashed near start]` 错 —— 那会被当成不存在的键）。"
    "其余常见写法都认（`\\arrow[r, \"f\"]`、`swap`、`bend left`、`shift left=0.6ex`），"
    "`phantom`、`very near start` 这类装饰性选项引擎不一定支持 —— 编不出来就简化它）"
    "（如果你确实想先交代一句，那句也必须是中文；任何情况下都不要输出英文句子。）\n"
    # 这一条**不分模式** —— 极简那份另有自己的版本（见 MINIMAL_PROMPT），而挂了
    # 工具的那几份原来一条都没写。后果实测过两次：模型张嘴就是"你最近在看 NOC、
    # circular buffer 吧"，或者一上来列一串"要不要我查 X、Y、Z" —— 那些 X/Y/Z
    # 全是从材料库里捡的词。用户的原话："无论是什么模式，你都不能预设用户在学什么。"
    "**不许预设他在学什么**：不要替他挑主题，不要假设他的方向、进度或最近在看什么，"
    "也不要把材料里的词拼成`你大概在啃 X`这种话。想推进就直接问他想聊哪一块。\n"
    "**举例也要中性**：只有你自己查到了、或者他自己提到了，才用材料里的词；"
    "否则宁可举一个中性的例子，或者干脆不举 —— 开场就报出三个「他在学的主题」"
    "是最像猜的一种说话方式。"
)

# 检索纪律**只在挂了工具时**才进提示词：它提到"工具""查""最后一轮工具会被收走"，
# 摆在极简模式里等于又告诉模型"你是有工具的"—— 第一版踩了这个坑（人看不出来，
# 模型会照着演）。
TOOL_DISCIPLINE = (
    "**调用工具前后那半句话也必须是中文**。"
    "凡引用材料，必须来自工具返回的出处，**不要凭记忆编材料名或行号**；"
    "工具查不到就直说没查到，然后用你自己的理解回答，并说明这部分没有材料支撑。\n"
    # 2026-09-26 实测踩过：子代理把 `gpu-3926` 的形状当成"编号规律"，于是"推"出了
    # `gpu-4c7d`《1994-2000 图形工作站与PC的融合》这样一个**库里根本不存在**的条目，
    # 还标了行号。根子在 slug 是入库时从文件名派生的残片（中文全被丢掉），它**不是名字**。
    "**`slug` 是编号，不是标题**：它是入库时从文件名/标题派生出来的，常常只是个残片"
    "（`gpusgi`、`gpu-3926`、`doc-df51313b` 都是这种）—— **别把它当标题读，更别从它的"
    "形状去推断「库里还应该有什么」**，那条路上编出来的名字一定不存在。"
    "名字看 `title`；要判断「有没有 / 有几份」就 `list_materials` 列出来看。"
    "写 slug 或标题，只能照抄工具输出。\n"
    "说「我去查一下」然后就把话停在那里，等于什么都没做 —— 说完就去查，查完再说结论；"
    "但**查到够回答就收口**，别把回合全花在检索上（最后一轮工具会被收走，你必须开口）。\n"
    # 长工具链派给子代理（详细说明在 `run_subagent` 的工具描述里）：
    # 这是"怎么用工具"的一条纪律，与上面几条同一层。
    "**要串行跑很多次查询时，派给子代理**（`run_subagent`）：它在自己的上下文里"
    "试查询词、换说法、逐段找原文，你这边只收到一份结果 —— 也比你自己一步步调省得多。"
)

# 极简：没有任何工具，连"这个应用是干什么的"都不告诉它 —— 否则它就会猜。
MINIMAL_PROMPT = (
    "你是 QuizForge 里的聊天助手。这一版**没有任何工具**，也没有办法读取他的资料或"
    "记录：所有你需要的信息，只能来自他自己说的话。\n"
    "所以：不要猜他最近在学什么，不要说你「看得到 / 查得到」任何东西，也不要说"
    "「我看看你的掌握度」「我去查一下」这类话。要问就直接问，要聊就直接聊。\n"
    # 光说"没有工具"不够：**模型照样会把调用写进正文**。实测（用户原话："我这一轮
    # 写了极简模式，结果我让它跑 python"）：它回了一屏 `<||DSML|| invoke name="run_python">`，
    # 那一轮被判成协议泄漏、整段丢掉，人只看到"协议泄漏、本轮不作数" ——
    # 他的反应是"气死了，我还以为是故障"。所以这句要把"别试"说到底：
    # 不点名任何工具（原则照旧），只说不许把调用写成文本，并把该走的路指出来。
    "**也不要把工具调用写成文本**（哪怕你还记得它的名字和格式）：这一轮你只能说话。"
    "需要跑代码、查资料、出题时，直接说「这件事得换个带工具的模式 才能做」，"
    "不要写调用格式，也不要假装在做。\n"
    + VOICE
)

# 应用领域只在**挂了工具**时才交代：它要理解材料，就必须知道这是给谁用的。
# 但**领域是"材料库的主题"，不是"他在学什么"** —— 原来那句写的是"服务一个正在啃
# AI 加速器的学习者"，模型就顺杆爬成了"你最近在看 NOC、circular buffer 吧"。
DOMAIN = (
    "你是 QuizForge 的学习助手。这套工具的材料库主题是 AI 加速器"
    "（Tenstorrent / tt-metal）—— 那是**材料**的范围，不等于**他**在学什么："
    "他的方向、进度、最近在看哪一块，只有他自己说出来的才算数。"
)

# 每组一份片段，只有**挂了这一组**才进提示词。写的时候守住一件事：
# 片段里只说"能用什么、什么时候用"，不要把用不到的能力也念一遍。
GROUP_PROMPTS = {
    "notes": (
        "笔记（他自己的，只在他的库里找）：`search_notes` 跨库按内容找；"
        "`read_note` 读正文（长的要分次读 —— 别只看开头就下结论）。"
        "写有两种：`write_note` **追加到末尾**，`edit_note` **改某一处**"
        "（给它一段原文换成新的，要删就给它空串）—— **要改已有的内容用后者**，"
        "别拿追加去凑。两个写前都自动留快照、能撤回，写完把那句原文念给他听。"
    ),
    "library": (
        "资料：`attach_material` 按标题找条目、把正文节选读进上下文，`read_material` 精读某几十行。"
        "**找段落有两条**：`search_material` 搜**我在学的材料**（值得精读与出题的那一档），"
        "`search_library` 搜**资料库**（论文 / 白皮书这类「看看就好」的）。"
        "两边检索方式完全一样（字面 + 向量融合），**在一个里没找到就去另一个里再问一次** ——"
        "它们是两个书架，不是同一堆东西的两种搜法；只搜一个就说「没有」是最容易犯的错。"
        "**盘点「有没有 / 有几份」用 `list_materials`**（列 slug 与标题，没有盲区），"
        "别拿检索结果反推。"
        "`slug` 只是编号（`gpusgi`、`gpu-3926` 这种是文件名残片）—— **名字看 `title`**。"
        "引用原文时给出行号和材料名，别改写原话。"
    ),
    "graph": (
        "知识图谱：`search_knowledge` 找概念/知识点，`get_point_detail` 看一个点的定义、"
        "出处与挂着的题，`explore_graph` 沿关系走邻域（前置、后继、包含、易混）。"
        "问「该先学什么」「这两个有什么区别」时用它，别凭记忆答。"
    ),
    "quiz": (
        "**他的学习方式叫「递归学习法」**：一个概念不懂（比如提纲里的一个词），就求讲解；"
        "讲解里又冒出几个不懂的概念，就**一个一个问出去** —— 每个是一支；"
        "弄明白了再回到主线。所以他会频繁在某一轮之后**分叉**，那不是跑偏，是方法本身。\n"
        "他问「我这次学了什么 / 复盘一下 / 我是不是跑偏了 / 这样学对不对」时，"
        "**先看系统提示里那份「他的学习轨迹」再答** —— 那是每轮自动带上的仪表盘："
        "他探过哪些概念、停在哪几支、主线怎么走到这儿的都在上面。"
        "分叉的形状只存在于 `parent_id` 里，你手上的历史是拍平的一条线，凭印象说一定说错。\n"
        "题库与进度：查题用 `get_existing_questions`（默认**不带答案**），"
        "看掌握度用 `get_mastery`，看该复习什么用 `get_due_reviews`。\n"
        "要考他就调 `push_question` 推一张卡，或 `create_question` 现编一道 —— "
        "**卡片只在你真的调了工具时才会出现**，在文字里说「我给你推了一道」"
        "而他那边什么都没有，是最糟的一种回答。推了题就等他自己作答："
        "不要报答案、不要替他念选项，等他答完再讲；批改用 `grade_problem`。\n"
        "要动他的记录（收藏、标记掌握）走 `flag_question` / `mark_mastered` 的**提案**："
        "界面上会出现一张凭条，他点了才生效 —— 永远不要声称你已经替他改了记录。"
    ),
    "sandbox": (
        "沙箱两件事，别混：\n"
        "* **验证与算数**用 `run_python` —— 脚本**直接交给工具**（写进 `code` 参数），"
        "**不要在正文里贴代码、也不要贴你「预期」的输出**。"
        "这一次调用**会等它跑完**（通常几毫秒；第一次要等运行时启动，几秒）："
        "输出作为这次调用的结果回到你手里，所以**拿到结果再说话**，直接下结论。\n"
        "  **不要说**「脚本已经交进去了」「输出马上就到」「下一轮再看」这类话 —— "
        "那一轮就是现在。也**不许**在没有输出的时候声称跑出了什么。"
        "（这里原来写的是「调完就停、下一轮再说」；用户的原话是"
        "「让 agent 等命令返回了再说话」，所以改成了现在的等待。）\n"
        "* **可视化**（空间 / 时间结构：数据怎么流、流水线怎么排、瓶颈在哪）用 `render_demo` "
        "画一个能动的演示（里面已备好 React + JSX + d3 与现成组件，见该工具说明里的骨架 —— "
        "**别自己引库、也别手画坐标轴**）。\n"
        "不要因为「我做不到」就推辞，也不要把「做不到」当结论 —— 先看手上的工具能做到哪一步。"
    ),
    "web": (
        "联网有三个：`web_search` 搜公开网页（回标题 / 网址 / 摘要），`search_papers` "
        "搜**学术文献**（论文，回作者 / 年份 / 发表处 / 被引数 / 摘要），`read_web_page` "
        "把某一页读成正文。\n"
        "**两个搜索是两个索引，不是两种搜法**：要找**文献**（哪篇论文、谁做的、哪一年的、"
        "某个方法的出处、某个领域的综述）走 `search_papers`；要找**网页**（现在怎么做、"
        "某个工具或型号的参数、官方文档）走 `web_search`。\n"
        "**尤其当一个词是缩写、或者是人名时，先想 `search_papers`** —— 通用网页索引在"
        "这类词上是结构性做不到的：搜 `WCET` 会回来同名协会与词典释义，"
        "搜作者名会回来同名名人，**而那些都是有结果的搜索结果**，从「有没有结果」看不出错。"
        "两条路都给**主题词**与**原词**各自的给法：学术那边给主题词（按相关度排），"
        "网页那边挑特别的原词（按字面匹配）。\n"
        "**他的材料里没有**、或者他问的是外面的事才用它；材料库与图谱里查得到的，"
        "优先走那几路 —— 更准，也带行号与出处。"
        "**关于他自己的事不要上网找**（他学到哪、记过什么，网上没有）。\n"
        "**搜到要读**：摘要只是索引，常常正好缺了你要的那一句 —— 要引用事实就先 "
        "`read_web_page`。引用时**把网址或 DOI 写上**，让他能点开核对；读不到就说读不到，"
        "不要拿摘要凑，也不要因为没搜到就改口编。"
    ),
    "trees": (
        "**别的对话**在这一组。注意与你头顶那份「他的学习轨迹」的分工：那份是**这一条**"
        "对话的轨迹，每轮自动带上、不用你查；这一组管的是**别的**对话 —— "
        "`docs/对话树.md` §三 把这一层叫森林，规矩是**靠工具访问，不靠预先建边**："
        "跨树的连接不预先建好，而是「当场查」。\n"
        "**什么时候必须去翻**：他说「上次」「之前」「我以前问过」「我是不是学过这个」时，"
        "**先查再答**。你手上只有当前这条主线，而「我记得没讲过」这种话在搜过之前不该说 —— "
        "那是你的印象，不是记录。\n"
        "翻的顺序是三步，别跳：先 `list_conversations` 看有哪几段（标题、条数、最近动过的"
        "日子），再按内容 `search_conversations`（命中会给一句上下文），"
        "然后 `read_tree` 看那一棵的**形状**（主线怎么走、在哪儿下钻、每支多深），"
        "最后 `read_tree_nodes` 把需要逐字看的那几条取出来 —— 骨架里只有预览。\n"
        "**引用要带出处**：哪一段对话、第几条消息。别把两段揉成一句「你之前说过」——"
        "那等于把出处丢掉了，他回头看会对不上。\n"
        "**只读**：翻到的任何东西都不许在其中写一个字（这一组四个工具全是只读档）。"
    ),
}

# 组名 → 中文，未挂时用来告诉模型"别提这些"。
GROUP_LABELS = {
    "notes": "笔记", "library": "资料", "graph": "知识图谱", "quiz": "题库与进度",
    "sandbox": "沙箱", "web": "联网", "trees": "对话树",
}


from .. import tools as tool_registry  # noqa: E402  提示词要点名工具，得问注册表

#: 组 → 里面的工具名（**兜底**）。正常路径是调用方把"这一轮真声明了哪些工具"传进来 ——
#: 因为同一个组里还分权限档：查询模式挂着 `notes` 组，但 `write_note`（write 档）没声明，
#: 提示词里就不能念它（念了它就会去调，然后被拒）。 —— 只说"你有笔记工具"是不够的：
#: 实测模型会坚持"我没有 search_notes 这个东西"，点到名字才肯动手。
#: 有测试盯着它和 `tools.REGISTRY` 一致（抄成两份必然漂）——
#: 见 `tests/test_prompt.py::test_every_group_has_a_prompt_a_label_and_its_tools`。
GROUP_TOOLS = {
    # `list_notes` / `read_note` 原先漏在这儿了 —— 而这一条正是"清单会漂"的现场：
    # 它是**兜底**，正常路径由调用方传入真声明的工具名，所以漂了也看不出来。
    # 兜底一旦被用到（`declared` 没传），模型会被告知"你只有那两个工具"。
    "notes": ("list_notes", "read_note", "search_notes", "write_note", "edit_note"),
    "library": ("list_materials", "attach_material", "search_material", "search_library",
                "read_material"),
    "graph": ("search_knowledge", "get_point_detail", "explore_graph"),
    # `read_learning_tree` **不再是工具**：轨迹是每轮自动带上的仪表盘
    #（见 `recursion.dashboard` 与 `build_prompt(..., dashboard=...)`）。
    "quiz": ("get_existing_questions", "get_mastery", "get_due_reviews",
             "grade_problem", "flag_question", "mark_mastered", "push_question",
             "create_question"),
    "sandbox": ("run_python", "render_demo"),
    "web": ("web_search", "search_papers", "read_web_page"),
    # 森林那一层（`docs/对话树.md` §三）：翻**别的**对话。四个全是只读档。
    "trees": ("list_conversations", "search_conversations", "read_tree", "read_tree_nodes"),
}


def capability_line(mounted, declared=None) -> str:
    """**当前能力**的权威声明 —— 每一轮都带，不看历史、不看快照。

    为什么不能只靠"模式变了才提示"：那个判断要读"上一条用户消息当时是什么模式"，
    而老消息的快照列是空的（快照功能是后加的）→ 读出来是 [] → 与"现在是极简"
    一比竟然算作"没变"，于是**一个字都不提示**。用户实测踩到两次。
    所以这里改成无条件的：把"现在能用什么"写死在每一轮的提示里，并明确
    "对话历史里你对自己能力的说法，一律以本条为准"。
    """
    now_keys = [k for k in GROUP_PROMPTS if k in set(mounted or ())]
    if not now_keys:
        head = "【当前这一版】极简：**你没有任何工具**，也读不到他的笔记、资料、知识图谱、题库、掌握度。"
    else:
        if declared is None:
            names = "、".join("`%s`" % n for k in now_keys for n in GROUP_TOOLS.get(k, ()))
        else:
            names = "、".join("`%s`" % n for n in declared)
        head = ("【当前这一版】" + "、".join(GROUP_LABELS[k] for k in now_keys)
                + "：你能用这些工具：" + names + "。")
    return (head + "\n"
            "**对话历史里你此前对自身能力的任何描述（「我有…」「我没有…」"
            "「我查不了」）一律以本条为准** —— 与本条冲突时，说明模式换过了，按本条来。")


def mode_notice(previous, mounted, declared=None) -> str:
    """模式在**一个对话内**变了 —— 这一条必须跟着本轮提示走。

    为什么非要它：光把系统提示换成新的不够。用户实测过一次：切到"查询"之后模型
    仍然说"我这边没有拿到任何工具"，因为**它自己前面刚说过三遍"我没有工具"**，
    它更信自己刚说的话。所以这里明说"变的是什么"、"现在有哪些工具"、
    以及"历史里那些话作废"。反过来（工具被收走）也要说，否则它会去调不存在的工具。
    """
    now_keys = [k for k in GROUP_PROMPTS if k in set(mounted or ())]
    was_keys = [k for k in GROUP_PROMPTS if k in set(set() if previous is None else set(previous))]
    if set(now_keys) == set(was_keys):
        return ""
    was = "、".join(GROUP_LABELS[k] for k in was_keys) or "极简（没有任何工具）"
    if not now_keys:
        return (
            "【这一轮的变更】用户刚把模式换成了「极简」：**从现在起你没有任何工具**。"
            "不要再调用任何工具，也不要再引用笔记、资料、知识图谱、题库里的内容 —— "
            "你看不到它们了。"
        )
    if declared is None:
        names = "、".join("`%s`" % n for k in now_keys for n in GROUP_TOOLS.get(k, ()))
    else:
        names = "、".join("`%s`" % n for n in declared)
    return (
        "【这一轮的变更】用户刚把模式从「" + was + "」换成了「"
        + ("、".join(GROUP_LABELS[k] for k in now_keys)) + "」。"
        "**从这一轮起**你能用这些工具：" + names + "。\n"
        "对话历史里你此前说过「我没有工具」「我查不了」「工具没挂上」这类话 —— "
        "**那些话从现在起作废，不要再重复**；用户要你查什么、要你做什么，直接调工具给他结果。"
    )


def build_prompt(
    mounted, previous=None, declared=None, dashboard: str = "", skill_block: str = ""
) -> str:
    """按这一版挂载的组拼提示词。空集 = 极简模式（见 MINIMAL_PROMPT）。

    `previous` 是**上一条用户消息当时挂载的组**；与现在不同时，末尾追加一条
    "模式变了"的实时提示（见 `mode_notice`）。

    `dashboard` 是 `recursion.dashboard` 算出来的「他的学习轨迹」，**两种模式都带上**：
    它不是工具（用户："对话轨迹是 llm 长期可见的用户信息仪表盘"），不受挂载影响 ——
    极简模式只是"没有工具"，不是"不认识这位用户"。

    `skill_block` 是他用 `/` 召唤的那个流程（`app/skills.py`），同样两种模式都带 ——
    它与挂载无关：那是"他要求我这一段怎么陪他"，不是"我能查什么"。放在靠后的位置：
    它是**这一轮的行为约束**，与 `mode_notice` 一样属于"现在最要紧的"。
    """
    keys = [k for k in GROUP_PROMPTS if k in set(mounted or ())]
    if not keys:
        # 极简是一份完全不同的提示词，但它**同样要带上"当前能力"那一行** ——
        # 原先这里直接 return，于是模型会继续假装自己能查（用户实测：切到极简后
        # 它还在报上一轮的四条线）。"模式变了"那条作为加强，能判断出来就加。
        out = MINIMAL_PROMPT + "\n" + capability_line(mounted, declared)
        if previous:
            note = mode_notice(previous, mounted, declared or [])
            if note:
                out += "\n" + note
        if dashboard:
            out += "\n\n" + dashboard
        if skill_block:
            out += "\n\n" + skill_block + "\n\n" + SKILL_TAIL
        return out
    off = [GROUP_LABELS[k] for k in GROUP_PROMPTS if k not in set(mounted or ())]
    parts = [
        DOMAIN + VOICE + TOOL_DISCIPLINE,
        capability_line(mounted, declared),          # 现在有什么：无条件、权威
        "\n".join(GROUP_PROMPTS[k] for k in keys),
    ]
    if off:
        parts.append(
            "**这一版没有挂**：" + "、".join(off) + "。这些你看不到，"
            "不要提议去查它们，也不要假装能看到。"
        )
    if dashboard:
        parts.append(dashboard)       # 「他是谁、走到哪儿了」：每轮都带（见 recursion.dashboard）
    if skill_block:
        parts.append(skill_block + "\n\n" + SKILL_TAIL)
    notice = mode_notice(previous, mounted, declared)
    if notice:
        parts.append(notice)          # 放最后：这是"现在"最要紧的一条
    return "\n".join(parts)


#: 带着流程时，末尾那句提醒。放在**最靠后**（紧挨 `mode_notice`）是因为它压过一切：
#: 一条"要不要换种讲法"的风格建议，不该把"讲的人是他"这件事盖掉。
SKILL_TAIL = (
    "**上面这个流程压过别的风格要求**：它管的是这一轮**谁在做主**。"
    "与他平时的偏好（口吻、详细程度）冲突时，以流程为准。"
)

#: 思考强度那一档，写进系统提示的那一句。
#:
#: 为什么除了给上游传参数，还要**说出来**：上游认的只有"开/关"（见
#: `ai_gateway.THINK_LEVELS`），中间几档没有可靠的字段可传。而用户抱怨的
#: overthinking（"一个简单的问题思考特别久"）恰恰发生在**开着思考**的时候 ——
#: 所以"省着点"这一档必须靠说清"这一轮该想多深"来落地。
#:
#: 写法上有个讲究：**说"别 overthinking"没用**，得给它一个可执行的判据
#: （先给答案 / 遇到分叉才多想一步 / 想不下去就停下来问）。`normal` 那档不加 ——
#: 不加就是原来的行为，别在提示词里塞一句等于没说的话。
_THINKING_LINES: dict[str, str] = {
    "off": (
        "**这一轮不要思考，直接答。** 他按了「不想」—— 有把握就直接给结论，"
        "拿不准就说不准，不要展开推理，也不要列「我先想想」这种过场。"
    ),
    "low": (
        "**这一轮想少一点。** 这是个简单问题：先给答案，能一句话说清就一句话。"
        "只有真遇到分叉（两种说法都讲得通）才多想一步；想不下去就停下来问他，"
        "别自己绕圈。"
    ),
    "high": (
        "**这一轮多想几步。** 他按了「使劲」：把前提、边界、反例都过一遍，"
        "把没把握的地方**说清是哪里没把握**，别拿一堆肯定句凑长度。"
    ),
}


def thinking_line(effort: object) -> str:
    """思考强度 → 系统提示末尾那一句（`normal` 与不认识的值都不加）。"""
    from .. import ai_gateway

    line = _THINKING_LINES.get(ai_gateway.thinking_level(effort), "")
    return "\n\n" + line if line else ""


# 消息最长留 **20 万字**：够贴一整篇论文 / 技术文章，又还挡得住"把整本书灌进来"。
#
# 这条以前是 8000 —— 实测撞到：用户贴了一篇稿子，**前半还在、后半没了**，而界面上
# 一声不吭（他是从模型嘴里知道的："你稿子最后一句被截断了"，库里那条消息的字数
# 正好 8000）。8000 字对"贴一段材料"也许够，对"贴一篇文章"远远不够 ——
# 而**截断这件事绝不该悄悄发生**：真要超（20 万也很少有人撞到），前端会明说。
MAX_CONTENT = 200000

# 一条消息最多挂几处引用：再多就成了引文清单，而不是对话
CITATION_MAX = 6

# 图片发给模型时的两道闸：单张上限与每轮张数上限。
# base64 之后体积要涨三分之一，而这图会进每一次请求 —— 一张 4MB 的照片
# 换来的上下文占用，比它带来的信息值钱得多。
VISION_MAX_BYTES = 4 * 1024 * 1024
VISION_MAX_IMAGES = 3

# 沙箱输出存下来时留多少字（回放给模型时另按 HISTORY_RUN_CHARS 夹一次）
RUN_TEXT_LIMIT = 40_000
# 回放沙箱输出给模型时留多少字。它会被每一轮重新带上，所以要比"存下来的"更狠
HISTORY_RUN_CHARS = 1600

# 回放历史时，单条工具输出最多带这么多字。
# 工具输出会被**每一轮**重新带上，而它往往比对话本身长（一次知识点详情上千字）；
# 实测不夹紧的话，第二轮请求的 prompt 就涨到撞超时。
# 4000 字是"给模型看一次够用"，这里要的是"一直背着也不贵"。
HISTORY_TOOL_CHARS = 700

# 边收边写的间隔：用户停下时，他屏幕上已经出现的字必须已经在库里。
# 值取 1.5 秒 —— 比一次 UPDATE 的成本重要得多的是「停下就留住了」。
SPILL_SECONDS = 1.5

# 超过这么久还停在 streaming 的消息，按「被中断但没来得及收尾」处理。
# 判据用 created_at 而不是 updated_at —— 流式期间我们不写库（省流量），
# 所以 updated_at 停在创建那一刻。代价是：真的连续生成超过 5 分钟会被误判为中断，
# 而这在这个规模上不会发生（真发生了，用户看到的是「已中断 + 已有内容」，不丢东西）。
STALE_STREAM_SECONDS = 300


# ------------------------------------------------------------------ 小工具


def _own_conversation(db: DbSession, cid: uuid.UUID) -> Conversation:  # noqa: ANN001
    """取出这条会话（不存在就 404）。"""
    conv = db.get(Conversation, cid)
    if conv is None:
        raise HTTPException(404, "没有这个对话")
    return conv


def _message_out(m: Message) -> dict:
    return {
        "id": m.id,
        "conversationId": str(m.conversation_id),
        "parentId": m.parent_id,
        "role": m.role,
        "content": m.content,
        "status": m.status,
        "error": m.error,
        "finishReason": m.finish_reason,
        "model": m.model,
        # 这一轮挂载了哪几组工具（老消息是空列表：那时还没记这一项）
        "mounts": [str(one) for one in as_json(m.mounts, []) or []],
        "promptTokens": m.prompt_tokens,
        "completionTokens": m.completion_tokens,
        "latencyMs": m.latency_ms,
        "createdAt": m.created_at.isoformat() if m.created_at else None,
        # 毫秒时间戳与 records.last_at 同一风格：前端的 fmtRelative / fmtTime 直接吃它
        "createdAtMs": int(m.created_at.timestamp() * 1000) if m.created_at else 0,
        # 零件是真相，content 是它的正文投影；旧消息没有 parts，按 content 兜一个
        "parts": msgparts.normalize(m.parts, m.content),
    }


def _ensure_folder(db, path: str) -> None:  # noqa: ANN001
    """保证这个分组**及其各级父分组**都登记在册（幂等；不提交）。"""
    if not path:
        return
    parts = path.split("/")
    for i in range(1, len(parts) + 1):
        one = "/".join(parts[:i])
        exists = db.execute(
            select(func.count())
            .select_from(ConversationFolder)
            .where(ConversationFolder.path == one)
        ).scalar()
        if not exists:
            db.add(ConversationFolder(path=one))


def _conversation_out(c: Conversation, count: int, preview: str) -> dict:
    return {
        "id": str(c.id),
        "title": c.title,
        "messageCount": count,
        "preview": " ".join((preview or "").split())[:80],
        "pinned": bool(c.pinned),
        # 归档：前端把它摆进左栏的"归档"区（没有归档的落在"未归档"区）
        "archived": bool(c.archived),
        # 所属分组（路径，`''` = 根）。**左栏已经不画目录树了**（改成"置顶 / 归档 /
        # 未归档"三区，见 chat.js 的 renderZones）—— 这个字段留着是因为数据在、
        # 接口还有人用，删它要动的东西比留它多。
        "folder": c.folder or "",
        "createdAt": c.created_at.isoformat() if c.created_at else None,
        "updatedAt": c.updated_at.isoformat() if c.updated_at else None,
        "updatedAtMs": int(c.updated_at.timestamp() * 1000) if c.updated_at else 0,
    }


def _sse(event: str, data) -> str:  # noqa: ANN001
    """一条 SSE 记录。

    不加 `id:` 字段：那是给 `Last-Event-ID` 断点续传用的，我们现在不支持 ——
    加上它等于暗示支持。JSON 会把换行转义掉，所以不会破坏 `\\n\\n` 分帧。
    """
    return "event: " + event + "\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n"


def _title_from(text: str) -> str:
    line = " ".join((text or "").split())[:24]
    return line + ("…" if len(" ".join((text or "").split())) > 24 else "")


def _leaf_id(db: DbSession, conv: Conversation) -> int | None:  # noqa: ANN001
    """这条会话里最新的一条消息 —— 新消息挂在它下面。

    严格说「当前分支的叶节点」才是正确起点（树可能分叉），但最新的一条
    必然属于某条分支的末端，而界面这一版只画一条线，所以等价。
    """
    return db.scalar(
        select(Message.id)
        .where(Message.conversation_id == conv.id)
        .order_by(Message.id.desc())
        .limit(1)
    )


def _chain(db: DbSession, node: Message | None) -> list[Message]:  # noqa: ANN001
    """从这条消息沿 `parent_id` 走回根（含自身），返回正序。

    这一个函数同时解决两件事：

    * **界面只画一条线**：库里是树（再生成会分叉），若把所有消息按 id 排出来，
      一次再生成就会显示成两条重复回答；走一遍父亲链正好只拿到当前那条。
    * **上下文从哪儿截**：喂给模型的必须是「到某条消息为止」的那一段。
      再生成时若把当前分支（含上一条回答）当上下文，模型会顺着自己的旧答案
      往下写，而不是重新回答 —— 那就不叫重新生成了。
    """
    chain: list[Message] = []
    seen: set[int] = set()
    while node is not None and node.id not in seen and len(chain) < 500:
        chain.append(node)
        seen.add(node.id)
        node = db.get(Message, node.parent_id) if node.parent_id else None
    chain.reverse()
    return chain


def _active_path(db: DbSession, conv: Conversation) -> list[Message]:  # noqa: ANN001
    """当前分支 = 最新那条消息所在的链。"""
    node = db.scalar(
        select(Message)
        .where(Message.conversation_id == conv.id)
        .order_by(Message.id.desc())
        .limit(1)
    )
    return _chain(db, node)


def _heal_stale(db: DbSession, conv: Conversation) -> None:  # noqa: ANN001
    """把「流到一半没人收尾」的消息标成中断。

    正常路径由 `_finish` 收尾；但进程被杀、连接被中间层掐断这些情况下，
    库里会留一条永远的 `streaming`。读的时候顺手治一下，比留一个定时任务简单，
    也比让界面永远转圈诚实。
    """
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=STALE_STREAM_SECONDS)
    rows = db.scalars(
        select(Message).where(
            Message.conversation_id == conv.id,
            Message.status == "streaming",
            Message.created_at < cutoff,
        )
    ).all()
    if not rows:
        return
    for m in rows:
        m.status = "partial" if m.content else "error"
        if not m.content:
            m.error = "生成中断（服务端没有收到收尾信号）"
    db.commit()


def _attachment_rows(db: DbSession, message: Message) -> list:  # noqa: ANN001
    """这条消息挂的附件行（按零件顺序）。"""
    ids = []
    for part in message.parts or []:
        if isinstance(part, dict) and part.get("type") == "file":
            try:
                ids.append(uuid.UUID(str(part.get("attachmentId"))))
            except (TypeError, ValueError):
                continue
    if not ids:
        return []
    rows = db.scalars(select(Attachment).where(Attachment.id.in_(ids))).all()
    order = {str(value): index for index, value in enumerate(ids)}
    return sorted(rows, key=lambda row: order.get(str(row.id), 0))


def _attachment_images(db: DbSession, message: Message) -> list[dict]:  # noqa: ANN001
    """图片附件 → 上游能吃的图像块（data URL）。

    只在模型能读图时调用（见 `ai_gateway.model_reads_images`）。
    单张限 `VISION_MAX_BYTES`、最多 `VISION_MAX_IMAGES` 张：base64 之后体积还要涨三分之一，
    而这张图会进每一次请求的上下文。
    """
    blocks = []
    for row in _attachment_rows(db, message):
        if row.kind != "image" or (row.size or 0) > VISION_MAX_BYTES:
            continue
        if len(blocks) >= VISION_MAX_IMAGES:
            break
        mime = str(row.mime or "").strip()
        if mime not in gateway.VISION_MIMES:
            # bmp / svg 存得下，但上游只收 png/jpeg/webp/gif（多传会 400）。
            # 不静默丢掉：说明里会讲清是哪张、为什么没给出去。
            continue
        try:
            raw = attach.path_of(row).read_bytes()
        except OSError:
            continue
        blocks.append(
            {
                "type": "image_url",
                "image_url": {"url": "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii")},
            }
        )
    return blocks


def _attachment_block(db: DbSession, message: Message, *, model: str = "", vision: bool = False) -> str:  # noqa: ANN001
    """把这条消息挂的附件正文拼成一段，附在**发给模型的那一份**后面。

    刻意不写进库里那条 `content`：那是用户说的话，规矩是"原样留着他说的"。
    抽不到正文的（图片）也要说清是怎么回事 —— 但**别只说"读不到"**：

    * 模型能读图（`vision`）→ 图像已经随消息发过去了，这里只留一行"有图"的说明
    * 读不了 → 说清是**哪个模型**读不了、以及能怎么办（抄文字 / 换个能读图的模型）。
      原先只说"读不到"，用户看到的是一句无解的话（实测他就是这么问回来的）。
    """
    rows = _attachment_rows(db, message)
    if not rows:
        return ""

    blocks = []
    for row in rows:
        head = f"【附件：{row.name}（{row.size // 1024}KB）】"
        if row.text:
            blocks.append(head + "\n" + row.text[: attach.CONTEXT_LIMIT])
        elif row.kind == "image":
            mime = str(row.mime or "").strip()
            if vision and mime in gateway.VISION_MIMES:
                blocks.append(head + "\n（这是一张图片，图像已随这条消息交给你，可以直接看图回答。）")
            elif vision:
                blocks.append(
                    head
                    + "\n（这是一张图片，格式是 "
                    + (mime or "未知")
                    + "：上游只接受 png / jpeg / webp / gif，所以这张没能交给你。"
                    "跟他说一声，让他转成 png 或 jpg 再发。）"
                )
            else:
                blocks.append(
                    head
                    + "\n（这是一张图片。当前模型"
                    + (f"（{model}）" if model else "")
                    + "读不到图像内容：你只知道他贴了一张图，看不到里面是什么。"
                    "别猜图里有什么 —— 直接说你需要他把关键内容抄成文字，"
                    "或者让他换一个能读图的模型。）"
                )
        else:
            blocks.append(head + "\n（没能从里面抽出文本。）")
    return "\n\n".join(blocks)


def _history(db: DbSession, messages: list[Message], *, vision: bool = False, model: str = "") -> list[dict]:  # noqa: ANN001
    """把当前分支转成上游要的 messages。

    ## 为什么必须回放工具调用

    这里原先只回放文本（当时记下的一个取舍：工具输出比对话还长，怕上下文膨胀）。
    它的代价实测出来了：模型看不见自己调过什么工具，历史读起来就是
    「我说了句『让我查一下』，然后直接给结论」—— 于是它照着学，
    也先交代一句、然后什么都不做就停下。问「考我一道」有一半次数
    只得到一句英文承诺，就是这么来的。

    所以 assistant 消息要带上 `tool_calls`，紧跟对应的 `role: tool` 结果 ——
    与循环里发给上游的形状完全一样。代价是上下文长一些，
    但预算机制本来就按"从最新往前塞"裁剪，工具输出也各自截断过。

    ## 图片只在"最新那条"上带

    `vision` 为真（模型能读图）时，用户消息里的图片会作为图像发出去。
    但**只带最后一条**：图像很占上下文，历史里每轮重发一次既贵又没意义 ——
    上一轮的图，上一轮已经讲过了。

    系统提示**不在这里加**：它由 `agent_loop.build_messages` 统一放进去，
    两边各加一次会让模型收到两条 system 消息（白花 token，还容易被带偏）。
    """
    out: list[dict] = []
    last_user_id = messages[-1].id if messages else None
    for message in messages:
        if message.role == "user":
            text = message.content or ""
            extra = _attachment_block(db, message, model=model, vision=vision)
            if extra:
                text = (text + "\n\n" + extra).strip()
            images = (
                _attachment_images(db, message)
                if vision and message.id == last_user_id
                else []
            )
            if images:
                # OpenAI 兼容的多模态形状：文本块 + 图像块
                content = [{"type": "text", "text": text or "（见附件）"}] + images
                out.append({"role": "user", "content": content})
            elif text:
                out.append({"role": "user", "content": text})
            continue
        if message.role != "assistant":
            continue

        calls = [
            part
            for part in (message.parts or [])
            if isinstance(part, dict) and part.get("type") == "tool_call" and part.get("name")
        ]
        text = message.content or ""

        # 沙箱跑出来的输出也回放进来：模型于是能读到自己那段代码的结果。
        # 原先它读不到 —— 只能靠用户在面板上按一下「把输出发给它」再转述，
        # 那是把用户当传话筒。输出由前端回填（见 attach_run），这里只是把它带上。
        runs = [
            str((part.get("run") or {}).get("text") or "").strip()
            for part in (message.parts or [])
            if isinstance(part, dict)
            and part.get("type") == "demo"
            and isinstance(part.get("run"), dict)
        ]
        for output in runs:
            if output:
                text = (text + "\n\n〔沙箱输出〕\n" + msgparts.clip(output, HISTORY_RUN_CHARS)).strip()

        if not calls:
            if text:
                out.append({"role": "assistant", "content": text})
            continue

        ids = [
            str(part.get("id") or ("call_" + str(index)))
            for index, part in enumerate(calls)
        ]
        out.append(
            {
                "role": "assistant",
                "content": text,
                "tool_calls": [
                    {
                        "id": ids[index],
                        "type": "function",
                        "function": {
                            "name": part["name"],
                            "arguments": json.dumps(
                                part.get("args") or {}, ensure_ascii=False
                            ),
                        },
                    }
                    for index, part in enumerate(calls)
                ],
            }
        )
        for index, part in enumerate(calls):
            # 回放时把工具输出**夹紧**：它会被每一轮重新带上，4000 字一次的历史
            # 会让第二轮就撞上超时（实测：整份题卡 + 掌握度列表 → 91 秒）。
            # 模型真要那些内容，它会再调一次工具 —— 那比一直背着便宜。
            output = msgparts.clip(str(part.get("output") or ""), HISTORY_TOOL_CHARS)
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": ids[index],
                    "content": output or "（这次调用没有返回内容）",
                }
            )
    return out


# ------------------------------------------------------------------ 会话


def _heal_folder_archived(db: DbSession) -> None:  # noqa: ANN001
    """把「在分组里却没归档」的会话修回来。

    这个组合是**旧代码**造出来的：`create_conversation` 收下 `folder` 时没有
    同时置 `archived`（前端从分组里点「新对话」会把当前分组带上），而左栏那棵树
    是按 `archived` 渲染的 —— 于是这些会话在界面上**看不见**，却仍被
    `delete_folder` 照 `folder` 算进分组里：用户看到的是"分组里明明没东西，
    却删不掉"。

    读列表时顺手治一次，与 `_heal_stale` 同一套做法（比留一个一次性脚本可靠：
    谁的库都不会漏掉）。已经一致时它只做一次 SELECT、不写库。
    """
    rows = db.scalars(
        select(Conversation).where(
            Conversation.folder != "",
            Conversation.archived.is_(False),
        )
    ).all()
    if not rows:
        return
    for conv in rows:
        conv.archived = True
    db.commit()


@router.get("/conversations")
def list_conversations(db: DbSession) -> dict:
    """会话列表：标题、条数、最后一句话的预览。

    排序 = **置顶的在前**，各自按最近活动倒序。置顶是自己钉的
    （"这条我在攻"），所以它必须压过"谁最近动过"。
    """
    # 先把"在分组里却没归档"的历史数据修回来（见 `_heal_folder_archived`）。
    # 不修的话它们在树上不显示，既看不见也挪不动 —— 只能对着一个删不掉的
    # 分组发呆（那正是报上来的现象）。
    _heal_folder_archived(db)

    rows = db.execute(
        select(Conversation, func.count(Message.id))
        .join(Message, Message.conversation_id == Conversation.id, isouter=True)
        .group_by(Conversation.id)
        .order_by(Conversation.pinned.desc(), Conversation.updated_at.desc())
        .limit(200)
    ).all()

    # 每个会话的最后一条消息：一次取回，避免 N+1。
    #
    # 用**窗口函数**而不是 `DISTINCT ON`：后者是 Postgres 专有，SQLite 上会被
    # **静默忽略** —— 那比报错更糟（列表里的预览会变成随便哪一条）。
    ranked = (
        select(
            Message.conversation_id.label("cid"),
            Message.content.label("content"),
            func.row_number()
            .over(partition_by=Message.conversation_id, order_by=Message.id.desc())
            .label("rank"),
        )
        .subquery()
    )
    last = {
        row.cid: row.content
        for row in db.execute(select(ranked.c.cid, ranked.c.content).where(ranked.c.rank == 1))
    }
    # 分组：表里那些（含**空**分组）+ 会话自己带的那些（老数据可能没登记过）。
    # 一次请求全给出去：左栏那棵树要"目录 + 会话"一起才画得出来。
    names = {
        str(row[0])
        for row in db.execute(select(ConversationFolder.path))
    }
    for c, _n in rows:
        if c.folder:
            names.add(c.folder)
            # 父级也要在：`a/b` 存在就说明 `a` 也是分组（可能是搬迁留下来的）
            parts = c.folder.split("/")
            for i in range(1, len(parts)):
                names.add("/".join(parts[:i]))

    return {
        "conversations": [_conversation_out(c, int(n or 0), last.get(c.id, "")) for c, n in rows],
        "folders": sorted(names),
    }


# ------------------------------------------------------------------ 分组


MAX_FOLDER_LEN = 240


def _clean_folder(raw) -> str:  # noqa: ANN001
    """把用户给的名字/路径整理成一个干净的路径：去掉空段与 `.`/`..`，限长。

    `.` 与 `..` 必须去掉 —— 在这一层它们只是**字符串**（不是真文件系统），
    留着只会在界面上长出一个叫 `..` 的目录，谁也说不清它是什么意思。
    """
    text = str(raw or "").replace("\\", "/")
    parts = [p.strip() for p in text.split("/")]
    return "/".join(p for p in parts if p and p not in (".", ".."))[:MAX_FOLDER_LEN]


def _folder_rows(db, path: str):  # noqa: ANN001, ANN202
    """这个分组**及其子树**（按路径前缀认子树）。"""
    return (
        db.execute(
            select(ConversationFolder).where(
                or_(ConversationFolder.path == path, ConversationFolder.path.like(path + "/%")),
            )
        )
        .scalars()
        .all()
    )


@router.post("/folders")
def create_folder(payload: dict, db: DbSession) -> dict:
    """新建一个分组（连同它的各级父分组）。**幂等**：同名再点一次不报错。

    父级一起建：`a/b` 建出来而 `a` 不在的话，树里会凭空多出一层没有名字的中间层。
    """
    path = _clean_folder((payload or {}).get("path"))
    if not path:
        raise HTTPException(400, "分组名不能为空")
    _ensure_folder(db, path)
    db.commit()
    return {"ok": True, "path": path}


@router.patch("/folders")
def rename_folder(payload: dict, db: DbSession) -> dict:
    """改名 / 搬家：整个子树一起走（分组记录 + 里面的会话）。

    目标已存在时**拒绝**，不合并：两个分组悄悄并成一个，用户回头会找不到东西。
    """
    body = payload or {}
    old = _clean_folder(body.get("path"))
    new = _clean_folder(body.get("to"))
    if not old or not new:
        raise HTTPException(400, "分组名不能为空")
    if new == old:
        return {"ok": True, "path": new}
    if new.startswith(old + "/"):
        raise HTTPException(400, "不能把分组挪进它自己里")
    exists = db.execute(
        select(func.count())
        .select_from(ConversationFolder)
        .where(ConversationFolder.path == new)
    ).scalar()
    if exists:
        raise HTTPException(409, "已经有这个分组了")

    # 前缀替换：`old` 与 `old/...` 一律换成 `new` 开头（会话与分组记录两边都要）
    for conv in (
        db.execute(
            select(Conversation).where(
                or_(Conversation.folder == old, Conversation.folder.like(old + "/%")),
            )
        )
        .scalars()
        .all()
    ):
        conv.folder = new + (conv.folder or "")[len(old):]
    for folder in _folder_rows(db, old):
        folder.path = new + folder.path[len(old):]
    # 先把上面的改名**落库**，再问"新名字在不在"。
    # 踩过：会话是 `autoflush=False`，不 flush 的话 `_ensure_folder` 数的还是改名前的
    # 库（数到 0）→ 又插一条新名字 → 提交时撞 UNIQUE（子树自己已经占着那个名字了）。
    db.flush()
    _ensure_folder(db, new)
    db.commit()
    return {"ok": True, "path": new}


@router.delete("/folders")
def delete_folder(path: str, db: DbSession) -> dict:
    """删一个分组。**里面还有东西就拒绝** —— 与 `rmdir` 一样。

    用户的会话是最贵的产物，一个误点不该连带删掉一堆；想清空就先自己挪出来。
    """
    target = _clean_folder(path)
    if not target:
        raise HTTPException(400, "分组名不能为空")
    rows = _folder_rows(db, target)
    if not rows:
        raise HTTPException(404, "没有这个分组")
    if len(rows) > 1:
        raise HTTPException(400, "这个分组里还有子分组")
    inside = db.execute(
        select(func.count())
        .select_from(Conversation)
        .where(
            or_(Conversation.folder == target, Conversation.folder.like(target + "/%")),
        )
    ).scalar()
    if inside:
        raise HTTPException(400, "这个分组里还有 " + str(inside) + " 个对话")
    for folder in rows:
        db.delete(folder)
    db.commit()
    return {"ok": True}


@router.get("/search")
def search_messages(
    db: DbSession,
    q: str = Query("", description="在消息正文里搜一段字"),
    limit: int = Query(20, ge=1, le=50),
) -> dict:
    """在自己所有对话的消息里搜一段字 —— 「我到底在哪儿学过这个」。

    为什么是 ILIKE 而不是全文索引：中文没有词边界，PG 自带的 FTS 对中文等于没用
    （要装分词扩展），而这里要的恰恰是**子串**命中。消息是千级、一次查回内存，
    ILIKE 够用且零依赖 —— 这也正是 LibreChat 在这个位置上用 MeiliSearch
    而我们不需要它的原因。

    两个字的门槛是刻意的：一个字（"的"、"是"）会命中几乎全部消息，那不是搜索。
    """
    query = " ".join((q or "").split())
    if len(query) < 2:
        return {"items": [], "query": query, "note": "至少输入两个字"}

    rows = db.execute(
        select(Message, Conversation.title)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Message.content.ilike("%" + query + "%"))
        .order_by(Message.id.desc())
        .limit(limit)
    ).all()

    return {
        "items": [
            {
                "messageId": m.id,
                "conversationId": str(m.conversation_id),
                "title": title or "未命名对话",
                "role": m.role,
                "snippet": _snippet(m.content, query),
                "atMs": int(m.created_at.timestamp() * 1000) if m.created_at else 0,
            }
            for m, title in rows
        ],
        "query": query,
    }


def _snippet(content: str, query: str, span: int = 40) -> str:
    """命中处前后各留一点：列表里要能一眼看出"是不是我要找的那句"。"""
    text = " ".join((content or "").split())
    index = text.lower().find(query.lower())
    if index < 0:
        return text[: span * 2] + ("…" if len(text) > span * 2 else "")
    start = max(0, index - span)
    end = min(len(text), index + len(query) + span)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


@router.post("/conversations")
def create_conversation(payload: dict, db: DbSession) -> dict:
    """新建一个空会话。标题留空，等第一条消息自动生成。

    可以带 `folder` 指定它落在哪个分组里 —— 在某个分组上右键「新建对话」时，
    新建的那条就该在那个分组里；只在根上开、再让用户自己拖进去是多余的一步。
    """
    body = payload or {}
    title = str(body.get("title") or "").strip()[:120]
    conv = Conversation(title=title)
    if body.get("folder"):
        conv.folder = _clean_folder(body.get("folder"))
        _ensure_folder(db, conv.folder)
        # **进分组就是归档** —— 与拖放同一条规矩（前端 `moveConvTo` 也是两个字段一起发）。
        #
        # 少了这一句会造出「在分组里却没归档」的会话：左栏那棵树是按 `archived`
        # 渲染的，所以它在界面上**根本看不见**，而 `delete_folder` 又照 `folder`
        # 把它算进分组里 —— 表现就是"分组里明明没东西，却删不掉"
        #（用户实测：`考研/数学` 里 7 条这样的会话）。
        conv.archived = True
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return {"conversation": _conversation_out(conv, 0, "")}


@router.get("/conversations/{cid}")
def get_conversation(cid: uuid.UUID, db: DbSession) -> dict:
    """一个会话的**全部消息**（树），当前分支由前端按 `parentId` 算。

    为什么把树整个给出去，而不是只在服务端切好一条线（LibreChat 也是这么做的）：
    「上一个/下一个分支」这个交互需要知道兄弟有哪些、各是哪几条 ——
    每次切换都回服务端问一次，会让翻分支变成一件有延迟的事。
    消息量是千级、一次读回内存，比来回问便宜。
    """
    conv = _own_conversation(db, cid)
    _heal_stale(db, conv)

    rows = db.scalars(
        select(Message)
        .where(Message.conversation_id == conv.id)
        .order_by(Message.id)
    ).all()
    active = _active_path(db, conv)
    return {
        "conversation": _conversation_out(
            conv, len(rows), (active[-1].content if active else "")
        ),
        "messages": [_message_out(m) for m in rows],
        # 服务端算好的当前分支（前端默认用它，之后按用户的切换自己算）
        "activePath": [m.id for m in active],
    }


@router.patch("/conversations/{cid}")
def rename_conversation(cid: uuid.UUID, payload: dict, db: DbSession) -> dict:
    """改标题 / 置顶。两个字段都**按需**更新（`pinned` 只认真正的布尔值，
    否则 `pinned: false` 会被 `or` 当成"没给"）。"""
    conv = _own_conversation(db, cid)
    body = payload or {}

    if "title" in body:
        title = str(body.get("title") or "").strip()[:120]
        if not title:
            raise HTTPException(400, "标题不能为空")
        conv.title = title

    if isinstance(body.get("pinned"), bool):
        conv.pinned = body["pinned"]

    if isinstance(body.get("archived"), bool):
        # 归档 / 取消归档。**不碰置顶**：用户要的是"有没有归档都可以置顶"，
        # 所以归档一条置顶过的对话，它在置顶区那份副本照旧在。
        conv.archived = body["archived"]
        # **取消归档 = 同时出分组**：分组住在归档区（左栏那棵树），一条会话不能
        # 既"在分组里"又"未归档" —— 那个组合在界面上是隐形的（树按 `archived`
        # 渲染），却仍被 `delete_folder` 算进分组，于是成了"分组里没东西却删不掉"。
        # 同一请求里显式给了 `folder` 的，以它为准（下面一段处理）。
        if not conv.archived and "folder" not in body:
            conv.folder = ""

    if "folder" in body:
        # 挪进某个分组：目标分组顺手登记一次（拖放时用户常常没先建过它）
        conv.folder = _clean_folder(body.get("folder"))
        _ensure_folder(db, conv.folder)
        # 反过来：**进分组就是归档** —— 与 `create_conversation` 和前端
        # `moveConvTo` 同一条规矩。移出分组（`folder: ""`）时不动归档：
        # "在归档区里挪到最外层"是个真实动作，不该顺手把它扔回未归档。
        if conv.folder:
            conv.archived = True

    # 这里**不碰** `updated_at`：改名与置顶不是"活动"，列表的"最近"排序
    # 只该被消息推动（模型上的注释解释了为什么那一列没有 `onupdate`）。
    db.commit()
    return {"ok": True, "pinned": bool(conv.pinned)}


@router.delete("/conversations/{cid}")
def delete_conversation(cid: uuid.UUID, db: DbSession) -> dict:
    """删会话并连带它的消息（外键 CASCADE）。

    这里**不是**「标记退役」—— 那道规矩是给题目与知识点的（历史统计不能有空洞）。
    对话是用户自己的东西，他说删就该真删。
    """
    conv = _own_conversation(db, cid)
    db.delete(conv)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ 消息（流式）


@router.post("/attachments")
async def upload_attachment(
    db: DbSession, file: UploadFile = File(...)
) -> dict:
    """上传一个附件。**先传、再在发消息时引用它**。

    为什么分两步而不是跟着消息一起 multipart 上去：发送那条路是 SSE 流，
    要在里面同时收文件、校验、落盘、再开流，会把"流"这件事和非流的事情搅在一起。
    分开之后这条路由是普通的请求-响应，失败也不用在半开的流里报错。

    抽出来的正文（PDF 走 `pdftotext`）跟着附件一起入库，供模型读；
    图片抽不出正文，但照常存下来给人看。
    """
    data = await file.read()
    result = attach.save(db, file.filename or "附件", file.content_type or "", data)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router.get("/attachments/{aid}")
def read_attachment(aid: uuid.UUID, db: DbSession) -> FileResponse:
    """取回附件本身（图片直接显示、文本可预览）。别人的附件按 404 处理。"""
    row = db.get(Attachment, aid)
    if row is None:
        raise HTTPException(404, "没有这个附件")
    path = attach.path_of(row)
    if not path.exists():
        raise HTTPException(410, "附件文件不在这台机器上（它只在本机存着）")
    return FileResponse(
        path,
        media_type=row.mime or "application/octet-stream",
        filename=row.name,
    )


EXPORT_FORMAT = "quizforge-chat/1"


def _messages_of(db: DbSession, conv: Conversation) -> list[Message]:  # noqa: ANN001
    """整个会话的消息（**全树**，按 id 升序 = 发生顺序）。

    `order_by` 不是装饰：不加它，数据库按心情给顺序（子查询计划一变就变），
    导出的文件里对话就是乱的 —— 这个洞是导出用例在全量跑时才露出来的
    （单跑时恰好是插入顺序）。
    """
    return list(
        db.scalars(
            select(Message)
            .where(Message.conversation_id == conv.id)
            .order_by(Message.id)
        ).all()
    )


def _markdown_of(conv: Conversation, messages: list[Message]) -> str:
    """给人读的那一份：只画**当前分支**，零件各记一笔但不抄正文。

    为什么只画当前分支：md 是拿去读与归档的，把被顶下去的分支也铺进去就成了一份
    到处是岔路的草稿。要看全树就用 json —— 那份连工具调用与每个分支都在。
    """
    lines = [f"# {conv.title or '未命名对话'}", ""]
    lines.append(
        f"> 导出时间 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        f" · 当前分支 {len(messages)} 条消息"
    )
    lines.append("")

    for message in messages:
        lines.append("## " + ("我" if message.role == "user" else "AI"))
        lines.append("")
        if (message.content or "").strip():
            lines.append(message.content.strip())
            lines.append("")

        notes = []
        for part in message.parts or []:
            kind = part.get("type")
            if kind == "tool_call":
                notes.append(
                    "工具 `" + str(part.get("name")) + "`"
                    + ("" if part.get("ok", True) else "（失败）")
                )
            elif kind == "card":
                notes.append("题卡 `" + str((part.get("payload") or {}).get("questionId")) + "`")
            elif kind == "citation":
                notes.append(
                    f"引用 `{part.get('slug')}:{part.get('startLine')}–{part.get('endLine')}`"
                )
            elif kind == "file":
                notes.append("附件 `" + str(part.get("name")) + "`")
            elif kind == "demo":
                notes.append("演示「" + str(part.get("title")) + "」（HTML 见 json 导出）")
            elif kind == "action":
                notes.append("动作凭条 " + str(part.get("kind")))
        if notes:
            lines.extend("- " + note for note in notes)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


@router.get("/conversations/{cid}/export")
def export_conversation(
    cid: uuid.UUID,
    db: DbSession,
    format: str = Query(
        "md", description="md（给人读，当前分支）/ json（给机器读，全树）/ html（单页分享）"
    ),
) -> PlainTextResponse:
    """导出一次对话。

    三种格式的分工是刻意的：`md` 只画**当前分支**（拿去读、拿去归档）；
    `json` 带走**整棵树**（含被顶下去的分支、工具调用、令牌数）——
    所以它是"轨迹"的完整备份，而不只是"看过的那些字"；
    `html` 是**发给别人**的那一种：一个自带数据的网页，对话正文那棵对话树都在，
    对方双击就能看，不需要装任何东西（见 `app/share.py`）。
    """
    conv = _own_conversation(db, cid)
    all_messages = _messages_of(db, conv)

    if format == "html":
        # 装配很重（要 base64 掉 300KB 字体），但这条路由是同步 `def`，
        # FastAPI 会把它丢进线程池，不挡事件循环。
        #
        # **自检不过就当场报错**，不给一个"能下载、打开是白的"文件 ——
        # 那种文件大小正常、浏览器也不报错，人只会以为是自己那边的问题。
        try:
            page, _stats = share.build_single_page(
                get_settings().web_dir,
                share.payload_of(conv, [_message_out(m) for m in all_messages]),
            )
        except (FileNotFoundError, share.ShareBuildError) as err:
            raise HTTPException(status_code=500, detail="分享页没装配出来：%s" % err) from err
        return PlainTextResponse(
            page,
            media_type="text/html; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="chat-{str(conv.id)[:8]}.html"'
            },
        )

    if format == "json":
        payload = {
            "format": EXPORT_FORMAT,
            "exportedAt": datetime.now(timezone.utc).isoformat(),
            "conversation": {
                "id": str(conv.id),
                "title": conv.title,
                "createdAt": conv.created_at.isoformat() if conv.created_at else "",
                "updatedAt": conv.updated_at.isoformat() if conv.updated_at else "",
            },
            "messages": [_message_out(m) for m in all_messages],
        }
        return PlainTextResponse(
            json.dumps(payload, ensure_ascii=False, indent=2),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="chat-{str(conv.id)[:8]}.json"'},
        )

    branch = _active_path(db, conv)
    return PlainTextResponse(
        _markdown_of(conv, branch),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="chat-{str(conv.id)[:8]}.md"'},
    )


@router.get("/export")
def export_all(
    db: DbSession,
    format: str = Query("json", description="只有 json：全部分支 + 全部零件"),
) -> PlainTextResponse:
    """把所有对话导出成一个文件 —— 这是"我的轨迹"的完整备份。"""
    conversations = db.scalars(
        select(Conversation).order_by(Conversation.created_at)
    ).all()
    payload = {
        "format": EXPORT_FORMAT,
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "conversations": [
            {
                "id": str(conv.id),
                "title": conv.title,
                "updatedAt": conv.updated_at.isoformat() if conv.updated_at else "",
                "messages": [_message_out(m) for m in _messages_of(db, conv)],
            }
            for conv in conversations
        ],
    }
    return PlainTextResponse(
        json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="quizforge-chats.json"'},
    )


@router.post("/conversations/{cid}/messages")
def post_message(
    cid: uuid.UUID, payload: dict, db: DbSession
) -> StreamingResponse:
    """追加一条用户消息，并以 SSE 流式回一条助手消息。

    `replyTo` 给出一条**用户消息**的 id 时，表示「对这条重新生成」——
    不会新增用户消息，只在该消息下再挂一个助手分支（树就长在这里）。

    配置与配额在**开流之前**校验：一旦开始发 SSE，状态码就发出去了，
    那时再报「没填密钥」用户只会看到一个空白的错误。
    """
    conv = _own_conversation(db, cid)
    body = payload or {}
    content = str(body.get("content") or "").strip()
    reply_to = body.get("replyTo")
    # 「深度思考」（输入框里那颗药丸）：**缺省 = 开** —— 默认模型是
    # `deepseek-flash`，思考是它的常态；前端关掉才传 false，那时由
    # 思考强度：前端传等级（"off"/"low"/"normal"/"high"），老客户端传布尔也认
    # （`gateway.thinking_level` 把两者收敛成同一档）。往下**原样**交给 `thinking_params`。
    _deep = body.get("thinking")
    thinking = True if _deep is None else _deep

    conf = gateway.resolve_config(db)
    gateway.enforce_quota(db)

    cont = False

    if reply_to not in (None, "", 0):
        try:
            parent_id = int(reply_to)
        except (TypeError, ValueError):
            raise HTTPException(400, "replyTo 不是合法的消息 id") from None
        parent = db.get(Message, parent_id)
        if parent is None or parent.conversation_id != conv.id:
            raise HTTPException(404, "没有这条消息")
        if parent.role != "user":
            raise HTTPException(400, "只能针对用户消息重新生成")
        user_msg = parent
    elif body.get("continue"):
        # 「接一轮」：**不新增用户消息**，只让 agent 看着已有的历史说话。
        #
        # 用途是"大题批完自动开口"：批改结果会由 `problem._append_grade_note`
        # 写一条 assistant 记事进对话，接着就该主 agent 就着它讲两句。
        # 不能靠"存一条用户消息"来触发 —— 库里那条 `content` 只放**用户原话**，
        # 机器生成的"继续"混进去，他会以为是自己说的。
        last = db.scalars(
            select(Message)
            .where(Message.conversation_id == conv.id)
            .order_by(Message.id.desc())
            .limit(1)
        ).first()
        if last is None:
            raise HTTPException(400, "这条对话还没有消息，没什么可接着说的")
        user_msg = last  # 只是"挂载点"：历史从它往回取，回复挂在它下面
        cont = True
    else:
        if not content:
            raise HTTPException(400, "消息内容不能为空")
        # 新消息挂在谁下面：默认挂最新那条；但前端可以指定 ——
        # 用户翻到旧分支上接着问时，就该挂在那条分支上，而不是挂到最新那条去
        parent_id = _leaf_id(db, conv)
        if "parentId" in body and body["parentId"] is None:
            # **显式**说"没有父节点"。编辑第一条消息时会用到：那一条本来就在根上，
            # 「挂到最新那条下面」是完全不同的意思 —— 不把它与"没给"分开，
            # 编辑第一条消息就会把它挪到会话末尾去。
            parent_id = None
        elif body.get("parentId") not in (None, "", 0):
            try:
                wanted = int(body["parentId"])
            except (TypeError, ValueError):
                raise HTTPException(400, "parentId 不是合法的消息 id") from None
            parent = db.get(Message, wanted)
            if parent is None or parent.conversation_id != conv.id:
                raise HTTPException(404, "没有这条消息")
            parent_id = parent.id

        user_msg = Message(
            conversation_id=conv.id,
            parent_id=parent_id,
            role="user",
            content=content[:MAX_CONTENT],
            status="ok",
            # 快照"这一刻的模式"：开关是随时会改的，事后再也推不出当时挂了几组。
            # 对话树就是靠它画出"对话过程里模式变过几次、各是什么"。
            mounts=json.dumps(sorted(mounts.effective(db)), ensure_ascii=False),
        )
        # 附件：先上传、后引用（见 upload_attachment 的说明）。
        # 只接受**本人**的、且还没挂到别的消息上的那些 —— 别人传的 id 猜不出来，
        # 但猜出来了也不能用。
        attach_ids = body.get("attachments") or []
        attached = []
        if attach_ids:
            for raw in attach_ids[:8]:
                try:
                    row = db.get(Attachment, uuid.UUID(str(raw)))
                except (TypeError, ValueError):
                    continue
                if row is not None and row.message_id is None:
                    attached.append(row)
            if attached:
                user_msg.parts = [
                    msgparts.file_part(
                        attachment_id=row.id,
                        name=row.name,
                        mime=row.mime,
                        size=row.size,
                        kind=row.kind,
                        text_chars=len(row.text),
                    )
                    for row in attached
                ]

        db.add(user_msg)
        if not conv.title.strip():
            conv.title = _title_from(content)
        conv.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(user_msg)

        for row in attached:
            row.message_id = user_msg.id
        if attached:
            db.commit()

    # 上下文 = 到这条提问为止的那一段（不含任何别的分支上的回答）
    # 模型能读图时，图片才作为图像发出去（判断见 ai_gateway.model_reads_images）
    history = _history(
        db,
        _chain(db, user_msg),
        vision=bool(conf.get("vision")),
        model=str(conf.get("model") or ""),
    )
    if cont:
        # 合成的这一句**只进请求、不进库**：它替模型点明"现在该就着什么说话"。
        # 不点的话，历史末尾是一条 assistant 记事，模型常常当自己已经答完了。
        history.append(
            {
                "role": "user",
                "content": "（以上是大题的批改结果。）就着它说几句：哪里对、哪里缺、"
                "下一步练什么。不要重复题面，也不要复述批语全文。",
            }
        )

    assistant = Message(
        conversation_id=conv.id,
        parent_id=user_msg.id,
        role="assistant",
        content="",
        status="streaming",
        model=conf["model"],
    )
    db.add(assistant)
    db.commit()
    db.refresh(assistant)

    return StreamingResponse(
        _stream(db, conv, conf, user_msg, assistant, history, thinking=thinking),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # nginx 默认会攒够一个缓冲区才往下发 —— 那会把流式体验直接抵消掉
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/runs/{run_id}")
def deliver_run(run_id: str, body: dict, db: DbSession) -> dict:
    """**按 runId** 交回沙箱输出：页面跑完就报，与"那条消息落库了没有"无关。

    为什么非要这条（原来只有消息那条）：跑脚本大多发生在**流式过程中**，那时消息
    还没落库，前端拿不到 message id —— 于是输出被丢掉，块永远停在"运行中…"，
    而库里那次运行其实**有**输出（实测查库确认过）。这条按 runId 认领，顺手把
    agent 循环里那个"等输出"的会合点放行（见 `app/runs.py`）。

    `body`：`{ok, text, ms}`（与 postMessage 回传的形状一致）。
    """
    body = body or {}
    payload = {
        "ok": body.get("ok") is not False,
        "text": str(body.get("text") or ""),
        "ms": int(body.get("ms") or 0),
        # 图（matplotlib 那种）：壳收走后以 base64 跟着回来 —— 存进那行运行里，
        # 刷新后还在。限量与大小在 `runs.deliver` 里统一收（见 `_clean_images`）。
        "images": body.get("images") or [],
    }
    delivered = runs.deliver(str(run_id or "").strip(), payload)
    return {"ok": True, "delivered": delivered}


@router.post("/conversations/{cid}/messages/{mid}/run")
def attach_run(
    cid: uuid.UUID, mid: int, payload: dict, db: DbSession
) -> dict:
    """把沙箱那次运行的结果**回填到消息上**。

    ## 为什么要回填，而不是让用户点一下「把输出发给它」

    输出本来就在宿主手里（沙箱跑完 `postMessage` 回来）。让用户按一下再复制过来，
    等于把他当传话筒 —— 而这套系统里没人该做这件事。回填之后：

    * 那条消息**直接显示输出**（不必点开面板）
    * **下一轮对话时它进模型的历史**（见 `_history`）—— 模型自己就能读到自己
      那段代码跑出了什么，不必等谁转述

    按 `runId` 认领：一次运行对应一个零件；认不出来就当没这回事（不静默写坏零件）。
    """
    conv = _own_conversation(db, cid)
    message = db.get(Message, mid)
    if message is None or message.conversation_id != conv.id:
        raise HTTPException(404, "没有这条消息")

    body = payload or {}
    run_id = str(body.get("runId") or "").strip()
    if not run_id:
        raise HTTPException(400, "缺少 runId")
    text = str(body.get("text") or "")[:RUN_TEXT_LIMIT]
    # 顺手把会合点放行：跑脚本的那一轮可能正停在这儿等（见 app/runs.py）。
    # 两条上报路（这条与 `/chat/runs/{runId}`）都汇到那儿，谁先到都算数。
    runs.deliver(
        run_id,
        {
            "ok": body.get("ok") is not False,
            "text": text,
            "ms": int(body.get("ms") or 0),
            # 图也一起放行（同 `/chat/runs/{runId}` 那条路）
            "images": body.get("images") or [],
        },
    )

    # 造**新的** dict 而不是原地改：JSON 列的脏检查比较的是值，
    # 原地改完再赋值会被判成"没变"，于是什么都不写（实测就是这样静默丢的）。
    # 再加上 flag_modified 兜一道。
    parts = []
    hit = False
    for part in message.parts or []:
        if (
            isinstance(part, dict)
            and part.get("type") == "demo"
            and str(part.get("runId") or "") == run_id
        ):
            part = dict(part, run={"ok": body.get("ok") is not False, "text": text})
            hit = True
        parts.append(part)
    if not hit:
        raise HTTPException(404, "这条消息里没有这次运行")

    message.parts = parts
    flag_modified(message, "parts")
    db.commit()
    return {"ok": True, "chars": len(text)}


@router.post("/conversations/{cid}/messages/{mid}/stop")
def stop_message(cid: uuid.UUID, mid: int, db: DbSession) -> dict:
    """前端按了停止 —— 把这条正在生成的消息收尾成「已中断」。

    为什么要专门开一个端点：服务端**自己感知不到**客户端断开（见模块 docstring），
    没有这一步的话，那条消息会停在 `streaming`，直到下次读会话被 `_heal_stale`
    补上（五分钟之后）—— 中间这段时间库里的事实与用户刚做的事不一致。

    `_finish` 会尊重这次收尾，不再覆盖（否则"停止"过一会儿会自己变回 `ok`，
    用户下次打开看到一条他没读完却被标成完整的回答）。
    """
    conv = _own_conversation(db, cid)
    m = db.get(Message, mid)
    if m is None or m.conversation_id != conv.id:
        raise HTTPException(404, "没有这条消息")

    if m.status == "streaming":
        m.status = "partial" if m.content else "error"
        if not m.content:
            m.error = "已被中断"
        conv.updated_at = datetime.now(timezone.utc)
        db.commit()
    return {"ok": True, "status": m.status}


def _subtree_ids(db, cid: uuid.UUID, mid: int) -> list[int]:
    """以 `mid` 为根的整棵子树的 id（**含根**），按层序。

    用显式队列而不是递归：这棵树是用户随手点出来的，没有深度上限；
    顺带带一道 `seen` 与上限，碰到病态数据（万一绕着环）也不至于把请求拖死。

    删（`delete_message`）与分家（`split_message`）都要这一份名单 —— 写两遍遍历迟早
    有一处忘了防环，所以数子树的 `_subtree_size` 也走这里（`len(ids)`）。
    """
    ids = [mid]
    frontier = [mid]
    seen = {mid}
    while frontier and len(ids) < 10000:
        rows = db.scalars(
            select(Message.id).where(
                Message.conversation_id == cid, Message.parent_id.in_(frontier)
            )
        ).all()
        frontier = [one for one in rows if one not in seen]
        seen.update(frontier)
        ids.extend(frontier)
    return ids


def _remap_parts(node, idmap: dict):  # noqa: ANN001
    """把零件里所有 `attachmentId` 换成新 id（零件是嵌套的，递归走一遍）。

    传空 `idmap` 就是一次**深拷贝** —— 复制出来的消息不能与原消息共用同一份
    parts 对象（JSON 列是原地可变的，共用等于两条消息拴在同一个列表上）。
    """
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key == "attachmentId":
                out[key] = idmap.get(str(value), value)
            else:
                out[key] = _remap_parts(value, idmap)
        return out
    if isinstance(node, list):
        return [_remap_parts(one, idmap) for one in node]
    return node


def _copy_attachments(db, src_id: int, dst: Message) -> None:  # noqa: ANN001
    """把一条消息的附件也复制一份（新 id、同一个文件），并改写零件里的 `attachmentId`。

    为什么要连附件一起复制：零件里存的是 `attachmentId`（见 `app/parts.py` 的
    `file_part`），前端拿它去 `GET /chat/attachments/{id}` 取图。只复制消息的话，
    副本的图仍然指**原树那条**消息名下的附件行 —— 原树哪天被删，附件行跟着 CASCADE
    走，副本里的图就全断了。
    文件本身不必复制：这套服务从来不删附件文件（库里删行、盘上留着），
    两份行指同一个 `path` 是安全的。
    """
    rows = db.scalars(select(Attachment).where(Attachment.message_id == src_id)).all()
    if not rows:
        return
    idmap = {}
    for src in rows:
        copy = Attachment(
            message_id=dst.id,
            name=src.name,
            mime=src.mime,
            size=src.size,
            sha256=src.sha256,
            path=src.path,
            kind=src.kind,
            text=src.text,
            created_at=src.created_at,
        )
        db.add(copy)
        db.flush()          # 先拿到新 id，下面要写进零件
        idmap[str(src.id)] = str(copy.id)
    # JSON 列**整体重新赋值**才会被认作已改（原地改 key 是留不住的）
    dst.parts = _remap_parts(dst.parts, idmap)


def _copy_subtree(db, ids: list[int], child: Conversation) -> None:  # noqa: ANN001
    """把 `ids` 这一支**复制**进 `child`：新 id，父子关系、零件、附件、时间都跟着走。

    `ids` 来自 `_subtree_ids`，是**层序**（父一定排在子前面）—— 照它走一遍就能把
    `parent_id` 重映射对，不需要递归，也不会出现"子先于父"。
    """
    by_id = {one.id: one for one in db.scalars(select(Message).where(Message.id.in_(ids))).all()}
    remap = {}
    for old_id in ids:
        src = by_id.get(old_id)
        if src is None:
            continue
        copy = Message(
            conversation_id=child.id,
            # 子树根的 parent 不在名单里 → None（它在新会话里就是根）
            parent_id=remap.get(src.parent_id),
            role=src.role,
            content=src.content,
            status=src.status,
            error=src.error,
            finish_reason=src.finish_reason,
            model=src.model,
            prompt_tokens=src.prompt_tokens,
            completion_tokens=src.completion_tokens,
            latency_ms=src.latency_ms,
            # 空 idmap = 深拷贝（见 `_remap_parts`）
            parts=_remap_parts(src.parts, {}),
            mounts=src.mounts,
            created_at=src.created_at,   # 时间跟着走：这一支就是那时候长出来的
        )
        db.add(copy)
        db.flush()                       # 先拿到新 id，子要用
        remap[src.id] = copy.id
        _copy_attachments(db, src.id, copy)

def _subtree_size(db, cid: uuid.UUID, mid: int) -> int:
    """这条消息连同它下面一共几条（**删之前**先数一下）。

    为什么要数：用户是在对话树上点了一个节点，而他**看不见那下面挂了多少** ——
    一声不吭删掉十几条，那不是"操作成功"，那是悄悄删了他的东西。数出来之后，
    界面能把它写进确认框、也写进返回值里，这一步才是有交代的。
    """
    return len(_subtree_ids(db, cid, mid))


@router.delete("/conversations/{cid}/messages/{mid}")
def delete_message(cid: uuid.UUID, mid: int, db: DbSession) -> dict:
    """删掉一条消息，**连同它下面的整棵子树**（对话树界面上"删掉这个分支"那一步）。

    为什么连子树一起删：这是棵树，留下孩子就等于留下**指不到根的孤枝**
    （`parent_id` 指向一条已经不在的消息），读会话、算分支、画树三处会各自崩一次。
    所以语义只有一种：**删一个节点 = 删掉以它为根的那一棵子树** ——
    删一条"提问"，就是把从那次提问长出去的东西一起删掉。

    删除动作交给 `messages.parent_id` 上的 `ondelete="CASCADE"`（引擎启动时
    `PRAGMA foreign_keys=ON`，见 `db.py`）—— **不自己递归删**：递归要写"防环 + 上限"
    的遍历，而数据库那条是原子的、也不可能绕成环。

    回给界面 `deleted`（这一支一共几条），好让它说的是确数而不是"删好了"。
    """
    conv = _own_conversation(db, cid)
    m = db.get(Message, mid)
    if m is None or m.conversation_id != conv.id:
        raise HTTPException(404, "没有这条消息")

    deleted = _subtree_size(db, conv.id, mid)
    db.delete(m)
    conv.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True, "deleted": deleted}


@router.post("/conversations/{cid}/messages/{mid}/split")
def split_message(cid: uuid.UUID, mid: int, payload: dict, db: DbSession) -> dict:
    """把以 `mid` 为根的**一整棵子树**搬到（或复制到）一条新会话里。

    两个去处，用户各要一半（"另成一棵树，有两种选项，一种是不保留，一种是保留原树的
    子树"）：

      * `keep=False`（默认）= **剪下去**：子树改籍到新会话，原树从此没有这一支；
      * `keep=True` = **留一份**：子树完整复制到新会话，原树**一点不动**。

    「剪」是改籍，消息 id 不动（附件、题目卡、引用都还挂在原处）；「留」只能真复制，
    副本是新的消息 id（一条消息不可能同时属于两棵树）—— 连附件行一起复制、零件里的
    `attachmentId` 一并改写，副本才能自己活下去，细节见 `_copy_attachments`。

    新会话的归属：**沿用原来的分组**（分出来的这一支属于同一个话题），但**不跟着归档**
    —— 它是一条新的、正要接着用的对话，落进归档堆里会找不着。
    """
    body = payload or {}
    keep = bool(body.get("keep"))
    conv = _own_conversation(db, cid)
    m = db.get(Message, mid)
    if m is None or m.conversation_id != conv.id:
        raise HTTPException(404, "没有这条消息")

    ids = _subtree_ids(db, conv.id, mid)
    text = " ".join(str(m.content or "").split())
    child = Conversation(
        title=(text[:30] + "…") if len(text) > 30 else (text or "分出来的那一支"),
        folder=conv.folder,
    )
    db.add(child)
    db.flush()  # 先拿到 child.id（下面每一条都要写）

    if keep:
        _copy_subtree(db, ids, child)
    else:
        # 一条一条改，不用 bulk update：这一层是 ORM 对象，房子里的规矩是让它自己同步
        for row in db.scalars(select(Message).where(Message.id.in_(ids))).all():
            row.conversation_id = child.id
        m.parent_id = None  # 它在新会话里是根：断掉与原树那一条边

    now = datetime.now(timezone.utc)
    conv.updated_at = now
    child.updated_at = now
    db.commit()
    return {
        "ok": True,
        "kept": keep,
        "moved": len(ids),
        "conversation": {"id": str(child.id), "title": child.title},
    }


# ------------------------------------------------------------------ 流式一轮
#
# 下面五个是「发一条消息、边收边落库」的核心，从 HEAD 原样取回：
# 早前给 `split_message` 打补丁时按"从它的 def 替换到文件尾"取区间，把它们一起
# 删掉了（它们没有 `@router.` 装饰，于是"往下找下一个路由"这个定位法找不着锚点）。
# 症状是发送那条路一进来就 `NameError: _stream`。


def _push_text(parts: list[dict], chunk: str) -> None:
    """把一段增量并进零件数组。

    同类相邻就合并 —— 否则一次回答会碎成几百个 `text` 零件，
    存库、传输、渲染都白受一遍。
    """
    if parts and parts[-1].get("type") == "text":
        parts[-1]["text"] = str(parts[-1].get("text") or "") + chunk
    else:
        parts.append(msgparts.text_part(chunk))


def _push_think(parts: list[dict], chunk: str) -> None:
    if parts and parts[-1].get("type") == "think":
        parts[-1]["text"] = str(parts[-1].get("text") or "") + chunk
    else:
        parts.append(msgparts.think_part(chunk))


def _spill(db: DbSession, assistant: Message, parts: list[dict]) -> None:  # noqa: ANN001
    """把已经收到的零件先写进库（见模块 docstring 的「边收边写」）。

    只改内容，不动状态：状态由 `_finish` 或读会话时的 `_heal_stale` 负责。
    这样即使进程被杀，库里也留着「一部分内容 + streaming」——
    读的时候它会自己变成 `partial`，而不是白屏。
    """
    assistant.parts = list(parts)
    assistant.content = msgparts.text_of(parts)
    db.commit()


def _finish(  # noqa: ANN001
    db: DbSession,
    conv: Conversation,
    assistant: Message,
    parts: list[dict],
    status: str,
    error: str,
    finish: str,
    latency_ms: int,
    usage: dict,
) -> None:
    """落库 + 记账。正常、出错、被中断三条路都走它。"""
    db.refresh(assistant)
    closed = assistant.status in ("partial", "error")
    if not closed:
        # 没被别人收尾（比如前端点了停止调了 stop 端点）才写内容与状态；
        # 但用量照样记 —— 那部分 token 是真的花掉了
        assistant.parts = list(parts)
        assistant.content = msgparts.text_of(parts)
        assistant.status = status
        assistant.error = error[:2000]
    assistant.finish_reason = finish
    assistant.latency_ms = latency_ms
    assistant.prompt_tokens = int(usage.get("promptTokens") or 0)
    assistant.completion_tokens = int(usage.get("completionTokens") or 0)
    conv.updated_at = datetime.now(timezone.utc)
    db.commit()

    gateway.record_usage(
        db,
        ok=(status == "ok"),
        latency_ms=latency_ms,
        prompt_tokens=assistant.prompt_tokens,
        completion_tokens=assistant.completion_tokens,
        detail=error,
    )


def _stream(  # noqa: ANN001
    db: DbSession,
    conv: Conversation,
    conf: dict,
    user_msg: Message,
    assistant: Message,
    history: list[dict],
    thinking: object = True,
):
    started = time.perf_counter()
    parts: list[dict] = []
    tool_parts: dict[str, dict] = {}  # callId → 零件（结果回来时原地补上）
    citations: list[dict] = []  # 这条消息已经挂上去的引用
    cited: set[tuple] = set()  # (材料, 起, 止)，用来去重
    usage = {"promptTokens": 0, "completionTokens": 0}
    finish = ""
    status = "ok"
    error = ""
    error_sent = False
    last_spill = started

    yield _sse("user", _message_out(user_msg))
    yield _sse("start", _message_out(assistant))

    try:
        # 这一轮的模式（挂载集 + 权限档）先算一次：拼提示词、声明工具、执行守卫
        # 用的是同一份 —— 三处不一致是这种系统最容易出的 bug。
        # **模式由输入锚定**（用户定的规则）：
        #
        #   一条输入可以有多个输出，输入锚定模式，因此所有的输出都是一个模式，
        #   要想改模式，必须更改输入。
        #
        # 所以这一轮用哪几组、放到哪一档，以那条用户消息**当时记下的快照**为准，
        # 而不是"现在设置里是什么"—— 设置里那个只是"下一条输入用什么"。
        # 老消息没记过快照才回落到设置（`from_snapshot` 返回 None）。
        anchored = mounts.from_snapshot(getattr(user_msg, "mounts", None))
        if anchored is None:
            turn_mounts, turn_access = mounts.effective(db), mounts.access_of(db)
        else:
            turn_mounts, turn_access = anchored
        # 上一条用户消息当时是什么模式（用户消息上存着快照，对话树也靠它）——
        # 模式在一个对话内会变，变了就要在提示词里明说，见 mode_notice。
        # 这一轮**真正声明**出去的工具名（组 × 权限档的结果）——
        # 提示词里点名的就是它，与发给模型的 tools 参数同源。
        turn_declared = [
            item["function"]["name"]
            for item in tool_registry.specs(turn_mounts, turn_access)
        ]
        # 上一条用户消息当时是什么模式（用户消息上存着快照）—— 模式在一个对话内会变，
        # 变了要在提示词里明说（见 mode_notice）。查不到就当"没变"：这只是提示词的
        # 一条附加说明，绝不该因为它让整轮对话失败。
        prev_mounts = None
        try:
            _prev = (
                db.query(Message)
                .filter(
                    Message.conversation_id == conv.id,
                    Message.role == "user",
                    Message.id != user_msg.id,
                )
                # 按自增主键排，不按 created_at —— **Message 上没有 created_at 这一列**
                # （实测过：写 created_at 会每轮抛异常，而这里外面套了兜底，于是被静默
                # 吃成"没变"，症状是"模式变多会提示、变少从不提示"，一次灵一次不灵）。
                .order_by(Message.id.desc())
                .first()
            )
            if _prev is not None:
                prev_mounts = [str(one) for one in (as_json(_prev.mounts, []) or [])]
        except Exception:  # noqa: BLE001  提示词附加项，失败就按"没变"处理
            prev_mounts = None

        # 「他的学习轨迹」：**每轮都算一遍**（它随对话长，不随设置变）。
        # 兜底包着：仪表盘是背景信息，绝不该因为它让整轮对话失败。
        try:
            from .. import recursion

            trail = recursion.dashboard(db, conv, user_msg)
        except Exception:  # noqa: BLE001
            trail = ""

        # 他召唤的流程（skill）：沿这条链从根往下取最近的那条记录（见 `app/skills.py`）。
        # 同样兜底 —— 一段加成性质的行为约束，不该让整轮生成失败。
        try:
            from .. import skills as skill_lib

            flow = skill_lib.block(skill_lib.resolve(db, conv, user_msg))
        except Exception:  # noqa: BLE001
            flow = ""

        for event in agent_loop.run(
            db,
            conf,
            # 提示词随**这一版挂载的组**变（极简模式是一份完全不同的提示词：
            # 它不该知道用户有笔记/资料/题库，见 build_prompt 的说明）
            # 仪表盘与流程都不受挂载影响 —— 它们不是工具，见 build_prompt 的说明。
            # 思考强度接在最末尾：它管的是"这一轮想多深"，与模式、口吻都无关。
            system=build_prompt(turn_mounts, prev_mounts, turn_declared, trail, flow)
            + thinking_line(thinking),
            history=history,
            tools=tools,
            # 只声明**已挂载**的那几组；未挂载的即使被叫到名字也不执行（纵深防御）
            mounts=turn_mounts,
            allow=turn_access,
            # 工具要知道"这是哪次对话"：push_question 靠它避开这次已经推过的题
            tool_context={"conversationId": str(conv.id)},
            # 「深度思考」开关（前端药丸 → 请求体 → 这里，见 `thinking_params`）。
            # 这是唯一开思考的调用路径：子代理与批改不传（默认关）。
            thinking=thinking,
        ):
            kind = event["kind"]

            if kind in ("text", "docx", "pptx"):
                _push_text(parts, event["text"])
                yield _sse("delta", {"text": event["text"]})
            elif kind == "think":
                _push_think(parts, event["text"])
                yield _sse("think", {"text": event["text"]})
            elif kind == "run_output":
                # 跑脚本的**最终输出**回来了（agent 循环在那儿等过它，见 agent_loop）。
                # 写进那块零件：库里的消息因此自带结果 —— 刷新、换设备都还在，
                # 模型下一轮读历史时看到的也是"跑完了、输出是这些"。
                run = event.get("run")
                if isinstance(run, dict) and run:
                    for one in parts:
                        if one.get("type") == "demo" and one.get("runId") == event["runId"]:
                            one["run"] = run
                            break
                yield _sse("run", {"runId": event["runId"], "run": run})
            elif kind == "note":
                yield _sse("note", {"text": event["text"]})
            elif kind == "tool_start":
                part = msgparts.tool_part(
                    name=event["name"], args=event["args"], call_id=event["callId"]
                )
                parts.append(part)
                tool_parts[event["callId"]] = part
                yield _sse(
                    "tool",
                    {
                        "phase": "start",
                        "callId": event["callId"],
                        "name": event["name"],
                        "args": event["args"],
                    },
                )
            elif kind == "tool_result":
                # **这里原来会给"工具前那段话"打 `process` 标记**（界面上折成"过程"块），
                # 出发点是"紧跟着工具调用的话是铺垫、不是结论"。已删：模型先说一段正文
                # （"我一次验到底：两条路各出一张…"）再调工具时，那 1500 字会被整段折走，
                # 正文区只剩工具气泡 —— 用户的原话："正文被吞到思考里面了"。
                #
                # 通道本来就分得清楚：思维链走 `reasoning_content`（think 零件），
                # 对用户说的话走 `content`（text 零件）。正文永远留正文。
                part = tool_parts.get(event["callId"])
                if part is not None:
                    part["output"] = event["output"]
                    part["ok"] = event["ok"]
                    part["ms"] = event["ms"]
                yield _sse(
                    "tool",
                    {
                        "phase": "result",
                        "callId": event["callId"],
                        "name": event["name"],
                        "ok": event["ok"],
                        "ms": event["ms"],
                        "output": event["output"],
                    },
                )

                # 有的工具结果不只给模型看，还要变成零件：题卡就是 ——
                # 模型看到题面好围绕它说话，界面把它渲染成**可作答的卡片**。
                # 一条消息因此能"长出"一道题，而不是只有一段文字。
                card = (event.get("payload") or {}).get("card")
                if isinstance(card, dict) and card.get("questionId"):
                    parts.append(msgparts.card_part(kind="question", payload=card))
                    yield _sse("card", {"callId": event["callId"], "card": card})

                # 临时题卡（`create_question`）：模型现编的一道题，还不在任何题单里。
                # 答案随卡片交给界面（它要判分）—— 模型那一侧只留一行答案。
                draft = (event.get("payload") or {}).get("draft")
                if isinstance(draft, dict) and draft.get("id"):
                    parts.append(msgparts.draft_part(payload=draft))
                    yield _sse("draft", {"callId": event["callId"], "draft": draft})

                # 写操作也一样：工具只**提案**，界面上长出一张待确认的凭条，
                # 他点了才落到记录里（走的是收藏夹 / 错题本那两条老路）。
                # 演示沙箱：模型给的是一段自包含 HTML，界面上长成一个沙箱 iframe
                demo = (event.get("payload") or {}).get("demo")
                # `runId` 也算：跑 Python 现在走**常驻运行壳**，零件本身没有 html
                #（见 tools.py 的 `_PYODIDE_SHELL`），但照样要长出一个零件。
                if isinstance(demo, dict) and (demo.get("html") or demo.get("runId")):
                    parts.append(
                        msgparts.demo_part(
                            title=demo.get("title") or "演示",
                            html=demo.get("html") or "",
                            run_id=demo.get("runId") or "",
                            # 跑脚本 vs 画演示：前端据此选长相（命令块 / 卡片）
                            kind=(
                                "python"
                                if demo.get("kind") == "python" or event.get("name") == "run_python"
                                else "demo"
                            ),
                            # 脚本原文（`run_python` 的入参）：给界面折起来看
                            code=(
                                demo.get("code")
                                or (event.get("args") or {}).get("code")
                                or ""
                            ),
                        )
                    )
                    # 跑 Python 的那种：把 `kind` 与**脚本原文**一起发过去 ——
                    # 流式那一路是照着这个 payload 直接长出零件的（不走库里那份），
                    # 少了 code，前端就没法把它交给常驻运行壳。
                    payload = dict(demo)
                    if payload.get("runId") and not payload.get("html"):
                        payload["kind"] = "python"
                        payload["code"] = str((event.get("args") or {}).get("code") or "")
                        # 点名要装的包（动态导入那种）也跟着走 —— 壳要用它
                        payload["packages"] = list(demo.get("packages") or [])
                    yield _sse("demo", {"callId": event["callId"], "demo": payload})

                proposal = (event.get("payload") or {}).get("proposal")
                if isinstance(proposal, dict) and proposal.get("kind"):
                    parts.append(
                        msgparts.action_part(kind=proposal["kind"], payload=proposal)
                    )
                    yield _sse("action", {"callId": event["callId"], "proposal": proposal})

                # 出处 → 引用零件：任何工具只要在结果里带 `sources`，这一处就统一
                # 把它变成可点回原文的引用。所以"有没有引用"不取决于工具，
                # 而取决于它有没有给出处（这是流水线早就立下的规矩）。
                for source in event.get("payload", {}).get("sources") or []:
                    if len(citations) >= CITATION_MAX:
                        break
                    key = (
                        str(source.get("material") or ""),
                        int(source.get("startLine") or 0),
                        int(source.get("endLine") or 0),
                    )
                    if not key[0] or key in cited:
                        continue
                    cited.add(key)
                    citation = msgparts.citation_part(
                        slug=key[0],
                        start_line=key[1],
                        end_line=key[2],
                        quote=str(source.get("quote") or ""),
                        point_key=str(source.get("pointKey") or ""),
                    )
                    citations.append(citation)
                    parts.append(citation)
                    yield _sse("citation", {"callId": event["callId"], "citation": citation})
            elif kind == "error":
                status = "error"
                error = str(event.get("detail") or event.get("message") or "生成失败")
                error_sent = True
                yield _sse(
                    "error",
                    {
                        "message": event.get("message") or "生成失败",
                        "detail": event.get("detail") or "",
                        "retryable": bool(event.get("retryable")),
                    },
                )
            elif kind == "finish":
                finish = str(event.get("reason") or "")
                usage = event.get("usage") or usage

            now = time.perf_counter()
            if now - last_spill >= SPILL_SECONDS:
                last_spill = now
                _spill(db, assistant, parts)
    except GeneratorExit:
        # 客户端走了（点停止 / 关页面）。产出不能丢，但这里**不能再 yield** ——
        # 生成器正在关闭，往一条已死的连接写东西只会抛 RuntimeError。
        produced = bool(msgparts.text_of(parts)) or any(
            part.get("type") == "tool_call" for part in parts
        )
        _finish(
            db,
            conv,
            assistant,
            parts,
            "partial" if produced else "error",
            "" if produced else "已被中断",
            finish,
            gateway.elapsed_ms(started),
            usage,
        )
        raise
    except Exception as exc:  # noqa: BLE001
        # 状态码早就发出去了，这里能做的就是变成一条 error 事件，
        # 而不是让连接就这么断在半路（前端收不到收尾事件会一直转圈）
        status = "error"
        error = str(exc)[:300]

    _finish(db, conv, assistant, parts, status, error, finish, gateway.elapsed_ms(started), usage)

    if status == "error":
        if not error_sent:
            yield _sse("error", {"message": "生成失败", "detail": error, "retryable": True})
        return

    yield _sse("usage", usage)
    yield _sse("done", _message_out(assistant))
