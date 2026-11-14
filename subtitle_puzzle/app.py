"""视频台词拼图工具 —— PySide6 图形界面。

只处理预先导入的台词图片：导入 → 排序 → 框选 → 批量裁剪 → 拼接 → 导出。
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageFilter
from PySide6.QtCore import (
    Qt, QObject, QTimer, Signal, QRect, QRectF, QPoint, QSize, QSettings, QUrl,
    QPropertyAnimation, QEasingCurve, QParallelAnimationGroup,
    QStandardPaths,
)
from PySide6.QtGui import (
    QPixmap, QImage, QPainter, QPainterPath, QPen, QBrush, QColor, QIcon, QAction,
    QPalette, QKeySequence, QShortcut, QFont, QFontDatabase,
    QDesktopServices,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QDialog, QSplitter, QVBoxLayout, QHBoxLayout,
    QGridLayout, QFormLayout, QLabel, QPushButton, QToolButton, QListWidget,
    QListWidgetItem, QAbstractItemView, QSpinBox, QComboBox, QCheckBox,
    QLineEdit, QGroupBox, QScrollArea, QFileDialog, QColorDialog, QMessageBox,
    QStatusBar, QPlainTextEdit, QSizePolicy, QFrame, QStackedWidget,
    QGraphicsOpacityEffect, QLayout, QTabWidget, QMenu, QTextBrowser,
    QStyledItemDelegate, QStyleOptionViewItem, QStyle, QKeySequenceEdit,
)

from .core import (
    SUPPORTED_EXT, FORMAT_EXT, CropRect, Padding, LayoutParams, ImageItem,
    probe_size, project_scale, load_preview, compose, resolve_export_path,
    resolve_export_paths, save_image, render_filename, natural_key,
    embedded_number, parse_ratio, find_subtitle_band, detect_content_box,
    canvas_fill,
)
from .capture import CaptureConfig, capture_loop, finalize_session

APP_NAME = "台词匠"
APP_TITLE = APP_NAME

PREVIEW_PAD = 14        # 预览区四周留白：长图不贴边，能看出成品边界

# 主操作按钮（导出）比次要按钮更实：同一套深灰里靠明度和字重分级
PRIMARY_BTN_QSS = """
QPushButton {
    background: #3d3d3d;
    border: 1px solid #5a5a5a;
    border-radius: 6px;
    padding: 5px 14px;
    font-weight: 600;
}
QPushButton:hover { background: #4a4a4a; border-color: #6a6a6a; }
QPushButton:pressed { background: #343434; }
"""


# --------------------------------------------------------------------------- 工具函数

def pil_to_qpixmap(im: Image.Image) -> QPixmap:
    if im.mode != "RGBA":
        im = im.convert("RGBA")
    data = im.tobytes("raw", "RGBA")
    qimg = QImage(data, im.width, im.height, im.width * 4,
                  QImage.Format.Format_RGBA8888).copy()
    return QPixmap.fromImage(qimg)


def qpixmap_to_pil(pix: QPixmap) -> Image.Image:
    """QPixmap → PIL（用于拖拽遮罩的模糊截图）。"""
    img = pix.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    return Image.frombytes("RGBA", (img.width(), img.height()),
                           bytes(img.bits()))


def _logical_to_physical(rect: QRect, dpr: float) -> tuple[int, int, int, int]:
    """Qt 逻辑坐标 → 屏幕物理像素（ImageGrab 抓屏用的是物理像素）。"""
    if dpr <= 0:
        dpr = 1.0
    x1, y1 = round(rect.left() * dpr), round(rect.top() * dpr)
    x2, y2 = round(rect.right() * dpr), round(rect.bottom() * dpr)
    return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


def make_app_icon(size: int = 256) -> QIcon:
    """台词匠的极简 logo：深色圆角底 + 三条等高、左对齐、宽度递减的胶囊条。

    第一条亮蓝代表保留的完整画面，下面两条渐弱的白条代表台词。
    关键是等高等距 + 宽度递减，形成规整的"文本行"节奏。
    """
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(21, 21, 21))
    painter.drawRoundedRect(0, 0, size, size, size * 0.12, size * 0.12)

    bar = size * 0.10           # 条高一致
    gap = size * 0.075          # 间距一致
    left = size * 0.24
    widths = (0.52, 0.40, 0.28)  # 宽度递减（相对 size）
    colors = (QColor(76, 194, 255), QColor(255, 255, 255, 235),
              QColor(255, 255, 255, 150))
    total = 3 * bar + 2 * gap
    y = (size - total) / 2
    for width_factor, color in zip(widths, colors):
        painter.setBrush(color)
        w = size * width_factor
        painter.drawRoundedRect(QRect(round(left), round(y), round(w), round(bar)),
                                bar / 2, bar / 2)
        y += bar + gap
    painter.end()
    return QIcon(pix)


def short(text: str, n: int = 22) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def fit_thumb(image: Image.Image, max_h: int = 72) -> Image.Image:
    """按高度等比缩放（长图会变成窄条，但内容完整可见）。"""
    w, h = image.size
    s = max_h / max(1, h)
    return image.resize((max(1, round(w * s)), max_h), Image.Resampling.LANCZOS)


def square_thumb(image: Image.Image, size: int = 60) -> Image.Image:
    """居中裁成正方形的缩略图，避免列表里出现大片空白。"""
    w, h = image.size
    side = max(1, min(w, h))
    left = (w - side) // 2
    top = (h - side) // 2
    return image.crop((left, top, left + side, top + side)).resize(
        (size, size), Image.Resampling.LANCZOS)


# --------------------------------------------------------------------------- 框选画布

class CropCanvas(QWidget):
    """显示当前图片，支持鼠标拖框 / 移动框 / 八控制点缩放框。

    所有对外坐标都是「原图像素坐标」；内部按显示比例换算。
    """
    rectChanged = Signal(object)
    HANDLES = ("nw", "n", "ne", "e", "se", "s", "sw", "w")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pix: QPixmap | None = None
        self._full = (0, 0)
        self._view = QRect()
        self._rect = CropRect()
        self._mode = ""
        self._handle = ""
        self._start_full = QPoint()
        self._orig = CropRect()
        self._scale = 1.0
        self.setMinimumHeight(210)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(58, 58, 58))
        self.setPalette(pal)

    # ---- 对外接口
    def set_content(self, item: ImageItem | None, rect: CropRect | None):
        if item is not None and item.preview is not None:
            if not hasattr(item, "qpix"):
                item.qpix = pil_to_qpixmap(item.preview)
            self._pix = item.qpix
            self._full = item.full_size
        else:
            self._pix = None
            self._full = (0, 0)
        self._rect = rect.copy() if rect else CropRect()
        self._recompute()
        self.update()

    def set_rect(self, rect: CropRect):
        self._rect = rect.copy()
        self.update()

    def crop_rect(self) -> CropRect:
        """注意：不能叫 rect()，那会覆盖 QWidget.rect() 导致绘制崩溃。"""
        return self._rect.copy()

    # ---- 坐标换算
    def _recompute(self):
        if self._pix is None:
            self._view = QRect()
            return
        pad = 14
        avail = QRect(pad, pad, max(1, self.width() - 2 * pad),
                      max(1, self.height() - 2 * pad))
        s = min(avail.width() / self._pix.width(), avail.height() / self._pix.height())
        w = max(1, round(self._pix.width() * s))
        h = max(1, round(self._pix.height() * s))
        self._view = QRect(avail.x() + (avail.width() - w) // 2,
                           avail.y() + (avail.height() - h) // 2, w, h)
        self._scale = self._view.width() / max(1, self._full[0])

    def _to_view(self, x: float, y: float) -> QPoint:
        return QPoint(round(self._view.x() + x * self._scale),
                      round(self._view.y() + y * self._scale))

    def _to_full(self, p: QPoint) -> QPoint:
        if self._scale <= 0:
            return QPoint(0, 0)
        return QPoint(round((p.x() - self._view.x()) / self._scale),
                      round((p.y() - self._view.y()) / self._scale))

    def _box_view(self) -> QRect:
        a = self._to_view(self._rect.x, self._rect.y)
        b = self._to_view(self._rect.x + self._rect.w, self._rect.y + self._rect.h)
        return QRect(a, b).normalized()

    def _handle_points(self) -> dict[str, QPoint]:
        b = self._box_view()
        cx, cy = b.center().x(), b.center().y()
        return {
            "nw": QPoint(b.left(), b.top()), "n": QPoint(cx, b.top()),
            "ne": QPoint(b.right(), b.top()), "e": QPoint(b.right(), cy),
            "se": QPoint(b.right(), b.bottom()), "s": QPoint(cx, b.bottom()),
            "sw": QPoint(b.left(), b.bottom()), "w": QPoint(b.left(), cy),
        }

    def _hit(self, pos: QPoint) -> str:
        if not self._rect.valid():
            return ""
        for name, p in self._handle_points().items():
            if abs(p.x() - pos.x()) <= 6 and abs(p.y() - pos.y()) <= 6:
                return name
        if self._box_view().adjusted(-2, -2, 2, 2).contains(pos):
            return "move"
        return ""

    # ---- 绘制
    def paintEvent(self, _event):
        painter = QPainter(self)
        widget_rect = QRect(0, 0, self.width(), self.height())
        painter.fillRect(widget_rect, QColor(58, 58, 58))
        if self._pix is None or self._view.isEmpty():
            painter.setPen(QColor(215, 215, 215))
            painter.drawText(widget_rect, Qt.AlignmentFlag.AlignCenter,
                             "请先在左侧导入并选中一张图片")
            painter.end()
            return

        # 棋盘底，方便辨认黑底台词图
        step = 12
        painter.setPen(Qt.PenStyle.NoPen)
        for yy in range(self._view.top(), self._view.bottom(), step):
            for xx in range(self._view.left(), self._view.right(), step):
                dark = ((xx // step) + (yy // step)) % 2 == 0
                painter.fillRect(xx, yy, step, step,
                                 QColor(96, 96, 96) if dark else QColor(120, 120, 120))
        painter.drawPixmap(self._view, self._pix)
        painter.setPen(QPen(QColor(30, 30, 30), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self._view.adjusted(0, 0, -1, -1))

        if self._rect.valid():
            b = self._box_view()
            # 框外压暗
            dim = QColor(0, 0, 0, 120)
            regions = [
                QRect(self._view.left(), self._view.top(), self._view.width(),
                      max(0, b.top() - self._view.top())),
                QRect(self._view.left(), b.bottom() + 1, self._view.width(),
                      max(0, self._view.bottom() - b.bottom())),
                QRect(self._view.left(), b.top(), max(0, b.left() - self._view.left()),
                      b.height()),
                QRect(b.right() + 1, b.top(), max(0, self._view.right() - b.right()),
                      b.height()),
            ]
            for r in regions:
                if r.width() > 0 and r.height() > 0:
                    painter.fillRect(r.intersected(self._view), dim)
            painter.setPen(QPen(QColor(255, 72, 72), 2, Qt.PenStyle.SolidLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(b.adjusted(0, 0, -1, -1))
            # 八控制点
            painter.setPen(QPen(QColor(255, 72, 72), 1))
            painter.setBrush(QBrush(QColor(255, 255, 255)))
            for p in self._handle_points().values():
                painter.drawRect(QRect(p.x() - 4, p.y() - 4, 8, 8))
            tag = f"裁剪框  {self._rect.w} × {self._rect.h} px  @ ({self._rect.x}, {self._rect.y})"
            # 固定在画布左上角，不遮挡字幕区域
            tw = painter.fontMetrics().horizontalAdvance(tag) + 14
            painter.fillRect(QRect(6, 6, tw, 24), QColor(0, 0, 0, 170))
            painter.setPen(QPen(QColor(255, 150, 150), 1))
            painter.drawText(QPoint(13, 23), tag)
        else:
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(self._view, Qt.AlignmentFlag.AlignCenter,
                             "按住鼠标左键，拖出字幕所在的矩形区域")
        painter.end()

    # ---- 交互
    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton or self._pix is None:
            return super().mousePressEvent(e)
        pos = e.position().toPoint()
        hit = self._hit(pos)
        self._orig = self._rect.copy()
        self._start_full = self._to_full(pos)
        if hit in self.HANDLES:
            self._mode, self._handle = "resize", hit
        elif hit == "move":
            self._mode, self._handle = "move", ""
        else:
            self._mode, self._handle = "create", ""
            p = self._clamp_point(self._start_full)
            self._rect = CropRect(p.x(), p.y(), 0, 0)
            self.update()
        e.accept()

    def mouseMoveEvent(self, e):
        pos = e.position().toPoint()
        if not self._mode:
            hit = self._hit(pos)
            cursors = {
                "nw": Qt.CursorShape.SizeFDiagCursor, "se": Qt.CursorShape.SizeFDiagCursor,
                "ne": Qt.CursorShape.SizeBDiagCursor, "sw": Qt.CursorShape.SizeBDiagCursor,
                "n": Qt.CursorShape.SizeVerCursor, "s": Qt.CursorShape.SizeVerCursor,
                "e": Qt.CursorShape.SizeHorCursor, "w": Qt.CursorShape.SizeHorCursor,
                "move": Qt.CursorShape.SizeAllCursor,
            }
            self.setCursor(cursors.get(hit, Qt.CursorShape.CrossCursor))
            return super().mouseMoveEvent(e)

        cur = self._to_full(pos)
        if self._mode == "create":
            p0 = self._clamp_point(self._start_full)
            p1 = self._clamp_point(cur)
            # QRect.width() 是包含式(+1)，这里必须用坐标差，否则框会比图片宽 1px
            x0, x1 = min(p0.x(), p1.x()), max(p0.x(), p1.x())
            y0, y1 = min(p0.y(), p1.y()), max(p0.y(), p1.y())
            self._rect = CropRect(x0, y0, x1 - x0, y1 - y0)
        elif self._mode == "move":
            dx, dy = cur.x() - self._start_full.x(), cur.y() - self._start_full.y()
            x = max(0, min(self._orig.x + dx, self._full[0] - self._orig.w))
            y = max(0, min(self._orig.y + dy, self._full[1] - self._orig.h))
            self._rect = CropRect(x, y, self._orig.w, self._orig.h)
        else:
            self._rect = self._resize_to(cur)
        self.update()
        self.rectChanged.emit(self._rect.copy())
        e.accept()

    def mouseReleaseEvent(self, e):
        if self._mode:
            if self._rect.w < 8 or self._rect.h < 8:
                self._rect = CropRect()
            self._mode = ""
            self._handle = ""
            self.update()
            self.rectChanged.emit(self._rect.copy())
        return super().mouseReleaseEvent(e)

    def _clamp_point(self, p: QPoint) -> QPoint:
        return QPoint(max(0, min(p.x(), self._full[0])),
                      max(0, min(p.y(), self._full[1])))

    def _resize_to(self, cur: QPoint) -> CropRect:
        o = self._orig
        x0, y0, x1, y1 = o.x, o.y, o.x + o.w, o.y + o.h
        h = self._handle
        if "w" in h:
            x0 = min(cur.x(), x1 - 8)
        if "e" in h:
            x1 = max(cur.x(), x0 + 8)
        if "n" in h:
            y0 = min(cur.y(), y1 - 8)
        if "s" in h:
            y1 = max(cur.y(), y0 + 8)
        x0 = max(0, x0)
        y0 = max(0, y0)
        x1 = min(self._full[0], x1)
        y1 = min(self._full[1], y1)
        return CropRect(x0, y0, max(0, x1 - x0), max(0, y1 - y0))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._recompute()
        self.update()


# --------------------------------------------------------------------------- 缩略图列表

class RowDelegate(QStyledItemDelegate):
    """列表行自绘：整行圆角高亮，去掉 Win11 原生的浮块+青色竖条。

    背景自己画，图标/文字仍交给系统委托（配色跟随当前主题，不会变白底）。
    """

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        hovered = bool(opt.state & QStyle.StateFlag.State_MouseOver)
        painter.save()
        r = opt.rect.adjusted(2, 3, -2, -3)
        if selected or hovered:
            if selected:
                painter.setBrush(QColor(255, 255, 255, 34))   # 中性高亮，不用系统强调色
            else:
                painter.setBrush(QColor(255, 255, 255, 16))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(r, 7, 7)
        painter.restore()
        opt.state &= ~(QStyle.StateFlag.State_Selected
                       | QStyle.StateFlag.State_MouseOver
                       | QStyle.StateFlag.State_HasFocus)
        opt.backgroundBrush = QBrush(Qt.BrushStyle.NoBrush)
        super().paint(painter, opt, index)


class ThumbList(QListWidget):
    orderChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # 竖排列表：图标 + 文字一行，条目紧凑、没有大块留白
        # 注意 ListMode 下不能再设 setFlow / setWrapping（那是 IconMode 的参数，
        # 一旦设成 LeftToRight 列表会变成横向流动）
        self.setViewMode(QListWidget.ViewMode.ListMode)
        self.setIconSize(QSize(56, 56))
        self.setSpacing(2)
        self.setUniformItemSizes(True)
        self.setWordWrap(False)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setMinimumWidth(200)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)          # 悬停态需要
        self.setItemDelegate(RowDelegate(self))

    def dropEvent(self, e):
        super().dropEvent(e)
        self.orderChanged.emit()


# --------------------------------------------------------------------------- 结果预览

class NoWheelSpinBox(QSpinBox):
    """数值框永不响应滚轮——滚轮只用来滚动参数面板，不会误改数值。

    （即使焦点正好落在这个框上也不响应，否则「点过某个输入框后再滚动」
    就会把值滚掉，是最容易误改的来源。）
    """

    def wheelEvent(self, e):
        e.ignore()


class NoWheelComboBox(QComboBox):
    """下拉框同样不响应滚轮，避免滚动面板时把选项滚掉。"""

    def wheelEvent(self, e):
        e.ignore()


class VScrollArea(QScrollArea):
    """垂直滚动区：宽度自适应视口，高度按内容算准（避免底部控件被切掉）。"""

    def resizeEvent(self, e):
        super().resizeEvent(e)
        inner = self.widget()
        if inner is not None and inner.layout() is not None:
            need = inner.layout().minimumSize().height()
            if need > 0:
                inner.setMinimumHeight(need)


class ResultView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.setWidgetResizable(False)
        self.scroll.setAutoFillBackground(True)
        pal = self.scroll.viewport().palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(70, 70, 70))
        self.scroll.viewport().setPalette(pal)
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.setWidget(self.label)
        lay.addWidget(self.scroll)
        self._pix: QPixmap | None = None
        self._hint = ""
        self._fit = True
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._refresh)
        self.set_hint("导入图片并框选台词区域后，这里会实时显示拼接结果。")

    def _later_refresh(self):
        # 布局稳定后再算一次，避免首次拿到错误尺寸
        self._refresh_timer.start(0)

    def set_fit(self, value: bool):
        self._fit = value
        self._refresh()
        self._later_refresh()

    def fit(self) -> bool:
        return self._fit

    def set_pixmap(self, pix: QPixmap):
        self._pix = pix
        self._hint = ""
        self._refresh()
        self._later_refresh()

    def set_hint(self, text: str):
        self._pix = None
        self._hint = text
        self._refresh()
        self._later_refresh()

    def set_backdrop(self, transparent: bool):
        """透明背景时把预览底色压暗一档，边界看得更清楚。"""
        pal = self.scroll.viewport().palette()
        pal.setColor(QPalette.ColorRole.Window,
                     QColor(52, 52, 52) if transparent else QColor(70, 70, 70))
        self.scroll.viewport().setPalette(pal)

    def _refresh(self):
        # 适应窗口时关掉滚动条：否则「图片撑大 → 出滚动条 → 视口变小 → 图片更溢出」
        if self._fit:
            self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        else:
            self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        base = self.scroll.size()  # 外部尺寸，不受滚动条影响
        if self._hint:
            self.label.setPixmap(QPixmap())
            self.label.setStyleSheet("")
            self.label.setText(f'<div style="color:#e8e8e8;font-size:13px;'
                               f'padding:24px;line-height:1.7">{self._hint}</div>')
            self.label.resize(max(320, base.width() - 8), max(120, base.height() - 8))
            return
        if self._pix is None:
            return
        if self._fit:
            # 四周留白：长图贴边时看不出成品边界
            target = QSize(max(1, base.width() - 2 * PREVIEW_PAD),
                           max(1, base.height() - 2 * PREVIEW_PAD))
            shown = self._pix.scaled(target, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
        else:
            shown = self._pix
        self.label.setPixmap(shown)
        # 1px 细边框：黑底长图与深灰预览底之间有个分界
        self.label.setStyleSheet(
            "border: 1px solid rgba(255, 255, 255, 40); border-radius: 3px;")
        self.label.resize(shown.size() + QSize(2, 2))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._refresh()


# --------------------------------------------------------------------------- 起始页 / 历史侧栏

CARD_QSS = """
#dropCard {
    border-radius: 16px;
    border: 1px dashed rgba(140, 140, 140, 0.55);
    background: rgba(140, 140, 140, 0.07);
}
#historyCard {
    border-radius: 14px;
    border: 1px solid rgba(140, 140, 140, 0.28);
    background: rgba(140, 140, 140, 0.06);
}
#histItem {
    border-radius: 8px;
}
"""


class BreathingButton(QPushButton):
    """会缓慢呼吸的圆按钮（自绘）。

    不用 QGraphicsOpacityEffect：它会把控件先渲染到离屏图，缩放屏（125%/150%）
    上再放大 → 圆边和「+」都发虚。自绘在任意 DPI 下都是矢量级清晰。
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._phase = 0.0
        self._hover = False
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self):
        self._phase = (self._phase + 0.018) % 1.0
        if not self._hover:
            self.update()

    def _alpha(self) -> float:
        if self._hover:
            return 1.0
        return 0.66 + 0.34 * (0.5 + 0.5 * math.cos(2 * math.pi * self._phase))

    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        a = self._alpha()
        rect = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        fill = 46 if self._hover else 26
        p.setBrush(QColor(150, 150, 150, int(fill * a)))
        p.setPen(QPen(QColor(158, 158, 158, int(215 * a)), 1.6))
        p.drawEllipse(rect)
        p.setFont(self.font())
        p.setPen(QColor(238, 238, 238, int(245 * a)))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text())
        p.end()


