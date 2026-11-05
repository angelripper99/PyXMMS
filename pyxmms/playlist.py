"""Playlist model, path expansion, and playlist files.

Track equality is identity on purpose. Two entries can share a path, and the
shuffle bag and selection both need to tell those rows apart.

Copyright (C) 2026 the PyXMMS authors. Released under GPL-2.0-or-later. See LICENSE.
"""

from __future__ import annotations

import os
import random
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse

AUDIO_EXT = {
    ".mp3", ".mp2", ".mpga", ".mpa", ".ogg", ".oga", ".opus", ".spx",
    ".flac", ".wav", ".wave", ".aif", ".aiff", ".aifc", ".caf",
    ".m4a", ".aac", ".mp4", ".m4b", ".wma", ".mpc", ".wv", ".ape",
    ".mka", ".tta", ".tak", ".dsf", ".dff", ".webm", ".mkv",
    ".mod", ".xm", ".it", ".s3m", ".mid", ".midi", ".3ga",
}
VIDEO_EXT = {
    ".3g2", ".3gp", ".asf", ".avi", ".divx", ".dv", ".f4v", ".flv",
    ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mpe", ".mpeg", ".mpg",
    ".mts", ".nut", ".ogm", ".ogv", ".qt", ".rm", ".rmvb", ".ts", ".vob",
    ".webm", ".wmv",
}
MEDIA_EXT = AUDIO_EXT | VIDEO_EXT
PLAYLIST_EXT = {".m3u", ".m3u8", ".pls", ".xspf", ".audpl"}
# One program, not a list of stations. IPTV uses these, and the program may include a picture.
# .m3u8s is a rare HLS name used by a few channel lists.
IPTV_EXT = {".m3u8", ".m3u8s", ".mpd"}
# Audacious, Amarok, and QMPlay2 all read and write the first three.
# .audpl is Audacious's own list.
XSPF_NS = "http://xspf.org/ns/0/"
SAVE_FORMATS = (
    ("M3U playlist (*.m3u *.m3u8)", ".m3u"),
    ("PLS playlist (*.pls)", ".pls"),
    ("XSPF playlist (*.xspf)", ".xspf"),
    ("Audacious playlist (*.audpl)", ".audpl"),
)
# Some stations refuse the default "libmpv" agent. A browser agent is accepted.
STREAM_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# The iptv-org index is a few megabytes. A short cap would keep only the first channels.
_PLAYLIST_FETCH_LIMIT = 8 * 1024 * 1024
_PLAYLIST_FETCH_TIMEOUT = 20.0
# A folder of music can hold a long list. Stop well short of reading a huge file whole.
_LOCAL_LIST_LIMIT = 32 * 1024 * 1024
_MAX_LIST_ENTRIES = 50000
_MAX_TITLE = 240
_MAX_USER_AGENT = 300
_MAX_REFERRER = 500
# Schemes a downloaded list may hand to the player. file: and other schemes stay out.
_REMOTE_SCHEMES = frozenset({
    "http", "https",
    "rtmp", "rtmps", "rtmpe", "rtmpt", "rtmpte",
    "mmsh", "mmst", "srt",
})

_NUMBER_SPLIT = re.compile(r"(\d+)")


def natural_key(text: str):
    """Sort key that orders numbers by value, so track 2 comes before track 10."""
    parts = _NUMBER_SPLIT.split(str(text))
    key = []
    for part in parts:
        if part.isdigit():
            key.append(int(part))
        else:
            key.append(part.lower())
    return key


def format_time(seconds: float | None) -> str:
    """Format seconds as minutes and seconds, or hours when needed."""
    if seconds is None or seconds < 0:
        return "--:--"
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


@dataclass(eq=False)
class Track:
    """One playlist row. Two rows with the same path stay distinct."""
    path: str
    title: str = ""
    artist: str = ""
    duration: float | None = None
    probed: bool = False
    _exists: bool | None = None
    user_agent: str = ""
    referrer: str = ""

    @property
    def name(self) -> str:
        """The label shown on the row: artist and title, the title, or the file name."""
        if self.artist and self.title:
            return f"{self.artist} - {self.title}"
        if self.title:
            return self.title
        if "://" in self.path:
            return self.path
        return Path(self.path).stem

    def available(self) -> bool:
        """True for a stream, or when the local file is still on disk."""
        if self._exists is None:
            if "://" in self.path:
                self._exists = True
            else:
                self._exists = Path(self.path).is_file()
        return self._exists


@dataclass
class PlaylistEntry:
    """One row from a playlist file, including a channel name and HTTP headers."""

    path: str
    title: str = ""
    user_agent: str = ""
    referrer: str = ""


def _coerce_track(item) -> Track | None:
    """Build a Track from a path, a playlist entry, or an existing Track."""
    if isinstance(item, Track):
        return item if item.path else None
    if isinstance(item, PlaylistEntry):
        if not item.path:
            return None
        return Track(
            item.path,
            title=item.title,
            user_agent=item.user_agent,
            referrer=item.referrer,
        )
    if isinstance(item, str) and item:
        return Track(item)
    return None


class Playlist:
    """The ordered rows, the current row, shuffle, and repeat."""
    def __init__(self):
        """Start with an empty list."""
        self.tracks: list[Track] = []
        self.current: int | None = None
        self.shuffle = False
        self.repeat = "off"  # off, all, one
        self.history: list[Track] = []
        self._bag: list[Track] = []
        self._listeners = []

    def on_change(self, fn):
        """Register a callback for row changes."""
        self._listeners.append(fn)

    def _emit(self):
        """Tell listeners that the rows changed."""
        for fn in list(self._listeners):
            fn()

    @property
    def current_track(self) -> Track | None:
        """The row that is current, or None."""
        if self.current is None:
            return None
        if 0 <= self.current < len(self.tracks):
            return self.tracks[self.current]
        return None

    def set_current(self, index: int | None):
        """Make one row current and take it out of the shuffle bag."""
        if index is None or not self.tracks:
            self.current = None
            return
        index = max(0, min(int(index), len(self.tracks) - 1))
        self.current = index
        track = self.tracks[index]
        self._bag = [item for item in self._bag if item is not track]

    def set_shuffle(self, on: bool):
        """Turn shuffle on or off and refill the bag."""
        self.shuffle = bool(on)
        if self.shuffle:
            self._refill_bag()
        else:
            self._bag.clear()
        self._emit()

    def set_repeat(self, mode: str):
        """Set repeat to off, the whole list, or one track."""
        if mode not in ("off", "all", "one"):
            mode = "off"
        self.repeat = mode
        self._emit()

    def cycle_repeat(self) -> str:
        """Move repeat to the next mode and return it."""
        order = ("off", "all", "one")
        self.repeat = order[(order.index(self.repeat) + 1) % len(order)]
        self._emit()
        return self.repeat

    def _refill_bag(self):
        """Shuffle every row except the one that is playing."""
        current = self.current_track
        self._bag = [track for track in self.tracks if track is not current]
        random.shuffle(self._bag)

    def mark_history(self):
        """Remember the current row so Previous can return to it."""
        track = self.current_track
        if track is None:
            return
        if self.history and self.history[-1] is track:
            return
        self.history.append(track)
        if len(self.history) > 400:
            del self.history[:-400]

    def insert(self, index: int, paths) -> list[Track]:
        """Insert paths or entries at an index and return the new rows."""
        index = max(0, min(int(index), len(self.tracks)))
        made = []
        for item in paths:
            track = _coerce_track(item)
            if track is not None:
                made.append(track)
        if not made:
            return []
        self.tracks[index:index] = made
        if self.current is not None and self.current >= index:
            self.current += len(made)
        if self.shuffle:
            current = self.current_track
            self._bag.extend(track for track in made if track is not current)
        self._emit()
        return made

    def remove(self, tracks_to_remove) -> bool:
        """Remove rows. True when the current row was one of them."""
        gone = {id(track) for track in tracks_to_remove}
        if not gone:
            return False
        current = self.current_track
        playing_removed = current is not None and id(current) in gone
        old_index = self.current if self.current is not None else 0
        self.tracks = [track for track in self.tracks if id(track) not in gone]
        self._bag = [track for track in self._bag if id(track) not in gone]
        self.history = [track for track in self.history if id(track) not in gone]
        if not self.tracks:
            self.current = None
        elif current is not None and id(current) not in gone:
            self.current = self.tracks.index(current)
        else:
            self.current = min(max(0, old_index), len(self.tracks) - 1)
        self._emit()
        return playing_removed

    def reorder(self, new_tracks: list[Track]):
        """Replace the row order and keep the current row."""
        current = self.current_track
        self.tracks = list(new_tracks)
        if current is not None and current in self.tracks:
            self.current = self.tracks.index(current)
        else:
            self.current = None
        alive = {id(track) for track in self.tracks}
        self._bag = [track for track in self._bag if id(track) in alive]
        self.history = [track for track in self.history if id(track) in alive]
        self._emit()

    def clear(self):
        """Remove every row."""
        self.tracks.clear()
        self._bag.clear()
        self.history.clear()
        self.current = None
        self._emit()

    def sort(self, how: str):
        """Sort the rows by title, artist, path, or duration."""
        keys = {
            "title": lambda track: (natural_key(track.name), natural_key(track.path)),
            "filename": lambda track: natural_key(Path(track.path).name),
            "path": lambda track: natural_key(track.path),
        }
        key = keys.get(how)
        if key is None:
            return
        current = self.current_track
        self.tracks.sort(key=key)
        if current is not None:
            self.current = self.tracks.index(current)
        self._emit()

    def reverse(self):
        """Reverse the rows and keep the current row."""
        current = self.current_track
        self.tracks.reverse()
        if current is not None:
            self.current = self.tracks.index(current)
        self._emit()

    def randomize(self):
        """Put the rows in a random order and keep the current row."""
        current = self.current_track
        random.shuffle(self.tracks)
        if current is not None:
            self.current = self.tracks.index(current)
        self._emit()

    def next_index(self, automatic: bool) -> int | None:
        """The next row for shuffle, repeat, or linear play."""
        count = len(self.tracks)
        if count == 0:
            return None
        current = self.current
        if automatic and self.repeat == "one" and current is not None:
            return current
        if self.shuffle:
            alive = {id(track) for track in self.tracks}
            current_id = id(self.tracks[current]) if current is not None else None
            self._bag = [
                track for track in self._bag
                if id(track) in alive and id(track) != current_id
            ]
            if not self._bag:
                if self.repeat != "all":
                    return None
                self._refill_bag()
                self._bag = [track for track in self._bag if id(track) != current_id]
                if not self._bag:
                    return current
            choice = self._bag.pop()
            return self.tracks.index(choice)
        if current is None:
            return 0
        nxt = current + 1
        if nxt >= count:
            return 0 if self.repeat == "all" else None
        return nxt

    def previous_index(self) -> int | None:
        """The previous row, using the history when shuffle is on."""
        if not self.tracks:
            return None
        alive = {id(track): index for index, track in enumerate(self.tracks)}
        while self.history:
            track = self.history.pop()
            index = alive.get(id(track))
            if index is not None and index != self.current:
                return index
        if self.current is None:
            return len(self.tracks) - 1
        if self.current <= 0:
            if self.repeat == "all" and len(self.tracks) > 1:
                return len(self.tracks) - 1
            return 0
        return self.current - 1

    def apply_probe(self, path: str, title: str, artist: str, duration: float | None) -> bool:
        """Store a probed title, artist, and duration on matching rows."""
        changed = False
        for track in self.tracks:
            if track.path == path and not track.probed:
                track.title = title
                track.artist = artist
                track.duration = duration
                track.probed = True
                changed = True
        return changed

    def note_stream_title(self, path: str, title: str) -> bool:
        """Store the name a station sends. The raw address is left as it is."""
        cleaned = title.strip()
        if not cleaned or cleaned == path or "://" in cleaned:
            return False
        # "index.m3u8" is the file name, not a channel name. Keep the list title.
        leaf = unquote(path.split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1])
        if leaf and cleaned == leaf:
            return False
        changed = False
        for track in self.tracks:
            if track.path != path or track.title == cleaned:
                continue
            track.title = cleaned
            changed = True
        return changed

    def known_duration(self) -> tuple[float, bool]:
        """Return (seconds, complete). complete is false when a row has no duration yet."""
        total = 0.0
        complete = True
        for track in self.tracks:
            if track.duration is None:
                complete = False
            else:
                total += track.duration
        return total, complete and bool(self.tracks)


