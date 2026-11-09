#!/usr/bin/env python3
"""启动器图标：把白砖里那枚黑色圆环放大，生成各密度 PNG。

2026-11-09 用户反馈"桌面图标里那个黑色的元素要调大"：自适应图标的前景层
（108dp 画布）里，白砖只占 60%，黑环又只占白砖的 55% —— 在桌面上看就是
一枚小小的环浮在一大块白里。这里保持**白砖大小不变**，只把黑环 + 青点放大
`RING_SCALE` 倍（外径约白砖的 74%），环内的白圈跟着环一起变大。

为什么不直接改 SVG：`assets/web/icon.svg` 是网页用的 256 画布版，尺寸关系
与 Android 的 108dp 安全区不同；重排一次容易把两处画歪。这里只做"元素层
放大"这一件事，白砖、圆角、配色一律不动。

幂等：原图备份在 `mobile/icons/origin/`。第一次运行从 res 里拷一份过去，
之后每次都从备份生成 —— 重复运行不会越放越大。

用法：
    python3 mobile/scripts/make-icons.py
"""

from __future__ import annotations

import shutil
from pathlib import Path

from PIL import Image

#: 黑环相对白砖的目标外径（当前 55%，调到 74%）
RING_SCALE = 1.33

#: 各密度对应的前景画布边长（108dp）与旧式图标边长（48dp）
FOREGROUND = {"mdpi": 108, "hdpi": 162, "xhdpi": 216, "xxhdpi": 324, "xxxhdpi": 432}
LEGACY = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}

DARK = (0x17, 0x20, 0x3A)
TEAL = (0x2D, 0xD4, 0xBF)
BRICK = (0xF0, 0xF4, 0xF8)

ROOT = Path(__file__).resolve().parents[2]
RES = ROOT / "mobile" / "android" / "app" / "src" / "main" / "res"
ORIGIN = ROOT / "mobile" / "icons" / "origin"

NAMES = ("ic_launcher_foreground.png", "ic_launcher.png", "ic_launcher_round.png")


def _near(pixel: tuple[int, int, int, int], color: tuple[int, int, int], tol: int = 14) -> bool:
    return (
        pixel[3] > 8
        and abs(pixel[0] - color[0]) < tol
        and abs(pixel[1] - color[1]) < tol
        and abs(pixel[2] - color[2]) < tol
    )


def _mark_mask(im: Image.Image) -> Image.Image:
    """黑环 + 青点的像素（白砖、透明部分不算）。"""
    mask = Image.new("L", im.size, 0)
    src = im.load()
    dst = mask.load()
    for y in range(im.height):
        for x in range(im.width):
            p = src[x, y]
            if _near(p, DARK) or _near(p, TEAL):
                dst[x, y] = 255
    return mask


def enlarge(im: Image.Image, scale: float) -> Image.Image:
    """把元素层放大 `scale` 倍贴回原处；白砖 / 圆角 / 背景一律不动。

    直接裁 bbox 整块缩放，而不是按颜色抠出来再拼：bbox 的四角与环内白圈
    本来就是砖色，缩放后盖在砖上完全看不出来，而边缘的**抗锯齿 alpha 是连续
    的**，跟着 LANCZOS 一起缩放才不会有毛刺（抠色再缩放会得到硬边 + 锯齿）。
    """
    mask = _mark_mask(im)
    box = mask.getbbox()
    if box is None:
        raise SystemExit("图标里找不到黑环 / 青点 —— 配色变了？看一眼 make-icons.py 的常量")
    cx = (box[0] + box[2]) / 2
    cy = (box[1] + box[3]) / 2

    mark = im.crop(box).resize(
        (round((box[2] - box[0]) * scale), round((box[3] - box[1]) * scale)),
        Image.LANCZOS,
    )

    # 原位置先用砖色盖住（这块完全落在白砖里，不会碰圆角）
    out = im.copy()
    pad = 4
    out.paste(
        Image.new("RGBA", (box[2] - box[0] + pad * 2, box[3] - box[1] + pad * 2), (*BRICK, 255)),
        (box[0] - pad, box[1] - pad),
    )
    out.alpha_composite(mark, (round(cx - mark.width / 2), round(cy - mark.height / 2)))
    return out


def main() -> None:
    src_dir = RES / "mipmap-xxxhdpi"
    ORIGIN.mkdir(parents=True, exist_ok=True)
    for name in NAMES:
        if not (ORIGIN / name).is_file():
            shutil.copy2(src_dir / name, ORIGIN / name)

    masters = {
        "ic_launcher_foreground.png": (Image.open(ORIGIN / "ic_launcher_foreground.png"), FOREGROUND),
        "ic_launcher.png": (Image.open(ORIGIN / "ic_launcher.png"), LEGACY),
        "ic_launcher_round.png": (Image.open(ORIGIN / "ic_launcher_round.png"), LEGACY),
    }
    for name, (im, sizes) in masters.items():
        im = im.convert("RGBA")
        big = enlarge(im, RING_SCALE)
        for density, size in sizes.items():
            target = big.resize((size, size), Image.LANCZOS)
            path = RES / f"mipmap-{density}" / name
            target.save(path, "PNG", optimize=True)
            print(f"{path.relative_to(ROOT)}  {size}x{size}")


if __name__ == "__main__":
    main()
