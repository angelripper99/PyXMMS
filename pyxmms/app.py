"""Application controller: windows, playback, and saved state.

Copyright (C) 2026 the PyXMMS authors. Released under GPL-2.0-or-later. See LICENSE.
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import queue
import signal
import subprocess
import sys
import time
from pathlib import Path

from PyQt6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPalette
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QInputDialog,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
)

from . import APP_NAME, __version__
from .audiofx import PRESETS
from .eqwin import EqWindow
from .mainwin import MainWindow
from .player import END_EOF, END_ERROR, END_STOP, MpvPlayer, ensure_c_numeric
from .playlist import (
    MEDIA_EXT,
    Playlist,
    expand_entries,
    is_live_playback,
    is_stream_url,
    shows_picture,
    playlist_open_filter,
    playlist_save_filter,
    playlist_text,
    track_from_config,
    track_record,
    with_playlist_suffix,
)
from .plwin import PlaylistWindow
from .skin import Metrics, make_icon_pixmap
from .videowin import VideoWindow

log = logging.getLogger("pyxmms")

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER_NAME = "pyxmms"
DESKTOP_ID = "pyxmms"
# Newest previous name first. Settings are copied once, and a running
# process that still uses one of these folders still counts as this player.
LEGACY_CONFIG_DIRS = (
    Path.home() / ".config" / "pyxmms-clone",
    Path.home() / ".config" / "xmms-clone",
)
CONFIG_DIR = Path.home() / ".config" / "pyxmms"
CONFIG_PATH = CONFIG_DIR / "state.json"

HELP = f"""\
{APP_NAME} — a classic-style music player

Usage:
  {LAUNCHER_NAME} [files or folders...]
  python3 -m pyxmms [files or folders...]

Options:
  --debug     log mpv and player messages
  -h, --help  show this help

Shortcuts:
  Z previous    X play    C pause    V stop    B next
  Left / Right  seek 5 seconds (Shift seeks 30)
  Up / Down     volume
  S shuffle     R repeat    L playlist    E equalizer
  Space         pause
  Ctrl+O        add files
  Ctrl+D        add a folder
  Ctrl+S        save the playlist (M3U, PLS, XSPF, or Audacious)
  Ctrl+Q        quit

Shift-drag a window to separate it from the stack.
Double-click a title bar, or use its shade button, to roll that window up.
A video file opens a picture window. Closing that window leaves the sound playing.
Add URL plays an internet station or an IPTV stream. A channel list opens as separate channels. The link needs to start with http:// or https://.

A second launch stops if {APP_NAME} is already running.
"""


def media_filter() -> str:
    """File-dialog filter for audio, video, and playlist suffixes."""
    patterns = " ".join("*" + ext for ext in sorted(MEDIA_EXT))
    return f"Media ({patterns});;{playlist_open_filter()};;All files (*)"


def probe_file(path: str):
    """Read a local file's title, artist, and duration with ffprobe."""
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-protocol_whitelist", "file,crypto",
                "-print_format", "json", "-show_format",
                "--", path,
            ],
            capture_output=True,
            text=True,
            timeout=8,
        )
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return "", "", None
    fmt = data.get("format") or {}
    tags = {str(key).lower(): str(value) for key, value in (fmt.get("tags") or {}).items()}
    title = tags.get("title", "").strip()
    artist = (tags.get("artist") or tags.get("album_artist") or "").strip()
    duration = None
    raw = fmt.get("duration")
    if raw not in (None, ""):
        try:
            duration = float(raw)
        except (TypeError, ValueError):
            duration = None
        if duration is not None and (duration < 0 or duration > 24 * 60 * 60):
            duration = None
    return title, artist, duration


class Prober(QThread):
    """Background thread that fills titles and durations for local files."""
    probed = pyqtSignal(str, str, str, float)

    def __init__(self):
        """Create the queue the controller feeds with new paths."""
        super().__init__()
        self._queue = queue.Queue()
        self._stop = False
        self._seen = set()

    def enqueue(self, paths):
        """Queue local files that have not been probed yet."""
        for path in paths:
            if not path or "://" in path or path in self._seen:
                continue
            self._seen.add(path)
            self._queue.put(path)

    def stop(self):
        """Ask the thread to finish after the current file."""
        self._stop = True
        self._queue.put(None)

    def run(self):
        """Probe queued files and emit title, artist, and duration."""
        while not self._stop:
            path = self._queue.get()
            if path is None or self._stop:
                return
            title, artist, duration = probe_file(path)
            self.probed.emit(path, title, artist, -1.0 if duration is None else float(duration))


