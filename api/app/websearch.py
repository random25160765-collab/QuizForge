"""联网搜索 —— 让模型能查到"它自己不知道"的东西。

## 为什么这一组要有

模型的知识有个截止日期，而项目里那几路检索（笔记 / 资料 / 图谱 / 题库）全部长在
**本机**：问"这个方法现在怎么做"、"这本书第几版改了什么"、"这个词的英文通常怎么写"，
它只有两条路 —— 硬答（编），或者说"我查不到"。这一组补的就是这个缺口。

## 为什么是"搜 + 读"两个工具，而不是一个

搜索只回标题、网址与一小段摘要，而**摘要常常正好缺了你要的那一句**。只给摘要的
后果不是"查不到"，是模型拿着一句残缺的摘要继续往下编 —— 那比承认查不到更糟。
所以配一个 `read_web_page`：搜到之后能真去读正文。这与库内那两路的分工是同一条
道理（`search_material` 找段落 / `read_material` 精读）。

## 网页 → 文本是**启发式**，不是解析

为一个功能拉一棵 HTML 解析依赖（bs4 / trafilatura）不划算，项目里 `attachments.py`
也是正则处理。这里的做法：先整块删掉脚本 / 样式 / 导航 / 页脚，再按 `article` /
`main` 这类语义标签收窄，然后把块级标签换成换行、剥掉剩下的标签。
**它一定在某些站点上抽不好** —— 所以抽出来的正文太短时明确交回一句"这一页可能是
脚本渲染的，别当成页面没内容"，而不是给一片空白让模型以为原文本来就是空的。

## 默认那条路**不要密钥**（必应的 RSS 输出）

第一版这里是"必须先去申请一把密钥"。用户一句话把它问回来了：

> 为什么一定要配密钥呢？我们自己实现一个联网搜索的功能不可以吗？

可行，而且对这套东西更对路 —— 它是**本地、单用户、量很小**的形态，
"先去注册一个账号拿 key" 与"双击即用"是互相别扭的两件事。所以：

* **默认走必应**：它有一个老式的 RSS 输出（`?format=rss`），回的是一份
  规规矩矩的 XML（`title` / `link` / `description` / `pubDate`），
  **不用解析搜索结果页的 DOM** —— 那才是"自己实现搜索"最容易写坏的地方
  （别人的页面结构一改，你的正则就悄悄失效）。中文查询也正常。
* **收费那三家是升级，不是入场券**：博查 / Tavily / Serper 各自有额度、
  有 SLA、摘要给得更长，量大或要稳定就填一把自己的密钥切过去。

代价要说清，别让人以为它和收费的等价：

1. **一次最多 10 条**（RSS 输出的上限），要更多只能换关键词多搜几次；
2. **它是"个人非商业用途"的口子**（必应自己在 `<copyright>` 里写明：
   允许"出于个人的非商业用途在 RSS 聚合器中呈现"）。本项目正好是这个情形 ——
   但**这不是一条可以拿去做产品的路**，要商用就得换成收费服务商。
3. **量大了会被限流**，表现是回一页 HTML（同意页 / 验证码）而不是 XML。
   这一条**专门判**：不是 XML 就明说"被挡了、换一家"，而不是表现成"没搜到"
   —— 那两种情况的下一步动作完全不同。

## 学术文献走**另一条**索引（同样免密钥）

通用网页索引在这件事上不是"差一点"，是**结构性做不到**：它按网页排序，而学术
文献的关键词是**缩写**。实测（用户真撞上的那一轮）：

| 搜什么 | 拿回来什么 |
|---|---|
| `WCET analysis cache abstract interpretat` | 中国计算机学会的 WCET 研讨会、世界造口治疗师协会、美国某高等教育政策机构 |
| `worst-case execution time analysis abstr` | "worst" 的词典释义 |
| `cache analysis WCET includeDomains=arxiv/ACM/IEEE/Springer` | **零结果** |
| `"static cache simulation" "timing analys` | C 语言 `static` 关键字的教程 |
| `Ferdinand Heckmann "cache behavior predi` | 斐迪南大公、里奥·费迪南德、动画电影《公牛历险记》 |
| `Wilhelm "the worst-case execution-time p` | 豆包大模型的产品讨论 |

**这几种全是"成功的搜索"** —— 有结果、有摘要、没有任何错误。所以调用方没法从
"有没有结果"判断成败，只能从"这是不是我要的东西"判断，而那要读过才知道。
**这是最坏的一类失败：看起来查过了。**

所以学术这一路挂 **OpenAlex**（免密钥、按学术实体索引）：标题 / 作者 / 年份 /
发表处 / DOI / 被引数 / 摘要。同一批查询它一条不落：

```
· Cache behavior prediction by abstract interpretation
  1996 | Christian Ferdinand, Florian Martin | 被引 172      ← 上表第三行那个"没核到"的
· The Mälardalen WCET Benchmarks: Past, Present And Future   2010 | 被引 336
· A Survey on Static Cache Analysis for Real-Time Systems    2016 | 被引 67
· Scope-Aware Data Cache Analysis for WCET Estimation        2011 | 被引 88
```

两者分工写在 `search_papers` 的说明里：**问"哪篇论文 / 谁做的 / 哪一年的"走它，
问"现在怎么做 / 这个型号的参数"走 `web_search`。**

密钥那份配置仍然认（`user_settings.data.search`，以及内测通道
`config/ai.local.json` 里的 `search` 块）：填了就用你填的，没填就用免密钥那条。

## 第一条纪律：**不许卡住**

联网是唯一会去碰"不属于这台机器的东西"的一层，所以它每一种"没反应"都必须有一个
**看得见的墙钟上限**（用户："联网这一块是堵塞重灾区 —— 如果连不上网，务必不要让它
卡住"）：连接 6 秒、DNS 5 秒、读一页总计 15 秒。见下面那组常量。

两处"看着有超时、其实没有"的地方，是这次专门补的：

* **`socket.getaddrinfo` 没有超时参数**。网络不通时它自己会重试十几秒，而"连不上网"
  正是最先走到这里的时候 —— 所以套了一层墙钟，见 `_resolve`。
* **httpx 的 `read` 超时是"两次读之间"的上限，不是总时长**。对方一次吐一个字节，
  就永远不触发它，而连接一直"有响应"、能挂到天荒地老 —— 所以读正文另有
  `PAGE_TOTAL_S` 那条总预算，见 `_read_limited`。

失败一律**变成一条说清楚的错误**交回模型（"连不上/超时/被挡了"各自的下一步不同），
而不是让用户对着转圈等。

## 出网的边界（`read_web_page` 为什么要查 IP）

网址是**模型**给的，而模型会从搜索结果、甚至网页正文里捡 URL 出来读 ——
"去读一下 http://127.0.0.1:8100/…"、或者云主机上的 `169.254.169.254`（元数据服务），
是一条真实存在的路。所以：只放 http/https，**解析之后的每个 IP** 落在回环 /
私有 / 链路本地 / 保留段就拒绝，并且**手动跟重定向、每一跳都重新查一遍**
（自动跟的话，一个公网网址 302 到内网就绕过去了）。这是"能让模型主动发请求"
这类工具的必答题，不是可选项。见 `_guard_url`。
"""

