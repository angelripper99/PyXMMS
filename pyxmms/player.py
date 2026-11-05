"""Thin libmpv wrapper.

libmpv's client API is stable C. This module speaks to libmpv.so.2 directly
so the player has no Python binding to install. The numeric event and format
values match mpv 0.41 (client API 2.5), which is what this system ships.

Copyright (C) 2026 the PyXMMS authors. Released under GPL-2.0-or-later. See LICENSE.
"""

from __future__ import annotations

import ctypes
import locale
import logging
import os

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from . import APP_NAME
from .audiofx import build_af
from .playlist import STREAM_USER_AGENT, needs_ytdl, stream_headers

log = logging.getLogger("pyxmms")


def ensure_c_numeric() -> None:
    """Keep LC_NUMERIC at C so libmpv will start and parse filter numbers.

    Qt applies the process environment with setlocale(LC_ALL, "") when the
    application starts. LC_ALL overrides LC_NUMERIC, so drop LC_ALL and pin
    the numeric category before that happens, and again before mpv_create.
    """
    lc_all = os.environ.get("LC_ALL")
    if lc_all:
        os.environ.pop("LC_ALL", None)
        for key in (
            "LC_CTYPE", "LC_TIME", "LC_COLLATE", "LC_MONETARY", "LC_MESSAGES",
            "LC_PAPER", "LC_NAME", "LC_ADDRESS", "LC_TELEPHONE",
            "LC_MEASUREMENT", "LC_IDENTIFICATION",
        ):
            os.environ.setdefault(key, lc_all)
    os.environ["LC_NUMERIC"] = "C"
    try:
        locale.setlocale(locale.LC_NUMERIC, "C")
    except locale.Error:
        pass

MPV_FORMAT_NONE = 0
MPV_FORMAT_STRING = 1
MPV_FORMAT_FLAG = 3
MPV_FORMAT_INT64 = 4
MPV_FORMAT_DOUBLE = 5

END_EOF = 0
END_STOP = 2
END_QUIT = 3
END_ERROR = 4
END_REDIRECT = 5

EVENT_NONE = 0
EVENT_SHUTDOWN = 1
EVENT_LOG = 2
EVENT_END_FILE = 7
EVENT_FILE_LOADED = 8
EVENT_PROPERTY = 22

WAKE_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p)


class MpvEvent(ctypes.Structure):
    """libmpv event header. The numeric fields match the client API."""
    _fields_ = [
        ("event_id", ctypes.c_int),
        ("error", ctypes.c_int),
        ("reply_userdata", ctypes.c_uint64),
        ("data", ctypes.c_void_p),
    ]


class MpvEventProperty(ctypes.Structure):
    """A property-change event from libmpv."""
    _fields_ = [
        ("name", ctypes.c_char_p),
        ("format", ctypes.c_int),
        ("data", ctypes.c_void_p),
    ]


class MpvEventEndFile(ctypes.Structure):
    """The reason a file stopped, including errors and redirects."""
    _fields_ = [
        ("reason", ctypes.c_int),
        ("error", ctypes.c_int),
        ("playlist_entry_id", ctypes.c_int64),
        ("playlist_insert_id", ctypes.c_int64),
        ("playlist_insert_num_entries", ctypes.c_int),
    ]


class MpvEventLog(ctypes.Structure):
    """One log line from libmpv."""
    _fields_ = [
        ("prefix", ctypes.c_char_p),
        ("level", ctypes.c_char_p),
        ("text", ctypes.c_char_p),
        ("log_level", ctypes.c_int),
    ]


# libmpv render API. The numeric values match the client API this player
# already targets (mpv 0.41). Video is drawn into our window, not mpv's.
MPV_RENDER_PARAM_INVALID = 0
MPV_RENDER_PARAM_API_TYPE = 1
MPV_RENDER_PARAM_OPENGL_INIT_PARAMS = 2
MPV_RENDER_PARAM_OPENGL_FBO = 3
MPV_RENDER_PARAM_FLIP_Y = 4
MPV_RENDER_PARAM_ADVANCED_CONTROL = 10
MPV_RENDER_PARAM_SKIP_RENDERING = 13

OPENGL_PROC = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p)
RENDER_UPDATE = ctypes.CFUNCTYPE(None, ctypes.c_void_p)


