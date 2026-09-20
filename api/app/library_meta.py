"""用模型判一份资料的元数据（标题 / 作者 / 年份 / 类型 / 主题）。

## 为什么整件事交给模型

用户原话："你现在都是自己写模式匹配，对吧？可是文件千奇百怪呢。丢进去一份文件，
llm 自动完成处理。" —— 这一层因此**没有规则兜底**。

这几周在 `library.py` 里攒下的判据就是理由本身：目录行（点引线 / 编号开头 / 页码尾巴）、
没有逗号的署名行、地名、机构词、出版社、地址后缀、虚词结尾、网址不能当标题……
十几条，每一条都是被**某一份**文件打脸之后补的，而下一份文件又会打脸
（PDF 第一页可以是封面、封面 + 目录、版权页、arXiv 戳、双栏错行、中文封面）。
模式匹配在这里是在跟无穷多种排版较劲；模型天生干这个。

## 判不出来就留空

`MetaFailed` 一律往上抛，**不猜**。理由是元数据的下游很重：引用格式、检索、
去重、笔记里的 `@` 引用都挂在它上面 —— 一个错的作者比一个空的作者坏得多
（空的界面显示"待补"，人会去补；错的没人会怀疑）。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from . import ai_gateway as gateway

#: 送给模型的文字上限。第一页的信息量就够定标题与作者，再多只是烧 token。
MAX_CHARS = 6000

#: 元数据"由哪一版管线写的"。**判据是它，不是 `origin`。**
#:
#: 踩过：库里 55 条元数据的 `origin` 早就写着 `llm`，而里面的标题是"目录页 + 一串人名"
#: —— 那一版的 `llm` 名不副实（值是老的规则法写的，标签却是 llm）。所以"这条判过没有"
#: 不能信 `origin`（它会撒谎，而且撒得理直气壮），要信一个**只有真调过模型的代码才写**
#: 的版本号。人手工改的（`origin: manual`）永远不动。
META_REV = 2

#: 允许的类型（与 `library.py` 的 KIND_LABELS 对齐；模型给了别的就算 other）
KINDS = ("paper", "book", "manual", "doc", "code", "slides", "notebook", "report", "other")

#: 字段长度上限：模型偶尔会把整段摘要塞进标题，截断比截断前好
MAX_TITLE = 200
MAX_AUTHOR = 60
MAX_AUTHORS = 12
MAX_TOPIC = 32
MAX_TOPICS = 6

SYSTEM = (
    "你是一个文献编目助手。用户给你一份学习资料的开头文字（从文件里机械抽取的，"
    "可能包含页眉页脚、目录、版权页、错行、乱码）。你的任务是判断这份资料的元数据。\n"
    "只输出一个 JSON 对象，不要输出解释、不要用 Markdown 代码块。字段如下：\n"
    '{"title": "书名或篇名", "authors": ["作者姓名"], "year": 2024, '
    '"kind": "paper|book|manual|doc|code|slides|notebook|report|other", '
    '"topics": ["主题词"]}\n'
    "规则：\n"
    "1. `title` 只写**标题本身** —— 不要把作者、机构、页码、目录条目、期刊名带上；"
    "标题在原文里被换行拆开就接成一句。\n"
    "2. `authors` 只写**人**：机构、院系、公司、出版社、会议名不算作者。"
    "认不出就给空数组，**不要编**。\n"
    "3. `year` 是四位年份；认不出给 0。不要拿版权年、页码、编号当年份。\n"
    "4. `topics` 给 2~4 个短的名词性主题词，用于检索；不要照抄标题。\n"
    "5. 认不出来的字段就给空值（\"\" / [] / 0），**宁可空着也不要猜**。\n"
    "6. 例外：如果文字部分**不可用**（空白、乱码、只有页眉），那就根据**文件名**"
    "给一个干净的标题（作者与年份仍留空）—— 文件名是可靠的线索，"
    "而一个按文件名起的标题比空标题有用。\n"
)


class MetaFailed(Exception):
    """这次判定没成。`message` 是给用户看的一句人话（可操作）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def build_messages(item: Any, head_text: str) -> list[dict[str, str]]:
    """组这次要发的话。文件名也带上 —— 它常常是模型最可靠的一条线索。"""
    name = str(getattr(item, "rel", "") or getattr(item, "path", "") or "")
    head = (head_text or "")[:MAX_CHARS]
    body = (
        "文件名：" + name + "\n\n"
        "--- 文字开始 ---\n" + head + "\n--- 文字结束 ---\n\n"
        "请输出 JSON。"
    )
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": body},
    ]


def parse_reply(text: str) -> dict[str, Any]:
    """从模型的回复里把 JSON 抠出来并清洗成元数据。

    三种回复都见过，所以不能直接 `json.loads`：
      * 干净的 JSON；
      * 包在 ``` 围栏里（明明说了不要用）；
      * 前面带一句"好的，这是结果："再跟 JSON。
    取**第一个 `{` 到最后一个 `}`**，这一段之外的内容一律丢掉。
    """
    raw = (text or "").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise MetaFailed("模型没有给出 JSON：" + raw[:120])
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError as exc:
        raise MetaFailed("模型给的 JSON 读不动：" + str(exc)[:80]) from exc
    if not isinstance(data, dict):
        raise MetaFailed("模型给的不是一个对象")
    return sanitize(data)


