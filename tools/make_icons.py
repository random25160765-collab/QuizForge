#!/usr/bin/env python3
"""生成 QFStudio 的图标：浏览器 favicon（SVG）· 预览 PNG · Windows exe 的 `.ico`。

## 为什么这里可以用图像库

图标是**构建期**产物：在开发机上跑一次，把 SVG / PNG / ICO 写进仓库；运行时
（打包出来的那个应用）只吃这些文件，一字节的 Pillow 都不会带进包里。
所以形状直接用设计稿那几行画（`rounded_rectangle` / `ellipse`），不必再拿
逐像素采样去还原它 —— 那种写法只在"运行时不许有依赖"的前提下才划算，
而这条约束管的是应用，不是 `tools/`。

## 图形本身

浅灰蓝的圆角方砖 + 墨色的**环**（右下切掉一块）+ 一颗青绿实心点落在缺口里
（Q 的环与它的点）。几何与配色照设计稿（1024 画布，2026-11-09）：

    方砖 56..968（圆角 220）· 圆心 (512,512) · 实心圆半径 256
    分隔环：半径 160、宽 18（用底色画，于是外圈 256..160、内圆 0..142）
    缺口：圆角矩形 (560,560)–(800,800) r=70，右下角伸到圆外
    青点：(630,630) r=70

16px 下只认得出"环 + 点"，所以缺口与青点都刻意做大。

用法：

    python3 tools/make_icons.py            # 网页 favicon + 预览 PNG + Windows ICO
    python3 tools/make_icons.py --android  # 再加 Android 的 launcher 与自适应前景
    python3 tools/make_icons.py --check    # 只检查产物在不在、尺寸对不对
"""

from __future__ import annotations

import argparse
import sys
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
THEME_ASSETS = ROOT / "theme" / "assets"
BUILD_DIR = ROOT / "build"
ICO_PATH = BUILD_DIR / "icon.ico"
SVG_PATH = THEME_ASSETS / "icon.svg"
PNG_PATH = THEME_ASSETS / "icon-256.png"
#: 浏览器标签页专用：透明底，只有环与点（带砖的那张在标签栏里像个小方块）
MARK_SVG = THEME_ASSETS / "mark.svg"

#: 配色（取自设计稿：浅灰蓝砖 / 深海墨环 / 品牌青绿点）
TILE = (240, 244, 248, 255)   # #F0F4F8
INK = (23, 32, 58, 255)       # #17203A
TEAL = (45, 212, 191, 255)    # #2DD4BF

#: 设计稿的画布与几何（1024 单位，直接照抄，改的时候只动这一块）
S = 1024
PAD = 56
TILE_RADIUS = 220
CENTER = (512, 512)
RING = 256
GAP_RADIUS = 160
GAP_WIDTH = 18
NOTCH = (560, 560, 800, 800)
NOTCH_RADIUS = 70
DOT_CENTER = (630, 630)
DOT_RADIUS = 70

#: ICO 里要放的尺寸（Windows 会按场景挑：任务栏 32/48、资源管理器 16/32/256）
ICO_SIZES = (16, 32, 48, 64, 128, 256)

#: Android 资源（launcher 5 档密度 + 自适应图标的前景）
ANDROID_RES = ROOT / "mobile" / "android" / "app" / "src" / "main" / "res"
MIPMAPS = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}

#: 超采样倍数：ImageDraw 的圆与圆角是**硬边**（不抗锯齿），先画大再缩才平滑
SS = 4