class Controller(QObject):
    """Owns the windows, the playlist, playback, and saved state."""
    def __init__(self, start_paths=None, config_path=None, show=True):
        """Load settings, build the windows, and start playback of any launch paths."""
        super().__init__()
        if config_path is None:
            migrate_config()
        self.config_path = Path(config_path) if config_path else CONFIG_PATH
        self.quitting = False
        self._shut = False
        self._minimizing = False
        self._drag_origin = None
        self._drag_pos = {}
        self._error_skips = 0
        self.transport = "stopped"
        self.position = 0.0
        self.duration = 0.0
        self.title = ""
        self.kbps = 0
        self.hz = 0
        self.channels = 0
        self.notice = ""
        self.notice_until = 0.0
        self.volume = 80
        self.balance = 0.0
        self.muted = False
        self.remaining = False
        self.vis_mode = "bars"
        self.shaded = False
        self.eq_shaded = False
        self.playlist_shaded = False
        self.show_playlist = True
        self.show_eq = True
        self.video_shaded = False
        self.saved_video = None
        self._file_has_video = False
        self._video_closed = False
        self._video_closed_path = ""
        self._video_ready = False
        self.docked = True
        self.on_top = False
        self.eq_enabled = False
        self.eq_preamp = 0.0
        self.eq_bands = [0.0] * 10
        self.eq_preset = "Flat"
        self.last_dir = str(Path.home())
        self._saved_geometry = None
        self._saved_shuffle = False
        self._saved_repeat = "off"
        saved_tracks = []
        saved_index = None
        data = self._read_config()
        if data:
            self._apply_config(data)
            saved_tracks = []
            for item in data.get("tracks") or []:
                track = track_from_config(item)
                if track is not None:
                    saved_tracks.append(track)
            saved_index = data.get("current")
        try:
            scale = int(data.get("scale", 1)) if data else 1
        except (TypeError, ValueError):
            scale = 1
        self.metrics = Metrics(scale)
        self.playlist = Playlist()
        if saved_tracks:
            self.playlist.insert(0, saved_tracks)
            if isinstance(saved_index, int):
                self.playlist.set_current(saved_index)
            self.playlist.shuffle = bool(self._saved_shuffle)
            self.playlist.repeat = self._saved_repeat
            if self.playlist.shuffle:
                self.playlist._refill_bag()

        self.player = MpvPlayer()
        self.icon = QIcon(make_icon_pixmap(128))
        self.main = MainWindow(self)
        self.eqwin = EqWindow(self)
        self.plwin = PlaylistWindow(self)
        self.videowin = VideoWindow(self)
        for window in (self.main, self.eqwin, self.plwin, self.videowin):
            window.setWindowIcon(self.icon)
        self.playlist.on_change(self._on_playlist_change)
        self.player.position_changed.connect(self._on_position)
        self.player.duration_changed.connect(self._on_duration)
        self.player.state_changed.connect(self._on_state)
        self.player.title_changed.connect(self._on_title)
        self.player.info_changed.connect(self._on_info)
        self.player.loaded.connect(self._on_loaded)
        self.player.ended.connect(self._on_ended)
        self.player.log_line.connect(self._on_log)
        self.player.video_changed.connect(self._on_video)
        self.player.video_sized.connect(self.videowin.note_size)
        self._video_hide_timer = QTimer(self)
        self._video_hide_timer.setSingleShot(True)
        self._video_hide_timer.setInterval(400)
        self._video_hide_timer.timeout.connect(self._video_gone)
        self._stream_retries = 0
        self._stream_retry_path = ""
        self._stream_retry_timer = QTimer(self)
        self._stream_retry_timer.setSingleShot(True)
        self._stream_retry_timer.timeout.connect(self._retry_stream)
        self._stream_stable = QTimer(self)
        self._stream_stable.setSingleShot(True)
        self._stream_stable.setInterval(8000)
        self._stream_stable.timeout.connect(self._stream_held)
        self._picture_wait_path = ""
        self._picture_timer = QTimer(self)
        self._picture_timer.setSingleShot(True)
        self._picture_timer.setInterval(700)
        self._picture_timer.timeout.connect(self._consider_picture)
        self._push_audio()

        self.prober = Prober()
        self.prober.probed.connect(self._on_probed)
        self.prober.start()
        self.prober.enqueue(track.path for track in self.playlist.tracks)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(700)
        self._save_timer.timeout.connect(self.save_state)
        self._probe_timer = QTimer(self)
        self._probe_timer.setSingleShot(True)
        self._probe_timer.setInterval(120)
        self._probe_timer.timeout.connect(self._flush_probe_ui)

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self.tray = None
        if show:
            self._show()
            self._setup_tray()
            if start_paths:
                before = len(self.playlist.tracks)
                self.add_paths(start_paths, play=False)
                if len(self.playlist.tracks) > before:
                    self.play_index(before)

    def _read_config(self) -> dict:
        """Read state.json, or an empty dict when it is missing or broken."""
        try:
            return json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _apply_config(self, data: dict):
        """Copy saved volume, equalizer, window, and playback choices onto the controller."""
        self.volume = _clamp_int(data.get("volume"), 0, 100, 80)
        self.balance = _clamp_float(data.get("balance"), -1, 1, 0)
        self.muted = bool(data.get("muted", False))
        self.remaining = bool(data.get("remaining", False))
        self.vis_mode = data.get("vis") if data.get("vis") in ("bars", "scope", "off") else "bars"
        self.shaded = bool(data.get("shaded", False))
        self.eq_shaded = bool(data.get("eq_shaded", False))
        self.playlist_shaded = bool(data.get("playlist_shaded", False))
        self.show_playlist = bool(data.get("show_playlist", True))
        self.show_eq = bool(data.get("show_eq", True))
        self.video_shaded = bool(data.get("video_shaded", False))
        video = data.get("video")
        self.saved_video = video if isinstance(video, list) and len(video) >= 4 else None
        self.docked = bool(data.get("docked", True))
        self.on_top = bool(data.get("on_top", False))
        self.eq_enabled = bool(data.get("eq_on", False))
        self.eq_preamp = _clamp_float(data.get("eq_preamp"), -12, 12, 0)
        bands = data.get("eq_bands")
        if isinstance(bands, list) and len(bands) == 10:
            self.eq_bands = [_clamp_float(value, -12, 12, 0) for value in bands]
        preset = data.get("eq_preset")
        self.eq_preset = preset if isinstance(preset, str) and preset else "Flat"
        last_dir = data.get("last_dir")
        if isinstance(last_dir, str) and last_dir:
            self.last_dir = last_dir
        geometry = data.get("geometry")
        self._saved_geometry = geometry if isinstance(geometry, dict) else None
        self._saved_shuffle = bool(data.get("shuffle", False))
        repeat = data.get("repeat")
        self._saved_repeat = repeat if repeat in ("off", "all", "one") else "off"

    def _show(self):
        """Show the windows that were open in the saved state."""
        if self.on_top:
            self._apply_on_top()
        self._place()
        self.main.show()
        if self.show_eq:
            self.eqwin.show()
        if self.show_playlist:
            self.plwin.show()
        self.raise_windows()

    def _place(self):
        """Put each window back where it was, or center the stack."""
        screen = QApplication.primaryScreen()
        primary = screen.availableGeometry() if screen is not None else QRect(0, 0, 1280, 800)
        saved = self._saved_geometry or {}
        main_pos = saved.get("main")
        if isinstance(main_pos, list) and len(main_pos) >= 2:
            x, y = int(main_pos[0]), int(main_pos[1])
            there = QApplication.screenAt(QPoint(x + 20, y + 10))
            if there is not None:
                self.main.move(x, y)
                playlist = saved.get("playlist")
                if isinstance(playlist, list) and len(playlist) >= 3:
                    cap = self.metrics.title_h + self.metrics.bottom_h + self.metrics.row_h * 12
                    height = max(self.plwin.minimum_open_height(), min(cap, int(playlist[2])))
                    self.plwin.set_open_height(height)
                    if not self.docked:
                        self.plwin.move(int(playlist[0]), int(playlist[1]))
                eq_pos = saved.get("eq")
                if not self.docked and isinstance(eq_pos, list) and len(eq_pos) >= 2:
                    self.eqwin.move(int(eq_pos[0]), int(eq_pos[1]))
                if self.docked:
                    self.reflow()
                return
        self._center_stack(primary)

    def _center_stack(self, area: QRect):
        """Stack the open windows in the middle of the screen."""
        playlist_h = self.plwin.height()
        eq_h = self.eqwin.height() if self.show_eq else 0
        pl_h = playlist_h if self.show_playlist else 0
        spare = area.height() - self.main.height() - eq_h - 24
        if self.show_playlist and playlist_h > spare:
            playlist_h = max(self.plwin.minimumHeight(), spare)
            self.plwin.resize(self.metrics.w, playlist_h)
            pl_h = playlist_h
        total = self.main.height() + eq_h + pl_h
        x = area.x() + max(0, (area.width() - self.metrics.w) // 2)
        y = area.y() + max(0, (area.height() - total) // 2)
        self.main.move(x, y)
        self.reflow()

    def reflow(self):
        """Snap docked windows together after a move or a size change."""
        if not self.docked:
            return
        x = self.main.x()
        y = self.main.y() + self.main.height()
        self.eqwin.move(x, y)
        if self.show_eq:
            y += self.eqwin.height()
        self.plwin.move(x, y)
        self.plwin.resize(self.metrics.w, self.plwin.height())

    def _apply_on_top(self):
        """Raise or clear the always-on-top flag on every window."""
        for window in (self.main, self.eqwin, self.plwin, self.videowin):
            visible = window.isVisible()
            window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self.on_top)
            if visible:
                window.show()

    def _setup_tray(self):
        """Add the system-tray icon and its menu."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(self.icon, self.main)
        tray.setToolTip(APP_NAME)
        menu = QMenu()
        menu.addAction("Play / Pause", self.pause_or_play)
        menu.addAction("Next", self.next_track)
        menu.addAction("Previous", self.prev_track)
        menu.addAction("Show", self.raise_windows)
        menu.addSeparator()
        menu.addAction("Quit", self.quit)
        tray.setContextMenu(menu)
        tray.activated.connect(self._tray_activated)
        tray.show()
        self.tray = tray

    def _tray_activated(self, reason):
        """Show the windows when the tray icon is clicked."""
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.raise_windows()

    def raise_windows(self):
        """Bring the open windows to the front."""
        for window in (self.plwin, self.eqwin, self.videowin, self.main):
            if window.isVisible():
                window.showNormal()
                window.raise_()
        self.main.activateWindow()

    def begin_drag(self, window, origin, shift: bool):
        """Start moving one window, or the docked stack, from a title-bar drag."""
        video = window is self.videowin
        if shift and self.docked and not video:
            self.docked = False
        self._drag_origin = QPoint(origin)
        if self.docked and not video:
            windows = [item for item in (self.main, self.eqwin, self.plwin) if item.isVisible()]
        else:
            windows = [window]
        self._drag_pos = {item: QPoint(item.pos()) for item in windows}

    def drag_to(self, pos):
        """Move the dragged window, and any windows docked to it."""
        if self._drag_origin is None:
            return
        delta = pos - self._drag_origin
        for window, origin in self._drag_pos.items():
            window.move(origin + delta)

    def end_drag(self):
        """Finish a title-bar drag and save the new positions."""
        self._drag_origin = None
        self._drag_pos = {}
        self.save_soon()

    def _push_audio(self):
        """Send volume, mute, balance, and the equalizer to the player."""
        self.player.set_volume(self.volume)
        self.player.set_mute(self.muted)
        self.player.set_balance(self.balance)
        self.player.set_eq(self.eq_enabled, self.eq_preamp, self.eq_bands)
        self.player.apply_audio_now()

    def _on_playlist_change(self):
        """Refresh the playlist window after the rows change."""
        self.plwin.sync()
        self.main.sync()
        self.save_soon()

    def _on_position(self, seconds: float):
        """Store the playback position and refresh the main window."""
        self.position = seconds
        if not self.main.seek._dragging:
            self.main.sync()

    def _on_duration(self, seconds: float):
        """Store the loaded file's length and refresh the main window."""
        self.duration = max(0.0, float(seconds))
        self.main.sync()

    def _on_state(self, state: str):
        """Store play, pause, or stop and refresh the main window."""
        self.transport = state
        if state == "stopped":
            self.position = 0.0
        self.main.sync()

    def _on_title(self, title: str):
        """Show the current name, and keep a station name on its playlist row."""
        track = self.playlist.current_track
        if track is not None and "://" not in track.path:
            # Playlist names stay "Artist - Title" or the file stem. mpv's
            # media-title for an untagged file is the filename with its extension.
            self.title = track.name
        elif title:
            self.title = title
            if track is not None and self.playlist.note_stream_title(track.path, title):
                self.plwin.sync()
        self.main.sync()

    def _on_info(self, kbps: int, hz: int, channels: int):
        """Store bitrate, sample rate, and channel count for the display."""
        self.kbps = int(kbps)
        self.hz = int(hz)
        self.channels = int(channels)
        self.main.update()

    def _on_loaded(self):
        """Mark the file as playing, then open a picture if this stream has one."""
        self._error_skips = 0
        self.transport = "playing"
        self._consider_picture()
        if self._current_is_live():
            self._stream_stable.start()
        else:
            self._stream_retries = 0
        self.main.sync()

    def _consider_picture(self):
        """Open the picture window when a stream has video but no known extension."""
        if self._shut or self.quitting:
            return
        track = self.playlist.current_track
        if track is None or self._video_closed or "://" not in track.path:
            return
        if shows_picture(track.path):
            return
        if self.player.has_video_track():
            self._picture_wait_path = ""
            if self._ensure_video():
                self.player.set_video_enabled(True, hardware=True)
            return
        if self._picture_wait_path != track.path:
            self._picture_wait_path = track.path
            self._picture_timer.start()

    def _current_is_live(self) -> bool:
        """True when the current row is a station or a live channel."""
        track = self.playlist.current_track
        if track is None:
            return False
        return is_live_playback(track.path, self.duration)

    def _stream_held(self):
        """Forget reconnect attempts after the stream has stayed up."""
        self._stream_retries = 0

    def _cancel_stream_retry(self):
        """Stop a pending reconnect."""
        self._stream_retry_timer.stop()
        self._stream_stable.stop()

    def _schedule_stream_retry(self):
        """Reload a dropped live stream, up to three times."""
        if self._stream_retries >= 3:
            self._stream_retries = 0
            self.notify("Station stopped answering")
            self._advance()
            return
        self._stream_retries += 1
        self.notify(f"Reconnecting ({self._stream_retries}/3)")
        self._stream_retry_timer.start(1000 * self._stream_retries)

    def _retry_stream(self):
        """Play the current station again after a drop."""
        if self._shut or self.quitting:
            return
        index = self.playlist.current
        track = self.playlist.current_track
        if index is None or track is None or "://" not in track.path:
            return
        self.play_index(index, push_history=False, reset_errors=False)

    def _on_ended(self, reason: int, text: str):
        """Advance, retry a live stream, or stop after a playback error."""
        self._stream_stable.stop()
        if reason == END_STOP:
            self.transport = "stopped"
            self.position = 0.0
            self.main.sync()
            self.plwin.sync()
            return
        if reason in (END_EOF, END_ERROR) and self._current_is_live():
            if reason == END_ERROR and self._stream_retries == 0:
                track = self.playlist.current_track
                name = track.name if track is not None else "station"
                detail = text or "unknown error"
                self.notify(f"Cannot play {name}: {detail}")
            self._schedule_stream_retry()
            return
        if reason == END_EOF:
            self._advance()
            return
        if reason == END_ERROR:
            self._error_skips += 1
            track = self.playlist.current_track
            name = track.name if track is not None else "track"
            detail = text or "unknown error"
            self.notify(f"Cannot play {name}: {detail}")
            if self._error_skips > 8:
                self.notify("Stopped after repeated errors")
                self.stop()
                return
            self._advance()

    def _on_log(self, line: str):
        """Show a player error. Hook failures are not shown here."""
        lowered = line.lower()
        # The hook's own failure text is not a playback error. A real failure
        # still arrives as an end-of-file event.
        if "ytdl" in lowered or "youtube-dl" in lowered or "yt-dlp" in lowered or "hook command" in lowered:
            return
        if any(word in lowered for word in ("fail", "error", "equalizer")):
            self.notify(line[:160])

    def _on_probed(self, path: str, title: str, artist: str, duration: float):
        """Store a probed title and duration, then refresh the windows."""
        parsed = None if duration < 0 else duration
        if not self.playlist.apply_probe(path, title, artist, parsed):
            return
        track = self.playlist.current_track
        if track is not None and track.path == path and (track.artist or track.title):
            if self.transport != "stopped":
                self.title = track.name
        self._probe_timer.start()

    def _flush_probe_ui(self):
        """Redraw after a batch of probed files and save."""
        self.plwin.sync()
        self.main.sync()
        self.save_soon()

    def notify(self, text: str, seconds: float = 4.5):
        """Show a short message in the main window."""
        self.notice = text
        self.notice_until = time.monotonic() + seconds
        self.main.update()

    def _advance(self):
        """Play the next row, or stop at the end of the list."""
        nxt = self.playlist.next_index(automatic=True)
        if nxt is None:
            self.transport = "stopped"
            self.position = 0.0
            self.main.sync()
            self.plwin.sync()
            return
        same = nxt == self.playlist.current and self.playlist.repeat == "one"
        self.play_index(nxt, push_history=not same, reset_errors=False)

    def play_index(self, index: int, push_history: bool = True, reset_errors: bool = True):
        """Load one playlist row and follow it in the list."""
        if index < 0 or index >= len(self.playlist.tracks):
            return
        self._cancel_stream_retry()
        self._picture_timer.stop()
        self._picture_wait_path = ""
        if push_history:
            self.playlist.mark_history()
        if reset_errors:
            self._error_skips = 0
        self.playlist.set_current(index)
        track = self.playlist.tracks[index]
        if track.path != self._stream_retry_path:
            self._stream_retries = 0
        self._stream_retry_path = track.path
        self.title = track.name
        self.position = 0.0
        self.duration = 0.0
        self.kbps = 0
        self.hz = 0
        self.channels = 0
        self.transport = "playing"
        if track.path != self._video_closed_path:
            self._video_closed = False
            self._file_has_video = False
        self._enable_video_for(track.path)
        self.player.load(track.path, user_agent=track.user_agent, referrer=track.referrer)
        self.plwin.view.follow(index)
        self.main.sync()
        self.plwin.sync()

    def play(self):
        """Resume, restart a recorded file, or start the current row."""
        if self.transport == "paused":
            self.player.set_paused(False)
            self.transport = "playing"
            self.main.sync()
            return
        if self.transport == "playing":
            if self._current_is_live():
                return
            self.player.seek(0)
            self.position = 0.0
            self.main.sync()
            return
        if self.playlist.current is not None:
            self.play_index(self.playlist.current, push_history=False)
            return
        if not self.playlist.tracks:
            self.open_files()
            return
        self.play_index(0, push_history=False)

    def pause(self):
        """Pause or resume the current file."""
        if self.transport == "playing":
            self.player.set_paused(True)
            self.transport = "paused"
        elif self.transport == "paused":
            self.player.set_paused(False)
            self.transport = "playing"
        self.main.sync()

    def pause_or_play(self):
        """Pause when audio is playing, and play otherwise."""
        if self.transport == "playing":
            self.pause()
        else:
            self.play()

    def stop(self):
        """Stop playback and hide the picture."""
        self._cancel_stream_retry()
        self._picture_timer.stop()
        self._picture_wait_path = ""
        self._stream_retries = 0
        self.player.stop()
        self.transport = "stopped"
        self.position = 0.0
        self.kbps = 0
        self.hz = 0
        self.channels = 0
        self._video_hide_timer.stop()
        self.videowin.hide()
        self.main.sync()
        self.plwin.sync()

    def next_track(self):
        """Play the next row."""
        if not self.playlist.tracks:
            return
        nxt = self.playlist.next_index(automatic=False)
        if nxt is None:
            self.stop()
            return
        self.play_index(nxt, push_history=True)

    def prev_track(self):
        """Play the previous row, or restart a recorded file."""
        if not self.playlist.tracks:
            return
        if (
            not self._current_is_live()
            and self.transport in ("playing", "paused")
            and self.position > 3
        ):
            self.player.seek(0)
            self.position = 0.0
            self.main.sync()
            return
        index = self.playlist.previous_index()
        if index is None:
            return
        self.play_index(index, push_history=False)

    def seek_fraction(self, fraction: float):
        """Jump to a fraction of the current file."""
        if self.duration <= 0 or self.transport == "stopped":
            return
        fraction = min(1.0, max(0.0, float(fraction)))
        self.position = self.duration * fraction
        self.player.seek(self.position)
        self.main.sync()

    def seek_by(self, delta: float):
        """Jump forward or back by a number of seconds."""
        if self.transport == "stopped" or self.duration <= 0:
            return
        self.player.seek_relative(delta)

    def set_volume(self, volume: int, from_slider: bool = False):
        """Set the volume and save it."""
        self.volume = max(0, min(100, int(volume)))
        if self.volume > 0 and self.muted:
            self.muted = False
            self.player.set_mute(False)
        self.player.set_volume(self.volume)
        if from_slider:
            self.main.update()
        else:
            self.main.sync()
        self.save_soon()

    def nudge_volume(self, delta: int):
        """Change the volume by a small step."""
        self.set_volume(self.volume + delta)

    def toggle_mute(self):
        """Mute or unmute."""
        self.muted = not self.muted
        self.player.set_mute(self.muted)
        self.main.sync()
        self.save_soon()

    def set_balance(self, value: float, from_slider: bool = False):
        """Set the stereo balance and save it."""
        self.balance = max(-1.0, min(1.0, float(value)))
        self.player.set_balance(self.balance)
        if not from_slider:
            self.main.sync()
        self.save_soon()

    def center_balance(self):
        """Put the balance back in the center."""
        self.set_balance(0.0)

    def toggle_remaining(self):
        """Switch the time display between elapsed and remaining."""
        self.remaining = not self.remaining
        self.main.update()
        self.save_soon()

    def cycle_vis(self):
        """Switch the analyzer between bars, scope, and off."""
        order = ("bars", "scope", "off")
        self.vis_mode = order[(order.index(self.vis_mode) + 1) % len(order)]
        self.main.update()
        self.save_soon()

    def toggle_shuffle(self):
        """Turn shuffle on or off."""
        self.playlist.set_shuffle(not self.playlist.shuffle)
        self.main.sync()

    def cycle_repeat(self):
        """Cycle repeat through off, the whole list, and one track."""
        self.playlist.cycle_repeat()
        self.main.sync()

    def toggle_shade(self):
        """Roll the main window up or down."""
        self.shaded = not self.shaded
        self.main.apply_shade()
        if self.docked:
            self.reflow()
        self.save_soon()

    def toggle_eq_shade(self):
        """Roll the equalizer up or down."""
        self.eq_shaded = not self.eq_shaded
        self.eqwin.apply_shade()
        if self.docked:
            self.reflow()
        self.save_soon()

    def toggle_playlist_shade(self):
        """Roll the playlist window up or down."""
        self.playlist_shaded = not self.playlist_shaded
        self.plwin.apply_shade()
        if self.docked:
            self.reflow()
        self.save_soon()

    def toggle_video_shade(self):
        """Roll the video window up or down."""
        self.video_shaded = not self.video_shaded
        self.videowin.apply_shade()
        self.save_soon()

    def hide_video(self):
        """Close the picture and stop decoding it. The sound keeps playing."""
        track = self.playlist.current_track
        self._video_closed = True
        self._video_closed_path = track.path if track is not None else ""
        self._video_hide_timer.stop()
        self.player.set_video_enabled(False)
        self.videowin.hide()
        self.save_soon()

    def show_video(self):
        """Open the picture again for the file that is playing."""
        if not self._ensure_video():
            self.notify("Video output is unavailable. The sound will still play.")
            return
        self._video_closed = False
        self.player.set_video_enabled(True, hardware=True)
        if self._file_has_video:
            self.videowin.present()

    def _ensure_video(self) -> bool:
        """Start the OpenGL picture output once."""
        if self._video_ready:
            return True
        try:
            self._video_ready = bool(self.videowin.warm_render())
        except Exception:
            log.exception("video output failed to start")
            self._video_ready = False
        if not self._video_ready:
            log.warning("video output is unavailable; files will play as audio")
        return self._video_ready

    def _enable_video_for(self, path: str):
        # A plain station stays on audio. A video file, including one served
        # over HTTP, and an HLS or DASH stream attach the picture output first.
        # The window opens only when a picture actually arrives.
        """Attach the picture output for a video file or an IPTV stream."""
        wants_picture = shows_picture(path)
        if wants_picture and not self._video_closed:
            self._ensure_video()
        self.player.set_video_enabled(not self._video_closed, hardware=wants_picture)

    def toggle_video_window(self):
        """Show or close the picture."""
        if self.videowin.isVisible():
            self.hide_video()
        else:
            self.show_video()

    def _on_video(self, present: bool):
        """Open the picture window when a frame arrives."""
        if present:
            self._video_hide_timer.stop()
            self._file_has_video = True
            if not self._video_closed:
                self.videowin.present()
            return
        if self._video_closed:
            return
        self._video_hide_timer.start()

    def _video_gone(self):
        """Hide the picture window after the frame has gone."""
        if self._video_closed:
            return
        self._file_has_video = False
        self.videowin.hide()

    def toggle_playlist_window(self):
        """Show or hide the playlist."""
        self.set_playlist_visible(not self.show_playlist)

    def toggle_eq_window(self):
        """Show or hide the equalizer."""
        self.set_eq_visible(not self.show_eq)

    def set_playlist_visible(self, visible: bool):
        """Show or hide the playlist and remember the choice."""
        self.show_playlist = bool(visible)
        self.plwin.setVisible(self.show_playlist)
        if self.show_playlist:
            self.plwin.raise_()
        if self.docked:
            self.reflow()
        self.main.sync()
        self.save_soon()

    def set_eq_visible(self, visible: bool):
        """Show or hide the equalizer and remember the choice."""
        self.show_eq = bool(visible)
        self.eqwin.setVisible(self.show_eq)
        if self.show_eq:
            self.eqwin.raise_()
        if self.docked:
            self.reflow()
        self.main.sync()
        self.save_soon()

    def set_scale(self, scale: int):
        """Resize the windows to small, medium, or large."""
        scale = int(scale)
        if scale == self.metrics.s:
            return
        anchor = self.main.pos()
        old = self.metrics
        rows = (self.plwin.open_height() - old.title_h - old.bottom_h) / max(1, old.row_h)
        self.metrics = Metrics(scale)
        self.main.apply_metrics()
        self.eqwin.apply_metrics()
        self.plwin.apply_metrics(resize_height=False)
        height = int(self.metrics.title_h + self.metrics.bottom_h + self.metrics.row_h * max(3.0, rows))
        self.plwin.set_open_height(height)
        self.main.move(anchor)
        self.videowin.apply_metrics()
        if self.docked:
            self.reflow()
        self.save_soon()

    def set_on_top(self, on: bool):
        """Keep the windows above other programs, or let them sit normally."""
        self.on_top = bool(on)
        self._apply_on_top()
        self.save_soon()

    def set_docked(self, on: bool):
        """Stick the windows together, or let each one move on its own."""
        self.docked = bool(on)
        if self.docked:
            self.reflow()
        self.save_soon()

    def toggle_eq_enabled(self):
        """Turn the equalizer on or off."""
        self.eq_enabled = not self.eq_enabled
        self.player.set_eq(self.eq_enabled, self.eq_preamp, self.eq_bands)
        self.eqwin.sync()
        self.save_soon()

    def set_preamp(self, value: float):
        """Set the equalizer preamp and mark the preset as custom."""
        self.eq_preamp = float(value)
        self.eq_preset = "Custom"
        self.player.set_eq(self.eq_enabled, self.eq_preamp, self.eq_bands)
        self.eqwin.sync()
        self.save_soon()

    def set_band(self, index: int, value: float):
        """Set one equalizer band and mark the preset as custom."""
        bands = list(self.eq_bands)
        bands[index] = float(value)
        self.eq_bands = bands
        self.eq_preset = "Custom"
        self.player.set_eq(self.eq_enabled, self.eq_preamp, self.eq_bands)
        self.eqwin.sync()
        self.save_soon()

    def apply_preset(self, name: str):
        """Load a named equalizer preset and turn the equalizer on."""
        gains = PRESETS.get(name)
        if gains is None:
            return
        self.eq_bands = [float(value) for value in gains]
        self.eq_preamp = 0.0
        self.eq_preset = name
        self.eq_enabled = True
        self.player.set_eq(True, self.eq_preamp, self.eq_bands)
        self.eqwin.sync()
        self.save_soon()
        self.save_soon()

    def add_paths(self, raw_paths, insert_at=None, play=None):
        """Add files, folders, or addresses to the playlist."""
        entries = expand_entries(list(raw_paths))
        if not entries:
            self.notify("Nothing playable was found")
            return
        was_empty = not self.playlist.tracks
        if insert_at is None:
            index = len(self.playlist.tracks)
        else:
            index = max(0, min(int(insert_at), len(self.playlist.tracks)))
        self.playlist.insert(index, entries)
        self.prober.enqueue(entry.path for entry in entries)
        if play is None:
            play = was_empty
        if play:
            self.play_index(index)
        else:
            self.plwin.sync()
        self.save_soon()

    def remove_tracks(self, tracks):
        """Remove rows, and keep playing when the current row remains."""
        if not tracks:
            return
        active = self.transport in ("playing", "paused")
        removed_current = self.playlist.remove(list(tracks))
        self.plwin.view.prune_selection()
        if removed_current:
            if active and self.playlist.current is not None:
                self.play_index(self.playlist.current, push_history=False)
            else:
                self.stop()
        self.plwin.sync()
        self.save_soon()

    def crop(self):
        """Remove every row that is not selected."""
        chosen = {id(track) for track in self.plwin.view.selected_tracks()}
        if not chosen:
            self.notify("Select the tracks you want to keep")
            return
        gone = [track for track in self.playlist.tracks if id(track) not in chosen]
        self.remove_tracks(gone)

    def move_tracks(self, moving, dest: int):
        """Reorder the selected rows."""
        if not moving:
            return
        idset = {id(track) for track in moving}
        tracks = self.playlist.tracks
        ordered = [track for track in tracks if id(track) in idset]
        dest = max(0, min(int(dest), len(tracks)))
        target = None
        for track in tracks[dest:]:
            if id(track) not in idset:
                target = track
                break
        remaining = [track for track in tracks if id(track) not in idset]
        insert_at = len(remaining) if target is None else remaining.index(target)
        for offset, track in enumerate(ordered):
            remaining.insert(insert_at + offset, track)
        if len(remaining) == len(tracks) and all(a is b for a, b in zip(remaining, tracks)):
            return
        self.playlist.reorder(remaining)
        self.plwin.sync()
        self.save_soon()

    def sort_playlist(self, how: str):
        """Sort the playlist by the chosen field."""
        self.playlist.sort(how)
        self.plwin.sync()

    def reverse_playlist(self):
        """Reverse the playlist order."""
        self.playlist.reverse()
        self.plwin.sync()

    def randomize_playlist(self):
        """Shuffle the playlist order."""
        self.playlist.randomize()
        self.plwin.sync()

    def clear_playlist(self):
        """Ask, then remove every row and stop."""
        if not self.playlist.tracks:
            return
        answer = QMessageBox.question(
            self.main,
            "Clear playlist",
            "Remove every track from the playlist?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.stop()
        self.playlist.clear()
        self.plwin.view.selected.clear()
        self.plwin.view.cursor = None
        self.title = ""
        self.plwin.sync()
        self.main.sync()
        self.save_soon()

    def open_files(self):
        """Ask for files and add them."""
        paths, _selected = QFileDialog.getOpenFileNames(
            self.main, "Add files", self.last_dir, media_filter()
        )
        if not paths:
            return
        self.last_dir = str(Path(paths[0]).parent)
        self.add_paths(paths)

    def open_directory(self):
        """Ask for a folder and add the media inside it."""
        path = QFileDialog.getExistingDirectory(self.main, "Add folder", self.last_dir)
        if not path:
            return
        self.last_dir = path
        self.add_paths([path])

    def open_url(self):
        """Ask for an http or https address and add it."""
        text, ok = QInputDialog.getText(self.main, "Add URL", "Station or file URL:")
        if not ok or not text.strip():
            return
        raw = text.strip()
        if not is_stream_url(raw):
            self.notify("Paste a link that starts with http:// or https://")
            return
        self.add_paths([raw])

    def load_playlist_file(self):
        """Ask for a playlist file and add its entries."""
        path, _selected = QFileDialog.getOpenFileName(
            self.main,
            "Load playlist",
            self.last_dir,
            playlist_open_filter() + ";;All files (*)",
        )
        if path:
            self.last_dir = str(Path(path).parent)
            self.add_paths([path])

    def save_playlist_as(self):
        """Write the playlist as M3U, PLS, XSPF, or Audacious."""
        if not self.playlist.tracks:
            self.notify("Playlist is empty")
            return
        suggested = str(Path(self.last_dir) / "playlist.m3u")
        path, selected = QFileDialog.getSaveFileName(
            self.main, "Save playlist", suggested, playlist_save_filter()
        )
        if not path:
            return
        path = with_playlist_suffix(path, selected)
        try:
            written = playlist_text(self.playlist.tracks, Path(path).suffix)
            Path(path).write_text(written, encoding="utf-8")
        except OSError as exc:
            self.notify(f"Could not save playlist: {exc}")
            return
        self.last_dir = str(Path(path).parent)
        self.notify("Playlist saved", seconds=2.0)

    def replace_with_files(self):
        """Replace the playlist with files chosen in a dialog."""
        paths, _selected = QFileDialog.getOpenFileNames(
            self.main, "Replace playlist", self.last_dir, media_filter()
        )
        if not paths:
            return
        self.last_dir = str(Path(paths[0]).parent)
        self.stop()
        self.playlist.clear()
        self.plwin.view.selected.clear()
        self.add_paths(paths, play=True)

    def popup_menu(self, global_pos):
        """Open the main menu at a screen position."""
        menu = self._build_menu()
        menu.exec(global_pos)

    def playlist_menu(self, global_pos):
        """Open the playlist menu at a screen position."""
        menu = self._build_menu()
        menu.exec(global_pos)

    def _build_menu(self) -> QMenu:
        """Build the main menu."""
        menu = QMenu(self.main)
        menu.addAction("Add files\tCtrl+O", self.open_files)
        menu.addAction("Add folder\tCtrl+D", self.open_directory)
        menu.addAction("Add URL…", self.open_url)
        menu.addAction("Load playlist…", self.load_playlist_file)
        menu.addAction("Save playlist\tCtrl+S", self.save_playlist_as)
        menu.addAction("Replace playlist…", self.replace_with_files)
        menu.addSeparator()
        menu.addAction("Play\tX", self.play)
        menu.addAction("Pause\tC", self.pause)
        menu.addAction("Stop\tV", self.stop)
        menu.addAction("Next\tB", self.next_track)
        menu.addAction("Previous\tZ", self.prev_track)
        menu.addSeparator()
        shuffle = menu.addAction("Shuffle\tS", self.toggle_shuffle)
        shuffle.setCheckable(True)
        shuffle.setChecked(self.playlist.shuffle)
        repeat = menu.addMenu("Repeat")
        for mode, label in (("off", "Off"), ("all", "Repeat playlist"), ("one", "Repeat track")):
            action = repeat.addAction(label, lambda checked=False, mode=mode: self.playlist.set_repeat(mode))
            action.setCheckable(True)
            action.setChecked(self.playlist.repeat == mode)
        shown_pl = menu.addAction("Show playlist\tL", self.toggle_playlist_window)
        shown_pl.setCheckable(True)
        shown_pl.setChecked(self.show_playlist)
        shown_eq = menu.addAction("Show equalizer\tE", self.toggle_eq_window)
        shown_eq.setCheckable(True)
        shown_eq.setChecked(self.show_eq)
        shown_video = menu.addAction("Show video", self.toggle_video_window)
        shown_video.setCheckable(True)
        shown_video.setChecked(self.videowin.isVisible())
        shown_video.setEnabled(self._file_has_video or self.videowin.isVisible())
        menu.addSeparator()
        sort = menu.addMenu("Sort")
        sort.addAction("By title", lambda: self.sort_playlist("title"))
        sort.addAction("By filename", lambda: self.sort_playlist("filename"))
        sort.addAction("By path", lambda: self.sort_playlist("path"))
        sort.addAction("Reverse", self.reverse_playlist)
        sort.addAction("Randomize order", self.randomize_playlist)
        menu.addAction("Remove selected", lambda: self.remove_tracks(self.plwin.view.selected_tracks()))
        menu.addAction("Crop to selection", self.crop)
        menu.addAction("Clear playlist", self.clear_playlist)
        menu.addSeparator()
        scale = menu.addMenu("Window scale")
        for value, label in ((1, "Classic"), (2, "Medium"), (3, "Large")):
            action = scale.addAction(label, lambda checked=False, value=value: self.set_scale(value))
            action.setCheckable(True)
            action.setChecked(self.metrics.s == value)
        vis = menu.addMenu("Display")
        for mode, label in (("bars", "Bars"), ("scope", "Scope"), ("off", "Off")):
            action = vis.addAction(label, lambda checked=False, mode=mode: self._set_vis(mode))
            action.setCheckable(True)
            action.setChecked(self.vis_mode == mode)
        dock = menu.addAction("Snap windows together", lambda: self.set_docked(not self.docked))
        dock.setCheckable(True)
        dock.setChecked(self.docked)
        top = menu.addAction("Always on top", lambda: self.set_on_top(not self.on_top))
        top.setCheckable(True)
        top.setChecked(self.on_top)
        menu.addSeparator()
        menu.addAction(f"About {APP_NAME}", self.about)
        menu.addAction("Quit\tCtrl+Q", self.quit)
        return menu

    def _set_vis(self, mode: str):
        """Choose the analyzer mode and save it."""
        self.vis_mode = mode
        self.main.update()
        self.save_soon()

    def about(self):
        """Show the version, the formats, and the license."""
        QMessageBox.about(
            self.main,
            f"About {APP_NAME}",
            f"{APP_NAME}\n"
            f"Version {__version__}\n\n"
            "A classic-style music player for Linux.\n"
            "Playback is provided by libmpv, so it plays the formats mpv plays: "
            "MP3, Ogg, FLAC, Opus, WAV, M4A, MP4, MKV, and more.\n"
            "A video file opens its own window. Closing that window leaves the sound playing.\n\n"
            "This is a new program in the spirit of Winamp 2. "
            "It is not the 2007 GTK 1 player.\n\n"
            "Released under the GNU GPL, version 2 or later.\n\n"
            "Z previous   X play   C pause   V stop   B next\n"
            "Left / Right seek    Up / Down volume\n"
            "S shuffle   R repeat   L playlist   E equalizer",
        )

    def eventFilter(self, obj, event):
        """Handle keys and window minimize for the whole application."""
        if event.type() == QEvent.Type.WindowStateChange:
            self._sync_minimized(obj, event)
        if event.type() != QEvent.Type.KeyPress:
            return False
        if QApplication.activeModalWidget() is not None or QApplication.activePopupWidget() is not None:
            return False
        key = event.key()
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        focused = QApplication.focusWidget() is self.plwin.view
        if ctrl:
            if key == Qt.Key.Key_O:
                self.open_files()
                return True
            if key == Qt.Key.Key_D:
                self.open_directory()
                return True
            if key == Qt.Key.Key_S:
                self.save_playlist_as()
                return True
            if key == Qt.Key.Key_Q:
                self.quit()
                return True
            return False
        navigation = {
            Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Home, Qt.Key.Key_End,
            Qt.Key.Key_PageUp, Qt.Key.Key_PageDown, Qt.Key.Key_Delete,
            Qt.Key.Key_Backspace, Qt.Key.Key_Return, Qt.Key.Key_Enter,
        }
        if focused and key in navigation:
            return False
        if event.isAutoRepeat() and key in {
            Qt.Key.Key_X, Qt.Key.Key_C, Qt.Key.Key_V, Qt.Key.Key_B, Qt.Key.Key_Z,
            Qt.Key.Key_S, Qt.Key.Key_R, Qt.Key.Key_L, Qt.Key.Key_E, Qt.Key.Key_Space,
        }:
            return True
        actions = {
            Qt.Key.Key_X: self.play,
            Qt.Key.Key_C: self.pause,
            Qt.Key.Key_V: self.stop,
            Qt.Key.Key_B: self.next_track,
            Qt.Key.Key_Z: self.prev_track,
            Qt.Key.Key_S: self.toggle_shuffle,
            Qt.Key.Key_R: self.cycle_repeat,
            Qt.Key.Key_L: self.toggle_playlist_window,
            Qt.Key.Key_E: self.toggle_eq_window,
            Qt.Key.Key_Space: self.pause,
        }
        if key in actions:
            actions[key]()
            return True
        if key == Qt.Key.Key_Left:
            self.seek_by(-30 if shift else -5)
            return True
        if key == Qt.Key.Key_Right:
            self.seek_by(30 if shift else 5)
            return True
        if key == Qt.Key.Key_Up:
            self.nudge_volume(5)
            return True
        if key == Qt.Key.Key_Down:
            self.nudge_volume(-5)
            return True
        return False

    def _sync_minimized(self, window, event):
        """Iconify and restore the equalizer and playlist with the main window.

        The title-bar button calls showMinimized on the main window only. The
        other two are separate top-level windows, so they would otherwise stay
        on screen. Follow the main window, and leave a window alone when its
        own close button has hidden it.
        """
        if self._minimizing or self.quitting or self._shut or window is not self.main:
            return
        if not window.isVisible():
            return
        old_min = bool(event.oldState() & Qt.WindowState.WindowMinimized)
        new_min = bool(window.windowState() & Qt.WindowState.WindowMinimized)
        if old_min == new_min:
            return
        # Apply on the next turn. KWin drops a second showMinimized issued
        # inside this state-change event.
        self._minimizing = True

        def apply():
            """Minimize or restore the other windows on the next turn."""
            try:
                if self.quitting or self._shut:
                    return
                for other in (self.eqwin, self.plwin, self.videowin):
                    if not other.isVisible():
                        continue
                    if new_min:
                        if not other.isMinimized():
                            other.showMinimized()
                    elif other.isMinimized():
                        other.showNormal()
                if not new_min and self.main.isVisible() and not self.main.isMinimized():
                    self.main.raise_()
                    self.main.activateWindow()
            finally:
                self._minimizing = False

        QTimer.singleShot(0, apply)

    def save_soon(self):
        """Write settings after a short pause so a burst of changes is one write."""
        if self._shut or self.quitting:
            return
        self._save_timer.start()

    def save_state(self):
        """Write volume, playlist, and window positions to state.json."""
        if self._shut:
            return
        payload = {
            "version": 1,
            "volume": self.volume,
            "balance": self.balance,
            "muted": self.muted,
            "shuffle": self.playlist.shuffle,
            "repeat": self.playlist.repeat,
            "remaining": self.remaining,
            "vis": self.vis_mode,
            "scale": self.metrics.s,
            "shaded": self.shaded,
            "eq_shaded": self.eq_shaded,
            "playlist_shaded": self.playlist_shaded,
            "eq_on": self.eq_enabled,
            "eq_preamp": self.eq_preamp,
            "eq_bands": self.eq_bands,
            "eq_preset": self.eq_preset,
            "show_playlist": self.show_playlist,
            "show_eq": self.show_eq,
            "video_shaded": self.video_shaded,
            "docked": self.docked,
            "on_top": self.on_top,
            "last_dir": self.last_dir,
            "tracks": [track_record(track) for track in self.playlist.tracks],
            "current": self.playlist.current,
            "geometry": {
                "main": [self.main.x(), self.main.y()],
                "eq": [self.eqwin.x(), self.eqwin.y()],
                "playlist": [self.plwin.x(), self.plwin.y(), self.plwin.open_height()],
            },
        }
        video_geometry = self.videowin.geometry_record()
        if video_geometry is not None:
            payload["video"] = video_geometry
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.config_path.with_suffix(".json.tmp")
            # 0o600: the file holds stream addresses, and some of those carry tokens.
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, indent=2) + "\n")
            os.chmod(temporary, 0o600)
            temporary.replace(self.config_path)
        except OSError as exc:
            log.warning("could not save state: %s", exc)

    def shutdown(self):
        """Save, stop the prober, and release the player."""
        if self._shut:
            return
        self._shut = True
        self._save_timer.stop()
        self._cancel_stream_retry()
        try:
            self.save_state()
        except Exception:
            log.exception("save on quit failed")
        try:
            self.prober.stop()
            self.prober.wait(1500)
        except Exception:
            log.exception("prober shutdown failed")
        try:
            self.videowin.release_render()
        except Exception:
            log.exception("video shutdown failed")
        self.player.shutdown()

    def quit(self):
        """Shut down and leave the application."""
        if self.quitting:
            return
        self.quitting = True
        self.shutdown()
        app = QApplication.instance()
        if app is not None:
            app.quit()


