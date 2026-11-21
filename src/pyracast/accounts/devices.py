import time
from typing import TYPE_CHECKING, Any, cast

from allauth.account.internal.flows.logout import logout
from allauth.core import ratelimit
from allauth.headless.base.response import AuthenticationResponse
from allauth.headless.base.views import AuthenticatedAPIView
from allauth.headless.internal.restkit.inputs import Input
from allauth.headless.internal.restkit.response import APIResponse
from allauth.headless.usersessions.inputs import SelectSessionsInput
from allauth.usersessions.models import UserSession
from django import forms
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseBase

from pyracast.accounts.session_cookie import SessionStore
from pyracast.accounts.sessions import end_sessions, expiry_reason

if TYPE_CHECKING:
    SessionsField = forms.ModelMultipleChoiceField[UserSession]
else:
    SessionsField = forms.ModelMultipleChoiceField

ENDED_BY_OWNER = "ended_by_owner"


class OwnSessionsField(SessionsField):
    # A selection is a non-empty list of integer session ids within the key's range, never
    # coerced. Another device may end a listed session before this request: an id that names no
    # session of this account selects nothing, whether it ended or belongs to another account.
    def clean(self, value: Any) -> QuerySet[UserSession]:
        if value in self.empty_values:
            raise ValidationError(self.error_messages["required"], code="required")
        if not isinstance(value, list):
            raise ValidationError(self.error_messages["invalid_list"], code="invalid_list")
        low, high = connection.ops.integer_field_range(UserSession._meta.pk.get_internal_type())
        for item in value:
            if isinstance(item, bool) or not isinstance(item, int) or not low <= item <= high:
                raise ValidationError(
                    self.error_messages["invalid_pk_value"],
                    code="invalid_pk_value",
                    params={"pk": item},
                )
        self.run_validators(value)
        return cast(QuerySet[UserSession], self.queryset).filter(pk__in=set(value))


class OwnSessionsInput(SelectSessionsInput):
    sessions = OwnSessionsField(queryset=UserSession.objects.none())


def live_sessions(user: User) -> list[UserSession]:
    # Sessions past one of the Pyracast limits end at their next request; they are not listed.
    now = time.time()
    return [
        session
        for session in UserSession.objects.purge_and_list(user)
        if expiry_reason(SessionStore(session.session_key), now) is None
    ]


def session_data(session: UserSession, current: str | None) -> dict[str, Any]:
    return {
        "id": session.pk,
        "ip": session.ip,
        "user_agent": session.user_agent,
        "created_at": session.created_at.timestamp(),
        "last_seen_at": session.last_seen_at.timestamp(),
        "is_current": session.session_key == current,
    }


class SessionsView(AuthenticatedAPIView):
    def get_input_class(self) -> type[Input] | None:
        return OwnSessionsInput if self.request.method == "DELETE" else None

    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        limited = ratelimit.consume_or_429(
            request, action="manage_sessions", user=request.user, limit_get=True
        )
        return limited or super().handle(request, *args, **kwargs)

    def get_input_kwargs(self) -> dict[str, Any]:
        return {"user": self.request.user}

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return self.listed(request)

    def delete(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        user = cast(User, request.user)
        keys = {session.session_key for session in self.input.cleaned_data["sessions"]}
        current = request.session.session_key
        end_sessions(user.pk, ENDED_BY_OWNER, keep=current, only=keys, request=request)
        if current in keys:
            logout(request)
            return AuthenticationResponse(request)
        return self.listed(request)

    @staticmethod
    def listed(request: HttpRequest) -> APIResponse:
        current = request.session.session_key
        sessions = live_sessions(cast(User, request.user))
        return APIResponse(request, data=[session_data(session, current) for session in sessions])
