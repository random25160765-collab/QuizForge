"""跨机器同步的命令行。

    # 看这台机器的同步状态（不会显示令牌）
    .venv/bin/python -m app.cli.sync status

    # 立刻跑一次（推 + 拉）
    .venv/bin/python -m app.cli.sync run

配置在 `config/settings.local.json` 的 `turso` 块（设置页 →「同步」也能改）：

    "turso": {"url": "libsql://…", "token": "…", "enabled": true}

对话是端到端加密的：`config/snapshot.key` 必须两台机器**同一份**。
没有它也能同步其余数据，但本机一旦有对话就会拒绝同步（而不是明文上传）。
"""

from __future__ import annotations

import argparse
import json
import sys

from .. import sync


def _status() -> int:
    st = sync.status()
    print(f"配置：{'已配' if st['configured'] else '没配'} · "
          f"{'启用' if st['enabled'] else '已关闭'} · {st['url'] or '(没有地址)'}")
    print(f"口令：{'有' if st['keyReady'] else '没有'}（{st['keyFile']}）")
    print(f"本机 origin：{st['origin'] or '(还没跑过)'}")
    print(f"待推送：{st['pending']} 行 · 拉取游标：{st['cursor']}")
    last = st.get("last")
    if last:
        if last.get("ok"):
            print(f"上次：{last.get('at')} · 推 {last.get('pushed', 0)} · 拉 {last.get('pulled', 0)}"
                  f" · {last.get('seconds', '?')}s")
        else:
            print(f"上次失败：{last.get('at')} · {last.get('error')}")
    else:
        print("上次：还没同步过")
    counts = st.get("tables") or {}
    if counts:
        print("本机行数：" + "、".join(f"{name} {n}" for name, n in counts.items()))
    return 0


def _run() -> int:
    try:
        result = sync.run_once()
    except sync.SyncNotConfigured as exc:
        print(f"没配好：{exc}", file=sys.stderr)
        return 2
    except sync.SyncError as exc:
        print(f"同步失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli.sync", description="跨机器同步")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("status", help="看状态（不显示令牌）")
    sub.add_parser("run", help="立刻跑一次（推 + 拉）")
    args = parser.parse_args(argv)
    if args.cmd == "status":
        return _status()
    return _run()


if __name__ == "__main__":
    raise SystemExit(main())