def sanitize(data: dict[str, Any]) -> dict[str, Any]:
    """清洗：长度、类型、去重。**只做机械清洗，不替模型编内容。**"""

    def one_line(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    title = one_line(data.get("title"))[:MAX_TITLE]

    authors: list[str] = []
    raw_authors = data.get("authors")
    if isinstance(raw_authors, str):
        raw_authors = re.split(r"[,;、]| and ", raw_authors)
    if isinstance(raw_authors, list):
        for one in raw_authors:
            name = one_line(one)[:MAX_AUTHOR]
            if name and name not in authors:
                authors.append(name)
    authors = authors[:MAX_AUTHORS]

    year = 0
    try:
        year = int(str(data.get("year") or 0).strip()[:4])
    except ValueError:
        year = 0
    if not (1000 <= year <= 2999):
        year = 0

    kind = one_line(data.get("kind")).lower()
    if kind not in KINDS:
        kind = "other"

    topics: list[str] = []
    raw_topics = data.get("topics")
    if isinstance(raw_topics, str):
        raw_topics = re.split(r"[,;、]", raw_topics)
    if isinstance(raw_topics, list):
        for one in raw_topics:
            tag = one_line(one)[:MAX_TOPIC]
            if tag and tag not in topics:
                topics.append(tag)
    topics = topics[:MAX_TOPICS]

    return {"title": title, "authors": authors, "year": year, "kind": kind, "topics": topics}


def infer(
    item: Any,
    head_text: str,
    conf: dict[str, Any],
    *,
    timeout_s: float | None = None,
    fallback_title: str = "",
) -> dict[str, Any]:
    """问一次模型，拿回清洗过的元数据。失败一律抛 `MetaFailed`（**不猜**）。

    用**非流式**的一次性调用：这一问的答案是一小段 JSON，流式没有意义
    （`ai_gateway` 的模块注释说过，非流式的一次性调用不放在网关里）。
    """
    payload: dict[str, Any] = {
        "model": conf["model"],
        "messages": build_messages(item, head_text),
        "temperature": 0,
        "max_tokens": 500,
        "stream": False,
    }
    if conf.get("jsonMode"):
        # 上游支持就要求它**只吐 JSON**。没有这一句也能用（`parse_reply` 会剥围栏、剥前言），
        # 但那是在跟模型的措辞较劲 —— 能不较就不较。
        payload["response_format"] = {"type": "json_object"}
    timeout = float(timeout_s or min(int(conf.get("timeoutMs") or 60000) / 1000, JUDGE_TIMEOUT_S))
    # 并发之后失败的主因是**网络抖动与限流**（实测全库那一轮：19 成 27 败，全是 timed out）。
    # 这种失败重发一次多半就好，而"重试"这件事只有在批量的语境下才划算 ——
    # 单条重试两次不会让人等更久，批量却能把一整轮的失败率压下来。
    last = ""
    for attempt in range(RETRIES + 1):
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(
                    gateway.completion_endpoint(conf["baseUrl"]),
                    json=payload,
                    headers={
                        "Authorization": "Bearer " + str(conf["apiKey"]),
                        "Content-Type": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            last = gateway.connection_hint(conf) + "（" + str(exc)[:120] + "）"
            if attempt < RETRIES:
                time.sleep(RETRY_WAIT_S * (attempt + 1))
                continue
            raise MetaFailed(last) from exc

        # 429 与 5xx 值得重试（限流 / 上游打嗝）；4xx 是**我们发错了**，重发没意义
        if response.status_code >= 400:
            last = "模型返回 HTTP " + str(response.status_code) + "：" + response.text[:160]
            if response.status_code in RETRYABLE_STATUS and attempt < RETRIES:
                time.sleep(RETRY_WAIT_S * (attempt + 1))
                continue
            raise MetaFailed(last)
        break

    try:
        data = response.json()
    except ValueError as exc:
        raise MetaFailed("模型的响应不是 JSON：" + response.text[:120]) from exc

    choices = data.get("choices") or [{}]
    content = ((choices[0].get("message") or {}).get("content")) or ""
    meta = parse_reply(content)
    if not meta.get("title"):
        # 抽不出正文的条目（扫描件 / 乱码 / 只有页眉）走这里：模型按提示词如实留空。
        # 这时**不抛错**，用调用方给的"按文件名起的标题"兜住 —— 否则这几条会被
        # 每一轮都重判一遍、又每一轮都失败（实测 4 条一直卡在这句上）。
        if not (fallback_title or "").strip():
            raise MetaFailed("模型没认出标题（也没有可用的文件名）")
        meta["title"] = fallback_title.strip()
        meta["fromFilename"] = True
    return meta


#: 并发上限。判定之间互不依赖，是最适合并发的形状；但再高就是拿上游的限流换速度
#: （真被限流，整批反而更慢），所以给一个保守的默认，需要时由调用方调。
DEFAULT_WORKERS = 6

#: 单条判定的超时上限（秒）。
#:
#: 踩过：这里原来跟着用户的 `timeoutMs` 走（配置里是 90 秒）—— 只要有一条请求
#: 卡住，整个批次就挂在那一条上，界面上什么都没有（用户："卡死了，你的并发真的
#: 有并发吗？"）。并发的意义是**快**，那就不能让一条把大家按住。
#:
#: 15 秒是用户定的调子（"网络故障不要等太长时间"）：正常一次判定 1 秒左右，
#: 15 秒没回来就不是"慢"，是这条不通了 —— 记成失败、判下一条更划算。
JUDGE_TIMEOUT_S = 15.0

#: 失败后重发几次、每次等多久（秒）。429 与 5xx 才重试 —— 4xx 是我们发错了。
#: 重试也一样：宁可少重试一次，也不要让人盯着一批失败等下去。
RETRIES = 1
RETRY_WAIT_S = 1.0
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def stem_title(item: Any) -> str:
    """文件名派生的人话标题（`（规格书）DS5319 stm32f10x…` → `DS5319 stm32f10x…`）。

    只做机械整理，不是"猜内容"：抽不出正文时它是**唯一**站得住的信息。
    """
    import re as _re  # noqa: PLC0415 —— 只在这一个辅助函数里用

    stem = str(getattr(item, "stem", "") or "")
    text = _re.sub(r"[_\-]+", " ", stem)
    return _re.sub(r"\s+", " ", text).strip()


def judge_many(
    jobs: list[tuple[Any, str]],
    conf: dict[str, Any],
    *,
    workers: int = DEFAULT_WORKERS,
    on_done: Any = None,
) -> list[tuple[Any, dict[str, Any] | None, str]]:
    """并发判一批：`jobs` 是 `[(item, head_text)]`，返回 `[(item, meta, why)]`。

    **顺序与输入一致**，结果里带上失败原因 —— "哪几条没判成、为什么"与"判成了什么"
    一样要看得见。

    `on_done(完成数, 总数, (item, meta, why))` 在**每一条完成时立刻**回调：
    调用方靠它画进度。原先用 `executor.map` 把结果攒到最后一起返回，表现是
    "屏幕上一动不动地等十几秒" —— 用户看到的就是卡死（用户："卡死了，你的并发
    真的有并发吗？做个进度条吧"）。并发是对的，**看不见**是错的。

    为什么要并发（用户原话："对于大库你要加并发，提升性能"）：一次判定是一次网络
    往返，实测一秒左右一条；串行跑几百条就是几分钟。
    """
    if not jobs:
        return []
    total = len(jobs)
    width = max(1, min(int(workers or DEFAULT_WORKERS), 16))

    def one(job: tuple[Any, str]) -> tuple[Any, dict[str, Any] | None, str]:
        item, head = job
        try:
            # 没有正文的条目也要能收口：标题退到文件名（否则每轮都重判、每轮都失败）
            return (item, infer(item, head, conf, fallback_title=stem_title(item)), "")
        except MetaFailed as exc:
            return (item, None, exc.message)

    out: list[tuple[Any, dict[str, Any] | None, str]] = [
        (item, None, "未判定") for item, _head in jobs
    ]
    if width == 1 or total == 1:
        for index, job in enumerate(jobs):
            out[index] = one(job)
            if on_done:
                on_done(index + 1, total, out[index])
        return out

    from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: PLC0415

    with ThreadPoolExecutor(max_workers=width, thread_name_prefix="libmeta") as pool:
        pending = {pool.submit(one, job): index for index, job in enumerate(jobs)}
        done_count = 0
        for future in as_completed(pending):
            out[pending[future]] = future.result()
            done_count += 1
            if on_done:
                on_done(done_count, total, out[pending[future]])
    return out


def looks_unjudged(meta: dict[str, Any]) -> bool:
    """这条元数据是不是"还没让当前这版管线判过"。

    **先看人手工改过的没有**（那种永远不动），再看版本号：
    `metaRev` 只由真正问过模型的代码写。不信 `origin` —— 老数据里它写着 `llm`，
    内容却是规则法留下的"目录页 + 一串人名"（见 `META_REV` 上面那段）。
    """
    if str(meta.get("origin") or "") == "manual":
        return False
    try:
        rev = int(meta.get("metaRev") or 0)
    except (TypeError, ValueError):
        rev = 0
    return rev < META_REV


__all__ = [
    "DEFAULT_WORKERS",
    "KINDS",
    "META_REV",
    "MetaFailed",
    "build_messages",
    "infer",
    "judge_many",
    "looks_unjudged",
    "parse_reply",
    "sanitize",
]