def normalize_path(raw: str) -> str:
    """Leave a URL unchanged, and make a local path absolute."""
    if "://" in raw:
        return raw
    path = Path(raw).expanduser()
    try:
        return str(path.resolve())
    except OSError:
        return str(path.absolute())


def playlist_open_filter() -> str:
    """The open-dialog filter for playlist files."""
    patterns = " ".join("*" + ext for ext in sorted(PLAYLIST_EXT))
    return f"Playlists ({patterns})"


def playlist_save_filter() -> str:
    """The save-dialog filters for M3U, PLS, XSPF, and Audacious."""
    return ";;".join(name for name, _suffix in SAVE_FORMATS)


def with_playlist_suffix(path: str, selected_filter: str) -> str:
    """Keep a known playlist suffix. Otherwise use the format chosen in the dialog."""
    if Path(path).suffix.lower() in PLAYLIST_EXT:
        return path
    chosen = ".m3u"
    for name, suffix in SAVE_FORMATS:
        if selected_filter == name:
            chosen = suffix
            break
    return path + chosen


def _local_name(tag: str) -> str:
    """An XML tag name without its namespace."""
    return tag.rsplit("}", 1)[-1]


def _looks_like_xspf(text: str) -> bool:
    """True when the text looks like an XSPF document."""
    head = text.lstrip()[:300].lower()
    return head.startswith("<?xml") or head.startswith("<playlist") or "<tracklist" in head


