"""Model tests for the playlist and equalizer filter string."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import xml.etree.ElementTree as ET

from pyxmms.audiofx import build_af
import urllib.request

from pyxmms.playlist import (
    Playlist,
    PlaylistEntry,
    _HttpRedirect,
    _MAX_TITLE,
    expand_entries,
    expand_paths,
    is_live_playback,
    is_station_playlist,
    is_stream_url,
    m3u_text,
    media_suffix,
    needs_ytdl,
    shows_picture,
    natural_key,
    parse_playlist_text,
    playlist_text,
    station_entries,
    stream_entries,
    stream_headers,
    track_from_config,
    track_record,
    with_playlist_suffix,
)


class NaturalSortTests(unittest.TestCase):
    def test_numbers_sort_in_numeric_order(self):
        names = ["track10.mp3", "track2.mp3", "track1.mp3"]
        self.assertEqual(
            sorted(names, key=natural_key),
            ["track1.mp3", "track2.mp3", "track10.mp3"],
        )


class PlaylistLogicTests(unittest.TestCase):
    def setUp(self):
        self.playlist = Playlist()
        self.playlist.insert(0, [f"/music/{index}.mp3" for index in range(5)])
        self.playlist.set_current(0)

    def test_linear_next_stops_at_the_end(self):
        seen = []
        for _ in range(6):
            nxt = self.playlist.next_index(automatic=True)
            if nxt is None:
                seen.append(None)
                break
            seen.append(nxt)
            self.playlist.set_current(nxt)
        self.assertEqual(seen, [1, 2, 3, 4, None])

    def test_repeat_all_wraps(self):
        self.playlist.set_repeat("all")
        self.playlist.set_current(4)
        self.assertEqual(self.playlist.next_index(automatic=True), 0)

    def test_repeat_one_is_only_automatic(self):
        self.playlist.set_repeat("one")
        self.assertEqual(self.playlist.next_index(automatic=True), 0)
        self.assertEqual(self.playlist.next_index(automatic=False), 1)

    def test_shuffle_plays_each_other_track_once(self):
        self.playlist.set_shuffle(True)
        seen = []
        for _ in range(6):
            nxt = self.playlist.next_index(automatic=True)
            if nxt is None:
                break
            seen.append(nxt)
            self.playlist.set_current(nxt)
        self.assertEqual(len(seen), 4)
        self.assertEqual(len(set(seen)), 4)
        self.assertNotIn(0, seen)

    def test_previous_uses_history_then_linear(self):
        self.playlist.mark_history()
        self.playlist.set_current(3)
        self.assertEqual(self.playlist.previous_index(), 0)
        self.assertEqual(self.playlist.previous_index(), 2)

    def test_remove_current_lands_on_the_next_row(self):
        current = self.playlist.current_track
        removed = self.playlist.remove([current])
        self.assertTrue(removed)
        self.assertEqual(self.playlist.current, 0)
        self.assertEqual(self.playlist.tracks[0].path, "/music/1.mp3")

    def test_sort_keeps_the_current_track(self):
        self.playlist.tracks[2].title = "Aaa"
        self.playlist.tracks[2].artist = "Aaa"
        current = self.playlist.tracks[2]
        self.playlist.set_current(2)
        self.playlist.sort("title")
        self.assertIs(self.playlist.current_track, current)


class PathTests(unittest.TestCase):
    def test_playlist_text_resolves_relative_files_and_keeps_urls(self):
        text = "#EXTM3U\n#EXTINF:3,Name\nsub/a.mp3\nhttp://example.com/a.ogg\n"
        paths = parse_playlist_text(text, Path("/music"), ".m3u")
        self.assertEqual(paths, ["/music/sub/a.mp3", "http://example.com/a.ogg"])

    def test_pls_entries(self):
        text = "[playlist]\nFile1=song.mp3\nTitle1=Song\nFile2=https://example.com/a\n"
        paths = parse_playlist_text(text, Path("/music"), ".pls")
        self.assertEqual(paths[0], "/music/song.mp3")
        self.assertEqual(paths[1], "https://example.com/a")

    def test_directory_walk_is_natural_and_skips_other_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "notes.txt").write_text("nope", encoding="utf-8")
            (root / "clip.avi").write_bytes(b"a")
            (root / "track10.mp3").write_bytes(b"a")
            (root / "track2.mp3").write_bytes(b"a")
            nested = root / "disc"
            nested.mkdir()
            (nested / "track1.ogg").write_bytes(b"a")
            found = expand_paths([str(root)])
        names = [Path(path).name for path in found]
        self.assertEqual(sorted(names), ["clip.avi", "track1.ogg", "track10.mp3", "track2.mp3"])
        self.assertLess(names.index("track2.mp3"), names.index("track10.mp3"))

    def test_m3u_round_trip_text(self):
        playlist = Playlist()
        playlist.insert(0, ["/tmp/a.mp3"])
        playlist.tracks[0].title = "Song"
        playlist.tracks[0].artist = "Artist"
        playlist.tracks[0].duration = 12.4
        text = m3u_text(playlist.tracks)
        paths = parse_playlist_text(text, Path("/tmp"), ".m3u")
        self.assertEqual(paths, ["/tmp/a.mp3"])

    def test_shared_formats_round_trip_paths(self):
        playlist = Playlist()
        playlist.insert(0, ["/tmp/my song.mp3", "https://example.com/a.ogg"])
        playlist.tracks[0].title = "Song"
        playlist.tracks[0].artist = "A & B"
        playlist.tracks[0].duration = 12.4
        for suffix in (".m3u", ".m3u8", ".pls", ".xspf", ".audpl"):
            text = playlist_text(playlist.tracks, suffix)
            paths = parse_playlist_text(text, Path("/tmp"), suffix)
            self.assertEqual(paths, ["/tmp/my song.mp3", "https://example.com/a.ogg"], suffix)

    def test_xspf_keeps_title_creator_and_duration(self):
        playlist = Playlist()
        playlist.insert(0, ["/tmp/a.mp3"])
        playlist.tracks[0].title = "Song"
        playlist.tracks[0].artist = "A & B"
        playlist.tracks[0].duration = 12.4
        root = ET.fromstring(playlist_text(playlist.tracks, ".xspf"))
        track = next(item for item in root.iter() if item.tag.endswith("track"))
        values = {item.tag.rsplit("}", 1)[-1]: item.text for item in list(track)}
        self.assertEqual(values["creator"], "A & B")
        self.assertEqual(values["title"], "Song")
        self.assertEqual(values["duration"], "12400")
        self.assertTrue(values["location"].startswith("file://"))

    def test_xspf_from_other_players_resolves_entries(self):
        text = """<?xml version="1.0" encoding="UTF-8"?>
