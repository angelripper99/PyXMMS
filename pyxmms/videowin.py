"""Video window. libmpv draws into an OpenGL surface inside the chrome.

Copyright (C) 2026 the PyXMMS authors. Released under GPL-2.0-or-later. See LICENSE.
"""

from __future__ import annotations

import ctypes
import logging

from PyQt6.QtCore import QEvent, QPoint, Qt
from PyQt6.QtGui import (
    QColor,
    QFontMetrics,
    QOffscreenSurface,
    QOpenGLContext,
    QPainter,
    QPalette,
    QSurfaceFormat,
    QWindow,
)
from PyQt6.QtWidgets import QApplication, QWidget

from . import APP_NAME
from .player import OPENGL_PROC
from .skin import LED, paint_title_rule, paint_window
from .widgets import MetalWindow, SkinButton, mime_paths

log = logging.getLogger("pyxmms")

GL_COLOR_BUFFER_BIT = 0x00004000
_CLEAR_COLOR = ctypes.CFUNCTYPE(None, ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float)
_CLEAR = ctypes.CFUNCTYPE(None, ctypes.c_uint)


class VideoSurface(QWindow):
    """OpenGL window mpv draws into. Embedded in the chrome by the host."""

    def __init__(self, host: "VideoWindow"):
        """Create the OpenGL window the picture is drawn into."""
        super().__init__()
        self._host = host
        self.setSurfaceType(QWindow.SurfaceType.OpenGLSurface)

    def event(self, event):
        """Redraw when the window system asks for an update."""
        kind = event.type()
        if kind == QEvent.Type.UpdateRequest:
            self._host.draw_frame()
            return True
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.RightButton:
            self._host.controller.popup_menu(event.globalPosition().toPoint())
            return True
        if kind in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
            mime = event.mimeData() if hasattr(event, "mimeData") else None
            if mime_paths(mime):
                event.acceptProposedAction()
            else:
                event.ignore()
            return True
        if kind == QEvent.Type.Drop:
            self._host.dropEvent(event)
            return True
        exposed = kind == QEvent.Type.Expose
        result = super().event(event)
        if exposed and self.isExposed():
            self._host.draw_frame()
        return result


