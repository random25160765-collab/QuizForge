#!/usr/bin/env python3
"""密钥扫描器 —— 在提交/构建前拦住「把密钥带出去」。

## 为什么要有它

2026-09-17 撞过一次：开发库快照（当时是 `db/quizforge.sql.gz`，现在是
`db/quizforge.db.gz`）是进版本控制的，而
`user_settings` 里存着用户自己填的 API 密钥（见 `api/app/routers/ai.py` 里
那个写明的取舍）—— 一把真的 DeepSeek 密钥就这么跟着快照上了公开仓库，
追下去有十一个提交。**事后清理没有意义**（旧对象仍可按 SHA 取到，克隆与
fork 里也有），只能换密钥。所以真正的解法是别再让它出去。

配套的两道口子：`make db-snapshot` 落盘前抹 `"apiKey"`（源头），这个扫描器
在 `make check` 里兜底（出口）。

## 扫什么

扫的是 `git ls-files`（**会被提交的那些**），不是整个目录：
`.env`、`config/ai.local.json`、`reference/` 这些本来就该待在库外，
扫它们只会天天误报，然后就没人看这个报告了。

`.gz` 会解压着扫（那次事故就是栽在这里：密钥没在明文里，在压缩包里）。

## 用法

    python3 tools/secret_scan.py                 # 扫所有被跟踪的文件
    python3 tools/secret_scan.py a.py b.md       # 只扫这几个
    python3 tools/secret_scan.py --history 20    # 连最近 20 个提交一起扫（慢）

退出码：有 [E] 就 1（构建/提交应该被拦下），否则 0。
"""

from __future__ import annotations

import gzip
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 命中即 [E]。阈值都刻意偏高一点：宁可漏掉短得像占位符的串，
# 也不要天天误报 —— 一份总在喊狼来了的报告等于没有报告。
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("API 密钥（sk-…）", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub 令牌", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("apiKey 字段里有值", re.compile(r'"apiKey"\s*:\s*"(?!\s*")([^"]{8,})"')),
    ("私钥块", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Bearer 令牌", re.compile(r"\bBearer\s+[A-Za-z0-9._-]{24,}")),
)

# 明显是占位符 / 测试值的不算 —— 命中里出现这些词就跳过。
# 真密钥是随机串，不会含这些词，所以这样放行是安全的；
# 而测试桩里的 `sk-beta-test` 天天报警的话，这个报告就没人看了。
PLACEHOLDERS = (
    "redacted",
    "placeholder",
    "your_",
    "your-",
    "example",
    "test",
    "fake",
    "dummy",
    "sample",
    "xxxx",
    "<",
)

# 单个文件最大扫多少字节（防着一个几 G 的产物把内存吃掉）
MAX_BYTES = 8 * 1024 * 1024


def mask(text: str) -> str:
    """只留头尾 —— 报告里不能把密钥原样打出来（日志也会被存下来）。"""
    text = text.strip()
    if len(text) <= 10:
        return text[:2] + "…"
    return text[:6] + "…" + text[-2:] + f"（{len(text)} 位）"


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def read_text(path: Path) -> str | None:
    """读成文本。`.gz` 解压着读；其他二进制跳过（扫不出东西，还慢）。"""
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rb") as handle:
                return handle.read(MAX_BYTES).decode("utf-8", errors="replace")
        raw = path.read_bytes()[:MAX_BYTES]
    except OSError:
        return None
    if b"\0" in raw[:4096]:
        return None
    return raw.decode("utf-8", errors="replace")


def scan_text(text: str) -> list[tuple[str, str, str]]:
    """返回 [(类型, 位置提示, 掩码后的命中)]。位置对 .gz 无意义，故允许为空。"""
    found: list[tuple[str, str, str]] = []
    for label, pattern in PATTERNS:
        for match in pattern.finditer(text):
            hit = match.group(match.lastindex or 0)
            if any(token in hit.lower() for token in PLACEHOLDERS):
                continue
            line = text.count("\n", 0, match.start()) + 1
            found.append((label, f":{line}", mask(hit)))
    return found


def scan_files(paths: list[str]) -> int:
    problems = 0
    for name in paths:
        path = ROOT / name
        if not path.is_file():
            continue
        text = read_text(path)
        if text is None:
            continue
        for label, where, hit in scan_text(text):
            print(f"[E] {name}{where}  {label}  {hit}")
            problems += 1
    print(
        f"[OK] 扫过 {len(paths)} 个被跟踪的文件"
        if not problems
        else f"[E] {problems} 处疑似密钥 —— 别提交，先抹掉（make db-snapshot 已会自动抹 apiKey）"
    )
    return problems


def scan_history(limit: int) -> int:
    """连历史一起扫。**只读**，不会改任何东西 —— 但历史里的密钥只能靠换密钥解决。"""
    revisions = subprocess.run(
        ["git", "log", f"-{max(1, limit)}", "--format=%h"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    problems = 0
    for revision in revisions:
        changed = subprocess.run(
            ["git", "show", "--name-only", "--format=", revision],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        for name in changed:
            blob = subprocess.run(
                ["git", "show", f"{revision}:{name}"],
                cwd=ROOT,
                capture_output=True,
                check=False,
            )
            if blob.returncode != 0:
                continue
            raw = blob.stdout[:MAX_BYTES]
            if name.endswith(".gz"):
                try:
                    text = gzip.decompress(raw).decode("utf-8", errors="replace")
                except OSError:
                    continue
            elif b"\0" in raw[:4096]:
                continue
            else:
                text = raw.decode("utf-8", errors="replace")
            for label, _, hit in scan_text(text):
                print(f"[E] {revision}:{name}  {label}  {hit}")
                problems += 1
    print(
        f"[OK] 最近 {len(revisions)} 个提交里没有密钥"
        if not problems
        else f"[E] 历史里有 {problems} 处 —— 它们**已经出去了**，只能换密钥（清理历史收不回来）"
    )
    return problems


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--history":
        limit = int(argv[1]) if len(argv) > 1 else 20
        return 1 if scan_history(limit) else 0
    return 1 if scan_files(argv or tracked_files()) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
