# 台词匠 · v1.0.0

> 哪句值得留，你自己定；剩下的交给它。

把视频里一句句台词截下来，自动拼成一张（或一组长图）。适合追剧台词整理、电影语录长图、双语字幕合集。

**看到喜欢的台词，按一次 F8，五分钟内出图** —— 不用找视频文件、不用记时间点、不用等 AI 跑完。

![四句台词拼成的长图：也许不是今天 / 也许不是明天 / 也许不是下个月，但总有一天 / 我会成为冠军](docs/demo/case-01.jpg)

**English** — *SubtitlePuzzle* is a Windows tool that watches a video playing on screen, captures every subtitle line automatically and stitches them into one long image. Python + PySide6, no OCR, no network.
**你可能在找**：视频台词做成一张长图 · 字幕截图拼接 · 追剧截图工具 · 电影语录长图 · subtitle screenshot stitcher

## 这是什么

| | |
|---|---|
| **做什么** | 屏幕上播视频 → 自动截下每一句字幕 → 拼成一张（或一组长图） |
| **平台** | Windows 10/11 桌面程序，有单文件 exe，也有 Python 源码 |
| **不做什么** | 不解析 SRT/ASS、不做语音识别、不做 OCR（不把字幕转成文字）、不联网 |

## 成品

另一组五句台词拼成的长图，点图片看原图。

<a href="docs/demo/case-02.jpg"><img src="docs/demo/case-02.jpg" alt="五句台词拼成的长图成品"></a>

## 自动捕获怎么用

**① 框一次字幕区域** —— 点左侧「重选」拉框：**宽度与画面同宽，高度只比字幕行高一点**（示例 1920×104）。框错再点「重选」；「全屏」则整屏捕获。这两个按钮都只是选区域，不会自己开始。区域记住一次就够。

**② 点「● 开始捕获」** —— 先把播放器全屏，再回软件点按钮。默认 8 秒倒计时，够你切回播放器；倒计时浮层不抢键盘焦点，播放器的空格 / F 键照常可用。

**③ 播完按 `F8` 停** —— 或点屏幕右上角悬浮条，它置顶显示「已截 N 张」，且被系统排除在截图之外，不会录进成品。

**④ 自动收尾** —— 去重、剔除无字幕帧、按顺序导入，接着选模式导出。

```
① 框选区域 → ② 开始捕获 → ③ 播完按 F8 → ④ 自动去重导入 → ⑤ 导出
```

**工作区**（左侧素材、中间框选与预览、右侧参数）

![工作区：左侧素材、中间框选与预览、右侧参数](docs/screenshots/02-workspace.png)

**参数面板**（「高级 → 自动捕获」）

<img src="docs/screenshots/04-capture-panel.png" alt="高级 → 自动捕获 参数面板">

**捕获中的悬浮条**（实时计数，点它也能停）

<img src="docs/screenshots/05-capture-bar.png" alt="捕获中的悬浮状态条">

**容易踩的四个点**

- **必须全屏播放**：捕获是屏幕级的，播放器被别的窗口压住，那块也会被截进去
- **区域别罩住本软件**：重叠时悬浮条会变黄提醒，先切到播放器再开始
- **没字幕的帧不用管**：字幕消失（黑场 / 转场 / 空镜）就不存盘，停止后还会再筛一遍
- **热键被占用也能停**：区域行显示「被占用」时，点悬浮条即可

## 三种拼接模式

同一批 5 张截图、同一个框选（1920×104）的成品，均为全分辨率未缩放，点图片看原图。

**① 纯台词** · 1920×520 —— 只留字幕，最紧凑

<a href="docs/demo/mode-text.png"><img src="docs/demo/mode-text.png" alt="纯台词模式成品"></a>

**② 首图保留画面（默认）** · 1920×1231 —— 首图保留整幅画面与它自己的字幕，后面的按字幕条接上

<a href="docs/demo/mode-first.png"><img src="docs/demo/mode-first.png" alt="首图保留画面模式成品"></a>

**③ 每张都保留画面** · 1920×3935 —— 一句一帧依次叠放，成品最长

<a href="docs/demo/mode-all.png"><img src="docs/demo/mode-all.png" alt="每张都保留画面模式成品"></a>

## 功能一览

| | |
|---|---|
| **捕获** | 字幕变化自动截图 · 去空白 · 去重留最清晰一帧 · 全局热键停止 · 悬浮条实时计数 |
| **排版** | 三种模式 · 画面保留高度（字幕永不切）· 纵横排列 / 间距 · 对齐 / 背景色 / 透明背景 |
| **输出** | 自适应 / 定宽 / 定比例 / 定像素 · 分页长图（末页孤图自动借一张）· PNG / JPEG / WEBP · `{date}` `{n}` `{page}` |
| **编辑** | 拖拽排序 · 右键移动 / 移除 · Ctrl / Shift 多选 · 逐张独立框选 · 历史批次重开 |

**去重和去空白是怎么做的**：字幕带按列中位数估背景 → 提取文字掩膜（背景明暗变化不干扰）→ 膨胀掩膜差分（±2px 平移容差）+ 列占用分布兜底 → 同句聚簇只留最锐的一帧；无字帧在捕获阶段就不落盘。

## 参数速查