def _xml_root(text: str):
    """Parse playlist XML. None when it is too large or declares entities."""
    if not text or len(text) > _PLAYLIST_FETCH_LIMIT:
        return None
    folded = text.upper()
    if "<!DOCTYPE" in folded or "<!ENTITY" in folded:
        return None
    try:
        return ET.fromstring(text)
    except (ET.ParseError, RecursionError):
        return None


def _xspf_locations(text: str) -> list[str]:
    """The first location in each XSPF track."""
    root = _xml_root(text)
    if root is None:
        return []
    found = []
    for element in root.iter():
        if _local_name(element.tag) != "track":
            continue
        for child in list(element):
            if _local_name(child.tag) != "location" or not child.text or not child.text.strip():
                continue
            found.append(child.text.strip())
            break
    return found


def _audpl_uris(text: str) -> list[str]:
    """The uri lines in an Audacious playlist."""
    found = []
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip().lower() == "uri" and value.strip():
            found.append(value.strip())
    return found


def _pls_files(text: str) -> list[str]:
    """The File entries in a PLS playlist."""
    found = []
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip().lower().startswith("file") and value.strip():
            found.append(value.strip())
    return found


def _m3u_paths(text: str) -> list[str]:
    """The non-comment lines in an M3U playlist."""
    found = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        found.append(line)
    return found


_UA_KEYS = ("http-user-agent", "user-agent")
_REF_KEYS = ("http-referrer", "http-referer", "referrer", "referer")
_ATTR_RE = re.compile(r"""([A-Za-z0-9_-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|(\S+))""")


def _extinf_parts(body: str) -> tuple[str, str]:
    """Split an EXTINF body into attributes and the title.

    The title starts at the first comma that is not inside quotes. Logo URLs
    in the attributes contain commas.
    """
    quote = ""
    for index, char in enumerate(body):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
            continue
        if char == ",":
            return body[:index], body[index + 1 :].strip()
    return body, ""


def _attr_map(text: str) -> dict[str, str]:
    """Quoted and bare key=value pairs from an EXTINF line."""
    found = {}
    for match in _ATTR_RE.finditer(text):
        value = match.group(2)
        if value is None:
            value = match.group(3)
        if value is None:
            value = match.group(4)
        found[match.group(1).lower()] = (value or "").strip()
    return found


def _mapped(found: dict[str, str], keys: tuple[str, ...]) -> str:
    """The first present value among the given attribute names."""
    for key in keys:
        value = found.get(key, "").strip()
        if value:
            return value
    return ""


def _m3u_entries(text: str, base: Path, base_url: str = "") -> list[PlaylistEntry]:
    """Extended M3U rows, keeping the channel name and VLC HTTP options."""
    entries = []
    pending_title = ""
    pending_ua = ""
    pending_ref = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if lowered.startswith("#extinf:"):
            attrs, title = _extinf_parts(stripped.split(":", 1)[1])
            pending_title = title
            found = _attr_map(attrs)
            user_agent = _mapped(found, _UA_KEYS)
            referrer = _mapped(found, _REF_KEYS)
            if user_agent:
                pending_ua = user_agent
            if referrer:
                pending_ref = referrer
            continue
        if lowered.startswith("#extvlcopt:"):
            key, sep, value = stripped.split(":", 1)[1].partition("=")
            if not sep:
                continue
            name = key.strip().lower()
            value = value.strip()
            if not value:
                continue
            if name in _UA_KEYS:
                pending_ua = value
            elif name in _REF_KEYS:
                pending_ref = value
            continue
        if stripped.startswith("#"):
            continue
        path = _resolve_entry(stripped, base, base_url)
        if path and len(entries) < _MAX_LIST_ENTRIES:
            entries.append(PlaylistEntry(
                path,
                _plain(pending_title, _MAX_TITLE),
                _plain(pending_ua, _MAX_USER_AGENT),
                _plain(pending_ref, _MAX_REFERRER),
            ))
        pending_title = ""
        pending_ua = ""
        pending_ref = ""
    return entries