def card_thumb(image: Image.Image, w: int = 200, h: int = 125) -> Image.Image:
    """等比放大到覆盖 w×h 后居中裁剪——所有卡片视觉比例统一（类似达芬奇的海报帧）。"""
    iw, ih = image.size
    scale = max(w / iw, h / ih)
    nw, nh = max(w, round(iw * scale)), max(h, round(ih * scale))
    img = image.resize((nw, nh), Image.Resampling.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def make_new_card_icon(size: int = 256) -> QIcon:
    """「新建拼图」卡片图标：深色圆角底 + 白色粗加号。"""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(21, 21, 21))
    r = size * 0.12
    painter.drawRoundedRect(0, 0, size, size, r, r)
    pen = QPen(QColor(255, 255, 255), size * 0.07, Qt.PenStyle.SolidLine,
               Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    c = size / 2
    arm = size * 0.26
    painter.drawLine(QPoint(round(c - arm), round(c)), QPoint(round(c + arm), round(c)))
    painter.drawLine(QPoint(round(c), round(c - arm)), QPoint(round(c), round(c + arm)))
    painter.end()
    return QIcon(pix)


class HistoryPanel(QWidget):
    """达芬奇式首页卡片墙：第一格「新建拼图」，其余是历史拼图卡片。

    左键 = 打开工程（恢复那批图和参数）；右键 = 打开输出文件夹。
    """
    newRequested = Signal()
    openRequested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._icon_cache: dict[str, QIcon] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.setViewMode(QListWidget.ViewMode.IconMode)
        # 行优先网格：LeftToRight + wrapping（TopToBottom 是列优先，会竖着排一列）
        self.list.setFlow(QListWidget.Flow.LeftToRight)
        self.list.setWrapping(True)
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.setIconSize(QSize(200, 125))
        self.list.setGridSize(QSize(224, 188))
        self.list.setSpacing(12)
        self.list.setUniformItemSizes(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.list.setStyleSheet(
            "QListWidget::item:hover { background: rgba(140,140,140,0.16);"
            " border-radius: 10px; }")
        self.list.itemClicked.connect(self._on_click)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._on_context)
        lay.addWidget(self.list)

    def set_records(self, records: list[dict]):
        self.list.clear()
        new_item = QListWidgetItem(make_new_card_icon(), "新建拼图")
        new_item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter |
                                  Qt.AlignmentFlag.AlignBottom)
        new_item.setData(Qt.ItemDataRole.UserRole, {"__new__": True})
        self.list.addItem(new_item)
        for rec in records:
            name = Path(str(rec.get("output", ""))).stem or "未命名"
            stamp = str(rec.get("time", ""))
            text = f"{short(name, 14)}\n{stamp}"
            item = QListWidgetItem(self._thumb_icon(rec), text)
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter |
                                  Qt.AlignmentFlag.AlignBottom)
            item.setToolTip(str(rec.get("output", "")))
            item.setData(Qt.ItemDataRole.UserRole, rec)
            self.list.addItem(item)

    def _on_click(self, item):
        rec = item.data(Qt.ItemDataRole.UserRole)
        if not rec:
            return
        if rec.get("__new__"):
            self.newRequested.emit()
        else:
            self.openRequested.emit(rec)

    def _on_context(self, pos):
        item = self.list.itemAt(pos)
        if item is None:
            return
        rec = item.data(Qt.ItemDataRole.UserRole)
        if rec and not rec.get("__new__"):
            self._open_folder(rec)

    def _thumb_icon(self, rec: dict) -> QIcon:
        thumb = str(rec.get("thumb", ""))
        if not thumb:
            return QIcon()
        if thumb in self._icon_cache:
            return self._icon_cache[thumb]
        icon = QIcon()
        try:
            with Image.open(thumb) as im:
                icon = QIcon(pil_to_qpixmap(card_thumb(im.convert("RGB"), 200, 125)))
        except Exception:
            icon = QIcon()
        self._icon_cache[thumb] = icon
        return icon

    def _open_folder(self, rec: dict):
        out = str(rec.get("output", ""))
        if not out:
            return
        folder = Path(out).parent
        if folder.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


def _asset_path(name: str) -> str:
    """资源文件路径（开发：subtitle_puzzle/assets；打包后：_MEIPASS 里）。"""
    for base in (getattr(sys, "_MEIPASS", None),
                 str(Path(__file__).resolve().parent)):
        if not base:
            continue
        for sub in ("subtitle_puzzle/assets", "assets"):
            p = os.path.join(base, sub, name)
            if os.path.exists(p):
                return p
    return ""


def _rounded_thumb(path: str, size: int, radius: int = 10) -> QPixmap:
    """把图片画成圆角卡片（带 1px 描边），避免两张收款码直接贴在深色底上。"""
    src = QPixmap(path)
    if src.isNull():
        return src
    src = src.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                     Qt.TransformationMode.SmoothTransformation)
    out = QPixmap(size, size)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    clip = QPainterPath()
    clip.addRoundedRect(0, 0, size, size, radius, radius)
    p.setClipPath(clip)
    p.drawPixmap(0, 0, src)
    p.setClipping(False)
    p.setPen(QPen(QColor(255, 255, 255, 46), 1))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(0.5, 0.5, size - 1, size - 1, radius, radius)
    p.end()
    return out


def _playful_font(size: int | None = None) -> QFont:
    """可爱向弹窗用的字体：沿用界面字体，只放大一号，避免和主界面割裂。"""
    f = QFont(QApplication.font())
    f.setPointSize(size if size else max(10, QApplication.font().pointSize() + 1))
    return f


def _link_button(text: str, tooltip: str = "") -> QPushButton:
    """统一的「链接样式」按钮（起始页与弹窗共用，保证外观一致）。

    命中区比文字大一圈（最小 26px 高），避免贴边小目标点不中。
    """
    btn = QPushButton(text)
    btn.setFlat(True)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setMinimumHeight(26)
    btn.setMinimumWidth(84)
    btn.setStyleSheet(
        "QPushButton { border: none; padding: 4px 10px; color: palette(link); }"
        "QPushButton:hover { text-decoration: underline; }")
    if tooltip:
        btn.setToolTip(tooltip)
    return btn


BAND_PAD_PX = 6     # 自动定位字幕条时，上下各留的安全边距（原图像素，按预览比例换算）


def band_to_rect(band, preview_size, full_size, pad: int = BAND_PAD_PX):
    """把自动定位得到的条带（预览图坐标）换算到原图坐标，并上下各留 pad 余量。

    贴边会让首尾行文字看着像被裁掉，所以左右不动、只把上下撑开一点点。
    """
    x, y, w, h = band
    pw, ph = preview_size
    fw, fh = full_size
    k = fw / max(1, pw)
    d = max(2, round(pad * k))
    y0 = max(0, round(y * k) - d)
    y1 = min(fh, round((y + h) * k) + d)
    x0 = max(0, min(fw - 1, round(x * k)))
    x1 = max(x0 + 1, min(fw, round((x + w) * k)))
    return CropRect(x0, y0, x1 - x0, max(1, y1 - y0))


QUOTE_TEXT = "让台词慢一点，让画面停一会"

FAQ_HTML = """
<h3>常见问题与解决方法</h3>

<p><b>Q：捕获开始了，按快捷键（默认 F8）停不下来？</b><br>
A：① 快捷键可能被录屏、游戏助手一类的软件占了 —— 左侧「区域」那一行会显示「被占用」，
到「高级 → 捕获快捷键」换一组就好（Ctrl+Alt 组合最不容易撞）；<br>
② 点屏幕右上角的悬浮状态条也能随时停止；<br>
③ 开始捕获只能在软件里点「● 开始捕获」按钮 —— 快捷键不负责开始，防止误触。</p>

<p><b>Q：第一次用，从哪开始？</b><br>
A：导入任意一张图（点「导入图片」，或把图片/文件夹直接拖进窗口）→ 左侧点「重选」，
在视频画面上框住「含字幕」的那块区域 → 点「● 开始捕获」→ 8 秒倒计时里切到播放器 →
播放视频，每换一句台词自动截一张；结束后按快捷键或点悬浮条停止。</p>

<p><b>Q：截出来的图全是软件界面 / 一片空白？</b><br>
A：捕获区域要框在播放器画面里，并且用全屏播放（F 或双击）。区域被别的窗口挡住时，
右上角悬浮条会变成提醒文案，切回播放器即可。</p>

<p><b>Q：截得太多，或者漏了字幕？</b><br>
A：在「高级 → 自动捕获」里调：「捕获敏感度」（框选后自动算出，偏小更敏感）、
「最小截图间隔」（防止一秒内连拍）、「采样间隔」（检测频率，越小越不容易漏）。</p>

<p><b>Q：字幕没变，却截了一堆重复的？</b><br>
A：打开「去重」开关，按相似度每句只留最清晰的一帧；「去重阈值」越大越严格。
注意去重是在捕获结束后统一跑，会多花几秒。</p>

<p><b>Q：每张图的字幕位置不一样（比如画面有缩放）？</b><br>
A：选中那一张 → 左侧勾「本图独立框选」，单独拖框；不勾则跟随统一框选。改完可「应用到全部图片」。
也可以在预览里直接拖 8 个手柄微调。</p>

<p><b>Q：拼图顺序不对 / 想删掉某几张？</b><br>
A：左栏可上下拖拽排序；右键有「上移 / 下移 / 移到最前 / 移到最后 / 移除」。
按住 Ctrl 或 Shift 多选后右键，整组一起移动或移除。</p>

<p><b>Q：拼出来太大、太糊，或怪怪的？</b><br>
A：「画面保留高度」决定每格留多少画面（默认 100% 整幅原图，0 就是纯字幕长条）；
「输出方式」有按内容自适应 / 指定宽度 / 指定比例（扩展取景）/ 指定比例（补画布）/ 指定 W × H；
「图片间距 = 0」是无缝长图，加大一点就是常见的「截图卡片」。
想每张都保留完整画面，就把「拼接模式」改成第三项。</p>

<p><b>Q：想直接出 4:3 或 9:16 发社媒，怎么弄？</b><br>
A：在「输出尺寸 → 输出方式」选「指定比例（扩展取景）」，点下面的纯比例预设（3:4 / 9:16 / 1:1 / 4:3 / 16:9）即可；
它是以字幕框为基准从原图多取画面把画布撑成目标比例，不是补黑边。「方向」可选自动 / 横屏 / 竖屏，
台词多到横屏放不下时会自动转成竖屏。<br>
「字幕位置」默认「偏下」：取景下沿贴住原片底边，缺的高度从上方多取画面，不产生黑边；
切「居中」会把字幕挪到每格中间，代价是下方补一段背景色。高级里「字幕微调」还能上下挪 ±150px。</p>

<p><b>Q：导出到哪？会动我的原图吗？</b><br>
A：原图只读，不会被改。输出默认在工程目录的 output 里，可在「导出 → 导出目录」改；
文件名支持 {date}、{n}、{page} 变量，导出前「将保存为」会显示实际文件名。
格式可选 PNG（无损）/ JPEG / WEBP。</p>

<p><b>Q：截屏时右上角那条提示会被录进画面吗？</b><br>
A：不会。它被系统排除在截屏之外（Windows 10 2004 以后支持），只会出现在你的屏幕上。
放心截，画面里只有你的剧 (๑•̀ㅂ•́)و✧</p>

<p style="margin-top: 20px;"><b>还有问题?请联系:yi.run@qq.com</b></p>
"""


