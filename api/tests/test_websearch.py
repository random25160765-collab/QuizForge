"""联网搜索：三家的解析、正文抽取、以及那条"不许读内网"的闸。

这些是**契约**测试，不是"接口通不通"：

* 没配密钥时，交回模型的是**一条可操作的话**（去哪儿填），而不是一次异常
* 三家返回的形状差别很大，出口只有一种（`title / url / snippet / site / date`）
* 域名过滤是**本地筛**（三家语法不一样），所以要"多取再筛" ——
  否则"只在这两个站里找"很容易筛成空，看着像"这世上没有"
* 网页 → 文本是**启发式**：`<article>` 优先、脚本导航丢掉、
  但抽得太少时要明说"这一页可能是脚本渲染的"（而不是给一片空白）
* `read_web_page` **不许读本机与内网** —— 网址是模型给的，这是一条真实的攻击面

假服务商用的是本机端口（`127.0.0.1`），而"读网页"那条路**恰好**要挡掉本机地址 ——
所以那个用例 monkeypatch 掉 `_guard_url`，把闸门本身留给专门的用例去验。
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest

from app import settings_store, tools, websearch

#: 测试用的假密钥。**必须含 `test` / `fake` 这类占位词** —— `tools/secret_scan.py`
#: 靠一张占位词表区分"真密钥"与"测试桩"（它自己的注释：真密钥是随机串，不会含这些词）。
#: 顺带说明这不是误报：**看着像真密钥的串本来就不该进仓库**，
#: 所以约定的做法是让测试值一眼可辨（原先写 `super-secret` 就被它拦了）。
_FAKE_KEY = "test-key-not-a-real-secret"


# ------------------------------------------------------------------ 假服务商


class _Handler(BaseHTTPRequestHandler):
    """只回一段 canned 内容，顺便把收到的请求记下来。"""

    body: dict | str = {}
    content_type = "application/json"
    received: dict = {}

    def _record(self) -> None:  # noqa: ANN001
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {"（不是 JSON）": raw[:200].decode("utf-8", "replace")}
        type(self).received = {
            "path": self.path,
            "headers": {key.lower(): value for key, value in self.headers.items()},
            "json": payload,
        }

    def _reply(self) -> None:  # noqa: ANN001
        raw = (
            json.dumps(type(self).body).encode("utf-8")
            if isinstance(type(self).body, dict)
            else str(type(self).body).encode("utf-8")
        )
        self.send_response(200)
        self.send_header("Content-Type", type(self).content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:  # noqa: N802
        self._record()
        self._reply()

    def do_GET(self) -> None:  # noqa: N802
        self._record()
        self._reply()

    def log_message(self, *args) -> None:  # noqa: ANN002
        pass


@contextmanager
def _serve(body, content_type: str = "application/json"):  # noqa: ANN001
    """起一个本机 HTTP 服务，yield (base_url, handler)。"""
    handler = type(
        "_Case", (_Handler,), {"body": body, "content_type": content_type, "received": {}}
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_address[1]) + "/search", handler
    finally:
        server.shutdown()
        server.server_close()


def _conf(endpoint: str, **over) -> dict:  # noqa: ANN001
    return {
        "kind": "bocha",
        "label": "博查（Bocha）",
        "endpoint": endpoint,
        "apiKey": "test-key",
        "count": 6,
        "timeoutMs": 5000,
        "source": "user",
        **over,
    }


def _bocha_body(items: list[dict]) -> dict:
    return {"code": 200, "data": {"webPages": {"value": items}}}


# ------------------------------------------------------------------ 解析


def test_search_normalizes_the_provider_shape():
    """三家的字段名都不一样，出口只有一种 —— 而且请求头要按该家的规矩来。"""
    body = _bocha_body(
        [
            {
                "name": "<em>梯度下降</em>是什么",
                "url": "https://example.com/a",
                "snippet": "一段摘要",
                "siteName": "示例站",
                "datePublished": "2026-01-02",
            }
        ]
    )
    with _serve(body) as (url, handler):
        items = websearch.search(_conf(url), "梯度下降")

    assert items == [
        {
            "title": "梯度下降是什么",          # `<em>` 高亮是给人看的，进了上下文只是噪声
            "url": "https://example.com/a",
            "snippet": "一段摘要",
            "site": "示例站",
            "date": "2026-01-02",
        }
    ]
    assert handler.received["headers"]["authorization"] == "Bearer test-key"
    assert handler.received["json"]["query"] == "梯度下降"
    assert handler.received["json"]["count"] == 6


def test_each_provider_has_its_own_request_shape():
    """Serper 用 `X-API-KEY` 头与 `q`/`num`，Tavily 用 `max_results` —— 别串了。"""
    serper = {"organic": [{"title": "t", "link": "https://a.com/", "snippet": "s"}]}
    with _serve(serper) as (url, handler):
        items = websearch.search(_conf(url, kind="serper"), "q")
    assert items[0]["url"] == "https://a.com/"
    assert handler.received["headers"]["x-api-key"] == "test-key"
    assert handler.received["json"] == {"q": "q", "num": 6}

    tavily = {"results": [{"title": "t", "url": "https://b.com/", "content": "正文摘要"}]}
    with _serve(tavily) as (url, handler):
        items = websearch.search(_conf(url, kind="tavily"), "q")
    assert items[0]["snippet"] == "正文摘要"
    assert handler.received["json"]["max_results"] == 6


def test_a_business_error_is_not_an_empty_result():
    """博查把业务错误放在 200 的 body 里。不看它，"没额度"会表现成"一条都没搜到" ——
    那是最难查的一种坏法：用户以为网上没有，其实是密钥的事。"""
    with _serve({"code": 403, "msg": "额度不足"}) as (url, _h):
        with pytest.raises(websearch.SearchError) as err:
            websearch.search(_conf(url), "x")
    text = str(err.value)
    assert "额度不足" in text
    assert "设置" in text, "要告诉他去哪儿解决"


def test_domain_filter_is_local_and_over_fetches():
    """过滤在本地做（三家语法不同），所以要**多取再筛**。

    只按 `count` 取回那么多条再筛：第一条恰好不在范围内，就会筛成空 ——
    而"筛成空"在模型眼里是"这世上没有"。所以这里把不在范围内的放在**第一条**。
    """
    body = _bocha_body(
        [
            {"name": "b", "url": "https://zhihu.com/question/2"},
            {"name": "a", "url": "https://arxiv.org/abs/1"},
            {"name": "c", "url": "https://docs.python.org/3/"},
        ]
    )
    with _serve(body) as (url, handler):
        items = websearch.search(
            _conf(url), "x", count=2, include_domains=["arxiv.org", "python.org"]
        )
    assert [one["url"] for one in items] == [
        "https://arxiv.org/abs/1",
        "https://docs.python.org/3/",
    ]
    assert handler.received["json"]["count"] > 2, "要的时候多取了几条，不然一筛就空"

    with _serve(body) as (url, _h):
        items = websearch.search(_conf(url), "x", exclude_domains=["zhihu.com"])
    assert [one["url"] for one in items] == [
        "https://arxiv.org/abs/1",
        "https://docs.python.org/3/",
    ]


# ------------------------------------------------------------------ 配置


def _no_beta(monkeypatch) -> None:  # noqa: ANN001
    """把内测通道那份配置按掉 —— 否则开发机上真配了搜索，用例结果就随机器变。"""
    from app import ai_gateway

    monkeypatch.setattr(ai_gateway, "beta_config", lambda: None)


def test_keyless_is_the_default(db_session, monkeypatch):
    """**零配置就能搜** —— 这是刻意的。

    用户的原话："为什么一定要配密钥呢？我们自己实现一个联网搜索的功能不可以吗？"
    所以默认走免密钥那条（必应），密钥是升级不是入场券。
    """
    _no_beta(monkeypatch)
    conf = websearch.resolve(db_session)
    assert conf["kind"] == "bing"
    assert conf["source"] == "keyless"
    assert conf["apiKey"] == ""
    state = websearch.state(db_session)
    assert state["ready"] is True and state["keyless"] is True


def test_switched_off_says_so(db_session, monkeypatch):
    """显式关掉时才真的用不了 —— 那时要说清是"你关的"，别让人以为坏了。"""
    _no_beta(monkeypatch)
    settings_store.put(db_session, search={"enabled": False})
    with pytest.raises(websearch.SearchUnavailable) as err:
        websearch.resolve(db_session)
    assert "关掉" in str(err.value)
    state = websearch.state(db_session)
    assert state["ready"] is False and "关掉" in state["message"]
    # 关着的时候模型也拿得到理由（而不是一条空的失败）
    ok, payload = tools.call(
        db_session, "web_search", {"query": "x"}, {}, mounts={"web"}, allow=("read",)
    )
    assert ok is True and "关掉" in payload["error"]


def test_a_key_still_wins_and_a_broken_choice_falls_back(db_session, monkeypatch):
    """配了密钥（或内测通道给了）就按那个来；选了收费那家却没填密钥 → **回落**到免密钥那条。"""
    from app import ai_gateway

    _no_beta(monkeypatch)
    settings_store.put(db_session, search={"kind": "bocha", "apiKey": "k1"})
    conf = websearch.resolve(db_session)
    assert (conf["kind"], conf["source"], conf["apiKey"]) == ("bocha", "user", "k1")

    # 选了要密钥的一家却没填 → 不是报错，是回落（用户没配就该能用）
    settings_store.put(db_session, search={"kind": "tavily"})
    assert websearch.resolve(db_session)["kind"] == "bing"

    settings_store.put(db_session, search=None)  # 清掉
    monkeypatch.setattr(
        ai_gateway, "beta_config", lambda: {"search": {"kind": "serper", "apiKey": "sk-beta"}}
    )
    conf = websearch.resolve(db_session)
    assert (conf["kind"], conf["source"]) == ("serper", "beta")


def test_the_state_never_returns_the_key(db_session, monkeypatch):
    """设置面板读的那份状态：**不回密钥**。"""
    _no_beta(monkeypatch)
    settings_store.put(db_session, search={"kind": "bocha", "apiKey": _FAKE_KEY})
    state = websearch.state(db_session)
    assert state["ready"] is True and state["keyless"] is False
    assert [one["kind"] for one in state["providers"]][0] == "bing", "下拉的第一个仍是默认那条"
    assert _FAKE_KEY not in json.dumps(state)


def test_a_configured_key_goes_through_the_whole_chain(db_session, monkeypatch):
    """走真链路：设置 → `resolve` → 请求 → 交回模型的那份结果。"""
    _no_beta(monkeypatch)
    body = _bocha_body([{"name": "标题", "url": "https://example.com/x", "snippet": "摘要"}])
    with _serve(body) as (url, _h):
        settings_store.put(
            db_session,
            search={"enabled": True, "kind": "bocha", "apiKey": "k1", "endpoint": url},
        )
        ok, payload = tools.call(
            db_session, "web_search", {"query": "梯度下降"}, {},
            mounts={"web"}, allow=("read",),
        )
    assert ok is True
    assert payload["results"][0]["url"] == "https://example.com/x"
    assert "read_web_page" in payload["note"], "要提醒它：摘要只是索引"


# ------------------------------------------------------------------ 免密钥那条


def test_the_keyless_path_reads_bing_rss():
    """免密钥那条走 `GET ?format=rss`，解析的是一份固定 XML —— **不是搜索结果页的 DOM**。

    这就是"自己实现一个搜索"里唯一站得住的做法：人家的页面版式一改，啃 DOM 的正则
    就悄悄失效；而 `item` 里那几个字段是给机器读的，稳得多。
    """
    xml = (
        '<?xml version="1.0" encoding="utf-8" ?><rss version="2.0"><channel>'
        "<item><title>结果一</title><link>https://example.com/a</link>"
        "<description>摘要一</description><pubDate>周五, 18 9月 2026 00:02:00 GMT</pubDate></item>"
        # 它偶尔把"再搜一次这个词"本身也当成一条结果（link 指回它自己）—— 那不是结果
        "<item><title>再搜一次</title><link>http://www.bing.com/search?q=x</link>"
        "<description></description></item>"
        "<item><title>结果二</title><link>https://example.com/b</link>"
        "<description>摘要二</description></item>"
        "</channel></rss>"
    )
    with _serve(xml, content_type="text/xml; charset=utf-8") as (url, handler):
        items = websearch.search(_conf(url, kind="bing", apiKey=""), "x")

    assert [one["url"] for one in items] == ["https://example.com/a", "https://example.com/b"]
    assert items[0]["title"] == "结果一"
    assert items[0]["snippet"] == "摘要一"
    assert items[0]["site"] == "example.com"
    # 走的是 GET 那条路，并且**要了 RSS 输出**（否则拿回来的是一整页 DOM）
    assert handler.received["path"].startswith("/search?")
    assert "format=rss" in handler.received["path"], handler.received["path"]


def test_a_blocked_keyless_response_is_not_an_empty_result():
    """被限流时它回的是一页 HTML —— 必须与"没搜到"**分开说**。

    都报"没搜到"的话，用户会一直换搜索词；真正该做的是等一会儿或者换一家。
    """
    with _serve("<html><body>请验证你是人类</body></html>", content_type="text/html") as (url, _h):
        with pytest.raises(websearch.SearchError) as err:
            websearch.search(_conf(url, kind="bing", apiKey=""), "x")
    text = str(err.value)
    assert "HTML" in text, text
    assert "换一家" in text, "要给出下一步"


def test_an_rss_without_items_is_genuinely_empty():
    """根元素是 `rss`、里面一个 item 都没有 = **真的**没搜到：不报错，交回空列表。

    与上一条配对：一个说"被挡了"，一个说"没有"。这两种情况的下一步动作不同，
    所以判据落在**根元素**上，而不是"有没有 item" ——
    一页 HTML 有时恰好是合法 XML（上一条就是这么来的）。
    """
    xml = '<?xml version="1.0" encoding="utf-8" ?><rss version="2.0"><channel></channel></rss>'
    with _serve(xml, content_type="text/xml") as (url, _h):
        assert websearch.search(_conf(url, kind="bing", apiKey=""), "x") == []


# ------------------------------------------------------------------ 网页 → 文本


def test_html_to_text_prefers_the_article_and_drops_the_chrome():
    page = (
        "<html><head><title>标题 A</title>"
        "<style>.x{color:red}</style><script>alert(1)</script></head><body>"
        "<nav><a href='/'>首页</a> 菜单一 菜单二</nav>"
        "<div class='sidebar'>推荐阅读：别的东西</div>"
        "<article><h1>正文标题</h1>"
        + "".join("<p>第 %d 段正文，够长才看得出这是正文。</p>" % i for i in range(12))
        + "<p>最后一段 &amp; 符号</p></article>"
        "<footer>版权所有</footer></body></html>"
    )
    text = websearch.html_to_text(page)
    assert "正文标题" in text and "第 3 段正文" in text
    assert "最后一段 & 符号" in text, "实体要解开"
    assert "\n" in text, "段落之间要有换行"
    for junk in ("菜单一", "推荐阅读", "版权所有", "alert", "color:red"):
        assert junk not in text, junk
    assert websearch.title_of(page) == "标题 A"


def test_a_tiny_article_shell_does_not_swallow_the_page():
    """有些站点的 `<article>` 只是个标题壳 —— 那时**不能**收窄，
    否则正文全在壳外面，被当成"这一页没内容"。"""
    page = (
        "<article><h1>一个壳</h1></article>"
        "<div><p>" + "真正的正文在这里。" * 40 + "</p></div>"
    )
    assert "真正的正文在这里" in websearch.html_to_text(page)


# ------------------------------------------------------------------ 出网那道闸


def test_read_page_refuses_local_and_inner_addresses():
    """网址是**模型**给的，它会从搜索结果甚至网页正文里捡 URL。

    一个公网网址 302 到内网也算 —— 所以 `_fetch` 是手动跟重定向、每一跳都过这道闸。
    """
    for url in (
        "http://127.0.0.1:8100/chat.html",
        "http://localhost:8100/",
        "http://10.0.0.5/",
        "http://169.254.169.254/latest/meta-data/",   # 云上的元数据服务
        "file:///etc/passwd",
        "javascript:alert(1)",
    ):
        with pytest.raises(websearch.SearchError) as err:
            websearch.read_page(url)
        assert "读" in str(err.value), url


def test_read_page_extracts_the_text(monkeypatch):
    html_body = (
        "<html><head><title>页面标题</title></head><body>"
        "<article>" + "".join("<p>正文段落 %d。</p>" % i for i in range(40)) + "</article>"
        "</body></html>"
    )
    with _serve(html_body, content_type="text/html; charset=utf-8") as (url, _h):
        # 假服务商就在本机 —— 那条闸留给上一个用例验，这里只验"抽得对不对"
        monkeypatch.setattr(websearch, "_guard_url", lambda raw: urlparse(str(raw)))
        page = websearch.read_page(url)

    assert page["title"] == "页面标题"
    assert "正文段落 5。" in page["text"]
    assert page["truncated"] is False
    assert "抽出来的正文" in page["note"]


def test_a_script_rendered_page_says_so(monkeypatch):
    """抽不出正文时要**明说**，而不是给一片空白让模型以为这页本来就是空的。"""
    with _serve("<html><body><div id='app'></div><script>render()</script></body></html>",
                content_type="text/html") as (url, _h):
        monkeypatch.setattr(websearch, "_guard_url", lambda raw: urlparse(str(raw)))
        page = websearch.read_page(url)

    assert page["chars"] < websearch.MIN_GOOD_CHARS
    assert "脚本渲染" in page["note"]


def test_read_page_refuses_something_that_is_not_a_page(monkeypatch):
    with _serve("%PDF-1.7 ...", content_type="application/pdf") as (url, _h):
        monkeypatch.setattr(websearch, "_guard_url", lambda raw: urlparse(str(raw)))
        with pytest.raises(websearch.SearchError) as err:
            websearch.read_page(url)
    assert "application/pdf" in str(err.value)


# ------------------------------------------------------------------ 整条链


def test_the_agent_loop_declares_it_and_feeds_the_result_back(db_session, monkeypatch):
    """挂上这一组之后：两个工具**真被声明**给模型，模型调了之后结果也**真回到它手里**。

    一组新工具最容易坏的地方不是它自己，而是**接线**：有没有被挂上、结果会不会在
    "喂回模型"那一层（`output_text`）被弄坏。所以这里跑**真循环**，只有网关是假的
    （按轮次发工具调用 —— 与 `test_agent_wait.py` 同一套做法）。
    """
    from app import agent_loop

    _no_beta(monkeypatch)
    page = (
        "<html><head><title>页面</title></head><body><article>"
        + "".join("<p>正文段落 %d。</p>" % i for i in range(40))
        + "</article></body></html>"
    )
    # 走**默认那条（免密钥）**：这才是用户实际会跑的路
    found = (
        '<?xml version="1.0" encoding="utf-8" ?><rss version="2.0"><channel>'
        "<item><title>梯度下降</title><link>https://example.com/gd</link>"
        "<description>一句摘要</description></item>"
        "</channel></rss>"
    )
    with _serve(found, content_type="text/xml; charset=utf-8") as (search_url, _h1):
        with _serve(page, content_type="text/html; charset=utf-8") as (page_url, _h2):
            settings_store.put(
                db_session,
                search={"kind": "bing", "endpoint": search_url},  # 没有 apiKey
            )
            # 假服务商就在本机，而那条闸专门挡本机 —— 它有自己的用例（见上），
            # 这里要验的是"循环接没接上"。
            monkeypatch.setattr(websearch, "_guard_url", lambda raw: urlparse(str(raw)))
            seen: dict = {}

            def fake_stream(conf, messages, tools=None, params=None):  # noqa: A002
                seen["declared"] = [item["function"]["name"] for item in (tools or [])]
                results = [one for one in messages if one.get("role") == "tool"]
                seen["results"] = results
                if not results:
                    yield ("tool_calls", [{"id": "c1", "name": "web_search",
                                           "arguments": '{"query": "梯度下降"}'}])
                    yield ("finish", "tool_calls")
                    return
                if len(results) == 1:
                    yield ("tool_calls", [{"id": "c2", "name": "read_web_page",
                                           "arguments": json.dumps({"url": page_url})}])
                    yield ("finish", "tool_calls")
                    return
                yield ("delta", "看完了。")
                yield ("finish", "stop")

            monkeypatch.setattr(agent_loop.gateway, "stream_completion", fake_stream)
            events = list(
                agent_loop.run(
                    db_session,
                    {"model": "fake"},
                    system="s",
                    history=[{"role": "user", "content": "帮我查一下梯度下降"}],
                    tools=tools,
                    tool_context={},
                    mounts={"web"},
                    allow=None,
                )
            )

    assert {"web_search", "read_web_page"} <= set(seen["declared"]), seen["declared"]
    ends = [one for one in events if one["kind"] == "tool_result"]
    assert [one["name"] for one in ends] == ["web_search", "read_web_page"], ends
    assert all(one["ok"] for one in ends), ends
    # 喂回模型的那两条：搜到的网址在第一条里，读到的正文在第二条里
    assert "https://example.com/gd" in seen["results"][0]["content"]
    assert "正文段落 5。" in seen["results"][1]["content"]


# ------------------------------------------------------------------ 不许卡住
#
# 用户的原话："联网这一块是堵塞重灾区 —— 如果连不上网，务必不要让它卡住。"
#
# 联网是唯一会去碰"不属于这台机器的东西"的一层，所以这里的每一条路都要有一个
# **看得见的墙钟上限**。三种堵法各有一个用例，都是本机假服务、都断言"花了多久"：
#
#   1. 接受连接、什么都不回（离线 / DNS 黑洞 / 丢包 —— 最常见的那种"卡住"）
#   2. 一直吐字节、永远不结束（**按间隔计的超时拦不住它**，只有总预算能）
#   3. DNS 解析卡住（`getaddrinfo` 没有超时参数，得自己套一层）


class _Silent(BaseHTTPRequestHandler):
    """收下请求，然后**什么都不回**（连接不关）。"""

    def do_GET(self) -> None:  # noqa: N802
        time.sleep(30)

    do_POST = do_GET

    def log_message(self, *args) -> None:  # noqa: ANN002
        pass


class _Drip(BaseHTTPRequestHandler):
    """一直吐字节、**永远不结束**，而且每次都远在超时以内。

    这条专门打"超时按间隔计"那个洞：httpx 的 read 超时是两次读之间的上限，
    对方每 20ms 吐十来个字节，就永远不触发它，而连接一直"有响应"。
    能拦住它的只有**总预算** —— 也就是这次加的那条墙钟。
    """

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", "10000000")  # 声称很大，好让"字节上限"不背锅
        self.end_headers()
        try:
            while True:
                self.wfile.write(b"<p>x</p>")
                self.wfile.flush()
                time.sleep(0.02)
        except OSError:
            pass

    do_POST = do_GET

    def log_message(self, *args) -> None:  # noqa: ANN002
        pass


@contextmanager
def _hang_server(handler_cls):
    """起一个"坏掉的"服务（不回 / 一直吐），yield 它的地址。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield "http://127.0.0.1:" + str(server.server_address[1]) + "/x"
    finally:
        server.shutdown()
        server.server_close()


