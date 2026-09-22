"""在**仓库之外**找素材目录：别写死盘符。

素材（手册、论文、笔记库）不在仓库里，放在哪块盘是**这台机器的事**。写死一个
`F:/Documents` 看着省事，实际是"换台机器就悄悄失效" —— 它不报错，只是找不到资料，
然后退化成空列表。这种失败最难回头发现。

两种写法都要认：

* WSL 下 Windows 盘挂在 `/mnt/<单字母>`（`/mnt/f/Documents`）；
* 原生 Python（Windows 侧那份、打包版）直接写 `F:/Documents`。

这里只负责**给候选**，不判断存在性 —— "候选"和"选定"是两件事：有的调用方按顺序
试第一个存在的，有的要把全列出来。
"""

from __future__ import annotations

from pathlib import Path

#: 盘符扫描的范围。C–H 覆盖常见情形；换成别的盘，用环境变量指（各调用方都有自己的）。
DEFAULT_LETTERS = "CDEFGH"


def drive_roots(name: str, *, letters: str = DEFAULT_LETTERS) -> list[Path]:
    """可能的盘根目录，按顺序给出：先 WSL 挂载点，再盘符写法。去重、保序。

    `name` 是目标目录名（`Documents` / `Vaults` …）。
    """
    found: list[Path] = []

    try:
        # 只认单字母挂载点：`/mnt/c`、`/mnt/f` 是 Windows 盘，
        # 而 `/mnt/wsl`、`/mnt/wslg` 是 WSL 自己的内部挂载，不是用户的盘
        mounts = sorted(
            path for path in Path("/mnt").iterdir() if path.is_dir() and len(path.name) == 1
        )
    except OSError:  # pragma: no cover - 非 WSL 环境里 /mnt 不可读
        mounts = []

    found += [mount / name for mount in mounts]
    found += [Path(f"{letter}:/{name}") for letter in letters]

    seen: set[str] = set()
    unique: list[Path] = []
    for path in found:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique
