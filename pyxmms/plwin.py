"""Playlist window and its row view.

Copyright (C) 2026 the PyXMMS authors. Released under GPL-2.0-or-later. See LICENSE.
"""

from __future__ import annotations

from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtGui import QColor, QFontMetrics, QPainter
from PyQt6.QtWidgets import QWidget

from . import APP_NAME
from .playlist import format_time
from .skin import ACCENT_DEEP, ACCENT_SOFT, LED, TEXT, TEXT_DIM, paint_title_rule, paint_window
from .widgets import MetalWindow, SkinButton, mime_paths


class PlaylistView(QWidget):
    """The scrolling list of rows inside the playlist window."""
    def __init__(self, controller, parent):
        """Prepare selection, scroll position, and drag state."""
        super().__init__(parent)
        self.controller = controller
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.scroll = 0
        self.selected = set()
        self.anchor = None
        self.cursor = None
        self._press_track = None
        self._press_pos = QPoint()
        self._press_mods = Qt.KeyboardModifier.NoModifier
        self._dragging = False
        self._drop_row = None
        self._sb_drag = False
        self._sb_grab = 0

    def selected_tracks(self):
        """The rows that are selected, in playlist order."""
        order = []
        chosen = {id(track) for track in self.selected}
        for track in self.controller.playlist.tracks:
            if id(track) in chosen:
                order.append(track)
        return order

    def prune_selection(self):
        """Drop selections that no longer point at a row."""
        alive = {id(track) for track in self.controller.playlist.tracks}
        self.selected = {track for track in self.selected if id(track) in alive}
        if self.anchor is not None and id(self.anchor) not in alive:
            self.anchor = None
        if self.cursor is not None and id(self.cursor) not in alive:
            self.cursor = self.controller.playlist.current_track

    def select_all(self):
        """Select every row."""
        self.selected = set(self.controller.playlist.tracks)
        self.update()
        self._status()

    def follow(self, index: int):
        """Scroll so the given row is visible."""
        tracks = self.controller.playlist.tracks
        if not tracks:
            return
        index = max(0, min(index, len(tracks) - 1))
        self.cursor = tracks[index]
        self.ensure_visible(index)
        self.update()

    def ensure_visible(self, index: int):
        """Scroll the minimum amount that shows one row."""
        if index < self.scroll:
            self.scroll = index
        elif index >= self.scroll + self._visible_rows():
            self.scroll = index - self._visible_rows() + 1
        self.scroll = max(0, min(self.scroll, self._scroll_max()))

    def _row_h(self) -> int:
        """The height of one row at the current scale."""
        return max(14, self.controller.metrics.row_h)

    def _visible_rows(self) -> int:
        """How many rows fit in the view."""
        return max(1, self.height() // self._row_h())

    def _scroll_max(self) -> int:
        """The largest scroll offset."""
        return max(0, len(self.controller.playlist.tracks) - self._visible_rows())

    def _clamp_scroll(self):
        """Keep the scroll offset inside the list."""
        self.scroll = max(0, min(self.scroll, self._scroll_max()))

    def _content_width(self) -> int:
        """The width available for row text."""
        if self._scroll_max() > 0:
            return self.width() - self._scrollbar_width() - 2
        return self.width()

    def _scrollbar_width(self) -> int:
        """The scrollbar width at the current scale."""
        return max(8, 5 * self.controller.metrics.s)

    def _scrollbar_rect(self) -> QRect:
        """The scrollbar rectangle."""
        width = self._scrollbar_width()
        return QRect(self.width() - width - 1, 2, width, max(4, self.height() - 4))

    def _thumb_rect(self) -> QRect | None:
        """The scrollbar thumb, or None when the list fits."""
        if self._scroll_max() <= 0:
            return None
        track = self._scrollbar_rect()
        visible = self._visible_rows()
        total = max(1, len(self.controller.playlist.tracks))
        thumb_h = max(16, int(track.height() * visible / total))
        thumb_h = min(thumb_h, track.height())
        travel = max(1, track.height() - thumb_h)
        y = track.top() + int(travel * (self.scroll / self._scroll_max()))
        return QRect(track.left(), y, track.width(), thumb_h)

    def _row_at(self, y: int, for_drop: bool = False):
        """The row under a y position, or the insertion point during a drop."""
        tracks = self.controller.playlist.tracks
        if y < 0:
            return 0 if for_drop else None
        row_h = self._row_h()
        index = self.scroll + int(y // row_h)
        if for_drop:
            if row_h and (y % row_h) > row_h / 2:
                index += 1
            return max(0, min(len(tracks), index))
        if 0 <= index < len(tracks):
            return index
        return None

    def _status(self):
        """The total count and duration shown under the list."""
        window = self.window()
        if hasattr(window, "refresh_status"):
            window.refresh_status()

    def _select_range(self, start, end):
        """Select every row between two indexes."""
        tracks = self.controller.playlist.tracks
        if start not in tracks or end not in tracks:
            return
        lo, hi = sorted((tracks.index(start), tracks.index(end)))
        self.selected = set(tracks[lo:hi + 1])

    def mousePressEvent(self, event):
        """Select rows, start a drag, or grab the scrollbar."""
        self.setFocus()
        pos = event.position().toPoint()
        thumb = self._thumb_rect()
        if event.button() == Qt.MouseButton.LeftButton and thumb is not None and self._scrollbar_rect().contains(pos):
            self._sb_drag = True
            self._sb_grab = pos.y() - thumb.top()
            if not thumb.contains(pos):
                self._page_to(pos.y())
            return
        index = self._row_at(pos.y())
        tracks = self.controller.playlist.tracks
        if index is None:
            if not (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                self.selected.clear()
            self.cursor = None
            self._press_track = None
            self.update()
            self._status()
            return
        track = tracks[index]
        self.cursor = track
        mods = event.modifiers()
        if event.button() == Qt.MouseButton.RightButton:
            if track not in self.selected:
                self.selected = {track}
                self.anchor = track
            self.update()
            self._status()
            self.controller.playlist_menu(event.globalPosition().toPoint())
            return
        if mods & Qt.KeyboardModifier.ControlModifier:
            if track in self.selected:
                self.selected.discard(track)
            else:
                self.selected.add(track)
            self.anchor = track
        elif mods & Qt.KeyboardModifier.ShiftModifier and self.anchor is not None:
            self._select_range(self.anchor, track)
        elif track not in self.selected:
            self.selected = {track}
            self.anchor = track
        self._press_track = track
        self._press_pos = pos
        self._press_mods = mods
        self._dragging = False
        self._drop_row = None
        self.update()
        self._status()

    def mouseMoveEvent(self, event):
        """Drag rows or the scrollbar."""
        pos = event.position().toPoint()
        if self._sb_drag:
            self._drag_scrollbar(pos.y())
            return
        if self._press_track is None or not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        if not self._dragging and (pos - self._press_pos).manhattanLength() >= 6:
            self._dragging = True
            if self._press_track not in self.selected:
                self.selected = {self._press_track}
        if self._dragging:
            self._drop_row = self._row_at(pos.y(), for_drop=True)
            self.update()

    def mouseReleaseEvent(self, event):
        """Finish a row drag or a scrollbar drag."""
        if self._sb_drag and event.button() == Qt.MouseButton.LeftButton:
            self._sb_drag = False
            self.update()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._dragging and self._press_track is not None and self._drop_row is not None:
            moving = self.selected_tracks()
            if self._press_track not in moving:
                moving = [self._press_track]
            self.controller.move_tracks(moving, self._drop_row)
        elif (
            self._press_track is not None
            and not self._dragging
            and not (self._press_mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier))
        ):
            self.selected = {self._press_track}
            self.anchor = self._press_track
            self._status()
        self._dragging = False
        self._press_track = None
        self._drop_row = None
        self.update()

    def mouseDoubleClickEvent(self, event):
        """Play the row that was double-clicked."""
        index = self._row_at(int(event.position().y()))
        if index is not None:
            self.controller.play_index(index)

    def wheelEvent(self, event):
        """Scroll the list with the mouse wheel."""
        step = 3 if abs(event.angleDelta().y()) >= 120 else 1
        direction = -1 if event.angleDelta().y() > 0 else 1
        self.scroll = max(0, min(self._scroll_max(), self.scroll + direction * step))
        self.update()

    def keyPressEvent(self, event):
        """Remove, select, or move the selection from the keyboard."""
        key = event.key()
        tracks = self.controller.playlist.tracks
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.controller.remove_tracks(self.selected_tracks())
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.cursor is not None and self.cursor in tracks:
                self.controller.play_index(tracks.index(self.cursor))
            return
        if key == Qt.Key.Key_A and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.select_all()
            return
        if key not in (
            Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Home,
            Qt.Key.Key_End, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown,
        ):
            super().keyPressEvent(event)
            return
        if not tracks:
            return
        if self.cursor in tracks:
            index = tracks.index(self.cursor)
        elif self.controller.playlist.current is not None:
            index = self.controller.playlist.current
        else:
            index = 0
        page = self._visible_rows()
        if key == Qt.Key.Key_Up:
            index -= 1
        elif key == Qt.Key.Key_Down:
            index += 1
        elif key == Qt.Key.Key_Home:
            index = 0
        elif key == Qt.Key.Key_End:
            index = len(tracks) - 1
        elif key == Qt.Key.Key_PageUp:
            index -= page
        elif key == Qt.Key.Key_PageDown:
            index += page
        index = max(0, min(len(tracks) - 1, index))
        self.cursor = tracks[index]
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier and self.anchor is not None:
            self._select_range(self.anchor, self.cursor)
        else:
            self.selected = {self.cursor}
            self.anchor = self.cursor
        self.ensure_visible(index)
        self.update()
        self._status()

    def _page_to(self, y: int):
        """Scroll by a page toward a y position."""
        thumb = self._thumb_rect()
        if thumb is None:
            return
        page = self._visible_rows()
        if y < thumb.top():
            self.scroll = max(0, self.scroll - page)
        elif y > thumb.bottom():
            self.scroll = min(self._scroll_max(), self.scroll + page)
        self.update()

    def _drag_scrollbar(self, y: int):
        """Scroll so the thumb follows the pointer."""
        track = self._scrollbar_rect()
        thumb = self._thumb_rect()
        if thumb is None:
            return
        travel = max(1, track.height() - thumb.height())
        origin = y - self._sb_grab - track.top()
        fraction = min(1.0, max(0.0, origin / travel))
        self.scroll = int(round(fraction * self._scroll_max()))
        self.update()

    def paintEvent(self, _event):
        """Draw the rows, the current row, and the scrollbar."""
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#07060C"))
        tracks = self.controller.playlist.tracks
        self._clamp_scroll()
        if not tracks:
            painter.setPen(TEXT_DIM)
            font = self.controller.metrics.font(self.controller.metrics.font_px)
            painter.setFont(font)
            painter.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), "Drop audio files here")
            painter.end()
            return
        metrics = self.controller.metrics
        font = metrics.font(max(10, metrics.font_px - 1))
        painter.setFont(font)
        fm = QFontMetrics(font)
        row_h = self._row_h()
        width = self._content_width()
        digits = max(2, len(str(len(tracks))))
        number_w = fm.horizontalAdvance("0" * digits) + 10
        duration_w = fm.horizontalAdvance("00:00:00") + 8
        current = self.controller.playlist.current
        y = 0
        index = self.scroll
        while y < self.height() and index < len(tracks):
            track = tracks[index]
            row = QRect(0, y, width, row_h)
            is_current = index == current
            is_selected = track in self.selected
            if is_current:
                painter.fillRect(row, QColor("#1A0E2E"))
            elif is_selected:
                painter.fillRect(row, QColor("#140E1C"))
            elif index % 2:
                painter.fillRect(row, QColor("#0C0A10"))
            if is_current:
                painter.fillRect(QRect(0, y, 3, row_h), LED)
            elif is_selected:
                painter.setPen(ACCENT_DEEP)
                painter.drawRect(row.adjusted(0, 0, -1, -1))
            if track.available():
                color = LED if is_current else QColor("#d5d5d5")
            else:
                color = QColor("#8a4a4a")
            painter.setPen(TEXT_DIM if not is_current else ACCENT_SOFT)
            painter.drawText(
                QRect(6, y, number_w, row_h),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight),
                str(index + 1),
            )
            duration = format_time(track.duration) if track.duration else ""
            name_left = 8 + number_w
            name_width = max(10, width - name_left - duration_w - 4)
            painter.setPen(color)
            name = fm.elidedText(track.name, Qt.TextElideMode.ElideRight, name_width)
            painter.drawText(
                QRect(name_left, y, name_width, row_h),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                name,
            )
            if duration:
                painter.setPen(ACCENT_SOFT if is_current else TEXT_DIM)
                painter.drawText(
                    QRect(width - duration_w, y, duration_w - 6, row_h),
                    int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight),
                    duration,
                )
            index += 1
            y += row_h
        if self._dragging and self._drop_row is not None:
            line_y = (self._drop_row - self.scroll) * row_h
            painter.setPen(LED)
            painter.drawLine(0, line_y, width, line_y)
        self._paint_scrollbar(painter)
        painter.setPen(ACCENT_DEEP)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        painter.end()

    def _paint_scrollbar(self, painter: QPainter):
        """Draw the scrollbar track and thumb."""
        thumb = self._thumb_rect()
        if thumb is None:
            return
        track = self._scrollbar_rect()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#121412"))
        painter.drawRect(track)
        painter.setBrush(QColor("#3a3a3a"))
        painter.drawRect(thumb)

    def dragEnterEvent(self, event):
        """Accept a drop of files or http addresses."""
        if mime_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        """Show where a dropped row would be inserted."""
        if mime_paths(event.mimeData()):
            event.acceptProposedAction()
            self._drop_row = self._row_at(int(event.position().y()), for_drop=True)
            self._dragging = True
            self.update()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        """Clear the drop marker."""
        self._dragging = False
        self._drop_row = None
        self.update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        """Insert the dropped files or addresses."""
        paths = mime_paths(event.mimeData())
        index = self._row_at(int(event.position().y()), for_drop=True)
        self._dragging = False
        self._drop_row = None
        self.update()
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self.controller.add_paths(paths, insert_at=index if index is not None else len(self.controller.playlist.tracks))


