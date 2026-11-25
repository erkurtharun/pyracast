from collections.abc import Callable
from typing import cast

from django.conf import settings
from django.contrib.auth.models import AnonymousUser, User
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.utils import translation
from django.utils.cache import patch_vary_headers

from pyracast.accounts.models import UserLanguagePreference
from pyracast.errors import UNEXPECTED, DomainError, ErrorCode
from pyracast.languages import LANGUAGE_COOKIE, LANGUAGES, Language, negotiated_language


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
    if language not in LANGUAGES:
        raise DomainError(ErrorCode.INVALID_REQUEST, UNEXPECTED, field="language")
    User.objects.select_for_update(no_key=True).get(pk=user.pk)
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


def request_language(request: HttpRequest, preference: Language | None) -> Language:
    if preference is not None:
        return preference
    return negotiated_language(
        request.COOKIES.get(LANGUAGE_COOKIE),
        request.headers.get("Accept-Language", ""),
        request.META.get("HTTP_CF_IPCOUNTRY"),
    )


class RequestLanguage:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        preference = user_language(request.user)
        request.LANGUAGE_CODE = request_language(request, preference)
        with translation.override(request.LANGUAGE_CODE):
            response = self.get_response(request)
        if (
            preference is not None
            and preference != request.COOKIES.get(LANGUAGE_COOKIE)
            and LANGUAGE_COOKIE not in response.cookies
        ):
            set_language_cookie(response, preference)
        response.headers["Content-Language"] = request.LANGUAGE_CODE
        patch_vary_headers(response, ("Accept-Language", "Cookie"))
        return response