from __future__ import annotations

import html as html_mod
import ipaddress
import json
import re
import socket
import threading
import time
import xml.etree.ElementTree as ET
from urllib.parse import ParseResult, urljoin, urlparse

import httpx

#: 设置里那一块（`user_settings.data.search` / 内测配置里的 `search`）。
KEY = "search"

#: 没配任何东西时走哪家 —— **免密钥**那条（见模块 docstring：
#: "零配置就能用"是刻意的，密钥是升级不是入场券）。
DEFAULT_KIND = "bing"

#: 必应那个 RSS 输出一次最多给几条。它**不认** `count` 参数，所以这是硬上限。
BING_MAX_COUNT = 10

DEFAULT_COUNT = 6
MAX_COUNT = 20
SEARCH_TIMEOUT_S = 15.0

# ---------------------------------------------------------------- "不卡住"这条线
#
# 联网是唯一会去碰**不属于这台机器**的东西的一层，所以它的每一种"没反应"都必须有
# 一个**看得见的墙钟上限**（用户："联网这一块是堵塞重灾区 —— 如果连不上网，
# 务必不要让它卡住"）。四个上限：

#: **连不上就早点认输。** 这条最关键：离线 / DNS 黑洞 / 防火墙丢包时，TCP 连接会
#: 一直重试（系统默认能磨一两分钟），那就是"卡住"最常见的形态。
CONNECT_TIMEOUT_S = 6.0
#: DNS 解析的墙钟上限 —— `socket.getaddrinfo` **没有超时参数**，得自己套一层（见 `_resolve`）。
DNS_TIMEOUT_S = 5.0
#: 读**一个网页**的总预算：跟重定向、对方慢慢吐字节全算在里面。
#:
#: 只设"每阶段超时"是不够的 —— 对方一次吐一个字节，就永远不触发读超时，
#: 而它又始终"有响应"，于是能一直挂着。所以这条是**总时长**，不是每次读的间隔。
PAGE_TOTAL_S = 15.0
#: 一页最多收这么多字节（正文抽出来之后还要再截一次，见 `MAX_PAGE_CHARS`）。
MAX_PAGE_BYTES = 512 * 1024
#: 一次搜索响应最多收这么多字节。搜索响应本来就小（几 KB），这个上限是防"对方一直吐"。
MAX_SEARCH_BYTES = 1024 * 1024

#: 学术通道（OpenAlex）：免密钥、按**学术实体**索引。见模块 docstring 那一节。
SCHOLAR_ENDPOINT = "https://api.openalex.org/works"
SCHOLAR_MAX_COUNT = 50
#: 只要这几个字段。**显式列出来**不只是为了省流量 —— 不列的话它默认返回一大堆
#: （`abstract_inverted_index` 尤其大），一次好几 MB；而这通道是公开的免费服务，
#: 少要点是起码的礼貌。实测显式列了之后一份响应约 20KB。
OPENALEX_SELECT = (
    "id,doi,title,display_name,publication_year,cited_by_count,type,"
    "authorships,primary_location,best_oa_location,abstract_inverted_index"
)
#: 论文摘要给到 1200 字（网页那条是 600）。理由见 `_result`。
SCHOLAR_SNIPPET_CHARS = 1200
MAX_PAGE_CHARS = 12000
#: 抽出来的正文少于这个字数就怀疑"抽失败了"（脚本渲染 / 正文在图片里）。
MIN_GOOD_CHARS = 220
#: 最多跟几跳重定向。
MAX_REDIRECTS = 4
_REDIRECT_CODES = (301, 302, 303, 307, 308)

#: 老实交代自己是谁。`Mozilla/5.0 (compatible; …)` 这个前缀很多站点的机器人过滤
#: 会放行 —— 写一个完全不认识的 UA，一半的页面会直接 403。
USER_AGENT = "Mozilla/5.0 (compatible; QuizForge/1.0)"


class SearchError(Exception):
    """联网这一层的失败。消息会**原样交给模型**（`tools.call` 收成一条 error 结果）。

    所以措辞要当"给模型看的话"来写：说清下一步该干什么，而不是只说"失败了"。
    """


class SearchUnavailable(SearchError):
    """这一项现在用不了（被关掉了、或者服务商不认识）。

    注意：**"没配密钥"不再是这一条的触发条件** —— 免密钥那条默认就在（见 `resolve`）。
    这条消息其实也是**写给用户看**的：模型会照着念给他听。
    """


# ------------------------------------------------------------------ 服务商
#
# 加一家就是加一条：`request` 造请求、`results` 解析回来的东西。别处都不用动。
# 两家共用一套形状 —— JSON 的那些与免密钥那条（是 XML）走的都是同一条出口。


def _clip(text, limit: int = 160) -> str:  # noqa: ANN001
    return " ".join(str(text or "").split())[:limit]


def _text_of(item, tag: str) -> str:  # noqa: ANN001
    found = item.find(tag)
    return (found.text or "").strip() if found is not None and found.text else ""


def _json_body(reply) -> dict:  # noqa: ANN001
    try:
        data = reply.json()
    except ValueError as exc:
        raise SearchError(
            "搜索服务回了不是 JSON 的东西（可能被网关挡了）：" + _clip(reply.text)
        ) from exc
    if not isinstance(data, dict):
        raise SearchError("搜索服务回了意料之外的形状（不是对象）。")
    return data


# -- 免密钥：必应 ---------------------------------------------------------
#
# 走它那个老式的 RSS 输出（`?format=rss`）。**这是刻意选的**：搜索结果页的 DOM
# 是给浏览器看的，拿正则去啃它，人家改一次版式你就悄悄失效；RSS 是给机器读的
# 一份固定 XML（`item` 里就 title / link / description / pubDate 四个字段）。
# 这就是"自己实现一个搜索"里唯一值得做的那条路 —— 见模块 docstring 的取舍说明。