def _bypass_guard(monkeypatch) -> None:  # noqa: ANN001
    """假服务在本机，而那条闸专门挡本机 —— 它有自己的用例，这里只验"卡不卡"。"""
    monkeypatch.setattr(websearch, "_guard_url", lambda raw: urlparse(str(raw)))


def test_a_search_that_never_answers_gives_up_quickly():
    """对方不响应就得**早点认输**，不能挂着。"""
    with _hang_server(_Silent) as url:
        started = time.perf_counter()
        with pytest.raises(websearch.SearchError) as err:
            websearch.search(_conf(url, timeoutMs=1200), "x")
        took = time.perf_counter() - started

    assert took < 6.0, "挂了 " + str(round(took, 1)) + " 秒：这条路上没有墙钟"
    assert "没有响应" in str(err.value) or "连不上" in str(err.value), str(err.value)


def test_reading_a_page_that_never_answers_gives_up(monkeypatch):
    monkeypatch.setattr(websearch, "PAGE_TOTAL_S", 1.5)
    _bypass_guard(monkeypatch)
    with _hang_server(_Silent) as url:
        started = time.perf_counter()
        with pytest.raises(websearch.SearchError) as err:
            websearch.read_page(url)
        took = time.perf_counter() - started

    assert took < 6.0, "挂了 " + str(round(took, 1)) + " 秒"
    text = str(err.value)
    assert "超时" in text or "太慢" in text, text


