"""生成台词匠的 ico 文件（打包 exe 用）。运行：python subtitle_puzzle/make_icon.py

图形与 app.py 里的 make_app_icon() 一致：深色圆角底 + 三条堆叠横条。
"""
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 256


def build_image() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = SIZE
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=round(s * 0.12),
                        fill=(21, 21, 21, 255))
    bar = s * 0.10
    gap = s * 0.075
    left = s * 0.24
    widths = (0.52, 0.40, 0.28)
    colors = ((76, 194, 255, 255), (255, 255, 255, 235), (255, 255, 255, 150))
    total = 3 * bar + 2 * gap
    y = (s - total) / 2
    for width_factor, color in zip(widths, colors):
        w = s * width_factor
        d.rounded_rectangle([left, y, left + w, y + bar], radius=bar / 2, fill=color)
        y += bar + gap
    return img


def main():
    out = Path(__file__).resolve().parent / "icon.ico"
    build_image().save(out, sizes=[(16, 16), (32, 32), (48, 48), (64, 64),
                                   (128, 128), (256, 256)])
    png = out.with_suffix(".png")
    build_image().save(png)
    print("已生成:", out, "和", png)


if __name__ == "__main__":
    main()
