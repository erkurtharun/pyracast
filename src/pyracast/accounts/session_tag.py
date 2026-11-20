import secrets
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from pyracast.api.errors import coded, error_json
from pyracast.errors import ErrorCode

# The tag names one signed-in session across the key renewals of its confirmations: a sign-in
# issues a new one and a sign-out's flush drops it. It is an observation for the web client,
# never a credential, and travels only in a request header and the session read, never in a log
# line or an address.
TAG = "pyracast_session_tag"
HEADER = "X-Session-Tag"
SESSION_READ = "/api/session/"


def issue_tag(request: HttpRequest) -> None:
    request.session[TAG] = secrets.token_urlsafe(24)


def session_tag(request: HttpRequest) -> str | None:
    # A signed-in session stored before sessions carried a tag gets one at its first read.
    if not request.user.is_authenticated:
        return None
    if not isinstance(request.session.get(TAG), str):
        issue_tag(request)
    tag: str = request.session[TAG]
    return tag


def _held(request: HttpRequest) -> str:
    tag = request.session.get(TAG)
    return tag if isinstance(tag, str) else ""


class SessionTagCheck:
    # A request the web client prepared for another session than the one the browser now holds
    # is refused before any later middleware or view runs, so it changes nothing. The session
    # read is never refused: it is how a tab learns the session the browser holds.
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        carried = request.headers.get(HEADER)
        session_read = request.path == SESSION_READ and request.method in {"GET", "HEAD"}
        if carried is not None and not session_read and carried != _held(request):
            return error_json(request, lambda: coded(ErrorCode.SESSION_CHANGED, 409))
        return self.get_response(request)