def test_a_page_that_drips_forever_is_cut_off_by_the_clock(monkeypatch):
    """**一次吐一点、永远不结束** —— 只有总预算拦得住（见 `_Drip` 的说明）。"""
    monkeypatch.setattr(websearch, "PAGE_TOTAL_S", 2.0)
    _bypass_guard(monkeypatch)
    with _hang_server(_Drip) as url:
        started = time.perf_counter()
        with pytest.raises(websearch.SearchError) as err:
            websearch.read_page(url)
        took = time.perf_counter() - started

    assert took < 6.0, "挂了 " + str(round(took, 1)) + " 秒：字节一直在来，只有墙钟能停它"
    text = str(err.value)
    assert "太慢" in text or "超时" in text, text


def test_a_search_that_drips_forever_is_cut_off_too(monkeypatch):
    """搜索那一侧也一样（它也走"总预算"，不是 `follow_redirects` 那种每跳一份）。"""
    with _hang_server(_Drip) as url:
        started = time.perf_counter()
        with pytest.raises(websearch.SearchError):
            websearch.search(_conf(url, timeoutMs=1500), "x")
        took = time.perf_counter() - started

    assert took < 6.0, "挂了 " + str(round(took, 1)) + " 秒"


def test_dns_resolution_has_a_wall_clock(monkeypatch):
    """`socket.getaddrinfo` **没有超时参数**：网络不通时它自己会重试十几秒，
    而"连不上网"正是最先走到这里的时候。所以外面套了一层墙钟。
    """
    monkeypatch.setattr(websearch, "DNS_TIMEOUT_S", 0.5)

    def hang(*_args, **_kwargs):  # noqa: ANN002, ANN003
        time.sleep(30)
        return []

    monkeypatch.setattr(websearch.socket, "getaddrinfo", hang)
    started = time.perf_counter()
    with pytest.raises(websearch.SearchError) as err:
        websearch.read_page("http://example.com/")
    took = time.perf_counter() - started

    assert took < 3.0, "挂了 " + str(round(took, 1)) + " 秒：DNS 那一层没有墙钟"
    assert "解析超时" in str(err.value), str(err.value)


