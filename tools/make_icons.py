#!/usr/bin/env python3
"""生成 QuizForge 的图标：浏览器 favicon（SVG）· 预览 PNG · Windows exe 的 `.ico`。

## 为什么手写而不是找现成的

仓库里**没有**任何图像库（PIL / cairosvg / wand 实测都没有），而图标是分发的一部分：
Windows 上双击 exe 看到的那个图标、浏览器标签页上那个小图标，都在这里。

三样东西各自都很简单，用标准库就够：

* **SVG** —— 矢量，现代浏览器的 favicon 直接吃它（清晰且体积极小）；
* **PNG** —— 用 `zlib` + `struct` 手写（几十行，没有依赖）；
* **ICO** —— 微软的容器格式，每个尺寸塞一张 **BMP**（32 位 BGRA，自下而上）；
  比"PNG 塞进 ICO"兼容得多（后者要 Vista+，小尺寸还容易出问题）。

## 图形本身

一个圆角方砖（品牌青绿渐变）+ 白色的 **Q**（圆环 + 斜尾）——
和顶栏、对话头像用的是**同一个字形**（`theme/shell.html` 与 `chat.js` 的 `logoMark()`
两处同源，改的时候要一起改）。16px 下也要认得出，所以线条刻意做粗。

用法：

    python3 tools/make_icons.py            # 生成全套
    python3 tools/make_icons.py --check    # 只检查产物在不在、尺寸对不对
"""

from __future__ import annotations

import argparse
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
THEME_ASSETS = ROOT / "theme" / "assets"
BUILD_DIR = ROOT / "build"
ICO_PATH = BUILD_DIR / "icon.ico"
SVG_PATH = THEME_ASSETS / "icon.svg"
PNG_PATH = THEME_ASSETS / "icon-256.png"

#: 品牌色（与 app.css 的令牌一致：青绿）
TOP = (45, 212, 191)      # #2DD4BF
BOTTOM = (20, 184, 166)   # #14B8A6
INK = (255, 255, 255)

#: ICO 里要放的尺寸（Windows 会按场景挑：任务栏 32/48、资源管理器 16/32/256）
ICO_SIZES = (16, 32, 48, 64, 128, 256)

#: 超采样倍数：先把母版画大再缩，边缘才不会有锯齿
SS = 4
MASTER = 256 * SS

# --- 几何：全部用 0..1 的归一化坐标，乘上尺寸即可 -----------------------------
TILE_RADIUS = 0.22
RING_CENTER = (0.5, 0.5)
RING_OUTER = 0.305
RING_INNER = 0.185
TAIL_FROM = (0.60, 0.60)
TAIL_TO = (0.80, 0.80)
TAIL_WIDTH = 0.115


def _distance_to_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-12:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    cx, cy = ax + t * dx, ay + t * dy
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5