def _bing_request(conf: dict, query: str, count: int) -> dict:
    return {
        "method": "GET",
        "url": str(conf["endpoint"]),
        # UA 用老实那个就行 —— 实测它不挑 UA（`curl/8.0` 也一样拿得到），
        # 真正要紧的是**跟重定向**（见 `search`）：直接请求先吃一个 302。
        "headers": {
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
        "params": {"q": query, "format": "rss", "count": min(count, BING_MAX_COUNT)},
    }


def _bing_blocked(reply) -> SearchError:  # noqa: ANN001
    """被挡了的统一说法。**必须与"这次真的没搜到"分开**：

    前者要等一会儿或换一家服务商，后者只要换个搜索词 —— 都报"没搜到"，
    用户会一直换词，而真正该做的那件事一次都没做。
    """
    return SearchError(
        "必应没有回结果（回的是一页 HTML，通常是限流或要求验证）："
        + _clip(reply.text, 120)
        + "　等一下再试；或者去「设置 → 联网搜索」换一家（博查 / Tavily / Serper，要填一把密钥）。"
    )


def _bing_results(reply) -> list[dict]:  # noqa: ANN001
    try:
        root = ET.fromstring(reply.content)
    except ET.ParseError as exc:
        raise _bing_blocked(reply) from exc
    # **还要看根元素**：一页 HTML 有时**恰好是合法 XML**（`<html><body>…</body></html>`
    # 就解析得动，实测撞到过）—— 只按"解析失败"判，那一页会被当成"这次真的没搜到"。
    # 根元素是 `rss` / `feed` 才是我们要的那份；那时**一个 item 都没有**才是真的没搜到。
    if str(root.tag or "").split("}")[-1].lower() not in ("rss", "feed"):
        raise _bing_blocked(reply)
    out: list[dict] = []
    for item in root.findall(".//item"):
        url = _text_of(item, "link")
        # 它偶尔把"再搜一次这个词"本身也当成一条结果（link 指回 bing 自己）。
        # 那不是结果，交出去只会白占模型一次判断。
        if not url or _host_of(url).endswith("bing.com"):
            continue
        out.append(
            _result(
                title=_text_of(item, "title"),
                url=url,
                snippet=_text_of(item, "description"),
                site=_host_of(url),
                date=_text_of(item, "pubDate"),
            )
        )
    return out


# -- 收费三家（填了密钥才走这些）------------------------------------------------


def _bocha_request(conf: dict, query: str, count: int) -> dict:
    return {
        "method": "POST",
        "url": str(conf["endpoint"]),
        "headers": {"Authorization": "Bearer " + conf["apiKey"], "Content-Type": "application/json"},
        # `summary` 让博查多给一段比 snippet 长的摘要 —— 模型先看它决定要不要读正文
        "json": {"query": query, "count": count, "summary": True},
    }


def _bocha_results(reply) -> list[dict]:  # noqa: ANN001
    data = _json_body(reply)
    # 博查把业务错误放在 200 的 body 里（HTTP 是通的，`code` 不是 200）——
    # 不看它的话，"密钥没额度"会表现成"一条都没搜到"，那是最难查的一种坏法。
    code = data.get("code")
    if isinstance(code, int) and code != 200:
        raise SearchError(
            "搜索服务返回错误 " + str(code) + "：" + _clip(data.get("msg"), 120)
            + "（密钥或额度的问题，去「设置 → 联网搜索」看一眼）"
        )
    payload = data.get("data")
    if isinstance(payload, str):  # 有的版本把 data 塞成一段 JSON 字符串
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    pages = ((payload or {}).get("webPages") or {}).get("value") or []
    return [
        _result(
            title=one.get("name"),
            url=one.get("url"),
            snippet=one.get("summary") or one.get("snippet"),
            site=one.get("siteName"),
            date=one.get("datePublished"),
        )
        for one in pages
        if isinstance(one, dict)
    ]


def _tavily_request(conf: dict, query: str, count: int) -> dict:
    return {
        "method": "POST",
        "url": str(conf["endpoint"]),
        "headers": {"Authorization": "Bearer " + conf["apiKey"], "Content-Type": "application/json"},
        "json": {"query": query, "max_results": count, "search_depth": "basic"},
    }


def _tavily_results(reply) -> list[dict]:  # noqa: ANN001
    data = _json_body(reply)
    return [
        _result(title=one.get("title"), url=one.get("url"), snippet=one.get("content"))
        for one in (data.get("results") or [])
        if isinstance(one, dict)
    ]


def _serper_request(conf: dict, query: str, count: int) -> dict:
    return {
        "method": "POST",
        "url": str(conf["endpoint"]),
        "headers": {"X-API-KEY": conf["apiKey"], "Content-Type": "application/json"},
        "json": {"q": query, "num": count},
    }


def _serper_results(reply) -> list[dict]:  # noqa: ANN001
    data = _json_body(reply)
    return [
        _result(
            title=one.get("title"),
            url=one.get("link"),
            snippet=one.get("snippet"),
            date=one.get("date"),
        )
        for one in (data.get("organic") or [])
        if isinstance(one, dict)
    ]


# -- 学术通道（OpenAlex，免密钥）--------------------------------------------------
#
# 为什么**不**再往 `PROVIDERS` 里塞一家：那不是"再添一个选项"，是换了一种索引
# 方式 —— 通用网页索引按**网页**排序、学术索引按**文献实体**排序，同一个问题在
# 两边的正确用法都不一样（一边教"给特别的原词"，一边教"给主题词、看年份与被引"）。
# 所以出口是**另一个工具**（`search_papers`），让模型按问题的性质选。
#
# 结构仍然照 `PROVIDERS` 的样子：加一家（Crossref / arXiv / Semantic Scholar）
# = 加一条 `request` + `results`，别处不用动。


def _abstract_of(inverted) -> str:  # noqa: ANN001
    """把 OpenAlex 的**倒排摘要**还原成一段话。

    它给的不是摘要文本，而是 `{词: [位置, …]}`（倒排是为了压缩与检索）。
    交回模型之前必须还原成能读的顺序 —— 否则"有摘要"等于没摘要，
    而**摘要正是判断一篇文献要不要读的唯一依据**。

    实测有一部分作品**根本没有摘要**（`abstract_inverted_index` 缺席）：
    那种就留空，不编。
    """
    if not isinstance(inverted, dict) or not inverted:
        return ""
    slots: list[tuple[int, str]] = []
    for word, positions in inverted.items():
        if not isinstance(positions, list):
            continue
        for position in positions:
            try:
                slots.append((int(position), str(word)))
            except (TypeError, ValueError):
                continue
    slots.sort()
    return " ".join(word for _position, word in slots)


def _openalex_request(conf: dict, query: str, count: int) -> dict:
    return {
        "method": "GET",
        "url": SCHOLAR_ENDPOINT,
        "headers": {"User-Agent": USER_AGENT, "Accept": "application/json"},
        # `search` 是**相关度**排序（不是字面匹配）—— 这正是这一路的价值：
        # 问"缓存分析怎么用抽象解释做"它能找回来，不要求你猜中作者写的原词。
        "params": {
            "search": query,
            "per-page": min(count, SCHOLAR_MAX_COUNT),
            "select": OPENALEX_SELECT,
        },
    }


def _openalex_results(reply) -> list[dict]:  # noqa: ANN001
    data = _json_body(reply)
    out: list[dict] = []
    for one in data.get("results") or []:
        if not isinstance(one, dict):
            continue
        authors = [
            str((entry.get("author") or {}).get("display_name") or "")
            for entry in (one.get("authorships") or [])
            if isinstance(entry, dict)
        ]
        authors = [name for name in authors if name]
        # 发表处：`source.display_name` 是规范名，但**实测很多记录它是 `null`**
        # （尤其 IEEE / ACM 那批 conference paper），而旁边那个 `raw_source_name`
        # 有值（"2011 17th IEEE Real-Time…"）。只用前一个会把发表处整列丢空 ——
        # 而"发在哪"正是模型判断一篇文献可不可信的主要依据之一。
        location = one.get("primary_location") or {}
        venue = str(
            ((location.get("source") or {}).get("display_name") or "")
            or location.get("raw_source_name")
            or ""
        )
        # 一行书目信息 —— 模型靠它判断可信度与时效（哪一年、谁做的、被引多少）
        facts: list[str] = []
        if authors:
            facts.append(", ".join(authors[:3]) + (" 等" if len(authors) > 3 else ""))
        if venue:
            facts.append(venue)
        if one.get("cited_by_count"):
            facts.append("被引 " + str(one["cited_by_count"]))
        if one.get("type"):
            facts.append(str(one["type"]).replace("-", " "))
        snippets: list[str] = []
        if facts:
            snippets.append(" · ".join(facts) + "。")
        abstract = _abstract_of(one.get("abstract_inverted_index"))
        if abstract:
            snippets.append(abstract)
        # URL 优先给**开放获取的 PDF**（能直接读全文），没有才退回 DOI。
        # 顺序要紧：DOI 点进去常常是付费墙，而 `read_web_page` 读不到正文时，
        # 模型会以为"这篇没内容"。
        pdf = str((one.get("best_oa_location") or {}).get("pdf_url") or "")
        out.append(
            _result(
                title=one.get("title") or one.get("display_name"),
                url=pdf or str(one.get("doi") or one.get("id") or ""),
                snippet=" ".join(snippets),
                site=venue,
                date=str(one.get("publication_year") or ""),
                snippet_limit=SCHOLAR_SNIPPET_CHARS,
            )
        )
    return out


#: 学术通道的服务商表。结构与 `PROVIDERS` 一样（`keyless` 那条同样不带密钥）。
SCHOLAR: dict[str, dict] = {
    "openalex": {
        "label": "OpenAlex（免密钥）",
        "endpoint": SCHOLAR_ENDPOINT,
        "keyless": True,
        "request": _openalex_request,
        "results": _openalex_results,
    },
}
DEFAULT_SCHOLAR = "openalex"


#: 支持的服务商。**顺序就是界面下拉的顺序**，第一项是默认值。
#:
#: * `bing` 是**免密钥**那条（`keyless: True`）：零配置可用，是默认。
#:   代价写在模块 docstring 里（一次最多 10 条、个人非商业用途、量大会被限流）。
#: * 另外三家要一把自己的密钥，换来的是额度、稳定与更长的摘要。
#:   博查国内可直连、中文语料好；Tavily 是"给模型用的搜索"；Serper 背后是 Google。
#:
#: 加一家 = 加一条 `request` + `results`，别处都不用动。
PROVIDERS: dict[str, dict] = {
    "bing": {
        "label": "必应（免密钥）",
        "endpoint": "https://www.bing.com/search",
        "keyless": True,
        "request": _bing_request,
        "results": _bing_results,
    },
    "bocha": {
        "label": "博查（Bocha）",
        "endpoint": "https://api.bochaai.com/v1/web-search",
        "request": _bocha_request,
        "results": _bocha_results,
    },
    "tavily": {
        "label": "Tavily",
        "endpoint": "https://api.tavily.com/search",
        "request": _tavily_request,
        "results": _tavily_results,
    },
    "serper": {
        "label": "Serper（Google）",
        "endpoint": "https://google.serper.dev/search",
        "request": _serper_request,
        "results": _serper_results,
    },
}

#: 界面上的下拉选项：**从上面那张表推出来**，不另抄一份
#:（抄成两份，早晚有一天对不上 —— 加一家只改了上面就漏了这里）。
PROVIDER_CHOICES: tuple[tuple[str, str, str], ...] = tuple(
    (kind, preset["label"], preset["endpoint"]) for kind, preset in PROVIDERS.items()
)


# ------------------------------------------------------------------ 配置


def _clamp_int(value, low: int, high: int, default: int) -> int:  # noqa: ANN001
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(number, high))