@lru_cache(maxsize=2)
def _draw(tile: bool) -> Image.Image:
    """1024 设计稿的 SS 倍版。画一次缓存住，各尺寸都从它降采样。

    `tile=False` 是"只有环与点"的透明版（自适应图标的前景、圆形图标用它）：
    这时缺口必须是**透明**而不是砖色，否则会在透明底上露出一块浅色楔子。
    """
    k = SS
    img = Image.new("RGBA", (S * k, S * k), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    hole = TILE if tile else (0, 0, 0, 0)

    def box(x0: float, y0: float, x1: float, y1: float) -> list[float]:
        return [x0 * k, y0 * k, x1 * k, y1 * k]

    if tile:
        # 方砖：四周各留 PAD，圆角 220
        draw.rounded_rectangle(
            box(PAD, PAD, S - PAD, S - PAD), radius=TILE_RADIUS * k, fill=TILE
        )
    # 大圆（实心），随后用底色在 160 处画一圈宽 18 的环 —— 于是剩下
    # 外圈（96 厚）+ 中间那枚实心圆（半径 142），正是设计稿的样子
    draw.ellipse(box(CENTER[0] - RING, CENTER[1] - RING,
                     CENTER[0] + RING, CENTER[1] + RING), fill=INK)
    draw.ellipse(box(CENTER[0] - GAP_RADIUS, CENTER[1] - GAP_RADIUS,
                     CENTER[0] + GAP_RADIUS, CENTER[1] + GAP_RADIUS),
                 outline=hole, width=GAP_WIDTH * k)
    # 缺口：圆角矩形（右下角伸到圆外，实际切掉的是它与圆的交集）
    draw.rounded_rectangle(box(*NOTCH), radius=NOTCH_RADIUS * k, fill=hole)
    # 青点：压在缺口上，也压住内圆的右下边缘
    draw.ellipse(box(DOT_CENTER[0] - DOT_RADIUS, DOT_CENTER[1] - DOT_RADIUS,
                     DOT_CENTER[0] + DOT_RADIUS, DOT_CENTER[1] + DOT_RADIUS),
                 fill=TEAL)
    return img


def _master() -> Image.Image:
    """带砖的完整版。"""
    return _draw(True)


def render(size: int) -> Image.Image:
    """一张 size×size 的成品（从 SS 倍母版降采样）。"""
    return _master().resize((size, size), Image.LANCZOS)


# --------------------------------------------------------------------- SVG


def _svg(tile: bool = True) -> str:
    """矢量版：与位图同一套几何（256 视口 = 设计稿 ÷ 4）。

    `tile=False` 是**浏览器标签页用的透明版**：没有那块砖，环直接用描边画 ——
    此时中间那圈"分隔环"是空气，不是砖色（否则透明底上会浮出一圈浅蓝）。
    """
    def at(value: float) -> float:
        """1024 单位 → 256 视口（保留一位小数）。"""
        return round(value / 4, 1)

    mask = f"""  <defs>
    <!-- 右下角的缺口（圆角矩形）：环在这里断开（Q 的尾巴） -->
    <mask id="notch" maskUnits="userSpaceOnUse" x="0" y="0" width="256" height="256">
      <rect width="256" height="256" fill="#fff"/>
      <rect x="{at(NOTCH[0])}" y="{at(NOTCH[1])}"
            width="{at(NOTCH[2] - NOTCH[0])}" height="{at(NOTCH[3] - NOTCH[1])}"
            rx="{at(NOTCH_RADIUS)}" fill="#000"/>
    </mask>
  </defs>"""

    if tile:
        plate = (
            f'  <rect x="{at(PAD)}" y="{at(PAD)}" width="{at(S - 2 * PAD)}"'
            f' height="{at(S - 2 * PAD)}" rx="{at(TILE_RADIUS)}" fill="#F0F4F8"/>\n'
        )
        ring = (
            f'    <circle cx="128" cy="128" r="{at(RING)}" fill="#17203A"/>\n'
            f'    <circle cx="128" cy="128" r="{at(GAP_RADIUS)}" fill="#F0F4F8"/>\n'
            f'    <circle cx="128" cy="128" r="{at(GAP_RADIUS - GAP_WIDTH)}" fill="#17203A"/>'
        )
    else:
        plate = ""
        thick = at(RING) - at(GAP_RADIUS)
        middle = round((at(RING) + at(GAP_RADIUS)) / 2, 1)
        ring = (
            f'    <circle cx="128" cy="128" r="{middle}" fill="none"'
            f' stroke="#17203A" stroke-width="{thick}"/>\n'
            f'    <circle cx="128" cy="128" r="{at(GAP_RADIUS - GAP_WIDTH)}" fill="#17203A"/>'
        )

    # 透明版没有砖，图形只占画布中间那一半 —— 标签页里就会显得小。
    # 所以 viewBox 收到圆的外接框（64..192）再各留 4 的余量：圆占 94%，
    # 既铺满 16px 的格子，又不至于被浏览器圆形遮罩切到边。
    view = "0 0 256 256" if tile else "60 60 136 136"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view}" role="img"
     aria-label="QFStudio">
{plate}{mask}
  <g mask="url(#notch)">
{ring}
  </g>
  <circle cx="{at(DOT_CENTER[0])}" cy="{at(DOT_CENTER[1])}" r="{at(DOT_RADIUS)}" fill="#2DD4BF"/>
