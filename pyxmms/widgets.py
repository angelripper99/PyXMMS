"""Skinned buttons and sliders.

Copyright (C) 2026 the PyXMMS authors. Released under GPL-2.0-or-later. See LICENSE.
"""

from __future__ import annotations

from PyQt6.QtCore import QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFontMetrics, QPainter
from PyQt6.QtWidgets import QWidget

from .skin import LED, TEXT, ACCENT_DEEP, paint_button_face, paint_icon


class SkinButton(QWidget):
    """A drawn button with an icon or a short label."""
    clicked = pyqtSignal()

    def __init__(self, kind: str = "", text: str = "", parent=None):
        """Create a button with an icon name or a text label."""
        super().__init__(parent)
        self.kind = kind
        self.text = text
        self.lit = False
        self._pressed = False
        self._hovered = False
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_lit(self, on: bool):
        """Turn the highlighted state on or off."""
        on = bool(on)
        if on == self.lit:
            return
        self.lit = on
        self.update()

    def set_text(self, text: str):
        """Change the label and redraw."""
        if text == self.text:
            return
        self.text = text
        self.update()

    def enterEvent(self, event):
        """Redraw the button as hovered."""
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        """Clear hover and a stuck press."""
        self._hovered = False
        self._pressed = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        """Show the button as pressed."""
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        self._pressed = True
        self.update()

    def mouseReleaseEvent(self, event):
        """Emit a click when the press ends inside the button."""
        if self._pressed and event.button() == Qt.MouseButton.LeftButton:
            self._pressed = False
            inside = self.rect().contains(event.position().toPoint())
            self.update()
            if inside:
                self.clicked.emit()

    def paintEvent(self, _event):
        """Draw the button face and its icon or label."""
        painter = QPainter(self)
        rect = self.rect().adjusted(0, 0, -1, -1)
        paint_button_face(painter, rect, self._pressed, self.lit, self._hovered)
        if self.text:
            font = self.font()
            font.setBold(True)
            font.setPixelSize(max(8, int(self.height() * 0.5)))
            painter.setFont(font)
            painter.setPen(LED if self.lit else TEXT)
            painter.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), self.text)
        elif self.kind:
            color = LED if self.lit else QColor("#f0f0f0")
            inset = max(2, self.width() // 8)
            paint_icon(painter, self.kind, self.rect().adjusted(inset, inset, -inset, -inset), color)
        painter.end()

    def preferred_width(self, height: int, pad: int) -> int:
        """The width the label needs at a given height."""
        if not self.text:
            return height
        font = self.font()
        font.setBold(True)
        font.setPixelSize(max(8, int(height * 0.5)))
        return QFontMetrics(font).horizontalAdvance(self.text) + pad


class HSlider(QWidget):
    """Horizontal slider. value is 0..1 from left to right."""

    moved = pyqtSignal(float)
    released = pyqtSignal(float)

    def __init__(self, parent=None):
        """Create a horizontal slider at the left end."""
        super().__init__(parent)
        self._value = 0.0
        self._dragging = False
        self._skip_release = False
        self.default: float | None = None
        self.setMouseTracking(True)
        self.setMinimumHeight(10)

    def value(self) -> float:
        """The slider position from 0 to 1."""
        return self._value

    def set_value(self, value: float):
        """Move the slider when the user is not dragging it."""
        if self._dragging:
            return
        value = min(1.0, max(0.0, float(value)))
        if abs(value - self._value) < 0.0008:
            return
        self._value = value
        self.update()

    def _radius(self) -> int:
        """The knob radius for the current height."""
        return max(3, min(6, self.height() // 2 - 1))

    def _pad(self) -> int:
        """The space kept at each end so the knob stays inside."""
        return self._radius() + 2

    def _value_from_x(self, x: int) -> float:
        """Convert an x position to a value from 0 to 1."""
        pad = self._pad()
        usable = max(1, self.width() - 2 * pad)
        return min(1.0, max(0.0, (x - pad) / usable))

    def _knob_x(self) -> int:
        """The knob's x position for the current value."""
        pad = self._pad()
        usable = max(1, self.width() - 2 * pad)
        return int(pad + self._value * usable)

    def mousePressEvent(self, event):
        """Jump the knob to the click and start a drag."""
        if event.button() != Qt.MouseButton.LeftButton or not self.isEnabled():
            event.ignore()
            return
        self._dragging = True
        self._value = self._value_from_x(int(event.position().x()))
        self.moved.emit(self._value)
        self.update()
        self.grabMouse()

    def mouseMoveEvent(self, event):
        """Move the knob while the button is held."""
        if not self._dragging:
            return
        self._value = self._value_from_x(int(event.position().x()))
        self.moved.emit(self._value)
        self.update()

    def mouseReleaseEvent(self, event):
        """Finish the drag and emit the released signal."""
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._skip_release:
            self._skip_release = False
            self._dragging = False
            self.releaseMouse()
            return
        if self._dragging:
            self._value = self._value_from_x(int(event.position().x()))
            self._dragging = False
            self.releaseMouse()
            self.released.emit(self._value)
            self.update()

    def mouseDoubleClickEvent(self, event):
        """Return the slider to its default value."""
        if self.default is None or event.button() != Qt.MouseButton.LeftButton:
            return
        self._value = float(self.default)
        self._skip_release = True
        self._dragging = False
        if self.mouseGrabber() is self:
            self.releaseMouse()
        self.moved.emit(self._value)
        self.released.emit(self._value)
        self.update()

    def wheelEvent(self, event):
        """Nudge the value with the mouse wheel."""
        if not self.isEnabled():
            return
        step = 0.03 if event.angleDelta().y() > 0 else -0.03
        self._value = min(1.0, max(0.0, self._value + step))
        self.moved.emit(self._value)
        self.released.emit(self._value)
        self.update()

    def paintEvent(self, _event):
        """Draw the track and the knob."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        cy = self.height() / 2
        pad = self._pad()
        groove_h = 3 if self.height() < 16 else 4
        groove = QRect(pad, int(cy - groove_h / 2), max(1, self.width() - 2 * pad), groove_h)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#141414"))
        painter.drawRoundedRect(groove, 1, 1)
        filled = QRect(groove.left(), groove.top(), max(0, self._knob_x() - groove.left()), groove.height())
        painter.setBrush(ACCENT_DEEP if self.isEnabled() else QColor("#2a2a2a"))
        painter.drawRoundedRect(filled, 1, 1)
        radius = self._radius()
        center = QPoint(self._knob_x(), int(cy))
        painter.setBrush(QColor("#d8d8d8") if not self._dragging else QColor("#f4f4f4"))
        painter.setPen(QColor("#222222"))
        painter.drawEllipse(center, radius, radius)
        painter.setBrush(LED if self.isEnabled() else QColor("#555555"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(center, max(2, radius // 3), max(2, radius // 3))
        painter.end()


class VSlider(QWidget):
    """Vertical dB slider. Up is boost. Range is -12..+12."""

    changed = pyqtSignal(float)

    def __init__(self, parent=None):
        """Create a vertical decibel slider at zero."""
        super().__init__(parent)
        self._db = 0.0
        self._dragging = False
        self.setMouseTracking(True)
        self.setMinimumWidth(12)

    def db(self) -> float:
        """The current gain in decibels."""
        return self._db

    def set_db(self, value: float):
        """Move the slider when the user is not dragging it."""
        if self._dragging:
            return
        value = _clamp_half(value)
        if abs(value - self._db) < 0.01:
            return
        self._db = value
        self.update()

    def _track(self) -> QRect:
        """The rectangle the knob travels in."""
        top = 11 if self.height() < 90 else 14
        bottom = 3
        return QRect(0, top, self.width(), max(8, self.height() - top - bottom))

    def _y_for(self, db: float) -> int:
        """The y position for a gain. Up is louder."""
        track = self._track()
        # +12 at the top, -12 at the bottom.
        fraction = (12.0 - db) / 24.0
        return int(track.top() + fraction * track.height())

    def _db_from_y(self, y: int) -> float:
        """Convert a y position to a gain in decibels."""
        track = self._track()
        fraction = (y - track.top()) / max(1, track.height())
        fraction = min(1.0, max(0.0, fraction))
        return _clamp_half(12.0 - fraction * 24.0)

    def _emit(self, value: float):
        """Store a gain and emit it."""
        self._db = value
        self.changed.emit(value)
        self.update()

    def mousePressEvent(self, event):
        """Drag the knob, or reset it to zero on a right click."""
        if event.button() == Qt.MouseButton.RightButton:
            self._emit(0.0)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._dragging = True
        self._emit(self._db_from_y(int(event.position().y())))
        self.grabMouse()

    def mouseMoveEvent(self, event):
        """Move the knob while the button is held."""
        if not self._dragging:
            return
        self._emit(self._db_from_y(int(event.position().y())))

    def mouseReleaseEvent(self, event):
        """Finish the drag."""
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self.releaseMouse()
            self._emit(self._db_from_y(int(event.position().y())))

    def mouseDoubleClickEvent(self, event):
        """Reset the band to zero."""
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            if self.mouseGrabber() is self:
                self.releaseMouse()
            self._emit(0.0)

    def wheelEvent(self, event):
        """Nudge the gain by half a decibel."""
        step = 0.5 if event.angleDelta().y() > 0 else -0.5
        self._emit(_clamp_half(self._db + step))

    def paintEvent(self, _event):
        """Draw the scale, the track, and the knob."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        track = self._track()
        cx = self.width() / 2
        groove = QRect(int(cx - 1), track.top(), 3, track.height())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#141414"))
        painter.drawRoundedRect(groove, 2, 2)
        zero = self._y_for(0.0)
        knob = self._y_for(self._db)
        if abs(self._db) >= 0.25:
            top = min(zero, knob)
            painter.setBrush(LED if self._db > 0 else QColor("#c45a2a"))
            painter.drawRect(groove.left(), top, groove.width(), abs(zero - knob))
        painter.setPen(QColor("#3a3a3a"))
        painter.drawLine(int(cx - 5), zero, int(cx + 5), zero)
        if abs(self._db) >= 0.5:
            painter.setPen(LED if self._db > 0 else QColor("#ffb080"))
            font = self.font()
            font.setPixelSize(max(7, min(9, max(8, self.width() // 2))))
            font.setBold(True)
            painter.setFont(font)
            label = f"{self._db:+.0f}"
            painter.drawText(QRect(0, 0, self.width(), 11), int(Qt.AlignmentFlag.AlignCenter), label)
        radius = max(3, min(5, self.width() // 3))
        painter.setBrush(QColor("#e4e4e4"))
        painter.setPen(QColor("#222222"))
        painter.drawEllipse(QPoint(int(cx), knob), radius, radius)
        painter.end()


def _clamp_half(value: float) -> float:
    """Keep a gain inside the equalizer range, in half-decibel steps."""
    value = max(-12.0, min(12.0, float(value)))
    return round(value * 2.0) / 2.0


class MetalWindow(QWidget):
    """Frameless window with a draggable title strip."""

    def __init__(self, controller, title: str):
        """Create a frameless window with a title, shade, and close button."""
        super().__init__(None)
        self.controller = controller
        self.chrome_title = title
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self._drag_armed = False
        self._dragging = False
        self._press_global = QPoint()
        self.close_btn = SkinButton("close", parent=self)
        self.close_btn.setToolTip("Close")
        self.close_btn.clicked.connect(self._on_close)

    def _on_close(self):
        """Close this window."""
        self.close()

    def title_button_size(self) -> tuple[int, int, int]:
        """The size and position of one title-bar button."""
        metrics = self.controller.metrics
        size = max(9, metrics.title_h - 5 * metrics.s)
        y = max(1, (metrics.title_h - size) // 2)
        return size, y, max(3, 3 * metrics.s)

    def place_title_buttons(self, *buttons) -> int:
        """Park title buttons at the right edge. The first button is the rightmost."""
        size, y, margin = self.title_button_size()
        x = self.width() - margin
        step = max(2, self.controller.metrics.s)
        for button in buttons:
            x -= size
            button.setGeometry(x, y, size, size)
            x -= step
        return x

    def place_close_button(self) -> int:
        """Park the close button at the right of the title bar."""
        return self.place_title_buttons(self.close_btn)

    def mousePressEvent(self, event):
        """Start a title-bar drag, or open the menu on a right click."""
        if event.button() == Qt.MouseButton.RightButton:
            self.controller.popup_menu(event.globalPosition().toPoint())
            return
        if (
            event.button() == Qt.MouseButton.LeftButton
            and event.position().y() < self.controller.metrics.title_h
        ):
            self._drag_armed = True
            self._dragging = False
            self._press_global = event.globalPosition().toPoint()
            self.setFocus()

    def mouseMoveEvent(self, event):
        """Drag the window when a title-bar drag is active."""
        if not self._drag_armed:
            return
        pos = event.globalPosition().toPoint()
        if not self._dragging and (pos - self._press_global).manhattanLength() > 4:
            self._dragging = True
            shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self.controller.begin_drag(self, self._press_global, shift)
        if self._dragging:
            self.controller.drag_to(pos)

    def mouseReleaseEvent(self, event):
        """Finish a title-bar drag."""
        if self._dragging:
            self.controller.end_drag()
        self._drag_armed = False
        self._dragging = False

    def mouseDoubleClickEvent(self, event):
        """Roll the window when the title bar is double-clicked."""
        if event.position().y() < self.controller.metrics.title_h:
            self._drag_armed = False
            self._dragging = False
            self.on_title_double_click()

    def on_title_double_click(self):
        """Hook for a subclass to roll its own window."""
        return

    def dragEnterEvent(self, event):
        """Accept a drop of local files or http addresses."""
        if mime_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        """Keep accepting the drop while it moves over the window."""
        if mime_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        """Add the dropped files or addresses to the playlist."""
        paths = mime_paths(event.mimeData())
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self.controller.add_paths(paths)


def mime_paths(mime) -> list[str]:
    """Local paths and http addresses from a drag-and-drop event."""
    paths = []
    if mime is None or not mime.hasUrls():
        return paths
    for url in mime.urls():
        if url.isLocalFile():
            local = url.toLocalFile()
            if local:
                paths.append(local)
        else:
            scheme = url.scheme().lower()
            if scheme in ("http", "https"):
                paths.append(url.toString())
    return paths
