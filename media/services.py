import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

THUMBNAIL_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".bmp"}
OUTPUT_EXTENSIONS = THUMBNAIL_EXTENSIONS | {".m4a"}


class MediaError(Exception):
    pass


def run_command(command):
    try:
        subprocess.run(
            command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", timeout=7200,
        )
    except FileNotFoundError as exc:
        logger.exception("Media executable not found")
        raise MediaError(f"{command[0]} was not found. Install it and add it to PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        logger.exception("Media command timed out")
        raise MediaError("The operation timed out after two hours.") from exc
    except subprocess.CalledProcessError as exc:
        logger.error("Media command failed: %s", exc.stderr)
        lines = (exc.stderr or "").strip().splitlines()
        detail = lines[-1] if lines else "No error details were provided."
        raise MediaError(f"{command[0]} failed: {detail}") from exc


def save_output(source):
    folder = settings.OUTPUT_DIR
    folder.mkdir(parents=True, exist_ok=True)
    stem, suffix = source.stem, source.suffix
    for index in range(10000):
        name = f"{stem}{f' ({index})' if index else ''}{suffix}"
        target = folder / name
        try:
            with target.open("xb") as output:
                try:
                    with source.open("rb") as input_file:
                        shutil.copyfileobj(input_file, output)
                except OSError:
                    output.close()
                    target.unlink(missing_ok=True)
                    raise
        except FileExistsError:
            continue
        return {"name": name}
    raise MediaError("Could not find an available output filename.")


def cut_audio(media_file, start_time, end_time):
    # Keep the original extension for tools that use it to identify the format.
    original_name = Path(media_file.name.replace("\\", "/")).name
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(original_name).stem).strip(" .")
    stem = (stem or "audio")[:120]
    with tempfile.TemporaryDirectory(prefix="media-tool-") as directory:
        temporary = Path(directory)
        input_directory = temporary / "input"
        input_directory.mkdir()
        input_path = input_directory / ("source" + Path(original_name).suffix[:20])
        output_path = temporary / f"{stem}.m4a"
        with input_path.open("wb") as file:
            for chunk in media_file.chunks():
                file.write(chunk)
        command = ["ffmpeg", "-nostdin"]
        if end_time:
            command.extend(["-ss", start_time or "00:00:00", "-to", end_time])
        # Read artwork from an untrimmed input so seeking cannot discard its packet.
        command.extend([
            "-i", str(input_path), "-i", str(input_path),
            "-map", "0:a:0", "-map", "1:v:disp:attached_pic?",
            "-c:a", "aac", "-c:v", "copy", str(output_path),
        ])
        run_command(command)
        return [save_output(output_path)]


def run_ytdlp(command, url):
    cookies = settings.BASE_DIR / "cookies.txt"
    if cookies.is_file():
        with tempfile.TemporaryDirectory(prefix="media-tool-cookies-") as directory:
            cookie_copy = Path(directory) / "cookies.txt"
            shutil.copyfile(cookies, cookie_copy)
            run_command([*command, "--cookies", str(cookie_copy), "--", url])
    else:
        run_command([*command, "--", url])


def download_audio(url):
    with tempfile.TemporaryDirectory(prefix="media-tool-") as directory:
        temporary = Path(directory)
        command = [
            "yt-dlp", "--ignore-config", "--windows-filenames", "--trim-filenames", "120",
            "--audio-quality", "0", "--extract-audio", "--audio-format", "m4a",
            "--embed-thumbnail", "-o", str(temporary / "%(title)s.%(ext)s"),
        ]
        run_ytdlp(command, url)
        files = sorted(temporary.glob("*.m4a"))
        if not files:
            raise MediaError("yt-dlp completed without producing an M4A file.")
        return [save_output(file) for file in files]


def download_thumbnail(url):
    with tempfile.TemporaryDirectory(prefix="media-tool-") as directory:
        temporary = Path(directory)
        command = [
            "yt-dlp", "--ignore-config", "--windows-filenames", "--trim-filenames", "120",
            "--lazy-playlist", "--skip-download", "--write-thumbnail",
            "-o", str(temporary / "%(autonumber)06d" / "%(title)s.%(ext)s"),
        ]
        run_ytdlp(command, url)
        files = sorted(path for path in temporary.rglob("*") if path.is_file())
        if not files:
            raise MediaError("yt-dlp completed without producing a thumbnail.")
        if any(file.suffix.lower() not in THUMBNAIL_EXTENSIONS for file in files):
            raise MediaError("yt-dlp produced a thumbnail in an unsupported image format.")
        return [save_output(file) for file in files]