# ------------------------------------------------------------------ 学术通道
#
# 这一路的理由见 `websearch.py` 模块 docstring 那张表：通用网页索引搜学术关键词
# 是**结构性做不到**（缩写撞名），而它失败的样子是"有结果的搜索" ——
# 有标题、有摘要、不报错，从"有没有结果"根本看不出错了。
#
# 这里钉四件事：出口形状与网页那条**一样**、摘要能从倒排**还原**、
# 开放获取链接要**优先给**、以及**筛空不等于没搜到**。


def _openalex_body(items: list[dict]) -> dict:
    return {"meta": {"count": len(items)}, "results": items}


def _paper(**over) -> dict:  # noqa: ANN003
    base = {
        "id": "https://openalex.org/W1",
        "doi": "https://doi.org/10.1000/xyz",
        "title": "Cache behavior prediction by abstract interpretation",
        "publication_year": 1996,
        "cited_by_count": 172,
        "type": "conference-paper",
        "authorships": [
            {"author": {"display_name": "Christian Ferdinand"}},
            {"author": {"display_name": "Florian Martin"}},
        ],
        "primary_location": {"source": {"display_name": "Lecture Notes in CS"}},
        "best_oa_location": {},
        "abstract_inverted_index": None,
    }
    base.update(over)
    return base


