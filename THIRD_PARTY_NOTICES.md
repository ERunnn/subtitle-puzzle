# 第三方代码归属声明

本项目为独立实现，其中一部分算法改编自以下 MIT 许可项目。按 MIT 要求保留版权与许可声明。

---

## subtitle-screenshot-stitcher

- 来源：https://github.com/colacao0909/subtitle-screenshot-stitcher
- 原始文件：`stack-subtitle-screenshots/scripts/stack_screenshots.py`
- 本项目使用位置：`subtitle_puzzle/core.py`
- 改编内容：
  - `natural_key()` —— 自然排序（frame_2 排在 frame_10 之前），改为按 `Path.name` 排序
  - `embedded_number()` —— 提取文件名中的编号，改为返回 `int | None`
  - `parse_ratio()` —— 解析 `3:4` / `3/4` 比例
  - `_axis_activity()` / `_active_bounds()` —— 行列亮度检测（灰度阈值 12、占比 5%）
  - `detect_content_box()` —— 自动检测有效内容框，改为接收内存中的 `PIL.Image`
  - 分页与末页平衡逻辑（每页张数超限时，从上一页借一张到末页）
- 改动说明：原实现面向命令行与文件路径，本项目改为面向 GUI 与内存图像；
  所有坐标统一为「原图像素」，新增字幕行自动定位与逐张独立框选能力。

```
MIT License

Copyright (c) 2026 stack-subtitle-screenshots contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## 交互设计参考（未复制代码）

- Vibo Player — https://github.com/stevenspage/video_screenshots （MIT）
  仅参考其「8 控制点可视化裁剪 / 第一张裁剪应用到后续截图 / 间距与边距 / 实时预览 / 导出 PNG」的交互设计，
  未复制其代码（该项目的核心是浏览器端视频播放器，与本项目的图片处理流程不同）。