| 参数 | 说明 |
| --- | --- |
| 拼接模式 | 纯台词 / 首图保留画面（默认）/ 每张都保留画面 |
| 画面保留高度 | 字幕**上方**留多少画面，100% ≈ 整幅原图，0% 只剩字幕条 |
| 图片间距 | 0 = 无缝长图，调大变成「截图卡片」 |
| 捕获敏感度 | 字幕变化阈值，框选后自动估一个值，偏小更敏感 |
| 采样间隔 / 最小截图间隔 | 检测频率 220 ms；两次截图至少隔 700 ms |
| 去重阈值 | 越大越严格；判定用「掩膜差异 + 列占用分布」双指标 |

## 获取与运行

- **直接用**：从 [Releases](https://github.com/ERun-3/subtitle-puzzle/releases/latest) 下载 `Taicijiang_v1.0.0.exe`，双击即用（约 64 MB，免安装）
- **从源码**：`pip install -r requirements.txt` → `python run.py`（没有 Python 时可双击 `run.bat`）
- **环境**：Windows 10/11；「悬浮条不进截图」需要 Windows 10 2004+

<details>
<summary>打包成单文件 exe</summary>

```bash
pip install -r requirements-dev.txt
python -m PyInstaller --onefile --windowed --name 台词匠 ^
  --icon subtitle_puzzle/icon.ico ^
  --add-data "subtitle_puzzle/assets;subtitle_puzzle/assets" ^
  --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtWebEngineWidgets ^
  --exclude-module PySide6.QtQuick --exclude-module PySide6.QtQml --exclude-module PySide6.Qt3DCore ^
  --exclude-module PySide6.QtCharts --exclude-module PySide6.QtMultimedia --exclude-module PySide6.QtTest ^
  --exclude-module PySide6.QtPdf --exclude-module tkinter run.py
```

产物 `dist/台词匠.exe`。发版一条命令：`python tools/release.py 1.0.1`（自检 → 打包 → 打标签 → 推送 → 建 Release）。
</details>

## 常见问题

<details>
<summary>按 F8 停不下来 / 没反应？</summary>

快捷键可能被录屏、游戏助手占用（区域行会显示「被占用」），到「高级 → 捕获快捷键」换一组；或者直接点右上角悬浮条停止。另外：**快捷键只负责结束**，开始必须在软件里点按钮。
</details>

<details>
<summary>截出来的图全是软件界面 / 一片空白？</summary>

区域要框在播放器画面内，并且全屏播放；被别的窗口压住就会把那块也截进去。
</details>

<details>
<summary>截太多 / 漏了字幕？</summary>

截太多：调大「捕获敏感度」和「最小截图间隔」；漏句：调小「采样间隔」。
</details>

## 其它

- **自检**：`python selftest.py`（153 项断言）；打包程序 `台词匠.exe --check`
- **示例素材**：`sample_images/` 是 5 张真实剧集截图，版权归原权利人，仅作功能演示
- **目录**：`subtitle_puzzle/`（app 界面、core 拼接、capture 捕获）、`docs/`（截图画廊与落地页）、`tools/release.py`
- **已知限制**：仅 Windows；屏幕级捕获怕遮挡；HDR 素材可能偏色；竖排 / 花字字幕建议手动框选
- **许可**：代码 [MIT](LICENSE)；第三方归属见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
- **更新记录**：[CHANGELOG.md](CHANGELOG.md)
<!-- AI-BRIEF:START
项目：台词匠 SubtitlePuzzle。Windows 桌面工具，Python 3.13 + PySide6；源码 subtitle_puzzle/（app.py 界面 / core.py 拼接导出 / capture.py 捕获），入口 run.py。
用途：截取屏幕上正在播放的视频字幕，去重去空白后拼成长图导出。不解析 SRT·ASS，不做 OCR·ASR，不联网，不含模型。
核心算法在 subtitle_puzzle/capture.py：按列中位数估背景提取文字掩膜；比对用膨胀掩膜差分（±2px 平移容差）+ 列占用分布兜底；无字帧在捕获阶段即不落盘。
实测依据：selftest.py 共 153 项断言（含离屏 GUI 全链路）；打包后的 exe 内置冒烟自检，退出码 0 为通过。
阈值实测（仅限仓库内测试用例，非全量语料）：同句在跨背景·模糊·平移场景差异 0.00 至 0.15，不同句 0.22 至 0.31；dedup_threshold 默认 0.08。
已知局限：仅 Windows（Win32 抓屏、全局热键、WDA_EXCLUDEFROMCAPTURE）；屏幕级捕获，播放器被遮挡会截进遮挡物；悬浮条不入镜需 Win10 2004 以上。
已知局限：字幕行定位是启发式（core.find_subtitle_band），竖排·花字·重描边需手动框选；亮背景配白字且无描边时可能漏检。
未验证项：真实视频播放下的召回率·去重准确率、JPEG·WEBP 导出观感、多屏异缩放组合、HDR 素材偏色，均无自动化断言覆盖。
发布流程：tools/release.py <版本> 串起自检→打包→打标签→推送→建 Release 并上传；当前 tag 为 v1.0.0，附件名用 ASCII 的 Taicijiang_v1.0.0.exe（中文名会被 GitHub 截断）。
仓库约定：subtitle_puzzle/assets/qr_*.png 被 .gitignore 排除（收款码不公开），缺图时弹窗显示「收款码未随仓库分发」；软件内入口保留。
文档分布：README.md 面向使用者；CHANGELOG.md 版本记录；llms.txt 机器可读摘要；docs/index.html 为 GitHub Pages 落地页，线上地址 https://erun-3.github.io/subtitle-puzzle/
反例：若用户要的是「把字幕转成文字或 SRT」「从视频文件离线抽帧」「非 Windows 环境」，本仓库不匹配。
AI-BRIEF:END -->
