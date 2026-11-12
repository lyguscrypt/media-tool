import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, SimpleTestCase, override_settings
from django.urls import reverse

from .forms import CutForm, DownloadForm
from .services import MediaError, download_audio, download_thumbnail, save_output
from .views import MAX_CONCURRENT_OPERATIONS, operation_slots


class FormTests(SimpleTestCase):
    def test_cut_validates_times_and_file(self):
        upload = SimpleUploadedFile("clip.mp4", b"video")
        form = CutForm(
            {"start_time": "00:01:30.250", "end_time": "00:02:00"},
            {"media_file": upload},
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_cut_optional_times(self):
        for start, end, valid in [
            ("", "", True),
            ("00:00:01", "", True),
            ("", "00:00:02", True),
            ("", "00:00:00", False),
            ("invalid", "", False),
            ("", "invalid", False),
        ]:
            with self.subTest(start=start, end=end):
                form = CutForm(
                    {"start_time": start, "end_time": end},
                    {"media_file": SimpleUploadedFile("clip.mp4", b"video")},
                )
                self.assertEqual(form.is_valid(), valid, form.errors)

    def test_cut_rejects_bad_or_reversed_times(self):
        for start, end in [("00:60:00", "01:00:00"), ("00:01:00", "00:01:00"), ("1", "2")]:
            with self.subTest(start=start, end=end):
                form = CutForm(
                    {"start_time": start, "end_time": end},
                    {"media_file": SimpleUploadedFile("clip.mp4", b"video")},
                )
                self.assertFalse(form.is_valid())

    def test_download_accepts_youtube_and_rejects_other_hosts(self):
        for url, valid in [
            ("https://www.youtube.com/watch?v=abc", True),
            ("https://youtu.be/abc", True),
            ("https://youtube.com.evil.example/watch?v=abc", False),
            ("https://user@youtube.com/watch?v=abc", False),
        ]:
            with self.subTest(url=url):
                form = DownloadForm({"url": url})
                self.assertEqual(form.is_valid(), valid)


class MediaTests(SimpleTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.override = override_settings(OUTPUT_DIR=self.root / "outputs", BASE_DIR=self.root)
        self.override.enable()
        self.addCleanup(self.override.disable)

    def test_save_avoids_overwriting(self):
        source = self.root / "clip.m4a"
        source.write_bytes(b"first")
        first = save_output(source)
        source.write_bytes(b"second")
        second = save_output(source)
        self.assertEqual(first["name"], "clip.m4a")
        self.assertEqual(second["name"], "clip (1).m4a")
        self.assertEqual((self.root / "outputs" / first["name"]).read_bytes(), b"first")
        self.assertEqual((self.root / "outputs" / second["name"]).read_bytes(), b"second")

    def test_concurrent_save_avoids_overwriting(self):
        source = self.root / "clip.m4a"
        source.write_bytes(b"audio")
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(save_output, [source] * 3))
        self.assertEqual({result["name"] for result in results}, {
            "clip.m4a", "clip (1).m4a", "clip (2).m4a",
        })
        for result in results:
            self.assertEqual((self.root / "outputs" / result["name"]).read_bytes(), b"audio")

    def test_download_command_and_results(self):
        (self.root / "cookies.txt").write_text("cookies")

        def fake_command(command):
            self.assertIn("--cookies", command)
            cookie_copy = Path(command[command.index("--cookies") + 1])
            self.assertNotEqual(cookie_copy, self.root / "cookies.txt")
            self.assertEqual(cookie_copy.read_text(), "cookies")
            cookie_copy.write_text("updated")
            self.assertIn("--embed-thumbnail", command)
            self.assertEqual(command[-2:], ["--", "https://youtu.be/abc"])
            template = Path(command[command.index("-o") + 1])
            (template.parent / "Video title.m4a").write_bytes(b"audio")

        with patch("media.services.run_command", side_effect=fake_command):
            results = download_audio("https://youtu.be/abc")
        self.assertEqual(results, [{"name": "Video title.m4a"}])
        self.assertEqual((self.root / "outputs" / "Video title.m4a").read_bytes(), b"audio")
        self.assertEqual((self.root / "cookies.txt").read_text(), "cookies")

    def test_thumbnail_download_command_and_playlist_results(self):
        (self.root / "cookies.txt").write_text("cookies")
        output = self.root / "outputs"
        output.mkdir()
        (output / "Video title.webp").write_bytes(b"existing")

        def fake_command(command):
            self.assertIn("--skip-download", command)
            self.assertIn("--write-thumbnail", command)
            self.assertIn("--ignore-config", command)
            self.assertIn("--lazy-playlist", command)
            self.assertEqual(command[command.index("--trim-filenames") + 1], "120")
            self.assertIn("--cookies", command)
            self.assertEqual(command[-2:], ["--", "https://youtu.be/abc"])
            template = Path(command[command.index("-o") + 1])
            self.assertEqual(template.name, "%(title)s.%(ext)s")
            self.assertEqual(template.parent.name, "%(autonumber)06d")
            for index, title, content in [
                (1, "Video title", b"first"),
                (2, "Video title", b"second"),
                (3, "Another video", b"third"),
            ]:
                folder = template.parent.parent / f"{index:06d}"
                folder.mkdir()
                (folder / f"{title}.webp").write_bytes(content)

        with patch("media.services.run_command", side_effect=fake_command):
            results = download_thumbnail("https://youtu.be/abc")
        self.assertEqual(results, [
            {"name": "Video title (1).webp"},
            {"name": "Video title (2).webp"},
            {"name": "Another video.webp"},
        ])
        self.assertEqual((output / "Video title.webp").read_bytes(), b"existing")
        self.assertEqual((output / "Video title (1).webp").read_bytes(), b"first")
        self.assertEqual((output / "Video title (2).webp").read_bytes(), b"second")
        self.assertEqual((output / "Another video.webp").read_bytes(), b"third")

    def test_thumbnail_missing_or_unsupported_output(self):
        with patch("media.services.run_command") as runner:
            with self.assertRaisesMessage(MediaError, "without producing a thumbnail"):
                download_thumbnail("https://youtu.be/abc")
        self.assertNotIn("--cookies", runner.call_args.args[0])

        def fake_command(command):
            template = Path(command[command.index("-o") + 1])
            folder = template.parent.parent / "000001"
            folder.mkdir()
            (folder / "Video title.unknown").write_bytes(b"image")

        with patch("media.services.run_command", side_effect=fake_command):
            with self.assertRaisesMessage(MediaError, "unsupported image format"):
                download_thumbnail("https://youtu.be/abc")

    def test_thumbnail_post_redirects_and_shows_paths(self):
        with patch("media.views.download_thumbnail", return_value=[
            {"name": "Video title.webp"}, {"name": "Another video.jpg"},
        ]) as downloader:
            response = self.client.post("/", {
                "action": "thumbnail", "thumbnail-url": "https://youtu.be/abc",
            }, follow=True)
            self.assertEqual(len(response.redirect_chain), 1)
            self.assertContains(response, str(self.root / "outputs" / "Video title.webp"))
            self.assertContains(response, str(self.root / "outputs" / "Another video.jpg"))
            self.assertNotContains(self.client.get("/"), "Processing complete")
            downloader.assert_called_once_with(url="https://youtu.be/abc")

    def test_thumbnail_rejects_invalid_url_and_shows_errors(self):
        with patch("media.views.download_thumbnail") as downloader:
            response = self.client.post("/", {
                "action": "thumbnail", "thumbnail-url": "https://example.com/video",
            })
            self.assertContains(response, "Enter a valid YouTube video or playlist URL.")
            downloader.assert_not_called()
        with patch("media.views.download_thumbnail", side_effect=MediaError("thumbnail failed")):
            response = self.client.post("/", {
                "action": "thumbnail", "thumbnail-url": "https://youtu.be/abc",
            })
            self.assertContains(response, "thumbnail failed")
            self.assertNotContains(response, "Processing complete")

    def test_cut_post_selects_file_and_reports_result(self):
        def fake_cut(media_file, start_time, end_time):
            self.assertEqual(media_file.name, "my recording.mp4")
            self.assertEqual((start_time, end_time), ("00:00:01", "00:00:02"))
            return [{"name": "my recording.m4a"}]

        with patch("media.views.cut_audio", autospec=True, side_effect=fake_cut) as cut:
            response = self.client.post("/", {
                "action": "cut",
                "cut-media_file": SimpleUploadedFile("my recording.mp4", b"video"),
                "cut-start_time": "00:00:01",
                "cut-end_time": "00:00:02",
            }, follow=True)
            cut.assert_called_once()
        self.assertEqual(len(response.redirect_chain), 1)
        self.assertContains(response, "Processing complete")
        self.assertContains(response, str(self.root / "outputs" / "my recording.m4a"))
        self.assertNotContains(response, "Ready to listen")
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertNotContains(self.client.get("/"), "Processing complete")

    def test_download_without_cookies_and_missing_output(self):
        with patch("media.services.run_command") as runner:
            with self.assertRaisesMessage(MediaError, "without producing an M4A"):
                download_audio("https://youtu.be/abc")
        self.assertNotIn("--cookies", runner.call_args.args[0])

    def test_failed_command_is_shown(self):
        with patch("media.views.download_audio", side_effect=MediaError("yt-dlp failed")):
            response = self.client.post("/", {
                "action": "download", "download-url": "https://youtu.be/abc",
            })
        self.assertContains(response, "yt-dlp failed")
        self.assertNotContains(response, "Ready to listen")

    def test_page_and_output_download(self):
        page = self.client.get("/")
        self.assertContains(page, "Cut media")
        self.assertContains(page, 'class="processing" hidden', count=3)
        self.assertContains(page, 'role="progressbar"', count=3)
        self.assertContains(page, "YouTube thumbnail")
        output = self.root / "outputs" / "clip.m4a"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"audio")
        response = self.client.get(reverse("media:output", args=["clip.m4a"]))
        self.assertEqual(b"".join(response.streaming_content), b"audio")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(self.client.get("/outputs/root/clip.m4a/").status_code, 404)
        image = output.parent / "1.webp"
        image.write_bytes(b"image")
        response = self.client.get(reverse("media:output", args=["1.webp"]))
        self.assertEqual(b"".join(response.streaming_content), b"image")
        self.assertEqual(self.client.get(reverse("media:output", args=["file.txt"])).status_code, 404)

    def test_async_response_validation_and_errors(self):
        headers = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}
        with patch("media.views.download_thumbnail", return_value=[{"name": "video.webp"}]):
            response = self.client.post("/", {
                "action": "thumbnail", "thumbnail-url": "https://youtu.be/abc",
            }, **headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "output_paths": [str(self.root / "outputs" / "video.webp")],
        })
        self.assertNotIn("media_output_results", response.cookies)

        with patch("media.views.download_thumbnail") as downloader:
            response = self.client.post("/", {
                "action": "thumbnail", "thumbnail-url": "https://example.com/",
            }, **headers)
            self.assertEqual(response.status_code, 400)
            self.assertIn("YouTube URL", response.json()["error"])
            downloader.assert_not_called()
        with patch("media.views.download_thumbnail", side_effect=MediaError("download failed")):
            response = self.client.post("/", {
                "action": "thumbnail", "thumbnail-url": "https://youtu.be/abc",
            }, **headers)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"], "download failed")
        response = self.client.post("/", {"action": "invalid"}, **headers)
        self.assertEqual(response.status_code, 400)

    def test_operations_run_concurrently_and_enforce_limit(self):
        started = threading.Barrier(MAX_CONCURRENT_OPERATIONS + 1)
        release = threading.Event()

        def fake_download(url):
            started.wait(timeout=10)
            if not release.wait(timeout=10):
                raise MediaError("Test operation timed out.")
            return [{"name": f"{url.rsplit('/', 1)[-1]}.m4a"}]

        def submit(index):
            return Client().post("/", {
                "action": "download", "download-url": f"https://youtu.be/{index}",
            }, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        with patch("media.views.download_audio", side_effect=fake_download):
            with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_OPERATIONS) as executor:
                futures = [executor.submit(submit, index) for index in range(MAX_CONCURRENT_OPERATIONS)]
                try:
                    started.wait(timeout=10)
                    blocked = submit("extra")
                    self.assertEqual(blocked.status_code, 429)
                    self.assertIn("Too many operations", blocked.json()["error"])
                finally:
                    release.set()
                responses = [future.result(timeout=10) for future in futures]
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(len({response.json()["output_paths"][0] for response in responses}), 3)
        with patch("media.views.download_audio", return_value=[{"name": "after.m4a"}]):
            self.assertEqual(submit("after").status_code, 200)

    def test_operation_slot_is_released_on_error(self):
        with patch("media.views.download_audio", side_effect=MediaError("failed")):
            response = self.client.post("/", {
                "action": "download", "download-url": "https://youtu.be/abc",
            }, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 400)
        acquired = 0
        try:
            for _ in range(MAX_CONCURRENT_OPERATIONS):
                if operation_slots.acquire(blocking=False):
                    acquired += 1
            self.assertEqual(acquired, MAX_CONCURRENT_OPERATIONS)
        finally:
            for _ in range(acquired):
                operation_slots.release()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and ffprobe required")
    def test_real_ffmpeg_optional_timestamps(self):
        source = self.root / "source.wav"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3", str(source)],
            check=True,
        )
        for name, start, end, expected in [
            ("whole", "", "", 3),
            ("ignore-start", "00:00:01", "", 3),
            ("from-beginning", "", "00:00:02", 2),
        ]:
            with self.subTest(name=name):
                response = self.client.post("/", {
                    "action": "cut",
                    "cut-media_file": SimpleUploadedFile(f"{name}.wav", source.read_bytes()),
                    "cut-start_time": start,
                    "cut-end_time": end,
                }, follow=True)
                self.assertContains(response, "Processing complete")
                output = self.root / "outputs" / f"{name}.m4a"
                duration = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(output)],
                    check=True, capture_output=True, text=True,
                )
                self.assertAlmostEqual(float(duration.stdout.strip()), expected, delta=0.1)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and ffprobe required")
    def test_real_ffmpeg_cut_preserves_embedded_artwork(self):
        image = self.root / "cover.ppm"
        image.write_bytes(b"P6\n2 2\n255\n" + b"\xff\x00\x00" * 4)
        source = self.root / "source.m4a"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
             "-i", str(image), "-map", "0:a", "-map", "1:v", "-c:a", "aac",
             "-c:v", "mjpeg", "-disposition:v", "attached_pic", str(source)],
            check=True,
        )
        response = self.client.post("/", {
            "action": "cut",
            "cut-media_file": SimpleUploadedFile("artwork.m4a", source.read_bytes()),
            "cut-start_time": "00:00:01",
            "cut-end_time": "00:00:02",
        }, follow=True)
        self.assertContains(response, "Processing complete")
        output = self.root / "outputs" / "artwork.m4a"
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)],
            check=True, capture_output=True, text=True,
        )
        streams = json.loads(probe.stdout)["streams"]
        artwork = [stream for stream in streams if stream["disposition"]["attached_pic"]]
        self.assertEqual(len(artwork), 1)
        self.assertEqual(artwork[0]["codec_name"], "mjpeg")
        for path in (source, output):
            image_data = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0",
                 "-c", "copy", "-f", "image2pipe", "pipe:1"],
                check=True, capture_output=True,
            ).stdout
            if path == source:
                source_image = image_data
            else:
                self.assertEqual(image_data, source_image)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and ffprobe required")
    def test_real_ffmpeg_cut_excludes_regular_video(self):
        source = self.root / "video.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
             "-f", "lavfi", "-i", "color=red:size=16x16:duration=3",
             "-c:a", "aac", "-c:v", "mpeg4", str(source)],
            check=True,
        )
        response = self.client.post("/", {
            "action": "cut",
            "cut-media_file": SimpleUploadedFile("video.mp4", source.read_bytes()),
            "cut-start_time": "00:00:01",
            "cut-end_time": "00:00:02",
        }, follow=True)
        self.assertContains(response, "Processing complete")
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json",
             str(self.root / "outputs" / "video.m4a")],
            check=True, capture_output=True, text=True,
        )
        self.assertEqual([stream["codec_type"] for stream in json.loads(probe.stdout)["streams"]], ["audio"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and ffprobe required")
    def test_real_ffmpeg_cut_uses_input_name_and_timestamps(self):
        source = self.root / "source.wav"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3", str(source)],
            check=True,
        )
        response = self.client.post("/", {
            "action": "cut",
            "cut-media_file": SimpleUploadedFile("input.m4a", source.read_bytes()),
            "cut-start_time": "00:00:01",
            "cut-end_time": "00:00:02",
        }, follow=True)
        self.assertContains(response, "Processing complete")
        self.assertContains(response, str(self.root / "outputs" / "input.m4a"))
        self.assertContains(response, "input.m4a")
        output = self.root / "outputs" / "input.m4a"
        duration = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(output)],
            check=True, capture_output=True, text=True,
        )
        self.assertAlmostEqual(float(duration.stdout.strip()), 1, delta=0.1)