def _normalize(raw: dict, *, source: str) -> dict | None:
    """一处设置 → 一份能用的配置。

    **要密钥的那几家没有密钥就返回 `None`**（= 这次没配，让调用方往下找）；
    免密钥那条（`keyless`）不填密钥也照样成立 —— 这正是"零配置可用"的落点。
    """
    kind = str(raw.get("kind") or DEFAULT_KIND).strip().lower()
    if kind not in PROVIDERS:
        raise SearchUnavailable(
            "设置里的搜索服务商不认识：" + kind + "。可填：" + "、".join(PROVIDERS) + "。"
        )
    preset = PROVIDERS[kind]
    key = str(raw.get("apiKey") or "").strip()
    if not key and not preset.get("keyless"):
        return None
    return {
        "kind": kind,
        "label": preset["label"],
        "endpoint": str(raw.get("endpoint") or "").strip() or preset["endpoint"],
        "apiKey": key,
        "count": _clamp_int(raw.get("count"), 1, MAX_COUNT, DEFAULT_COUNT),
        "timeoutMs": _clamp_int(raw.get("timeoutMs"), 3000, 60000, int(SEARCH_TIMEOUT_S * 1000)),
        "source": source,
    }


def resolve(db) -> dict:  # noqa: ANN001
    """这次该用哪套搜索配置，顺序是：

    1. **用户自己填的**（`user_settings.data.search`，填了密钥就用他的）；
    2. **内测通道那份**（`config/ai.local.json` 的 `search`，只在 beta 通道）；
    3. **免密钥那条**（必应）—— 所以"什么都没配"不等于"用不了"。

    第 3 条是刻意的（见模块 docstring）：联网搜索**零配置就能用**，
    密钥是升级不是入场券。用户的原话："为什么一定要配密钥呢？"

    只有一种情况它真的"不可用"：用户在设置里**显式关掉了**这一项 ——
    那时说清是关着的，别让人以为坏了。
    """
    from . import ai_gateway, settings_store  # 延迟导入：`ai_gateway` 会回调这边

    mine = settings_store.load(db).get(KEY)
    if isinstance(mine, dict) and mine.get("enabled") is False:
        raise SearchUnavailable(
            "联网搜索你在设置里关掉了 —— 要用的话打开「设置 → 联网搜索」的那个开关，"
            "它是免密钥就能走的（默认走必应）。"
        )

    for source, raw in (
        ("user", mine if isinstance(mine, dict) else None),
        ("beta", ((ai_gateway.beta_config() or {}).get(KEY) or None)),
    ):
        if not isinstance(raw, dict):
            continue
        found = _normalize(raw, source=source)
        if found:
            return found

    # 什么都没配：走免密钥那条（`DEFAULT_KIND`）。
    found = _normalize({"kind": DEFAULT_KIND}, source="keyless")
    if found:
        return found
    # 只有把默认那家改成"要密钥的"才会走到这儿 —— 兜底报一条说得清的话
    raise SearchUnavailable(
        "联网搜索没有可用的服务商：去「设置 → 联网搜索」选一个（免密钥的必应，"
        "或者填一把博查 / Tavily / Serper 的密钥）。"
    )