def test_the_scholar_channel_normalizes_papers(monkeypatch):  # noqa: ANN001
    """论文走**同一个出口形状** —— 字段名一个不变，只是 `snippet` 变成了
    「书目信息 + 摘要」。调用方不必认识两种结果。"""
    with _serve(_openalex_body([_paper()])) as (url, handler):
        monkeypatch.setattr(websearch, "SCHOLAR_ENDPOINT", url)
        items = websearch.search_papers(_conf(url), "cache analysis abstract interpretation")

    assert len(items) == 1
    one = items[0]
    assert set(one) == {"title", "url", "snippet", "site", "date"}, "出口形状不能变"
    assert one["title"] == "Cache behavior prediction by abstract interpretation"
    assert one["date"] == "1996"
    assert one["site"] == "Lecture Notes in CS"
    assert "Christian Ferdinand" in one["snippet"]
    assert "被引 172" in one["snippet"]
    # 请求形状：相关度检索（不是字面匹配）+ 只要显式列出的那几个字段
    assert "search=cache" in handler.received["path"]
    assert "select=" in handler.received["path"], "不列字段会拉回好几 MB"


def test_the_abstract_is_rebuilt_from_the_inverted_index(monkeypatch):  # noqa: ANN001
    """OpenAlex 给的是**倒排**（`{词: [位置, …]}`）—— 不还原，等于没有摘要，
    而摘要是判断一篇文献要不要读的唯一依据。"""
    with _serve(
        _openalex_body(
            [_paper(abstract_inverted_index={"Caches": [0], "bridge": [1], "the": [2], "gap": [3]})]
        )
    ) as (url, _handler):
        monkeypatch.setattr(websearch, "SCHOLAR_ENDPOINT", url)
        items = websearch.search_papers(_conf(url), "x")

    assert items[0]["snippet"].endswith("Caches bridge the gap")