class MpvOpenGLInitParams(ctypes.Structure):
    """How libmpv looks up OpenGL functions in our context."""
    _fields_ = [
        ("get_proc_address", OPENGL_PROC),
        ("get_proc_address_ctx", ctypes.c_void_p),
    ]


class MpvOpenGLFBO(ctypes.Structure):
    """The framebuffer libmpv draws a frame into."""
    _fields_ = [
        ("fbo", ctypes.c_int),
        ("w", ctypes.c_int),
        ("h", ctypes.c_int),
        ("internal_format", ctypes.c_int),
    ]


class MpvRenderParam(ctypes.Structure):
    """One name and value pair passed to the render API."""
    _fields_ = [
        ("type", ctypes.c_int),
        ("data", ctypes.c_void_p),
    ]


_lib = None
_render_bound = False


def mpv_library():
    """Load libmpv.so.2 once and set the function signatures we call."""
    global _lib
    if _lib is not None:
        return _lib
    try:
        lib = ctypes.CDLL("libmpv.so.2")
    except OSError as exc:
        raise RuntimeError(
            "libmpv.so.2 was not found. Install the system mpv library and try again."
        ) from exc
    lib.mpv_create.restype = ctypes.c_void_p
    lib.mpv_initialize.argtypes = [ctypes.c_void_p]
    lib.mpv_initialize.restype = ctypes.c_int
    lib.mpv_command.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_char_p)]
    lib.mpv_command.restype = ctypes.c_int
    lib.mpv_set_property_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
    lib.mpv_set_property_string.restype = ctypes.c_int
    lib.mpv_observe_property.argtypes = [
        ctypes.c_void_p, ctypes.c_uint64, ctypes.c_char_p, ctypes.c_int,
    ]
    lib.mpv_observe_property.restype = ctypes.c_int
    lib.mpv_request_log_messages.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.mpv_request_log_messages.restype = ctypes.c_int
    lib.mpv_wait_event.argtypes = [ctypes.c_void_p, ctypes.c_double]
    lib.mpv_wait_event.restype = ctypes.POINTER(MpvEvent)
    lib.mpv_set_wakeup_callback.argtypes = [ctypes.c_void_p, WAKE_CB, ctypes.c_void_p]
    lib.mpv_terminate_destroy.argtypes = [ctypes.c_void_p]
    lib.mpv_error_string.argtypes = [ctypes.c_int]
    lib.mpv_error_string.restype = ctypes.c_char_p
    lib.mpv_get_property_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.mpv_get_property_string.restype = ctypes.c_void_p
    lib.mpv_free.argtypes = [ctypes.c_void_p]
    _lib = lib
    return lib


def _bind_render_api(lib) -> bool:
    """Attach render-context signatures once. False if this libmpv has none."""
    global _render_bound
    if _render_bound:
        return True
    if not hasattr(lib, "mpv_render_context_create"):
        return False
    lib.mpv_render_context_create.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(MpvRenderParam),
    ]
    lib.mpv_render_context_create.restype = ctypes.c_int
    lib.mpv_render_context_set_update_callback.argtypes = [
        ctypes.c_void_p, RENDER_UPDATE, ctypes.c_void_p,
    ]
    lib.mpv_render_context_update.argtypes = [ctypes.c_void_p]
    lib.mpv_render_context_update.restype = ctypes.c_uint64
    lib.mpv_render_context_render.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(MpvRenderParam),
    ]
    lib.mpv_render_context_render.restype = ctypes.c_int
    lib.mpv_render_context_report_swap.argtypes = [ctypes.c_void_p]
    lib.mpv_render_context_free.argtypes = [ctypes.c_void_p]
    _render_bound = True
    return True


def _text(pointer) -> str:
    """Decode a C string from libmpv, or return an empty string."""
    if not pointer:
        return ""
    try:
        return pointer.decode("utf-8", "replace")
    except Exception:
        return ""