def _allowed_remote(url: str) -> bool:
    """True when the address uses a stream scheme the player may open."""
    scheme = url.split(":", 1)[0].lower()
    return scheme in _REMOTE_SCHEMES


def _has_controls(text: str) -> bool:
    """True when text contains a character that could split a playlist line."""
    return any(not char.isprintable() for char in text)


def is_stream_url(url: str) -> bool:
    """True when a pasted address is a station or stream, not a local file."""
    text = url.strip()
    if not text or _has_controls(text) or "://" not in text:
        return False
    return _allowed_remote(text)


def _resolve_entry(entry: str, base: Path, base_url: str = "") -> str:
    """Turn one playlist line into a path. A remote list cannot name a local file."""
    raw = entry.strip()
    remote = base_url.lower().startswith(("http://", "https://"))
    if raw.lower().startswith("file:"):
        if remote:
            return ""
        parsed = urlparse(raw)
        if parsed.netloc.lower() in ("", "localhost"):
            path_text = unquote(parsed.path)
            if path_text:
                path = Path(path_text)
                if not path.is_absolute():
                    path = base / path
                return str(path)
        return ""
    if "://" in raw:
        if _has_controls(raw):
            return ""
        if remote and not _allowed_remote(raw):
            return ""
        return raw
    if base_url:
        joined = urljoin(base_url, raw)
        if _has_controls(joined) or (remote and not _allowed_remote(joined)):
            return ""
        return joined
    path = Path(raw)
    if not path.is_absolute():
        path = base / path
    return str(path)


def parse_playlist_text(text: str, base: Path, suffix: str, base_url: str = "") -> list[str]:
    """Paths from M3U, PLS, XSPF, or Audacious text."""
    suffix = suffix.lower()
    stripped = text.lstrip()
    if suffix == ".xspf" or _looks_like_xspf(stripped):
        entries = _xspf_locations(text)
    elif suffix == ".audpl":
        entries = _audpl_uris(text)
    elif suffix == ".pls" or stripped.lower().startswith("[playlist]"):
        entries = _pls_files(text)
    else:
        entries = _m3u_paths(text)
    resolved = [_resolve_entry(entry, base, base_url) for entry in entries]
    return [path for path in resolved if path][:_MAX_LIST_ENTRIES]


def stream_entries(text: str, base: Path, suffix: str, base_url: str = "") -> list[PlaylistEntry]:
    """Playlist rows. M3U keeps channel names and per-channel HTTP headers."""
    suffix = suffix.lower()
    stripped = text.lstrip()
    if suffix == ".xspf" or _looks_like_xspf(stripped):
        paths = _xspf_locations(text)
    elif suffix == ".audpl":
        paths = _audpl_uris(text)
    elif suffix == ".pls" or stripped.lower().startswith("[playlist]"):
        paths = _pls_files(text)
    else:
        return _m3u_entries(text, base, base_url)[:_MAX_LIST_ENTRIES]
    found = []
    for entry in paths:
        if not entry.strip():
            continue
        path = _resolve_entry(entry, base, base_url)
        if path:
            found.append(PlaylistEntry(path))
        if len(found) >= _MAX_LIST_ENTRIES:
            break
    return found


def media_suffix(path: str) -> str:
    """File type, ignoring a query string or fragment on a URL."""
    clean = path.split("?", 1)[0].split("#", 1)[0]
    return Path(unquote(clean)).suffix.lower()


# Pages whose media address is not the page itself. Everything else, including
# HLS, DASH, and a raw stream URL, is played by libmpv directly.
_PAGE_HOSTS = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "twitch.tv",
    "vimeo.com",
    "dailymotion.com",
    "dai.ly",
    "soundcloud.com",
    "facebook.com",
    "fb.watch",
    "fb.me",
    "tiktok.com",
    "instagram.com",
    "twitter.com",
    "x.com",
)


def needs_ytdl(url: str) -> bool:
    """True when the address is a video page rather than a direct stream."""
    if "://" not in url:
        return False
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    for page in _PAGE_HOSTS:
        if host == page or host.endswith("." + page):
            return True
    return False


def shows_picture(path: str) -> bool:
    """A video file, or an IPTV stream that may carry a picture."""
    suffix = media_suffix(path)
    return suffix in VIDEO_EXT or suffix in IPTV_EXT


