"""台词拼图工具启动入口：python run.py

附加参数：
  --check   不开界面，跑一遍核心冒烟测试（给打包后的 exe 自检用），退出码 0 = 通过
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _smoke_check() -> int:
    import os
    import tempfile

    from PIL import Image
    from PIL import ImageDraw

    from subtitle_puzzle.core import (
        CropRect, LayoutParams, ImageItem, compose, load_preview, save_image,
    )
    from subtitle_puzzle.capture import band_signature, diff_ratio

    tmp = Path(tempfile.mkdtemp(prefix="puzzle_check_"))
    src = tmp / "a.png"
    Image.new("RGB", (720, 1280), (12, 14, 18)).save(src)

    item = ImageItem(path=src, full_size=(720, 1280))
    item.preview = load_preview(src, 1.0)

    rect = CropRect(0, 1000, 720, 120)
    res = compose([item], rect, LayoutParams(gap=0), scale=1.0)
    if not res.ok:
        print("FAIL:", res.error)
        return 1
    if res.image.size != (720, 120):
        print("FAIL: 尺寸不对", res.image.size)
        return 1
    out = tmp / "out.png"
    save_image(res.image, out, "PNG", LayoutParams())
    if not out.exists() or out.stat().st_size < 100:
        print("FAIL: 导出失败")
        return 1
    print("OK: 裁剪/拼接/导出全部正常 ->", out)

    # 自动捕获核心：字幕行差分应显著
    a = Image.new("RGB", (640, 360), (0, 0, 0))
    ImageDraw.Draw(a).rectangle((80, 300, 560, 330), fill=(255, 255, 255))
    b = a.copy()
    ImageDraw.Draw(b).rectangle((80, 300, 560, 330), fill=(20, 20, 20))
    d = diff_ratio(band_signature(a, (0.8, 1.0)), band_signature(b, (0.8, 1.0)))
    if d < 0.1:
        print("FAIL: 字幕行差分不显著：", d)
        return 1
    print(f"OK: 自动捕获核心正常（字幕行差分 {d:.3f}）")

    # 环境变量指定截图路径时，额外验证 GUI 能真正启动（给打包后的 exe 用）
    shot = os.environ.get("PUZZLE_GUI_CHECK")
    if shot:
        try:
            from PySide6.QtWidgets import QApplication

            from subtitle_puzzle.app import MainWindow, _ensure_cjk_font, apply_theme

            app = QApplication.instance() or QApplication([])
            _ensure_cjk_font(app)
            apply_theme(app)
            win = MainWindow()
            win.show()
            for _ in range(5):
                app.processEvents()
            win.grab().save(shot)
            win.close()
            print("OK: GUI 启动并渲染正常 ->", shot)
        except Exception as exc:
            print("FAIL: GUI 启动异常:", exc)
            return 1
    return 0


def main():
    if "--check" in sys.argv:
        sys.exit(_smoke_check())
    from subtitle_puzzle.app import main as gui_main
    gui_main()


if __name__ == "__main__":
    main()