def _provider_list() -> list[dict]:
    return [
        {"kind": kind, "label": label, "endpoint": endpoint,
         "keyless": bool(PROVIDERS.get(kind, {}).get("keyless"))}
        for kind, label, endpoint in PROVIDER_CHOICES
    ]


def state(db) -> dict:  # noqa: ANN001
    """给界面的一份状态：走哪条路、要不要密钥。**不含密钥**。"""
    try:
        conf = resolve(db)
    except SearchUnavailable as exc:
        return {"ready": False, "message": str(exc), "providers": _provider_list()}
    return {
        "ready": True,
        "kind": conf["kind"],
        "label": conf["label"],
        "source": conf["source"],
        "keyless": bool(PROVIDERS.get(conf["kind"], {}).get("keyless")),
        "providers": _provider_list(),
    }


# ------------------------------------------------------------------ 搜索


def _one_line(value, limit: int) -> str:  # noqa: ANN001
    """压成一行。搜索回来的标题 / 摘要里常带 `<em>` 高亮与换行 —— 那些是它们
    为了让**人**在结果页上看得清才加的，进了上下文只是噪声。"""
    text = _TAG.sub("", str(value or ""))
    text = html_mod.unescape(text)
    return " ".join(text.split())[:limit]


def _result(*, title=None, url=None, snippet=None, site=None, date=None,
            snippet_limit: int = 600) -> dict:  # noqa: ANN001
    """一条结果。字段名**固定这五个** —— 所有服务商都映射到同一种形状。

    `snippet_limit` 是给学术通道留的口子：论文摘要比网页那段"高亮片段"长得多，
    而**摘要正是判断一篇文献要不要读的唯一依据**（网页还能点进去看，文献
    点进去常常是付费墙）。而且缩在 600 字上正好会砍掉摘要的后半段 ——
    结论往往在那儿。
    """
    return {
        "title": _one_line(title, 200),
        "url": _one_line(url, 500),
        "snippet": _one_line(snippet, snippet_limit),
        "site": _one_line(site, 80),
        "date": _one_line(date, 40),
    }


def _domains(raw) -> list[str]:  # noqa: ANN001
    """`["arxiv.org", "  docs.python.org "]` / `"arxiv.org, docs.python.org"` 都收。"""
    if raw is None:
        return []
    parts = raw if isinstance(raw, (list, tuple, set)) else re.split(r"[,，;；\s]+", str(raw))
    out: list[str] = []
    for one in parts:
        name = str(one).strip().lower().lstrip(".").removeprefix("www.")
        if name and name not in out:
            out.append(name)
    return out


def _host_of(url: str) -> str:
    try:
        return (urlparse(str(url)).hostname or "").lower()
    except ValueError:
        return ""


def _matches(host: str, domains: list[str]) -> bool:
    return any(host == one or host.endswith("." + one) for one in domains)


def _filter(items: list[dict], include: list[str], exclude: list[str]) -> list[dict]:
    if not include and not exclude:
        return items
    out = []
    for one in items:
        host = _host_of(one.get("url") or "")
        if include and not _matches(host, include):
            continue
        if exclude and _matches(host, exclude):
            continue
        out.append(one)
    return out


def _http_hint(preset: dict, conf: dict, reply) -> str:  # noqa: ANN001
    """HTTP 失败的**可操作**说法：401 与 429 的下一步动作完全不同。

    `preset` 单独传进来：学术通道不在 `PROVIDERS` 里（见那张表上面的注释），
    而"要不要提示去改密钥"取决于 `keyless` —— 从 preset 上读，两条通道共用这一处话术。
    """
    code = reply.status_code
    body = _clip(reply.text, 140)
    tail = ("　它说：" + body) if body else ""
    if code in (401, 403):
        if preset.get("keyless"):
            # 免密钥那条没有"密钥填错了"这回事，给它同一句话只会让人去改一个不存在的框
            return (
                "搜索服务把这次请求挡了（HTTP " + str(code) + "）—— 免密钥那条走的是"
                "**个人小量**的口子，被挡通常是量大了或者要过验证。等一下再试；"
                "或者去「设置 → 联网搜索」换一家（那几家要填一把自己的密钥）。" + tail
            )
        return (
            "搜索服务的密钥被拒了（HTTP " + str(code) + "）—— 去「设置 → 联网搜索」"
            "核对一下密钥有没有填错、额度还在不在。" + tail
        )
    if code == 429:
        return "搜索服务限流了（HTTP 429）：等一会儿再搜，或者换一家服务商。" + tail
    return "搜索服务返回 HTTP " + str(code) + "（" + conf["label"] + "）。" + tail