def is_live_playback(path: str, duration: float) -> bool:
    """A station or live channel has no program length. A remote file with a length can restart.

    Live HLS often reports only the short window it can rewind. A recorded
    program is much longer than that window.
    """
    if "://" not in path:
        return False
    if duration <= 1.0:
        return True
    return media_suffix(path) in IPTV_EXT and duration < 10 * 60


def is_hls_playlist(text: str) -> bool:
    """True when the text is one HLS program rather than a list of channels."""
    return "#ext-x-" in text.lower()


def url_resource(url: str) -> str:
    """Path after the host, with a Shoutcast ';' kept.

    urlparse treats ';' as a parameter, which would turn `/;.m3u` into `/`.
    """
    tail = url.split("://", 1)[-1]
    tail = tail.split("?", 1)[0].split("#", 1)[0]
    slash = tail.find("/")
    if slash < 0:
        return ""
    return unquote(tail[slash:])


def is_station_playlist(url: str) -> bool:
    """A remote list of stations. An HLS `.m3u8` is one stream, so it stays whole."""
    scheme = url.split(":", 1)[0].lower()
    if scheme not in ("http", "https"):
        return False
    resource = url_resource(url).lower()
    if resource.endswith(".m3u8"):
        return False
    name = resource.rsplit("/", 1)[-1]
    return name.endswith((".m3u", ".pls", ".xspf", ".audpl"))


def looks_like_playlist_text(text: str) -> bool:
    """True when the start of the text looks like a playlist."""
    head = text.lstrip()[:400].lower()
    if not head:
        return False
    if head.startswith(("#extm3u", "#extinf", "[playlist]", "<?xml", "<playlist", "http://", "https://")):
        return True
    return "\nfile1=" in "\n" + head or head.startswith("file1=") or head.startswith("uri=")


def _playlist_content_type(value: str) -> bool:
    """True when a response may be playlist text rather than audio."""
    lowered = value.split(";", 1)[0].strip().lower()
    if not lowered:
        return True
    if "mpegurl" in lowered or "scpls" in lowered or "xspf" in lowered:
        return True
    if lowered.startswith(("audio/", "video/")):
        return False
    return True


def _body_is_audio(raw: bytes) -> bool:
    """True when the first bytes are a common audio file."""
    return raw.startswith((
        b"ID3", b"OggS", b"fLaC", b"RIFF",
        b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xf1", b"\xff\xf9",
    ))


class _HttpRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only when the new address is a stream. A list cannot point at a local file."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Accept an http or https redirect and refuse any other scheme."""
        if not _allowed_remote(newurl):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _playlist_opener():
    """An HTTP opener that will not follow a redirect off the web."""
    return urllib.request.build_opener(_HttpRedirect)