class MpvPlayer(QObject):
    """libmpv instance. Signals are delivered on the GUI thread.

    Video is rendered by the caller with an OpenGL context. Until that
    context is attached, `vid` stays off so a picture cannot stall playback.
    """

    position_changed = pyqtSignal(float)
    duration_changed = pyqtSignal(float)
    state_changed = pyqtSignal(str)
    title_changed = pyqtSignal(str)
    info_changed = pyqtSignal(int, int, int)
    loaded = pyqtSignal()
    ended = pyqtSignal(int, str)
    log_line = pyqtSignal(str)
    video_changed = pyqtSignal(bool)
    video_sized = pyqtSignal(int, int)
    frame_ready = pyqtSignal()
    _wake = pyqtSignal()

    def __init__(self):
        """Create libmpv, set the audio and stream options, and start the event pump."""
        super().__init__()
        self.state = "stopped"
        self._ctx = None
        self._closing = False
        self._in_pump = False
        self._has_file = False
        self._replacing = False
        self._token = 0
        self._load_token = 0
        self._balance = 0.0
        self._eq_on = False
        self._preamp = 0.0
        self._bands = [0.0] * 10
        self._af = None
        self._kbps = 0
        self._hz = 0
        self._channels = 0
        self._render = None
        self._render_ready = False
        self._video_on = False
        self._dwidth = 0
        self._dheight = 0
        self._dsize = (0, 0)
        self._last_render_error = 0
        self._in_render = False

        ensure_c_numeric()
        lib = mpv_library()
        ctx = lib.mpv_create()
        if not ctx:
            raise RuntimeError(
                "mpv could not start. LC_NUMERIC must be C, and libmpv has to be usable."
            )
        self._ctx = ctx
        self._lib = lib
        # Keep the callback object alive for the life of the player. mpv only
        # stores the function pointer.
        self._wake_cb = WAKE_CB(self._on_wake)
        self._noop_cb = WAKE_CB(self._on_noop)
        self._render_update_cb = RENDER_UPDATE(self._on_render_update)
        self._render_noop_cb = RENDER_UPDATE(self._on_render_noop)

        for key, value in (
            # libmpv draws into a context we create. vid stays off until that
            # context exists, so a video file cannot block waiting for a frame.
            ("vo", "libmpv"),
            ("vid", "no"),
            ("osd-level", "0"),
            ("terminal", "no"),
            ("idle", "yes"),
            ("force-window", "no"),
            ("audio-display", "no"),
            ("osc", "no"),
            ("input-default-bindings", "no"),
            ("input-vo-keyboard", "no"),
            ("keep-open", "no"),
            ("gapless-audio", "weak"),
            ("volume-max", "100"),
            ("audio-client-name", APP_NAME),
            # Stations often refuse the default "libmpv" agent. Reconnect covers
            # a short network drop. A clean end of a file still ends the track.
            ("user-agent", STREAM_USER_AGENT),
            (
                "stream-lavf-o",
                "reconnect=1,reconnect_streamed=1,reconnect_on_network_error=1,reconnect_delay_max=5",
            ),
            # The youtube-dl hook runs on every network address when it is left
            # on. On a direct IPTV or radio URL yt-dlp fails, and that failure
            # aborts playback. A video page turns the hook back on for that load.
            ("ytdl", "no"),
        ):
            rc = self._set(key, value)
            if rc < 0:
                log.warning("mpv option %s was not set (%s)", key, self._err(rc))

        rc = lib.mpv_initialize(ctx)
        if rc < 0:
            lib.mpv_terminate_destroy(ctx)
            self._ctx = None
            raise RuntimeError(f"mpv failed to initialize ({self._err(rc)}).")

        # Connect before arming the callback. mpv calls it once, then not
        # again until mpv_wait_event runs. An emit with no listener is lost
        # and playback position would never move.
        self._wake.connect(self._pump)
        lib.mpv_set_wakeup_callback(ctx, self._wake_cb, None)
        lib.mpv_request_log_messages(ctx, b"error")

        observed = (
            ("time-pos", MPV_FORMAT_DOUBLE),
            ("duration", MPV_FORMAT_DOUBLE),
            ("pause", MPV_FORMAT_FLAG),
            ("media-title", MPV_FORMAT_STRING),
            ("audio-bitrate", MPV_FORMAT_DOUBLE),
            ("audio-params/samplerate", MPV_FORMAT_INT64),
            ("audio-params/channel-count", MPV_FORMAT_INT64),
            ("vid", MPV_FORMAT_INT64),
            ("dwidth", MPV_FORMAT_INT64),
            ("dheight", MPV_FORMAT_INT64),
        )
        for index, (name, fmt) in enumerate(observed, start=1):
            rc = lib.mpv_observe_property(ctx, index, name.encode("utf-8"), fmt)
            if rc < 0:
                log.warning("mpv will not report %s (%s)", name, self._err(rc))

        self._af_timer = QTimer(self)
        self._af_timer.setSingleShot(True)
        self._af_timer.timeout.connect(self._apply_af)
        # If a wakeup is missed, position and end-of-track still arrive.
        self._poll = QTimer(self)
        self._poll.setInterval(40)
        self._poll.timeout.connect(self._pump)
        self._poll.start()
        self._pump()

    def _err(self, code: int) -> str:
        """Turn a libmpv error code into text."""
        raw = self._lib.mpv_error_string(int(code))
        return _text(raw) or f"error {code}"

    def _set(self, key: str, value: str) -> int:
        """Set one libmpv string property."""
        if self._ctx is None:
            return -1
        key_b = key.encode("utf-8")
        value_b = value.encode("utf-8")
        return int(self._lib.mpv_set_property_string(self._ctx, key_b, value_b))

    def _command(self, *args: str) -> int:
        """Run one libmpv command."""
        if self._ctx is None:
            return -1
        encoded = [arg.encode("utf-8") for arg in args]
        array = (ctypes.c_char_p * (len(encoded) + 1))()
        for index, item in enumerate(encoded):
            array[index] = item
        array[len(encoded)] = None
        return int(self._lib.mpv_command(self._ctx, array))

    def _set_state(self, state: str):
        """Emit play, pause, or stop when the state actually changes."""
        if state == self.state:
            return
        self.state = state
        self.state_changed.emit(state)

    def _on_noop(self, _ctx):
        """Wakeup callback used after the real callback is detached."""
        return

    def _on_wake(self, _ctx):
        """Ask the GUI thread to read libmpv events."""
        try:
            self._wake.emit()
        except RuntimeError:
            return

    def _pump(self):
        """Read every pending libmpv event on the GUI thread."""
        if self._closing or self._ctx is None or self._in_pump:
            return
        self._in_pump = True
        try:
            while self._ctx is not None and not self._closing:
                event = self._lib.mpv_wait_event(self._ctx, 0)
                if not event:
                    break
                body = event.contents
                event_id = int(body.event_id)
                if event_id == EVENT_NONE:
                    break
                self._handle(event_id, body.data)
        finally:
            self._in_pump = False

    def _handle(self, event_id: int, data):
        """Dispatch one libmpv event to the matching handler."""
        if event_id == EVENT_PROPERTY and data:
            prop = ctypes.cast(data, ctypes.POINTER(MpvEventProperty)).contents
            name = _text(prop.name)
            self._on_property(name, int(prop.format), prop.data)
            return
        if event_id == EVENT_END_FILE and data:
            end = ctypes.cast(data, ctypes.POINTER(MpvEventEndFile)).contents
            self._on_end(int(end.reason), int(end.error))
            return
        if event_id == EVENT_FILE_LOADED:
            if self._token != self._load_token:
                return
            self._has_file = True
            self._set_state("playing")
            self.loaded.emit()
            return
        if event_id == EVENT_LOG and data:
            message = ctypes.cast(data, ctypes.POINTER(MpvEventLog)).contents
            text = _text(message.text).strip()
            prefix = _text(message.prefix)
            if text:
                line = f"{prefix}: {text}" if prefix else text
                log.warning("mpv %s", line)
                self.log_line.emit(line)
            return
        if event_id == EVENT_SHUTDOWN:
            self._set_state("stopped")

    def _read_value(self, fmt: int, data):
        """Read a number, flag, or string out of a property event."""
        if not data or fmt == MPV_FORMAT_NONE:
            return None
        if fmt == MPV_FORMAT_DOUBLE:
            return float(ctypes.cast(data, ctypes.POINTER(ctypes.c_double)).contents.value)
        if fmt == MPV_FORMAT_FLAG:
            return int(ctypes.cast(data, ctypes.POINTER(ctypes.c_int)).contents.value)
        if fmt == MPV_FORMAT_INT64:
            return int(ctypes.cast(data, ctypes.POINTER(ctypes.c_int64)).contents.value)
        if fmt == MPV_FORMAT_STRING:
            raw = ctypes.cast(data, ctypes.POINTER(ctypes.c_char_p)).contents.value
            return _text(raw)
        return None

    def _on_property(self, name: str, fmt: int, data):
        """Update position, duration, title, and picture size from libmpv."""
        value = self._read_value(fmt, data)
        if name == "time-pos":
            if isinstance(value, float) and value >= 0:
                self.position_changed.emit(value)
            return
        if name == "duration":
            if isinstance(value, float) and value >= 0:
                self.duration_changed.emit(value)
            return
        if name == "pause":
            if not self._has_file or value is None:
                return
            self._set_state("paused" if int(value) else "playing")
            return
        if name == "media-title":
            if isinstance(value, str) and value:
                self.title_changed.emit(value)
            return
        if name == "vid":
            self._set_video_on(isinstance(value, int) and value > 0)
            return
        if name in ("dwidth", "dheight"):
            self._note_display_size(name == "dwidth", value)
            return
        changed = False
        if name == "audio-bitrate" and isinstance(value, (int, float)) and value > 0:
            kbps = int(round(value / 1000.0)) if value > 2000 else int(round(value))
            if kbps != self._kbps:
                self._kbps = kbps
                changed = True
        elif name == "audio-params/samplerate" and isinstance(value, int) and value > 0:
            if value != self._hz:
                self._hz = value
                changed = True
        elif name == "audio-params/channel-count" and isinstance(value, int) and value > 0:
            if value != self._channels:
                self._channels = value
                changed = True
        if changed:
            self.info_changed.emit(self._kbps, self._hz, self._channels)

    def _on_end(self, reason: int, error: int):
        """Emit the end of a file, and ignore the end caused by replacing it."""
        if reason == END_STOP and self._replacing:
            self._replacing = False
            return
        if reason == END_REDIRECT:
            return
        if self._token != self._load_token and reason == END_ERROR:
            return
        self._has_file = False
        self._kbps = 0
        self._hz = 0
        self._channels = 0
        self._dwidth = 0
        self._dheight = 0
        self._dsize = (0, 0)
        self._set_video_on(False)
        self.info_changed.emit(0, 0, 0)
        self._set_state("stopped")
        text = ""
        if reason == END_ERROR:
            text = self._err(error)
        self.ended.emit(reason, text)

    def _property_string(self, name: str) -> str | None:
        """Read one libmpv property as text and free the returned buffer."""
        if self._ctx is None or self._closing:
            return None
        try:
            pointer = self._lib.mpv_get_property_string(self._ctx, name.encode("utf-8"))
        except Exception:
            return None
        if not pointer:
            return None
        try:
            raw = ctypes.cast(pointer, ctypes.c_char_p).value
            if not raw:
                return ""
            return raw.decode("utf-8", "replace")
        finally:
            self._lib.mpv_free(pointer)

    def has_video_track(self) -> bool:
        """True when the loaded file has a picture track. Cover art does not count."""
        count_text = self._property_string("track-list/count")
        if not count_text:
            return False
        try:
            count = int(count_text)
        except ValueError:
            return False
        for index in range(min(max(count, 0), 64)):
            kind = self._property_string(f"track-list/{index}/type")
            if kind != "video":
                continue
            art = (self._property_string(f"track-list/{index}/albumart") or "").lower()
            if art in ("yes", "true", "1"):
                continue
            return True
        return False

    def load(self, path: str, *, user_agent: str = "", referrer: str = "") -> bool:
        """Play one path, with the channel's own agent and referrer when it has them."""
        self._token += 1
        self._load_token = self._token
        self._replacing = self._has_file
        self._kbps = 0
        self._hz = 0
        self._channels = 0
        agent, refer = stream_headers(user_agent, referrer)
        # Direct streams skip the hook. A video page still needs it.
        self._set("ytdl", "yes" if needs_ytdl(path) else "no")
        self._set("user-agent", agent)
        self._set("referrer", refer)
        rc = self._command("loadfile", path, "replace")
        if rc < 0:
            self._replacing = False
            self._has_file = False
            self._set_state("stopped")
            message = self._err(rc)
            # Defer so a failed load inside an end-of-track handler cannot recurse.
            QTimer.singleShot(0, lambda message=message: self._emit_ended(END_ERROR, message))
            return False
        self._set("pause", "no")
        return True

    def _emit_ended(self, reason: int, text: str):
        """Report a failed load on the next turn so the handler cannot recurse."""
        if self._closing:
            return
        self.ended.emit(reason, text)

    def stop(self):
        """Stop the current file."""
        self._token += 1
        self._replacing = False
        if self._ctx is not None and (self._has_file or self.state != "stopped"):
            self._command("stop")
        self._has_file = False
        self._set_state("stopped")

    def set_paused(self, paused: bool):
        """Pause or resume."""
        if not self._has_file:
            return
        self._set("pause", "yes" if paused else "no")

    def seek(self, seconds: float):
        """Jump to a position in seconds."""
        if not self._has_file:
            return
        self._command("seek", f"{max(0.0, float(seconds)):.3f}", "absolute")

    def seek_relative(self, delta: float):
        """Jump forward or back by a number of seconds."""
        if not self._has_file:
            return
        self._command("seek", f"{float(delta):.3f}", "relative")

    @property
    def render_ready(self) -> bool:
        """True after the OpenGL render context has been created."""
        return self._render_ready and self._render is not None

    def set_video_enabled(self, enabled: bool, hardware: bool = False):
        """Select a picture track. Ignored until an OpenGL context is attached."""
        enabled = bool(enabled) and self.render_ready
        if enabled and hardware:
            self._set("hwdec", "auto-safe")
        self._set("vid", "auto" if enabled else "no")

    def bind_render(self, get_proc) -> bool:
        """Create the OpenGL render context. The GL context must be current.

        `get_proc` is an OPENGL_PROC callback. It has to stay valid until
        free_render, and the same GL context has to be current for every
        later render call.
        """
        if self._ctx is None or self._closing:
            return False
        if self.render_ready:
            return True
        if not _bind_render_api(self._lib):
            log.warning("this libmpv has no video render API")
            return False
        self._get_proc = get_proc
        self._gl_init = MpvOpenGLInitParams()
        self._gl_init.get_proc_address = get_proc
        self._gl_init.get_proc_address_ctx = None
        self._advanced = ctypes.c_int(1)
        self._api_name = ctypes.c_char_p(b"opengl")
        init_ptr = ctypes.cast(ctypes.pointer(self._gl_init), ctypes.c_void_p)
        advanced_ptr = ctypes.cast(ctypes.pointer(self._advanced), ctypes.c_void_p)
        api_ptr = ctypes.cast(self._api_name, ctypes.c_void_p)
        params = self._render_params((
            (MPV_RENDER_PARAM_API_TYPE, api_ptr),
            (MPV_RENDER_PARAM_OPENGL_INIT_PARAMS, init_ptr),
            (MPV_RENDER_PARAM_ADVANCED_CONTROL, advanced_ptr),
        ))
        out = ctypes.c_void_p()
        rc = int(self._lib.mpv_render_context_create(ctypes.byref(out), self._ctx, params))
        if rc < 0 or not out.value:
            log.warning("video render context was not created (%s)", self._err(rc))
            return False
        self._render = out.value
        self._lib.mpv_render_context_set_update_callback(
            self._render, self._render_update_cb, None
        )
        self._render_ready = True
        return True

    def render(self, fbo: int, width: int, height: int, *, skip: bool = False, flip: bool = True) -> bool:
        """Draw into an FBO. The GL context from bind_render must be current."""
        if not self.render_ready or self._closing or self._in_render:
            return False
        if width < 1 or height < 1:
            return False
        self._in_render = True
        rc = -1
        try:
            self._lib.mpv_render_context_update(self._render)
            frame = MpvOpenGLFBO()
            frame.fbo = int(fbo)
            frame.w = int(width)
            frame.h = int(height)
            frame.internal_format = 0
            flip_flag = ctypes.c_int(1 if flip else 0)
            skip_flag = ctypes.c_int(1 if skip else 0)
            # The structs have to outlive the call. They are locals, and render is synchronous.
            params = self._render_params((
                (MPV_RENDER_PARAM_OPENGL_FBO, ctypes.cast(ctypes.pointer(frame), ctypes.c_void_p)),
                (MPV_RENDER_PARAM_FLIP_Y, ctypes.cast(ctypes.pointer(flip_flag), ctypes.c_void_p)),
                (MPV_RENDER_PARAM_SKIP_RENDERING, ctypes.cast(ctypes.pointer(skip_flag), ctypes.c_void_p)),
            ))
            rc = int(self._lib.mpv_render_context_render(self._render, params))
        finally:
            self._in_render = False
        if rc < 0:
            if rc != self._last_render_error:
                self._last_render_error = rc
                log.warning("video frame was not drawn (%s)", self._err(rc))
            return False
        return True

    def report_swap(self):
        """Tell libmpv that the drawn frame was shown."""
        if self.render_ready and not self._closing:
            self._lib.mpv_render_context_report_swap(self._render)

    def free_render(self):
        """Drop the render context. The GL context must be current."""
        render = self._render
        self._render = None
        self._render_ready = False
        if render is None:
            return
        self._lib.mpv_render_context_set_update_callback(render, self._render_noop_cb, None)
        self._lib.mpv_render_context_free(render)

    def _render_params(self, entries):
        """Build a render-parameter array terminated by an invalid entry."""
        params = (MpvRenderParam * (len(entries) + 1))()
        for index, (kind, data) in enumerate(entries):
            params[index].type = int(kind)
            params[index].data = data
        params[len(entries)].type = MPV_RENDER_PARAM_INVALID
        params[len(entries)].data = None
        return params

    def _set_video_on(self, on: bool):
        """Emit whether a picture is present, once per change."""
        on = bool(on)
        if on == self._video_on:
            return
        self._video_on = on
        self.video_changed.emit(on)

    def _note_display_size(self, width_axis: bool, value):
        """Remember the picture size and emit it when both sides are known."""
        size = int(value) if isinstance(value, int) and value > 0 else 0
        if width_axis:
            self._dwidth = size
        else:
            self._dheight = size
        pair = (self._dwidth, self._dheight)
        if pair[0] > 0 and pair[1] > 0 and pair != self._dsize:
            self._dsize = pair
            self.video_sized.emit(pair[0], pair[1])

    def _on_render_update(self, _ctx):
        """Ask the video window to draw a new frame."""
        if self._closing or not self._render_ready:
            return
        try:
            self.frame_ready.emit()
        except RuntimeError:
            return

    def _on_render_noop(self, _ctx):
        """Render callback used after the real callback is detached."""
        return

    def set_volume(self, volume: int):
        """Set the playback volume from 0 to 100."""
        volume = max(0, min(100, int(volume)))
        self._set("volume", str(volume))

    def set_mute(self, muted: bool):
        """Mute or unmute."""
        self._set("mute", "yes" if muted else "no")

    def set_balance(self, balance: float):
        """Set the stereo balance from left to right."""
        self._balance = max(-1.0, min(1.0, float(balance)))
        self._af_timer.start(70)

    def set_eq(self, enabled: bool, preamp: float, bands):
        """Store the equalizer and apply it on the next turn."""
        self._eq_on = bool(enabled)
        self._preamp = float(preamp)
        self._bands = [float(value) for value in bands]
        self._af_timer.start(70)

    def apply_audio_now(self):
        """Apply the equalizer and balance immediately."""
        self._af_timer.stop()
        self._apply_af()

    def _apply_af(self):
        """Send the equalizer and balance filter string to libmpv."""
        audio_filter = build_af(self._eq_on, self._preamp, self._bands, self._balance)
        if audio_filter == self._af:
            return
        if abs(self._balance) >= 0.01:
            self._set("audio-channels", "stereo")
        else:
            self._set("audio-channels", "auto")
        rc = self._set("af", audio_filter)
        if rc < 0 and audio_filter:
            log.warning("equalizer rejected (%s): %s", self._err(rc), audio_filter)
            self.log_line.emit(f"equalizer: {self._err(rc)}")
            return
        self._af = audio_filter

    def shutdown(self):
        """Stop events and destroy the libmpv handle."""
        if self._ctx is None:
            return
        self._closing = True
        self._af_timer.stop()
        self._poll.stop()
        ctx = self._ctx
        self._ctx = None
        try:
            self._wake.disconnect(self._pump)
        except TypeError:
            pass
        self._lib.mpv_set_wakeup_callback(ctx, self._noop_cb, None)
        if self._render is not None:
            log.warning("video render context was still open at shutdown")
            self.free_render()
        self._lib.mpv_terminate_destroy(ctx)
