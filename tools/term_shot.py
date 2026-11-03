"""把终端输出渲染成 macOS 风格窗口截图（PNG），用于文档配图。

用法：
    python tools/term_shot.py --title "标题" --input out.txt --output docs/img/x.png
    cat out.txt | python tools/term_shot.py --title "标题" --output docs/img/x.png

不依赖第三方库（只用 Pillow，随 harness 环境自带）。
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# 配色（VS Code 暗色）
BG = (30, 30, 30)
BAR = (50, 50, 52)
FG = (212, 212, 212)
DIM = (128, 128, 128)
GREEN = (106, 190, 106)
YELLOW = (219, 183, 106)
RED = (220, 110, 110)
BLUE = (110, 160, 220)

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\consola.ttf",
    r"C:\Windows\Fonts\lucon.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]
# 中文字体（与等宽拉丁字体混排，避免中文变成方块）
CJK_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\NotoSansSC-VF.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
]

MAX_COLS = 104
FONT_SIZE = 15
LINE_H = 22
PAD = 22
BAR_H = 34


def is_wide(ch: str) -> bool:
    """CJK / 全角字符按两列宽计。"""
    o = ord(ch)
    return (0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0xA4CF or
            0xAC00 <= o <= 0xD7A3 or 0xF900 <= o <= 0xFAFF or
            0xFE30 <= o <= 0xFE6F or 0xFF00 <= o <= 0xFF60 or
            0xFFE0 <= o <= 0xFFE6 or 0x1F300 <= o <= 0x1FAFF)


def display_width(s: str) -> int:
    return sum(2 if is_wide(c) else 1 for c in s)


def wrap_display(s: str, limit: int) -> list[str]:
    """按显示宽度换行（中文算两列）。"""
    out, cur, w = [], [], 0
    for ch in s:
        cw = 2 if is_wide(ch) else 1
        if w + cw > limit and cur:
            out.append("".join(cur))
            cur, w = [ch], cw
        else:
            cur.append(ch)
            w += cw
    out.append("".join(cur))
    return out


def pick_font(paths: list[str]) -> ImageFont.FreeTypeFont:
    for path in paths:
        if Path(path).exists():
            return ImageFont.truetype(path, FONT_SIZE)
    return ImageFont.load_default()


def draw_mixed(d, x: int, y: int, text: str, latin, cjk, color) -> None:
    """拉丁/中文分段绘制，避免中文字形缺失。"""
    i = 0
    while i < len(text):
        wide = is_wide(text[i])
        j = i
        while j < len(text) and is_wide(text[j]) == wide:
            j += 1
        seg = text[i:j]
        font = cjk if wide else latin
        d.text((x, y), seg, font=font, fill=color)
        x += d.textlength(seg, font=font)
        i = j


def color_for(line: str) -> tuple[int, int, int]:
    s = line.strip()
    if s.startswith(("$", "PS>", "❯")):
        return GREEN
    if s.startswith(("#", "//")):
        return DIM
    if s.startswith(("✓", "✔", "OK", "resolved", "Loaded", "完成", "成功")):
        return GREEN
    if s.startswith(("✗", "✘", "ERROR", "FAILED", "error", "失败")):
        return RED
    if s.startswith(("!", "⚠", "WARN", "warn")):
        return YELLOW
    if s.startswith(("|", "+---", "|---")):
        return BLUE
    return FG


def render(title: str, lines: list[str], out: Path) -> None:
    wrapped: list[tuple[str, tuple[int, int, int]]] = []
    for raw in lines:
        raw = raw.rstrip("\n").replace("\t", "    ")
        if not raw:
            wrapped.append(("", FG))
            continue
        for c in wrap_display(raw, MAX_COLS):
            wrapped.append((c, color_for(raw)))

    latin = pick_font(FONT_CANDIDATES)
    cjk = pick_font(CJK_FONT_CANDIDATES)
    col_w = latin.getlength("M")
    width = PAD * 2 + int(col_w * MAX_COLS) + 20
    height = BAR_H + PAD + len(wrapped) * LINE_H + PAD

    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    # 标题栏
    d.rectangle([0, 0, width, BAR_H], fill=BAR)
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([16 + i * 22, 11, 16 + i * 22 + 12, 23], fill=c)
    if title:
        tw = d.textlength(title, font=cjk)
        draw_mixed(d, (width - tw) / 2, 8, title, latin, cjk, (205, 205, 205))

    y = BAR_H + PAD
    for text, color in wrapped:
        if text:
            draw_mixed(d, PAD, y, text, latin, cjk, color)
        y += LINE_H

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG")
    print(f"→ {out}  ({width}x{height})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="")
    ap.add_argument("--input", default=None, help="文本文件；省略则读 stdin")
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    if args.input:
        lines = Path(args.input).read_text(encoding="utf-8", errors="replace").splitlines()
    else:
        data = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        lines = data.splitlines()
    render(args.title, lines, Path(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