def fetch_playlist_text(url: str, timeout: float = _PLAYLIST_FETCH_TIMEOUT) -> str | None:
    """Read a remote station list. None means the address itself should be played.

    A Shoutcast `;.m3u` address is often the audio itself, not a text list.
    Leave that for the player instead of downloading the stream here.
    A list past the cap is kept up to the cap when it still looks like a playlist,
    so a long channel list is not thrown away.
    """
    if not url.lower().startswith(("http://", "https://")):
        return None
    request = urllib.request.Request(url, headers={"User-Agent": STREAM_USER_AGENT})
    try:
        with _playlist_opener().open(request, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type") or ""
            if not _playlist_content_type(content_type):
                return None
            raw = response.read(4096)
            if _body_is_audio(raw):
                return None
            if len(raw) >= 4096:
                raw += response.read(_PLAYLIST_FETCH_LIMIT + 1 - len(raw))
    except (OSError, urllib.error.URLError, ValueError, TimeoutError):
        return None
    truncated = len(raw) > _PLAYLIST_FETCH_LIMIT
    if truncated:
        raw = raw[:_PLAYLIST_FETCH_LIMIT]
    text = raw.decode("utf-8-sig", errors="replace")
    if truncated:
        if not looks_like_playlist_text(text):
            return None
        text = text.rsplit("\n", 1)[0]
    return text


def listed_entries(url: str, text: str) -> list[PlaylistEntry] | None:
    """Rows inside a fetched station list. None keeps `url` as the thing to play.

    An HLS body (`#EXT-X-`) is one program. It is not split into segments.
    """
    if not text or is_hls_playlist(text) or not looks_like_playlist_text(text):
        return None
    suffix = media_suffix(url)
    if suffix not in PLAYLIST_EXT:
        suffix = ".m3u"
    entries = stream_entries(text, Path("."), suffix, base_url=url)
    return entries or None


def station_entries(url: str, text: str) -> list[str] | None:
    """Paths inside a fetched station list. None keeps `url` as the thing to play."""
    entries = listed_entries(url, text)
    if not entries:
        return None
    return [entry.path for entry in entries]


def _one_line(text: str) -> str:
    """One playlist field. Line breaks and other control characters are removed."""
    chars = []
    for char in text:
        if char in "\r\n\t":
            chars.append(" ")
        elif char.isprintable():
            chars.append(char)
    return "".join(chars)


def _plain(text: str, limit: int) -> str:
    """One line of playlist text, with control characters removed."""
    chars = []
    for char in text:
        if char in "\r\n\t":
            chars.append(" ")
        elif char.isprintable():
            chars.append(char)
    return " ".join("".join(chars).split())[:limit]


def stream_headers(user_agent: str, referrer: str) -> tuple[str, str]:
    """The agent and referrer for one load. A blank agent uses the browser agent."""
    agent = _plain(user_agent or "", _MAX_USER_AGENT) or STREAM_USER_AGENT
    refer = _plain(referrer or "", _MAX_REFERRER)
    return agent, refer


def track_location(path: str) -> str:
    """File URI for a local path. Stream URLs stay as they are."""
    if "://" in path:
        return path
    file_path = Path(path)
    try:
        if not file_path.is_absolute():
            file_path = file_path.absolute()
        return file_path.as_uri()
    except (OSError, ValueError):
        return path


def m3u_text(tracks: list[Track]) -> str:
    """Write an extended M3U, including a channel's agent and referrer."""
    lines = ["#EXTM3U"]
    for track in tracks:
        duration = int(track.duration) if track.duration else -1
        lines.append(f"#EXTINF:{duration},{_one_line(track.name)}")
        if track.user_agent:
            lines.append("#EXTVLCOPT:http-user-agent=" + _one_line(track.user_agent))
        if track.referrer:
            lines.append("#EXTVLCOPT:http-referrer=" + _one_line(track.referrer))
        lines.append(_one_line(track.path))
    return "\n".join(lines) + "\n"


def pls_text(tracks: list[Track]) -> str:
    """Write a PLS playlist."""
    lines = ["[playlist]"]
    for index, track in enumerate(tracks, start=1):
        lines.append(f"File{index}={_one_line(track.path)}")
        lines.append(f"Title{index}={_one_line(track.name)}")
        length = int(track.duration) if track.duration else -1
        lines.append(f"Length{index}={length}")
    lines.append(f"NumberOfEntries={len(tracks)}")
    lines.append("Version=2")
    return "\n".join(lines) + "\n"


def xspf_text(tracks: list[Track]) -> str:
    """Write an XSPF playlist."""
    ET.register_namespace("", XSPF_NS)
    root = ET.Element(f"{{{XSPF_NS}}}playlist", {"version": "1"})
    title = ET.SubElement(root, f"{{{XSPF_NS}}}title")
    title.text = "PyXMMS"
    track_list = ET.SubElement(root, f"{{{XSPF_NS}}}trackList")
    for track in tracks:
        node = ET.SubElement(track_list, f"{{{XSPF_NS}}}track")
        location = ET.SubElement(node, f"{{{XSPF_NS}}}location")
        location.text = _one_line(track_location(track.path))
        label = track.title or track.name
        if label:
            item = ET.SubElement(node, f"{{{XSPF_NS}}}title")
            item.text = _one_line(label)
        if track.artist:
            item = ET.SubElement(node, f"{{{XSPF_NS}}}creator")
            item.text = _one_line(track.artist)
        if track.duration and track.duration > 0:
            item = ET.SubElement(node, f"{{{XSPF_NS}}}duration")
            item.text = str(int(track.duration * 1000))
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"


def audpl_text(tracks: list[Track]) -> str:
    """Audacious playlist. `uri` is a normal URI. Other fields are percent-encoded."""
    lines = [f"title={quote('PyXMMS', safe='')}"]
    for track in tracks:
        lines.append(f"uri={_one_line(track_location(track.path))}")
        if track.title:
            lines.append(f"title={quote(_one_line(track.title), safe='')}")
        if track.artist:
            lines.append(f"artist={quote(_one_line(track.artist), safe='')}")
        if track.duration and track.duration > 0:
            lines.append(f"length={int(track.duration * 1000)}")
    return "\n".join(lines) + "\n"


def playlist_text(tracks: list[Track], suffix: str) -> str:
    """Write the playlist in the format chosen by the file suffix."""
    suffix = suffix.lower()
    if suffix == ".pls":
        return pls_text(tracks)
    if suffix == ".xspf":
        return xspf_text(tracks)
    if suffix == ".audpl":
        return audpl_text(tracks)
    return m3u_text(tracks)


def track_record(track: Track):
    """Saved playlist row. A plain path stays a string. Headers and names are objects."""
    extra = {}
    if track.title:
        extra["title"] = track.title
    if track.artist:
        extra["artist"] = track.artist
    if track.user_agent:
        extra["user_agent"] = track.user_agent
    if track.referrer:
        extra["referrer"] = track.referrer
    if not extra:
        return track.path
    extra["path"] = track.path
    return extra


def track_from_config(item) -> Track | None:
    """Rebuild a row from a saved path or from a saved object."""
    if isinstance(item, str):
        return Track(item) if item else None
    if not isinstance(item, dict):
        return None
    path = item.get("path")
    if not isinstance(path, str) or not path:
        return None

    def text(key: str) -> str:
        """A saved string field, or an empty string when the value is not text."""
        value = item.get(key)
        return value if isinstance(value, str) else ""

    return Track(
        path,
        title=text("title"),
        artist=text("artist"),
        user_agent=text("user_agent"),
        referrer=text("referrer"),
    )


def expand_entries(raw_paths: list[str]) -> list[PlaylistEntry]:
    """Playable rows. A remote station list is expanded. An HLS URL stays one row."""
    found: list[PlaylistEntry] = []
    seen_dirs = set()
    seen_lists = set()

    def add_media(path: Path, title: str = "", user_agent: str = "", referrer: str = ""):
        """Append one local file, with any name and headers it inherited."""
        found.append(PlaylistEntry(normalize_path(str(path)), title, user_agent, referrer))

    def walk(raw: str, title: str = "", user_agent: str = "", referrer: str = ""):
        """Expand one path, folder, or station list into playable rows."""
        if not raw:
            return
        if "://" in raw:
            if is_station_playlist(raw) and raw not in seen_lists and len(seen_lists) < 8:
                seen_lists.add(raw)
                text = fetch_playlist_text(raw)
                entries = listed_entries(raw, text) if text else None
                if entries:
                    for entry in entries:
                        walk(entry.path, entry.title, entry.user_agent, entry.referrer)
                    return
            found.append(PlaylistEntry(raw, title, user_agent, referrer))
            return
        path = Path(raw).expanduser()
        try:
            is_dir = path.is_dir()
            is_file = path.is_file()
        except OSError:
            is_dir = False
            is_file = False
        if is_dir:
            try:
                key = str(path.resolve())
            except OSError:
                key = str(path)
            if key in seen_dirs:
                return
            seen_dirs.add(key)
            children = []
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
                for name in filenames:
                    if name.startswith("."):
                        continue
                    child = Path(dirpath) / name
                    suffix = child.suffix.lower()
                    if suffix in MEDIA_EXT or suffix in PLAYLIST_EXT:
                        children.append(child)
            children.sort(key=lambda item: natural_key(str(item)))
            for child in children:
                walk(str(child))
            return
        if is_file and path.suffix.lower() in PLAYLIST_EXT:
            try:
                key = str(path.resolve())
            except OSError:
                key = str(path)
            if key in seen_lists:
                return
            seen_lists.add(key)
            try:
                with path.open("rb") as handle:
                    raw_bytes = handle.read(_LOCAL_LIST_LIMIT + 1)
            except OSError:
                return
            if len(raw_bytes) > _LOCAL_LIST_LIMIT:
                raw_bytes = raw_bytes[:_LOCAL_LIST_LIMIT].rsplit(b"\n", 1)[0]
            text = raw_bytes.decode("utf-8-sig", errors="replace")
            # A media playlist is one program. A channel list has no #EXT-X- tags.
            if is_hls_playlist(text):
                add_media(path, title, user_agent, referrer)
                return
            for entry in stream_entries(text, path.parent, path.suffix.lower()):
                walk(entry.path, entry.title, entry.user_agent, entry.referrer)
            return
        add_media(path, title, user_agent, referrer)

    for raw in raw_paths:
        walk(raw)
    return found


def expand_paths(raw_paths: list[str]) -> list[str]:
    """Playable paths from files, folders, and station lists."""
    return [entry.path for entry in expand_entries(raw_paths)]
