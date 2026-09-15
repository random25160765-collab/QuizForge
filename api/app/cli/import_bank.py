"""题库导入命令。

    python -m app.cli.import_bank                 # 导入（有校验错误则拒绝）
    python -m app.cli.import_bank --dry-run       # 只报告影响面，不写库
    python -m app.cli.import_bank --check-only    # 只校验，完全不连数据库
    python -m app.cli.import_bank --force         # 有校验错误也强行导入
    python -m app.cli.import_bank --json out.json # 额外把报告写成 JSON

刻意复用 `tools/` 下的解析器与校验器：导入进库的题目与离线产物
逐字节一致，`make check` 能过的题也一定能导入。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..bank_import import ImportReport, apply, scan
from ..config import get_settings
from ..db import get_session_factory, reset_engine


def _print_diagnostics(result) -> None:  # noqa: ANN001
    for diag in result.diagnostics:
        stream = sys.stderr if diag.level == "ERROR" else sys.stdout
        print(diag.render(), file=stream)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.import_bank",
        description="把 questions/ 与 meta/topics.yaml 导入数据库",
    )
    parser.add_argument("--dry-run", action="store_true", help="只报告影响面，不写库")
    parser.add_argument("--check-only", action="store_true", help="只做校验，不连数据库")
    parser.add_argument("--force", action="store_true", help="即使有校验错误也导入")
    parser.add_argument("--json", default="", help="把报告写成 JSON 文件的路径")
    args = parser.parse_args(argv)

    settings = get_settings()
    print(f"题目目录：{settings.questions_dir}")
    print(f"考纲文件：{settings.topics_file}")

    result = scan(settings)
    if not args.check_only:
        print(f"数据库：{_safe_db(settings.database_url)}")

    errors = len(result.errors)
    warns = len(result.warnings)
    print(f"扫描完成：{result.file_count} 个文件 · {len(result.questions)} 道题 · {errors} 个错误 / {warns} 个告警\n")

    _print_diagnostics(result)

    if args.check_only:
        if errors:
            print(f"\n校验未通过：{errors} 个错误", file=sys.stderr)
            return 1
        print("\n校验通过")
        return 0

    if errors and not args.force:
        print(f"\n校验未通过（{errors} 个错误），已拒绝导入。修好后重跑，或加 --force。", file=sys.stderr)
        return 1

    # 延迟到确认要写库时才创建引擎：--check-only 与纯校验失败都不该碰数据库
    reset_engine()
    session = get_session_factory()()
    try:
        report = apply(session, result, dry_run=args.dry_run, force=args.force)
        if report.errors:
            for line in report.errors:
                print(f"[ERROR] {line}", file=sys.stderr)
            session.rollback()
            return 1
        if args.dry_run:
            session.rollback()
        else:
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    print()
    print(report.render())

    if args.json:
        out = Path(args.json)
        out.write_text(
            json.dumps(_report_payload(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n报告已写入 {out}")

    return 0


def _report_payload(report: ImportReport) -> dict:
    return {
        "contentHash": report.content_hash,
        "dryRun": report.dry_run,
        "questions": {
            "added": report.added,
            "updated": report.updated,
            "unchanged": len(report.unchanged),
            "retired": report.retired,
            "restored": report.restored,
        },
        "topics": {
            "added": report.topics_added,
            "updated": report.topics_updated,
            "retired": report.topics_retired,
        },
        "errors": report.errors,
    }


def _safe_db(url: str) -> str:
    """日志里隐去用户名与密码。"""
    return f"{url.split('://', 1)[0]}://{url.rsplit('@', 1)[-1]}"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