def _perform(preset: dict, conf: dict, text: str, ask: int) -> list[dict]:
    """发一次请求并解析。**重定向、总预算、每一种超时与它们各自的话术，全在这一处。**

    为什么抽出来：那套"不许卡住"的墙钟（连接单独 6 秒、一条总预算管住
    "连 + 跳 + 读完"）与"手动跟重定向"是**踩出来的**（见模块 docstring 那两条）。
    给学术通道复制一份，早晚有一份会走样 —— 而走样的表现是"偶尔卡住"，最难查。
    """
    req = preset["request"](conf, text, ask)
    method = str(req.get("method") or "POST").upper()

    # **一条总预算管住整件事**（连、跟重定向、读完 body 全算在里面）。
    # 不用 `follow_redirects=True`：那是"每一跳各给一份超时"，跳几跳就翻几倍。
    total = conf["timeoutMs"] / 1000
    deadline = time.monotonic() + total
    # 这里**不**过 `_guard_url`（读网页那条路才过）：接口地址来自**设置**，
    # 是用户自己填的 —— 本机自建一个搜索网关（SearXNG 之类）是正当用法。
    url = str(req["url"])
    reply = None
    with httpx.Client() as client:
        for _hop in range(MAX_REDIRECTS + 1):
            left = deadline - time.monotonic()
            if left <= 0:
                break
            timeout = httpx.Timeout(left, connect=min(CONNECT_TIMEOUT_S, left))
            try:
                with client.stream(
                    method,
                    url,
                    params=req.get("params") if _hop == 0 else None,
                    json=req.get("json"),
                    headers=req.get("headers"),
                    timeout=timeout,
                ) as response:
                    if response.status_code in _REDIRECT_CODES and _hop < MAX_REDIRECTS:
                        # 免密钥那条（必应的 RSS）直接请求先吃一个 302 —— 得跟，
                        # 但每一跳都重新算一次剩下的预算，而不是各给一份。
                        location = response.headers.get("location")
                        if not location:
                            raise SearchError("搜索服务要求跳转，但没说要跳去哪。")
                        url = urljoin(str(response.url), location)
                        if response.status_code == 303:  # 协议规定：303 之后改用 GET
                            method, req = "GET", {**req, "json": None}
                        continue  # 查询串由 Location 带着走，下一跳不再重发参数
                    body = _read_body(response, deadline)
                    reply = _Reply(
                        response.status_code, body, str(response.headers.get("content-type") or "")
                    )
            except httpx.ConnectTimeout as exc:
                # **连接那一段单独说**：它比整体预算短得多（6 秒对 15 秒），
                # 报"超过 15 秒"会让去排查的人按错的数字找问题 —— 实测踩到过。
                raise SearchError(
                    "连不上搜索服务（" + str(int(CONNECT_TIMEOUT_S)) + " 秒内没连上）—— "
                    "这台机器可能连不上网。等一下再试，或者先把要查的东西贴给我。"
                ) from exc
            except httpx.TimeoutException as exc:
                raise SearchError(
                    "搜索服务没有响应（超过 " + str(int(total)) + " 秒）—— "
                    "这台机器可能连不上网。等一下再试，或者先把要查的东西贴给我。"
                ) from exc
            except httpx.HTTPError as exc:
                raise SearchError(
                    "连不上搜索服务（" + type(exc).__name__ + "）：" + str(exc)[:140]
                    + "。检查一下这台机器能不能出网。"
                ) from exc
            break
    if reply is None:
        raise SearchError(
            "搜索服务没有响应（超过 " + str(int(total)) + " 秒）—— 这台机器可能连不上网，"
            "或者那个服务太慢。等一下再试；也可以去「设置 → 联网搜索」换一家。"
        )
    if reply.status_code >= 400:
        raise SearchError(_http_hint(preset, conf, reply))
    return preset["results"](reply)


def _prepare(query: str) -> str:
    """搜索词归一。两条通道共用。"""
    text = " ".join(str(query or "").split())
    if not text:
        raise SearchError("搜索词是空的 —— 要搜什么？")
    return text


def search(
    conf: dict,
    query: str,
    *,
    count=None,  # noqa: ANN001
    include_domains=None,  # noqa: ANN001
    exclude_domains=None,  # noqa: ANN001
    stats: dict | None = None,
) -> list[dict]:
    """搜一次公开网页。各家的请求形状与返回格式都不一样，**出口只有这一种**。

    域名过滤是**本地筛**：各家的语法不同（写进 query 只有 Google 那一系认
    `site:` —— 实测免密钥那条连 `site:` 都不认），所以统一在拿回来之后按 host 筛。
    代价是要多取几条再筛 —— 不然"只在这两个站里找"很容易被筛成空，
    看着像"这世上没有"。

    `stats` 是给调用方看的**过滤账**（`raw` / `kept` / `dropped`）：
    **筛空与"真的没搜到"是两件事**，而调用方只有拿到这个数才分得清 ——
    否则它会照着"零结果"告诉用户"这世上没有"，而那是最坏的一种结论
    （用户真撞上过：限定 arxiv/ACM/IEEE/Springer 之后拿回"零结果"，
    而实际是那 10 条里一条都不在这些站上）。
    """
    text = _prepare(query)
    want = _clamp_int(count, 1, MAX_COUNT, int(conf["count"]))
    include = _domains(include_domains)
    exclude = _domains(exclude_domains)
    ask = want if not (include or exclude) else min(MAX_COUNT, max(want * 3, want + 6))

    items = _perform(PROVIDERS[conf["kind"]], conf, text, ask)
    kept = _filter(items, include, exclude)
    if stats is not None:
        stats.update(raw=len(items), kept=len(kept), dropped=len(items) - len(kept))
    return kept[:want]


def search_papers(
    conf: dict,
    query: str,
    *,
    count=None,  # noqa: ANN001
    stats: dict | None = None,
) -> list[dict]:
    """搜学术文献（OpenAlex，免密钥）。与 `search` 共用同一套网络纪律与出口形状。

    **它与 `search` 不是"同一个东西的两种搜法"，是两个索引。** 通用网页索引按
    **网页**排序，而学术文献的关键词是**缩写** —— 实测 `WCET` 搜回来的是同名协会、
    造口治疗师协会与词典释义（见模块 docstring 那张表），而**那些全是"成功的搜索"**。

    这一路**不设 `include_domains`**：它本来就在学术索引里，再按域名筛只会筛空
    （而且 `site:` 语法在这个接口上不存在，只能拿回来再筛，那等于白筛）。

    **超时预算沿用设置里的那一份**："不许卡住"是纪律，不是给某一家的优待。
    """
    text = _prepare(query)
    want = _clamp_int(count, 1, SCHOLAR_MAX_COUNT, DEFAULT_COUNT)
    preset = SCHOLAR[DEFAULT_SCHOLAR]
    # 学术通道不经设置里的"服务商"（它不是那类东西），但超时那条照旧从 conf 取
    scholar_conf = {**conf, "kind": DEFAULT_SCHOLAR, "label": preset["label"]}
    items = _perform(preset, scholar_conf, text, want)
    if stats is not None:
        stats.update(raw=len(items), kept=len(items), dropped=0)
    return items[:want]


# ------------------------------------------------------------------ 读网页