</svg>
"""


# ------------------------------------------------------------------ Android


def _round_icon(size: int) -> Image.Image:
    """圆形版：圆底 + 环 + 点（方砖的四角本来就要被圆切掉）。"""
    k = SS
    big = size * k
    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(canvas).ellipse([0, 0, big - 1, big - 1], fill=TILE)
    inner = int(big * 0.8)
    canvas.alpha_composite(
        _draw(False).resize((inner, inner), Image.LANCZOS),
        ((big - inner) // 2, (big - inner) // 2),
    )
    return canvas.resize((size, size), Image.LANCZOS)


def _foreground(size: int) -> Image.Image:
    """自适应图标的前景：108dp 画布，图形落在中央 72dp 的可见区。

    背景由 `values/ic_launcher_background.xml` 给（#F0F4F8，与砖同色），
    所以这里带着砖画也没有边界 —— 圆角那圈和背景融在一起。
    """
    k = SS
    big = size * k
    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    inner = int(big * 72 / 108)
    canvas.alpha_composite(
        _master().resize((inner, inner), Image.LANCZOS),
        ((big - inner) // 2, (big - inner) // 2),
    )
    return canvas.resize((size, size), Image.LANCZOS)


def android() -> int:
    """写 Android 的 launcher 图标与自适应前景。

    启动页（`activity_splash.xml`）读的是 `@mipmap/ic_launcher`，所以这一套换掉，
    桌面、任务切换、启动页三处就一起换了。
    """
    if not ANDROID_RES.is_dir():
        print(f"没有 Android 工程：{ANDROID_RES}（跳过）")
        return 0
    for dpi, size in MIPMAPS.items():
        folder = ANDROID_RES / f"mipmap-{dpi}"
        folder.mkdir(parents=True, exist_ok=True)
        _master().resize((size, size), Image.LANCZOS).save(
            folder / "ic_launcher.png", format="PNG", optimize=True
        )
        _round_icon(size).save(folder / "ic_launcher_round.png", format="PNG", optimize=True)
        _foreground(size * 108 // 48).save(
            folder / "ic_launcher_foreground.png", format="PNG", optimize=True
        )
    # `@drawable/splash` 只是 Activity 画出来之前的窗口底色（真正的启动页是
    # `activity_splash.xml`：底色 + `@mipmap/ic_launcher`）。原来这些 PNG 里有个
    # 模板残留的蓝色叉，这里抹成纯色，免得开屏闪一下陌生的图。
    for path in sorted(ANDROID_RES.glob("drawable-*/splash.png")):
        with Image.open(path) as old:
            size = old.size
        Image.new("RGBA", size, (247, 249, 251, 255)).save(
            path, format="PNG", optimize=True
        )
    print(f"Android launcher：{len(MIPMAPS)} 档（{', '.join(MIPMAPS)}）+ 自适应前景 + 窗口底色")
    return 0


def build() -> int:
    THEME_ASSETS.mkdir(parents=True, exist_ok=True)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    SVG_PATH.write_text(_svg(True), encoding="utf-8")
    print(f"矢量（带砖）：{SVG_PATH.relative_to(ROOT)}")
    MARK_SVG.write_text(_svg(False), encoding="utf-8")
    print(f"矢量（透明，标签页用）：{MARK_SVG.relative_to(ROOT)}")

    render(256).save(PNG_PATH, format="PNG", optimize=True)
    print(f"预览 PNG：{PNG_PATH.relative_to(ROOT)}（256×256，{PNG_PATH.stat().st_size / 1024:.1f} KB）")

    # ICO：Pillow 写的是 BMP 帧（32 位 BGRA），兼容性比"PNG 塞进 ICO"好
    render(max(ICO_SIZES)).save(
        ICO_PATH, format="ICO", sizes=[(size, size) for size in ICO_SIZES]
    )
    print(
        f"Icon：{ICO_PATH.relative_to(ROOT)}（{ICO_PATH.stat().st_size / 1024:.1f} KB，"
        f"含 {'/'.join(str(s) for s in ICO_SIZES)}）"
    )
    return 0


def check() -> int:
    problems: list[str] = []
    for path in (SVG_PATH, MARK_SVG, PNG_PATH, ICO_PATH):
        if not path.is_file():
            problems.append(f"缺 {path.relative_to(ROOT)}")
            continue
    if ICO_PATH.is_file():
        with Image.open(ICO_PATH) as ico:
            sizes = sorted(ico.ico.sizes())
        expected = [(size, size) for size in ICO_SIZES]
        if sizes != expected:
            problems.append(f"ICO 里的尺寸是 {sizes}，期望 {expected}")
    if PNG_PATH.is_file():
        with Image.open(PNG_PATH) as png:
            if png.size != (256, 256):
                problems.append(f"PNG 是 {png.size}，期望 (256, 256)")
    if ANDROID_RES.is_dir():
        for dpi, size in MIPMAPS.items():
            folder = ANDROID_RES / f"mipmap-{dpi}"
            expected = {
                "ic_launcher.png": size,
                "ic_launcher_round.png": size,
                "ic_launcher_foreground.png": size * 108 // 48,
            }
            for name, side in expected.items():
                path = folder / name
                if not path.is_file():
                    problems.append(f"缺 {path.relative_to(ROOT)}")
                    continue
                with Image.open(path) as img:
                    if img.size != (side, side):
                        problems.append(
                            f"{path.relative_to(ROOT)} 是 {img.size}，期望 {side}×{side}"
                        )
    if problems:
        print("图标检查没过：")
        for item in problems:
            print(f"  ✗ {item}")
        return 1
    print("图标齐全 ✓")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
    parser = argparse.ArgumentParser(prog="tools/make_icons.py", description="生成图标")
    parser.add_argument("--check", action="store_true", help="只检查产物")
    parser.add_argument("--android", action="store_true", help="同时写 Android 资源")
    args = parser.parse_args(argv)
    if args.check:
        return check()
    code = build()
    if args.android:
        code = android() or code
    return code


if __name__ == "__main__":
    raise SystemExit(main())