def _clamp_int(value, low, high, default):
    """Return an int inside the range, or the default when the value is not an int."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _clamp_float(value, low, high, default):
    """Return a float inside the range, or the default when the value is not a float."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _launcher_path() -> Path:
    # The package directory is already named pyxmms, so the command script lives in bin/.
    """Path of the bin/pyxmms launch script."""
    return ROOT / "bin" / LAUNCHER_NAME


def _desktop_text() -> str:
    """Menu entry that points at this checkout's launcher and icon."""
    launcher = _launcher_path()
    icon = ROOT / "assets" / "pyxmms.png"
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        "GenericName=Music Player\n"
        f"Comment={APP_NAME}, a classic-style music player\n"
        f"Exec={launcher}\n"
        f"Icon={icon}\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Audio;Player;\n"
        "StartupNotify=true\n"
        f"StartupWMClass={DESKTOP_ID}\n"
    )


def _remove_installed(path: Path, label: str):
    """Delete an old launcher or menu file, and log the reason if it stays."""
    try:
        path.unlink()
    except OSError as exc:
        log.warning("could not remove %s: %s", label, exc)


def _retire_previous_names(desktop_dir: Path, local_bin: Path):
    """Drop earlier launcher names so the menu and PATH keep one entry."""
    for name in ("pyxmms-clone", "xmms-clone"):
        old_desktop = desktop_dir / f"{name}.desktop"
        if old_desktop.is_file():
            try:
                text = old_desktop.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            # Match this name on its own. "xmms-clone" also sits inside "pyxmms-clone".
            owned = f"StartupWMClass={name}" in text or f"/{name}" in text
            if name == "xmms-clone" and "XMMS-clone" in text:
                owned = True
            if owned:
                _remove_installed(old_desktop, "previous menu entry")
        old_link = local_bin / name
        if not old_link.is_symlink():
            continue
        try:
            raw = os.readlink(old_link)
        except OSError:
            continue
        if raw.endswith(f"/{name}") or (name == "xmms-clone" and "XMMS-clone" in raw):
            _remove_installed(old_link, "previous launcher")
    stale_desktop = ROOT / "pyxmms-clone.desktop"
    if stale_desktop.is_file():
        _remove_installed(stale_desktop, "previous desktop file")


