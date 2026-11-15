import re
from collections.abc import Callable
from typing import Literal, cast

from django.conf import settings
from django.contrib.auth.models import AnonymousUser, User
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.utils import translation
from django.utils.cache import patch_vary_headers

from pyracast.accounts.models import UserLanguagePreference

Language = Literal["en", "tr"]
LANGUAGES = ("en", "tr")
LANGUAGE_COOKIE = "pyracast_language"
_LANGUAGE_RANGE = re.compile(
    r"\s*([a-z]{1,8}(?:-[a-z0-9]{1,8})*|\*)\s*"
    r"(?:;\s*q=(0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)\s*)?",
    re.IGNORECASE,
)


def explicit_language(value: str | None) -> Language | None:
    return cast(Language, value) if value in LANGUAGES else None


def user_language(user: User | AnonymousUser) -> Language | None:
    if not user.is_authenticated:
        return None
    return cast(
        Language | None,
        UserLanguagePreference.objects.filter(user_id=user.pk)
        .values_list("language", flat=True)
        .first(),
    )


@transaction.atomic
def save_user_language(user: User, language: Language) -> None:
    User.objects.select_for_update().get(pk=user.pk)
    UserLanguagePreference.objects.update_or_create(
        user_id=user.pk, defaults={"language": language}
    )


def set_language_cookie(response: HttpResponse, language: Language) -> None:
    response.set_cookie(
        LANGUAGE_COOKIE,
        language,
        max_age=365 * 24 * 60 * 60,
        secure=not settings.DEBUG,
        httponly=True,
        samesite="Lax",
    )


def browser_language(header: str) -> tuple[Language | None, frozenset[str]]:
    if len(header) > 8192:
        return None, frozenset({"*"})
    ranked: list[tuple[float, Language]] = []
    rejected: set[str] = set()
    for item in header.split(","):
        match = _LANGUAGE_RANGE.fullmatch(item)
        if match is None:
            continue
        tag, quality = match.groups()
        base = tag.lower().split("-", 1)[0]
        weight = float(quality) if quality is not None else 1.0
        if weight == 0:
            rejected.add(base)
        elif base in LANGUAGES:
            ranked.append((weight, cast(Language, base)))
    if ranked:
        return max(ranked, key=lambda candidate: candidate[0])[1], frozenset(rejected)
    return None, frozenset(rejected)


def request_language(request: HttpRequest) -> Language:
    preference = user_language(request.user)
    cookie = explicit_language(request.COOKIES.get(LANGUAGE_COOKIE))
    if preference is not None or cookie is not None:
        return preference or cast(Language, cookie)
    browser, rejected = browser_language(request.headers.get("Accept-Language", ""))
    if browser is not None:
        return browser
    country: Language = "tr" if request.META.get("HTTP_CF_IPCOUNTRY") == "TR" else "en"
    return country if country not in rejected and "*" not in rejected else "en"


class RequestLanguage:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request.LANGUAGE_CODE = request_language(request)
        with translation.override(request.LANGUAGE_CODE):
            response = self.get_response(request)
        response.headers["Content-Language"] = request.LANGUAGE_CODE
        patch_vary_headers(response, ("Accept-Language", "Cookie"))
        return response
