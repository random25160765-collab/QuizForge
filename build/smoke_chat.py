#!/usr/bin/env python3
"""对**跑起来的那个应用**做一次真实对话烟测（发一句、看落库、看回复）。

## 为什么要有它

打包自检（`build/package.py verify`）刻意不碰 AI：它只验"能启动、能读、能写"。
但"对话"这条路上有几个只在真跑时才会暴露的环节 —— 用户消息落库、
助手占位落库、流式增量、最终消息回写。**内测包第一次发出去就死在这条路上**：
`messages.id` 在库里是 `BIGINT`，SQLite 上不自增 → 一发「你好」就
`NOT NULL constraint failed: messages.id`。而当时的自检全绿。

所以这一层单独存在：它**会花钱**（走一次模型），所以不放进每次打包，
而是发版前手动跑一次。

用法（在哪边跑都行，只要那个地址通）：

    python3 build/smoke_chat.py                       # 默认 http://127.0.0.1:8100
    python3 build/smoke_chat.py --url http://127.0.0.1:8123
    python3 build/smoke_chat.py --text "讲一下 NoC"

注意：Windows 侧的 `127.0.0.1` 与 WSL 的不是同一个（网络命名空间不同），
所以对 exe 跑的时候要用 **Windows 那边的 python** —— 见 `build/smoke_win.py`（一条命令搞定）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def _post(base: str, path: str, payload: dict, timeout: float = 15.0) -> dict:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _get(base: str, path: str, timeout: float = 15.0) -> dict:
    with urllib.request.urlopen(base + path, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _wait_ready(base: str, seconds: float) -> dict:
    deadline = time.time() + seconds
    last: Exception | None = None
    while time.time() < deadline:
        try:
            return _get(base, "/api/health", timeout=3)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            time.sleep(1)
    raise SystemExit(f"服务在 {seconds:.0f} 秒内没起来：{last}")


def _stream(base: str, cid: str, content: str) -> tuple[list[str], str]:
    """发一条消息并读完流。返回（事件名列表，助手正文）。

    帧格式是 `event: <名>\\ndata: <json>\\n\\n`（见 `chat.py` 的 `_sse`）：
    事件名在 `event:` 行、**不在** data 里 —— 第一版脚本看错了这里，
    于是"明明回了却说没回"。
    """
    request = urllib.request.Request(
        f"{base}/api/chat/conversations/{cid}/messages",
        data=json.dumps({"content": content}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    kinds: list[str] = []
    answer: list[str] = []
    name = ""
    with urllib.request.urlopen(request, timeout=300) as response:  # noqa: S310
        for raw in response:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line.startswith("event:"):
                name = line[6:].strip()
                kinds.append(name)
                continue
            if not line.startswith("data:"):
                continue
            try:
                payload = json.loads(line[5:].strip())
            except ValueError:
                continue
            if name == "delta":
                answer.append(str((payload or {}).get("text") or ""))
            elif name == "error":
                answer.append(f"[错误] {(payload or {}).get('message')}")
    return kinds, "".join(answer).strip()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass

    parser = argparse.ArgumentParser(prog="build/smoke_chat.py", description="真实对话烟测")
    parser.add_argument("--url", default="http://127.0.0.1:8100", help="应用地址")
    parser.add_argument("--text", default="你好", help="发什么")
    parser.add_argument("--ready", type=float, default=60.0, help="等多久算超时")
    parser.add_argument("--keep", action="store_true", help="留着这次烟测的会话（默认也不删，只是标记）")
    args = parser.parse_args(argv)

    base = args.url.rstrip("/")
    print("等它起来…（`--noconsole` 的包里没有终端，日志在数据目录的 quizforge.log）")
    health = _wait_ready(base, args.ready)
    bank = health.get("bank") or {}
    ai = health.get("ai") or {}
    print(
        f"健康检查：ok={health.get('ok')} 通道={health.get('channel')} "
        f"题数={bank.get('questions')} 内测额度={ai.get('betaEnabled')} 密钥={ai.get('betaConfigured')}"
    )

    conv = _post(base, "/api/chat/conversations", {"title": "烟测"})["conversation"]
    cid = conv["id"]
    print(f"新建会话：{cid}")

    kinds, answer = _stream(base, cid, args.text)
    print(f"流里的事件：{','.join(sorted(set(kinds)))}")
    if answer:
        print(f"助手回复（前 120 字）：{answer[:120]}")

    detail = _get(base, f"/api/chat/conversations/{cid}")
    messages = detail.get("messages") or []
    print(f"会话里存了 {len(messages)} 条消息：")
    for item in messages:
        body = str(item.get("content") or "")[:60].replace("\n", " ")
        print(f"   #{item.get('id')} {item.get('role')}: {body}")

    problems: list[str] = []
    roles = {item.get("role") for item in messages}
    if "user" not in roles:
        problems.append("用户消息没落库（`messages.id` 自增是不是又坏了？）")
    if "assistant" not in roles:
        problems.append("助手消息没落库")
    if not answer:
        problems.append("模型没有回内容（流里没有 delta）")
    if not (ai.get("betaEnabled") and ai.get("betaConfigured")) and health.get("channel") == "beta":
        problems.append("内测包却没带上内测额度")

    if problems:
        print("\n烟测没过：")
        for item in problems:
            print(f"  ✗ {item}")
        return 1
    print("\n烟测通过：消息落库 · 模型有回复 · 通道与额度正常 ✓")
    if not args.keep:
        print("（这次烟测的会话留在库里了，标题是「烟测」—— 可以直接删掉）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