def ensure_assets():
    """Install the menu entry and the launcher link for this user."""
    icon_path = ROOT / "assets" / "pyxmms.png"
    icon_path.parent.mkdir(parents=True, exist_ok=True)
    if not icon_path.exists():
        legacy_icon = ROOT / "assets" / "xmms.png"
        if legacy_icon.is_file():
            legacy_icon.replace(icon_path)
        else:
            make_icon_pixmap(128).save(str(icon_path), "PNG")
    text = _desktop_text()
    destination = Path.home() / ".local" / "share" / "applications" / "pyxmms.desktop"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    local_bin = Path.home() / ".local" / "bin"
    local_bin.mkdir(parents=True, exist_ok=True)
    _retire_previous_names(destination.parent, local_bin)
    link = local_bin / LAUNCHER_NAME
    target = _launcher_path()
    try:
        if link.is_symlink() and link.resolve() == target.resolve():
            return
        if link.exists():
            return
        link.symlink_to(target)
    except OSError as exc:
        log.warning("could not link launcher: %s", exc)


def prepare_app(app: QApplication):
    """Set the application name, the dark palette, and the tooltip style."""
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName(DESKTOP_ID)
    app.setDesktopFileName(DESKTOP_ID)
    app.setStyle("Fusion")
    palette = app.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#2c2c2c"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#e8e8e8"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#161616"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#e8e8e8"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#3a3a3a"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#f0f0f0"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#5A2D86"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#F6E8FF"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#100818"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#F3E6FF"))
    app.setPalette(palette)
    app.setStyleSheet(
        "QToolTip { color: #F3E6FF; background-color: #100818; "
        "border: 1px solid #5A2D86; padding: 2px; }"
    )


