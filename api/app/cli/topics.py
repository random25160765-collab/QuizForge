"""考纲的读写命令。**树在库里**，`topics.yaml` 只是导出物。

树会越长越大（学科 × 单元 × 知识点，还会跨材料），所以按**节点**操作，
而不是每次重写整份文件：改名、换父（搬家）、退役、导出，都是单点操作。

用法（在 `api/` 目录下）：

    .venv/bin/python -m app.cli.topics tree
    .venv/bin/python -m app.cli.topics add --key tt-metal-tile --name "tile 布局" --parent tt-metal
    .venv/bin/python -m app.cli.topics rename --key tt-metal-tile --name "tile 与 tilize"
    .venv/bin/python -m app.cli.topics move --key tt-metal-tile --parent tt-metal-compute-api
    .venv/bin/python -m app.cli.topics retire --key tt-metal-tile
    .venv/bin/python -m app.cli.topics export --out ../meta/topics.yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import outline


def _tree() -> int:
    nodes = outline.list_nodes()
    for node in nodes:
        indent = "  " * (int(node["depth"]) - 1)
        leaf = "" if node["is_leaf"] else "  ▸"
        print(f"{indent}{node['key']}  {node['name']}{leaf}")
    print(f"\n共 {len(nodes)} 个节点 · 分组 {[g['key'] for g in outline.groups()]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli.topics", description="考纲的读写命令")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("tree", help="打印整棵树")
    sub.add_parser("groups", help="打印分组定义")

    add = sub.add_parser("add", help="新增节点")
    add.add_argument("--key", required=True)
    add.add_argument("--name", required=True)
    add.add_argument("--parent", default=None)
    add.add_argument("--order", type=int, default=None)
    add.add_argument("--color", default="")
    add.add_argument("--desc", default="")
    add.add_argument("--group", default="")

    ren = sub.add_parser("rename", help="改名")
    ren.add_argument("--key", required=True)
    ren.add_argument("--name", required=True)

    mov = sub.add_parser("move", help="换父（搬家）")
    mov.add_argument("--key", required=True)
    mov.add_argument("--parent", required=True)

    ret = sub.add_parser("retire", help="退役（不删除）")
    ret.add_argument("--key", required=True)
    ret.add_argument("--keep-children", action="store_true", help="只退役自己，子节点留下")

    exp = sub.add_parser("export", help="从库里导出一份 YAML 快照")
    exp.add_argument("--out", default=None, help="不写的话打到标准输出")

    args = parser.parse_args(argv)

    if args.cmd == "tree":
        return _tree()
    if args.cmd == "groups":
        for group in outline.groups():
            print(f"{group['key']}  {group['name']}  order={group['order_index']}")
        return 0
    if args.cmd == "add":
        node = outline.add_node(
            args.key,
            args.name,
            parent=args.parent,
            order=args.order,
            color=args.color,
            desc=args.desc,
            group=args.group,
        )
        print(f"已新增 {node['key']}（深度 {node['depth']}，order {node['order']}）")
        return 0
    if args.cmd == "rename":
        outline.update_node(args.key, name=args.name)
        print(f"已改名 {args.key} → {args.name}")
        return 0
    if args.cmd == "move":
        outline.update_node(args.key, parent_key=args.parent)
        print(f"已把 {args.key} 挂到 {args.parent} 下")
        return 0
    if args.cmd == "retire":
        result = outline.retire_node(args.key, cascade=not args.keep_children)
        print(f"已退役 {len(result['retired'])} 个节点：{'、'.join(result['retired'][:8])}")
        return 0

    text = outline.render_yaml()
    if args.out:
        path = Path(args.out)
        path.write_text(text, encoding="utf-8")
        print(f"已导出 → {path}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
