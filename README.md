# Media Tool

A small, local, single-page Django app that uses FFmpeg and yt-dlp to process media. It does not use Django REST Framework.

## Requirements

- Python 3.10 or later.
- FFmpeg and yt-dlp installed and available on `PATH`. FFmpeg must support AAC encoding.
- An internet connection for YouTube downloads.

## Run on Windows

From this directory in PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python manage.py runserver
```

Open http://127.0.0.1:8000/. The development server is intended for a **trusted local user only**; it has no authentication and must not be exposed to a network or the internet.

## Features

- **Cut media:** Select a local media file and use FFmpeg to extract AAC audio to M4A. Timestamps use `HH:MM:SS` (optional milliseconds): leave the end time blank to process the whole file, regardless of the start time, or leave the start time blank to trim from the beginning. Embedded cover artwork is preserved, while ordinary video is excluded. The result uses the selected file's name.
- **YouTube audio:** Paste a YouTube video or playlist URL. yt-dlp extracts M4A audio at its best audio-quality setting and embeds the thumbnail, with output named after the video title.
- **YouTube thumbnail:** Paste a YouTube video or playlist URL. yt-dlp downloads thumbnails without downloading the videos, using each video's title as the filename and preserving the image format. Playlist URLs download every thumbnail and may take a long time; use a video-only URL for one thumbnail. The completion notice appears after the entire playlist finishes.
- All operations save directly to the `outputs` folder.
- Existing outputs are never overwritten; a numbered suffix is added instead. The completion notice shows the absolute output paths.
- If `cookies.txt` exists next to `manage.py`, yt-dlp uses it as a Netscape-format cookies file. Without it, downloads are attempted without cookies. Cookies can contain sensitive account information: keep the file private and do not commit it.

The browser uploads a selected local file to the server for processing; browsers cannot directly give a web app arbitrary access to local paths. Temporary files are cleaned up after each operation. With JavaScript enabled, each form can run independently without leaving the page, with its own progress and result; up to three operations can run at once per server process. If the limit is reached, wait for an operation to finish and retry. Processing runs in the request, so keep the page open until it finishes. Refreshing or losing the connection may hide the result while the server continues processing; check `outputs` before retrying. Downloads or conversions that run longer than two hours time out. Concurrent yt-dlp operations use separate temporary copies of `cookies.txt`, so any cookie updates are not written back to the original file.

Run the tests with `.\.venv\Scripts\python manage.py test`.