ALREADY_RUNNING = f"{APP_NAME} is already running."
_instance_lock_files: list = []


def migrate_config():
    """Copy saved settings from an earlier config folder, once."""
    if CONFIG_PATH.parent != CONFIG_DIR or CONFIG_PATH.exists():
        return
    for legacy_dir in LEGACY_CONFIG_DIRS:
        legacy = legacy_dir / "state.json"
        if not legacy.is_file():
            continue
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_bytes(legacy.read_bytes())
        except OSError as exc:
            log.warning("could not copy previous settings: %s", exc)
        return


def acquire_single_instance(path: Path | None = None) -> bool:
    """Hold the per-user instance lock. False means another PyXMMS already has it.

    The lock is advisory and tied to this open file. It drops when the process
    exits, including after a crash, so a stale lock cannot block the next launch.
    Earlier config folders are locked too while they still exist, so a process
    started under an older folder name still counts as running.
    """
    if _instance_lock_files:
        return True
    primary = path if path is not None else CONFIG_PATH.parent / "instance.lock"
    targets = [primary]
    if path is None and primary.parent == CONFIG_DIR:
        for legacy_dir in LEGACY_CONFIG_DIRS:
            if not legacy_dir.is_dir():
                continue
            legacy = legacy_dir / "instance.lock"
            if legacy != primary:
                targets.append(legacy)
    handles = []
    for index, target in enumerate(targets):
        try:
            if index == 0:
                target.parent.mkdir(parents=True, exist_ok=True)
            handle = target.open("a+")
        except OSError as exc:
            log.warning("could not open instance lock: %s", exc)
            continue
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                for held in handles:
                    held.close()
                return False
            log.warning("could not lock instance: %s", exc)
            continue
        handles.append(handle)
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n")
            handle.flush()
        except OSError:
            pass
    if not handles:
        return True
    _instance_lock_files.extend(handles)
    return True


