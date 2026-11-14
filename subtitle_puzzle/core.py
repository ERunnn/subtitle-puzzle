"""台词拼图核心逻辑：只依赖 Pillow，不含任何 Qt 代码，可单独命令行测试。

设计要点
--------
* 输入是已经导出的台词图片，本模块不碰视频、不做 OCR。
* 框选矩形(CropRect)永远使用「原图像素坐标」，界面上显示的数值即原图数值。
* 实时预览时所有图片按同一个 project_scale 缩小，几何参数(间距/边距/输出尺寸)
  同步乘以该比例，保证「所见即所得」；导出时用原图 1:1 重新合成。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageOps

SUPPORTED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
PREVIEW_MAX_EDGE = 1400  # 项目统一的预览图最长边


# ---------------------------------------------------------------------------
# 改编自 colacao0909/subtitle-screenshot-stitcher（MIT License）
#   Copyright (c) 2026 colacao0909
#   原文：stack-subtitle-screenshots/scripts/stack_screenshots.py
#   改动：改为在内存图像上工作、返回矩形而非直接裁图、增加字幕行检测、
#        比例解析改为可直接喂给界面
# 完整许可声明见 THIRD_PARTY_NOTICES.md
# ---------------------------------------------------------------------------

def natural_key(value: str) -> list:
    """自然排序：frame_2 排在 frame_10 之前。"""
    return [int(part) if part.isdigit() else part.casefold()
            for part in re.split(r"(\d+)", value)]


def embedded_number(path: Path) -> int | None:
    """取文件名里最后一个数字：subtitle_001.png -> 1。"""
    matches = re.findall(r"\d+", path.stem)
    return int(matches[-1]) if matches else None


def parse_ratio(value: str) -> tuple[int, int]:
    """解析 '3:4' / '3/4' 形式的比例。"""
    match = re.fullmatch(r"\s*(\d+)\s*[:/]\s*(\d+)\s*", value)
    if not match:
        raise ValueError("比例格式应为 3:4")
    w, h = int(match.group(1)), int(match.group(2))
    if w <= 0 or h <= 0:
        raise ValueError("比例必须为正数")
    return w, h


def _axis_activity(gray: Image.Image, axis: str, threshold: int = 12,
                   fraction: float = 0.05) -> list[bool]:
    """逐列/逐行判断是否含有足够多的亮像素。"""
    pixels = gray.load()
    width, height = gray.size
    if axis == "x":
        required = max(1, math.ceil(height * fraction))
        return [sum(1 for y in range(height) if pixels[x, y] > threshold) >= required
                for x in range(width)]
    required = max(1, math.ceil(width * fraction))
    return [sum(1 for x in range(width) if pixels[x, y] > threshold) >= required
            for y in range(height)]


def _active_bounds(values: Sequence[bool]) -> tuple[int, int]:
    active = [i for i, v in enumerate(values) if v]
    if not active:
        return 0, len(values)
    return active[0], active[-1] + 1


def detect_content_box(images: Sequence[Image.Image],
                       samples: int = 3) -> tuple[float, float, float, float]:
    """自动检测有效内容框（去掉纯黑边），返回归一化 (x1, y1, x2, y2)。"""
    if not images:
        return 0.0, 0.0, 1.0, 1.0
    if len(images) <= samples:
        picks = list(range(len(images)))
    else:
        picks = sorted({0, len(images) // 2, len(images) - 1})
    boxes = []
    for index in picks:
        gray = images[index].convert("L")
        gray.thumbnail((900, 900), Image.Resampling.LANCZOS)
        left, right = _active_bounds(_axis_activity(gray, "x"))
        top, bottom = _active_bounds(_axis_activity(gray, "y"))
        boxes.append((left / gray.width, top / gray.height,
                      right / gray.width, bottom / gray.height))
    x1 = max(0.0, min(b[0] for b in boxes) - 0.002)
    y1 = max(0.0, min(b[1] for b in boxes) - 0.002)
    x2 = min(1.0, max(b[2] for b in boxes) + 0.002)
    y2 = min(1.0, max(b[3] for b in boxes) + 0.002)
    if x2 - x1 < 0.50 or y2 - y1 < 0.50:
        return 0.0, 0.0, 1.0, 1.0
    return x1, y1, x2, y2


def _row_features(band: Image.Image, mode: str, bright_threshold: int, delta: int,
                  min_ratio: float) -> tuple[list[bool], list[float]]:
    """逐行统计"明显发亮"的像素占比及其横向中心。

    absolute：灰度高于 bright_threshold（白字/黄字字幕）
    relative：比本行中位亮度高 delta（兜底，用于非纯白字幕）
    """
    pixels = band.load()
    bw, bh = band.size
    step = max(1, bw // 300)
    need = max(1, int(bw * min_ratio / step))
    rows: list[bool] = []
    centers: list[float] = []
    for y in range(bh):
        values = [pixels[x, y] for x in range(0, bw, step)]
        xs = list(range(0, bw, step))
        if mode == "absolute":
            bright = [x for x, v in zip(xs, values) if v > bright_threshold]
        else:
            median = sorted(values)[len(values) // 2]
            bright = [x for x, v in zip(xs, values) if v > median + delta]
        rows.append(len(bright) >= need)
        centers.append(sum(bright) / len(bright) if bright else -1.0)
    return rows, centers


def find_subtitle_band(image: Image.Image, region: tuple[float, float] = (0.40, 1.0),
                       bright_threshold: int = 160, delta: int = 45,
                       min_ratio: float = 0.02,
                       merge_gap: float = 0.05) -> tuple[int, int, int, int] | None:
    """在画面下部找字幕所在的行区间，返回 (x, y, w, h) 原图像素坐标。

    主判据是「接近白色的绝对亮度」——字幕字多是白/浅黄，画面再亮也够不着这个
    阈值；非纯白字幕时退回「比本行中位亮度高 delta」的相对判据。多个候选段里
    取「长度为主、横向居中为辅」的那一段。找不到时返回 None。
    """
    gray = image.convert("L")
    w, h = gray.size
    y_start = int(h * region[0])
    band = gray.crop((0, y_start, w, h))
    bw, bh = band.size
    gap = max(2, int(h * merge_gap))

    spans: list[list[int]] = []
    centers: list[float] = []
    for mode in ("absolute", "relative"):
        rows, centers = _row_features(band, mode, bright_threshold, delta, min_ratio)
        raw: list[list[int]] = []
        start = None
        for y, flag in enumerate(rows):
            if flag and start is None:
                start = y
            elif not flag and start is not None:
                raw.append([start, y])
                start = None
        if start is not None:
            raw.append([start, bh])
        if not raw:
            continue
        merged = [raw[0]]
        for span in raw[1:]:
            if span[0] - merged[-1][1] <= gap:
                merged[-1][1] = span[1]
            else:
                merged.append(list(span))
        spans = merged
        break

    if not spans:
        return None

    best = None
    for s, e in spans:
        cs = [centers[y] for y in range(s, e) if centers[y] >= 0]
        cx = sum(cs) / len(cs) if cs else 0.0
        centered = 1.0 - min(1.0, abs(cx - bw / 2) / (bw / 2))
        score = (e - s) * (0.5 + 0.5 * centered)
        if best is None or score > best[0]:
            best = (score, s, e)
    _, s, e = best
    top = y_start + s
    bottom = y_start + e
    if bottom - top < max(6, int(h * 0.01)):
        return None
    return (0, top, w, bottom - top)



# --------------------------------------------------------------------------- 数据模型

@dataclass
class CropRect:
    """原图像素坐标下的裁剪框。"""
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0

    def valid(self) -> bool:
        return self.w > 0 and self.h > 0

    def copy(self) -> "CropRect":
        return CropRect(self.x, self.y, self.w, self.h)

    def as_box(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.x + self.w, self.y + self.h

    def clamped(self, width: int, height: int) -> tuple["CropRect", bool]:
        """收缩到图片范围内，返回 (新框, 是否发生过裁剪)。"""
        x0 = max(0, min(self.x, width))
        y0 = max(0, min(self.y, height))
        x1 = max(0, min(self.x + self.w, width))
        y1 = max(0, min(self.y + self.h, height))
        out = CropRect(x0, y0, max(0, x1 - x0), max(0, y1 - y0))
        changed = (out.x, out.y, out.w, out.h) != (self.x, self.y, self.w, self.h)
        return out, changed


@dataclass
class Padding:
    """裁剪框向外扩展的边距（原图像素）。"""
    top: int = 0
    bottom: int = 0
    left: int = 0
    right: int = 0


@dataclass
class LayoutParams:
    direction: str = "vertical"          # vertical | horizontal
    gap: int = 0                        # 条目间距，默认 0 = 无黑缝
    margin: int = 0                     # 画布外边距
    align: str = "center"               # start | center | end
    padding: Padding = field(default_factory=Padding)
    uniform_width: bool = True          # 统一条带宽度（保持字幕横向对齐）
    uniform_height: bool = False        # 统一条带高度（补背景，不拉伸文字）
    stack_mode: str = "text"            # text | first_full | all_full
    hero_ratio: float = 0.75            # 保留画面时，字幕上方留多少画面（1=整幅原图）
    background: tuple[int, int, int] = (0, 0, 0)
    transparent: bool = False
    output_mode: str = "content"        # content | width | ratio | size
    output_width: int = 1080
    output_height: int = 1920
    ratio_w: int = 3
    ratio_h: int = 4
    orientation: str = "auto"        # landscape | portrait | auto（仅 expand 模式）
    items_per_page: int = 0          # 0 = 拼成一张；>0 = 每页张数，超出分页导出
    resample: str = "LANCZOS"           # NEAREST | BILINEAR | BICUBIC | LANCZOS


@dataclass
class ImageItem:
    """一张已导入的台词图片。"""
    path: Path
    full_size: tuple[int, int] = (0, 0)
    preview: Image.Image | None = None        # RGB，按 project_scale 缩小
    override: CropRect | None = None          # 逐张微调的独立框选
    load_error: str = ""                      # 读取失败原因

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class ComposeResult:
    image: Image.Image | None = None
    pages: list[Image.Image] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    content_size: tuple[int, int] = (0, 0)
    layout_size: tuple[int, int] = (0, 0)   # 未补边的内容画布尺寸（预览像素）
    ratio_note: str = ""                     # 扩展取景时的方向决策说明
    used_ratio: tuple[int, int] = (0, 0)

    @property
    def ok(self) -> bool:
        return self.image is not None and not self.error


# --------------------------------------------------------------------------- 图片加载

def probe_size(path: Path) -> tuple[int, int]:
    """只读文件头拿尺寸，不解码整张图。"""
    with Image.open(path) as im:
        return im.size


def project_scale(sizes: Sequence[tuple[int, int]]) -> float:
    """所有图片共用一个缩放比例，保证不同图之间的几何关系一致。"""
    longest = max((max(w, h) for w, h in sizes), default=0)
    if longest <= 0:
        return 1.0
    return min(1.0, PREVIEW_MAX_EDGE / longest)


def load_preview(path: Path, scale: float) -> Image.Image:
    """生成预览图。JPEG 用 draft() 走 DCT 快速缩放，避免解码全尺寸。"""
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        w, h = im.size
        target = (max(1, round(w * scale)), max(1, round(h * scale)))
        if scale < 1.0 and im.format == "JPEG":
            try:
                im.draft("RGB", target)
            except Exception:
                pass
        im = im.convert("RGB")
        if im.size != target:
            im = im.resize(target, Image.Resampling.LANCZOS)
        return im.copy()


def load_full(path: Path) -> Image.Image:
    """导出时读取原图，转成 RGBA 便于统一贴图。"""
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        return im.convert("RGBA").copy()


# --------------------------------------------------------------------------- 合成

def _resample(name: str):
    return getattr(Image.Resampling, name, Image.Resampling.LANCZOS)


def _bg(params: LayoutParams) -> tuple[int, int, int, int]:
    r, g, b = params.background
    return (r, g, b, 0 if params.transparent else 255)


def _crop_strip(item: ImageItem, rect: CropRect, params: LayoutParams,
                source: Image.Image, k: float, label: str,
                warnings: list[str]) -> Image.Image | None:
    """按原图坐标框选 + 边距，在 source(可能是缩略图) 上裁剪出一条台词。"""
    if not rect.valid():
        warnings.append(f"{label}：裁剪框为空，已跳过")
        return None

    sw, sh = source.size
    fw, fh = item.full_size
    if fw <= 0 or fh <= 0:
        warnings.append(f"{label}：图片尺寸无效，已跳过")
        return None

    clamped, changed = rect.clamped(fw, fh)
    if changed:
        warnings.append(f"{label}：裁剪框超出图片边界，已自动收缩")
    if not clamped.valid():
        warnings.append(f"{label}：裁剪框完全在图片外，已跳过")
        return None

    p = params.padding
    x0 = (clamped.x - p.left) * k
    y0 = (clamped.y - p.top) * k
    x1 = (clamped.x + clamped.w + p.right) * k
    y1 = (clamped.y + clamped.h + p.bottom) * k

    x0 = max(0, int(round(x0)))
    y0 = max(0, int(round(y0)))
    x1 = min(sw, int(round(x1)))
    y1 = min(sh, int(round(y1)))
    if x1 <= x0 or y1 <= y0:
        warnings.append(f"{label}：裁剪后尺寸为 0，已跳过")
        return None
    return source.crop((x0, y0, x1, y1)).convert("RGBA")


def _fit_canvas(canvas: Image.Image, params: LayoutParams, g: float) -> Image.Image:
    """按输出方式处理画布：宽度 / 比例 / 固定尺寸。"""
    mode = params.output_mode
    bg = _bg(params)
    res = _resample(params.resample)
    w, h = canvas.size

    if mode == "expand":
        return canvas

    if mode == "width":
        tw = max(1, round(params.output_width * g))
        th = max(1, round(h * tw / w))
        return canvas.resize((tw, th), res)

    if mode == "ratio":
        rw = max(1, params.ratio_w)
        rh = max(1, params.ratio_h)
        if w / h < rw / rh:
            nw, nh = max(w, round(h * rw / rh)), h
        else:
            nw, nh = w, max(h, round(w * rh / rw))
        if (nw, nh) == (w, h):
            return canvas
        out = Image.new("RGBA", (nw, nh), bg)
        out.alpha_composite(canvas, ((nw - w) // 2, (nh - h) // 2))
        return out

    if mode == "size":
        tw = max(1, round(params.output_width * g))
        th = max(1, round(params.output_height * g))
        factor = min(tw / w, th / h)
        nw, nh = max(1, round(w * factor)), max(1, round(h * factor))
        fitted = canvas.resize((nw, nh), res) if (nw, nh) != (w, h) else canvas
        out = Image.new("RGBA", (tw, th), bg)
        out.alpha_composite(fitted, ((tw - nw) // 2, (th - nh) // 2))
        return out

    return canvas  # content


def canvas_fill(layout_size: tuple[int, int], out_size: tuple[int, int]) -> float:
    """补边占最终画布的面积比例（0~1）；两个尺寸需处于同一缩放级别。"""
    lw, lh = layout_size
    ow, oh = out_size
    if lw <= 0 or lh <= 0 or ow <= 0 or oh <= 0:
        return 0.0
    return max(0.0, min(1.0, 1.0 - (lw * lh) / (ow * oh)))


def _frame_strip(source: Image.Image, ratio: float,
                 rect: CropRect | None = None, k: float = 1.0) -> Image.Image:
    """保留画面：字幕上方保留 ratio 比例的画面，一直取到字幕底边（字幕本身不会被切）。

    * ratio = 1.0 → 整幅原图（从顶部开始）；
    * ratio = 0.0 → 只剩字幕那一条（等价纯台词模式）；
    * 没有有效字幕框时，退回老行为「取图片顶部 ratio 高度」。
    """
    sw, sh = source.size
    r = max(0.0, min(1.0, ratio))
    if rect is not None and rect.valid():
        top = max(0, min(sh - 1, round(rect.y * k)))
        bot = max(top + 1, min(sh, round((rect.y + rect.h) * k)))
        y0 = round(top * (1.0 - r))       # 只裁字幕上方的画面，字幕永远保留
        return source.crop((0, y0, sw, bot)).convert("RGBA")
    crop_h = max(1, round(sh * r))
    return source.crop((0, 0, sw, crop_h)).convert("RGBA")


def _oriented_ratio(params: LayoutParams) -> tuple[int, int]:
    """按横屏 / 竖屏选项返回 (宽比, 高比)。"""
    rw, rh = max(1, params.ratio_w), max(1, params.ratio_h)
    if params.orientation == "portrait" and rw > rh:
        return rh, rw
    if params.orientation == "landscape" and rh > rw:
        return rh, rw
    return rw, rh


def _expand_plan(source_w: int, n: int, band_h: float,
                 params: LayoutParams, g: float) -> tuple[int, tuple[int, int], str]:
    """反推每条条带的目标高度：从原图多取画面，而不是补黑边。

    返回 (目标高, 实际比例, 说明)。目标高为 0 表示当前比例连字幕都放不下，
    应退回按内容自适应。
    """
    rw, rh = _oriented_ratio(params)

    def fits(r: tuple[int, int]) -> bool:
        total = source_w * r[1] / r[0]
        usable = total - round(params.gap * g) * (n - 1) - round(params.margin * g) * 2
        return usable / n >= band_h

    if not fits((rw, rh)):
        flipped = (rh, rw)
        if params.orientation == "auto" and fits(flipped):
            rw, rh = flipped
            note = "台词较多，已自动改为竖屏"
        else:
            return 0, (rw, rh), "当前比例放不下，已按内容自适应"
    else:
        note = ""
    total = source_w * rh / rw
    usable = total - round(params.gap * g) * (n - 1) - round(params.margin * g) * 2
    return max(1, round(usable / n)), (rw, rh), note


def _expand_strip(source: Image.Image, rect: CropRect, k: float,
                  target_h: int, bg: tuple[int, int, int, int]) -> Image.Image:
    """以字幕框为中心向外扩展，从原图多取画面凑到 target_h。

    触到原图上下边界时把余量补给另一侧；字幕始终完整。
    原图本身不够高时不足部分补背景，不缩放、不拉伸文字。
    """
    sw, sh = source.size
    top = max(0, min(sh - 1, round(rect.y * k)))
    bot = max(top + 1, min(sh, round((rect.y + rect.h) * k)))
    band_h = bot - top
    target_h = max(band_h, int(target_h))
    y0 = top - (target_h - band_h) // 2
    y1 = y0 + target_h
    if y0 < 0:
        y1 -= y0
        y0 = 0
    if y1 > sh:
        y0 -= (y1 - sh)
        y1 = sh
    y0, y1 = max(0, y0), min(sh, y1)
    strip = source.crop((0, y0, sw, y1)).convert("RGBA")
    if strip.height < target_h:
        layer = Image.new("RGBA", (sw, target_h), bg)
        layer.alpha_composite(strip, (0, (target_h - strip.height) // 2))
        return layer
    return strip


def _layout(strips: list[Image.Image], params: LayoutParams,
            g: float) -> tuple[Image.Image, tuple[int, int]]:
    """把若干条带拼到一张画布上，返回 (画布, 画布尺寸)。"""
    gap = max(0, round(params.gap * g))
    margin = max(0, round(params.margin * g))
    bg = _bg(params)
    vertical = params.direction == "vertical"
    n = len(strips)
    if vertical:
        content_w = max(s.width for s in strips)
        content_h = sum(s.height for s in strips) + gap * (n - 1)
    else:
        content_w = sum(s.width for s in strips) + gap * (n - 1)
        content_h = max(s.height for s in strips)
    size = (content_w + 2 * margin, content_h + 2 * margin)

    canvas = Image.new("RGBA", size, bg)
    cursor = margin
    for s in strips:
        if params.align == "start":
            off = margin
        elif params.align == "end":
            off = margin + (content_w if vertical else content_h) - (
                s.width if vertical else s.height)
        else:
            off = margin + ((content_w if vertical else content_h) - (
                s.width if vertical else s.height)) // 2
        off = max(margin, off)
        if vertical:
            canvas.alpha_composite(s, (off, cursor))
            cursor += s.height + gap
        else:
            canvas.alpha_composite(s, (cursor, off))
            cursor += s.width + gap
    return canvas, size


def compose(items: Sequence[ImageItem], global_rect: CropRect,
            params: LayoutParams, full_res: bool = False,
            scale: float = 1.0, items_per_page: int | None = None) -> ComposeResult:
    """按 items 当前顺序裁剪并拼接。

    full_res=False 使用预览缩略图（快，实时预览）；True 从磁盘读原图（导出）。
    scale 为项目统一的预览缩放比例，几何参数会乘它，保证预览/导出比例一致。
    items_per_page 为 None 时取 params.items_per_page；>0 时按每页张数分页输出
    （末页只剩 1 张会自动借一张过来）。
    """
    result = ComposeResult()
    if not items:
        result.error = "尚未导入图片：请先点击「导入图片」选择台词截图。"
        return result

    usable = [it for it in items if not it.load_error]
    for it in items:
        if it.load_error:
            result.warnings.append(f"{it.name}：读取失败（{it.load_error}），已跳过")
    if not usable:
        result.error = "所有图片都读取失败，请检查文件是否损坏或格式是否受支持。"
        return result

    rects = [it.override if it.override is not None else global_rect for it in usable]
    if not any(r.valid() for r in rects):
        result.error = "尚未框选台词区域：请在上方图片上按住鼠标左键拖出一个矩形。"
        return result

    g = 1.0 if full_res else scale
    bg = _bg(params)
    res = _resample(params.resample)

    strips: list[Image.Image] = []
    mode = params.stack_mode
    expand_mode = params.output_mode == "expand"
    target_h: int | None = None
    for i, (it, rect) in enumerate(zip(usable, rects)):
        label = f"第 {i + 1} 张 {it.name}"
        try:
            if full_res:
                source = load_full(it.path)
                k = 1.0
            else:
                source = it.preview
                if source is None:
                    result.warnings.append(f"{label}：预览图缺失，已跳过")
                    continue
                k = source.width / max(1, it.full_size[0])
            if expand_mode:
                if target_h is None:
                    target_h, used, note = _expand_plan(
                        source.width, len(usable), rect.h * k, params, g)
                    result.used_ratio, result.ratio_note = used, note
                if target_h > 0:
                    strips.append(_expand_strip(source, rect, k, target_h, bg))
                else:
                    fallback = _crop_strip(it, rect, params, source, k,
                                           label, result.warnings)
                    if fallback is not None:
                        strips.append(fallback)
                continue
            keep_frame = (mode == "all_full") or (mode == "first_full" and i == 0)
            if keep_frame:
                strip = _frame_strip(source, params.hero_ratio, rect, k)
            else:
                strip = _crop_strip(it, rect, params, source, k, label, result.warnings)
            if strip is not None:
                strips.append(strip)
        except Exception as exc:  # 单张失败不影响整批
            result.warnings.append(f"{label}：裁剪失败（{exc}），已跳过")

    if not strips:
        result.error = "没有一条台词成功裁剪：请检查框选位置与尺寸是否落在图片内。"
        return result

    # 统一条带宽度：缩放到最宽的一条（同源截图通常宽度本来就一致）
    if params.uniform_width:
        target_w = max(s.width for s in strips)
        strips = [
            s if s.width == target_w else s.resize(
                (target_w, max(1, round(s.height * target_w / s.width))), res)
            for s in strips
        ]

    # 统一条带高度：上下补背景，不拉伸文字
    if params.uniform_height:
        target_h = max(s.height for s in strips)
        padded = []
        for s in strips:
            if s.height == target_h:
                padded.append(s)
            else:
                layer = Image.new("RGBA", (s.width, target_h), bg)
                layer.alpha_composite(s, (0, (target_h - s.height) // 2))
                padded.append(layer)
        strips = padded

    # 分页（借鉴 MIT 项目的末页平衡：末页只剩 1 张时从上一页借一张）
    per_page = params.items_per_page if items_per_page is None else items_per_page
    groups: list[list[Image.Image]] = [strips]
    if per_page > 0 and len(strips) > per_page:
        groups = [strips[i:i + per_page]
                  for i in range(0, len(strips), per_page)]
        if len(groups) > 1 and len(groups[-1]) == 1 and len(groups[-2]) > 2:
            groups[-1].insert(0, groups[-2].pop())

    for grp in groups:
        canvas, size = _layout(grp, params, g)
        if not result.pages:
            result.layout_size = size
        result.pages.append(_fit_canvas(canvas, params, g))
    result.image = result.pages[0]
    result.content_size = result.image.size
    return result


# --------------------------------------------------------------------------- 导出

FORMAT_EXT = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}


def render_filename(template: str, items: Sequence[ImageItem], size: tuple[int, int],
                    seq: int, page: int = 1) -> str:
    """展开文件名占位符。"""
    from datetime import datetime
    now = datetime.now()
    values = {
        "date": now.strftime("%Y%m%d"),
        "time": now.strftime("%H%M%S"),
        "ts": now.strftime("%Y%m%d_%H%M%S"),
        "n": str(seq),
        "page": f"{page:02d}",
        "count": str(len(items)),
        "name": items[0].path.stem if items else "puzzle",
        "w": str(size[0]),
        "h": str(size[1]),
    }
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    # 清掉残留的未知占位符与非法文件名字符
    import re
    out = re.sub(r"\{[^}]*\}", "", out)
    out = re.sub(r'[\\/:*?"<>|]', "_", out).strip().strip(".")
    return out or "台词拼图"


def resolve_export_path(directory: Path, template: str, fmt: str,
                        items: Sequence[ImageItem], size: tuple[int, int]) -> Path:
    """文件名模板含 {n} 时自动找第一个空位；否则遇重名追加 _2/_3。"""
    ext = FORMAT_EXT.get(fmt, ".png")
    if "{n}" in template:
        for seq in range(1, 10000):
            stem = render_filename(template, items, size, seq)
            candidate = directory / (stem + ext)
            if not candidate.exists():
                return candidate
    stem = render_filename(template, items, size, 1)
    candidate = directory / (stem + ext)
    if not candidate.exists():
        return candidate
    for i in range(2, 10000):
        candidate = directory / f"{stem}_{i}{ext}"
        if not candidate.exists():
            return candidate
    return directory / f"{stem}_{id(items)}{ext}"


def resolve_export_paths(directory: Path, template: str, fmt: str,
                         items: Sequence[ImageItem], size: tuple[int, int],
                         page_count: int = 1) -> list[Path]:
    """按页数算出要写出的文件路径。

    单页：沿用 {n} 自动避让重名。
    多页：模板里可用 {page}；没写就自动追加 _01 / _02 后缀。
    """
    ext = FORMAT_EXT.get(fmt, ".png")
    if page_count <= 1:
        return [resolve_export_path(directory, template, fmt, items, size)]

    paths: list[Path] = []
    for page in range(1, page_count + 1):
        stem = render_filename(template, items, size, 1, page=page)
        if "{page}" not in template:
            stem = f"{stem}_{page:02d}"
        candidate = directory / (stem + ext)
        if candidate.exists():
            for i in range(2, 10000):
                alt = directory / f"{stem}_{i}{ext}"
                if not alt.exists():
                    candidate = alt
                    break
        paths.append(candidate)
    return paths


def save_image(image: Image.Image, path: Path, fmt: str, params: LayoutParams,
               quality: int = 95) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "JPEG" or (fmt == "WEBP" and image.mode == "RGBA" and not params.transparent):
        flat = Image.new("RGB", image.size, params.background)
        flat.paste(image, mask=image.getchannel("A"))
        out = flat
    else:
        out = image
    if fmt == "PNG":
        out.save(path, "PNG", optimize=True)
    elif fmt == "JPEG":
        out.save(path, "JPEG", quality=quality, subsampling=0, optimize=True)
    else:
        out.save(path, "WEBP", quality=quality, method=4)