<playlist version="1" xmlns="http://xspf.org/ns/0/">
  <trackList>
    <track>
      <location>file:///music/a%20b.mp3</location>
      <location>https://example.com/fallback.mp3</location>
    </track>
    <track><location>sub/c.ogg</location></track>
    <track><location>https://example.com/stream</location></track>
  </trackList>
</playlist>
"""
        paths = parse_playlist_text(text, Path("/music"), ".xspf")
        self.assertEqual(
            paths,
            ["/music/a b.mp3", "/music/sub/c.ogg", "https://example.com/stream"],
        )

    def test_audpl_decodes_the_uri_and_ignores_metadata_lines(self):
        text = (
            "title=Road%20trip\n"
            "uri=file:///music/a%20b.mp3\n"
            "title=Song%20Name\n"
            "artist=The%20Band\n"
            "length=12400\n"
            "uri=https://example.com/a.ogg\n"
        )
        paths = parse_playlist_text(text, Path("/music"), ".audpl")
        self.assertEqual(paths, ["/music/a b.mp3", "https://example.com/a.ogg"])

    def test_broken_xspf_adds_nothing(self):
        self.assertEqual(parse_playlist_text("<playlist>", Path("/music"), ".xspf"), [])

    def test_save_suffix_follows_the_chosen_format(self):
        self.assertEqual(
            with_playlist_suffix("/music/list", "XSPF playlist (*.xspf)"),
            "/music/list.xspf",
        )
        self.assertEqual(
            with_playlist_suffix("/music/list.audpl", "M3U playlist (*.m3u *.m3u8)"),
            "/music/list.audpl",
        )

    def test_expand_paths_reads_an_xspf_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            song = root / "song.mp3"
            song.write_bytes(b"a")
            listing = root / "show.xspf"
            playlist = Playlist()
            playlist.insert(0, [str(song)])
            listing.write_text(playlist_text(playlist.tracks, ".xspf"), encoding="utf-8")
            found = expand_paths([str(listing)])
        self.assertEqual(found, [str(song.resolve())])

    def test_direct_station_urls_are_not_fetched(self):
        urls = [
            "http://mr-stream.mediaconnect.hu/4742/mr3hq.mp3",
            "https://stream.zeno.fm/k1arxzckpontv",
            "https://radio.example/live.m3u8",
        ]
        with patch("pyxmms.playlist.fetch_playlist_text") as fetch:
            found = expand_paths(urls)
        fetch.assert_not_called()
        self.assertEqual(found, urls)
        self.assertFalse(any(is_station_playlist(url) for url in urls))

    def test_shoutcast_m3u_expands_to_the_station(self):
        page = "http://salalondon.ddns.net:8002/;.m3u"
        body = "http://salalondon.ddns.net:8002/;\n"
        with patch("pyxmms.playlist.fetch_playlist_text", return_value=body):
            found = expand_paths([page])
        self.assertEqual(found, ["http://salalondon.ddns.net:8002/;"])

    def test_remote_playlist_resolves_a_relative_entry(self):
        page = "http://radio.example/lists/stations.pls"
        body = "[playlist]\nFile1=../live\nTitle1=Live\n"
        self.assertEqual(
            station_entries(page, body),
            ["http://radio.example/live"],
        )

    def test_hls_body_stays_one_stream(self):
        page = "http://radio.example/live.m3u"
        body = "#EXTM3U\n#EXT-X-TARGETDURATION:5\nseg0.ts\n"
        self.assertIsNone(station_entries(page, body))
        with patch("pyxmms.playlist.fetch_playlist_text", return_value=body):
            found = expand_paths([page])
        self.assertEqual(found, [page])

    def test_failed_playlist_fetch_keeps_the_address(self):
        page = "http://radio.example/stations.m3u"
        with patch("pyxmms.playlist.fetch_playlist_text", return_value=None):
            found = expand_paths([page])
        self.assertEqual(found, [page])

    def test_stream_title_replaces_the_address_on_the_row(self):
        playlist = Playlist()
        playlist.insert(0, ["http://radio.example/a"])
        self.assertFalse(playlist.note_stream_title("http://radio.example/a", "http://radio.example/a"))
        self.assertTrue(playlist.note_stream_title("http://radio.example/a", "Station - Song"))
        self.assertEqual(playlist.tracks[0].name, "Station - Song")
        self.assertFalse(playlist.note_stream_title("http://radio.example/a", "Station - Song"))

    def test_live_means_a_url_with_no_length(self):
        self.assertTrue(is_live_playback("http://radio.example/a", 0))
        self.assertFalse(is_live_playback("http://radio.example/a", 30))
        self.assertFalse(is_live_playback("/tmp/a.mp3", 0))
        self.assertEqual(media_suffix("http://host/movie.mp4?token=1"), ".mp4")
        self.assertEqual(media_suffix("http://host:8002/;.m3u"), ".m3u")

    def test_iptv_hls_opens_a_picture_and_stays_live(self):
        streams = [
            "http://45.186.106.207:8000/play/a09t/index.m3u8",
            "https://aegis-cloudfront-1.tubi.video/d6cbb0de-68e4-4f3b-82f9-bf5d526e0bde/index.m3u8",
        ]
        for url in streams:
            self.assertTrue(shows_picture(url), url)
            self.assertFalse(is_station_playlist(url), url)
            self.assertTrue(is_live_playback(url, 0), url)
            self.assertTrue(is_live_playback(url, 40), url)
        self.assertTrue(shows_picture("https://cdn.example/channel.mpd?token=1"))
        self.assertFalse(shows_picture("https://stream.zeno.fm/k1arxzckpontv"))
        self.assertTrue(shows_picture("http://host/movie.mp4?token=1"))
        self.assertTrue(shows_picture("https://cdn.example/index%2Em3u8"))
        self.assertTrue(shows_picture("https://cdn.example/live.m3u8s"))
        self.assertFalse(shows_picture("http://host/play/a0cn"))
        self.assertFalse(is_live_playback("https://cdn.example/movie.m3u8", 90 * 60))

    def test_extinf_title_keeps_commas_inside_quotes(self):
        text = (
            "#EXTM3U\n"
            '#EXTINF:-1 tvg-logo="https://cdn.example/a,b.png" '
            'group-title="News, World",Channel One\n'
            "#EXTVLCOPT:http-user-agent=Mozilla/5.0 (X11; Linux)\n"
            "#EXTVLCOPT:http-referrer=https://player.example/watch\n"
            "https://cdn.example/one/index.m3u8\n"
            "#EXTINF:-1,Channel Two\n"
            "https://cdn.example/two.ts\n"
        )
        entries = stream_entries(text, Path("."), ".m3u")
        self.assertEqual(entries[0].title, "Channel One")
        self.assertEqual(entries[0].path, "https://cdn.example/one/index.m3u8")
        self.assertEqual(entries[0].user_agent, "Mozilla/5.0 (X11; Linux)")
        self.assertEqual(entries[0].referrer, "https://player.example/watch")
        self.assertEqual(entries[1].title, "Channel Two")
        self.assertEqual(entries[1].user_agent, "")
        self.assertEqual(entries[1].referrer, "")
        self.assertEqual(entries[1].path, "https://cdn.example/two.ts")

    def test_extvlcopt_replaces_the_extinf_user_agent(self):
        text = (
            '#EXTINF:-1 http-user-agent="FromTag",Name\n'
            "#EXTVLCOPT:http-user-agent=FromOpt\n"
            "https://cdn.example/a.m3u8\n"
        )
        entries = stream_entries(text, Path("."), ".m3u")
        self.assertEqual(entries[0].title, "Name")
        self.assertEqual(entries[0].user_agent, "FromOpt")

    def test_direct_streams_skip_the_youtube_dl_hook(self):
        self.assertFalse(needs_ytdl("https://jmp2.uk/plu.m3u8"))
        self.assertFalse(needs_ytdl("http://158.101.222.193:88/georgia_play.php?id=1"))
        self.assertFalse(needs_ytdl("http://host/play/a0cn"))
        self.assertFalse(needs_ytdl("rtmp://example/live"))
        self.assertFalse(needs_ytdl("/tmp/a.mp3"))
        self.assertTrue(needs_ytdl("https://www.youtube.com/watch?v=abc"))
        self.assertTrue(needs_ytdl("https://youtu.be/abc"))
        self.assertTrue(needs_ytdl("https://music.youtube.com/watch?v=abc"))
        self.assertTrue(needs_ytdl("https://www.twitch.tv/somechannel"))

    def test_remote_channel_list_is_not_fetched_per_stream(self):
        page = "https://iptv.example/index.m3u"
        body = (
            "#EXTM3U\n"
            '#EXTINF:-1 http-user-agent="Agent",One\n'
            "https://cdn.example/a.m3u8\n"
            "https://cdn.example/b.mpd\n"
        )
        with patch("pyxmms.playlist.fetch_playlist_text", return_value=body) as fetch:
            entries = expand_entries([page])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(entries[0].title, "One")
        self.assertEqual(entries[0].user_agent, "Agent")
        self.assertEqual(entries[0].path, "https://cdn.example/a.m3u8")
        self.assertEqual(entries[1].path, "https://cdn.example/b.mpd")
        self.assertEqual(entries[1].user_agent, "")

    def test_local_hls_playlist_stays_one_file(self):
        with tempfile.TemporaryDirectory() as folder:
            listing = Path(folder) / "live.m3u8"
            listing.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:6\nseg.ts\n", encoding="utf-8")
            found = expand_paths([str(listing)])
        self.assertEqual(found, [str(listing.resolve())])

    def test_m3u_round_trip_keeps_channel_headers(self):
        playlist = Playlist()
        playlist.insert(0, [PlaylistEntry(
            "https://cdn.example/a.m3u8",
            title="Channel One",
            user_agent="Mozilla/5.0 (Test)",
            referrer="https://player.example/",
        )])
        self.assertEqual(playlist.tracks[0].name, "Channel One")
        entries = stream_entries(m3u_text(playlist.tracks), Path("."), ".m3u")
        self.assertEqual(entries[0].path, "https://cdn.example/a.m3u8")
        self.assertEqual(entries[0].title, "Channel One")
        self.assertEqual(entries[0].user_agent, "Mozilla/5.0 (Test)")
        self.assertEqual(entries[0].referrer, "https://player.example/")

    def test_saved_channel_keeps_its_headers(self):
        track = Playlist().insert(0, [PlaylistEntry(
            "https://cdn.example/a.m3u8",
            title="Channel One",
            user_agent="Agent",
            referrer="https://player.example/",
        )])[0]
        restored = track_from_config(track_record(track))
        self.assertEqual(restored.path, track.path)
        self.assertEqual(restored.title, "Channel One")
        self.assertEqual(restored.user_agent, "Agent")
        self.assertEqual(restored.referrer, "https://player.example/")
        plain = track_from_config("/tmp/a.mp3")
        self.assertEqual(plain.path, "/tmp/a.mp3")
        self.assertEqual(track_record(plain), "/tmp/a.mp3")
        self.assertIsNone(track_from_config({"nope": 1}))
        self.assertIsNone(track_from_config(""))

    def test_remote_list_drops_local_files(self):
        text = (
            "#EXTM3U\n"
            "file:///etc/passwd\n"
            "javascript:alert(1)\n"
            "rtmp://cdn.example/live\n"
            "https://cdn.example/a.m3u8\n"
        )
        entries = stream_entries(
            text, Path("/tmp"), ".m3u", base_url="https://iptv.example/index.m3u"
        )
        self.assertEqual(
            [entry.path for entry in entries],
            ["rtmp://cdn.example/live", "https://cdn.example/a.m3u8"],
        )

    def test_xspf_with_a_doctype_is_ignored(self):
        text = (
            '<?xml version="1.0"?>\n'
            '<!DOCTYPE playlist [<!ENTITY x "bomb">]>\n'
            "<playlist><trackList><track><location>&x;</location></track></trackList></playlist>\n"
        )
        self.assertEqual(parse_playlist_text(text, Path("/music"), ".xspf"), [])

    def test_channel_fields_are_one_line_and_clipped(self):
        title = "A" * 400
        text = f"#EXTINF:-1,{title}\nhttps://cdn.example/a.m3u8\n"
        entries = stream_entries(text, Path("."), ".m3u")
        self.assertEqual(len(entries[0].title), _MAX_TITLE)
        agent, refer = stream_headers("Agent\r\nX-Evil: 1", "https://ref.example/a")
        self.assertNotIn("\n", agent)
        self.assertEqual(agent, "Agent X-Evil: 1")
        self.assertEqual(refer, "https://ref.example/a")

    def test_remote_list_drops_a_split_url(self):
        text = (
            "#EXTM3U\n"
            "http://cdn.example/ok.mp3\n"
            "http://cdn.example/a.mp3\x00file:///etc/passwd\n"
        )
        entries = stream_entries(
            text, Path("/tmp"), ".m3u", base_url="https://iptv.example/index.m3u"
        )
        self.assertEqual([entry.path for entry in entries], ["http://cdn.example/ok.mp3"])

    def test_deep_xspf_is_ignored(self):
        text = "<playlist>" + ("<track>" * 2000) + ("</track>" * 2000) + "</playlist>"
        self.assertEqual(parse_playlist_text(text, Path("/music"), ".xspf"), [])

    def test_pasted_address_must_be_a_stream(self):
        self.assertTrue(is_stream_url("https://stream.example/a"))
        self.assertTrue(is_stream_url("rtmp://cdn.example/live"))
        self.assertFalse(is_stream_url("file:///etc/passwd"))
        self.assertFalse(is_stream_url("not a url"))
        self.assertFalse(is_stream_url("http://evil.example/a\nfile:///etc/passwd"))

    def test_saved_list_cannot_grow_an_extra_row(self):
        playlist = Playlist()
        playlist.insert(0, ["http://cdn.example/a.mp3\nfile:///etc/passwd"])
        lines = [line for line in m3u_text(playlist.tracks).splitlines() if line and not line.startswith("#")]
        self.assertEqual(lines, ["http://cdn.example/a.mp3 file:///etc/passwd"])

    def test_playlist_redirect_stays_on_http(self):
        handler = _HttpRedirect()
        request = urllib.request.Request("https://example.com/list.m3u")
        self.assertIsNone(handler.redirect_request(
            request, None, 302, "Found", {}, "file:///etc/passwd"
        ))
        followed = handler.redirect_request(
            request, None, 302, "Found", {}, "https://cdn.example/list.m3u"
        )
        self.assertEqual(followed.get_full_url(), "https://cdn.example/list.m3u")

    def test_stream_filename_does_not_replace_the_channel_name(self):
        playlist = Playlist()
        playlist.insert(0, ["https://cdn.example/index.m3u8"])
        playlist.tracks[0].title = "Channel One"
        self.assertFalse(playlist.note_stream_title(
            "https://cdn.example/index.m3u8", "index.m3u8"
        ))
        self.assertEqual(playlist.tracks[0].title, "Channel One")
        self.assertTrue(playlist.note_stream_title(
            "https://cdn.example/index.m3u8", "Channel One HD"
        ))


class FilterTests(unittest.TestCase):
    def test_bypass_when_flat_and_centered(self):
        self.assertEqual(build_af(True, 0, [0] * 10, 0), "")
        self.assertEqual(build_af(False, 6, [3] * 10, 0), "")

    def test_eq_and_balance(self):
        audio = build_af(True, 3, [0, 0, 2.2, 0, 0, 0, 0, 0, 0, 0], -1)
        self.assertIn("volume=3.00dB", audio)
        self.assertIn("equalizer=f=310", audio)
        self.assertIn("pan=stereo|c0=1.0000*c0|c1=0.0000*c1", audio)


if __name__ == "__main__":
    unittest.main()