def _resolve(host: str, port: int) -> list:
    """DNS 解析，**带墙钟上限**。

    `socket.getaddrinfo` 没有超时参数：网络不通时解析器自己会重试，
    实测能挂十几秒 —— 而"连不上网"正是最先走到这条路的时候。

    实现上**不能**用 `ThreadPoolExecutor` 的上下文管理器：`with` 退出时会 join，
    那个卡住的线程会把超时又等回去（等于白设）。所以开一个 daemon 线程 + `Event.wait`，
    到点就返回 —— 那个线程留在库里等系统自己放弃，与调用方无关。
    """
    done = threading.Event()
    box: dict = {}

    def work() -> None:
        try:
            box["infos"] = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except OSError as exc:  # gaierror 是它的子类
            box["error"] = exc
        finally:
            done.set()

    threading.Thread(target=work, daemon=True, name="qf-dns").start()
    if not done.wait(DNS_TIMEOUT_S):
        raise SearchError(
            "域名解析超时（" + host + "，超过 " + str(int(DNS_TIMEOUT_S)) + " 秒）"
            "—— 这台机器可能连不上网。"
        )
    if "error" in box:
        raise SearchError("这个域名解析不了：" + host) from box["error"]
    return box.get("infos") or []


def _guard_url(raw) -> ParseResult:  # noqa: ANN001
    """只放 http/https，且**解析之后的每个 IP** 不能是本机 / 内网。

    为什么非查不可：网址是模型给的，而模型会从搜索结果、甚至网页正文里捡 URL。
    "去读一下 `http://127.0.0.1:8100/…`"、或者云主机上的 `169.254.169.254`
    （元数据服务，能拿临时凭据）—— 这类"能让模型主动发请求"的工具，这是必答题。

    查的是**域名解析出来的 IP**，不是字符串：`127.0.0.1.nip.io` 这种把内网地址
    写进域名的写法，只看字面是看不出来的。
    """
    text = str(raw or "").strip()
    parsed = urlparse(text)
    if parsed.scheme.lower() not in ("http", "https"):
        raise SearchError("只能读 http / https 的网址，给的是：" + (text[:80] or "（空）"))
    host = parsed.hostname
    if not host:
        raise SearchError("这个网址里没有主机名：" + text[:80])
    if parsed.username or parsed.password:
        raise SearchError("网址里不要带用户名密码（那多半是在套凭据）。")
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    for info in _resolve(host, port):
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:  # `::ffff:127.0.0.1` 也是 127.0.0.1
            ip = mapped
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            raise SearchError(
                # 说的是"本机 / 内网 / 保留段"三样：`198.51.100.1` 这种测试保留段
                # 既不是本机也不是内网，只说"内网"会让排查的人往错的方向找。
                "这个网址指向本机 / 内网 / 保留地址（" + host + " → " + str(address) + "），"
                "我不去读它。"
            )
    return parsed


def _read_limited(response: httpx.Response, deadline: float) -> bytes:
    """读一个网页：**边读边数、边看表**。

    两道闸缺一不可：

    * 字节数 —— 不设的话，一个几 G 的流式响应能把这台机器拖垮；
    * 墙钟（`deadline`）—— httpx 的 read 超时是"两次读之间"的上限，对方一次吐
      一个字节就永远不触发它，而连接始终"有响应"，能挂到天荒地老。
    """
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total >= MAX_PAGE_BYTES:
            break
        if time.monotonic() > deadline:
            raise SearchError(
                "这一页传得太慢（超过 " + str(int(PAGE_TOTAL_S)) + " 秒还没读完），不等了 —— "
                "换一条来源。"
            )
    return b"".join(chunks)


def _read_body(response: httpx.Response, deadline: float) -> bytes:
    """读一次搜索响应 —— 与 `_read_limited` 同一套闸，只是上限不同（搜索响应很小）。

    搜索这一侧**也要**有总预算，理由一模一样：`follow_redirects=True` 是"每一跳
    各给一份超时"，五跳就是五份；而"连接建立后一直慢慢吐字节"能把任何按间隔计的
    超时耗光。所以这里是手动跟重定向 + 一条总预算（见 `search`）。
    """
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total >= MAX_SEARCH_BYTES:
            break
        if time.monotonic() > deadline:
            raise SearchError(
                "搜索服务传得太慢（超过 " + str(int(SEARCH_TIMEOUT_S)) + " 秒还没读完），不等了 —— "
                "等一下再试。"
            )
    return b"".join(chunks)


class _Reply:
    """一次搜索响应 —— **只带解析真正要用的那几样**。

    为什么不用 httpx 的 `Response`：要给"读完整个 body"这件事也装上墙钟。
    流式模式下 httpx 的 Response 要么没有 body、要么 `read()` 只受"两次读之间"
    那个超时约束（就是上面说的那个洞）。自己拿一捧字节，反而干净。
    """

    __slots__ = ("status_code", "_content", "_ctype")

    def __init__(self, status_code: int, content: bytes, ctype: str = "") -> None:
        self.status_code = status_code
        self._content = content
        self._ctype = ctype

    @property
    def content(self) -> bytes:
        return self._content

    @property
    def text(self) -> str:
        return _decode(self._content, self._ctype)

    def json(self):  # noqa: ANN201
        return json.loads(self.text)