def main(argv=None) -> int:
    """Parse arguments, take the single-instance lock, and run the windows."""
    argv = list(sys.argv if argv is None else argv)
    if any(arg in ("-h", "--help") for arg in argv[1:]):
        print(HELP)
        return 0
    if any(arg.startswith("-") and arg != "--debug" for arg in argv[1:]):
        print("Unknown option. Try --help.")
        return 2
    debug = "--debug" in argv
    paths = [arg for arg in argv[1:] if arg != "--debug"]
    ensure_c_numeric()
    logging.basicConfig(level=logging.DEBUG if debug else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    app = QApplication(argv)
    ensure_c_numeric()
    prepare_app(app)
    if not acquire_single_instance():
        print(ALREADY_RUNNING, file=sys.stderr)
        QMessageBox.critical(None, APP_NAME, ALREADY_RUNNING)
        return 1
    ensure_assets()
    signal.signal(signal.SIGINT, lambda *_args: app.quit())
    try:
        controller = Controller(paths)
    except Exception as exc:
        log.exception("startup failed")
        QMessageBox.critical(None, APP_NAME, str(exc))
        return 1
    app.aboutToQuit.connect(controller.shutdown)
    ticker = QTimer()
    ticker.start(250)
    ticker.timeout.connect(lambda: None)
    controller.ticker = ticker
    return app.exec()