class StartPage(QWidget):
    """达芬奇式首页：没历史时显示大导入卡；有历史后变成网格卡片墙。"""

    importFilesRequested = Signal()
    importFolderRequested = Signal()
    newPuzzleRequested = Signal()
    historyOpenRequested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.records: list[dict] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self.stack = QStackedWidget()
        lay.addWidget(self.stack)

        # 页脚：只留左下角那一句话（「遇到问题？」已移到工作区顶部）
        foot = QHBoxLayout()
        foot.setContentsMargins(0, 0, 0, 6)
        foot.setSpacing(8)
        qf = QFont(self.font())
        qf.setPointSize(max(9, self.font().pointSize() - 1))
        self.lbl_quote = QLabel(QUOTE_TEXT)
        self.lbl_quote.setFont(qf)
        self.lbl_quote.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        self.lbl_quote.setToolTip("把视频里一句句台词，截成一张可以慢慢看的图")
        foot.addWidget(self.lbl_quote)
        foot.addStretch(1)
        lay.addLayout(foot)

        # 页 0：空状态的大导入卡
        self.card = QFrame()
        self.card.setObjectName("dropCard")
        self.card.setStyleSheet(CARD_QSS)
        cl = QVBoxLayout(self.card)
        cl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cl.setSpacing(16)
        self.plus = BreathingButton("+")
        self.plus.setObjectName("plusBtn")
        self.plus.setFixedSize(96, 96)
        plus_font = QFont(self.font())
        plus_font.setPointSize(34)
        self.plus.setFont(plus_font)
        self.plus.setCursor(Qt.CursorShape.PointingHandCursor)
        self.plus.setToolTip("新建拼图：挑选图片开始拼\n导入素材：选择文件夹整批导入")
        self.plus.clicked.connect(self._show_new_menu)
        cl.addWidget(self.plus, 0, Qt.AlignmentFlag.AlignHCenter)
        self.hint = QPushButton("导入 / 新建")
        self.hint.setFlat(True)
        hint_font = QFont(self.font())
        hint_font.setPointSize(13)
        self.hint.setFont(hint_font)
        self.hint.setCursor(Qt.CursorShape.PointingHandCursor)
        self.hint.setToolTip("新建拼图：挑选图片开始拼\n导入素材：选择文件夹整批导入。\n"
                             "自动捕获视频画面：导入任意一张图后，在工作区左侧即可使用")
        self.hint.clicked.connect(self._show_new_menu)
        cl.addWidget(self.hint, 0, Qt.AlignmentFlag.AlignHCenter)
        self.stack.addWidget(self.card)

        # 页 1：历史卡片墙
        self.panel = HistoryPanel()
        self.panel.newRequested.connect(self.newPuzzleRequested)
        self.panel.openRequested.connect(self.historyOpenRequested)
        self.stack.addWidget(self.panel)

    def _show_new_menu(self):
        """点 + 或「导入 / 新建」：弹窗二选一（新建拼图 / 导入素材）。"""
        dlg = NewChoiceDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if dlg.choice == "new":
            self.newPuzzleRequested.emit()
        elif dlg.choice == "import":
            self.importFolderRequested.emit()

    def set_records(self, records: list[dict]):
        self.records = records
        self.panel.set_records(records)
        self.stack.setCurrentIndex(1 if records else 0)


# --------------------------------------------------------------------------- 自动捕获 GUI 部件

class CaptureSignals(QObject):
    """捕获线程 → 主线程的信号桥（Qt 信号跨线程自动走队列连接）。"""
    frame_saved = Signal(int)                 # 已截帧数
    phase = Signal(str)                       # 阶段文本（如「正在去重」）
    finished = Signal(str, object, int, int)  # 会话目录, 保留帧路径列表, 保留数, 剔除数


class GlobalHotkey(QObject):
    """全局热键：即使焦点在视频播放器上也能收到。

    Windows RegisterHotKey + 独立线程消息循环，零第三方依赖；支持任意
    「修饰键 + 键」组合（由 sequence_to_hotkey 从 QKeySequence 换算）。
    """
    fired = Signal()
    WM_HOTKEY = 0x0312
    WM_QUIT_MSG = 0x0012
    MOD_NOREPEAT = 0x4000

    def __init__(self, parent=None):
        super().__init__(parent)
        self._vk = 0x77          # 默认 F8
        self._mods = 0
        self._thread: threading.Thread | None = None
        self._tid = 0
        self.ok = False

    def configure(self, vk: int, mods: int) -> bool:
        """换一组键重新注册；返回是否注册成功（被占用则 False）。"""
        self.stop()
        self._vk, self._mods = vk, mods
        self.ok = False
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="global-hotkey")
        self._thread.start()
        for _ in range(30):      # 最多等 300ms 拿注册结果
            time.sleep(0.01)
            if self.ok:
                return True
            if not self._thread.is_alive():
                return False
        return self.ok

    def start(self) -> bool:
        return self.configure(self._vk, self._mods)

    def _run(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._tid = kernel32.GetCurrentThreadId()
        HOTKEY_ID = 0xB00F
        if not user32.RegisterHotKey(None, HOTKEY_ID,
                                     self._mods | self.MOD_NOREPEAT, self._vk):
            self._tid = 0
            return  # 被其它程序占用
        self.ok = True
        from ctypes import wintypes
        m = wintypes.MSG()
        while True:
            r = user32.GetMessageW(ctypes.byref(m), None, 0, 0)
            if r <= 0:
                break
            if m.message == self.WM_HOTKEY and m.wParam == HOTKEY_ID:
                self.fired.emit()
        user32.UnregisterHotKey(None, HOTKEY_ID)
        self.ok = False

    def stop(self):
        if self._tid and sys.platform == "win32":
            # PostThreadMessage 在 user32（不是 kernel32）
            ctypes.windll.user32.PostThreadMessageW(
                self._tid, self.WM_QUIT_MSG, 0, 0)
        self._tid = 0
        # 必须等旧线程真正退出（UnregisterHotKey 执行完），否则立刻注册新键
        # 会被自己占着的旧键挡掉，表现为「换快捷键失败」
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=0.5)
        self._thread = None


