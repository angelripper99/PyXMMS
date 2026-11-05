# PyXMMS

A classic music player for current Linux: main controls, playlist, and a 10-band equalizer. A video window opens when the file has a picture. Playback goes through libmpv, so it plays what `mpv` plays (MP3, Ogg, FLAC, Opus, WAV, M4A, MP4, MKV, WebM, and the rest).

PyXMMS is a new player with the layout and habits of the classic GTK 1 player XMMS from 2007, drawn from scratch. PyXMMS is not that project, and it does not load Winamp skin files.

## Requirements

Nothing is compiled. Copy this folder to your Linux machine and run it.

PyXMMS is written for Linux. It loads `libmpv.so.2` by that soname, uses a Unix file lock so only one copy runs, and installs a Freedesktop menu entry. Windows and macOS would need code changes.

- Python 3
- PyQt6 6.5 or newer (`pip install -r requirements.txt`, or the distro package)
- `libmpv.so.2` from a recent mpv. This was written against mpv 0.41, client API 2.5. The package name varies by distro: `libmpv`, `libmpv2`, or `mpv`.
- `ffprobe` from FFmpeg, for titles and durations. Playback still works without it; titles then fall back to the filename.
- A sound output mpv can use (PipeWire, PulseAudio, or ALSA)
- Desktop OpenGL, for the video window. If OpenGL will not start, the sound still plays and the picture stays closed.
- DejaVu Sans and DejaVu Sans Mono. These are the requested fonts. Qt substitutes another font if they are missing.

## Run

```bash
./bin/pyxmms
./bin/pyxmms ~/Music
python3 -m pyxmms
```

The first launch also adds a menu entry named PyXMMS and a `pyxmms` command on `~/.local/bin`, when that directory is on your `PATH`.

The main window opens at 275×116. The menu’s Window scale entry switches to Medium or Large if you want it bigger.

State is stored in `~/.config/pyxmms/state.json` (volume, equalizer, playlist, window positions). If that file is missing, the first launch copies it from an older folder named `pyxmms` under `~/.config`. Nothing is uploaded anywhere.

## Shortcuts

| Key | Action |
| --- | --- |
| Z / X / C / V / B | Previous, play, pause, stop, next |
| Left / Right | Seek 5 seconds (Shift seeks 30) |
| Up / Down | Volume |
| S / R | Shuffle / cycle repeat (off, playlist, one track) |
| L / E | Playlist / equalizer |
| Space | Pause |
| Ctrl+O / Ctrl+D / Ctrl+S | Add files, add a folder, save playlist |
| Ctrl+Q | Quit |

Double-click a title bar, or use its shade button, to roll that window up. Shift-drag a title bar to pull that window off the stack. Click the time readout to switch between elapsed and remaining. Click the analyzer to cycle bars, scope, and off. The analyzer follows the playback clock; it is not a live trace of the audio. Click VOL to mute. Click BAL to center the balance.

A file that is only audio does not open the video window. Closing the video window leaves the sound playing and stops drawing the picture until the next file, or until you choose Show video from the menu. Drag the corner of that window to resize it. Album art is not shown as a video.

Video playback does not run the `ffmpeg` program. libmpv decodes the file with the FFmpeg libraries linked into the system mpv (`libavformat`, `libavcodec`, and the rest) and draws the picture with OpenGL. `ffprobe` is only used for titles and durations.

In the playlist: double-click a row to play it, drag a row to reorder, Delete to remove, Ctrl+A to select all. Load playlist and Save playlist read and write M3U, PLS, and XSPF, so a list can move to Audacious, Amarok, or QMPlay2. Audacious `.audpl` files open and save as well.

Add URL plays an internet station. These all work:

- A direct stream
- A station page that points at the audio
- A Shoutcast or Icecast address. When that address is a text list (`.m3u`, `.pls`, `.xspf`), PyXMMS plays the station inside it. When the server sends the audio itself, that address is played directly.

An `.m3u8` address is one HLS stream, the kind used for IPTV. It is played whole, not split into pieces. A `.mpd` DASH address is handled the same way. A channel list (an extended `.m3u` of many stations) opens as one row per channel. HLS, DASH, MPEG-TS, FLV, and other direct addresses play through libmpv. A normal video page, such as YouTube, still goes through the youtube-dl hook. When a channel lists a user agent or a referrer, that value is sent with the request. A link that is down still fails. When the stream has a picture, the video window opens. That includes a live address that does not end in a video extension. An audio-only stream stays on sound. A live channel has no program length, so Play leaves it running and a drop opens it again. A recorded program with a real length can still be restarted. The player sends a normal browser user agent, because some stations refuse `libmpv`. The playlist row takes the channel name from the list, and then the name the station sends. A pasted token, with no `http://` or `https://`, is not a station address. A link that ends in a video extension such as `.mp4` or `.mkv` still opens the picture window. A plain station address stays on audio.

## License

PyXMMS is free software under the GNU General Public License, version 2 or later. The full text is in `LICENSE`. Playback links to libmpv, which is published under the same GPL terms.

## Tests

```bash
python3 -m unittest tests.test_playlist tests.test_instance
```