def test_a_paper_without_an_abstract_does_not_get_a_fake_one(monkeypatch):  # noqa: ANN001
    """实测有一部分作品**根本没有摘要**（`abstract_inverted_index` 缺席）——
    那就留空，**不编**。"""
    with _serve(_openalex_body([_paper(abstract_inverted_index=None)])) as (url, _handler):
        monkeypatch.setattr(websearch, "SCHOLAR_ENDPOINT", url)
        items = websearch.search_papers(_conf(url), "x")

    assert "被引 172" in items[0]["snippet"], "书目信息该在"
    assert "None" not in items[0]["snippet"]


def test_the_open_access_pdf_wins_over_the_doi(monkeypatch):  # noqa: ANN001
    """有免费 PDF 就优先给那个：DOI 点进去常常是付费墙，而 `read_web_page`
    读不到正文时，模型会以为「这篇没内容」。"""
    with _serve(
        _openalex_body([_paper(best_oa_location={"pdf_url": "https://x.test/a.pdf"})])
    ) as (url, _handler):
        monkeypatch.setattr(websearch, "SCHOLAR_ENDPOINT", url)
        items = websearch.search_papers(_conf(url), "x")

    assert items[0]["url"] == "https://x.test/a.pdf"


def test_the_venue_falls_back_to_raw_source_name(monkeypatch):  # noqa: ANN001
    """实测不少 IEEE / ACM 记录的 `primary_location.source` 是 `null`，
    而旁边那个 `raw_source_name` 有值 —— 只用前一个会把「发表在哪」整列丢空，
    而那正是判断一篇文献可不可信的主要依据之一。"""
    with _serve(
        _openalex_body(
            [_paper(primary_location={"source": None, "raw_source_name": "2011 17th IEEE RTAS"})]
        )
    ) as (url, _handler):
        monkeypatch.setattr(websearch, "SCHOLAR_ENDPOINT", url)
        items = websearch.search_papers(_conf(url), "x")

    assert items[0]["site"] == "2011 17th IEEE RTAS"


