"""Main player window: transport, seek, volume, and the phosphor display.

Copyright (C) 2026 the PyXMMS authors. Released under GPL-2.0-or-later. See LICENSE.
"""

from __future__ import annotations

import math
import time

from PyQt6.QtCore import QPointF, QRect, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFontMetrics, QPainter

from . import APP_NAME
from .playlist import format_time
from .skin import AMBER, LED, LED_DIM, TEXT, TEXT_DIM, draw_led_text, paint_title_rule, paint_well, paint_window
from .widgets import HSlider, MetalWindow, SkinButton


class MainWindow(MetalWindow):
    """Transport, seek bar, volume, balance, and the time display."""
    def __init__(self, controller):
        """Build the transport buttons, sliders, and the menu button."""
        super().__init__(controller, APP_NAME)
        self.setWindowTitle(APP_NAME)
        self._scroll = 0
        self._banner_text = ""
        self._vis_t = 0.0
        self._press_local = None
        self._click = False
        self._title_left = 0
        self._title_right = 0
        self._vol_label = QRect()
        self._bal_label = QRect()

        self.menu_btn = self._icon_button("menu", "Menu")
        self.min_btn = self._icon_button("min", "Minimize all windows")
        self.shade_btn = self._icon_button("shade", "Shade")
        self.menu_btn.clicked.connect(self._open_menu)
        self.min_btn.clicked.connect(self.showMinimized)
        self.shade_btn.clicked.connect(self.controller.toggle_shade)

        self.prev_btn = self._icon_button("prev", "Previous (Z)")
        self.play_btn = self._icon_button("play", "Play (X)")
        self.pause_btn = self._icon_button("pause", "Pause (C)")
        self.stop_btn = self._icon_button("stop", "Stop (V)")
        self.next_btn = self._icon_button("next", "Next (B)")
        self.eject_btn = self._icon_button("eject", "Add files (Ctrl+O)")
        self.prev_btn.clicked.connect(self.controller.prev_track)
        self.play_btn.clicked.connect(self.controller.play)
        self.pause_btn.clicked.connect(self.controller.pause)
        self.stop_btn.clicked.connect(self.controller.stop)
        self.next_btn.clicked.connect(self.controller.next_track)
        self.eject_btn.clicked.connect(self.controller.open_files)

        self.shuffle_btn = self._text_button("SHUF", "Shuffle (S)")
        self.repeat_btn = self._text_button("REP", "Repeat (R)")
        self.eq_btn = self._text_button("EQ", "Equalizer (E)")
        self.pl_btn = self._text_button("PL", "Playlist (L)")
        self.shuffle_btn.clicked.connect(self.controller.toggle_shuffle)
        self.repeat_btn.clicked.connect(self.controller.cycle_repeat)
        self.eq_btn.clicked.connect(self.controller.toggle_eq_window)
        self.pl_btn.clicked.connect(self.controller.toggle_playlist_window)

        self._mini = []
        for kind, tip, slot in (
            ("prev", "Previous", self.controller.prev_track),
            ("play", "Play", self.controller.play),
            ("pause", "Pause", self.controller.pause),
            ("stop", "Stop", self.controller.stop),
            ("next", "Next", self.controller.next_track),
        ):
            button = self._icon_button(kind, tip)
            button.clicked.connect(slot)
            button.hide()
            self._mini.append(button)
        self.mini_play = self._mini[1]
        self.mini_pause = self._mini[2]

        self.seek = HSlider(self)
        self.seek.setToolTip("Seek")
        self.seek.released.connect(self.controller.seek_fraction)
        self.volume = HSlider(self)
        self.volume.default = 0.8
        self.volume.setToolTip("Volume")
        self.volume.released.connect(self._volume_released)
        self.volume.moved.connect(self._volume_moved)
        self.balance = HSlider(self)
        self.balance.default = 0.5
        self.balance.setToolTip("Balance. Double-click to center.")
        self.balance.released.connect(self._balance_released)

        self._transport = [
            self.prev_btn, self.play_btn, self.pause_btn, self.stop_btn,
            self.next_btn, self.eject_btn, self.shuffle_btn, self.repeat_btn,
            self.eq_btn, self.pl_btn, self.seek, self.volume, self.balance,
        ]

        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self.apply_metrics()

    def _icon_button(self, kind: str, tip: str) -> SkinButton:
        """Make a transport button that draws an icon."""
        button = SkinButton(kind, parent=self)
        button.setToolTip(tip)
        return button

    def _text_button(self, text: str, tip: str) -> SkinButton:
        """Make a small text button such as VOL or BAL."""
        button = SkinButton(text=text, parent=self)
        button.setToolTip(tip)
        return button

    def _text_width(self, button, height: int, sample: str, scale: int) -> int:
        """Width a text button needs at the current scale."""
        font = button.font()
        font.setBold(True)
        font.setPixelSize(max(8, int(height * 0.5)))
        return QFontMetrics(font).horizontalAdvance(sample) + 6 * scale

    def _open_menu(self):
        """Open the main menu under the menu button."""
        self.controller.popup_menu(self.menu_btn.mapToGlobal(self.menu_btn.rect().bottomLeft()))

    def _volume_moved(self, value: float):
        """Update the volume while the slider is dragged."""
        self.controller.set_volume(int(round(value * 100)), from_slider=True)

    def _volume_released(self, value: float):
        """Store the volume when the slider is released."""
        self.controller.set_volume(int(round(value * 100)), from_slider=True)

    def _balance_released(self, value: float):
        """Store the balance when the slider is released."""
        self.controller.set_balance(value * 2.0 - 1.0, from_slider=True)

    def apply_metrics(self):
        """Resize the main window for the current scale."""
        metrics = self.controller.metrics
        height = metrics.title_h if self.controller.shaded else metrics.main_h
        self.setFixedSize(metrics.w, height)
        self._layout()
        self.sync()

    def apply_shade(self):
        """Roll the main window to the saved shade state."""
        self.apply_metrics()

    def _layout(self):
        """Place the buttons and sliders for the open window."""
        metrics = self.controller.metrics
        shaded = self.controller.shaded
        for widget in self._transport:
            widget.setVisible(not shaded)
        self._layout_title()
        if shaded:
            return
        scale = metrics.s
        y = metrics.btn_y
        height = metrics.btn_h
        x = metrics.margin
        gap = max(1, scale)
        width = metrics.btn_w
        for button in (
            self.prev_btn, self.play_btn, self.pause_btn, self.stop_btn,
            self.next_btn, self.eject_btn,
        ):
            button.setGeometry(x, y, width, height)
            x += width + gap
        x += gap
        for button, sample in ((self.shuffle_btn, "SHUF"), (self.repeat_btn, "ALL")):
            text_w = self._text_width(button, height, sample, scale)
            button.setGeometry(x, y, text_w, height)
            x += text_w + gap
        right = metrics.w - metrics.margin
        for button in (self.pl_btn, self.eq_btn):
            text_w = button.preferred_width(height, 6 * scale)
            right -= text_w
            button.setGeometry(right, y, text_w, height)
            right -= gap

        self.seek.setGeometry(metrics.seek)
        label_font = metrics.font(metrics.small_px, bold=True)
        label_w = QFontMetrics(label_font).horizontalAdvance("VOL") + 6 * scale
        vy = metrics.vol_y
        vh = metrics.vol_h
        self._vol_label = QRect(metrics.margin, vy, label_w, vh)
        gap_mid = 8 * scale
        remain = metrics.w - 2 * metrics.margin - 2 * label_w - gap_mid - 8
        slider_w = max(20, remain // 2)
        self.volume.setGeometry(self._vol_label.right() + 4, vy, slider_w, vh)
        self._bal_label = QRect(self.volume.geometry().right() + gap_mid, vy, label_w, vh)
        bal_x = self._bal_label.right() + 4
        self.balance.setGeometry(bal_x, vy, metrics.w - metrics.margin - bal_x, vh)

    def _layout_title(self):
        """Place the shade and close buttons on the title bar."""
        size, y, margin = self.title_button_size()
        x = self.width() - margin
        step = max(2, self.controller.metrics.s)
        for button in (self.close_btn, self.min_btn, self.shade_btn):
            x -= size
            button.setGeometry(x, y, size, size)
            x -= step
        self.menu_btn.setGeometry(margin, y, size, size)
        if self.controller.shaded:
            for button in reversed(self._mini):
                x -= size
                button.setGeometry(x, y, size, size)
                button.show()
                x -= step
        else:
            for button in self._mini:
                button.hide()
        self._title_left = margin + size + 6 * max(1, self.controller.metrics.s // 2)
        self._title_right = x - 4

    def _zones(self):
        """Rectangles for the title, the display, and the time readout."""
        metrics = self.controller.metrics
        pad = max(2, 2 * metrics.s)
        inner = metrics.display.adjusted(pad, pad, -pad, -pad)
        info_w = max(54, 42 * metrics.s)
        time_w = max(64, 48 * metrics.s)
        time_rect = QRect(inner.left(), inner.top(), min(time_w, inner.width() // 3), inner.height())
        info_rect = QRect(inner.right() - info_w + 1, inner.top(), info_w, inner.height())
        mid_left = time_rect.right() + pad
        mid_width = max(8, info_rect.left() - mid_left - 4)
        title_h = max(12, int(inner.height() * 0.38))
        title_rect = QRect(mid_left, inner.top(), mid_width, title_h)
        vis_rect = QRect(
            mid_left,
            title_rect.bottom() + 2,
            mid_width,
            max(4, inner.bottom() - title_rect.bottom() - 2),
        )
        return time_rect, title_rect, vis_rect, info_rect

    def _banner(self) -> tuple[str, bool]:
        """The scrolling line and whether it should scroll."""
        controller = self.controller
        if controller.notice and time.monotonic() < controller.notice_until:
            return controller.notice, True
        if controller.transport != "stopped" and controller.title:
            return controller.title, False
        track = controller.playlist.current_track
        if track is not None:
            return track.name, False
        return "Drop music here   ·   Ctrl+O adds files", False

    def _led(self) -> tuple[str, QColor]:
        """The time readout text and its color."""
        controller = self.controller
        duration = controller.duration
        if self.seek._dragging and duration > 0:
            position = self.seek.value() * duration
        else:
            position = controller.position
        remaining = controller.remaining and duration > 0 and (
            controller.transport != "stopped" or self.seek._dragging
        )
        if remaining:
            position = max(0.0, duration - position)
        if controller.transport == "stopped" and not self.seek._dragging:
            position = 0
        return format_time(position), AMBER if remaining else LED

    def _tick(self):
        """Advance the scrolling title."""
        if self.controller.transport == "playing":
            self._vis_t += 0.12
        self._scroll += max(1, self.controller.metrics.s)
        self.update()

    def sync(self):
        """Match the buttons and sliders to playback, then redraw."""
        controller = self.controller
        playing = controller.transport == "playing"
        paused = controller.transport == "paused"
        self.play_btn.set_lit(playing)
        self.pause_btn.set_lit(paused)
        self.mini_play.set_lit(playing)
        self.mini_pause.set_lit(paused)
        self.shuffle_btn.set_lit(controller.playlist.shuffle)
        repeat = controller.playlist.repeat
        self.repeat_btn.set_text({"off": "REP", "all": "ALL", "one": "1"}.get(repeat, "REP"))
        self.repeat_btn.set_lit(repeat != "off")
        self.eq_btn.set_lit(controller.show_eq)
        self.pl_btn.set_lit(controller.show_playlist)
        duration = controller.duration
        self.seek.setEnabled(duration > 0 and controller.transport != "stopped")
        if duration > 0 and not self.seek._dragging:
            self.seek.set_value(min(1.0, max(0.0, controller.position / duration)))
        elif controller.transport == "stopped" and not self.seek._dragging:
            self.seek.set_value(0.0)
        if not self.volume._dragging:
            self.volume.set_value(controller.volume / 100.0)
        if not self.balance._dragging:
            self.balance.set_value((controller.balance + 1.0) / 2.0)
        if controller.transport != "stopped" and controller.title:
            self.setWindowTitle(f"{controller.title} — {APP_NAME}")
        else:
            self.setWindowTitle(APP_NAME)
        self.update()

    def paintEvent(self, _event):
        """Draw the main window chrome, display, and analyzer."""
        painter = QPainter(self)
        metrics = self.controller.metrics
        paint_window(painter, self.rect(), metrics.s)
        self._paint_title(painter)
        if self.controller.shaded:
            painter.end()
            return
        paint_well(painter, metrics.display)
        time_rect, title_rect, vis_rect, info_rect = self._zones()
        text, color = self._led()
        draw_led_text(painter, time_rect, text, color)
        banner, amber = self._banner()
        if banner != self._banner_text:
            self._banner_text = banner
            self._scroll = 0
        font = metrics.font(metrics.small_px, mono=True, bold=True)
        self._paint_scroll(painter, title_rect, banner, AMBER if amber else LED, font)
        self._paint_vis(painter, vis_rect)
        self._paint_info(painter, info_rect)
        self._paint_slider_labels(painter)
        painter.end()

    def _paint_title(self, painter: QPainter):
        """Draw the title bar text."""
        metrics = self.controller.metrics
        paint_title_rule(painter, self.width(), metrics.title_h)
        font = metrics.font(metrics.small_px + 1, bold=True)
        painter.setFont(font)
        fm = QFontMetrics(font)
        baseline = (metrics.title_h + fm.ascent() - fm.descent()) // 2
        painter.setPen(LED)
        painter.drawText(self._title_left, baseline, APP_NAME)
        if not self.controller.shaded:
            return
        brand = fm.horizontalAdvance(APP_NAME)
        left = self._title_left + brand + 8
        rect = QRect(left, 1, max(10, self._title_right - left), metrics.title_h - 2)
        banner, amber = self._banner()
        self._paint_scroll(
            painter, rect, banner, AMBER if amber else TEXT,
            metrics.font(metrics.small_px, mono=True),
        )

    def _paint_scroll(self, painter, rect: QRect, text: str, color: QColor, font):
        """Draw one scrolling line inside a rectangle."""
        if rect.width() < 8:
            return
        painter.save()
        painter.setClipRect(rect)
        painter.setFont(font)
        painter.setPen(color)
        fm = QFontMetrics(font)
        pixels = fm.horizontalAdvance(text)
        baseline = int(rect.top() + (rect.height() + fm.ascent() - fm.descent()) / 2)
        if pixels <= rect.width():
            painter.drawText(rect.left(), baseline, text)
        else:
            gap = max(32, rect.width() // 4)
            span = pixels + gap
            offset = self._scroll % span
            painter.drawText(rect.left() - offset, baseline, text)
            painter.drawText(rect.left() - offset + span, baseline, text)
        painter.restore()

    def _paint_vis(self, painter: QPainter, rect: QRect):
        """Draw the bars or the scope."""
        mode = self.controller.vis_mode
        if mode == "off" or rect.width() < 8 or rect.height() < 4:
            return
        active = self.controller.transport == "playing"
        if mode == "scope":
            painter.setPen(LED if active else LED_DIM)
            steps = max(12, rect.width() // 2)
            previous = None
            for index in range(steps + 1):
                frac = index / steps
                if active:
                    sample = math.sin(self._vis_t * 2.4 + frac * 8.0) * 0.55
                    sample += math.sin(self._vis_t * 1.1 + frac * 17.0) * 0.3
                else:
                    sample = 0.0
                x = rect.left() + frac * rect.width()
                y = rect.center().y() - sample * rect.height() * 0.42
                point = QPointF(x, y)
                if previous is not None:
                    painter.drawLine(previous, point)
                previous = point
            return
        count = max(10, min(28, rect.width() // max(3, self.controller.metrics.s * 3)))
        gap = max(1, self.controller.metrics.s // 2)
        width = max(2.0, (rect.width() - gap * (count - 1)) / count)
        for index in range(count):
            if active:
                level = 0.15 + 0.85 * abs(math.sin(self._vis_t * (1.15 + index * 0.13) + index))
                level *= 0.6 + 0.4 * abs(math.sin(self._vis_t * 0.63 + index * 0.47))
            else:
                level = 0.05
            bar_h = max(1.0, level * rect.height())
            x = rect.left() + index * (width + gap)
            painter.fillRect(QRectF(x, rect.bottom() - bar_h, width, bar_h), LED if active else LED_DIM)

    def _paint_info(self, painter: QPainter, rect: QRect):
        """Draw bitrate, sample rate, and the time readout."""
        controller = self.controller
        rate = f"{controller.kbps}k" if controller.kbps > 0 else "---"
        if controller.hz > 0:
            khz = controller.hz / 1000.0
            sample = f"{khz:.1f}".rstrip("0").rstrip(".")
        else:
            sample = "--"
        if controller.channels == 1:
            mode = "mono"
        elif controller.channels == 2:
            mode = "stereo"
        elif controller.channels > 2:
            mode = f"{controller.channels}ch"
        else:
            mode = ""
        font = self.controller.metrics.font(self.controller.metrics.small_px, mono=True)
        painter.setFont(font)
        painter.setPen(LED if controller.transport == "playing" else LED_DIM)
        lines = [rate, f"{sample} kHz", mode]
        fm = QFontMetrics(font)
        line_h = fm.height()
        block = line_h * 3
        y = rect.top() + max(0, (rect.height() - block) // 2) + fm.ascent()
        for line in lines:
            painter.drawText(rect.left(), y, line)
            y += line_h

    def _paint_slider_labels(self, painter: QPainter):
        """Draw the volume and balance captions."""
        font = self.controller.metrics.font(self.controller.metrics.small_px, bold=True)
        painter.setFont(font)
        vol_color = AMBER if self.controller.muted else TEXT_DIM
        painter.setPen(vol_color)
        painter.drawText(self._vol_label, int(Qt.AlignmentFlag.AlignCenter), "VOL")
        painter.setPen(TEXT_DIM)
        painter.drawText(self._bal_label, int(Qt.AlignmentFlag.AlignCenter), "BAL")

    def mousePressEvent(self, event):
        """Seek, toggle the time mode, or cycle the analyzer."""
        self._press_local = event.position().toPoint()
        self._click = event.button() == Qt.MouseButton.LeftButton
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        """Finish a click that started in the display."""
        dragged = self._dragging
        super().mouseReleaseEvent(event)
        if dragged or not self._click or event.button() != Qt.MouseButton.LeftButton:
            self._click = False
            return
        pos = event.position().toPoint()
        if self._press_local is not None and (pos - self._press_local).manhattanLength() > 6:
            return
        if self.controller.shaded:
            return
        time_rect, _title, vis_rect, _info = self._zones()
        if time_rect.contains(pos):
            self.controller.toggle_remaining()
        elif vis_rect.contains(pos):
            self.controller.cycle_vis()
        elif self._vol_label.contains(pos):
            self.controller.toggle_mute()
        elif self._bal_label.contains(pos):
            self.controller.center_balance()

    def wheelEvent(self, event):
        """Change the volume with the mouse wheel."""
        step = 5 if event.angleDelta().y() > 0 else -5
        self.controller.nudge_volume(step)

    def on_title_double_click(self):
        """Roll the main window up or down."""
        self.controller.toggle_shade()

    def closeEvent(self, event):
        """Quit the application when the main window closes."""
        event.accept()
        self.controller.quit()