class RegionSelectOverlay(QDialog):
    """全屏半透明浮层：拖出视频区域，回车/双击确认，Esc 取消。"""
    accepted_rect = Signal(QRect)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint)
        self.setModal(True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        screen = self.screen() or QApplication.primaryScreen()
        self.setGeometry(screen.geometry())
        self._rect = QRect()
        self._drag_from: QPoint | None = None
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 130))
        if not self._rect.isEmpty():
            p.fillRect(self._rect, QColor(255, 255, 255, 26))
            p.setPen(QPen(QColor(255, 255, 255, 225), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(self._rect)
            info = f"{self._rect.width()} × {self._rect.height()}  ·  回车/双击确认，Esc 重选"
            p.setPen(QColor(255, 255, 255))
            p.drawText(QPoint(self._rect.left() + 8,
                              max(22, self._rect.top() - 8)), info)
        else:
            p.setPen(QColor(255, 255, 255))
            f = p.font()
            f.setPointSize(15)
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "按住鼠标左键拖出一个框，框住视频画面（要包含底部字幕）\n"
                       "拖完按回车或双击确认 · Esc 取消\n"
                       "直接回车 / 按 F = 整屏捕获")
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_from = e.position().toPoint()
            self._rect = QRect()
            self.update()

    def mouseMoveEvent(self, e):
        if self._drag_from is not None:
            self._rect = QRect(self._drag_from, e.position().toPoint()).normalized()
            self.update()

    def mouseReleaseEvent(self, e):
        self._drag_from = None
        self.update()

    def mouseDoubleClickEvent(self, _e):
        self._confirm()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._confirm()
        elif e.key() == Qt.Key.Key_Escape:
            self.reject()
        elif e.key() == Qt.Key.Key_F:
            self._full_screen()
        else:
            super().keyPressEvent(e)

    def _full_screen(self):
        """整屏捕获：区域 = 整个屏幕。"""
        self._rect = QRect(self.rect())
        self.accept()

    def _confirm(self):
        if self._rect.width() < 40 or self._rect.height() < 40:
            self._full_screen()   # 没拖框直接回车 = 整屏捕获
            return
        self.accept()


class CountdownOverlay(QLabel):
    """捕获开始前的倒计时提示（大数字 + 提示语，确保一眼能看见）。"""

    def __init__(self, hint: str, seconds: int, on_done, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint |
                            Qt.WindowType.WindowTransparentForInput |
                            Qt.WindowType.WindowDoesNotAcceptFocus |
                            Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # 关键：提示窗只负责显示，绝不能抢焦点/接键，
        # 否则用户切到播放器后按 F（全屏）、Enter、空格都会失效。
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumWidth(260)
        self.setStyleSheet("background: rgba(18,18,18,235); color:#fff;"
                           "border-radius:16px; padding:18px 30px;")
        self._hint = hint
        self._left = max(1, seconds)
        self._on_done = on_done
        self._render()
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

    def _render(self):
        self.setText(
            f"<div style='font-size:38px; font-weight:600'>●  {self._left}</div>"
            f"<div style='font-size:13px; color:#cfcfcf'>{self._hint}</div>")
        self.adjustSize()

    def start(self):
        self.show()
        self.raise_()
        self._timer.start()

    def _tick(self):
        self._left -= 1
        if self._left <= 0:
            self._timer.stop()
            self.close()
            self._on_done()
        else:
            self._render()


WDA_EXCLUDEFROMCAPTURE = 0x00000011   # 让窗口「显示但不被截屏」


class CaptureBar(QLabel):
    """捕获中的悬浮状态条。

    两个关键属性：
    * 置顶（WindowStaysOnTopHint + Tool）→ 盖在视频播放器之上，随时能看到；
    * SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) → 只显示、不进截图，
      所以它不会出现在录制结果里（实测 Pillow ImageGrab 同样被排除）。
    """

    def __init__(self, hotkey: str, stop_callback, parent=None):
        super().__init__(parent)
        self._hotkey = hotkey
        self._stop = stop_callback
        self._warn = False
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint |
                            Qt.WindowType.WindowDoesNotAcceptFocus |
                            Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet("background: rgba(20,20,20,232); color:#fff;"
                           "border-radius:10px; padding:10px 18px; font-size:13px;")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_count(0)

    def showEvent(self, e):
        super().showEvent(e)
        try:
            ctypes.windll.user32.SetWindowDisplayAffinity(
                int(self.winId()), WDA_EXCLUDEFROMCAPTURE)
        except Exception:
            pass   # 老系统不支持时只是会被截进去，不影响功能

    def mousePressEvent(self, _e):
        self._stop()

    def set_warning(self, warn: bool):
        self._warn = warn
        self.set_count(0)

    def set_count(self, n: int):
        warn = "  ⚠ 请切到视频播放器" if self._warn else ""
        self.setText(f"● 捕获中 · 已截 {n} 张 · 按 {self._hotkey} 停止{warn}")
        self.adjustSize()

    def set_phase(self, text: str):
        self.setText(f"● {text} · 请稍候…")
        self.adjustSize()


_QT_TO_VK = {
    Qt.Key.Key_Space: 0x20, Qt.Key.Key_Tab: 0x09,
    Qt.Key.Key_Return: 0x0D, Qt.Key.Key_Enter: 0x0D,
    Qt.Key.Key_Escape: 0x1B, Qt.Key.Key_Backspace: 0x08,
    Qt.Key.Key_Insert: 0x2D, Qt.Key.Key_Delete: 0x2E,
    Qt.Key.Key_Home: 0x24, Qt.Key.Key_End: 0x23,
    Qt.Key.Key_PageUp: 0x21, Qt.Key.Key_PageDown: 0x22,
    Qt.Key.Key_Left: 0x25, Qt.Key.Key_Up: 0x26,
    Qt.Key.Key_Right: 0x27, Qt.Key.Key_Down: 0x28,
}


def sequence_to_hotkey(seq: QKeySequence) -> tuple[int, int] | None:
    """QKeySequence → (Windows 虚拟键码, RegisterHotKey 修饰位)。

    支持字母、数字、F1–F24 和常用功能键，可带 Ctrl/Alt/Shift/Win；
    不支持的组合返回 None（例如纯修饰键、多媒体键）。
    """
    if seq.isEmpty():
        return None
    combo = seq[0]
    try:
        key = combo.key()
        mods_q = combo.keyboardModifiers()
    except AttributeError:      # 旧版 PySide 返回 int
        raw = int(combo)
        key = raw & 0x01FFFFFF
        mods_q = Qt.KeyboardModifier(raw & 0xFE000000)

    mods = 0
    if mods_q & Qt.KeyboardModifier.ControlModifier:
        mods |= 0x0002
    if mods_q & Qt.KeyboardModifier.AltModifier:
        mods |= 0x0001
    if mods_q & Qt.KeyboardModifier.ShiftModifier:
        mods |= 0x0004
    if mods_q & Qt.KeyboardModifier.MetaModifier:
        mods |= 0x0008

    k = int(key)
    f1 = int(Qt.Key.Key_F1)
    if f1 <= k <= int(Qt.Key.Key_F24):
        return 0x70 + (k - f1), mods
    if 0x30 <= k <= 0x39 or 0x41 <= k <= 0x5A:   # 数字 / 字母
        return k, mods
    if key in _QT_TO_VK:
        return _QT_TO_VK[key], mods
    return None


class NewChoiceDialog(QDialog):
    """「新建拼图 / 导入素材」二选一弹窗（不是下拉菜单）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("开始")
        self.setModal(True)
        self.setMinimumWidth(380)
        self.choice = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(6)

        title = QLabel("开始")
        tf = QFont(self.font())
        tf.setPointSize(14)
        tf.setBold(True)
        title.setFont(tf)
        lay.addWidget(title)
        lay.addSpacing(8)

        self.btn_new = QPushButton("新建拼图")
        self.btn_new.setMinimumHeight(40)
        self.btn_new.setDefault(True)
        self.btn_new.setToolTip("进入空白工作区：可用自动捕获，或拖入照片")
        lay.addWidget(self.btn_new)
        sub1 = QLabel("进入空白工作区 —— 可用「自动捕获」录台词，或直接把照片拖进来")
        sub1.setWordWrap(True)
        sub1.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        lay.addWidget(sub1)
        lay.addSpacing(10)

        self.btn_import = QPushButton("导入素材")
        self.btn_import.setMinimumHeight(40)
        self.btn_import.setToolTip("选择文件夹，整批导入其中的图片")
        lay.addWidget(self.btn_import)
        sub2 = QLabel("选择一个文件夹，整批导入里面的图片")
        sub2.setWordWrap(True)
        sub2.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        lay.addWidget(sub2)

        self.btn_new.clicked.connect(lambda: self._pick("new"))
        self.btn_import.clicked.connect(lambda: self._pick("import"))

    def _pick(self, choice: str):
        self.choice = choice
        self.accept()


class DragHintOverlay(QWidget):
    """拖拽照片悬停时的遮罩：窗口内容模糊变暗 + 中央一行提示。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._pix: QPixmap | None = None
        self.hide()

    def refresh(self, widget: QWidget):
        self.hide()   # 先隐藏自己再截图，否则截到的是遮罩
        try:
            shot = widget.grab()
            blurred = qpixmap_to_pil(shot).filter(ImageFilter.GaussianBlur(7))
            self._pix = pil_to_qpixmap(blurred)
        except Exception:
            self._pix = widget.grab()
        self.setGeometry(widget.rect())
        self.raise_()
        self.show()

    def paintEvent(self, _e):
        p = QPainter(self)
        if self._pix is not None:
            p.drawPixmap(self.rect(), self._pix)
        p.fillRect(self.rect(), QColor(16, 18, 22, 160))
        p.setPen(QColor(255, 255, 255))
        f = p.font()
        f.setPointSize(16)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                   "松开鼠标，导入这些照片")
        p.end()


# --------------------------------------------------------------------------- 主窗口

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(make_app_icon())
        self.resize(1360, 860)
        self.setMinimumSize(1100, 700)
        self.setAcceptDrops(True)

        self.items: list[ImageItem] = []
        self.global_rect = CropRect()
        self.params = LayoutParams()
        self.scale = 1.0
        self._syncing = False
        self._dir_touched = False
        self._export_size = (0, 0)
        self._page_effect = None
        self._page_anim = None
        self.settings = QSettings("SubtitlePuzzle", "Desktop")
        self.records: list[dict] = self._load_history()

        # 自动捕获状态（监屏抓帧 → 去重留清晰 → 导入现有管线）
        self._cap_signals = CaptureSignals(self)
        self._cap_signals.frame_saved.connect(self._on_cap_frame)
        self._cap_signals.phase.connect(self._on_cap_phase)
        self._cap_signals.finished.connect(self._on_capture_finished)
        self._cap_thread: threading.Thread | None = None
        self._cap_stop: threading.Event | None = None
        self._cap_bbox: tuple[int, int, int, int] | None = None   # 屏幕物理像素
        self._cap_region: QRect | None = None                     # 屏幕逻辑坐标
        self._cap_frames = 0
        self._cap_overlay_open = False
        self._countdown: CountdownOverlay | None = None
        self._cap_bar: CaptureBar | None = None
        self._hotkey_text = "F8"
        self._hotkey = GlobalHotkey()
        self._hotkey.fired.connect(self._on_hotkey)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(130)
        self._debounce.timeout.connect(self.do_render)

        self._build_ui()
        # 拖拽照片时的模糊遮罩（覆盖工作区/起始页）
        self._drag_overlay = DragHintOverlay(self.centralWidget())
        self._drag_overlay.hide()
        # 恢复上次的捕获区域（重启后 F8 直接可用）
        self._load_capture_region()
        self._update_capture_ui()
        QTimer.singleShot(800, self._update_capture_ui)   # 热键线程就绪后刷新状态
        self._load_settings()
        self._init_hotkey()
        self._sync_from_params()
        self._sync_output_enabled()
        self._update_all()
        # 首次打开：先问一句导出到哪（不阻塞界面渲染，延迟到窗口出现后）
        QTimer.singleShot(120, self._maybe_ask_output_dir)

    def _maybe_ask_output_dir(self):
        """首次运行让用户选输出文件夹；之后不再打扰（可用「导出 → 导出目录」改）。"""
        if os.environ.get("PUZZLE_GUI_CHECK"):
            return                     # 打包自检里不弹窗
        if self.settings.value("first_run_done", False, type=bool):
            return
        self.settings.setValue("first_run_done", True)
        default_dir = str(self.ed_dir.text().strip() or "")
        box = QMessageBox(self)
        box.setWindowTitle("先选个导出文件夹")
        # 默认宽度会把「之后随时能在…」挤成两行，给文本区一个下限
        box.setStyleSheet("QLabel#qt_msgbox_label, QLabel#qt_msgbox_informativelabel"
                          " { min-width: 430px; }")
        box.setIcon(QMessageBox.Icon.NoIcon)   # 单色界面里不要系统蓝图标
        box.setText("拼好的图保存在哪里？")
        box.setInformativeText(
            "默认放在软件目录下的 output 文件夹。\n"
            "选一个自己的文件夹更顺手，之后随时能在「导出 → 导出目录」里改。")
        b_pick = box.addButton("选择文件夹…", QMessageBox.ButtonRole.AcceptRole)
        b_default = box.addButton("就用默认", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(b_pick)
        box.exec()
        if box.clickedButton() is b_pick:
            start = default_dir or str(Path.home())
            chosen = QFileDialog.getExistingDirectory(self, "选择导出文件夹", start)
            if chosen:
                self.ed_dir.setText(chosen)
                self.settings.setValue("export_dir", chosen)
                self._update_path_preview()
                self._say(f"导出目录已设为 {chosen}", "ok")
        if not self.settings.value("hotkey_tip_shown", False, type=bool):
            self.settings.setValue("hotkey_tip_shown", True)
            QTimer.singleShot(300, self._show_hotkey_tip)

    # ------------------------------------------------------------ UI 构建
    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 10, 12, 6)
        root.setSpacing(0)

        # 第 0 页 = 起始页（导入区 + 历史侧栏），第 1 页 = 工作区
        self.stack = QStackedWidget()
        self.start_page = StartPage()
        self.start_page.importFilesRequested.connect(self.import_images)
        self.start_page.importFolderRequested.connect(self.import_folder)
        self.start_page.newPuzzleRequested.connect(self.goto_workspace_empty)
        self.start_page.historyOpenRequested.connect(self.open_record)
        self.start_page.set_records(self.records)

        self.workspace = QWidget()
        wlay = QVBoxLayout(self.workspace)
        wlay.setContentsMargins(0, 0, 0, 0)
        wlay.setSpacing(0)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._build_left())
        split.addWidget(self._build_middle())
        split.addWidget(self._build_right())
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        split.setSizes([235, 700, 350])
        wlay.addWidget(split, 1)

        self.stack.addWidget(self.start_page)
        self.stack.addWidget(self.workspace)
        root.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.stack.currentChanged.connect(self._on_page_changed)

        self.status = QStatusBar()
        self.status.setSizeGripEnabled(False)
        # 空闲时不占地方：没有提示就整条隐藏，界面更干净（也不再有一行「就绪…」）
        self.status.messageChanged.connect(
            lambda t: self.status.setVisible(bool(t)))
        self.status.setVisible(False)
        self.setStatusBar(self.status)

        for key, fn in (("Ctrl+O", self.import_images), ("Ctrl+S", self.export_image),
                        ("Ctrl+R", lambda: self.do_render(True)),
                        ("Ctrl+Q", self.close)):
            QShortcut(QKeySequence(key), self).activated.connect(fn)

    def _build_left(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        btn_back = QPushButton("← 新建拼图")
        btn_back.setMinimumHeight(30)      # 与中栏标题同高，三栏首行对齐
        btn_back.clicked.connect(self.goto_start)
        lay.addWidget(btn_back)
        lay.addWidget(QLabel("<b>① 图片列表</b> · 拖拽缩略图可调顺序"))

        row = QHBoxLayout()
        self.btn_import = QPushButton("导入图片")
        self.btn_import.clicked.connect(self.import_images)
        self.btn_remove = QPushButton("移除")
        self.btn_remove.clicked.connect(self.remove_selected)
        self.btn_clear = QPushButton("清空")
        self.btn_clear.clicked.connect(self.clear_all)
        for b in (self.btn_import, self.btn_remove, self.btn_clear):
            row.addWidget(b)
        lay.addLayout(row)

        # 自动捕获：大按钮 + 区域状态行（点击开始，快捷键结束）
        self.btn_capture = QPushButton("● 开始捕获")
        self.btn_capture.setMinimumHeight(38)
        self.btn_capture.setToolTip("字幕每变一句就自动截图（PNG 无损）。\n"
                                    "结束后按快捷键（默认 F8）或点屏幕上的悬浮条停止。\n"
                                    "首次使用先框选一次视频区域，之后会记住。\n"
                                    "快捷键和参数在右侧「高级」面板顶部调整")
        self.btn_capture.clicked.connect(self.start_capture_flow)
        lay.addWidget(self.btn_capture)

        cap_row = QHBoxLayout()
        self.lbl_cap_region = QLabel("区域：未选择")
        cap_row.addWidget(self.lbl_cap_region, 1)
        self.btn_full = QToolButton()
        self.btn_full.setText("全屏")
        self.btn_full.setToolTip("整屏捕获：把整块屏幕作为捕获区域")
        self.btn_full.clicked.connect(self.capture_full_screen)
        cap_row.addWidget(self.btn_full)
        self.btn_reselect = QToolButton()
        self.btn_reselect.setText("重选")
        self.btn_reselect.setToolTip("重新框选视频区域")
        self.btn_reselect.clicked.connect(self.select_capture_region)
        cap_row.addWidget(self.btn_reselect)
        lay.addLayout(cap_row)

        self.lst = ThumbList()
        self.lst.currentItemChanged.connect(self._on_selection_changed)
        self.lst.orderChanged.connect(self._on_order_changed)
        self.lst.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.lst.customContextMenuRequested.connect(self._on_list_menu)
        lay.addWidget(self.lst, 1)

        row4 = QHBoxLayout()
        btn_sort = QPushButton("按名称排序")
        btn_sort.clicked.connect(lambda: self._sort("name"))
        btn_num = QPushButton("按编号排序")
        btn_num.clicked.connect(lambda: self._sort("number"))
        row4.addWidget(btn_sort, 1); row4.addWidget(btn_num, 1)
        lay.addLayout(row4)

        row5 = QHBoxLayout()
        btn_rev = QPushButton("当前顺序倒序")
        btn_rev.clicked.connect(lambda: self._sort("reverse"))
        row5.addWidget(btn_rev)
        lay.addLayout(row5)

        self.lbl_count = QLabel("共 0 张")
        lay.addWidget(self.lbl_count)
        return w

    def _build_middle(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        vsplit = QSplitter(Qt.Orientation.Vertical)

        top = QWidget()
        tl = QVBoxLayout(top); tl.setContentsMargins(0, 0, 0, 0); tl.setSpacing(6)
        hrow = QHBoxLayout()
        hrow.setContentsMargins(0, 0, 0, 0)
        hrow.setSpacing(8)
        head = QLabel("<b>② 框选台词区域</b> · 拖框 / 拖八个小方块微调")
        head.setToolTip("坐标单位为原图像素；拖动中间的分隔条可以把图片放大。")
        head.setMinimumHeight(30)          # 与左栏「← 新建拼图」同高，首行对齐
        head.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        hrow.addWidget(head, 1)
        # 常见问题入口放在工作区顶部右侧：干活时最可能需要，首页不占地方
        self.btn_feed_ws = _link_button("投喂作者 (๑´ㅂ`๑)", "请作者喝杯咖啡～")
        self.btn_feed_ws.setMinimumHeight(30)
        self.btn_feed_ws.clicked.connect(lambda: self._build_token_dialog().exec())
        hrow.addWidget(self.btn_feed_ws, 0, Qt.AlignmentFlag.AlignVCenter)
        self.btn_faq = _link_button("遇到问题？",
                                    "常见问题：快捷键失效、截图不对、导出位置……")
        self.btn_faq.setMinimumHeight(30)
        self.btn_faq.clicked.connect(self.show_faq)
        hrow.addWidget(self.btn_faq, 0, Qt.AlignmentFlag.AlignVCenter)
        tl.addLayout(hrow)
        self.canvas = CropCanvas()
        self.canvas.rectChanged.connect(self._on_canvas_rect)
        tl.addWidget(self.canvas, 1)
        vsplit.addWidget(top)

        bottom = QWidget()
        bl = QVBoxLayout(bottom); bl.setContentsMargins(0, 0, 0, 0); bl.setSpacing(6)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("<b>③ 实时预览</b>"))
        bar.addStretch(1)
        self.chk_fit = QCheckBox("适应窗口"); self.chk_fit.setChecked(True)
        self.btn_regen = QPushButton("重新生成")
        self.btn_regen.clicked.connect(lambda: self.do_render(True))
        self.btn_export_mid = QPushButton("导出图片")
        self.btn_export_mid.setDefault(True)
        self.btn_export_mid.setStyleSheet(PRIMARY_BTN_QSS)
        self.btn_export_mid.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_export_mid.clicked.connect(self.export_image)
        self.spin_page = self._spin(1, 999)
        self.spin_page.setFixedWidth(56)
        self.spin_page.valueChanged.connect(lambda *_: self._schedule())
        self.lbl_size = QLabel("—")
        bar.addWidget(self.chk_fit); bar.addWidget(self.btn_regen)
        bar.addWidget(QLabel("预览第")); bar.addWidget(self.spin_page)
        bar.addWidget(QLabel("页")); bar.addWidget(self.btn_export_mid)
        bar.addWidget(self.lbl_size)
        bl.addLayout(bar)
        self.result_view = ResultView()
        self.chk_fit.toggled.connect(self.result_view.set_fit)
        bl.addWidget(self.result_view, 1)
        self.warn_box = QPlainTextEdit()
        self.warn_box.setReadOnly(True)
        self.warn_box.setMaximumHeight(76)
        self.warn_box.setVisible(False)
        bl.addWidget(self.warn_box)
        vsplit.addWidget(bottom)

        vsplit.setSizes([420, 370])
        lay.addWidget(vsplit)
        return w

    def _build_right(self) -> QWidget:
        holder = QWidget()
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_basic_tab(), "基础")
        self.tabs.addTab(self._build_advanced_tab(), "高级")
        hl.addWidget(self.tabs)
        holder.setMinimumWidth(340)
        return holder

    def _panel(self):
        """一个可垂直滚动的参数面板，返回 (滚动区, 内容布局)。"""
        scroll = VScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(4, 10, 8, 10)   # 顶部留白：分组标题不贴 Tab 栏
        lay.setSpacing(8)
        scroll.setWidget(inner)
        return scroll, lay

    # ------------------------------------------------------------ 基础面板
    def _build_basic_tab(self) -> QWidget:
        scroll, lay = self._panel()

        g1 = QGroupBox("拼接")
        f1 = QFormLayout(g1)
        self.cmb_stack = NoWheelComboBox()
        # 下拉项只留短名，解释放 tooltip，避免下拉框被长文案撑得过宽
        for label, key, tip in (
            ("纯台词", "text", "全部按框选的区域裁剪，只留字幕条（最紧凑）"),
            ("首图保留画面", "first_full",
             "第一张保留整幅画面（含它自己的字幕），后面的按字幕条拼接（默认）"),
            ("每张都保留画面", "all_full",
             "每张都保留整幅画面并依次叠放，成品最长"),
        ):
            self.cmb_stack.addItem(label, key)
            self.cmb_stack.setItemData(self.cmb_stack.count() - 1, tip,
                                       Qt.ItemDataRole.ToolTipRole)
        self.cmb_stack.setCurrentIndex(1)   # 默认：首张保留画面（用户设定）
        self.cmb_stack.currentIndexChanged.connect(self._on_stack_mode_changed)
        f1.addRow("拼接模式", self.cmb_stack)

        self.sp_hero = self._spin(5, 100)
        self.sp_hero.setValue(100)
        self.sp_hero.setSuffix(" %")
        self.sp_hero.setToolTip("保留画面时，字幕上方留多少画面：\n"
                               "100% ≈ 整幅原图；75% 留大部分画面；0% 只剩字幕条。\n"
                               "字幕本身永远保留，不会被切掉。")
        f1.addRow("画面保留高度", self.sp_hero)

        self.cmb_dir = NoWheelComboBox()
        self.cmb_dir.addItem("纵向堆叠（常用）", "vertical")
        self.cmb_dir.addItem("横向排列", "horizontal")
        self.cmb_dir.currentIndexChanged.connect(self._on_direction_changed)
        f1.addRow("拼接方向", self.cmb_dir)

        self.sp_gap = self._spin(0, 2000)
        f1.addRow("图片间距", self.sp_gap)
        self.lbl_gap = QLabel("0 = 无黑缝；调大即出现间隔")
        self.lbl_gap.setWordWrap(True)
        f1.addRow("", self.lbl_gap)

        self.cmb_align = NoWheelComboBox()
        self.cmb_align.addItem("居左", "start")
        self.cmb_align.addItem("居中", "center")
        self.cmb_align.addItem("居右", "end")
        f1.addRow("对齐方式", self.cmb_align)

        brow = QHBoxLayout()
        self.swatch = QLabel("      ")
        self.swatch.setAutoFillBackground(True)
        self.btn_color = QPushButton("选择颜色")
        self.btn_color.clicked.connect(self.pick_color)
        brow.addWidget(self.swatch); brow.addWidget(self.btn_color, 1)
        f1.addRow("背景", brow)
        lay.addWidget(g1)

        g2 = QGroupBox("输出尺寸")
        f2 = QFormLayout(g2)
        self.cmb_out = NoWheelComboBox()
        for label, key in (("按内容自适应", "content"), ("指定宽度（高度等比）", "width"),
                           ("指定比例（扩展取景）", "expand"),
                           ("指定比例（补画布）", "ratio"),
                           ("指定 W × H（居中适配）", "size")):
            self.cmb_out.addItem(label, key)
        self.cmb_out.currentIndexChanged.connect(self._sync_output_enabled)
        f2.addRow("输出方式", self.cmb_out)
        self.sp_ow = self._spin(1, 20000); self.sp_ow.setValue(1080)
        f2.addRow("输出宽度", self.sp_ow)
        rrow = QHBoxLayout()
        self.ed_ratio = QLineEdit("3:4")
        self.ed_ratio.setMaximumWidth(70)
        self.ed_ratio.editingFinished.connect(self._on_ratio_text)
        self.sp_rw = self._spin(1, 999); self.sp_rw.setValue(3)
        self.sp_rh = self._spin(1, 999); self.sp_rh.setValue(4)
        self.sp_rw.valueChanged.connect(self._on_ratio_spin)
        self.sp_rh.valueChanged.connect(self._on_ratio_spin)
        rrow.addWidget(self.ed_ratio); rrow.addWidget(self.sp_rw)
        rrow.addWidget(QLabel(":")); rrow.addWidget(self.sp_rh)
        f2.addRow("比例 W:H", rrow)
        self.cmb_orient = NoWheelComboBox()
        for label, key in (("自动", "auto"), ("横屏", "landscape"), ("竖屏", "portrait")):
            self.cmb_orient.addItem(label, key)
        self.cmb_orient.setToolTip("自动：台词多到横屏放不下时，自动改为竖屏")
        self.cmb_orient.currentIndexChanged.connect(self._on_param_changed)
        f2.addRow("方向", self.cmb_orient)
        self.cmb_subpos = NoWheelComboBox()
        for label, key in (("居中", "center"), ("偏下", "bottom")):
            self.cmb_subpos.addItem(label, key)
        self.cmb_subpos.setToolTip("扩展取景时，字幕在每一小块里的垂直位置")
        self.cmb_subpos.currentIndexChanged.connect(self._on_param_changed)
        f2.addRow("字幕位置", self.cmb_subpos)
        preset = QHBoxLayout()
        preset.setSpacing(4)
        for text in ("3:4", "9:16", "1:1", "4:3", "16:9"):
            rw, rh = parse_ratio(text)
            btn = QPushButton(text)
            btn.setObjectName("ratioPreset")
            btn.setFixedWidth(52)
            btn.clicked.connect(lambda *_, a=rw, b=rh: self._on_ratio_preset(a, b))
            preset.addWidget(btn)
        preset.addStretch(1)
        f2.addRow(preset)
        lay.addWidget(g2)

        g3 = QGroupBox("导出")
        f3 = QFormLayout(g3)
        drow = QHBoxLayout()
        self.ed_dir = QLineEdit()
        self.btn_browse = QPushButton("浏览…")
        self.btn_browse.clicked.connect(self.pick_dir)
        drow.addWidget(self.ed_dir, 1); drow.addWidget(self.btn_browse)
        f3.addRow("导出目录", drow)
        self.ed_name = QLineEdit("台词拼图_{date}_{n}")
        self.ed_name.textChanged.connect(self._update_path_preview)
        f3.addRow("文件名", self.ed_name)
        self.cmb_fmt = NoWheelComboBox()
        for k in FORMAT_EXT:
            self.cmb_fmt.addItem(k, k)
        self.cmb_fmt.currentIndexChanged.connect(self._update_path_preview)
        f3.addRow("格式", self.cmb_fmt)
        self.lbl_path = QLabel("—")
        self.lbl_path.setWordWrap(True)
        self.lbl_path.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        f3.addRow("将保存为", self.lbl_path)
        self.btn_export = QPushButton("导出图片 (Ctrl+S)")
        self.btn_export.setDefault(True)
        self.btn_export.setStyleSheet(PRIMARY_BTN_QSS)
        self.btn_export.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_export.clicked.connect(self.export_image)
        self.btn_open_dir = QPushButton("打开输出文件夹")
        self.btn_open_dir.setToolTip("在资源管理器里打开当前导出目录")
        self.btn_open_dir.clicked.connect(self.open_output_folder)
        erow = QHBoxLayout()
        erow.addWidget(self.btn_export, 1)
        erow.addWidget(self.btn_open_dir)
        f3.addRow(erow)
        lay.addWidget(g3)

        lay.addStretch(1)
        return scroll

    # ------------------------------------------------------------ 高级面板
    def _build_advanced_tab(self) -> QWidget:
        scroll, lay = self._panel()

        g1 = QGroupBox("裁剪")
        f1 = QVBoxLayout(g1)
        self.chk_independent = QCheckBox("本图独立框选（逐张微调）")
        self.chk_independent.toggled.connect(self._on_independent_toggled)
        f1.addWidget(self.chk_independent)
        self.lbl_rect_from = QLabel("当前：未选中图片")
        f1.addWidget(self.lbl_rect_from)

        grid = QGridLayout()
        rect_hook = self._on_rect_field_changed
        self.sp_x = self._spin(0, 20000, rect_hook); self.sp_y = self._spin(0, 20000, rect_hook)
        self.sp_w = self._spin(0, 20000, rect_hook); self.sp_h = self._spin(0, 20000, rect_hook)
        for i, (label, sp) in enumerate((("X", self.sp_x), ("Y", self.sp_y),
                                         ("宽", self.sp_w), ("高", self.sp_h))):
            grid.addWidget(QLabel(label), i // 2, (i % 2) * 2)
            grid.addWidget(sp, i // 2, (i % 2) * 2 + 1)
        f1.addLayout(grid)
        f1.addWidget(QLabel("裁剪边距（原图像素）"))
        pgrid = QGridLayout()
        self.sp_pt = self._spin(0, 2000); self.sp_pb = self._spin(0, 2000)
        self.sp_pl = self._spin(0, 2000); self.sp_pr = self._spin(0, 2000)
        for i, (label, sp) in enumerate((("上", self.sp_pt), ("下", self.sp_pb),
                                         ("左", self.sp_pl), ("右", self.sp_pr))):
            pgrid.addWidget(QLabel(label), i // 2, (i % 2) * 2)
            pgrid.addWidget(sp, i // 2, (i % 2) * 2 + 1)
        f1.addLayout(pgrid)
        r1 = QVBoxLayout()
        r1.setSpacing(3)
        self.btn_apply_all = QPushButton("应用到全部图片")
        self.btn_apply_all.clicked.connect(self.apply_rect_to_all)
        self.btn_clear_override = QPushButton("清除本图独立框选")
        self.btn_clear_override.clicked.connect(self.clear_current_override)
        r1.addWidget(self.btn_apply_all); r1.addWidget(self.btn_clear_override)
        f1.addLayout(r1)
        self.btn_auto_rect = QPushButton("自动定位字幕条")
        self.btn_auto_rect.clicked.connect(self.auto_detect_rect)
        f1.addWidget(self.btn_auto_rect)
        lay.addWidget(g1)

        g2 = QGroupBox("拼接细节")
        f2 = QFormLayout(g2)
        self.sp_margin = self._spin(0, 2000)
        f2.addRow("整体外边距", self.sp_margin)
        self.chk_uw = QCheckBox("统一条带宽度（保持字幕对齐）")
        self.chk_uw.setChecked(True)
        self.chk_uh = QCheckBox("统一条带高度（补背景，不拉伸）")
        f2.addRow(self.chk_uw); f2.addRow(self.chk_uh)
        self.chk_transparent = QCheckBox("透明背景（仅 PNG / WebP 有效）")
        f2.addRow(self.chk_transparent)
        self.sp_suboff = self._spin(-150, 150)
        self.sp_suboff.setSuffix(" px")
        f2.addRow("字幕微调", self.sp_suboff)
        off_hint = QLabel("正值更靠下；在「字幕位置」基础上微调（仅扩展取景）")
        off_hint.setWordWrap(True)
        f2.addRow("", off_hint)
        lay.addWidget(g2)

        g3 = QGroupBox("输出细节")
        f3 = QFormLayout(g3)
        self.sp_oh = self._spin(1, 20000); self.sp_oh.setValue(1920)
        f3.addRow("输出高度", self.sp_oh)
        self.sp_per_page = self._spin(0, 500)
        f3.addRow("每页最多张数", self.sp_per_page)
        per_hint = QLabel("0 = 全部拼成一张；超出则按张数分页导出")
        per_hint.setWordWrap(True)
        f3.addRow("", per_hint)
        self.cmb_resample = NoWheelComboBox()
        for label, key in (("Lanczos（最清晰）", "LANCZOS"), ("双三次", "BICUBIC"),
                           ("双线性", "BILINEAR"), ("最近邻", "NEAREST")):
            self.cmb_resample.addItem(label, key)
        f3.addRow("缩放算法", self.cmb_resample)
        self.sp_quality = self._spin(1, 100); self.sp_quality.setValue(95)
        f3.addRow("JPEG/WebP 质量", self.sp_quality)
        lay.addWidget(g3)

        g4 = QGroupBox("自动捕获（监屏抓帧）")
        f4 = QFormLayout(g4)
        self.sp_countdown = self._spin(0, 15)
        self.sp_countdown.setValue(8)
        self.sp_countdown.setSuffix(" 秒")
        self.sp_countdown.setToolTip("点击开始后的倒计时（留出切到视频播放器的时间）。\n"
                                     "0 = 立即开始")
        f4.addRow("开始倒计时", self.sp_countdown)
        self.sp_hotkey = QKeySequenceEdit(QKeySequence("F8"))
        self.sp_hotkey.setToolTip("结束捕获的全局快捷键（焦点在播放器上也生效）。\n"
                                  "开始捕获请在软件里点「● 开始捕获」按钮。\n"
                                  "支持字母、数字、F1–F24，可加 Ctrl/Alt/Shift")
        self.sp_hotkey.editingFinished.connect(self._apply_hotkey)
        f4.addRow("捕获快捷键", self.sp_hotkey)
        self.lbl_hotkey = QLabel("当前：F8（按它停止捕获）")
        self.lbl_hotkey.setWordWrap(True)
        f4.addRow("", self.lbl_hotkey)
        self.sp_sample = self._spin(60, 2000)
        self.sp_sample.setValue(220)
        self.sp_sample.setSuffix(" ms")
        self.sp_sample.setToolTip("每隔多久抓一次屏。越小越不容易漏帧，CPU 占用略高")
        f4.addRow("采样间隔", self.sp_sample)
        self.sp_sens = self._spin(1, 10)
        self.sp_sens.setValue(7)
        self.sp_sens.setToolTip("捕获敏感度：越高 = 字幕稍有变化就截（帧多，宁多勿漏），\n"
                                "越低 = 明显换句才截（帧少）。内部映射为触发阈值 10%→1%")
        self.lbl_sens = QLabel()
        self.lbl_sens.setWordWrap(True)
        sens_row = QHBoxLayout()
        sens_row.addWidget(self.sp_sens)
        sens_row.addWidget(self.lbl_sens, 1)
        f4.addRow("捕获敏感度", sens_row)
        self.sp_sens.valueChanged.connect(self._sync_sens_label)
        self._sync_sens_label()
        self.sp_mincap = self._spin(0, 10000)
        self.sp_mincap.setValue(700)
        self.sp_mincap.setSuffix(" ms")
        self.sp_mincap.setToolTip("两次截图的最小间隔，防止字幕闪动瞬间连爆")
        f4.addRow("最小截图间隔", self.sp_mincap)
        self.chk_dedup = QCheckBox("自动去重（每句只留最清晰一张）")
        self.chk_dedup.setChecked(True)
        self.chk_dedup.setToolTip("关闭后全部截取的帧都会按顺序导入，由你手动挑选")
        f4.addRow("", self.chk_dedup)
        self.sp_dedup = self._spin(1, 100)
        self.sp_dedup.setValue(8)
        self.sp_dedup.setSuffix(" %")
        self.sp_dedup.setToolTip("去重开启时生效：字幕行差异 ≤ 此值视为同一句，\n"
                                 "每句只保留最清晰的一帧。调小 = 剔得更狠")
        f4.addRow("去重阈值", self.sp_dedup)
        self.chk_dedup.toggled.connect(self.sp_dedup.setEnabled)
        self.sp_dedup.setEnabled(self.chk_dedup.isChecked())
        cap_hint = QLabel("点「● 开始捕获」开始（先框选屏幕视频区域）；结束后按快捷键\n"
                          "或点屏幕上的悬浮条停止。截图为 PNG 无损；同一句字幕的多帧\n"
                          "只保留最清晰一张，没有字幕的画面会被自动剔除；\n"
                          "被剔除的帧留在会话文件夹里可找回。")
        cap_hint.setWordWrap(True)
        f4.addRow("", cap_hint)
        lay.insertWidget(0, g4)   # 放在高级页最上面：一眼能找到捕获开关

        lay.addStretch(1)
        return scroll

    def _spin(self, lo: int, hi: int, hook=None) -> QSpinBox:
        sp = NoWheelSpinBox()
        sp.setRange(lo, hi)
        sp.setAccelerated(True)
        sp.valueChanged.connect(hook or self._on_param_changed)
        return sp

    # ------------------------------------------------------------ 参数同步
    def _sync_from_params(self):
        self._syncing = True
        p = self.params
        self.sp_gap.setValue(p.gap)
        self.sp_margin.setValue(p.margin)
        self.sp_ow.setValue(p.output_width)
        self.sp_oh.setValue(p.output_height)
        self.sp_rw.setValue(p.ratio_w)
        self.sp_rh.setValue(p.ratio_h)
        self.ed_ratio.setText(f"{p.ratio_w}:{p.ratio_h}")
        self.cmb_orient.setCurrentIndex(max(0, self.cmb_orient.findData(p.orientation)))
        self.cmb_subpos.setCurrentIndex(max(0, self.cmb_subpos.findData(p.subtitle_pos)))
        self.sp_suboff.setValue(p.subtitle_offset)
        self._update_swatch()
        self._syncing = False

    def _collect_params(self):
        p = self.params
        p.direction = self.cmb_dir.currentData()
        p.gap = self.sp_gap.value()          # 默认 0，可调
        p.margin = self.sp_margin.value()
        p.align = self.cmb_align.currentData()
        p.padding = Padding(self.sp_pt.value(), self.sp_pb.value(),
                            self.sp_pl.value(), self.sp_pr.value())
        p.uniform_width = self.chk_uw.isChecked()
        p.uniform_height = self.chk_uh.isChecked()
        p.stack_mode = self.cmb_stack.currentData() or "text"
        p.hero_ratio = self.sp_hero.value() / 100.0
        p.transparent = self.chk_transparent.isChecked()
        p.output_mode = self.cmb_out.currentData()
        p.output_width = self.sp_ow.value()
        p.output_height = self.sp_oh.value()
        p.ratio_w = self.sp_rw.value()
        p.ratio_h = self.sp_rh.value()
        p.orientation = self.cmb_orient.currentData() or "auto"
        p.subtitle_pos = self.cmb_subpos.currentData() or "center"
        p.subtitle_offset = self.sp_suboff.value()
        p.items_per_page = self.sp_per_page.value()
        p.resample = self.cmb_resample.currentData()

    def _sync_output_enabled(self, *_):
        mode = self.cmb_out.currentData()
        self.sp_ow.setEnabled(mode in ("width", "size"))
        self.sp_oh.setEnabled(mode == "size")
        self.sp_rw.setEnabled(mode in ("ratio", "expand"))
        self.sp_rh.setEnabled(mode in ("ratio", "expand"))
        if hasattr(self, "cmb_orient"):
            self.cmb_orient.setEnabled(mode == "expand")
        self._schedule()

    def _update_swatch(self):
        r, g, b = self.params.background
        pal = self.swatch.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(r, g, b))
        self.swatch.setPalette(pal)
        self.btn_color.setText("选择背景颜色")

    def pick_color(self):
        r, g, b = self.params.background
        c = QColorDialog.getColor(QColor(r, g, b), self, "选择拼图背景色")
        if c.isValid():
            self.params.background = (c.red(), c.green(), c.blue())
            self._update_swatch()
            self._schedule()

    def pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择导出目录", self.ed_dir.text())
        if d:
            self.ed_dir.setText(d)
            self._dir_touched = True
            self._update_path_preview()
            self._save_settings()

    # ------------------------------------------------------------ 图片管理
    def import_images(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择台词截图（可多选）", self.ed_dir.text() or str(Path.home()),
            "图片文件 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff);;所有文件 (*)")
        if files:
            self.add_images([Path(f) for f in files])

    def add_images(self, paths: list[Path], sort: bool = True):
        if sort:
            # 自然排序：IMG_9 排在 IMG_10 之前（借鉴 MIT 项目的 natural_key）
            paths = sorted(paths, key=lambda p: natural_key(p.name))
        existing = {str(it.path.resolve()) for it in self.items}
        new: list[ImageItem] = []
        errors: list[str] = []
        for p in paths:
            if p.suffix.lower() not in SUPPORTED_EXT:
                errors.append(f"{p.name}：不支持的格式")
                continue
            if str(p.resolve()) in existing:
                continue
            try:
                size = probe_size(p)
                new.append(ImageItem(path=p, full_size=size))
                existing.add(str(p.resolve()))
            except Exception as exc:
                errors.append(f"{p.name}：{exc}")
        if not new:
            self._say("没有新增图片。" + (" ".join(errors) if errors else ""), "warn")
            return

        self.items.extend(new)
        sizes = [it.full_size for it in self.items]
        self.scale = project_scale(sizes)
        for it in self.items:
            if it.preview is None and not it.load_error:
                try:
                    it.preview = load_preview(it.path, self.scale)
                    it.qpix = pil_to_qpixmap(it.preview)
                except Exception as exc:
                    it.load_error = str(exc)

        # 首图给出建议框选：优先自动定位字幕条，失败则退回经验值
        if not self.global_rect.valid() and self.items:
            it0 = self.items[0]
            band = find_subtitle_band(it0.preview) if it0.preview is not None else None
            if band:
                self.global_rect = band_to_rect(
                    band, it0.preview.size, it0.full_size)
                self._say(f"已自动定位第一张图的字幕条（{self.global_rect.w}×{self.global_rect.h}），"
                          "拖动红框可校准。", "info")
            elif it0.full_size[0] and it0.full_size[1]:
                w, h = it0.full_size
                self.global_rect = CropRect(0, round(h * 0.78), w, round(h * 0.20))
                self._say("已根据第一张图片预填字幕区域，请拖动红框校准后再导出。", "info")

        if not self._dir_touched and self.items:
            self.ed_dir.setText(str(self.items[0].path.parent))
            self._save_settings()

        self._renumber()
        if self.lst.currentRow() < 0:
            self.lst.setCurrentRow(0)
        self._update_all()
        self.stack.setCurrentIndex(1)   # 导入后进入工作区
        msg = f"已导入 {len(new)} 张，共 {len(self.items)} 张。"
        if errors:
            msg += " 跳过：" + "；".join(errors[:3])
        self._say(msg, "ok" if not errors else "warn")

    def _renumber(self):
        self._syncing = True
        self.lst.clear()
        for i, it in enumerate(self.items):
            thumb = getattr(it, "thumb", None)
            if thumb is None and it.preview is not None:
                try:
                    thumb = square_thumb(it.preview, 60)
                    it.thumb = thumb
                except Exception:
                    thumb = None
            icon = QIcon(pil_to_qpixmap(thumb)) if thumb is not None else QIcon()
            item = QListWidgetItem(icon, f"{i + 1:02d}  {short(it.path.stem, 16)}")
            item.setSizeHint(QSize(0, 70))   # 统一行高，悬停/选中块不会忽高忽低
            item.setData(Qt.ItemDataRole.UserRole, str(it.path))
            item.setToolTip(it.path.name + ("（读取失败）" if it.load_error else ""))
            self.lst.addItem(item)
        self.lbl_count.setText(f"共 {len(self.items)} 张 · 预览缩放 {self.scale:.2f}×")
        self._syncing = False

    def _reorder_from_list(self):
        """拖拽后按列表顺序重排 items（用路径映射，避免重复文件歧义）。"""
        order: list[ImageItem] = []
        pool = list(self.items)
        for i in range(self.lst.count()):
            path = self.lst.item(i).data(Qt.ItemDataRole.UserRole)
            for j, it in enumerate(pool):
                if str(it.path) == path:
                    order.append(pool.pop(j))
                    break
        order.extend(pool)
        self.items = order
        self._renumber()

    def _on_order_changed(self):
        self._reorder_from_list()
        self._schedule()
        self._say("顺序已调整，预览已按新顺序重新生成。", "ok")

    def _selected_rows(self) -> list[int]:
        return sorted({self.lst.row(i) for i in self.lst.selectedItems()})

    def remove_selected(self):
        rows = self._selected_rows()
        if not rows:
            self._say("请先在列表里选中要移除的图片。", "warn")
            return
        for r in reversed(rows):
            if 0 <= r < len(self.items):
                self.items.pop(r)
        self._renumber()
        self.lst.setCurrentRow(min(rows[0], len(self.items) - 1))
        self._update_all()
        self._say(f"已移除 {len(rows)} 张，剩余 {len(self.items)} 张。", "info")

    def clear_all(self):
        if not self.items:
            return
        self.items.clear()
        self.lst.clear()
        self.global_rect = CropRect()
        self.scale = 1.0
        self._renumber()
        self._update_all()
        self._say("已清空图片列表。", "info")

    def _build_item_menu(self, row: int, rows: list[int] | None = None) -> QMenu:
        """素材区右键菜单；rows 为当前多选集合（含 row），移动/移除对其整体生效。"""
        sel = sorted(set(rows or [row]))
        n = len(self.items)
        menu = QMenu(self)
        multi = len(sel) > 1
        a_up = menu.addAction("上移")
        a_down = menu.addAction("下移")
        a_top = menu.addAction("移到最前")
        a_bottom = menu.addAction("移到最后")
        menu.addSeparator()
        a_del = menu.addAction(f"移除（{len(sel)} 张）" if multi else "移除")
        a_up.setEnabled(sel[0] > 0)
        a_down.setEnabled(sel[-1] < n - 1)
        a_top.setEnabled(sel[0] > 0)
        a_bottom.setEnabled(sel[-1] < n - 1)
        a_up.triggered.connect(lambda: self._shift_rows(sel, -1))
        a_down.triggered.connect(lambda: self._shift_rows(sel, +1))
        a_top.triggered.connect(lambda: self._move_rows_to_edge(sel, True))
        a_bottom.triggered.connect(lambda: self._move_rows_to_edge(sel, False))
        a_del.triggered.connect(lambda: self._remove_rows(sel))
        return menu

    def _on_list_menu(self, pos):
        item = self.lst.itemAt(pos)
        if item is None:
            return
        row = self.lst.row(item)
        sel = self._selected_rows()
        if row not in sel:
            # 右键落在选区之外：改为只选中这一张
            self.lst.setCurrentRow(row)
            sel = [row]
        menu = self._build_item_menu(row, sel)
        menu.exec(self.lst.viewport().mapToGlobal(pos))

    def _reselect(self, rows: list[int]):
        rows = [r for r in rows if 0 <= r < self.lst.count()]
        if not rows:
            return
        self._syncing = True
        self.lst.setCurrentRow(rows[0])
        for r in rows[1:]:
            it = self.lst.item(r)
            if it is not None:
                it.setSelected(True)
        self._syncing = False

    def _shift_rows(self, rows: list[int], delta: int):
        """整体上移/下移一格：与相邻未选中项逐个交换，选中块保持相对顺序。"""
        sel = set(r for r in rows if 0 <= r < len(self.items))
        if not sel:
            return
        order = sorted(sel) if delta < 0 else sorted(sel, reverse=True)
        moved = False
        for r in order:
            if r not in sel:
                continue
            nb = r + delta
            if not (0 <= nb < len(self.items)) or nb in sel:
                continue
            self.items[r], self.items[nb] = self.items[nb], self.items[r]
            sel.discard(r)
            sel.add(nb)
            moved = True
        if not moved:
            return
        self._renumber()
        self._update_all()
        self._reselect(sorted(sel))
        self._say(f"已{'上' if delta < 0 else '下'}移 {len(sel)} 张。", "info")

    def _move_rows_to_edge(self, rows: list[int], to_top: bool):
        sel = set(r for r in rows if 0 <= r < len(self.items))
        if not sel:
            return
        picked = [self.items[r] for r in sorted(sel)]
        rest = [it for i, it in enumerate(self.items) if i not in sel]
        self.items = picked + rest if to_top else rest + picked
        self._renumber()
        self._update_all()
        start = 0 if to_top else len(self.items) - len(picked)
        self._reselect(list(range(start, start + len(picked))))
        self._say(f"已移到{'最前' if to_top else '最后'}：{len(picked)} 张。", "info")

    def _move_row(self, row: int, target: int):
        if not (0 <= row < len(self.items)) or not (0 <= target < len(self.items)):
            return
        if row == target:
            return
        self.items.insert(target, self.items.pop(row))
        self._renumber()
        self.lst.setCurrentRow(target)
        self._update_all()
        self._say(f"已移动到第 {target + 1} 位。", "info")

    def _remove_rows(self, rows: list[int]):
        """右键「移除」：与左下角「移除」按钮同一套逻辑（支持多选）。"""
        rows = [r for r in rows if 0 <= r < len(self.items)]
        if not rows:
            return
        self._syncing = True
        self.lst.setCurrentRow(rows[0])
        for r in rows[1:]:
            it = self.lst.item(r)
            if it is not None:
                it.setSelected(True)
        self._syncing = False
        self.remove_selected()

    def _remove_row(self, row: int):
        self._remove_rows([row])

    def _sort(self, mode: str):
        if not self.items:
            return
        if mode == "name":
            # 自然排序：frame_2 排在 frame_10 之前
            self.items.sort(key=lambda it: natural_key(it.path.name))
        elif mode == "number":
            # 取文件名里最后一个数字；没有数字的排到最后
            self.items.sort(key=lambda it: (embedded_number(it.path) is None,
                                           embedded_number(it.path) or 0,
                                           natural_key(it.path.name)))
        elif mode == "reverse":
            self.items.reverse()
        self._renumber()
        self.lst.setCurrentRow(0)
        self._schedule()

    # ------------------------------------------------------------ 框选联动
    def _current_item(self) -> ImageItem | None:
        row = self.lst.currentRow()
        return self.items[row] if 0 <= row < len(self.items) else None

    def _effective_rect(self, it: ImageItem | None) -> CropRect:
        if it is None:
            return self.global_rect.copy()
        return (it.override or self.global_rect).copy()

    def _on_selection_changed(self, *_):
        if self._syncing:
            return
        it = self._current_item()
        r = self._effective_rect(it)
        # 切换图片时只反映状态，不能触发 toggled 把已存的独立框选覆盖掉
        self.chk_independent.blockSignals(True)
        self.chk_independent.setChecked(it is not None and it.override is not None)
        self.chk_independent.blockSignals(False)
        self._sync_rect_fields(r)
        self.canvas.set_content(it, r)
        self._refresh_rect_label()

    def _refresh_rect_label(self):
        it = self._current_item()
        if it is None:
            self.lbl_rect_from.setText("当前：未选中图片")
        elif it.override is not None:
            self.lbl_rect_from.setText(f"当前：{short(it.path.stem, 18)} 使用独立框选")
        else:
            self.lbl_rect_from.setText(f"当前：{short(it.path.stem, 18)} 使用统一框选")

    def _sync_rect_fields(self, r: CropRect):
        self._syncing = True
        self.sp_x.setValue(max(0, r.x))
        self.sp_y.setValue(max(0, r.y))
        self.sp_w.setValue(max(0, r.w))
        self.sp_h.setValue(max(0, r.h))
        self._syncing = False

    def _on_canvas_rect(self, r: CropRect):
        if self._syncing:
            return
        self._apply_rect(r, from_canvas=True)
        self._sync_rect_fields(r)
        self._schedule()

    def _apply_rect(self, r: CropRect, from_canvas: bool = False):
        it = self._current_item()
        if self.chk_independent.isChecked() and it is not None:
            it.override = r.copy() if r.valid() else CropRect()
        else:
            self.global_rect = r.copy()
        self._refresh_rect_label()
        if not from_canvas:
            self.canvas.set_rect(r)

    def _on_independent_toggled(self, checked: bool):
        if self._syncing:
            return
        it = self._current_item()
        if it is None:
            return
        if checked:
            it.override = self.global_rect.copy()
            self._say("已开启本图独立框选：之后的拖动只影响这张图。", "info")
        else:
            it.override = None
            r = self.global_rect.copy()
            self._sync_rect_fields(r)
            self.canvas.set_content(it, r)
            self._say("已恢复使用统一框选。", "info")
        self._refresh_rect_label()
        self._schedule()

    def _on_rect_field_changed(self, *_):
        if self._syncing:
            return
        r = CropRect(self.sp_x.value(), self.sp_y.value(),
                     self.sp_w.value(), self.sp_h.value())
        self._apply_rect(r)
        self._schedule()

    def apply_rect_to_all(self):
        if not self.items:
            self._say("请先导入图片。", "warn")
            return
        r = self._effective_rect(self._current_item())
        if not r.valid():
            self._say("当前框选为空，无法应用。请先在图片上拖出一个矩形。", "warn")
            return
        self.global_rect = r.copy()
        for it in self.items:
            it.override = None
        self.chk_independent.blockSignals(True)
        self.chk_independent.setChecked(False)
        self.chk_independent.blockSignals(False)
        self._refresh_rect_label()
        self._schedule()
        self._say(f"已把框选 {r.w}×{r.h} @({r.x},{r.y}) 应用到全部 {len(self.items)} 张图片。", "ok")

    def clear_current_override(self):
        it = self._current_item()
        if it is None:
            self._say("请先选中一张图片。", "warn")
            return
        it.override = None
        self.chk_independent.blockSignals(True)
        self.chk_independent.setChecked(False)
        self.chk_independent.blockSignals(False)
        self._sync_rect_fields(self.global_rect)
        self.canvas.set_content(it, self.global_rect.copy())
        self._refresh_rect_label()
        self._schedule()
        self._say("已清除本图独立框选，恢复统一框选。", "ok")

    # ------------------------------------------------------------ 渲染 / 导出
    def _on_param_changed(self, *_):
        if not self._syncing:
            self._schedule()

    def _on_direction_changed(self, *_):
        idx = self.cmb_align.currentIndex()
        vertical = self.cmb_dir.currentData() == "vertical"
        labels = ("居左", "居中", "居右") if vertical else ("顶部", "居中", "底部")
        self.cmb_align.blockSignals(True)
        self.cmb_align.clear()
        for label, key in zip(labels, ("start", "center", "end")):
            self.cmb_align.addItem(label, key)
        self.cmb_align.setCurrentIndex(max(0, idx))
        self.cmb_align.blockSignals(False)
        self._schedule()

    def _on_stack_mode_changed(self, *_):
        self.sp_hero.setEnabled(self.cmb_stack.currentData() != "text")
        self._schedule()

    def _on_ratio_text(self):
        if self._syncing:
            return
        try:
            w, h = parse_ratio(self.ed_ratio.text())
        except ValueError:
            self._say("比例格式应为 3:4 这样的写法，已还原为当前值。", "warn")
            self._sync_from_params()
            return
        self._syncing = True
        self.sp_rw.setValue(w)
        self.sp_rh.setValue(h)
        self._syncing = False
        self._schedule()

    def _on_ratio_spin(self, *_):
        if self._syncing or not hasattr(self, "sp_rh"):
            return
        self._syncing = True
        self.ed_ratio.setText(f"{self.sp_rw.value()}:{self.sp_rh.value()}")
        self._syncing = False
        self._schedule()

    def _on_ratio_preset(self, w: int, h: int):
        self.cmb_out.setCurrentIndex(max(0, self.cmb_out.findData("expand")))
        self._syncing = True
        self.sp_rw.setValue(w)
        self.sp_rh.setValue(h)
        self.ed_ratio.setText(f"{w}:{h}")
        self._syncing = False
        self._schedule()

    def auto_detect_rect(self):
        """自动定位当前图下半部分的字幕行（借鉴 MIT 项目的行亮度检测思路）。"""
        it = self._current_item()
        if it is None:
            self._say("请先选中一张图片。", "warn")
            return
        src = it.preview
        if src is None:
            self._say("这张图没有可用的预览图，无法自动定位。", "warn")
            return
        band = find_subtitle_band(src)
        if band is None:
            self._say("没能在当前图下半部分找到明显的字幕行，请手动框选。", "warn")
            return
        rect = band_to_rect(band, src.size, it.full_size)
        self._apply_rect(rect)
        self._sync_rect_fields(rect)
        self._schedule()
        self._say(f"已定位字幕条 {rect.w}×{rect.h} @({rect.x},{rect.y})，可继续手动微调。", "ok")

    def _schedule(self):
        self._debounce.start()

    def do_render(self, force: bool = False):
        if force:
            self._debounce.stop()
        self._collect_params()
        self.result_view.set_backdrop(bool(getattr(self.params, "transparent", False)))
        if not self.items:
            self.result_view.set_hint("⚠ 尚未导入图片：请点击左上角「导入图片」，"
                                      "或直接把图片文件拖进窗口。")
            self.warn_box.setVisible(False)
            self.lbl_size.setText("—")
            self._export_size = (0, 0)
            self._update_path_preview()
            return
        res = compose(self.items, self.global_rect, self.params,
                      full_res=False, scale=self.scale,
                      items_per_page=self.params.items_per_page)
        if not res.ok:
            self.result_view.set_hint("⚠ " + res.error)
            self._show_warnings(res.warnings)
            self.lbl_size.setText("—")
            self._export_size = (0, 0)
            self._update_path_preview()
            self._say(res.error, "warn")
            return

        pix = pil_to_qpixmap(res.image)
        self.spin_page.setMaximum(max(1, len(res.pages)))
        if self.spin_page.value() > len(res.pages):
            self.spin_page.setValue(max(1, len(res.pages)))
        idx = max(0, min(self.spin_page.value() - 1, len(res.pages) - 1))
        if len(res.pages) > 1:
            pix = pil_to_qpixmap(res.pages[idx])
        self.result_view.set_pixmap(pix)
        g = max(1e-6, self.scale)
        out_size = (round(res.image.width / g), round(res.image.height / g))
        self._export_size = out_size
        if self.params.output_mode == "expand":
            rw, rh = res.used_ratio or (self.params.ratio_w, self.params.ratio_h)
            size_text = (f"{len(self.items)} 句 · {rw}:{rh} · "
                         f"{canvas_fill(res.layout_size, res.image.size) * 100:.0f}% 补边")
            if res.ratio_note:
                size_text += f" · {res.ratio_note}"
        elif self.params.output_mode in ("ratio", "size") and res.layout_size[0]:
            content_size = (round(res.layout_size[0] / g), round(res.layout_size[1] / g))
            fill = canvas_fill(res.layout_size, res.image.size)
            size_text = (f"{content_size[0]}×{content_size[1]} → "
                         f"{out_size[0]}×{out_size[1]} · {fill * 100:.0f}% 补边")
        else:
            size_text = f"预览 {res.image.width}×{res.image.height}"
        if len(res.pages) > 1:
            size_text = f"第{idx + 1}/{len(res.pages)}页 · " + size_text
        self.lbl_size.setText(size_text)
        self._show_warnings(res.warnings)
        self._update_path_preview()
        if force:
            self._say(f"已重新生成：{len(self.items)} 条台词，间距 {self.params.gap}px。", "ok")

    def _show_warnings(self, warnings: list[str]):
        if warnings:
            lines = ["⚠ " + w for w in warnings]
            if any("边界" in w for w in warnings):
                # 越界警告要给出口：告诉用户去哪里修，而不是干看着
                lines.append("⚠ 提示：框选超出图片边界时，点左侧「重选」重新框选捕获区域，"
                             "或选中该张右键「移除」；也可以点「自动定位字幕条」重取。")
            self.warn_box.setPlainText("\n".join(lines))
            self.warn_box.setVisible(True)
        else:
            self.warn_box.clear()
            self.warn_box.setVisible(False)

    def _update_path_preview(self, *_):
        directory = Path(self.ed_dir.text()) if self.ed_dir.text().strip() else Path.cwd()
        fmt = self.cmb_fmt.currentData() or "PNG"
        size = self._export_size or (0, 0)
        per = self.params.items_per_page
        page_count = (-(-len(self.items) // per)) if (per > 0 and self.items) else 1
        paths = resolve_export_paths(directory, self.ed_name.text() or "台词拼图_{date}_{n}",
                                     fmt, self.items, size, page_count)
        name = paths[0].name
        if page_count > 1:
            name += f"  等 {page_count} 个文件"
        self.lbl_path.setText(name + "   （目录：" + str(directory) + "）")

    def open_output_folder(self):
        """在资源管理器打开当前导出目录。"""
        text = self.ed_dir.text().strip()
        path = Path(text) if text else Path.cwd()
        if not path.exists():
            self._say("导出目录还不存在：先导出一次，或点「浏览…」选一个目录。", "warn")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def export_image(self):
        self._collect_params()
        if not self.items:
            QMessageBox.warning(self, "无法导出", "尚未导入图片，请先导入台词截图。")
            return
        res = compose(self.items, self.global_rect, self.params,
                      full_res=True, scale=1.0,
                      items_per_page=self.params.items_per_page)
        if not res.ok:
            QMessageBox.warning(self, "无法导出", res.error)
            self._say(res.error, "warn")
            return
        directory = Path(self.ed_dir.text().strip()) if self.ed_dir.text().strip() else Path.cwd()
        fmt = self.cmb_fmt.currentData() or "PNG"
        template = self.ed_name.text() or "台词拼图_{date}_{n}"
        paths = resolve_export_paths(directory, template, fmt, self.items,
                                     res.image.size, len(res.pages))
        try:
            for path, page in zip(paths, res.pages):
                save_image(page, path, fmt, self.params, self.sp_quality.value())
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", f"写入文件失败：{exc}")
            self._say(f"导出失败：{exc}", "err")
            return
        self._save_settings()
        self._add_history({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "count": len(self.items),
            "output": str(paths[0]),
            "outputs": [str(p) for p in paths],
            "sources": [str(it.path) for it in self.items],
            "params": self._params_snapshot(),
            "rect": [self.global_rect.x, self.global_rect.y,
                     self.global_rect.w, self.global_rect.h],
            "thumb": self._save_thumb(res.pages[0]),
        })
        detail = (f"{res.image.width}×{res.image.height} · {len(self.items)} 条台词 · "
                  f"间距 {self.params.gap}px")
        if len(paths) > 1:
            detail += f" · 共 {len(paths)} 页"
            self._say(f"导出成功：{paths[0].name} 等 {len(paths)} 个文件（{detail}）", "ok")
            self.status.showMessage(f"已导出 {len(paths)} 个文件", 8000)
            QMessageBox.information(self, "导出完成",
                                    f"已保存 {len(paths)} 个文件：\n" +
                                    "\n".join(str(p) for p in paths) + f"\n\n{detail}")
        else:
            self._say(f"导出成功：{paths[0]}（{detail}）", "ok")
            self.status.showMessage(f"已导出 {paths[0]}", 8000)
            QMessageBox.information(self, "导出完成", f"已保存：\n{paths[0]}\n\n{detail}")

    # ------------------------------------------------------------ 历史记录
    def _load_history(self) -> list[dict]:
        raw = self.settings.value("history", "")
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except Exception:
            return []
        return data if isinstance(data, list) else []

    def _save_history(self, records: list[dict]):
        self.settings.setValue("history", json.dumps(records[:40], ensure_ascii=True))

    def _save_thumb(self, image: Image.Image) -> str:
        """把导出结果缩略存到本地缓存，供历史记录展示。"""
        try:
            base = Path(QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.AppDataLocation))
            folder = base / "thumbs"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{int(datetime.now().timestamp() * 1000)}.png"
            card_thumb(image.convert("RGB"), 400, 250).save(path, "PNG")
            return str(path)
        except Exception:
            return ""

    def _add_history(self, record: dict):
        records = self._load_history()
        records.insert(0, record)
        self._save_history(records)
        self.records = records
        self.start_page.set_records(records)

    def _params_snapshot(self) -> dict:
        p = self.params
        return {
            "direction": p.direction, "gap": p.gap, "margin": p.margin,
            "align": p.align, "uniform_width": p.uniform_width,
            "uniform_height": p.uniform_height, "background": list(p.background),
            "stack_mode": p.stack_mode, "hero_ratio": p.hero_ratio,
            "transparent": p.transparent, "output_mode": p.output_mode,
            "output_width": p.output_width, "output_height": p.output_height,
            "ratio_w": p.ratio_w, "ratio_h": p.ratio_h,
            "orientation": p.orientation,
            "subtitle_pos": p.subtitle_pos, "subtitle_offset": p.subtitle_offset,
            "items_per_page": p.items_per_page, "resample": p.resample,
            "padding": [p.padding.top, p.padding.bottom, p.padding.left, p.padding.right],
            "dir": self.ed_dir.text(), "name": self.ed_name.text(),
            "fmt": self.cmb_fmt.currentData(),
        }

    def _apply_params(self, snap: dict):
        if not snap:
            return
        self._syncing = True
        idx = self.cmb_dir.findData(snap.get("direction", "vertical"))
        if idx >= 0:
            self.cmb_dir.setCurrentIndex(idx)
        self.sp_gap.setValue(int(snap.get("gap", 0)))
        self.sp_margin.setValue(int(snap.get("margin", 0)))
        idx = self.cmb_align.findData(snap.get("align", "center"))
        if idx >= 0:
            self.cmb_align.setCurrentIndex(idx)
        self.chk_uw.setChecked(bool(snap.get("uniform_width", True)))
        self.chk_uh.setChecked(bool(snap.get("uniform_height", False)))
        idx = self.cmb_stack.findData(snap.get("stack_mode", "text"))
        if idx >= 0:
            self.cmb_stack.setCurrentIndex(idx)
        self.sp_hero.setValue(int(round(float(snap.get("hero_ratio", 1.0)) * 100)))
        bg = snap.get("background") or [0, 0, 0]
        self.params.background = tuple(int(v) for v in bg)
        self.chk_transparent.setChecked(bool(snap.get("transparent", False)))
        idx = self.cmb_out.findData(snap.get("output_mode", "content"))
        if idx >= 0:
            self.cmb_out.setCurrentIndex(idx)
        self.sp_ow.setValue(int(snap.get("output_width", 1080)))
        self.sp_oh.setValue(int(snap.get("output_height", 1920)))
        self.sp_rw.setValue(int(snap.get("ratio_w", 3)))
        self.sp_rh.setValue(int(snap.get("ratio_h", 4)))
        idx = self.cmb_orient.findData(snap.get("orientation", "auto"))
        if idx >= 0:
            self.cmb_orient.setCurrentIndex(idx)
        idx = self.cmb_subpos.findData(snap.get("subtitle_pos", "center"))
        if idx >= 0:
            self.cmb_subpos.setCurrentIndex(idx)
        self.sp_suboff.setValue(int(snap.get("subtitle_offset", 0)))
        self.sp_per_page.setValue(int(snap.get("items_per_page", 0)))
        idx = self.cmb_resample.findData(snap.get("resample", "LANCZOS"))
        if idx >= 0:
            self.cmb_resample.setCurrentIndex(idx)
        pad = snap.get("padding") or [0, 0, 0, 0]
        self.sp_pt.setValue(int(pad[0]))
        self.sp_pb.setValue(int(pad[1]))
        self.sp_pl.setValue(int(pad[2]))
        self.sp_pr.setValue(int(pad[3]))
        self.ed_dir.setText(str(snap.get("dir", "")))
        self.ed_name.setText(str(snap.get("name", "台词拼图_{date}_{n}")))
        idx = self.cmb_fmt.findData(snap.get("fmt", "PNG"))
        if idx >= 0:
            self.cmb_fmt.setCurrentIndex(idx)
        self._syncing = False
        self._update_swatch()
        self._collect_params()

    def open_record(self, record: dict):
        sources = [Path(p) for p in record.get("sources", [])]
        exist = [p for p in sources if p.exists()]
        if not exist:
            self._say("这条记录对应的原图已经找不到了，无法重新打开。", "warn")
            return
        self._apply_params(record.get("params", {}))
        self.add_images(exist, sort=False)
        rect = record.get("rect")
        if rect:
            self.global_rect = CropRect(*[int(v) for v in rect])
            self._sync_rect_fields(self.global_rect)
            it = self._current_item()
            if it is not None:
                self.canvas.set_content(it, self.global_rect.copy())
        self._schedule()
        self._say(f"已打开记录 {record.get('time', '')}（{len(exist)} 张）。", "ok")

    def import_folder(self):
        d = QFileDialog.getExistingDirectory(self, "选择包含台词截图的文件夹")
        if not d:
            return
        folder = Path(d)
        files = sorted(
            [p for p in folder.rglob("*")
             if p.is_file() and p.suffix.lower() in SUPPORTED_EXT],
            key=lambda p: natural_key(str(p.relative_to(folder))))
        if not files:
            self._say("这个文件夹里没有找到支持的图片文件。", "warn")
            return
        self.add_images(files, sort=False)

    # ------------------------------------------------------------ 自动捕获
    def _capture_busy(self) -> bool:
        return self._cap_thread is not None and self._cap_thread.is_alive()

    def select_capture_region(self) -> bool:
        """全屏框选视频区域；成功则记住并持久化，返回是否成功。"""
        if self._capture_busy() or self._cap_overlay_open:
            return False
        self._cap_overlay_open = True
        try:
            overlay = RegionSelectOverlay(self)
            if overlay.exec() != QDialog.DialogCode.Accepted or overlay._rect.isEmpty():
                return False
            region = QRect(overlay._rect)   # overlay 覆盖整屏，本地坐标即屏幕逻辑坐标
            dpr = (overlay.screen() or QApplication.primaryScreen()).devicePixelRatio()
            self._cap_bbox = _logical_to_physical(region, dpr)
            self._cap_region = QRect(region)
            self._save_capture_region()
            self._update_capture_ui()
            return True
        finally:
            self._cap_overlay_open = False

    def set_full_screen_region(self):
        """把捕获区域设为整块主屏。"""
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = QRect(screen.geometry())
        self._cap_region = geo
        self._cap_bbox = _logical_to_physical(geo, screen.devicePixelRatio())
        self._save_capture_region()
        self._update_capture_ui()

    def capture_full_screen(self):
        """只把捕获区域设成整屏 —— 不自动开始（开始必须点「● 开始捕获」）。"""
        if self._capture_busy():
            self._say("正在捕获中：先按快捷键或点悬浮条停止，再改区域。", "warn")
            return
        self.set_full_screen_region()
        self._say("已把捕获区域设为整屏。确认后点「● 开始捕获」开始。", "ok")

    def start_capture_flow(self):
        """「开始捕获」按钮：捕获中→停止；无区域先框选；然后按设置的秒数倒计时。"""
        if self._capture_busy():
            self._stop_capture()
            return
        if self._cap_bbox is None and not self.select_capture_region():
            return
        self._begin_countdown()

    def _begin_countdown(self, seconds: int | None = None):
        if self._cap_region is None:
            return
        secs = self.sp_countdown.value() if seconds is None else seconds
        if secs <= 0:
            self._start_capture()
            return
        if self._countdown is not None:
            self._countdown.close()
        cd = CountdownOverlay("请切到视频播放器…", secs, self._start_capture, self)
        cd.adjustSize()
        geo = cd.frameGeometry()
        geo.moveCenter(self._cap_region.center())
        cd.move(geo.topLeft())
        self._countdown = cd      # 必须持有引用：否则对象被回收，计时器永不触发
        fg = self._grab_foreground()
        cd.start()
        # 兜底：即使窗口管理器还是把焦点给了提示层，立刻还给原来的前台窗口（播放器）
        QTimer.singleShot(0, lambda: self._restore_foreground(fg))

    def _grab_foreground(self) -> int:
        try:
            return int(ctypes.windll.user32.GetForegroundWindow())
        except Exception:
            return 0

    def _restore_foreground(self, hwnd: int):
        """把前台焦点还给用户原来的窗口，保证播放器里 F/Enter/空格 仍然有效。"""
        if not hwnd:
            return
        try:
            if hwnd == int(self.winId()):
                return
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    def _save_capture_region(self):
        if self._cap_bbox and self._cap_region:
            self.settings.setValue("cap_bbox", list(self._cap_bbox))
            self.settings.setValue("cap_region",
                                   [self._cap_region.x(), self._cap_region.y(),
                                    self._cap_region.width(), self._cap_region.height()])

    def _load_capture_region(self):
        try:
            bbox = self.settings.value("cap_bbox")
            reg = self.settings.value("cap_region")
            if bbox and reg and len(bbox) == 4 and len(reg) == 4:
                self._cap_bbox = tuple(int(v) for v in bbox)
                self._cap_region = QRect(int(reg[0]), int(reg[1]),
                                         int(reg[2]), int(reg[3]))
        except (TypeError, ValueError):
            self._cap_bbox = None
            self._cap_region = None

    def _update_capture_ui(self):
        """按钮/区域行状态：待机、捕获中、快捷键被占用。文案随快捷键同步。"""
        hk = self._hotkey_text
        if self._capture_busy():
            self.btn_capture.setText(f"■ 停止捕获 · 已截 {self._cap_frames} 张（{hk}）")
        elif self._cap_bbox is None:
            self.btn_capture.setText("● 开始捕获 · 先框选区域")
        else:
            self.btn_capture.setText("● 开始捕获")
        region_txt = "区域：未选择" if self._cap_region is None else \
            f"区域：{self._cap_region.width()}×{self._cap_region.height()}（屏幕）"
        if not self._hotkey.ok:
            region_txt += f" · {hk} 被占用"
        self.lbl_cap_region.setText(region_txt)

    def _sync_sens_label(self):
        pct = round(self._sens_threshold() * 100)
        self.lbl_sens.setText(f"≈ 触发阈值 {pct}%（越高截得越多）")

    def _sens_threshold(self) -> float:
        """敏感度 1–10 → 触发阈值 10%–1%（线性映射，5 档 = 6%）。"""
        return (11 - self.sp_sens.value()) / 100.0

    def _collect_capture_cfg(self) -> CaptureConfig:
        return CaptureConfig(
            sample_interval_ms=self.sp_sample.value(),
            diff_threshold=self._sens_threshold(),
            min_capture_interval_ms=self.sp_mincap.value(),
            dedup_threshold=self.sp_dedup.value() / 100.0,
        )

    def _start_capture(self):
        self._countdown = None
        cfg = self._collect_capture_cfg()
        base = Path(QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppDataLocation))
        out_dir = base / "captures" / datetime.now().strftime("session_%Y%m%d_%H%M%S")
        self._cap_stop = threading.Event()
        self._cap_frames = 0
        # 悬浮状态条：置顶显示 + 排除出截屏，不会被录进结果
        self._cap_bar = CaptureBar(self._hotkey_text, self._stop_capture)
        self._cap_bar.set_warning(self._region_overlaps_window())
        geo = self._cap_bar.frameGeometry()
        geo.moveTopLeft(self._cap_region.topLeft() + QPoint(14, 14))
        self._cap_bar.move(geo.topLeft())
        fg = self._grab_foreground()
        self._cap_bar.show()
        QTimer.singleShot(0, lambda: self._restore_foreground(fg))
        self._cap_thread = threading.Thread(
            target=self._capture_worker, args=(cfg, out_dir), daemon=True,
            name="screen-capture")
        self._cap_thread.start()
        self._update_capture_ui()
        msg = ("捕获已开始：字幕每变一句就自动截一张（PNG 无损）。"
               f"按 {self._hotkey_text} 停止。")
        if self._region_overlaps_window():
            msg += " ⚠ 区域包含软件窗口，请切到视频播放器，否则界面会被截进去。"
        self._say(msg, "info")

    def _region_overlaps_window(self) -> bool:
        """捕获区域是否被主窗口挡住大半（挡住的话会把软件界面也录进去）。"""
        if self._cap_region is None:
            return False
        inter = self.frameGeometry().intersected(self._cap_region)
        if inter.isEmpty():
            return False
        area = max(1, self._cap_region.width() * self._cap_region.height())
        return inter.width() * inter.height() > 0.5 * area

    def _capture_worker(self, cfg: CaptureConfig, out_dir: Path):
        """后台线程：抓帧循环 + 去重。全程只通过信号碰 GUI。"""
        errors: list[str] = []

        def on_error(text: str):
            errors.append(text)

        captured = capture_loop(self._cap_bbox, out_dir, cfg, self._cap_stop,
                                on_frame=self._cap_signals.frame_saved.emit,
                                on_error=on_error)
        if self.chk_dedup.isChecked():
            self._cap_signals.phase.emit("正在去重，每句字幕只留最清晰一帧")
            kept: list[Path] = []
            try:
                kept, _dropped = finalize_session(out_dir, cfg)
            except Exception as exc:
                errors.append(f"去重失败：{exc}")
        else:
            kept = sorted(out_dir.glob("frame_*.png"))
        self._cap_signals.finished.emit(str(out_dir), kept, captured, len(kept))

    def _on_cap_frame(self, n: int):
        self._cap_frames = n
        if self._cap_bar is not None:
            self._cap_bar.set_count(n)
        if self._capture_busy():
            self.btn_capture.setText(f"■ 停止捕获 · 已截 {n} 张（{self._hotkey_text}）")

    def _on_cap_phase(self, text: str):
        if self._cap_bar is not None:
            self._cap_bar.set_phase(text)

    def _stop_capture(self):
        if self._cap_stop is not None:
            self._cap_stop.set()
        if self._cap_bar is not None:
            self._cap_bar.set_phase("正在去重")
        self.btn_capture.setText("正在去重…")

    def _on_capture_finished(self, dir_str: str, kept: list, captured: int, kept_n: int):
        self._cap_thread = None
        self._cap_stop = None
        self._cap_frames = 0
        if self._cap_bar is not None:
            self._cap_bar.close()
            self._cap_bar = None
        self._update_capture_ui()
        out_dir = Path(dir_str)
        if not kept:
            self._say(f"这次没截到可用画面（共 {captured} 帧）。"
                      f"原始帧保留在：{out_dir}", "warn")
            return
        self.add_images([Path(p) for p in kept], sort=False)
        if self.chk_dedup.isChecked():
            self._say(f"捕获完成：共截 {captured} 帧，去重后保留 {kept_n} 句并已按顺序导入；"
                      f"被剔除的帧保留在 {out_dir}，可手动找回。", "ok")
        else:
            self._say(f"捕获完成：共截 {captured} 帧（未去重），已按顺序导入。", "ok")

    def _on_hotkey(self):
        """快捷键只负责「结束捕获」；开始必须在软件里点按钮，避免误触。

        空闲时按快捷键只给一条温和提示，不做任何捕获动作。
        """
        if self._capture_busy():
            self._stop_capture()
            return
        self._say("捕获尚未开始：点「● 开始捕获」按钮开始（可先设置倒计时）。", "info")

    # ------------------------------------------------------------ 快捷键设置
    def _init_hotkey(self):
        seq = self.sp_hotkey.keySequence()
        custom = bool(self.settings.value("cap_hotkey", ""))
        # 目标键优先；被别的软件（或另一份本程序）占用时依次退到空闲键
        candidates = [seq]
        for alt in ("F8", "F9", "F10", "Ctrl+Alt+F8", "Shift+F8"):
            q = QKeySequence(alt)
            if q.toString() != seq.toString() and q not in candidates:
                candidates.append(q)
        ok = False
        chosen = seq
        for cand in candidates:
            res = sequence_to_hotkey(cand)
            if res is None:
                continue
            if self._hotkey.configure(*res):
                ok = True
                chosen = cand
                break
        if chosen.toString() != seq.toString():
            self.sp_hotkey.blockSignals(True)
            self.sp_hotkey.setKeySequence(chosen)
            self.sp_hotkey.blockSignals(False)
        self._hotkey_text = chosen.toString(QKeySequence.SequenceFormat.NativeText) or "F8"
        fallback_note = ""
        if not ok:
            fallback_note = " · 被占用，停止请点悬浮条"
        elif chosen.toString() != seq.toString():
            tip = "你设置的键被占用" if custom else "默认键被占用"
            fallback_note = f"（{tip}，已自动改用）"
        self.lbl_hotkey.setText(f"当前：{self._hotkey_text}（按它停止捕获）{fallback_note}")
        self._update_capture_ui()
        # 注意：启动期的回退提示不写状态栏，保持状态栏空闲隐藏
        # 首次运行时会先弹「选导出文件夹」，热键提示等它关掉再出现
        if (self.settings.value("first_run_done", False, type=bool)
                and not self.settings.value("hotkey_tip_shown", False, type=bool)):
            self.settings.setValue("hotkey_tip_shown", True)
            QTimer.singleShot(700, self._show_hotkey_tip)

    def _apply_hotkey(self):
        """用户改了快捷键：重新注册并同步所有提示文案。"""
        seq = self.sp_hotkey.keySequence()
        res = sequence_to_hotkey(seq)
        if res is None:
            self.sp_hotkey.setKeySequence(QKeySequence(self._hotkey_text))
            self._say("这个组合不支持：请用字母、数字或 F1–F12（可加 Ctrl/Alt/Shift）。", "warn")
            return
        if not self._hotkey.configure(*res):
            self.sp_hotkey.setKeySequence(QKeySequence(self._hotkey_text))
            self._update_capture_ui()
            self.lbl_hotkey.setText(f"当前：{self._hotkey_text}（按它停止捕获）· 新键被占用")
            self._say("这个快捷键已被其它软件占用，请换一个。", "warn")
            return
        self._hotkey_text = seq.toString(QKeySequence.SequenceFormat.NativeText) or "F8"
        self.settings.setValue("cap_hotkey", seq.toString())
        self.lbl_hotkey.setText(f"当前：{self._hotkey_text}（按它停止捕获）")
        self._update_capture_ui()
        self._say(f"捕获快捷键已改为 {self._hotkey_text}：捕获结束后按它停止。", "ok")

    def _build_faq_dialog(self) -> QDialog:
        """「遇到问题？」弹窗：常见问题 + 作者联系方式（版式参考 V2Live）。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("遇到问题？")
        dlg.resize(680, 620)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(10)
        browser = QTextBrowser()
        browser.setObjectName("faqBrowser")
        browser.setOpenExternalLinks(False)
        f = browser.font()
        f.setPointSize(10)
        browser.setFont(f)
        browser.document().setDefaultStyleSheet(
            "p { margin: 9px 0; line-height: 1.55; } h3 { margin: 2px 0 12px 0; }")
        browser.setHtml(FAQ_HTML)
        lay.addWidget(browser, 1)
        # 底部：三列等宽 —— 中列「知道了」居中，右列「投喂 Token」贴右下角（仿 V2Live）
        row = QGridLayout()
        row.setColumnStretch(0, 1)
        row.setColumnStretch(1, 1)
        row.setColumnStretch(2, 1)
        btn = QPushButton("知道了")
        btn.setObjectName("faqOk")
        btn.setFixedWidth(120)
        btn.setDefault(True)
        btn.clicked.connect(dlg.accept)
        row.addWidget(btn, 0, 1, alignment=Qt.AlignmentFlag.AlignCenter)
        feed = _link_button("投喂 Token (๑´ㅂ`๑)", "请作者喝杯咖啡～")
        feed.setObjectName("faqFeed")
        feed.clicked.connect(lambda: self._build_token_dialog().exec())
        row.addWidget(feed, 0, 2,
                      alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lay.addLayout(row)
        return dlg

    def _build_token_dialog(self) -> QDialog:
        """投喂 Token：微信/支付宝收款码（仿 V2Live 的可爱向弹窗）。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("投喂 Token")
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(12)

        msg = QLabel("如果这个小工具帮到了你，\n欢迎投喂作者一个 Token 续续命 (๑•̀ㅂ•́)و✧")
        msg.setObjectName("feedMsg")
        msg.setFont(_playful_font())
        msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg.setWordWrap(True)
        lay.addWidget(msg)

        rowd = QHBoxLayout()
        rowd.setSpacing(24)
        for img, name, kao in (("qr_wechat.png", "微信", "(๑´ㅂ`๑)"),
                               ("qr_alipay.png", "支付宝", "(≧▽≦)")):
            col = QVBoxLayout()
            col.setSpacing(8)
            pic = QLabel()
            pic.setObjectName(Path(img).stem)
            path = _asset_path(img)
            if path:
                pic.setPixmap(_rounded_thumb(path, 228))
            else:
                pic.setText("（收款码未随仓库分发）")
            pic.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col.addWidget(pic)
            lab = QLabel(f"{name} {kao}")
            lab.setFont(QFont(QApplication.font()))
            lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col.addWidget(lab)
            rowd.addLayout(col)
        lay.addLayout(rowd)

        ok = QPushButton("谢谢老板 (*^▽^*)")
        ok.setObjectName("feedOk")
        ok.setFixedWidth(180)
        ok.clicked.connect(dlg.accept)
        lay.addWidget(ok, 0, Qt.AlignmentFlag.AlignCenter)
        return dlg

    def show_faq(self):
        self._build_faq_dialog().exec()

    def _build_hotkey_tip(self) -> QMessageBox:
        box = QMessageBox(self)
        box.setWindowTitle("捕获快捷键")
        # 默认宽度会把说明挤成窄条，给文本区一个下限（与首次引导弹窗同一处理）
        box.setStyleSheet("QLabel#qt_msgbox_label, QLabel#qt_msgbox_informativelabel"
                          " { min-width: 400px; }")
        box.setIcon(QMessageBox.Icon.NoIcon)   # 单色界面里不要系统蓝图标
        box.setText(f"自动捕获快捷键：{self._hotkey_text}")
        box.setInformativeText(
            "点「● 开始捕获」开始；\n"
            "捕获开始后，在任何窗口按它即可停止。\n"
            "想换键：右侧「高级」→「自动捕获」→「捕获快捷键」。")
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        return box

    def _show_hotkey_tip(self):
        self._build_hotkey_tip().open()   # 非模态，不阻塞主界面

    def goto_start(self):
        self.items.clear()
        self.lst.clear()
        self.global_rect = CropRect()
        self._renumber()
        self.stack.setCurrentIndex(0)
        self._say("已新建拼图，当前批次已清空。", "info")

    def goto_workspace_empty(self):
        """新建拼图：直接进入空白工作区——自动捕获、拖入照片、导入图片都可用。"""
        self.items.clear()
        self.lst.clear()
        self.global_rect = CropRect()
        self.scale = 1.0
        self._renumber()
        self._update_all()
        self.stack.setCurrentIndex(1)
        self._say("工作区已就绪：点「● 开始捕获」录台词，或把照片拖进窗口、点「导入图片」。", "info")

    # ------------------------------------------------------------ 杂项
    def _say(self, text: str, kind: str = "info"):
        """状态提示统一走底部状态栏（顶部提示条已移除，界面上不多占一行）。"""
        prefix = {"warn": "注意：", "err": "错误："}.get(kind, "")
        color = {"ok": "#93c79b", "warn": "#e2b062",
                 "err": "#e28a8a"}.get(kind, "#d6d6d6")
        self.status.setStyleSheet(f"QStatusBar {{ color: {color}; }}")
        self.status.showMessage(prefix + text, 6000)

    def _on_page_changed(self, *_):
        """切页：新页面淡入。"""
        # 先清掉上一次可能残留的透明效果，避免快速切页时页面停在半透明
        if self._page_effect is not None:
            try:
                old = self._page_effect.parent()
                if old is not None:
                    old.setGraphicsEffect(None)
            except Exception:
                pass
            self._page_effect = None
        page = self.stack.currentWidget()
        if page is None:
            return
        try:
            # QGraphicsOpacityEffect 作用于含子控件的 QWidget 时，必须开透明背景，
            # 否则整页会被渲染成黑块
            page.setAttribute(Qt.WA_TranslucentBackground, True)
            effect = QGraphicsOpacityEffect(page)
            page.setGraphicsEffect(effect)
            self._page_effect = effect
            anim = QPropertyAnimation(effect, b"opacity", self)
            anim.setDuration(180)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.finished.connect(lambda p=page: self._drop_effect(p))
            self._page_anim = anim
            anim.start()
        except Exception:
            pass

    def _drop_effect(self, page):
        eff = self._page_effect
        if eff is not None and eff.parent() is page:
            self._page_effect = None
        try:
            page.setGraphicsEffect(None)
        except Exception:
            pass

    def _load_settings(self):
        s = self.settings
        d = s.value("export_dir", "")
        if d:
            self.ed_dir.setText(d)
            self._dir_touched = True
        else:
            self.ed_dir.setText(str(Path.cwd() / "output"))
        self.ed_name.setText(s.value("name_template", "台词拼图_{date}_{n}"))
        fmt = s.value("format", "PNG")
        idx = self.cmb_fmt.findData(fmt)
        if idx >= 0:
            self.cmb_fmt.setCurrentIndex(idx)
        gap = s.value("gap", None)
        if gap is not None:
            try:
                self.sp_gap.setValue(int(gap))
            except (TypeError, ValueError):
                pass
        for key, spin, default in (("cap_countdown_v3", self.sp_countdown, 8),
                                   ("cap_sample", self.sp_sample, 220),
                                   ("cap_sens_v2", self.sp_sens, 7),
                                   ("cap_mincap", self.sp_mincap, 700),
                                   ("cap_dedup_thr", self.sp_dedup, 8)):
            val = s.value(key, None)
            if val is not None:
                try:
                    spin.setValue(int(val))
                except (TypeError, ValueError):
                    spin.setValue(default)
        self.chk_dedup.setChecked(s.value("cap_dedup_on", True, type=bool))
        hk = s.value("cap_hotkey", "")
        if hk:
            self.sp_hotkey.setKeySequence(QKeySequence(hk))

    def _save_settings(self):
        s = self.settings
        s.setValue("export_dir", self.ed_dir.text())
        s.setValue("name_template", self.ed_name.text())
        s.setValue("format", self.cmb_fmt.currentData() or "PNG")
        s.setValue("gap", self.sp_gap.value())
        s.setValue("cap_countdown_v3", self.sp_countdown.value())
        s.remove("cap_countdown")      # 更早的旧键
        s.remove("cap_countdown_v2")   # 上一版键（曾被测试改存成 5 秒）作废
        s.setValue("cap_sample", self.sp_sample.value())
        s.setValue("cap_sens_v2", self.sp_sens.value())
        s.remove("cap_sens")   # 旧键（默认 5 档）作废
        s.setValue("cap_mincap", self.sp_mincap.value())
        s.setValue("cap_dedup_thr", self.sp_dedup.value())
        s.setValue("cap_dedup_on", self.chk_dedup.isChecked())
        s.setValue("cap_hotkey", self.sp_hotkey.keySequence().toString())

    def closeEvent(self, e):
        if self._cap_stop is not None:
            self._cap_stop.set()
        try:
            self._hotkey.stop()
        except Exception:
            pass
        self._save_settings()
        super().closeEvent(e)

    def _update_all(self):
        self._on_selection_changed()
        self._schedule()

    # 拖放导入
    def _urls_have_payload(self, mime) -> bool:
        if not mime.hasUrls():
            return False
        for u in mime.urls():
            p = Path(u.toLocalFile())
            if not str(p):
                continue
            if p.is_dir() or p.suffix.lower() in SUPPORTED_EXT:
                return True
        return False

    def _show_drag_hint(self):
        cw = self.centralWidget()
        if cw is not None and self._drag_overlay is not None:
            self._drag_overlay.refresh(cw)

    def _hide_drag_hint(self):
        if self._drag_overlay is not None:
            self._drag_overlay.hide()

    def dragEnterEvent(self, e):
        if self._urls_have_payload(e.mimeData()):
            e.acceptProposedAction()
            self._show_drag_hint()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if self._urls_have_payload(e.mimeData()):
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self._hide_drag_hint()
        super().dragLeaveEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        ov = getattr(self, "_drag_overlay", None)
        cw = self.centralWidget()
        if ov is not None and cw is not None:
            ov.setGeometry(cw.rect())

    def dropEvent(self, e):
        self._hide_drag_hint()
        paths = [Path(u.toLocalFile()) for u in e.mimeData().urls() if u.toLocalFile()]
        files: list[Path] = []
        for path in paths:
            if path.is_dir():
                files.extend(sorted(
                    (q for q in path.rglob("*")
                     if q.is_file() and q.suffix.lower() in SUPPORTED_EXT),
                    key=lambda q: natural_key(q.name)))
            elif path.is_file() and path.suffix.lower() in SUPPORTED_EXT:
                files.append(path)
        if files:
            self.add_images(files, sort=False)
            e.acceptProposedAction()
        else:
            self._say("拖入的内容里没有支持的图片文件。", "warn")


def apply_theme(app: QApplication):
    """固定深色主题：不跟随系统浅色/强调色，保证换台电脑看到的是同一套黑灰。

    Fusion 是唯一在三大平台上都听话的 QSS 友好风格；配色全部写死，
    选中态用中性深灰（不用系统强调色，避免有人是绿的、有人是紫的）。
    """
    app.setStyle("Fusion")
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: QColor(30, 30, 30),
        QPalette.ColorRole.WindowText: QColor(228, 228, 228),
        QPalette.ColorRole.Base: QColor(24, 24, 24),
        QPalette.ColorRole.AlternateBase: QColor(34, 34, 34),
        QPalette.ColorRole.ToolTipBase: QColor(42, 42, 42),
        QPalette.ColorRole.ToolTipText: QColor(235, 235, 235),
        QPalette.ColorRole.PlaceholderText: QColor(138, 138, 138),
        QPalette.ColorRole.Text: QColor(228, 228, 228),
        QPalette.ColorRole.Button: QColor(40, 40, 40),
        QPalette.ColorRole.ButtonText: QColor(228, 228, 228),
        QPalette.ColorRole.BrightText: QColor(255, 120, 120),
        QPalette.ColorRole.Link: QColor(206, 206, 206),
        QPalette.ColorRole.LinkVisited: QColor(168, 168, 168),
        QPalette.ColorRole.Highlight: QColor(58, 58, 58),
        QPalette.ColorRole.HighlightedText: QColor(255, 255, 255),
        QPalette.ColorRole.Light: QColor(62, 62, 62),
        QPalette.ColorRole.Midlight: QColor(50, 50, 50),
        QPalette.ColorRole.Mid: QColor(42, 42, 42),
        QPalette.ColorRole.Dark: QColor(16, 16, 16),
        QPalette.ColorRole.Shadow: QColor(0, 0, 0),
    }
    for role, color in roles.items():
        pal.setColor(role, color)
    app.setPalette(pal)
    # 提示气泡：圆角 + 深色 + 细边框，跟主界面同一套
    app.setStyleSheet(
        "QToolTip { background-color: #242424; color: #e8e8e8;"
        " border: 1px solid #4a4a4a; border-radius: 6px; padding: 5px 8px; }")


def _ensure_cjk_font(app: QApplication):
    """仅在 Qt 没拿到系统中文 UI 字体时兜底，正常 Windows 桌面不会改动任何设置。"""
    try:
        current = app.font().family()
        if any(k in current for k in ("YaHei", "SimHei", "SimSun", "CJK", "PingFang",
                                      "Heiti", "Microsoft JhengHei")):
            return
        if "Microsoft YaHei UI" in set(QFontDatabase.families()):
            app.setFont(QFont("Microsoft YaHei UI", 9))
            return
        fonts_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for filename in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf"):
            p = fonts_dir / filename
            if not p.exists():
                continue
            fid = QFontDatabase.addApplicationFont(str(p))
            if fid >= 0:
                names = QFontDatabase.applicationFontFamilies(fid)
                if names:
                    app.setFont(QFont(names[0], 9))
                    return
    except Exception:
        pass


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("SubtitlePuzzle")
    app.setQuitOnLastWindowClosed(True)
    _ensure_cjk_font(app)
    apply_theme(app)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
