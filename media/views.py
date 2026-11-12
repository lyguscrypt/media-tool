import logging
from pathlib import Path
from threading import BoundedSemaphore

from django.conf import settings
from django.core import signing
from django.core.signing import BadSignature
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods, require_safe

from .forms import CutForm, DownloadForm
from .services import OUTPUT_EXTENSIONS, MediaError, cut_audio, download_audio, download_thumbnail

logger = logging.getLogger(__name__)
RESULT_COOKIE = "media_output_results"
RESULT_COOKIE_MAX_AGE = 300
MAX_CONCURRENT_OPERATIONS = 3
operation_slots = BoundedSemaphore(MAX_CONCURRENT_OPERATIONS)


def output_paths(results):
    return [str((settings.OUTPUT_DIR / result["name"]).resolve()) for result in results]


@require_http_methods(["GET", "POST"])
def index(request):
    cut_form = CutForm(prefix="cut")
    download_form = DownloadForm(prefix="download")
    thumbnail_form = DownloadForm(prefix="thumbnail")
    results = []
    error = None
    error_status = 400
    active = "cut"
    if request.method == "POST":
        wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        active = request.POST.get("action", "")
        form = None
        operation = None
        if active == "cut":
            cut_form = form = CutForm(request.POST, request.FILES, prefix="cut")
            operation = cut_audio
        elif active == "download":
            download_form = form = DownloadForm(request.POST, prefix="download")
            operation = download_audio
        elif active == "thumbnail":
            thumbnail_form = form = DownloadForm(request.POST, prefix="thumbnail")
            operation = download_thumbnail
        else:
            error = "Choose a valid action."

        if form is not None and form.is_valid():
            if not operation_slots.acquire(blocking=False):
                error = "Too many operations are running. Wait for one to finish and try again."
                error_status = 429
            else:
                try:
                    results = operation(**form.cleaned_data)
                except MediaError as exc:
                    error = str(exc)
                except OSError:
                    logger.exception("Could not read or save media")
                    error = "Could not read or save the media file. Check the server logs and folder permissions."
                finally:
                    operation_slots.release()

        if wants_json:
            if error:
                return JsonResponse({"error": error}, status=error_status)
            if form is not None and form.errors:
                errors = [
                    f"{form.fields[field].label}: {message}" if field in form.fields else str(message)
                    for field, messages in form.errors.items() for message in messages
                ]
                return JsonResponse({"error": " ".join(errors)}, status=400)
            if not results:
                logger.error("Media operation %s returned no results", active)
                return JsonResponse({"error": "The operation completed without producing a file."}, status=500)
            return JsonResponse({"output_paths": output_paths(results)})

        if results:
            response = redirect("media:index")
            response.set_cookie(
                RESULT_COOKIE,
                signing.dumps([result["name"] for result in results], salt=RESULT_COOKIE),
                max_age=RESULT_COOKIE_MAX_AGE,
                httponly=True,
                secure=request.is_secure(),
                samesite="Lax",
            )
            return response
    elif result_cookie := request.COOKIES.get(RESULT_COOKIE):
        try:
            names = signing.loads(
                result_cookie, salt=RESULT_COOKIE, max_age=RESULT_COOKIE_MAX_AGE,
            )
            if isinstance(names, list):
                results = [
                    {"name": name} for name in names
                    if isinstance(name, str)
                    and Path(name).name == name
                    and Path(name).suffix.lower() in OUTPUT_EXTENSIONS
                ]
        except BadSignature:
            logger.info("Ignoring invalid or expired output result cookie")

    response = render(request, "media/index.html", {
        "cut_form": cut_form, "download_form": download_form, "thumbnail_form": thumbnail_form,
        "results": results,
        "output_paths": output_paths(results),
        "error": error, "active": active,
    })
    if request.method == "GET" and RESULT_COOKIE in request.COOKIES:
        response.delete_cookie(RESULT_COOKIE, samesite="Lax")
    return response


@require_safe
def output_file(request, name):
    if Path(name).name != name or Path(name).suffix.lower() not in OUTPUT_EXTENSIONS:
        raise Http404
    try:
        file = (settings.OUTPUT_DIR / name).open("rb")
    except FileNotFoundError as exc:
        raise Http404 from exc
    return FileResponse(file, as_attachment=True, filename=name)
