"""台词拼图自检：生成示例台词截图 → 验证核心逻辑 → 离屏启动一次 GUI。

运行：python selftest.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from subtitle_puzzle.core import (
    CropRect, Padding, LayoutParams, ImageItem, probe_size, project_scale,
    load_preview, compose, save_image, resolve_export_path, resolve_export_paths,
    natural_key, embedded_number, parse_ratio, find_subtitle_band,
    detect_content_box, canvas_fill,
)

LINES = [
    ("我希望 你能见识到令你惊奇的事物", "I hope you see things that startle you."),
    ("我希望 你能体验未曾有过的情感", "I hope you feel things you never felt before."),
    ("我希望 你能遇见一些想法不同的人", "I hope you meet people with a different point of view."),
    ("我希望 你为你自己的人生感到骄傲", "I hope you live a life you're proud of."),
    ("如果你发现自己还没有做到 我希望 你有勇气", "If you find that you're not, I hope you have the strength"),
    ("从头再来", "- to start all over again."),
]
CJK = "C:/Windows/Fonts/msyh.ttc"
PASSED = 0


def check(cond: bool, label: str):
    global PASSED
    if not cond:
        raise AssertionError("FAIL: " + label)
    PASSED += 1
    print(f"  [OK] {label}")


def font(size: int):
    try:
        return ImageFont.truetype(CJK, size)
    except Exception:
        return ImageFont.load_default()


def make_samples(folder: Path) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, (cn, en) in enumerate(LINES, 1):
        w, h = 720, 1280
        im = Image.new("RGB", (w, h), (12, 14, 18))
        d = ImageDraw.Draw(im)
        # 上半部：模拟影片画面
        for y in range(360, 900):
            t = (y - 360) / 540
            d.line([(0, y), (w, y)],
                   fill=(int(26 + 46 * t), int(34 + 52 * t), int(48 + 66 * t)))
        d.ellipse((430, 430, 600, 600), fill=(86, 96, 108))
        d.polygon([(0, 900), (150, 700), (280, 830), (430, 640), (720, 900)],
                  fill=(20, 26, 32))
        d.rectangle((0, 900, w, h), fill=(0, 0, 0))
        f_cn, f_en = font(31), font(22)
        d.text((w // 2, 1025), cn, font=f_cn, fill=(238, 238, 238), anchor="mm")
        d.text((w // 2, 1080), en, font=f_en, fill=(228, 228, 228), anchor="mm")
        d.text((28, 28), f"SCENE {i:02d}", font=font(20), fill=(150, 150, 150))
        p = folder / f"台词_{i:02d}.png"
        im.save(p)
        paths.append(p)
    return paths


def build_items(paths: list[Path]) -> tuple[list[ImageItem], float]:
    sizes = [probe_size(p) for p in paths]
    s = project_scale(sizes)
    items = []
    for p, size in zip(paths, sizes):
        it = ImageItem(path=p, full_size=size)
        it.preview = load_preview(p, s)
        items.append(it)
    return items, s


def test_core(paths: list[Path], out: Path):
    print("\n[1] 核心逻辑")
    items, s = build_items(paths)
    check(len(items) == 6 and all(it.preview is not None for it in items), "批量导入 + 预览图生成")
    check(all(it.full_size == (720, 1280) for it in items), "读取原图尺寸")

    r = compose([], CropRect(), LayoutParams())
    check(not r.ok and "尚未导入" in r.error, "未导入图片 → 明确提示")

    r = compose(items, CropRect(), LayoutParams(), scale=s)
    check(not r.ok and "尚未框选" in r.error, "未框选区域 → 明确提示")

    rect = CropRect(0, 990, 720, 130)
    p = LayoutParams(gap=0)
    r = compose(items, rect, p, scale=s)
    check(r.ok, "默认间距 0 正常合成")
    h0 = r.image.height
    check(h0 == 130 * s * len(items) or abs(h0 - 130 * len(items) * s) <= 3,
          f"无间隙：高度 = 6 × 条带高（实测 {h0}）")
    check(not r.warnings, f"默认无裁剪告警（实测 {r.warnings}）")

    p2 = LayoutParams(gap=20)
    r2 = compose(items, rect, p2, scale=s)
    gap_px = round(20 * s)
    check(r2.image.height - h0 == 5 * gap_px,
          f"间距 20 → 增加 5×{gap_px}px 黑缝（实测 +{r2.image.height - h0}）")

    r3 = compose(items, rect, LayoutParams(direction="horizontal", gap=0), scale=s)
    check(r3.image.width == round(720 * s) * 6 and r3.image.height == round(130 * s),
          f"横向拼接 {r3.image.size}")

    r4 = compose(items, rect, LayoutParams(output_mode="ratio", ratio_w=3, ratio_h=4), scale=s)
    check(abs(r4.image.width / r4.image.height - 3 / 4) < 0.01,
          f"指定 3:4 比例输出 {r4.image.size}")
    check(r4.layout_size[0] == r4.image.width and r4.layout_size[1] < r4.image.height,
          f"3:4 保留未补边尺寸 {r4.layout_size} → {r4.image.size}")
    check(abs(canvas_fill((1920, 520), (1920, 2560)) - 0.797) < 0.002 and
          canvas_fill((100, 100), (100, 100)) == 0.0 and
          canvas_fill((0, 0), (100, 100)) == 0.0,
          "补边占比计算与零尺寸边界")

    r_exp = compose(items, rect, LayoutParams(output_mode="expand", ratio_w=3, ratio_h=4),
                    scale=s)
    check(r_exp.ok and abs(r_exp.image.width / r_exp.image.height - 3 / 4) < 0.02,
          f"扩展取景按比例输出 {r_exp.image.size}")
    check(r_exp.ok and canvas_fill(r_exp.layout_size, r_exp.image.size) < 0.001,
          "扩展取景零补边（画面来自原图，不是黑边）")
    r_auto = compose(items, rect, LayoutParams(output_mode="expand", ratio_w=4, ratio_h=3),
                     scale=s)
    check(r_auto.used_ratio == (3, 4) and "竖屏" in r_auto.ratio_note,
          f"横屏放不下自动转竖屏 {r_auto.used_ratio} {r_auto.ratio_note}")
    r_force = compose(items, rect, LayoutParams(output_mode="expand", ratio_w=4, ratio_h=3,
                                               orientation="landscape"), scale=s)
    check(r_force.ok and "自适应" in r_force.ratio_note,
          "强制横屏且放不下时退回按内容自适应")
    from PIL import ImageChops
    base = dict(output_mode="expand", ratio_w=4, ratio_h=3)
    r_center = compose(items, rect, LayoutParams(subtitle_pos="center", **base), scale=s)
    r_bottom = compose(items, rect, LayoutParams(subtitle_pos="bottom", **base), scale=s)
    check(ImageChops.difference(r_center.image.convert("RGB"),
                                r_bottom.image.convert("RGB")).getbbox() is not None,
          "基础「居中 / 偏下」产出不同画面")
    r_off = compose(items, rect, LayoutParams(subtitle_offset=40, **base), scale=s)
    check(ImageChops.difference(r_center.image.convert("RGB"),
                                r_off.image.convert("RGB")).getbbox() is not None,
          "高级像素微调能改变字幕位置")

    r5 = compose(items, rect, LayoutParams(output_mode="size", output_width=800,
                                           output_height=1200), scale=s)
    check(r5.image.size == (800, 1200), f"指定 800×1200 输出 {r5.image.size}")

    r6 = compose(items, rect, LayoutParams(align="start", margin=30,
                                           uniform_height=True), scale=s)
    check(r6.ok and r6.image.width == round(720 * s) + 2 * round(30 * s),
          "外边距 + 统一条带高度 + 居左对齐")

    # 逐张微调
    items[1].override = CropRect(0, 1000, 720, 100)
    r7 = compose(items, rect, LayoutParams(), scale=s)
    check(r7.ok, "逐张独立框选参与合成")
    items[1].override = None

    # 越界与空框
    r8 = compose(items, CropRect(600, 1200, 500, 500), LayoutParams(), scale=s)
    check(any("超出" in w for w in r8.warnings), "框选越界 → 自动收缩并给出告警")

    bad = [ImageItem(path=Path("missing.png"), full_size=(100, 100),
                     preview=Image.new("RGB", (100, 100)), load_error="文件不存在")]
    r9 = compose(bad, CropRect(0, 0, 50, 50), LayoutParams(), scale=1.0)
    check(any("读取失败" in w for w in r9.warnings), "读取失败 → 逐张列出原因")

    # 导出
    out.mkdir(exist_ok=True)
    target = resolve_export_path(out, "自检_{date}_{n}", "PNG", items, r.image.size)
    save_image(r.image, target, "PNG", p)
    check(target.exists() and target.stat().st_size > 1000, f"导出 PNG：{target.name}")
    jpg = Path(tempfile.gettempdir()) / "puzzle_selftest.jpg"
    save_image(r.image, jpg, "JPEG", LayoutParams(background=(255, 255, 255)), 90)
    check(jpg.exists() and jpg.stat().st_size > 1000, "导出 JPEG（透明通道正确扁平化）")
    target2 = resolve_export_path(out, "自检_{date}_{n}", "PNG", items, r.image.size)
    check(target2.name != target.name, "文件名 {n} 自动避让重名")

    # 大图缩放路径
    big_path = out / "big.png"
    Image.new("RGB", (1080, 2400), (20, 20, 20)).save(big_path)
    big, bs = build_items([big_path])
    check(bs < 1.0, f"大图触发预览缩放（scale={bs:.3f}）")
    rb = compose(big, CropRect(0, 2200, 1080, 150), LayoutParams(gap=20), scale=bs)
    check(rb.ok and rb.image.height == round(150 * bs), "缩放下裁剪尺寸正确")

    print("\n[1b] 从 MIT 项目吸收的能力")
    names = ["IMG_10.png", "IMG_9.png", "IMG_2.png"]
    check(sorted(names, key=natural_key) == ["IMG_2.png", "IMG_9.png", "IMG_10.png"],
          "自然排序：IMG_2 < IMG_9 < IMG_10")
    check(embedded_number(Path("subtitle_001.png")) == 1, "提取文件名中的编号")
    check(parse_ratio("3:4") == (3, 4) and parse_ratio("16 / 9") == (16, 9), "比例解析")

    with Image.open(paths[0]) as im0:
        band = find_subtitle_band(im0.convert("RGB"))
        check(band is not None and band[1] > im0.height * 0.6,
              f"自动定位到下半部分字幕行 → {band}")
        box = detect_content_box([im0.convert("RGB")])
        check(0.0 <= box[0] <= box[2] <= 1.0 and 0.0 <= box[1] <= box[3] <= 1.0,
              f"内容框检测返回合法归一化框 → {tuple(round(v, 3) for v in box)}")

    rp = compose(items, rect, LayoutParams(items_per_page=4), scale=s)
    check(len(rp.pages) == 2, f"每页 4 张 → 6 张分 2 页（实测 {len(rp.pages)}）")
    rp2 = compose(items, rect, LayoutParams(items_per_page=5), scale=s)
    heights = [p.height for p in rp2.pages]
    check(len(rp2.pages) == 2 and heights[0] > heights[1],
          f"末页只剩 1 张时自动借一张 → 各页高度 {heights}")
    multi = resolve_export_paths(out, "分页自检_{page}", "PNG", items, (720, 780), 3)
    check(len(multi) == 3 and all("{page}" not in str(p) for p in multi),
          f"多页导出文件名带页码 → {[p.name for p in multi]}")

    # 三种拼接模式
    r_text = compose(items, rect, LayoutParams(stack_mode="text"), scale=s)
    r_first = compose(items, rect, LayoutParams(stack_mode="first_full",
                                                 hero_ratio=0.5), scale=s)
    r_all = compose(items, rect, LayoutParams(stack_mode="all_full",
                                               hero_ratio=0.5), scale=s)
    check(r_text.image.height == 780, f"纯台词模式：6×130=780（实测 {r_text.image.height}）")
    # 保留画面：字幕上方留 50%，从那儿一直取到字幕底边（字幕不会被切）
    exp_frame = (rect.y + rect.h) - round(rect.y * 0.5)
    check(r_first.image.height == exp_frame + 5 * 130,
          f"首图保留画面：{exp_frame} + 5×130（实测 {r_first.image.height}）")
    check(exp_frame >= rect.h, f"首图条带仍包含自己的字幕（{exp_frame} ≥ {rect.h}）")
    check(r_all.image.height == 6 * exp_frame,
          f"每张保留画面：6×{exp_frame}（实测 {r_all.image.height}）")
    return items, s, rect


def test_capture(out: Path):
    print("\n[1b] 自动捕获核心（监屏抓帧 → 去重留清晰）")
    from subtitle_puzzle.capture import (
        CaptureConfig, band_signature, diff_ratio, sharpness,
        finalize_session, detect_band_region,
    )

    cfg = CaptureConfig()

    # 字幕行签名与差分
    im_a = Image.new("RGB", (720, 400), (8, 10, 14))
    ImageDraw.Draw(im_a).rectangle((90, 330, 630, 366), fill=(240, 240, 240))
    # 换一句字幕（更短、位置不同）→ 掩膜应显著不同
    im_b = Image.new("RGB", (720, 400), (8, 10, 14))
    ImageDraw.Draw(im_b).rectangle((220, 330, 500, 366), fill=(240, 240, 240))
    sig_a = band_signature(im_a, (0.82, 1.0), cfg.sig_size)
    sig_b = band_signature(im_b, (0.82, 1.0), cfg.sig_size)
    check(diff_ratio(sig_a, sig_a) == 0.0, "同一画面字幕行差分为 0")
    check(diff_ratio(sig_a, sig_b) > 0.2,
          f"字幕内容变化 → 差分显著（实测 {diff_ratio(sig_a, sig_b):.3f}）")
    # 同一句字幕、背景从亮变暗 → 掩膜应基本不变（列中位数抵消静态背景）
    im_a2 = im_a.copy()
    ImageDraw.Draw(im_a2).rectangle((0, 300, 720, 400), fill=(170, 170, 170))
    im_b2 = im_b.copy()
    ImageDraw.Draw(im_b2).rectangle((0, 300, 720, 400), fill=(20, 20, 20))
    d_same = diff_ratio(band_signature(im_a2, (0.82, 1.0), cfg.sig_size),
                        band_signature(im_b2, (0.82, 1.0), cfg.sig_size))
    check(d_same < 0.15,
          f"同句字幕跨背景变化 → 差分仍很小（实测 {d_same:.3f}，去重不再误判）")

    # 锐度度量：清晰帧高于模糊帧
    check(sharpness(im_a) > sharpness(im_a.filter(ImageFilter.GaussianBlur(3))),
          "锐度：清晰帧 > 模糊帧")

    # 首帧自动定位字幕行
    region = detect_band_region(im_a)
    check(0.7 <= region[0] < region[1] <= 1.0,
          f"首帧自动定位字幕行 → ({region[0]:.3f}, {region[1]:.3f})")

    # 会话去重：3 句白字字幕 × 每句 2 帧（第二帧整体模糊），应保留 3 张清晰帧
    sess = out / "capture_session"
    sess.mkdir(parents=True, exist_ok=True)
    texts = ["我希望你能", "体验未曾有过的", "遇见不同的人"]
    n = 0
    for text in texts:
        for sharp in (True, False):
            n += 1
            im = Image.new("RGB", (720, 400), (8, 10, 14))
            d = ImageDraw.Draw(im)
            d.ellipse((80, 40, 240, 200), fill=(70, 90, 110))
            d.polygon([(400, 220), (500, 120), (620, 220)], fill=(30, 40, 50))
            d.rectangle((0, 300, 720, 400), fill=(0, 0, 0))
            d.text((360, 348), text, font=font(30), fill=(255, 255, 255), anchor="mm")
            if not sharp:
                im = im.filter(ImageFilter.GaussianBlur(3))
            im.save(sess / f"frame_{n:04d}.png")
    kept, dropped = finalize_session(sess, cfg)
    # 追加一帧「没有字幕的画面」（底部黑条但无字），应被剔除
    im = Image.new("RGB", (720, 400), (8, 10, 14))
    d = ImageDraw.Draw(im)
    d.ellipse((80, 40, 240, 200), fill=(70, 90, 110))
    d.polygon([(400, 220), (500, 120), (620, 220)], fill=(30, 40, 50))
    d.rectangle((0, 300, 720, 400), fill=(0, 0, 0))
    im.save(sess / "frame_0007.png")
    kept, dropped = finalize_session(sess, cfg)
    check(len(kept) == 3 and len(dropped) == 4,
          f"7 帧 3 句 + 1 帧无字幕 → 保留 3 帧、剔除 4 帧（实测 {len(kept)}/{len(dropped)}）")
    check(Path(sess / "frame_0007.png") in {p for p in dropped},
          "没有字幕的画面被自动剔除")
    check({p.name for p in kept} == {"frame_0001.png", "frame_0003.png", "frame_0005.png"},
          f"每句保留的都是更清晰的那帧 → {sorted(p.name for p in kept)}")
    check([p.name for p in kept] == sorted(p.name for p in kept), "保留帧保持时间顺序")

    # 检测失效兜底：全部帧都判断为无字幕时不能把结果删光
    from subtitle_puzzle.capture import text_ratio
    with Image.open(sess / "frame_0001.png") as ok_im:
        check(text_ratio(ok_im, (0.82, 1.0)) > cfg.min_text_ratio,
              "有字幕帧的亮像素占比高于阈值")
    sess2 = out / "no_text_session"
    sess2.mkdir(parents=True, exist_ok=True)
    for i in range(1, 4):
        im = Image.new("RGB", (720, 400), (8, 10, 14))
        d = ImageDraw.Draw(im)
        d.rectangle((0, 300, 720, 400), fill=(0, 0, 0))
        im.save(sess2 / f"frame_{i:04d}.png")
    kept2, _ = finalize_session(sess2, cfg)
    check(len(kept2) >= 1, "全部帧无字幕时放弃过滤（检测失效兜底，不删光）")

    # 同句字幕跨背景变化 / 亮背景空帧（新算法的重点场景）
    from subtitle_puzzle.capture import (pack_rows, rows_diff_min,
                                         _dilate_rows, column_profile)

    def _band_frame(bg, text=None):
        im = Image.new("RGB", (720, 400), (8, 10, 14))
        d = ImageDraw.Draw(im)
        d.rectangle((0, 300, 720, 400), fill=bg)
        if text:
            d.text((360, 348), text, font=font(30), fill=(255, 255, 255),
                   anchor="mm", stroke_width=2, stroke_fill=(0, 0, 0))
        return im

    s3 = out / "bg_change_session"
    s3.mkdir(parents=True, exist_ok=True)
    _band_frame((170, 170, 170), "我希望你能").save(s3 / "frame_0001.png")
    _band_frame((20, 20, 20), "我希望你能").save(s3 / "frame_0002.png")
    kept3, dropped3 = finalize_session(s3, cfg)
    check(len(kept3) == 1,
          f"同句字幕背景从亮变暗 → 不误判为新句，聚成一簇（实测 kept={len(kept3)}）")
    _band_frame((200, 200, 200)).save(s3 / "frame_0003.png")   # 亮背景、无字幕
    kept4, dropped4 = finalize_session(s3, cfg)
    check(len(kept4) == 1 and (s3 / "frame_0003.png") in {p for p in dropped4},
          "亮背景的无字幕帧同样被剔除（去空白不被亮背景骗过）")

    # 平移容差：同一句字幕整体挪 3px/1px，不算新句（走真实判定路径 same_line）
    from subtitle_puzzle.capture import same_line as _same_line, FrameMeta as _FM
    m1 = band_signature(_band_frame((0, 0, 0), "我希望你能"), (0.82, 1.0), cfg.sig_size)
    im_shift = Image.new("RGB", (720, 400), (8, 10, 14))
    ImageDraw.Draw(im_shift).rectangle((0, 300, 720, 400), fill=(0, 0, 0))
    ImageDraw.Draw(im_shift).text((363, 349), "我希望你能", font=font(30),
                                  fill=(255, 255, 255), anchor="mm",
                                  stroke_width=2, stroke_fill=(0, 0, 0))
    m2 = band_signature(im_shift, (0.82, 1.0), cfg.sig_size)
    fm1 = _FM(path=Path("a"), rows=pack_rows(m1, cfg.sig_size),
              rows_d=_dilate_rows(pack_rows(m1, cfg.sig_size), cfg.sig_size[0]),
              prof=column_profile(m1, cfg.sig_size))
    fm2 = _FM(path=Path("b"), rows=pack_rows(m2, cfg.sig_size),
              rows_d=_dilate_rows(pack_rows(m2, cfg.sig_size), cfg.sig_size[0]),
              prof=column_profile(m2, cfg.sig_size))
    check(_same_line(fm1, fm2, cfg.sig_size[0], cfg.dedup_threshold),
          "同句字幕平移 3px/1px → 平移容差内视为同句")


def test_gui(paths: list[Path], out: Path):
    print("\n[2] 图形界面（离屏）")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt, QPoint, QTimer, QPointF, QRect
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox
    from subtitle_puzzle.app import MainWindow, _ensure_cjk_font, apply_theme

    app = QApplication.instance() or QApplication([])
    _ensure_cjk_font(app)
    apply_theme(app)
    # 首次运行的「选导出目录」对话框会阻塞自检：先标记已完成，结束后恢复
    from PySide6.QtCore import QSettings as _QS
    _pre = _QS("SubtitlePuzzle", "Desktop")
    orig_first_run = _pre.value("first_run_done", None)
    _pre.setValue("first_run_done", True)
    orig_countdown = _pre.value("cap_countdown_v3", None)
    _pre.remove("cap_countdown_v3")
    w = MainWindow()
    w.resize(1440, 940)
    orig_export_dir = w.settings.value("export_dir", "")
    orig_name_tpl = w.settings.value("name_template", "")
    # 清掉本机已存在的历史记录，保证「无历史」断言成立；测试结束再清一次
    w.settings.setValue("history", "")
    w.records = []
    w.start_page.set_records([])
    # 捕获区域也清掉，保证「未选择区域」的初始态可断言
    w.settings.remove("cap_bbox")
    w.settings.remove("cap_region")
    w.settings.remove("cap_sens_v2")
    w.settings.remove("cap_countdown_v2")
    w.settings.remove("cap_hotkey")
    w.settings.remove("hotkey_tip_shown")
    w.sp_sens.setValue(7)
    w.sp_countdown.setValue(8)
    w._cap_bbox = None
    w._cap_region = None
    w._update_capture_ui()
    check(w.windowTitle() == "台词匠", "主窗口构建（软件名：台词匠）")
    check("导入图片" in w.btn_import.text() and w.sp_gap.value() == 0,
          "导入按钮存在且间距默认为 0")
    check(w.stack.currentIndex() == 0, "初始停在起始页")
    check(w.status.isHidden() and w.status.currentMessage() == "",
          "启动时状态栏隐藏（不再有「就绪…」这一行）")
    # 起始页页脚：左下角一句话 + 右下角「遇到问题？」
    from subtitle_puzzle.app import QUOTE_TEXT, FAQ_HTML
    check(w.start_page.lbl_quote.text() == QUOTE_TEXT and QUOTE_TEXT,
          f"起始页左下角有一句话 → {QUOTE_TEXT}")
    check(not hasattr(w.start_page, "btn_faq"), "首页不再放「遇到问题？」入口")
    check(w.btn_feed_ws.text().startswith("投喂作者"),
          "工作区顶部有「投喂作者」入口")
    check(w.btn_faq.text() == "遇到问题？" and w.btn_faq.minimumHeight() >= 26
          and w.btn_faq.minimumWidth() >= 84,
          "工作区顶部有「遇到问题？」入口（命中区 ≥84×26）")
    # 自动定位字幕条：上下各留安全边距（避免首尾行贴边）
    from subtitle_puzzle.app import band_to_rect
    r_pad = band_to_rect((0, 1011, 720, 82), (720, 1280), (720, 1280))
    check(r_pad.y == 1005 and r_pad.h == 94 and r_pad.w == 720,
          f"自动定位条带上下各留 6px → y={r_pad.y} h={r_pad.h}")
    r_top = band_to_rect((0, 2, 720, 4), (720, 1280), (720, 1280))
    check(r_top.y == 0 and r_top.y + r_top.h <= 1280, "余量不会越出图片边界（顶部）")
    r_bot = band_to_rect((0, 1270, 720, 10), (720, 1280), (720, 1280))
    check(r_bot.y + r_bot.h <= 1280, "余量不会越出图片边界（底部）")
    # 越界警告要给出可操作出口
    w._show_warnings(["第 1 张 a.png：裁剪框超出图片边界，已自动收缩"])
    check("重选" in w.warn_box.toPlainText(), "越界警告附带修复出口提示")
    w._show_warnings([])
    fired = []
    w.btn_faq.clicked.disconnect()                  # 避免真的弹出模态框卡住自检
    w.btn_faq.clicked.connect(lambda: fired.append(1))
    w.btn_faq.click()
    check(fired == [1], "点「遇到问题？」会触发帮助弹窗")
    w.btn_faq.clicked.connect(w.show_faq)
    dlg_faq = w._build_faq_dialog()
    from PySide6.QtWidgets import QPushButton as _PB, QTextBrowser as _TB
    body = dlg_faq.findChild(_TB, "faqBrowser")
    txt = body.toPlainText() if body is not None else ""
    check(body is not None and "yi.run@qq.com" in txt,
          "常见问题弹窗含联系方式")
    ok_btn = dlg_faq.findChild(_PB, "faqOk")
    check(ok_btn is not None and ok_btn.text() == "知道了",
          "常见问题弹窗底部有「知道了」按钮")
    dlg_faq.deleteLater()
    # 投喂 Token 弹窗（仿 V2Live：颜文字 + 微信/支付宝收款码）
    dlg_feed = w._build_token_dialog()
    from PySide6.QtWidgets import QLabel as _LB
    feed_msg = dlg_feed.findChild(_LB, "feedMsg")
    feed_ok = dlg_feed.findChild(_PB, "feedOk")
    check(feed_msg is not None and "๑" in feed_msg.text(),
          "投喂弹窗文案带颜文字")
    check(feed_ok is not None and feed_ok.text() == "谢谢老板 (*^▽^*)",
          "投喂弹窗按钮为「谢谢老板 (*^▽^*)」")
    qr = dlg_feed.findChild(_LB, "qr_wechat")
    check(qr is not None and qr.pixmap() is not None and not qr.pixmap().isNull(),
          "投喂弹窗能加载微信收款码")
    dlg_feed.deleteLater()
    check("还有问题?请联系" in FAQ_HTML,
          "常见问题末尾按原软件格式给出联系方式")
    check("yi.run@qq.com" in FAQ_HTML, "常见问题里留了联系方式")
    check(not any(k in FAQ_HTML for k in ("作者", "在读", "大学")),
          "常见问题不出现个人身份信息（作者段落已移除）")
    for key in ("快捷键", "开始捕获", "去重", "导出目录"):
        check(key in FAQ_HTML, f"常见问题覆盖：{key}")
    tip = w._build_hotkey_tip()
    check("min-width: 400px" in tip.styleSheet(), "捕获快捷键提示弹窗已加宽")
    check(tip.informativeText().count("\n") >= 2, "快捷键提示文案分行，不再挤成一团")
    tip.deleteLater()
    check(not hasattr(w, "msg"), "顶部提示条已移除（消息统一走底部状态栏）")
    check(w.stack.currentIndex() == 0 and w.start_page.stack.currentIndex() == 0,
          "无历史时首页显示大导入卡")

    # 新建拼图 = 直接进空白工作区（自动捕获才有用武之地）
    from subtitle_puzzle.app import NewChoiceDialog
    dlg = NewChoiceDialog(w)
    check(dlg.btn_new.text() == "新建拼图" and dlg.btn_import.text() == "导入素材"
          and dlg.btn_new.isDefault(),
          "选择弹窗含「新建拼图 / 导入素材」两个入口（非下拉菜单）")
    dlg.close()
    w.goto_workspace_empty()
    check(w.stack.currentIndex() == 1 and not w.items and w.lst.count() == 0,
          "「新建拼图」直接进入空白工作区（可用自动捕获）")
    check(w.btn_capture.isEnabled(), "空白工作区里自动捕获入口可用")
    w.goto_start()
    check(w.stack.currentIndex() == 0, "空白工作区可回到起始页")
    check(w._drag_overlay is not None and w._drag_overlay.isHidden(),
          "拖拽模糊遮罩已就绪且默认隐藏")

    w.add_images(paths)
    check(w.stack.currentIndex() == 1, "导入后自动进入工作区")
    check(w.status.currentMessage() != "", "状态提示由底部状态栏显示")
    check(not w.status.isHidden(), "有提示时状态栏自动出现")
    check(len(w.items) == 6 and w.lst.count() == 6, "GUI 批量导入 + 缩略图列表")
    check(w.global_rect.valid(), "自动预填字幕区域")
    w.do_render(True)
    check(w.result_view._pix is not None, "实时预览产出图像")

    # 纯比例预设：按钮上只有比例数字，点击即切到「指定比例」并刷新尺寸提示
    ratio_btns = w.findChildren(_PB, "ratioPreset")
    check(len(ratio_btns) == 5 and
          [b.text() for b in ratio_btns] == ["3:4", "9:16", "1:1", "4:3", "16:9"],
          f"输出尺寸含 5 个纯比例预设 {[b.text() for b in ratio_btns]}")
    check(not any(k in "".join(b.text() for b in ratio_btns)
                  for k in ("小红书", "竖屏", "社媒", "朋友圈")),
          "预设不含任何用途注释")
    check([w.cmb_orient.itemData(i) for i in range(w.cmb_orient.count())]
          == ["auto", "landscape", "portrait"] and w.cmb_orient.currentData() == "auto",
          "方向选项：自动 / 横屏 / 竖屏，默认自动")
    check([w.cmb_subpos.itemData(i) for i in range(w.cmb_subpos.count())]
          == ["center", "bottom"] and w.cmb_subpos.currentData() == "bottom",
          "基础「字幕位置」只有居中 / 偏下两项，默认偏下（零背景）")
    check(w.sp_hero.value() == 100, "首图保留画面默认 100% 整幅画面")
    check(w.sp_suboff.minimum() < 0 < w.sp_suboff.maximum(),
          "高级「字幕微调」可正负调节")
    next(b for b in ratio_btns if b.text() == "9:16").click()
    check(w.cmb_out.currentData() == "expand" and
          (w.sp_rw.value(), w.sp_rh.value()) == (9, 16) and
          w.ed_ratio.text() == "9:16",
          "点 9:16 预设 → 切到指定比例（扩展取景）并回填数值")
    check(w.cmb_orient.isEnabled(), "扩展取景模式下方向选项可用")
    w.do_render(True)
    size_text = w.lbl_size.text()
    check("9:16" in size_text and "% 补边" in size_text,
          f"预览显示实际比例与补边占比：{size_text}")
    w.cmb_out.setCurrentIndex(w.cmb_out.findData("ratio"))
    check(not w.cmb_orient.isEnabled(), "补画布模式下方向选项置灰")
    w.cmb_out.setCurrentIndex(0)
    w.do_render(True)

    # 素材区右键菜单：单删 / 上移 / 下移 / 移到最前 / 移到最后
    menu = w._build_item_menu(0)
    texts = [a.text() for a in menu.actions() if not a.isSeparator()]
    check(texts == ["上移", "下移", "移到最前", "移到最后", "移除"],
          f"右键菜单含 移除/上下移/移前移后 → {texts}")
    acts = [a for a in menu.actions() if not a.isSeparator()]
    check(not acts[0].isEnabled() and not acts[2].isEnabled() and acts[3].isEnabled(),
          "首项的上移/移到最前置灰，移到最后可用")
    menu.deleteLater()

    first_name = w.items[0].path.name
    w._move_row(0, len(w.items) - 1)
    check(w.items[-1].path.name == first_name and len(w.items) == 6,
          "右键「移到最后」生效且总数不变")
    w._move_row(len(w.items) - 1, 0)
    check(w.items[0].path.name == first_name, "右键「移到最前」移回首位")
    removed = w.items[0].path
    w._remove_row(0)
    check(len(w.items) == 5 and w.lst.count() == 5, "右键「移除」与左下角移除按钮同一逻辑")
    w.add_images([removed])
    check(len(w.items) == 6, "移除后重新导入恢复数量")

    # 多选后右键：整体移动 / 整体移除
    names = [it.path.name for it in w.items]
    w._reselect([1, 3, 5])
    check(w._selected_rows() == [1, 3, 5], "列表可多选 3 张（Ctrl/Shift）")
    m2 = w._build_item_menu(3, w._selected_rows())
    last = [a for a in m2.actions() if not a.isSeparator()][-1]
    check(last.text() == "移除（3 张）", f"多选时移除项显示数量 → {last.text()}")
    m2.deleteLater()

    sel_names = [names[i] for i in (1, 3, 5)]
    w._shift_rows([1, 3, 5], +1)
    got = [it.path.name for it in w.items]
    check(len(got) == 6 and got[2] == sel_names[0] and got[4] == sel_names[1]
          and got[5] == sel_names[2],
          "多选整体下移：块内相对顺序不变、末项不动")
    w._move_rows_to_edge([2, 4, 5], True)
    got = [it.path.name for it in w.items]
    check(got[:3] == sel_names and len(got) == 6, "多选整体移到最前")
    w._move_rows_to_edge([0, 1, 2], False)
    got = [it.path.name for it in w.items]
    check(got[-3:] == sel_names, "多选整体移到最后")

    w._remove_rows([3, 4, 5])
    check(len(w.items) == 3 and w.lst.count() == 3, "多选右键「移除」一次删掉 3 张")
    restore = [p for p in paths if p.name in set(sel_names)]
    w.add_images(restore)
    check(len(w.items) == 6, "多选移除后重新导入恢复数量")

    # 真实鼠标拖框
    b = w.canvas.rect()
    p1 = QPoint(b.width() // 2 - 60, b.height() // 2 - 30)
    p2 = QPoint(b.width() // 2 + 60, b.height() // 2 + 30)
    QTest.mousePress(w.canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
    QTest.mouseMove(w.canvas, p2)
    QTest.mouseRelease(w.canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p2)
    r = w.canvas.crop_rect()
    fw, fh = w.items[0].full_size
    check(r.valid() and r.x + r.w <= fw and r.y + r.h <= fh,
          f"鼠标拖框坐标映射正确且不越界 → ({r.x},{r.y},{r.w},{r.h})")

    # 恢复成字幕区域
    w.global_rect = CropRect(0, 990, 720, 130)
    w._sync_rect_fields(w.global_rect)
    w.canvas.set_content(w._current_item(), w.global_rect)
    w.chk_independent.setChecked(True)
    check(w.items[0].override is not None, "可为本图开启独立框选")
    w.chk_independent.setChecked(False)
    check(w.items[0].override is None, "关闭独立框选后恢复统一框选")
    w.apply_rect_to_all()
    check(all(it.override is None for it in w.items), "当前框选应用到全部图片")

    w.sp_gap.setValue(12)
    w._collect_params()
    check(w.params.gap == 12, "间距参数可由界面修改")
    w.cmb_dir.setCurrentIndex(1)
    w._collect_params()
    check(w.params.direction == "horizontal", "拼接方向可由界面修改")
    w.do_render(True)
    check(w.result_view._pix.width() > w.result_view._pix.height(), "切换横向后预览刷新")

    # 真实点击导出（自动关闭结果弹窗）
    w.cmb_dir.setCurrentIndex(0)
    w.sp_gap.setValue(0)
    w.ed_dir.setText(str(out))
    w.ed_name.setText("GUI导出自检_{n}")
    w.cmb_fmt.setCurrentIndex(0)
    before = {p.name for p in out.glob("GUI导出自检_*.png")}

    def dismiss():
        for top in QApplication.topLevelWidgets():
            if isinstance(top, QMessageBox):
                top.accept()

    QTimer.singleShot(0, dismiss)
    w.export_image()
    after = {p.name for p in out.glob("GUI导出自检_*.png")}
    check(bool(after - before), f"GUI 导出按钮真实写出文件：{sorted(after - before)}")

    check(len(w.records) >= 1 and w.records[0].get("count") == 6,
          f"导出后写入历史记录：{w.records[0]['time'] if w.records else '-'}")
    check(w.start_page.stack.currentIndex() == 1 and
          w.start_page.panel.list.count() == len(w.records) + 1,
          f"有历史后首页变成卡片墙（{w.start_page.panel.list.count()} 格 = 1 新建 + {len(w.records)} 记录）")
    check(w.start_page.panel.list.item(0).data(Qt.ItemDataRole.UserRole).get("__new__") is True,
          "第一格是「新建拼图」卡片")
    thumb = str(w.records[0].get("thumb", ""))
    check(bool(thumb) and Path(thumb).exists(), "导出后保存拼图缩略图")
    check(not w.start_page.panel.list.item(1).icon().isNull(),
          "历史卡片显示拼图缩略图")

    # 右键菜单能定位到导出目录（这里拦截系统调用，不真的打开文件夹）
    from PySide6.QtGui import QDesktopServices
    opened = []
    original = QDesktopServices.openUrl
    try:
        QDesktopServices.openUrl = staticmethod(lambda url: opened.append(url.toString()) or True)
        w.start_page.panel._open_folder(w.records[0])
    except Exception:
        pass
    finally:
        try:
            QDesktopServices.openUrl = original
        except Exception:
            pass
    check(bool(opened) and str(w.records[0]["output"]).replace("\\", "/").split("/")[-2] in opened[0],
          "右键「打开所在文件夹」能定位到导出目录")

    # 右栏双面板
    check(w.tabs.count() == 2 and w.tabs.tabText(0) == "基础" and w.tabs.tabText(1) == "高级",
          "右栏分为「基础」「高级」两个面板")
    check(w.sp_gap in w.tabs.widget(0).findChildren(type(w.sp_gap)),
          "基础面板含常用参数（图片间距）")
    check(w.sp_x in w.tabs.widget(1).findChildren(type(w.sp_x)),
          "高级面板含全部参数（框选 X）")

    # 自动捕获 GUI 接入
    check(all(hasattr(w, a) for a in ("sp_countdown", "sp_sample", "sp_sens", "sp_mincap",
                                      "sp_dedup", "chk_dedup")),
          "高级面板含自动捕获参数组（倒计时/采样/敏感度/最小间隔/去重开关/去重阈值）")
    check(w.sp_countdown.value() == 8, "开始倒计时默认 8 秒")
    check(w.sp_sens.value() == 7, "捕获敏感度默认 7 档（比之前更灵敏）")
    check(w.chk_dedup.isChecked() and w.sp_dedup.isEnabled(),
          "自动去重默认开启且阈值可用")
    check(w.btn_capture.text().startswith("● 开始捕获") and w.btn_capture.isEnabled(),
          "工作区有自动捕获入口按钮")
    check(not hasattr(w, "btn_up") and not hasattr(w, "spin_goto"),
          "排序区已删除上移/下移与移到第 n 位（拖拽排序替代）")
    check(w.lbl_cap_region.text().startswith("区域：未选择"), "初始区域状态为未选择")
    # 区域持久化 + 状态机
    w._cap_bbox = (100, 200, 900, 700)
    w._cap_region = QRect(50, 100, 800, 500)
    w._save_capture_region()
    w._cap_bbox = None
    w._cap_region = None
    w._load_capture_region()
    check(w._cap_bbox == (100, 200, 900, 700) and w._cap_region.width() == 800,
          "捕获区域持久化并可恢复（重启后快捷键直接可用）")
    w._update_capture_ui()
    check(w.btn_capture.text() == "● 开始捕获"
          and "800×500" in w.lbl_cap_region.text(),
          "有区域时按钮待机（开始只靠点击，文案不带热键）")
    # 快捷键只负责结束：空闲时按它不触发捕获
    w._on_hotkey()
    check(not w._capture_busy() and "开始捕获" in w.status.currentMessage(),
          "空闲时按快捷键不开始捕获，只提示去点按钮")
    w._cap_bbox = None
    w._cap_region = None
    w._update_capture_ui()
    check(w.btn_capture.text().startswith("● 开始捕获 · 先框选"),
          "无区域时按钮提示先框选区域")

    # 全屏捕获 + 倒计时 0 立即开始
    scr = QApplication.primaryScreen().geometry()
    w.set_full_screen_region()
    check(w._cap_region == QRect(scr) and w._cap_bbox is not None,
          "「全屏」把捕获区域设为整块屏幕")
    fired_fs = []
    orig_cd = w._begin_countdown
    w._begin_countdown = lambda *a, **k: fired_fs.append(1)
    w.capture_full_screen()          # 点「全屏」按钮的入口
    w._begin_countdown = orig_cd
    check(not fired_fs, "「全屏」只设区域、不自动开始捕获")
    fired0 = []
    orig_start2 = w._start_capture
    w._start_capture = lambda: fired0.append(1)
    w.sp_countdown.setValue(0)
    w._begin_countdown()
    check(bool(fired0), "开始倒计时设为 0 时立即开始捕获")
    w._start_capture = orig_start2
    w.sp_countdown.setValue(5)

    # 回归：倒计时浮层必须被持有引用（曾因无引用被 GC，点开始捕获毫无反应）
    import time as _time
    fired = []
    orig_start = w._start_capture
    w._start_capture = lambda: fired.append(1)
    w._cap_region = QRect(100, 100, 400, 300)
    w._begin_countdown(1)
    deadline = _time.time() + 2.5
    while _time.time() < deadline and not fired:
        app.processEvents()
        _time.sleep(0.02)
    check(bool(fired), "倒计时结束后真正触发开始捕获（浮层引用未丢）")
    w._start_capture = orig_start
    if w._countdown is not None:
        w._countdown.close()
        w._countdown = None
    w._cap_region = None
    check(hasattr(w.start_page, "_show_new_menu") and
          w.start_page.plus.text() == "+",
          "起始页 + 号绑定「新建拼图 / 导入素材」选择弹窗")
    w.sp_sens.setValue(8); w.sp_dedup.setValue(30)
    capcfg = w._collect_capture_cfg()
    check(abs(capcfg.diff_threshold - 0.03) < 1e-9 and abs(capcfg.dedup_threshold - 0.30) < 1e-9,
          "敏感度映射触发阈值（8 档 ≈ 3%）且参数可由界面修改")
    w.chk_dedup.setChecked(False)
    check(not w.sp_dedup.isEnabled(), "关闭去重开关后阈值置灰")
    w.chk_dedup.setChecked(True)
    w.sp_sens.setValue(5); w.sp_dedup.setValue(8)
    check(w._hotkey is not None and bool(w._hotkey_text),
          f"全局热键已初始化（当前 {w._hotkey_text}）")
    check(w._hotkey.ok, "当前快捷键已成功注册（被占用时自动回退到空闲键）")
    check(hasattr(w, "btn_open_dir") and w.btn_open_dir.text() == "打开输出文件夹",
          "导出模块有「打开输出文件夹」按钮")
    # 捕获悬浮条：常驻置顶 + 排除出截屏（以前会被一起录进画面）
    import ctypes as _ct
    from subtitle_puzzle.app import CaptureBar, WDA_EXCLUDEFROMCAPTURE
    check(hasattr(w, "_cap_bar"), "捕获悬浮条已恢复（置顶显示）")
    check(WDA_EXCLUDEFROMCAPTURE == 0x11 and hasattr(CaptureBar, "showEvent"),
          "悬浮条实现了「显示但不进截图」逻辑")
    bar = CaptureBar("F8", lambda: None, w)
    check("F8" in bar.text(), "悬浮条提示文案跟随快捷键")
    if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
        bar.show()
        app.processEvents()
        aff = _ct.c_uint(0)
        _ct.windll.user32.GetWindowDisplayAffinity(int(bar.winId()), _ct.byref(aff))
        check(aff.value == 0x11, "悬浮条已排除出截屏（不会被录进截图）")
    bar.close()
    # 提示层绝不能抢焦点：否则播放器里按 F（全屏）/Enter/空格 全失效
    from PySide6.QtCore import Qt as _Qt
    from subtitle_puzzle.app import CountdownOverlay
    bar2 = CaptureBar("F8", lambda: None, w)
    check(bool(bar2.windowFlags() & _Qt.WindowType.WindowDoesNotAcceptFocus)
          and bar2.testAttribute(_Qt.WidgetAttribute.WA_ShowWithoutActivating),
          "悬浮条不抢焦点（不影响播放器快捷键）")
    bar2.close()
    cdov = CountdownOverlay("请切到视频播放器…", 8, lambda: None, w)
    check(bool(cdov.windowFlags() & _Qt.WindowType.WindowDoesNotAcceptFocus)
          and bool(cdov.windowFlags() & _Qt.WindowType.WindowTransparentForInput)
          and cdov.testAttribute(_Qt.WidgetAttribute.WA_ShowWithoutActivating),
          "倒计时提示不抢焦点、鼠标穿透")
    check("8" in cdov.text() and "播放器" in cdov.text(), "倒计时显示剩余秒数与提示语")
    cdov.close()
    # 改快捷键 → 按钮/提示文案同步（挑本机大概率空闲的键）
    from PySide6.QtGui import QKeySequence as _QKS
    changed = False
    for cand in ("F9", "F10", "Ctrl+Alt+F9"):
        w.sp_hotkey.setKeySequence(_QKS(cand))
        w._apply_hotkey()
        if w._hotkey_text.replace(" ", "") == cand.replace(" ", ""):
            changed = True
            break
    check(changed and w._hotkey_text in w.lbl_hotkey.text(),
          f"改快捷键后热键行同步显示新键（{w._hotkey_text}）")

    # 滚动参数栏不会误改数值
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QWheelEvent
    before_gap = w.sp_gap.value()
    before_hero = w.sp_hero.value()
    try:
        ev = QWheelEvent(QPointF(10, 10), QPointF(10, 10), QPoint(0, 0),
                         QPoint(0, 120), Qt.MouseButton.NoButton,
                         Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(w.sp_gap, ev)
        QApplication.sendEvent(w.sp_hero, ev)
    except Exception as exc:
        print("   (滚轮事件构造跳过:", exc, ")")
    check(w.sp_gap.value() == before_gap and w.sp_hero.value() == before_hero,
          f"滚动参数面板不会误改数值（间距 {w.sp_gap.value()}）")
    rec = dict(w.records[0])
    w.goto_start()
    check(w.stack.currentIndex() == 0 and not w.items, "「新建拼图」回到起始页并清空批次")
    w.open_record(rec)
    check(w.stack.currentIndex() == 1 and len(w.items) == 6,
          f"双击历史记录可重新打开（{len(w.items)} 张）")

    w.clear_all()
    check(len(w.items) == 0, "清空列表")
    w.do_render(True)
    check("尚未导入" in w.result_view._hint, "清空后预览区给出明确提示")
    w.close()

    # 清理自检留下的历史记录与捕获区域，别污染用户的记录和设置
    recs = w._load_history()
    keep = [r for r in recs if "自检" not in str(r.get("output", ""))]
    if len(keep) != len(recs):
        w._save_history(keep)
        w.start_page.set_records(keep)
    w.settings.remove("cap_bbox")
    w.settings.remove("cap_region")
    w.settings.remove("cap_sens_v2")
    w.settings.remove("cap_hotkey")
    w.settings.remove("hotkey_tip_shown")
    # 恢复用户原来的导出设置；原值本身是历史测试留下的脏值就清掉
    if orig_export_dir and "puzzle_selftest" not in orig_export_dir:
        w.settings.setValue("export_dir", orig_export_dir)
    else:
        w.settings.remove("export_dir")
    if orig_countdown is None:
        w.settings.remove("cap_countdown_v3")
    else:
        w.settings.setValue("cap_countdown_v3", orig_countdown)
    if orig_first_run is None:
        w.settings.remove("first_run_done")
    else:
        w.settings.setValue("first_run_done", orig_first_run)
    if orig_name_tpl and "自检" not in orig_name_tpl:
        w.settings.setValue("name_template", orig_name_tpl)
    else:
        w.settings.remove("name_template")
    print("  [OK] GUI 全链路无异常")


def main():
    print("台词拼图自检 ——", sys.version.split()[0])
    # 测试用的合成图放临时目录：sample_images/ 里是仓库的真实示例素材，不能被覆盖
    tmp = Path(tempfile.mkdtemp(prefix="puzzle_selftest_"))
    samples = tmp / "samples"
    paths = make_samples(samples)
    print(f"\n[0] 已生成 {len(paths)} 张测试用台词图：{samples}")
    test_core(paths, tmp)
    test_capture(tmp)
    test_gui(paths, tmp)
    print(f"\n全部通过：{PASSED} 项断言。仓库 sample_images/ 里是真实示例，可直接拖进工具试用。")


if __name__ == "__main__":
    main()
