"""自动捕获核心逻辑：监屏抓帧 + 字幕带文字掩膜比对 + 去重留清晰。

只依赖 Pillow 和标准库，不含任何 Qt 代码，可脱机测试。

流程（用户的设计：过截图 + 算法对比字幕）：
1. 用户在屏幕上框选视频区域（要含底部字幕带）。
2. 快捷键开始后，后台线程按采样间隔抓屏；对字幕带做「局部对比二值化」
   得到文字掩膜（每列用中位数估计背景，只留下明显偏离背景的像素），
   与上一观察帧 / 上一已存帧比对，变化显著且当前帧确有字幕时才存盘。
3. 停止后对会话目录里的全部帧做「顺序聚类 + 每簇留最清晰」：同一句字幕
   停留期间产生的帧聚成一簇，只保留锐度最高的那张；没有任何文字的帧
   （黑场、转场、亮背景空镜）整类剔除，其余留在会话文件夹可手动找回。

边界约定：
* 掩膜用「列中位数 + 绝对差」而不是全局阈值：字幕带里的亮背景（白墙、
  天空、雪地）和字幕亮度接近时，全局阈值会把背景全判成文字，导致同句
  误判成新句（去重失效）、亮背景空帧判成有字（去空白失效）。列中位数
  天然抵消静态背景；字幕自带描边/阴影时暗像素也能被抓到。
* 差分允许 ±2px 平移容差：同一句字幕的亚像素抖动不算变化。
* 不做 OCR、不认字、零联网、零模型。
* 同一句字幕后来再次出现会另起一簇（顺序聚类），不会误删重复台词。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageFilter

from .core import find_subtitle_band

# 会话目录里帧文件的固定命名（finalize_session 按它收集）
FRAME_PATTERN = "frame_*.png"
SESSION_META = "session.json"


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class CaptureConfig:
    """捕获全流程的可调参数（高级面板可改）。"""
    sample_interval_ms: int = 220        # 每隔多久抓一次屏
    diff_threshold: float = 0.06         # 字幕行变化率 ≥ 此值才截图（过截偏松）
    min_capture_interval_ms: int = 700   # 两次截图的最小间隔，防瞬间连爆
    dedup_threshold: float = 0.08        # 去重：字幕行差异 ≤ 此值视为同一句
    min_text_ratio: float = 0.004        # 字幕行文字像素占比下限（低于视为无字）
    min_col_occupancy: float = 0.02      # 有文字像素的列占比下限（双重判定）
    band_region: tuple[float, float] = (0.82, 1.0)  # 兜底差分区域（比例，下部 18%）
    sig_size: tuple[int, int] = (240, 48)           # 掩膜缩放尺寸
    max_frames: int = 800                # 单次会话帧数上限（防磁盘写爆）


# ---------------------------------------------------------------------------
# 图像特征：字幕带文字掩膜 / 差分 / 锐度
# ---------------------------------------------------------------------------

def _band_gray(image: Image.Image,
               region: tuple[float, float],
               sig_size: tuple[int, int]) -> Image.Image:
    """取字幕带并缩放到签名尺寸（灰度）。"""
    gray = image.convert("L")
    w, h = gray.size
    y0 = max(0, min(int(h * region[0]), h - 1))
    y1 = max(y0 + 1, min(h, int(h * region[1])))
    band = gray.crop((0, y0, w, y1))
    return band.resize((max(1, sig_size[0]), max(1, sig_size[1])),
                       Image.Resampling.BILINEAR)


def band_signature(image: Image.Image,
                   region: tuple[float, float] = (0.82, 1.0),
                   sig_size: tuple[int, int] = (240, 48)) -> list[int]:
    """字幕行 → 二值「文字掩膜」（1 = 明显偏离本列背景的像素）。

    每列用中位数估计背景（字幕只占列内少数行，中位数落在背景上），然后
    只保留 |像素 - 背景| ≥ max(28, 背景×18%) 的像素。这样：
    * 亮背景（白墙/天空）不会整体被当成文字 —— 去空白不再误判；
    * 同一句字幕换了个更亮/更暗的场景，掩膜基本不变 —— 去重不再误判；
    * 字幕自带的黑色描边、阴影（比背景暗得多）同样会被抓到。
    """
    band = _band_gray(image, region, sig_size)
    # 轻微平滑：让抗锯齿边缘稳定下来，掩膜对 1px 级平移/抖动不敏感
    band = band.filter(ImageFilter.GaussianBlur(1))
    W, H = band.size
    values = list(band.getdata())
    if not values:
        return []
    # 每列中位数 = 该列的背景估计
    cols = [sorted(values[r * W + c] for r in range(H))[H // 2] for c in range(W)]
    mask: list[int] = []
    for r in range(H):
        base = r * W
        for c in range(W):
            v = values[base + c]
            bg = cols[c]
            delta = max(28, round(bg * 0.18))
            mask.append(1 if abs(v - bg) >= delta else 0)
    return mask


def _popcount(x: int) -> int:
    return bin(x).count("1")


def _dilate_rows(rows: list[int], W: int, times: int = 2) -> list[int]:
    """掩膜按 3×3 膨胀 times 次（纯位运算）。

    模糊帧的文字比清晰帧「胖」一圈，直接比对差异可达 0.27；两边都膨胀后
    形状趋于一致，同句差异回落到阈值以内，不同句仍相距甚远。
    """
    full = (1 << W) - 1
    for _ in range(times):
        out = []
        n = len(rows)
        for r in range(n):
            v = rows[r]
            h = v | ((v >> 1) & full) | ((v << 1) & full)
            up = rows[r - 1] if r > 0 else 0
            dn = rows[r + 1] if r < n - 1 else 0
            out.append(h | up | dn)
        rows = out
    return rows


def pack_rows(mask: list[int], sig_size: tuple[int, int]) -> list[int]:
    """把 0/1 掩膜按行打包成整数（每行一个 int），比对时用位运算加速。"""
    W, H = sig_size
    rows: list[int] = []
    for r in range(H):
        v = 0
        base = r * W
        for c in range(W):
            if mask[base + c]:
                v |= 1 << c
        rows.append(v)
    return rows


def _rows_diff(ra: list[int], rb: list[int], W: int,
               dx: int = 0, dy: int = 0) -> float:
    """两幅打包掩膜的差异（XOR/并集），允许 dx/dy 平移容差。"""
    H = len(ra)
    xor = union = 0
    full = (1 << W) - 1
    for r in range(H):
        sr = r - dy
        a = ra[r]
        b = rb[sr] if 0 <= sr < H else 0
        if dx:
            b = ((b >> dx) if dx > 0 else (b << -dx)) & full
        u = a | b
        if u:
            union += _popcount(u)
            xor += _popcount(a ^ b)
    return 0.0 if union == 0 else xor / union


def diff_ratio(a: list[int], b: list[int]) -> float:
    """两张掩膜的差异：XOR / 并集。0 = 完全相同，1 = 完全不重叠。

    兼容旧接口（平面 0/1 列表，无平移容差）；内部去重走 pack_rows +
    多偏移取最小值的版本。
    """
    if not a or len(a) != len(b):
        return 1.0
    union = xor = 0
    for x, y in zip(a, b):
        if x or y:
            union += 1
        if x != y:
            xor += 1
    return 0.0 if union == 0 else xor / union


# 平移容差：±2px 水平（字幕渲染/抓屏的亚像素抖动）、±1px 垂直
DEDUP_SHIFTS = ((0, 0), (-2, 0), (2, 0), (-1, 0), (1, 0), (0, -1), (0, 1))


def rows_diff_min(ra: list[int], rb: list[int], W: int,
                  shifts=DEDUP_SHIFTS) -> float:
    """多偏移取最小差异：同句字幕的轻微抖动/平移不算变化。"""
    return min(_rows_diff(ra, rb, W, dx, dy) for dx, dy in shifts)


def mask_stats(mask: list[int], sig_size: tuple[int, int]) -> tuple[float, float]:
    """返回 (文字像素密度, 有文字像素的列占比)。空带返回 (0, 0)。

    列占用率是密度的补充：有一行字时密度低但列占用不低；背景噪声通常
    又稀又散，两项同时过线才算「有字幕」。
    """
    if not mask:
        return 0.0, 0.0
    W, H = sig_size
    density = sum(mask) / len(mask)
    occ = 0
    for c in range(W):
        for r in range(H):
            if mask[r * W + c]:
                occ += 1
                break
    return density, occ / W


def text_ratio(image: Image.Image,
               region: tuple[float, float] = (0.82, 1.0)) -> float:
    """字幕行里文字像素的占比（= 掩膜密度）。

    没有字幕 → 0；有字幕（含描边）→ 通常 1%~8%。
    """
    sig = band_signature(image, region)
    return (sum(sig) / len(sig)) if sig else 0.0


def sharpness(image: Image.Image, sample: int = 320) -> float:
    """整帧锐度：缩到 sample 宽内 → FIND_EDGES → 像素方差。

    模糊帧的边缘能量低，清晰帧高。纯 PIL 实现，不需要 numpy。
    """
    g = image.convert("L")
    if g.width > sample:
        nh = max(1, round(g.height * sample / g.width))
        g = g.resize((sample, nh), Image.Resampling.LANCZOS)
    edges = g.filter(ImageFilter.FIND_EDGES)
    data = list(edges.getdata())
    n = len(data)
    if n == 0:
        return 0.0
    mean = sum(data) / n
    return sum((v - mean) ** 2 for v in data) / n


# ---------------------------------------------------------------------------
# 去重：顺序聚类，每簇保留最清晰的一帧
# ---------------------------------------------------------------------------

@dataclass
class FrameMeta:
    path: Path
    rows: list[int]           # 打包后的文字掩膜（按行位图）
    rows_d: list[int] = None  # 膨胀后的掩膜（同句比对用，容忍清晰/模糊差异）
    sharp: float = 0.0
    text_ratio: float = 1.0   # 字幕行文字像素占比
    col_occ: float = 1.0      # 有文字像素的列占比
    prof: int = 0             # 列占用分布（第 c 位 = 第 c 列是否有文字像素）


def column_profile(mask: list[int], sig_size: tuple[int, int]) -> int:
    """列占用分布压成一个整数：第 c 位 = 第 c 列是否有文字像素。

    这是比逐像素掩膜更粗、但更稳的特征：同一句字幕换背景 / 轻微模糊 /
    平移几像素时几乎不变（实测 ≤0.04），不同句通常 ≥0.14 —— 中间有清晰
    鸿沟，正好用来兜住「掩膜差但其实是同一句」的边界情况。
    """
    W, H = sig_size
    v = 0
    for c in range(W):
        for r in range(H):
            if mask[r * W + c]:
                v |= 1 << c
                break
    return v


def int_diff(a: int, b: int, W: int) -> float:
    """两个列占用整数的差异（XOR/并集）。"""
    u = a | b
    return 0.0 if not u else _popcount(a ^ b) / _popcount(u)


def same_line(m1: FrameMeta, m2: FrameMeta, W: int, thr: float) -> bool:
    """是否同一句字幕：膨胀掩膜差异达标，或列分布几乎一致且掩膜差异在两倍内。

    只看掩膜：字幕换背景（亮→暗）时的抗锯齿边缘像素会多出 ~0.15 的差异、
    模糊帧的文字会「胖」一圈（~0.27），都会被误判成新句；列分布在所有
    同句场景下都 ≤0.04，膨胀掩膜把模糊差异压回 0.08 以内 —— 两条路兜底。
    """
    md = rows_diff_min(m1.rows_d, m2.rows_d, W)
    if md <= thr:
        return True
    pd = int_diff(m1.prof, m2.prof, W)
    return pd <= thr and md <= thr * 2


def has_text(meta: FrameMeta, cfg: CaptureConfig) -> bool:
    """这一帧的字幕行里有没有字（密度与列占用双重判定）。"""
    return (meta.text_ratio >= cfg.min_text_ratio
            and meta.col_occ >= cfg.min_col_occupancy)


def analyze_frames(paths, cfg: CaptureConfig) -> list[FrameMeta]:
    """读取每张帧图，算字幕带掩膜、锐度和字幕存在度。读不了的文件静默跳过。"""
    metas: list[FrameMeta] = []
    for p in paths:
        try:
            with Image.open(p) as im:
                im.load()
                mask = band_signature(im, cfg.band_region, cfg.sig_size)
                rows = pack_rows(mask, cfg.sig_size)
                rows_d = _dilate_rows(rows, cfg.sig_size[0])
                sharp = sharpness(im)
                density, occ = mask_stats(mask, cfg.sig_size)
                prof = column_profile(mask, cfg.sig_size)
            metas.append(FrameMeta(path=Path(p), rows=rows, rows_d=rows_d,
                                   sharp=sharp, text_ratio=density,
                                   col_occ=occ, prof=prof))
        except Exception:
            continue
    return metas


def dedup_keep_sharpest(metas: list[FrameMeta],
                        cfg: CaptureConfig) -> tuple[list[Path], list[Path]]:
    """按时间顺序聚类：与簇首帧或簇尾帧的差异 ≤ dedup_threshold 视为同一句。

    * 与簇首比：同句判定；与簇尾比：容忍同句期间背景渐变造成的掩膜缓漂。
    * 每簇只留锐度最高的一帧。
    * 没有文字的帧（黑场 / 转场 / 亮背景空镜）整类剔除。
    * 若六成以上帧都被判为无字幕，说明检测失效（比如字幕位置不在预期行），
      此时放弃剔除，避免把好帧也删光。
    返回 (保留, 剔除)，均为时间序。
    """
    W = cfg.sig_size[0]
    usable = [m for m in metas if has_text(m, cfg)]
    no_text = [m for m in metas if not has_text(m, cfg)]
    if metas and len(usable) < max(1, int(len(metas) * 0.4)):
        usable, no_text = list(metas), []   # 检测不可信，退化为不做无字幕过滤

    kept: list[Path] = []
    cluster: list[FrameMeta] = []
    rep_rows: list[int] | None = None

    def flush():
        if not cluster:
            return
        kept.append(max(cluster, key=lambda m: m.sharp).path)

    for m in usable:
        new_line = rep_rows is None
        if rep_rows is not None:
            rep_meta = cluster[0]
            vs_head = not same_line(m, rep_meta, W, cfg.dedup_threshold)
            vs_tail = not same_line(m, cluster[-1], W, cfg.dedup_threshold)
            new_line = vs_head and vs_tail
        if new_line:
            flush()
            cluster = [m]
            rep_rows = m.rows
        else:
            cluster.append(m)
    flush()

    dropped = [m.path for m in no_text]
    kept_set = set(kept)
    for m in usable:
        if m.path not in kept_set:
            dropped.append(m.path)
    order = {m.path: i for i, m in enumerate(metas)}
    dropped.sort(key=lambda p: order.get(p, 0))
    return kept, dropped


def finalize_session(session_dir: Path,
                     cfg: CaptureConfig) -> tuple[list[Path], list[Path]]:
    """对会话目录里的全部帧跑去重，返回 (保留, 剔除) 的路径列表。

    若会话元数据记录了首帧定位的字幕行，就用那一行做签名（与捕获时一致）。
    """
    region = cfg.band_region
    try:
        meta = json.loads((session_dir / SESSION_META).read_text(encoding="utf-8"))
        r = meta.get("band_region")
        if isinstance(r, (list, tuple)) and len(r) == 2:
            region = (float(r[0]), float(r[1]))
    except Exception:
        pass
    cfg = replace_region(cfg, region)
    paths = sorted(session_dir.glob(FRAME_PATTERN))
    metas = analyze_frames(paths, cfg)
    return dedup_keep_sharpest(metas, cfg)


def replace_region(cfg: CaptureConfig,
                   region: tuple[float, float]) -> CaptureConfig:
    """返回仅 band_region 不同的配置副本。"""
    return CaptureConfig(
        sample_interval_ms=cfg.sample_interval_ms,
        diff_threshold=cfg.diff_threshold,
        min_capture_interval_ms=cfg.min_capture_interval_ms,
        dedup_threshold=cfg.dedup_threshold,
        min_text_ratio=cfg.min_text_ratio,
        min_col_occupancy=cfg.min_col_occupancy,
        band_region=region,
        sig_size=cfg.sig_size,
        max_frames=cfg.max_frames,
    )


# ---------------------------------------------------------------------------
# 监屏捕获循环（在后台线程里跑）
# ---------------------------------------------------------------------------

def detect_band_region(image: Image.Image,
                       fallback: tuple[float, float] = (0.82, 1.0)
                       ) -> tuple[float, float]:
    """首帧定位字幕行，返回归一化 (y0, y1)；定位失败则退回下部区域。

    关键设计：字幕行在视频里位置固定，首帧定位一次后全程复用——差分只发生在
    文字那一行，画面再怎么动都不会干扰「同一句/不同句」的判断。
    """
    band = find_subtitle_band(image, region=(0.40, 1.0))
    if band is None:
        return fallback
    _x, y, _w, h = band
    ih = max(1, image.height)
    y0 = max(0.0, y / ih)
    y1 = min(1.0, (y + h) / ih)
    if y1 - y0 < 0.02:  # 行高异常，退回兜底
        return fallback
    return (y0, y1)


def capture_loop(bbox: tuple[int, int, int, int],
                 out_dir: Path,
                 cfg: CaptureConfig,
                 stop_event,
                 on_frame=None,
                 on_error=None) -> int:
    """按采样间隔抓取屏幕 bbox 区域，字幕行变化时整帧存 PNG。

    bbox 为物理像素 (x1, y1, x2, y2)；stop_event 为 threading.Event；
    on_frame(n) 每成功截一帧回调一次；on_error(text) 出错时回调。
    返回成功保存的帧数。

    「去空白」在这里就做：字幕消失的帧（黑场、转场）不落盘——已经见过
    字幕之后再出现无字帧，就是句与句之间的空隙，没有保存价值。
    """
    try:
        from PIL import ImageGrab
    except Exception as exc:
        if on_error:
            on_error(f"屏幕捕获不可用：{exc}")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    interval = max(0.03, cfg.sample_interval_ms / 1000)
    min_gap = max(0.0, cfg.min_capture_interval_ms / 1000)
    last_obs: list[int] | None = None    # 上一观察帧的掩膜（每帧更新）
    last_saved: list[int] | None = None  # 上一已存帧的掩膜
    seen_text = 0                        # 已观察到多少帧「确有字幕」
    last_save = 0.0
    count = 0
    fails = 0
    band_region = cfg.band_region

    while not stop_event.is_set() and count < cfg.max_frames:
        t0 = time.perf_counter()
        try:
            img = ImageGrab.grab(bbox=bbox)
            fails = 0
        except Exception as exc:
            fails += 1
            if on_error:
                on_error(f"抓屏失败（{fails}/5）：{exc}")
            if fails >= 5:
                return count
            stop_event.wait(interval)
            continue

        # 首帧：精确定位字幕行并写进会话元数据，去重阶段复用同一行
        if last_obs is None:
            try:
                band_region = detect_band_region(img, cfg.band_region)
                (out_dir / SESSION_META).write_text(
                    json.dumps({"band_region": list(band_region)}),
                    encoding="utf-8")
            except Exception:
                band_region = cfg.band_region

        mask = band_signature(img, band_region, cfg.sig_size)
        density, occ = mask_stats(mask, cfg.sig_size)
        no_text = density < cfg.min_text_ratio or occ < cfg.min_col_occupancy
        if not no_text:
            seen_text += 1

        # 变化判定：与上一观察帧比（捕捉刚发生的变化），
        #          或与上一已存帧比（防止缓慢渐变累积漏截）
        if last_saved is None:
            changed = True                       # 会话第一帧先存一张
        elif last_obs is not None and diff_ratio(mask, last_obs) >= cfg.diff_threshold:
            changed = True
        elif diff_ratio(mask, last_saved) >= cfg.diff_threshold:
            changed = True
        else:
            changed = False

        # 去空白：已确认这个区域确实有字幕之后，无字帧一律不落盘
        skip_blank = no_text and seen_text > 3

        now = time.perf_counter()
        if changed and not skip_blank and now - last_save >= min_gap:
            count += 1
            img.save(out_dir / f"frame_{count:04d}.png")
            last_saved = mask
            last_save = now
            if on_frame:
                on_frame(count)

        last_obs = mask
        stop_event.wait(max(0.0, interval - (time.perf_counter() - t0)))
    return count
