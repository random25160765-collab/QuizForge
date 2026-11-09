#!/usr/bin/env python3
"""把本机的 Turso 配置与对话口令塞进 APK 的 assets/sync/（**可选**）。

⚠️ 这一步会把密钥写进 APK：`assets/sync/` 已被 .gitignore 忽略，但 APK 是可以
反编译的 —— 它只适合「自己的手机、自己的包」。不想带就跳过这个脚本，
首次启动后在设置页里手填即可。

拷什么：

* `config/settings.local.json` 里的 `turso` 块（url / token / enabled）**与
  `syncPassphrase`**（对话端到端加密口令）→ 设备首启时落到
  `filesDir/quizforge/config/settings.local.json`
  （2026-11-09 起口令也住设置文件，不再单独拷 `snapshot.key`）

已有的设备文件不会被覆盖（BootHelper 只补缺），所以升级包不会抹掉用户改过的设置。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "mobile" / "android" / "app" / "src" / "main" / "assets" / "sync"


def main() -> None:
    settings = ROOT / "config" / "settings.local.json"
    if not settings.is_file():
        raise SystemExit(f"没有 {settings}")

    data = json.loads(settings.read_text(encoding="utf-8"))
    turso = data.get("turso")
    if not isinstance(turso, dict) or not turso.get("url"):
        raise SystemExit("settings.local.json 里没有 turso 配置，没什么可注入的")

    payload = {"turso": turso}
    passphrase = str(data.get("syncPassphrase") or "").strip()
    if passphrase:
        payload["syncPassphrase"] = passphrase

    ASSETS.mkdir(parents=True, exist_ok=True)
    (ASSETS / "settings.local.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"✓ turso → {ASSETS / 'settings.local.json'}（url={turso.get('url')}）")
    if passphrase:
        print("✓ 口令 → 同一份 settings.local.json 的 syncPassphrase")
    else:
        print("· settings.local.json 里没有 syncPassphrase，对话同步会因缺口令被拒绝（符合端到端的设计）")

    # 老包留下的那个文件：口令已经搬进设置，留着它就是"两份"
    stale = ASSETS / "snapshot.key"
    if stale.is_file():
        stale.unlink()
        print(f"✓ 删掉旧的 {stale.name}（口令只在设置文件里）")

    print("⚠️ 以上内容会进入 APK，只适合自用包；不要提交到仓库。")


if __name__ == "__main__":
    sys.exit(main())