def _inside_tile(x: float, y: float) -> bool:
    """圆角方砖：中心区域 + 四角按圆角判断。"""
    r = TILE_RADIUS
    cx = min(max(x, r), 1.0 - r)
    cy = min(max(y, r), 1.0 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _color(x: float, y: float) -> tuple[int, int, int]:
    """底色：沿左上 → 右下的线性渐变。"""
    t = max(0.0, min(1.0, (x + y) / 2.0))
    return tuple(round(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3))  # type: ignore[return-value]


def _sample(x: float, y: float) -> tuple[int, int, int, int]:
    """一个采样点的颜色（含 alpha）。在归一化坐标上算。"""
    if not _inside_tile(x, y):
        return (0, 0, 0, 0)

    dist_center = ((x - RING_CENTER[0]) ** 2 + (y - RING_CENTER[1]) ** 2) ** 0.5
    on_ring = RING_INNER <= dist_center <= RING_OUTER
    on_tail = (
        _distance_to_segment(x, y, *TAIL_FROM, *TAIL_TO) <= TAIL_WIDTH / 2
        or _distance_to_segment(x, y, *TAIL_TO, *TAIL_TO) <= TAIL_WIDTH / 2
    )
    if on_ring or on_tail:
        return (*INK, 255)
    return (*_color(x, y), 255)


def render(size: int) -> list[list[tuple[int, int, int, int]]]:
    """画一张 size×size 的图（从母版降采样，边缘平滑）。"""
    step = MASTER / size
    rows: list[list[tuple[int, int, int, int]]] = []
    for row in range(size):
        line: list[tuple[int, int, int, int]] = []
        for col in range(size):
            r = g = b = a = 0
            for sy in range(SS):
                for sx in range(SS):
                    mx = (col * SS + sx + 0.5) / MASTER
                    my = (row * SS + sy + 0.5) / MASTER
                    sr, sg, sb, sa = _sample(mx, my)
                    # 预乘再平均：不然透明边缘会把颜色拉黑
                    r += sr * sa
                    g += sg * sa
                    b += sb * sa
                    a += sa
            n = SS * SS
            if a == 0:
                line.append((0, 0, 0, 0))
            else:
                line.append((round(r / a), round(g / a), round(b / a), round(a / n)))
        rows.append(line)
    return rows


# ------------------------------------------------------------------ PNG / ICO


def _png_bytes(rows: list[list[tuple[int, int, int, int]]]) -> bytes:
    """手写 PNG（无依赖）：8 位 RGBA + filter 0 + zlib。"""
    height = len(rows)
    width = len(rows[0])
    raw = bytearray()
    for line in rows:
        raw.append(0)
        for r, g, b, a in line:
            raw += bytes((r, g, b, a))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def _bmp_entry(rows: list[list[tuple[int, int, int, int]]]) -> bytes:
    """ICO 里的一张图：BITMAPINFOHEADER + BGRA（自下而上）+ AND 掩码。"""
    height = len(rows)
    width = len(rows[0])
    header = struct.pack(
        "<IiiHHIIiiII",
        40,          # biSize
        width,
        height * 2,  # XOR + AND 两张图叠在一起算高度
        1, 32, 0,    # planes, bitcount, compression
        0, 0, 0, 0, 0,
    )
    pixels = bytearray()
    for line in reversed(rows):  # BMP 自下而上
        for r, g, b, a in line:
            pixels += bytes((b, g, r, a))
    mask_row = ((width + 31) // 32) * 4  # 每行按 4 字节对齐
    return header + bytes(pixels) + bytes(mask_row * height)


def _ico_bytes(images: dict[int, list[list[tuple[int, int, int, int]]]]) -> bytes:
    entries = []
    blobs = []
    offset = 6 + 16 * len(images)
    for size in sorted(images):
        blob = _bmp_entry(images[size])
        entries.append(
            struct.pack(
                "<BBBBHHII",
                0 if size >= 256 else size,   # 256 用 0 表示
                0 if size >= 256 else size,
                0, 0, 1, 32, len(blob), offset,
            )
        )
        blobs.append(blob)
        offset += len(blob)
    return struct.pack("<HHH", 0, 1, len(images)) + b"".join(entries) + b"".join(blobs)


# --------------------------------------------------------------------- SVG


def _svg() -> str:
    """矢量版：与上面同一套几何（顶栏和对话头像用它的**线条版**，见 logoMark）。"""
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" role="img"
     aria-label="QuizForge">
  <defs>
    <linearGradient id="tile" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#2DD4BF"/>
      <stop offset="1" stop-color="#14B8A6"/>
    </linearGradient>
  </defs>
  <rect x="0" y="0" width="256" height="256" rx="{round(256 * TILE_RADIUS)}" fill="url(#tile)"/>
  <circle cx="{round(256 * RING_CENTER[0])}" cy="{round(256 * RING_CENTER[1])}"
          r="{round(256 * (RING_OUTER + RING_INNER) / 2)}" fill="none" stroke="#FFFFFF"
          stroke-width="{round(256 * (RING_OUTER - RING_INNER))}"/>
  <path d="M{round(256 * TAIL_FROM[0])} {round(256 * TAIL_FROM[1])}
           L{round(256 * TAIL_TO[0])} {round(256 * TAIL_TO[1])}" stroke="#FFFFFF"
        stroke-width="{round(256 * TAIL_WIDTH)}" stroke-linecap="round"/>
</svg>
"""


def build() -> int:
    THEME_ASSETS.mkdir(parents=True, exist_ok=True)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    SVG_PATH.write_text(_svg(), encoding="utf-8")
    print(f"矢量：{SVG_PATH.relative_to(ROOT)}")

    images = {size: render(size) for size in ICO_SIZES}
    PNG_PATH.write_bytes(_png_bytes(images[256]))
    print(f"预览 PNG：{PNG_PATH.relative_to(ROOT)}（256×256，{PNG_PATH.stat().st_size / 1024:.1f} KB）")

    ICO_PATH.write_bytes(_ico_bytes(images))
    print(
        f"Icon：{ICO_PATH.relative_to(ROOT)}（{ICO_PATH.stat().st_size / 1024:.1f} KB，"
        f"含 {'/'.join(str(s) for s in ICO_SIZES)}）"
    )
    return 0


def check() -> int:
    problems: list[str] = []
    for path in (SVG_PATH, PNG_PATH, ICO_PATH):
        if not path.is_file():
            problems.append(f"缺 {path.relative_to(ROOT)}")
    if ICO_PATH.is_file():
        head = ICO_PATH.read_bytes()[:6]
        reserved, kind, count = struct.unpack("<HHH", head)
        if (reserved, kind) != (0, 1):
            problems.append("ICO 头不对")
        elif count != len(ICO_SIZES):
            problems.append(f"ICO 里有 {count} 张图，期望 {len(ICO_SIZES)}")
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
    args = parser.parse_args(argv)
    return check() if args.check else build()


if __name__ == "__main__":
    raise SystemExit(main())