class PlaylistWindow(MetalWindow):
    """The playlist window, with its buttons and the row view."""
    def __init__(self, controller):
        """Build the playlist buttons and the row view."""
        super().__init__(controller, "PLAYLIST")
        self.setWindowTitle(f"{APP_NAME} Playlist")
        self.view = PlaylistView(controller, self)
        self.add_btn = self._button("Add", "Add files", controller.open_files)
        self.dir_btn = self._button("Dir", "Add folder", controller.open_directory)
        self.rem_btn = self._button("Rem", "Remove selected", self._remove)
        self.crop_btn = self._button("Crop", "Remove everything that is not selected", controller.crop)
        self.url_btn = self._button("URL", "Add a station or file URL", controller.open_url)
        self._bar_buttons = (self.add_btn, self.dir_btn, self.rem_btn, self.crop_btn, self.url_btn)
        self.shade_btn = SkinButton("shade", parent=self)
        self.shade_btn.setToolTip("Shade")
        self.shade_btn.clicked.connect(self.controller.toggle_playlist_shade)
        self._status = ""
        self._open_height = 0
        self._status_left = 0
        self._resizing = False
        self._resize_y = 0.0
        self._resize_h = 0
        self._pressed_in_grip = False
        self.apply_metrics(resize_height=True)

    def _button(self, text, tip, slot) -> SkinButton:
        """Make one playlist-bar button."""
        button = SkinButton(text=text, parent=self)
        button.setToolTip(tip)
        button.clicked.connect(slot)
        return button

    def _remove(self):
        """Remove the selected rows."""
        self.controller.remove_tracks(self.view.selected_tracks())

    def minimum_open_height(self) -> int:
        """The smallest height that still shows a few rows."""
        metrics = self.controller.metrics
        return metrics.title_h + metrics.bottom_h + metrics.row_h * 3

    def open_height(self) -> int:
        """The height used when the window is unrolled."""
        if self.controller.playlist_shaded:
            return max(self.minimum_open_height(), self._open_height)
        return self.height()

    def set_open_height(self, height: int):
        """Remember the unrolled height."""
        height = max(self.minimum_open_height(), int(height))
        self._open_height = height
        if not self.controller.playlist_shaded:
            self.setMaximumHeight(16777215)
            self.setMinimumHeight(self.minimum_open_height())
            self.resize(self.controller.metrics.w, height)

    def apply_shade(self):
        """Roll the playlist window to the saved shade state."""
        metrics = self.controller.metrics
        if self.controller.playlist_shaded and self.height() > metrics.title_h:
            self._open_height = self.height()
        self.apply_metrics(resize_height=False)

    def on_title_double_click(self):
        """Roll the playlist window up or down."""
        self.controller.toggle_playlist_shade()

    def apply_metrics(self, resize_height: bool = False):
        """Resize the playlist window for the current scale."""
        metrics = self.controller.metrics
        self.setMinimumWidth(metrics.w)
        self.setMaximumWidth(metrics.w)
        open_min = self.minimum_open_height()
        if self.controller.playlist_shaded:
            if self._open_height < open_min:
                self._open_height = metrics.title_h + metrics.bottom_h + metrics.row_h * 8
            self.setMinimumHeight(metrics.title_h)
            self.setMaximumHeight(metrics.title_h)
            self.resize(metrics.w, metrics.title_h)
        else:
            # Decide the height before raising the minimum. setMinimumHeight
            # grows a shaded window to that minimum and would look like a resize.
            default = metrics.title_h + metrics.bottom_h + metrics.row_h * 8
            if resize_height or (self.height() < open_min and self._open_height < open_min):
                target = default
            elif self.height() >= open_min:
                target = self.height()
            else:
                target = self._open_height
            self._open_height = max(open_min, target)
            self.setMaximumHeight(16777215)
            self.setMinimumHeight(open_min)
            self.resize(metrics.w, self._open_height)
        self._layout()

    def _layout(self):
        """Place the row view and the buttons."""
        metrics = self.controller.metrics
        self._layout_title()
        shaded = self.controller.playlist_shaded
        self.view.setVisible(not shaded)
        for button in self._bar_buttons:
            button.setVisible(not shaded)
        if shaded:
            return
        top = metrics.title_h + 3
        self.view.setGeometry(4, top, self.width() - 8, max(10, self.height() - top - metrics.bottom_h))
        y = self.height() - metrics.bottom_h + 3
        height = max(14, metrics.bottom_h - 6)
        x = 6
        gap = max(3, 2 * metrics.s)
        for button in self._bar_buttons:
            width = button.preferred_width(height, 8 * metrics.s)
            button.setGeometry(x, y, width, height)
            x += width + gap
        self._status_left = x + 4
        self.refresh_status()

    def _layout_title(self):
        """Place the shade and close buttons on the title bar."""
        self._title_right = self.place_title_buttons(self.close_btn, self.shade_btn)

    def resizeEvent(self, event):
        """Keep the unrolled height and reflow docked windows."""
        self._layout()
        super().resizeEvent(event)

    def refresh_status(self):
        """Update the count and duration under the list."""
        playlist = self.controller.playlist
        selected = self.view.selected_tracks()
        if len(selected) == 1:
            track = selected[0]
            label = track.name
            if track.duration:
                label = f"{label}   {format_time(track.duration)}"
            self._status = label
        else:
            total, complete = playlist.known_duration()
            label = f"{len(playlist.tracks)} track" + ("" if len(playlist.tracks) == 1 else "s")
            if playlist.tracks and total > 0:
                label += "   " + format_time(total)
                if not complete:
                    label += "+"
            if len(selected) > 1:
                label = f"{len(selected)} selected    {label}"
            self._status = label
        self.update()

    def sync(self):
        """Redraw the rows and the status line."""
        self.view.prune_selection()
        self.view._clamp_scroll()
        self.refresh_status()
        self.view.update()

    def _in_grip(self, pos) -> bool:
        """True when a point is on the resize grip."""
        if self.controller.playlist_shaded:
            return False
        grip = max(10, 6 * self.controller.metrics.s)
        return pos.y() >= self.height() - grip and pos.x() >= self.width() - 28 * self.controller.metrics.s

    def paintEvent(self, _event):
        """Draw the playlist chrome, buttons, and status line."""
        painter = QPainter(self)
        metrics = self.controller.metrics
        paint_window(painter, self.rect(), metrics.s)
        paint_title_rule(painter, self.width(), metrics.title_h)
        font = metrics.font(metrics.small_px + 1, bold=True)
        painter.setFont(font)
        fm = QFontMetrics(font)
        baseline = (metrics.title_h + fm.ascent() - fm.descent()) // 2
        painter.setPen(LED)
        label_x = 8 * max(1, metrics.s // 2) + 4
        painter.drawText(label_x, baseline, "PLAYLIST")
        if self.controller.playlist_shaded:
            track = self.controller.playlist.current_track
            name = track.name if track is not None else ""
            left = label_x + fm.horizontalAdvance("PLAYLIST") + 8
            if name and left < self._title_right - 8:
                painter.setFont(metrics.font(metrics.small_px))
                painter.setPen(TEXT)
                rect = QRect(left, 0, self._title_right - left, metrics.title_h)
                elided = QFontMetrics(painter.font()).elidedText(name, Qt.TextElideMode.ElideRight, rect.width())
                painter.drawText(rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), elided)
            painter.end()
            return
        if self._status and self._status_left < self.width() - 16:
            painter.setFont(metrics.font(metrics.small_px))
            painter.setPen(TEXT)
            rect = QRect(self._status_left, self.height() - metrics.bottom_h, self.width() - self._status_left - 18, metrics.bottom_h)
            elided = QFontMetrics(painter.font()).elidedText(self._status, Qt.TextElideMode.ElideLeft, rect.width())
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), elided)
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

    def mousePressEvent(self, event):
        """Start a resize from the grip, or a title-bar drag."""
        if event.button() == Qt.MouseButton.LeftButton and self._in_grip(event.position().toPoint()):
            self._resizing = True
            self._pressed_in_grip = True
            self._resize_y = event.globalPosition().y()
            self._resize_h = self.height()
            return
        self._pressed_in_grip = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """Resize the window while the grip is held."""
        if self._resizing:
            height = int(self._resize_h + (event.globalPosition().y() - self._resize_y))
            height = max(self.minimumHeight(), height)
            screen = self.screen()
            if screen is not None:
                limit = screen.availableGeometry().bottom() - self.y()
                height = min(height, max(self.minimumHeight(), limit))
            self.resize(self.width(), height)
            return
        if self._in_grip(event.position().toPoint()):
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        else:
            self.unsetCursor()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        """Finish a resize or a title-bar drag."""
        if self._resizing:
            self._resizing = False
            self.controller.end_drag()
            self.controller.save_soon()
            return
        super().mouseReleaseEvent(event)

    def closeEvent(self, event):
        """Hide the playlist, and quit only when the application is quitting."""
        if self.controller.quitting:
            event.accept()
            return
        event.ignore()
        self.controller.set_playlist_visible(False)
