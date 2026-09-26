"""归一：把原件变成一份**行可寻址、带标题层级**的 markdown（`data/library/.text/{citekey}.md`）。

这一步是 `pipeline/ingest.py` 文档里预留的那一格 ——「当前只支持 Markdown；PDF / HTML / 源码
将来各加一个前端」、「**入口归一，链上无感**」。为什么格式只允许出现在这一步：

* 下游（切片 / 抽取 / 出题 / 校验 / 提升）全都建立在同一个假设上 —— **材料是一份带稳定行号的
  文本**。让格式漏进链路，每个 agent、每条提示词都要分叉。
* 切片按 `##` 切（`ingest.split_slices`），所以产物**必须有标题层级**，否则整篇会被切成一片。

2026-09-26 定下的几条（记在这儿，别只活在对话里）：

* 一份材料 = 一份文件（一本书、一个网页）；**章节 = 切片**（行坐标落在切片上，materials 表
  不用加行区间列）。书**按章节拆**这一条就落在这里：把「第 N 章 / Chapter N / N.N / 短行大写」
  识别成 `##`。
* 网页：**一个主 HTML = 一份材料，`*_files/` 全算它的资源**（靠抽取时解析到的 `src`/`href` 判，
  不靠目录名猜）。
* OCR：**RapidOCR（ONNX）**先跑通。归一把 `judge_text` 判成 `garbled` / `none` 的挑出来 ——
  实测那两本正是这一类（算法导论 142M、人月神话 31M，前 20 页 0 字）。
* 合流：A（资料库）与 B（材料）**不是两份数据，是一条流水线 + 一个锚**。锚就是 **citekey** ——
  这里直接调 `app.library.citekey_for`（同一个规则，两边的文件名因此天生一致）—— 以及
  **sha256**（`materials` 表已有）。`materials.source_path` 指向这份产物，B 侧一行都不用改。
* 资料根是一份**列表**（`/mnt/f/Books`、`/mnt/f/Website`、`~/Source/tt-metal/tech_reports`
  平级），与盘符无关。
* 产物三要求：**不截断**（`attachments.extract(..., limit=0)`）、**有 `##` 层级**、
  **保留 `![]()` 图引用**（`ingest.collect_figures` 只认这种写法）。

用法：

    api/.venv/bin/python -m pipeline.normalize ~/Source/tt-metal/tech_reports
    api/.venv/bin/python -m pipeline.normalize /mnt/f/Books --subject books
    api/.venv/bin/python -m pipeline.normalize --dry-run "/mnt/f/Books/algo/xxx.pdf"

产物落 `data/library/.text/`：`{citekey}.md` 正文 + `{citekey}.json` 旁注（质量分档、页↔行
映射、来源路径、原名）。旁注与资料库那份**同形状**（`judge_text` 的键一个不少，另加几个），
所以资料页看得懂它，不必分两套。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))

from app import attachments, library  # noqa: E402  （要在 sys.path 之后）

#: 产物落这儿：与资料库 A 的正文缓存**同一处**（这就是"合流"的字面意思）。
TEXT_DIR = ROOT / "data" / "library" / ".text"

#: 默认扫什么。一个资料根下往往混着图片与附件，所以默认只认"能出正文"的那几类。
DEFAULT_GLOBS = ("**/*.pdf", "**/*.md", "**/*.markdown", "**/*.txt", "**/*.html", "**/*.htm")


def is_asset_path(path: Path) -> bool:
    """是不是"某份材料的资源"？

    **一个主 HTML = 一份材料**（用户确认的边界）：浏览器保存网页时会把整页资源拖进
    `xxx_files/`，实测 `/mnt/f/Website` 有 1398 个文件、其中只有 29 个是真页面 ——
    那 1369 个（js / css / 图片 / 子页面）都算那份材料的资源，不进材料清单。
    """
    return any(part.endswith("_files") or part in (".orphans", ".text") for part in path.parts)

#: 判成这两档的进"待 OCR"队列，不落产物（空材料进库比搜不到更糟：它会占元数据判定与切片）。
GOOD_STATES = ("ok", "poor")

#: 章节标题。中文书、英文书、技术文档三种路数都在这儿 —— 都是实打实翻过语料才写下的形状。
CHAPTER_PATTERNS = (
    re.compile(r"^\s*第\s*[0-9一二三四五六七八九十百零]+\s*[章节篇部]\s*[^\s].{0,40}$"),
    re.compile(r"^\s*(?:Chapter|CHAPTER|Part|PART)\s+[0-9IVXLC]+\b.{0,40}$"),
    re.compile(r"^\s*\d{1,2}(?:\.\d{1,2}){0,2}\s+\S.{0,40}$"),
    re.compile(r"^\s*(?:附录|Appendix|参考文献|References|Bibliography|索引|Index|前言|Preface|绪论)\s*$"),
)
#: 短行全大写也算标题（英文书的常见排法），但要避开公式与缩写堆。
ALLCAPS_RE = re.compile(r"^[A-Z][A-Z0-9 ,\-'&/]{6,48}$")

#: **书眉**（`87 5.1 Introduction`）：页码打头、后面跟着小节号 —— 它不是标题，是页眉。
#: 实测踩过：不加这一条，一本 1100 页的书报出 1022 个"章"（基本上一页一个），
#: 一本 629 页的中文教材报出 1290 个。
PAGEHEAD_RE = re.compile(r"^\s*\d{1,4}\s+\d")
#: **目录行**（`1.2 系统的分类 ………… 15`）：点线加结尾页码，同样不是标题。
TOC_RE = re.compile(r"[.·\s]{4,}\d{1,4}\s*$")

#: `pdftotext -layout` 用换页符分页 —— 「页↔行映射」就靠它（`ingest.py` 文档点名要的那件）。
PAGE_BREAK = "\f"

#: 单本抽取的上限秒数。上传那条路是 20 秒（够文档用），书不够：实测 `book.pdf` 4492 页、
#: 整本抽 486 万字符、要 32 秒 —— 用 20 秒会把它吞成空串，看着像"这本没有文字层"。
PDF_TIMEOUT_LONG = 1800

H1_RE = re.compile(r"^#\s+(.+)$")


def ascii_words(text: str, words: int = 3, limit: int = 20) -> str:
    """从一段随便什么里取"文件名骨架"：字母数字，取前几个词。

    只用来给**没有元数据时**的兜底判据做参照（真正的引用键交给 `library.citekey_for`，
    免得两处规则各写一遍、迟早对不上）。
    """
    chunks = [re.sub(r"[^A-Za-z0-9]+", "", part) for part in re.split(r"[\s\-_.]+", text or "")]
    chunks = [one for one in chunks if one]
    return "".join(chunks[:words]).lower()[:limit]


def is_ours(key: str, path: Path, out_dir: Path) -> bool:
    """产物目录里那个 `{key}.md` 是不是**这份原件**上一次跑出来的？

    为什么要问这一句：`taken` 用"已存在的文件名"做种子，那重跑同一份原件就会被判成撞车、
    写到 `xxx-2.md` 去 —— 实测踩过（改完代码复跑一遍，读到的还是旧文件，白折腾一轮）。
    判据用旁注里的 `source`：同一份原件 → 同一个键，**覆盖自己那份**，这才叫幂等。
    """
    sidecar = out_dir / f"{key}.json"
    if not sidecar.is_file():
        return False
    try:
        return str(json.loads(sidecar.read_text(encoding="utf-8")).get("source") or "") == str(path)
    except (OSError, ValueError):
        return False


def citekey_for(path: Path, taken: set[str], out_dir: Path) -> str:
    """这份产物的引用键 —— **不出自己的规则**，调资料库那一份（合流的锚就在这一处）。

    纯中文书名（`算法导论（原书第3版）`）取不到骨架，`citekey_for` 会退到 `doc-` + 8 位哈希：
    稳定、不撞车，而**原名照旧进 `title`**（人看得见的永远是原名）。
    真撞了（比如两本 `book.pdf` 在不同目录、或 Vol1/Vol2 的骨架都被截到同一串）才补 `-2`。
    """
    base = library.citekey_for({}, fallback=str(path))
    if base not in taken or is_ours(base, path, out_dir):
        return base
    index = 2
    while f"{base}-{index}" in taken and not is_ours(f"{base}-{index}", path, out_dir):
        index += 1
    return f"{base}-{index}"


def heading_of(line: str) -> str:
    """这一行是不是章节标题？是就给出去掉首尾空白后的文本，不是就返回空串。"""
    body = line.strip().rstrip("。.")
    if not body or len(body) > 60:
        return ""
    if PAGEHEAD_RE.match(body) or TOC_RE.search(body):
        return ""  # 页眉与目录行：看着像标题，其实是噪声
    for pattern in CHAPTER_PATTERNS:
        if pattern.match(body):
            return body
    if ALLCAPS_RE.match(body) and not re.search(r"[=<>{}[\]|]", body):
        return body
    return ""


def to_halfwidth(text: str) -> str:
    """全角 → 半角（NFKC）。

    为什么必须有这一步，实测（`#RD 信号与系统 第二版`）：它的文字层**是完整的**
    （174.8 万字符、每千字真实词 62~79），却被 `judge_text` 判成 `garbled` ——
    因为"怪符号占比"是 0.28，而 `ODD_LIMIT` 是 0.10。那些"怪符号"**全是全角字符**：
    `１ ．２ ｎ ０ － ＝ ［ ］ ａ ｅ ｓ`，私有区 0 个、替换符只有 29 个。
    中文数学排版常把公式排成全角，于是"字母数字"的判据全落空。

    这不只是让分档过关：**检索也吃这个亏** —— 用户搜 `1.5` 得能命中 `１．５`。
    NFKC 只动兼容字符（全角、连字、圈号），**不碰汉字**，所以拿它做归一很稳。
    """
    return unicodedata.normalize("NFKC", text or "")


def structure(pages_text: str, title: str) -> tuple[str, list[dict]]:
    """把抽出来的整篇文本整成 markdown：补 `# 标题`、把章节落成 `##`、记下页↔行。

    只动"行的前缀"，不改任何一行的内容 —— 行号因此是**原文的行号**，出处坐标才立得住。
    """
    lines: list[str] = []
    page_map: list[dict] = []
    page = 1
    start_line = 1
    for raw in pages_text.split("\n"):
        if PAGE_BREAK in raw:
            # 一页结束：记下这一页覆盖的行区间，再把换页符本身去掉（它不该占一行）
            page_map.append({"page": page, "lines": [start_line, max(start_line, len(lines))]})
            page += 1
            raw = raw.replace(PAGE_BREAK, "")
            start_line = len(lines) + 1
        lines.append(raw.rstrip())
    page_map.append({"page": page, "lines": [start_line, max(start_line, len(lines))]})

    out: list[str] = []
    has_h1 = False
    for line in lines:
        if H1_RE.match(line):
            has_h1 = True
        head = heading_of(line)
        if head:
            out.append("## " + head)
        else:
            out.append(line)
    # 首行补一个 `#`：`ingest` 拿它当 title（没有就退到文件名，那对书来说太难看了）
    if not has_h1:
        out.insert(0, "# " + title)
        out.insert(1, "")
        for one in page_map:
            one["lines"] = [one["lines"][0] + 2, one["lines"][1] + 2]
    body = "\n".join(out)
    body = re.sub(r"\n{3,}", "\n\n", body)
    if not body.endswith("\n"):
        body += "\n"
    return body, page_map


def title_of(path: Path) -> str:
    """标题：文件名去掉那几个下载站后缀（`(Z-Library)` / `- Anna's Archive` 之类）。"""
    stem = path.stem
    stem = re.sub(r"\((?:Z-Library|z-library|安娜|Anna'?s Archive)\)", "", stem)
    stem = re.sub(r"[_\s]+", " ", stem).strip(" -_")
    return stem or path.name


def normalize_one(path: Path, subject: str, out_dir: Path, taken: set[str], *, dry_run: bool = False) -> dict:
    """一份原件 → 一份 md + 一份旁注。返回这次的结果（给 CLI 打印用）。"""
    kind = attachments.kind_of(path.name, "")
    if kind in ("image", "legacy", "other"):
        return {"path": str(path), "ok": False, "why": f"不抽正文（kind={kind}）"}

    # 两条支路：网页走 HTML→Markdown（它自带标题层级），其余走抽取 + 章节识别。
    page_map: list[dict] = []
    if path.suffix.lower() in (".html", ".htm"):
        from . import htmlmd  # 网页那一格：转换表与正文定位都在它里面

        got = htmlmd.convert(path)
        if not got.get("ok"):
            return {"path": str(path), "ok": False, "why": "html：" + str(got.get("why") or "没抽出来")}
        text = to_halfwidth(str(got["md"]))
        title = str(got["title"])
    else:
        # limit=0 = 不截断（书要从第一页到最后一页都能被检索到）；再过一道全角→半角，
        # 否则中文数学排版的书会被质量门误判成乱码（见 `to_halfwidth`）。
        raw = to_halfwidth(attachments.extract(path, kind, limit=0, timeout=PDF_TIMEOUT_LONG))
        text, page_map = structure(raw, title_of(path))
        title = title_of(path)

    judged = library.judge_text(text)
    state = str(judged.get("state") or "none")
    if state not in GOOD_STATES:
        return {
            "path": str(path),
            "ok": False,
            "why": f"文字不足（{state}）—— 待 OCR",
            "state": state,
            "chars": judged.get("chars") or 0,
        }

    citekey = citekey_for(path, taken, out_dir)
    taken.add(citekey)
    lines = text.splitlines()
    meta = {
        **judged,
        "citekey": citekey,
        "title": title,
        "subject": subject,
        "source": str(path),
        "kind": kind,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "lines": len(lines),
        "pages": page_map,
        "at": datetime.now().isoformat(timespec="seconds"),
    }
    chapters = sum(1 for one in lines if one.startswith("## "))
    result = {
        "path": str(path),
        "ok": True,
        "citekey": citekey,
        "lines": len(lines),
        "chapters": chapters,
        "pages": len(page_map),
        "state": state,
    }
    if dry_run:
        # dry-run 也把这几项报全（少一项，CLI 就会打出 `?` 与 0，看着像识别失败）
        return {**result, "dry": True, "meta": meta}

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{citekey}.md").write_text(text, encoding="utf-8")
    (out_dir / f"{citekey}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def subject_of(path: Path, base: Path, subject: str) -> str:
    """科目：给了就用给的；否则取**根下面第一层目录名**（`Books/signal/x.pdf` → `signal`）。

    与现有语料的口径一致（`swe` / `cuda` / `nvidia` / `stm32` 都是这个层次的名字）。
    """
    if subject:
        return subject
    try:
        rel = path.relative_to(base)
    except ValueError:
        return base.name.lower() or "misc"
    return rel.parts[0].lower() if len(rel.parts) > 1 else (base.name.lower() or "misc")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.normalize", description="归一：原件 → 行可寻址的 markdown")
    parser.add_argument("path", help="一个文件或一个资料根目录")
    parser.add_argument("--subject", default="", help="科目名（不给就取根下面第一层目录名）")
    parser.add_argument("--out", default=str(TEXT_DIR), help=f"产物目录（默认 {TEXT_DIR}）")
    parser.add_argument("--glob", action="append", default=[], help="目录模式下的匹配式，可给多次")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不落盘")
    args = parser.parse_args(argv)

    target = Path(args.path).expanduser()
    if not target.exists():
        raise SystemExit(f"路径不存在：{target}")
    out_dir = Path(args.out).expanduser()
    patterns = args.glob or list(DEFAULT_GLOBS)
    if target.is_file():
        files, base = [target], target.parent
    else:
        base = target
        found: list[Path] = []
        for pattern in patterns:
            found.extend(sorted(base.glob(pattern)))
        files = [one for one in found if one.is_file() and not is_asset_path(one)]

    if not files:
        raise SystemExit(f"没匹配到文件：{target} --glob {' '.join(patterns)}")

    taken = {one.stem for one in out_dir.glob("*.md")} if out_dir.is_dir() else set()
    done = 0
    pending: list[dict] = []
    for path in files:
        result = normalize_one(path, subject_of(path, base, args.subject), out_dir, taken, dry_run=args.dry_run)
        if result.get("ok"):
            done += 1
            print(
                "  ok  %-52s %6s 行 · %3s 章 · %3s 页  [%s]"
                % (
                    result["citekey"],
                    result.get("lines", 0),
                    result.get("chapters", 0),
                    result.get("pages", 0),
                    result.get("state", "?"),
                )
            )
        else:
            pending.append(result)
            print("  --  %-52s %s" % (path.name[:52], result.get("why", "")))
    print(f"\n共 {len(files)} 份：成 {done}、待处理 {len(pending)}")
    if pending:
        print("待处理的（OCR 支路或还没接的格式）：")
        for one in pending:
            print("  ·", one["path"])
    if not args.dry_run:
        print(f"产物目录：{out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
