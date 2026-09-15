#!/usr/bin/env python3
"""quizforge 主构建脚本。

流程：
    questions/**/*.md  ──解析──▶  校验  ──▶  题库 JSON  ──内联──▶  dist/*.html

产物（全部自包含、可 file:// 双击打开）：
    dist/quiz.html        刷题应用（练习 / 组卷 / 复习 / 设置）
    dist/wrongbook.html   独立错题本
    dist/index.html       导航页（体积很小，不含 KaTeX）
    dist/data.json        题库明文导出（便于 diff 与二次开发）
    dist/topics/<key>.html  仅在 --split 时生成

用法：
    python3 tools/build.py                 # 全量构建
    python3 tools/build.py --incremental   # 复用未变更题目的解析缓存
    python3 tools/build.py --split         # 额外按主题拆分出多个 HTML
    python3 tools/build.py --no-check      # 跳过校验（不推荐）
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_web as _build_web  # noqa: E402
import inline as _inline  # noqa: E402
from dataset import build_dataset, load_topic_tree  # noqa: E402
from check import Diagnostic, check_file, load_topics  # noqa: E402
from question_parser import (  # noqa: E402
    TYPE_LABELS,
    Question,
    QuestionParseError,
    iter_question_files,
    parse_question,
    question_to_dict,   # 权威序列化，与在线导入共用同一份
)

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS_DIR = ROOT / "questions"
THEME_DIR = ROOT / "theme"
RUNTIME_DIR = THEME_DIR / "runtime"
PAGES_DIR = THEME_DIR / "pages"
VENDOR_KATEX = ROOT / "vendor" / "katex"
DIST_DIR = ROOT / "dist"

# 运行时脚本的加载顺序（后者依赖前者），ui.js 与 data.js 必须最先加载
RUNTIME_ORDER = [
    "ui.js",
    "data.js",
    "md.js",
    "highlight.js",
    "engine.js",
    "store.js",
    "sm2.js",
    "ai.js",
    "qview.js",
]

PAGES = {
    "quiz": {
        "title": "quizforge · 刷题",
        "body": "quiz.body.html",
        "js": ["app.js"],
        "css": ["markdown.css", "app.css"],
    },
    "wrongbook": {
        "title": "quizforge · 错题本",
        "body": "wrongbook.body.html",
        "js": ["wrongbook.js"],
        "css": ["markdown.css", "app.css"],
    },
}

CACHE_VERSION = 3


# ---------------------------------------------------------------- 日志


class Log:
    def __init__(self, quiet: bool = False) -> None:
        self.quiet = quiet

    def info(self, msg: str) -> None:
        if not self.quiet:
            print(f"[INFO] {msg}")

    def warn(self, msg: str) -> None:
        print(f"[WARN] {msg}")

    def error(self, msg: str) -> None:
        print(f"[ERROR] {msg}", file=sys.stderr)

    def step(self, msg: str) -> None:
        if not self.quiet:
            print(f"[INFO] → {msg}")


# ---------------------------------------------------------------- 解析缓存


def _file_stamp(path: Path) -> dict:
    stat = path.stat()
    return {"mtime": int(stat.st_mtime), "size": stat.st_size}


def load_cache() -> dict:
    cache_file = DIST_DIR / ".cache" / "questions.json"
    if not cache_file.is_file():
        return {}
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if data.get("version") != CACHE_VERSION:
        return {}
    return data.get("files", {})


def save_cache(files: dict) -> None:
    cache_dir = DIST_DIR / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {"version": CACHE_VERSION, "files": files}
    (cache_dir / "questions.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )


def parse_all(
    files: list[Path],
    log: Log,
    incremental: bool,
    diags: list[Diagnostic],
    topics: dict[str, dict],
) -> tuple[list[dict], int, int]:
    """解析全部题目。返回 (题目字典列表, 命中缓存数, 新解析数)。"""
    cache = load_cache() if incremental else {}
    new_cache: dict[str, dict] = {}
    results: list[dict] = []
    hits = misses = 0

    for path in files:
        rel = str(path.relative_to(ROOT))
        stamp = _file_stamp(path)
        cached = cache.get(rel)
        if cached and cached.get("stamp") == stamp and "data" in cached:
            new_cache[rel] = cached
            results.append(cached["data"])
            hits += 1
            continue

        misses += 1
        try:
            question = parse_question(path)
        except QuestionParseError as exc:
            diags.append(Diagnostic("ERROR", exc.path, exc.line, str(exc).split(": ", 1)[-1]))
            continue

        # 结构化校验（与 check.py 同一套逻辑，保证 --no-check 之外行为一致）
        check_file(path, topics, diags)

        data = question_to_dict(question)
        new_cache[rel] = {"stamp": stamp, "data": data}
        results.append(data)

    if incremental:
        save_cache(new_cache)
        log.info(f"解析缓存：命中 {hits}，重新解析 {misses}")
    return results, hits, misses


# ---------------------------------------------------------------- 渲染


def _read(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"缺少文件：{path}")
    return path.read_text(encoding="utf-8")


def render_app_page(
    page: str,
    dataset: dict,
    katex_js: str,
    katex_css: str,
    log: Log,
    title: str | None = None,
    base: str = "",
    ai_defaults: dict | None = None,
) -> str:
    spec = PAGES[page]
    shell = _read(THEME_DIR / "shell.html")

    app_css = _inline.concat_css([THEME_DIR / name for name in spec["css"]])
    runtime_js = _inline.concat_js([RUNTIME_DIR / name for name in RUNTIME_ORDER])
    page_js = _inline.concat_js([RUNTIME_DIR / name for name in spec["js"]])
    body = _read(PAGES_DIR / spec["body"])

    data_js = "window.__QB__=" + _inline.js_literal(dataset) + ";"
    if ai_defaults:
        data_js += "window.__QB_AI_DEFAULTS__=" + _inline.js_literal(ai_defaults) + ";"

    html = _inline.render_shell(
        shell,
        {
            "title": title or spec["title"],
            "page": page,
            # --split 的产物放在 dist/topics/ 下，需要 base 让 quiz.html /
            # wrongbook.html 这类相对链接指回上一级目录
            "base": f'<base href="{base}">' if base else "",
            "head_assets": _inline.offline_head(katex_css, app_css),
            "scripts": _inline.offline_scripts(
                data_js=data_js,
                katex_js=katex_js,
                runtime_js=runtime_js,
                page_js=page_js,
            ),
            "body": body,
        },
    )
    log.step(f"渲染 {page} 页：{len(html.encode('utf-8')) / 1024:.0f} KB")
    return html


def _render_landing_subjects(meta: dict, stats: dict, esc) -> tuple[str, int, int]:  # noqa: ANN001
    """首页「覆盖范围」的胶囊，返回 (HTML, 学科数, 方向数)。

    刻意只铺一级学科。三级主题树全展开有一百多项，那是筛选器该干的事；
    首页只需要让人一眼看到「覆盖了哪些方向」。

    数字与列表必须来自**同一个已裁剪的集合**：标题写「12 个学科」而下面只列 11 个，
    比不写数字更让人困惑。
    """
    by_topic = stats.get("byTopic", {})
    items: list[str] = []
    directions: set[str] = set()
    for node in meta.get("topics", []):
        if node.get("depth") != 1:
            continue
        count = int(by_topic.get(node.get("key"), 0) or 0)
        # 一道题都没有的学科不列 —— 与刷题应用里的筛选器同一条规则
        if count <= 0:
            continue
        directions.add(str(node.get("group") or ""))
        color = str(node.get("color") or "").strip()
        style = f' style="--subj:{esc(color)}"' if color else ""
        items.append(
            f'<li class="subj"{style}><span class="subj__dot"></span>'
            f'<span class="subj__name">{esc(node.get("name", ""))}</span>'
            f'<span class="subj__n">{count}</span></li>'
        )
    # 空分组名只是「没归类」，不该被算成一个方向
    directions.discard("")
    return "\n        ".join(items), len(items), len(directions)


def render_landing(dataset: dict) -> str:
    """导航页：刻意不内联 KaTeX 与运行时，体积保持在 10 KB 量级。"""
    meta = dataset["meta"]
    stats = meta["stats"]
    template = _read(THEME_DIR / "landing.html")

    def esc(value: object) -> str:
        return (
            str(value)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    # 首页只保留一个入口，题型/主题不再铺开成列表，只留两个规模数字
    by_type = stats.get("byType", {})
    type_count = sum(1 for key in ("single", "multi", "blank", "short", "problem") if by_type.get(key))

    ids_js = json.dumps([q["id"] for q in dataset["questions"]], ensure_ascii=False, separators=(",", ":"))

    subjects_html, subject_count, direction_count = _render_landing_subjects(meta, stats, esc)

    html = (
        template.replace("__LANDING_TITLE__", "quizforge")
        .replace("__LANDING_TOTAL__", str(stats.get("total", 0)))
        .replace("__LANDING_TOPICS_N__", str(subject_count))
        .replace("__LANDING_TYPES_N__", str(type_count))
        # 首页页脚只给到日期，秒级时间戳对用户没有意义
        .replace("__LANDING_GENERATED__", esc(str(meta.get("generatedAt", ""))[:10]))
        .replace("__LANDING_IDS__", ids_js)
        .replace("__LANDING_SUBJECTS__", subjects_html)
        .replace("__LANDING_GROUP_N__", str(direction_count))
    )

    leftover = sorted(set(re.findall(r"__LANDING_[A-Z_]+__", html)))
    if leftover:
        raise RuntimeError(f"导航页仍有未替换的占位符：{leftover}")
    return html


# ---------------------------------------------------------------- 主流程


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="构建 quizforge 离线题库")
    parser.add_argument("--incremental", action="store_true", help="复用未变更题目的解析缓存")
    parser.add_argument("--split", action="store_true", help="额外按主题拆分生成多个 HTML")
    parser.add_argument("--no-check", action="store_true", help="跳过题库校验")
    parser.add_argument("--quiet", "-q", action="store_true", help="安静模式")
    parser.add_argument("--out", default=str(DIST_DIR), help="输出目录，默认 dist/")
    parser.add_argument(
        "--web",
        action="store_true",
        help="在线模式：输出「页面 + /assets 静态资源」（不内联题库，数据来自 /api）",
    )
    parser.add_argument("--api-base", default="/api", help="在线模式下前端请求的接口前缀")
    parser.add_argument(
        "--ai-config",
        default="",
        help="可选的本地 AI 配置文件（JSON），内容会作为默认值注入产物。"
        "⚠ 若其中含 apiKey，密钥会被写进 HTML，只用于本机自用，切勿分发或部署。",
    )
    args = parser.parse_args(argv[1:])

    started = time.time()
    log = Log(quiet=args.quiet)
    out_dir = Path(args.out).resolve()

    # 1) 依赖就绪检查
    if not (VENDOR_KATEX / "katex.min.js").is_file():
        log.error("vendor/katex 未就绪，请先运行：python3 tools/vendor.py")
        return 2

    # 在线模式：不解析题库、不内联资源，只输出页面与 /assets
    if args.web:
        info = _build_web.build(out_dir, log, api_base=args.api_base)
        log.info(f"构建完成：{len(info['pages'])} 个页面 -> {out_dir}（{time.time() - started:.2f}s）")
        return 0

    # 可选的本地 AI 默认值注入
    ai_defaults: dict | None = None
    if args.ai_config:
        config_path = Path(args.ai_config).expanduser().resolve()
        if not config_path.is_file():
            log.error(f"--ai-config 指定的文件不存在：{config_path}")
            return 2
        try:
            ai_defaults = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            log.error(f"--ai-config 不是合法 JSON：{exc}")
            return 2
        if not isinstance(ai_defaults, dict):
            log.error("--ai-config 的内容必须是 JSON 对象")
            return 2
        if str(ai_defaults.get("apiKey", "")).strip():
            log.warn("已注入 API 密钥到产物 HTML —— 该文件只在本机使用，不要分发、不要部署到公网")
            log.warn("如需发布版本，请改用不带 --ai-config 的构建（make build）")

    # 2) 主题定义（学科 → 单元 → 知识点）
    topics, topic_order, topic_groups, topic_problems = load_topic_tree()
    for problem in topic_problems:
        log.warn(f"meta/topics.yaml: {problem}")
    if not topics:
        log.error("meta/topics.yaml 里没有定义任何 topic")
        return 2
    leaf_count = sum(1 for n in topics.values() if n["leaf"])
    subject_count = sum(1 for n in topics.values() if n["depth"] == 1)
    log.step(f"考纲主题树：{len(topic_groups)} 个分组 / {subject_count} 个学科 / {leaf_count} 个知识点")

    # 3) 解析题目
    files = iter_question_files(QUESTIONS_DIR) if QUESTIONS_DIR.is_dir() else []
    log.step(f"发现 {len(files)} 个题目文件")

    diags: list[Diagnostic] = []
    questions, _, _ = parse_all(files, log, args.incremental, diags, topics)

    errors = [d for d in diags if d.level == "ERROR"]
    warns = [d for d in diags if d.level == "WARN"]
    for diag in sorted(diags, key=lambda d: (str(d.path), d.line, d.level)):
        print(diag.render(), file=sys.stderr if diag.level == "ERROR" else sys.stdout)

    if errors and not args.no_check:
        log.error(f"校验未通过：{len(errors)} 个错误（用 --no-check 可强行构建）")
        return 1

    # 4) 数据集
    dataset = build_dataset(questions, topics, topic_order, topic_groups)
    total = dataset["meta"]["stats"]["total"]
    log.step(f"题库规模：{total} 题（{len(warns)} 个告警）")

    # 5) 内联 KaTeX
    katex_css_raw = _read(VENDOR_KATEX / "katex.css")
    katex_css, font_count = _inline.inline_fonts(katex_css_raw, VENDOR_KATEX / "fonts")
    katex_js = _read(VENDOR_KATEX / "katex.min.js")
    log.step(f"内联 KaTeX：引擎 {len(katex_js) / 1024:.0f} KB，字体 {font_count} 个")

    # 6) 输出
    out_dir.mkdir(parents=True, exist_ok=True)

    for page in PAGES:
        html = render_app_page(page, dataset, katex_js, katex_css, log, ai_defaults=ai_defaults)
        (out_dir / f"{page}.html").write_text(html, encoding="utf-8")

    (out_dir / "index.html").write_text(render_landing(dataset), encoding="utf-8")

    (out_dir / "data.json").write_text(
        json.dumps(dataset, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    # 7) 可选：按主题拆分
    if args.split:
        topics_dir = out_dir / "topics"
        topics_dir.mkdir(exist_ok=True)
        written = 0
        for topic in dataset["meta"]["topics"]:
            # 只按一级学科拆分（树里有一百来个知识点，逐个出文件没有意义），
            # 每个文件包含该学科下全部子孙知识点的题目。
            if topic["depth"] != 1:
                continue
            keys = set([topic["key"]] + topic["descendants"])
            subset = [q for q in dataset["questions"] if q["topic"] in keys]
            if not subset:
                continue
            sub_dataset = json.loads(json.dumps(dataset))
            sub_dataset["questions"] = subset
            sub_dataset["meta"]["stats"]["total"] = len(subset)
            sub_html = render_app_page(
                "quiz",
                sub_dataset,
                katex_js,
                katex_css,
                Log(quiet=True),
                title=f'quizforge · {topic["name"]}',
                base="../",
            )
            (topics_dir / f'{topic["key"]}.html').write_text(sub_html, encoding="utf-8")
            written += 1
        log.step(f"--split：额外生成 {written} 个按主题拆分的 HTML -> {topics_dir.relative_to(ROOT)}/")

    # 8) 摘要
    total_bytes = sum(p.stat().st_size for p in out_dir.rglob("*.html"))
    elapsed = time.time() - started
    log.info(
        f"构建完成：{total} 题，{elapsed:.2f}s，HTML 合计 {total_bytes / 1024 / 1024:.2f} MB -> {out_dir}"
    )
    if not args.quiet:
        for page in ("index.html", "quiz.html", "wrongbook.html"):
            p = out_dir / page
            if p.is_file():
                print(f"       {page:<16} {p.stat().st_size / 1024:8.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
