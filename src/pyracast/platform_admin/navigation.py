from urllib.parse import quote, unquote, urlsplit

from django.http import HttpRequest
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

CONSOLE = "/admin/"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _plain(text: str) -> bool:
    return "\\" not in text and text.isprintable()


def _console_segments(path: str) -> bool:
    *inner, last = unquote(path).removeprefix("/").split("/")
    return all(inner) and not {".", ".."} & {*inner, last}


def console_path(request: HttpRequest, target: str | None) -> str | None:
    # A return address is only ever a console path on this origin: raw or percent-encoded
    # backslashes, unprintable characters, surrounding spaces, empty or dot segments could leave
    # it once a browser normalises the address.
    if not target or target != target.strip() or not (_plain(target) and _plain(unquote(target))):
        return None
    if not url_has_allowed_host_and_scheme(
        target, {request.get_host()}, require_https=request.is_secure()
    ):
        return None
    parts = urlsplit(target)
    if not parts.path.startswith(CONSOLE) or not _console_segments(parts.path):
        return None
    return parts.path + (f"?{parts.query}" if parts.query else "")


def console_destination(request: HttpRequest, target: str | None) -> str:
    return console_path(request, target) or reverse("admin:index")


def sign_in(target: str) -> str:
    return "/app/?next=" + quote(target, safe="/")