def _fetch(url: str) -> tuple[str, str, bytes]:
    """抓一个网址。**手动跟重定向**，每一跳都过一遍 `_guard_url`。

    交给 httpx 自动跟是不行的：那样一个公网网址 302 到 `127.0.0.1` 就绕过了那道闸。

    `PAGE_TOTAL_S` 是**整件事**（含每一跳重定向与慢慢吐字节）的总预算，
    不是每一跳各给 15 秒 —— 否则五跳重定向就是 75 秒。
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    deadline = time.monotonic() + PAGE_TOTAL_S
    current = _guard_url(url)  # 里面有 DNS，也是带墙钟的
    with httpx.Client() as client:
        for _hop in range(MAX_REDIRECTS + 1):
            left = deadline - time.monotonic()
            if left <= 0:
                raise SearchError(
                    "这一页太慢了（超过 " + str(int(PAGE_TOTAL_S)) + " 秒还没拿到内容），不等了。"
                )
            try:
                with client.stream(
                    "GET",
                    current.geturl(),
                    headers=headers,
                    # 每一跳都用**剩下的预算**，连接另有更紧的上限（见 `CONNECT_TIMEOUT_S`）
                    timeout=httpx.Timeout(left, connect=min(CONNECT_TIMEOUT_S, left)),
                ) as response:
                    if response.status_code in _REDIRECT_CODES:
                        location = response.headers.get("location")
                        if not location:
                            raise SearchError("这个网址要求跳转，但没说要跳去哪。")
                        current = _guard_url(urljoin(current.geturl(), location))
                        continue
                    if response.status_code >= 400:
                        # 403 是最常见的一种（百度百科、部分媒体站不许程序读取）——
                        # 它的下一步是"换个来源"，与 404"这个网址没了"不是一回事
                        if response.status_code == 403:
                            raise SearchError(
                                "这一页返回 HTTP 403：这个站点不许程序读取（不是我们没抓到）。"
                                "换一条来源 —— 搜索结果里通常有同一件事的另一个页面。"
                            )
                        raise SearchError(
                            "这个网址返回 HTTP " + str(response.status_code) + "，读不到内容。"
                        )
                    ctype = str(response.headers.get("content-type") or "").lower()
                    return ctype, str(response.url), _read_limited(response, deadline)
            except httpx.ConnectTimeout as exc:
                raise SearchError(
                    "连不上这个站点（" + str(int(CONNECT_TIMEOUT_S)) + " 秒内没连上）—— "
                    "这台机器可能连不上网。换一条来源，或者先把内容贴给我。"
                ) from exc
            except httpx.TimeoutException as exc:
                # 超时的下一步是"换来源/等一会儿"，与"域名不存在"完全不同 —— 分开说
                raise SearchError(
                    "抓这一页超时了（超过 " + str(int(PAGE_TOTAL_S)) + " 秒还没拿到内容）—— "
                    "这台机器可能连不上网，或者那个站点太慢。换一条来源。"
                ) from exc
            except httpx.HTTPError as exc:
                # 这里两种可能都真实存在：本机连不上网，或者对方把连接重置了。
                # 一律说成"检查网络"会让人白折腾 —— 两种都说，让他自己看现象。
                raise SearchError(
                    "抓这一页失败了（" + type(exc).__name__ + "）：" + str(exc)[:140]
                    + "。可能是这台机器连不上网，也可能是那个站点拒绝了连接 —— "
                    "换一条来源试试。"
                ) from exc
    raise SearchError("这个网址跳转太多次了（超过 " + str(MAX_REDIRECTS) + " 跳），不跟了。")


#: 从文档里带出来的可执行东西与纯样式 —— 整块丢掉。
#: （与 `attachments.py` 的 `sanitize_html` 是同一个理由：别人的东西不注进我们的页面。
#: 这里更进一步：**内容也不留**，因为正文里没有它们的位置。）
_DROP_BLOCK = re.compile(
    r"<(script|style|noscript|template|svg|canvas|iframe|form)\b.*?</\1\s*>", re.I | re.S
)
_DROP_SELF = re.compile(
    r"<(script|style|noscript|template|svg|canvas|iframe|form|img|input|link|meta|source"
    r"|video|audio|embed|object)\b[^>]*>",
    re.I,
)
#: 正文之外的常客。整块删掉比"留着让模型自己忽略"便宜得多 ——
#: 一页导航栏能占掉几千字上下文，而它一个字都不是答案。
_DROP_CHROME = re.compile(r"<(nav|footer|header|aside)\b.*?</\1\s*>", re.I | re.S)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_ARTICLE = re.compile(r"<(article|main)\b[^>]*>(.*?)</\1\s*>", re.I | re.S)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.I | re.S)
_BR = re.compile(r"<br\s*/?>", re.I)
_BLOCK = re.compile(
    r"</(p|div|section|article|li|tr|h[1-6]|blockquote|pre|table|ul|ol|dl|dd|dt|figure"
    r"|figcaption|details|summary)\s*>",
    re.I,
)
_TAG = re.compile(r"<[^>]*>")
_SPACES = re.compile(r"[ \t\u00a0\u3000]+")


def html_to_text(source: str) -> str:
    """把一个 HTML 页面压成可读文本。**启发式**：够读就行，不追求"完美正文"。"""
    if not source:
        return ""
    text = _COMMENT.sub(" ", source)
    text = _DROP_BLOCK.sub(" ", text)   # 整块删：脚本、样式、表单
    text = _DROP_CHROME.sub(" ", text)  # 导航 / 页脚 / 侧栏
    text = _DROP_SELF.sub(" ", text)    # 剩下的自闭合标签
    # 有 `<article>` / `<main>` 就只看它 —— 这一步把"正文"和"旁边的推荐位"分开，
    # 是整套启发式里性价比最高的一下。只在它**确实装得下正文**时才用：
    # 有些站点的 `<article>` 只是一个标题壳，那时收窄反而把正文丢了。
    articles = [one[1] for one in _ARTICLE.findall(text)]
    if articles:
        picked = max(articles, key=len)
        if len(_TAG.sub("", picked)) > MIN_GOOD_CHARS:
            text = picked
    text = _BR.sub("\n", text)
    text = _BLOCK.sub("\n", text)       # 块级标签的**结束**换成换行（段落感靠这个）
    text = _TAG.sub("", text)
    text = html_mod.unescape(text)
    text = _SPACES.sub(" ", text)
    lines = [one.strip() for one in text.split("\n")]
    return "\n".join(one for one in lines if one)


def title_of(source: str) -> str:
    match = _TITLE.search(source or "")
    if not match:
        return ""
    return _one_line(match.group(1), 200)


def _declared_charset(raw: bytes, ctype: str) -> str:
    match = re.search(r"charset\s*=\s*[\"']?([\w\-]+)", ctype or "", re.I)
    if match:
        return match.group(1)
    head = raw[:4096].decode("latin-1", "ignore")
    match = re.search(r"charset\s*=\s*[\"']?([\w\-]+)", head, re.I)
    return match.group(1) if match else ""


def _decode(raw: bytes, ctype: str) -> str:
    """按声明解；没声明就猜。"""
    charset = _declared_charset(raw, ctype)
    if charset:
        try:
            return raw.decode(charset, "replace")
        except LookupError:
            pass
    # 没声明就猜：中文站点里 GB18030 还很多。这里必须**严格**解一次才分得出来 ——
    # `errors="replace"` 会把两种编码都解得"成功"，于是乱码被当成正文读进去。
    for name in ("utf-8", "gb18030"):
        try:
            return raw.decode(name)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def read_page(url: str, *, max_chars: int = MAX_PAGE_CHARS) -> dict:
    """抓一个网页，尽力抽出正文。"""
    ctype, final, raw = _fetch(url)
    if ctype and "html" not in ctype and "text" not in ctype and "xml" not in ctype:
        raise SearchError(
            "这个网址不是网页（Content-Type 是 " + ctype[:60] + "），读不了。"
            "如果它是 PDF 之类的东西，让他下载之后用附件功能发给你。"
        )
    body = _decode(raw, ctype)
    text = html_to_text(body)
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    note = "这是**抽出来的正文**（去掉了导航与脚本）。"
    if len(text) < MIN_GOOD_CHARS:
        note = (
            "抽出来的正文很少 —— 这一页很可能是**脚本渲染**的（正文由 JS 生成），"
            "或者正文在图片 / 视频里。别把这片空白当成「这一页没内容」："
            "换个来源，或者直接跟他说这一页读不到。"
        )
    elif truncated:
        note += "太长，只给了前面一段（截到 " + str(max_chars) + " 字）。"
    return {
        "url": str(url),
        "finalUrl": final,
        "title": title_of(body),
        "chars": len(text),
        "truncated": truncated,
        "text": text,
        "note": note,
    }