class VideoWindow(MetalWindow):
    """The picture window around the OpenGL surface."""
    def __init__(self, controller):
        """Build the video window and its OpenGL context."""
        super().__init__(controller, "VIDEO")
        self.setWindowTitle(f"{APP_NAME} Video")
        self.player = controller.player
        self._placed = False
        self._user_resized = False
        self._resizing = None
        self._aspect = 16 / 9
        self._open_size = (320, 180)
        self._drawing = False
        self._context_warned = False
        self.gl = None
        self._offscreen = None
        self._gl_clear_color = None
        self._gl_clear = None
        self._proc_cb = OPENGL_PROC(self._proc)

        self.shade_btn = SkinButton("shade", parent=self)
        self.shade_btn.setToolTip("Shade")
        self.shade_btn.clicked.connect(self.controller.toggle_video_shade)

        self._make_context()
        self.surface = VideoSurface(self)
        if self.gl is not None:
            self.surface.setFormat(self.gl.format())
        self.container = QWidget.createWindowContainer(self.surface, self)
        self.container.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.container.setAutoFillBackground(True)
        palette = self.container.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#000000"))
        self.container.setPalette(palette)

        self.player.frame_ready.connect(self._on_frame, Qt.ConnectionType.QueuedConnection)
        self._fit_aspect()
        self.resize(*self._open_size)
        self.setMinimumWidth(160)
        self.apply_layout()

    def _make_context(self):
        """Create an OpenGL context, trying a few profiles."""
        profiles = (
            (QSurfaceFormat.OpenGLContextProfile.CompatibilityProfile, 3, 3),
            (QSurfaceFormat.OpenGLContextProfile.CoreProfile, 3, 3),
            (QSurfaceFormat.OpenGLContextProfile.NoProfile, 2, 0),
        )
        for profile, major, minor in profiles:
            fmt = QSurfaceFormat()
            fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
            fmt.setVersion(major, minor)
            fmt.setProfile(profile)
            fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
            fmt.setSwapInterval(1)
            ctx = QOpenGLContext(self)
            ctx.setFormat(fmt)
            if not ctx.create():
                continue
            off = QOffscreenSurface()
            off.setFormat(ctx.format())
            off.create()
            if not off.isValid():
                continue
            self.gl = ctx
            self._offscreen = off
            return
        log.warning("OpenGL is not available; video will play as audio")

    def warm_render(self) -> bool:
        """Attach libmpv to this GL context before the first file loads."""
        if self.gl is None or self._offscreen is None or not self._offscreen.isValid():
            return False
        if not self.gl.makeCurrent(self._offscreen):
            log.warning("OpenGL context would not start")
            return False
        try:
            self._resolve_gl()
            return bool(self.player.bind_render(self._proc_cb))
        finally:
            self.gl.doneCurrent()

    def release_render(self):
        """Detach libmpv and drop the OpenGL context."""
        try:
            self.player.frame_ready.disconnect(self._on_frame)
        except TypeError:
            pass
        if not self.player.render_ready or self.gl is None:
            return
        current = False
        if self._offscreen is not None and self._offscreen.isValid():
            current = bool(self.gl.makeCurrent(self._offscreen))
        try:
            self.player.free_render()
        finally:
            if current:
                self.gl.doneCurrent()

    def _proc(self, _ctx, name):
        """Look up one OpenGL function for libmpv."""
        try:
            if not name or self.gl is None:
                return None
            if isinstance(name, str):
                raw = name.encode("utf-8")
            elif isinstance(name, bytes):
                raw = name
            else:
                raw = bytes(name)
            addr = self.gl.getProcAddress(raw)
            if not addr:
                return None
            return int(addr)
        except Exception:
            log.exception("OpenGL lookup failed")
            return None

    def _resolve_gl(self):
        """Load the clear functions used before the first frame."""
        if self._gl_clear is not None or self.gl is None:
            return
        color = self.gl.getProcAddress(b"glClearColor")
        clear = self.gl.getProcAddress(b"glClear")
        if not color or not clear:
            return
        self._gl_clear_color = _CLEAR_COLOR(int(color))
        self._gl_clear = _CLEAR(int(clear))

    def _clear(self):
        """Clear the picture to black."""
        if self._gl_clear_color is None or self._gl_clear is None:
            return
        self._gl_clear_color(0.0, 0.0, 0.0, 1.0)
        self._gl_clear(GL_COLOR_BUFFER_BIT)

    def _on_frame(self):
        """Draw when libmpv has a new frame and the window is visible."""
        if not self.player.render_ready:
            return
        if self._can_present():
            self.surface.requestUpdate()
            return
        self._drain()

    def _can_present(self) -> bool:
        """True when the window is on screen and can draw."""
        return (
            self.isVisible()
            and not self.isMinimized()
            and not self.controller.video_shaded
            and self.surface.isExposed()
        )

    def draw_frame(self):
        """Draw one frame if a draw is not already in progress."""
        if self._drawing or not self.player.render_ready:
            return
        self._drawing = True
        try:
            if not self._can_present():
                self._drain()
                return
            self._present()
        finally:
            self._drawing = False

    def _present(self):
        """Make the GL context current and draw into the window."""
        if self.gl is None or not self.gl.makeCurrent(self.surface):
            if not self._context_warned:
                self._context_warned = True
                log.warning("video window lost its OpenGL context")
            self._drain()
            return
        try:
            ratio = float(self.surface.devicePixelRatio() or 1)
            width = max(1, int(round(self.surface.width() * ratio)))
            height = max(1, int(round(self.surface.height() * ratio)))
            if self.surface.width() < 2 or self.surface.height() < 2:
                self.player.render(0, 16, 16, skip=True)
                return
            fbo = int(self.gl.defaultFramebufferObject() or 0)
            self._clear()
            # This is a window drawable, so the picture is stored bottom-up.
            if not self.player.render(fbo, width, height, skip=False, flip=True):
                return
            self.gl.swapBuffers(self.surface)
            self.player.report_swap()
        finally:
            self.gl.doneCurrent()

    def _drain(self):
        """Advance mpv without drawing, so a hidden picture cannot stall the sound."""
        if self.gl is None or self._offscreen is None or not self._offscreen.isValid():
            return
        if not self.gl.makeCurrent(self._offscreen):
            return
        try:
            self.player.render(0, 16, 16, skip=True, flip=False)
        finally:
            self.gl.doneCurrent()

    def _chrome(self) -> tuple[int, int, int, int]:
        """The title and border sizes around the picture."""
        metrics = self.controller.metrics
        grip = max(10, 6 * metrics.s)
        return metrics.title_h, metrics.margin, grip, grip

    def minimum_open_height(self) -> int:
        """The smallest height that still shows a picture."""
        top, _left, _right, bottom = self._chrome()
        return top + bottom + 90

    def _fit_aspect(self):
        """Resize the window so the picture keeps its aspect."""
        metrics = self.controller.metrics
        top, left, right, bottom = self._chrome()
        aspect = self._aspect if self._aspect > 0 else (16 / 9)
        client_w = max(320, metrics.w * 2)
        client_h = max(1, int(round(client_w / aspect)))
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            max_w = max(200, area.width() - left - right - 40)
            max_h = max(90, area.height() - top - bottom - 80)
            if client_w > max_w:
                client_w = max_w
                client_h = max(1, int(round(client_w / aspect)))
            if client_h > max_h:
                client_h = max_h
                client_w = max(1, int(round(client_h * aspect)))
        self._open_size = (client_w + left + right, client_h + top + bottom)

    def note_size(self, width: int, height: int):
        """Remember the picture size and fit the window once."""
        if width < 2 or height < 2:
            return
        self._aspect = width / float(height)
        if self._user_resized or self.controller.video_shaded:
            return
        self._fit_aspect()
        if self._placed and self.isVisible():
            self.resize(*self._open_size)

    def present(self):
        """Show the picture window."""
        already = self.isVisible()
        if not self._placed:
            self._place()
        if self.controller.video_shaded:
            self.apply_shade()
        elif not self._user_resized:
            self._fit_aspect()
            self.resize(*self._open_size)
        self.show()
        self.apply_layout()
        if not already:
            self.raise_()

    def _place(self):
        """Put the window where it was saved, or beside the player."""
        saved = self.controller.saved_video
        if isinstance(saved, list) and len(saved) >= 4:
            x, y, w, h = (int(saved[0]), int(saved[1]), int(saved[2]), int(saved[3]))
            if QApplication.screenAt(QPoint(x + 20, y + 10)) is not None:
                self._user_resized = True
                self._open_size = (max(160, w), max(self.minimum_open_height(), h))
                self.resize(*self._open_size)
                self.move(x, y)
                self._placed = True
                return
        main = self.controller.main
        frame_w, frame_h = self._open_size
        x = main.x() + main.width() + 16
        y = main.y()
        screen = QApplication.screenAt(main.pos()) or QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            if x + frame_w > area.right():
                x = main.x() - frame_w - 16
            if x < area.left():
                x = area.left() + 8
            if y + frame_h > area.bottom():
                y = max(area.top(), area.bottom() - frame_h - 8)
        self.move(x, y)
        self._placed = True

    def geometry_record(self) -> list[int] | None:
        """The saved position and size, or None before the first show."""
        if not self._placed:
            return None
        width, height = self._open_size
        return [self.x(), self.y(), int(width), int(height)]

    def apply_metrics(self):
        """Resize the title bar for the current scale."""
        self.apply_shade()

    def apply_shade(self):
        """Roll the video window up or open it to the picture size."""
        metrics = self.controller.metrics
        if self.controller.video_shaded:
            if self.height() > metrics.title_h:
                self._open_size = (self.width(), self.height())
            self.setMinimumHeight(metrics.title_h)
            self.setMaximumHeight(metrics.title_h)
            self.resize(max(160, self._open_size[0]), metrics.title_h)
        else:
            self.setMaximumHeight(16777215)
            self.setMinimumHeight(self.minimum_open_height())
            width, height = self._open_size
            self.resize(max(160, width), max(self.minimum_open_height(), height))
        self.apply_layout()

    def apply_layout(self):
        """Place the title buttons and the OpenGL surface."""
        self._title_right = self.place_title_buttons(self.close_btn, self.shade_btn)
        if self.controller.video_shaded:
            self.container.hide()
            return
        top, left, right, bottom = self._chrome()
        self.container.show()
        self.container.setGeometry(
            left,
            top,
            max(1, self.width() - left - right),
            max(1, self.height() - top - bottom),
        )

    def on_title_double_click(self):
        """Roll the video window up or down."""
        self.controller.toggle_video_shade()

    def resizeEvent(self, event):
        """Keep the picture filling the area under the title bar."""
        super().resizeEvent(event)
        if not self.controller.video_shaded:
            self._open_size = (self.width(), self.height())
        self.apply_layout()

    def showEvent(self, event):
        """Ask for a frame when the window becomes visible."""
        super().showEvent(event)
        self.surface.requestUpdate()

    def _edge(self, pos) -> str | None:
        """Which border the pointer is on, for resizing."""
        if self.controller.video_shaded:
            return None
        _top, _left, right, bottom = self._chrome()
        on_right = pos.x() >= self.width() - right
        on_bottom = pos.y() >= self.height() - bottom
        if on_right and on_bottom:
            return "both"
        if on_right:
            return "right"
        if on_bottom:
            return "bottom"
        return None

    def _resize_to(self, width: int, height: int):
        """Resize the window and remember the new size."""
        width = max(self.minimumWidth(), int(width))
        height = max(self.minimumHeight(), int(height))
        screen = self.screen()
        if screen is not None:
            area = screen.availableGeometry()
            width = min(width, max(self.minimumWidth(), area.right() - self.x() + 1))
            height = min(height, max(self.minimumHeight(), area.bottom() - self.y() + 1))
        self._user_resized = True
        self.resize(width, height)

    def mousePressEvent(self, event):
        """Start a resize from a border."""
        if event.button() == Qt.MouseButton.LeftButton:
            edge = self._edge(event.position().toPoint())
            if edge:
                self._resizing = edge
                self._press_global = event.globalPosition().toPoint()
                self._start_size = (self.width(), self.height())
                self.container.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
                self.grabMouse()
                return
        self._resizing = None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """Resize while a border drag is active."""
        if getattr(self, "_resizing", None):
            delta = event.globalPosition().toPoint() - self._press_global
            width, height = self._start_size
            if self._resizing in ("right", "both"):
                width += delta.x()
            if self._resizing in ("bottom", "both"):
                height += delta.y()
            self._resize_to(width, height)
            return
        edge = self._edge(event.position().toPoint())
        if edge == "both":
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif edge == "right":
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif edge == "bottom":
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        else:
            self.unsetCursor()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        """Finish a border resize."""
        if getattr(self, "_resizing", None):
            self._resizing = None
            self.container.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
            self.releaseMouse()
            self.unsetCursor()
            self.controller.save_soon()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event):
        """Draw the video chrome. The picture itself is the OpenGL surface."""
        painter = QPainter(self)
        metrics = self.controller.metrics
        paint_window(painter, self.rect(), metrics.s)
        paint_title_rule(painter, self.width(), metrics.title_h)
        font = metrics.font(metrics.small_px + 1, bold=True)
        painter.setFont(font)
        fm = QFontMetrics(font)
        baseline = (metrics.title_h + fm.ascent() - fm.descent()) // 2
        painter.setPen(LED)
        painter.drawText(8, baseline, "VIDEO")
        if not self.controller.video_shaded:
            self._paint_grip(painter)
        painter.end()

    def _paint_grip(self, painter: QPainter):
        """Draw the resize grip in the corner."""
        color = QColor("#8a8a8a")
        painter.setPen(color)
        right = self.width() - 6
        bottom = self.height() - 5
        for offset in (0, 4, 8):
            painter.drawLine(right - offset, bottom, right, bottom - offset)

    def closeEvent(self, event):
        """Close the picture and keep the sound playing."""
        if self.controller.quitting:
            event.accept()
            return
        event.ignore()
        self.controller.hide_video()
