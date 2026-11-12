import re
from urllib.parse import urlsplit

from django import forms


TIME_PATTERN = re.compile(r"^(\d+):([0-5]\d):([0-5]\d)(?:\.(\d{1,3}))?$")


def seconds(value):
    match = TIME_PATTERN.fullmatch(value)
    if not match:
        raise forms.ValidationError("Use hours:minutes:seconds, for example 00:01:30.")
    hours, minutes, whole_seconds, fraction = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(whole_seconds) + int(
        (fraction or "").ljust(3, "0")
    ) / 1000


class CutForm(forms.Form):
    media_file = forms.FileField(label="Media file")
    start_time = forms.CharField(label="Start time (optional)", max_length=32, required=False)
    end_time = forms.CharField(label="End time (optional)", max_length=32, required=False)

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("start_time")
        end = cleaned.get("end_time")
        start_seconds = 0 if not start else None
        end_seconds = None
        if start:
            try:
                start_seconds = seconds(start)
            except forms.ValidationError as exc:
                self.add_error("start_time", exc)
        if end:
            try:
                end_seconds = seconds(end)
            except forms.ValidationError as exc:
                self.add_error("end_time", exc)
        if start_seconds is not None and end_seconds is not None and end_seconds <= start_seconds:
            self.add_error("end_time", "End time must be later than start time.")
        return cleaned


class DownloadForm(forms.Form):
    url = forms.URLField(label="YouTube URL", max_length=2048)

    def clean_url(self):
        url = self.cleaned_data["url"]
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if (
            parts.scheme not in ("http", "https")
            or parts.username is not None
            or parts.password is not None
            or host not in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be")
            or parts.port not in (None, 80, 443)
        ):
            raise forms.ValidationError("Enter a valid YouTube video or playlist URL.")
        return url