# ------------------------------------------------- 筛空 ≠ 没搜到（这条是实测踩的）


def test_a_filter_that_empties_the_results_hands_the_tally_back():
    """域名过滤是**本地筛**，所以「零结果」有两种完全不同的成因 ——
    而 `stats` 就是把这两种分开的那个数。"""
    body = _bocha_body([{"name": "别的站", "url": "https://other.test/a", "snippet": "x"}])
    with _serve(body) as (url, _handler):
        stats: dict = {}
        items = websearch.search(_conf(url), "x", include_domains=["arxiv.org"], stats=stats)

    assert items == []
    assert stats == {"raw": 1, "kept": 0, "dropped": 1}


def test_the_tool_says_filtered_instead_of_not_found(db_session, monkeypatch):  # noqa: ANN001
    """**筛空要说成筛空。**

    实测踩到过：限定 arxiv / ACM / IEEE / Springer 之后拿回「零结果」，
    而实际是拿回的那几条**一条都不在**这些站上 —— 模型于是得出了
    「这世上没有」的结论。这两种情况的下一步动作完全不同（去掉域名限制 vs 换词），
    所以工具必须把话说清。
    """
    _no_beta(monkeypatch)
    body = _bocha_body([{"name": "别的站", "url": "https://other.test/a", "snippet": "x"}])
    with _serve(body) as (url, _h):
        settings_store.put(
            db_session,
            search={"enabled": True, "kind": "bocha", "apiKey": _FAKE_KEY, "endpoint": url},
        )
        ok, payload = tools.call(
            db_session, "web_search",
            {"query": "WCET", "includeDomains": ["arxiv.org", "dl.acm.org"]},
            {}, mounts={"web"}, allow=("read",),
        )
    assert ok is True, payload
    assert payload["results"] == []
    assert "筛掉" in payload["note"], payload["note"]
    assert "includeDomains" in payload["note"], "要给出下一步动作"


def test_the_paper_tool_is_declared_and_points_at_reading(db_session, monkeypatch):  # noqa: ANN001
    """走真链路：工具声明得到、学术通道解析得对、note 指向「摘要不是全文」。"""
    _no_beta(monkeypatch)
    with _serve(_openalex_body([_paper()])) as (url, _h):
        monkeypatch.setattr(websearch, "SCHOLAR_ENDPOINT", url)
        ok, payload = tools.call(
            db_session, "search_papers",
            {"query": "cache analysis abstract interpretation"},
            {}, mounts={"web"}, allow=("read",),
        )

    assert ok is True, payload
    assert payload["results"][0]["title"].startswith("Cache behavior prediction")
    assert "read_web_page" in payload["note"], "摘要不是全文，要指向读原文"
