"""钉住一个外部工具的 URL 与 sha256（**有网的时候跑一次**）。

    python -m tools.pin_toolchain pandoc --url https://…/pandoc-3.7-linux-amd64.tar.gz
    python -m tools.pin_toolchain --check          # 看哪些还没钉

为什么要有它：运行时**只装钉过哈希的包**（见 `api/app/toolchain.py`）。
所以"这个包从哪儿来、哈希是多少"必须先是人定下来的事实，
而不是运行时随手下一次 —— 那等于把供应链交给运气。

跑完写 `build/toolchain.json`（与 pyodide 那份清单同一个位置、同一个道理）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

from app import toolchain  # noqa: E402


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pin(name: str, url: str, *, platform: str) -> str:
    """把 URL 下下来、算哈希、写进清单。返回哈希。"""
    print(f"取 {url} …")
    with urllib.request.urlopen(url, timeout=300) as response:  # noqa: S310
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    raw = toolchain.manifest()
    raw.setdefault(name, {}).setdefault(platform, {})
    raw[name][platform] = {"url": url, "sha256": digest}
    toolchain.MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    toolchain.MANIFEST_FILE.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="钉住外部工具的 URL 与 sha256")
    parser.add_argument("component", nargs="?", help="组件名（pdftotext / pandoc）")
    parser.add_argument("--url", default="", help="这个平台上那个包的下载地址")
    parser.add_argument("--platform", default="", help="默认按当前机器")
    parser.add_argument("--check", action="store_true", help="只看现状，不动清单")
    args = parser.parse_args(argv)

    platform = args.platform or toolchain.platform_key()
    raw = toolchain.manifest()

    if args.check or not args.component:
        print(f"平台 {platform} · 清单 {toolchain.MANIFEST_FILE}")
        for key, spec in toolchain.COMPONENTS.items():
            pinned = (raw.get(key) or {}).get(platform) or {}
            mark = "已钉" if pinned.get("url") and pinned.get("sha256") else "没钉"
            path, why = toolchain.resolve(key)
            print(f"  {mark:4} {key:10} {spec['why']} · 现状：{why}")
        print("\n没钉的用：python -m tools.pin_toolchain <组件> --url <地址>")
        return 0

    if not args.url:
        print("得给 --url（这个平台上那个包的地址）")
        return 2
    if args.component not in toolchain.COMPONENTS:
        print(f"不认识的组件：{args.component}（可选：{'、'.join(toolchain.COMPONENTS)}）")
        return 2

    digest = pin(args.component, args.url, platform=platform)
    print(f"钉好了：{args.component} @ {platform} sha256={digest[:16]}…")
    print(f"写进 {toolchain.MANIFEST_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
