"""Drawn chrome for the player. Original artwork, classic proportions.

Copyright (C) 2026 the PyXMMS authors. Released under GPL-2.0-or-later. See LICENSE.
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRect, QRectF, Qt
from PyQt6.QtGui import (
    QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
)

UI_FONT = "DejaVu Sans"
MONO_FONT = "DejaVu Sans Mono"

LED = QColor("#C45CFF")
LED_DIM = QColor("#3A2158")
LED_BG = QColor("#0C0612")
AMBER = QColor("#FFB000")
TEXT = QColor("#E6E6E6")
TEXT_DIM = QColor("#9A9A9A")
WELL = QColor("#07040C")
ACCENT_DEEP = QColor("#5A2D86")
ACCENT_SOFT = QColor("#E4C2FF")


class Metrics:
    """Classic PyXMMS pixel grid. Scale 1 is 275×116, the original main window."""

    def __init__(self, scale: int):
        """Build the pixel sizes for scale 1, 2, or 3."""
        scale = int(scale)
        if scale < 1:
            scale = 1
        if scale > 3:
            scale = 3
        self.s = scale
        s = scale
        self.w = 275 * s
        self.title_h = 14 * s
        self.main_h = 116 * s
        self.eq_h = 116 * s
        self.bottom_h = 20 * s
        self.margin = 4 * s
        self.row_h = 15 * s
        self.font_px = 11 * s
        self.small_px = 9 * s
        self.display = QRect(self.margin, 16 * s, self.w - 2 * self.margin, 46 * s)
        self.seek = QRect(self.margin, 64 * s, self.w - 2 * self.margin, 10 * s)
        self.btn_y = 76 * s
        self.btn_h = 18 * s
        self.btn_w = 22 * s
        self.vol_y = 96 * s
        self.vol_h = 14 * s

    def font(self, px=None, mono=False, bold=False) -> QFont:
        """The interface font at a pixel size."""
        font = QFont(MONO_FONT if mono else UI_FONT)
        font.setPixelSize(int(px if px is not None else self.font_px))
        font.setBold(bool(bold))
        return font


def paint_window(painter: QPainter, rect: QRect, scale: int):
    """Fill a window with the metal gradient."""
    gradient = QLinearGradient(rect.left(), rect.top(), rect.left(), rect.bottom())
    gradient.setColorAt(0.0, QColor("#6a6a6a"))
    gradient.setColorAt(0.07, QColor("#3e3e3e"))
    gradient.setColorAt(0.5, QColor("#2b2b2b"))
    gradient.setColorAt(1.0, QColor("#454545"))
    painter.fillRect(rect, gradient)
    painter.setPen(QColor("#a0a0a0"))
    painter.drawLine(rect.topLeft(), rect.topRight())
    painter.drawLine(rect.topLeft(), rect.bottomLeft())
    painter.setPen(QColor("#101010"))
    painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())
    painter.drawLine(rect.right(), rect.top(), rect.right(), rect.bottom())
    inner_top = rect.top() + max(1, scale)
    painter.setPen(QColor("#1c1c1c"))
    painter.drawLine(rect.left() + 1, inner_top, rect.right() - 1, inner_top)


def paint_title_rule(painter: QPainter, width: int, title_h: int):
    """Draw the title-bar gradient."""
    gradient = QLinearGradient(0, 0, 0, max(1, title_h))
    gradient.setColorAt(0.0, QColor("#727272"))
    gradient.setColorAt(1.0, QColor("#3c3c3c"))
    painter.fillRect(QRect(1, 1, max(1, width - 2), max(1, title_h - 1)), gradient)
    painter.setPen(QColor("#101010"))
    painter.drawLine(0, title_h - 1, width, title_h - 1)
    painter.setPen(QColor("#6a6a6a"))
    painter.drawLine(0, title_h, width, title_h)


def paint_well(painter: QPainter, rect: QRect):
    """Draw a sunken rectangle for a display or a slider track."""
    painter.fillRect(rect, WELL)
    painter.setPen(QColor("#000000"))
    painter.drawLine(rect.topLeft(), rect.topRight())
    painter.drawLine(rect.topLeft(), rect.bottomLeft())
    painter.setPen(QColor("#3c3c3c"))
    bottom = rect.bottom()
    right = rect.right()
    painter.drawLine(rect.left(), bottom, right, bottom)
    painter.drawLine(right, rect.top(), right, bottom)
    inner = rect.adjusted(2, 2, -2, -2)
    painter.setPen(ACCENT_DEEP)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRect(inner)


def paint_button_face(painter: QPainter, rect: QRect, pressed: bool, lit: bool, hovered: bool):
    """Draw a button face for pressed, lit, or hovered."""
    if pressed:
        top, bottom = QColor("#2a2a2a"), QColor("#3a3a3a")
    elif hovered:
        top, bottom = QColor("#707070"), QColor("#424242")
    else:
        top, bottom = QColor("#5e5e5e"), QColor("#3a3a3a")
    gradient = QLinearGradient(rect.left(), rect.top(), rect.left(), rect.bottom())
    gradient.setColorAt(0, top)
    gradient.setColorAt(1, bottom)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(gradient)
    painter.drawRect(rect)
    highlight = QColor("#141414") if pressed else QColor("#c4c4c4")
    shadow = QColor("#5a5a5a") if pressed else QColor("#141414")
    painter.setPen(highlight)
    painter.drawLine(rect.topLeft(), rect.topRight())
    painter.drawLine(rect.topLeft(), rect.bottomLeft())
    painter.setPen(shadow)
    painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())
    painter.drawLine(rect.right(), rect.top(), rect.right(), rect.bottom())
    if lit:
        inset = 1 if rect.width() < 28 else 2
        inner = rect.adjusted(inset, inset, -inset, -inset)
        painter.setPen(LED)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(inner)


def _triangle(cx, cy, radius, direction: str) -> QPainterPath:
    """A play, previous, or next triangle."""
    path = QPainterPath()
    if direction == "left":
        path.moveTo(cx + radius, cy - radius)
        path.lineTo(cx - radius * 0.85, cy)
        path.lineTo(cx + radius, cy + radius)
    else:
        path.moveTo(cx - radius, cy - radius)
        path.lineTo(cx + radius * 0.85, cy)
        path.lineTo(cx - radius, cy + radius)
    path.closeSubpath()
    return path


def paint_icon(painter: QPainter, kind: str, rect: QRect, color: QColor):
    """Draw one transport or window icon."""
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    cx = rect.center().x()
    cy = rect.center().y()
    side = min(rect.width(), rect.height())
    if kind == "play":
        painter.fillPath(_triangle(cx + side * 0.04, cy, side * 0.26, "right"), color)
    elif kind == "pause":
        width = max(2, int(side * 0.14))
        height = int(side * 0.48)
        gap = max(2, int(side * 0.10))
        top = int(cy - height / 2)
        painter.fillRect(int(cx - gap - width), top, width, height, color)
        painter.fillRect(int(cx + gap), top, width, height, color)
    elif kind == "stop":
        size = max(4, int(side * 0.40))
        painter.fillRect(int(cx - size / 2), int(cy - size / 2), size, size, color)
    elif kind in ("prev", "next"):
        direction = "left" if kind == "prev" else "right"
        radius = side * 0.16
        shift = side * 0.16
        if kind == "prev":
            painter.fillRect(int(cx - side * 0.34), int(cy - side * 0.22), max(2, int(side * 0.08)), int(side * 0.44), color)
            painter.fillPath(_triangle(cx - shift * 0.15, cy, radius, direction), color)
            painter.fillPath(_triangle(cx + shift, cy, radius, direction), color)
        else:
            painter.fillPath(_triangle(cx - shift, cy, radius, direction), color)
            painter.fillPath(_triangle(cx + shift * 0.15, cy, radius, direction), color)
            painter.fillRect(int(cx + side * 0.26), int(cy - side * 0.22), max(2, int(side * 0.08)), int(side * 0.44), color)
    elif kind == "eject":
        path = QPainterPath()
        path.moveTo(cx, cy - side * 0.28)
        path.lineTo(cx + side * 0.24, cy + side * 0.02)
        path.lineTo(cx - side * 0.24, cy + side * 0.02)
        path.closeSubpath()
        painter.fillPath(path, color)
        painter.fillRect(
            int(cx - side * 0.24),
            int(cy + side * 0.12),
            int(side * 0.48),
            max(2, int(side * 0.08)),
            color,
        )
    elif kind == "close":
        pen = QPen(color, max(2, side * 0.12))
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        painter.setPen(pen)
        inset = side * 0.28
        painter.drawLine(QPointF(cx - inset, cy - inset), QPointF(cx + inset, cy + inset))
        painter.drawLine(QPointF(cx + inset, cy - inset), QPointF(cx - inset, cy + inset))
    elif kind == "min":
        pen = QPen(color, max(2, side * 0.12))
        painter.setPen(pen)
        y = cy + side * 0.16
        painter.drawLine(QPointF(cx - side * 0.24, y), QPointF(cx + side * 0.24, y))
    elif kind == "shade":
        pen = QPen(color, max(1, side * 0.08))
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        box = QRectF(cx - side * 0.22, cy - side * 0.18, side * 0.44, side * 0.36)
        painter.drawRect(box)
        painter.drawLine(QPointF(box.left(), box.top() + side * 0.12), QPointF(box.right(), box.top() + side * 0.12))
    elif kind == "menu":
        pen = QPen(color, max(2, side * 0.10))
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        painter.setPen(pen)
        for offset in (-0.16, 0.0, 0.16):
            y = cy + side * offset
            painter.drawLine(QPointF(cx - side * 0.24, y), QPointF(cx + side * 0.24, y))
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)


_SEGMENTS = {
    "0": "abcdef",
    "1": "bc",
    "2": "abdeg",
    "3": "abcdg",
    "4": "bcfg",
    "5": "acdfg",
    "6": "acdefg",
    "7": "abc",
    "8": "abcdefg",
    "9": "abcdfg",
    "-": "g",
}


def _segment_rects(x, y, w, h, thickness):
    """The seven rectangles of one LED digit."""
    t = thickness
    half = h / 2
    return {
        "a": QRectF(x + t, y, w - 2 * t, t),
        "b": QRectF(x + w - t, y + t, t, half - t),
        "c": QRectF(x + w - t, y + half, t, half - t),
        "d": QRectF(x + t, y + h - t, w - 2 * t, t),
        "e": QRectF(x, y + half, t, half - t),
        "f": QRectF(x, y + t, t, half - t),
        "g": QRectF(x + t, y + half - t / 2, w - 2 * t, t),
    }


def draw_led_text(painter: QPainter, rect: QRect, text: str, color: QColor = LED):
    """Seven-segment text fitted inside rect. Supports digits, colon, and minus."""
    if rect.width() < 4 or rect.height() < 4 or not text:
        return
    height = rect.height()
    digit_w = height * 0.62
    colon_w = digit_w * 0.42
    gap = max(1.0, height * 0.08)
    width = 0.0
    for char in text:
        width += colon_w if char == ":" else digit_w
        width += gap
    width -= gap
    if width > rect.width() and width > 0:
        scale = rect.width() / width
        height *= scale
        digit_w *= scale
        colon_w *= scale
        gap *= scale
        width = rect.width()
    thickness = max(1.0, digit_w * 0.18)
    x = float(rect.left())
    y = rect.top() + (rect.height() - height) / 2
    painter.setPen(Qt.PenStyle.NoPen)
    for char in text:
        if char == ":":
            size = max(1.0, thickness)
            cx = x + colon_w / 2
            painter.setBrush(color)
            painter.drawRect(QRectF(cx - size / 2, y + height * 0.28, size, size))
            painter.drawRect(QRectF(cx - size / 2, y + height * 0.62, size, size))
            x += colon_w + gap
            continue
        pieces = _segment_rects(x, y, digit_w, height, thickness)
        on = _SEGMENTS.get(char, "")
        for name, piece in pieces.items():
            painter.setBrush(color if name in on else LED_DIM)
            painter.drawRect(piece)
        x += digit_w + gap


def make_icon_pixmap(size: int = 128) -> QPixmap:
    """Draw the application icon."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    bounds = QRectF(1, 1, size - 2, size - 2)
    gradient = QLinearGradient(0, 0, 0, size)
    gradient.setColorAt(0, QColor("#6a6a6a"))
    gradient.setColorAt(1, QColor("#1c1c1c"))
    painter.setBrush(gradient)
    painter.setPen(QPen(QColor("#0e0e0e"), max(1, size * 0.03)))
    painter.drawRoundedRect(bounds, size * 0.18, size * 0.18)
    well = QRectF(size * 0.14, size * 0.18, size * 0.72, size * 0.64)
    painter.setBrush(LED_BG)
    painter.setPen(QPen(ACCENT_DEEP, max(1, size * 0.015)))
    painter.drawRoundedRect(well, size * 0.06, size * 0.06)
    heights = (0.38, 0.62, 0.90, 0.55, 0.74)
    count = len(heights)
    gap = well.width() * 0.08
    bar_w = (well.width() - gap * (count + 1)) / count
    x = well.left() + gap
    base = well.bottom() - well.height() * 0.16
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(LED)
    for frac in heights:
        bar_h = well.height() * 0.62 * frac
        painter.drawRoundedRect(QRectF(x, base - bar_h, bar_w, bar_h), 1.5, 1.5)
        x += bar_w + gap
    painter.end()
    return pixmap
