"""Ten-band equalizer window.

Copyright (C) 2026 the PyXMMS authors. Released under GPL-2.0-or-later. See LICENSE.
"""

from __future__ import annotations

from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtGui import QFontMetrics, QPainter
from PyQt6.QtWidgets import QMenu

from . import APP_NAME
from .audiofx import BAND_LABELS, PRESETS
from .skin import LED, TEXT, paint_title_rule, paint_window
from .widgets import MetalWindow, SkinButton, VSlider


class EqWindow(MetalWindow):
    """The ten-band equalizer, with a preamp and named presets."""
    def __init__(self, controller):
        """Build the on button, the preset button, and the sliders."""
        super().__init__(controller, "EQUALIZER")
        self.setWindowTitle(f"{APP_NAME} Equalizer")
        self.on_btn = SkinButton(text="ON", parent=self)
        self.on_btn.setToolTip("Turn the equalizer on or off")
        self.on_btn.clicked.connect(self.controller.toggle_eq_enabled)
        self.preset_btn = SkinButton(text="PRESET", parent=self)
        self.preset_btn.setToolTip("Equalizer presets")
        self.preset_btn.clicked.connect(self._open_presets)
        self.shade_btn = SkinButton("shade", parent=self)
        self.shade_btn.setToolTip("Shade")
        self.shade_btn.clicked.connect(self.controller.toggle_eq_shade)
        self.preamp = VSlider(self)
        self.preamp.setToolTip("Preamp. Right-click or double-click to zero.")
        self.preamp.changed.connect(self.controller.set_preamp)
        self.bands = []
        for index in range(10):
            slider = VSlider(self)
            slider.setToolTip(f"{BAND_LABELS[index]} Hz. Right-click to zero.")
            slider.changed.connect(lambda value, band=index: self.controller.set_band(band, value))
            self.bands.append(slider)
        self._readout = QRect()
        self._preset_name = "Flat"
        self.apply_metrics()

    def _open_presets(self):
        """Show the preset menu under the preset button."""
        menu = QMenu(self)
        for name in PRESETS:
            menu.addAction(name, lambda checked=False, preset=name: self.controller.apply_preset(preset))
        menu.exec(self.preset_btn.mapToGlobal(QPoint(0, self.preset_btn.height())))

    def apply_shade(self):
        """Roll the equalizer to the saved shade state."""
        self.apply_metrics()

    def on_title_double_click(self):
        """Roll the equalizer up or down."""
        self.controller.toggle_eq_shade()

    def apply_metrics(self):
        """Resize and place the equalizer controls for the current scale."""
        metrics = self.controller.metrics
        shaded = self.controller.eq_shaded
        self.setFixedSize(metrics.w, metrics.title_h if shaded else metrics.eq_h)
        for widget in (self.on_btn, self.preset_btn, self.preamp, *self.bands):
            widget.setVisible(not shaded)
        scale = metrics.s
        self._title_right = self.place_title_buttons(self.close_btn, self.shade_btn)
        top = metrics.title_h + 2 * scale
        height = 14 * scale
        on_w = max(self.on_btn.preferred_width(height, 6 * scale), 18 * scale)
        self.on_btn.setGeometry(metrics.margin, top, on_w, height)
        preset_w = self.preset_btn.preferred_width(height, 6 * scale)
        self.preset_btn.setGeometry(self.on_btn.geometry().right() + 3 * scale, top, preset_w, height)
        readout_x = self.preset_btn.geometry().right() + 6 * scale
        self._readout = QRect(readout_x, top, metrics.w - readout_x - metrics.margin, height)
        slider_top = top + height + 3 * scale
        label_h = max(10, 9 * scale)
        slider_h = metrics.eq_h - slider_top - label_h - 3 * scale
        count = 11
        gap = max(1, scale)
        usable = metrics.w - 2 * metrics.margin - gap * (count - 1)
        width = max(8, usable // count)
        x = metrics.margin
        for slider in (self.preamp, *self.bands):
            slider.setGeometry(x, slider_top, width, slider_h)
            x += width + gap
        self.sync()

    def sync(self):
        """Match the buttons and sliders to the current equalizer settings."""
        controller = self.controller
        self.on_btn.set_lit(controller.eq_enabled)
        self.preamp.set_db(controller.eq_preamp)
        for slider, value in zip(self.bands, controller.eq_bands):
            slider.set_db(value)
        self._preset_name = controller.eq_preset
        self.update()

    def paintEvent(self, _event):
        """Draw the equalizer chrome and the band labels."""
        painter = QPainter(self)
        metrics = self.controller.metrics
        paint_window(painter, self.rect(), metrics.s)
        paint_title_rule(painter, self.width(), metrics.title_h)
        font = metrics.font(metrics.small_px + 1, bold=True)
        painter.setFont(font)
        fm = QFontMetrics(font)
        baseline = (metrics.title_h + fm.ascent() - fm.descent()) // 2
        painter.setPen(LED)
        painter.drawText(metrics.margin, baseline, "EQUALIZER")
        if self.controller.eq_shaded:
            painter.setFont(metrics.font(metrics.small_px, bold=True))
            painter.setPen(LED if self.controller.eq_enabled else TEXT)
            left = metrics.margin + fm.horizontalAdvance("EQUALIZER") + 8
            rect = QRect(left, 0, max(0, self._title_right - left), metrics.title_h)
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), self._preset_name)
            painter.end()
            return
        painter.setFont(metrics.font(metrics.small_px, bold=True))
        painter.setPen(LED if self.controller.eq_enabled else TEXT)
        painter.drawText(
            self._readout,
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            self._preset_name,
        )
        painter.setFont(metrics.font(max(8, metrics.small_px - 1), bold=True))
        painter.setPen(TEXT)
        labels = ("PRE", *BAND_LABELS)
        for slider, label in zip((self.preamp, *self.bands), labels):
            geo = slider.geometry()
            painter.drawText(
                QRect(geo.x() - 2, geo.bottom() + 1, geo.width() + 4, metrics.eq_h - geo.bottom() - 2),
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                label,
            )
        painter.end()

    def dragEnterEvent(self, event):
        """Refuse a drop. Files go to the main window or the playlist."""
        event.ignore()

    def closeEvent(self, event):
        """Hide the equalizer, and quit only when the application is quitting."""
        if self.controller.quitting:
            event.accept()
            return
        event.ignore()
        self.controller.set_eq_visible(False)
