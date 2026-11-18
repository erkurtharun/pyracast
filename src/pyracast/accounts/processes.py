import json
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from allauth.account.internal.flows.email_verification_by_code import (
    EMAIL_VERIFICATION_CODE_SESSION_KEY,
    EmailVerificationProcess,
)
from allauth.account.internal.flows.password_reset_by_code import (
    PASSWORD_RESET_VERIFICATION_SESSION_KEY,
)
from allauth.account.internal.flows.reauthenticate_by_code import (
    REAUTHENTICATION_CODE_SESSION_KEY,
)
from allauth.account.internal.stagekit import LOGIN_SESSION_KEY, clear_login
from allauth.account.internal.userkit import str_to_user_id
from allauth.account.models import EmailAddress
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.headless.base.views import APIView
from allauth.mfa.models import Authenticator
from allauth.socialaccount.models import SocialAccount
from django.contrib.auth.models import User
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.utils.crypto import salted_hmac
from django.utils.http import base36_to_int

from pyracast.accounts.security import end_weaker_reauthentication

PROVIDER_SIGNUP_SESSION_KEY = "socialaccount_sociallogin"
STAMP = "pyracast_authority"
# Authenticator data allauth rewrites when a recovery code is used or the codes are shown; each
# code is checked against the current row when it is entered.
USE_MARKERS = frozenset({"used_mask", "viewed_at"})
_identified: ContextVar[set[Any] | None] = ContextVar("pyracast_identified", default=None)


class AuthorityChangedError(Exception):
    pass


def account_state(user_id: Any, *, lock: bool = False) -> str | None:
    # The credential state of an account as one keyed digest, never logged or returned to a
    # client; None when the account is gone. Locking follows the account lock order: its
    # addresses, its provider identities, the account row, then its authenticators, each in id
    # order. The locks block changes and deletion of these rows but let other rows reference the
    # account; the writers that add a credential row lock the account row first.
    addresses = EmailAddress.objects.filter(user_id=user_id).order_by("pk")
    identities = SocialAccount.objects.filter(user_id=user_id).order_by("pk")
    accounts = User.objects.filter(pk=user_id)
    authenticators = Authenticator.objects.filter(user_id=user_id).order_by("pk")
    if lock:
        addresses = addresses.select_for_update(no_key=True)
        identities = identities.select_for_update(no_key=True)
        accounts = accounts.select_for_update(no_key=True)
        authenticators = authenticators.select_for_update(no_key=True)
        if (identified := _identified.get()) is not None:
            identified.add(user_id)
    content: list[Any] = [
        list(addresses.values_list("pk", "email", "verified", "primary")),
        list(identities.values_list("pk", "provider", "uid")),
    ]
    try:
        content.append(
            accounts.values_list(
                "pk", "password", "is_active", "email", "is_staff", "is_superuser"
            ).get()
        )
    except User.DoesNotExist:
        return None
    content.append(
        [
            (pk, kind, {key: value for key, value in data.items() if key not in USE_MARKERS})
            for pk, kind, data in authenticators.values_list("pk", "type", "data")
        ]
    )
    serialized = json.dumps(content, sort_keys=True, default=str)
    return salted_hmac(
        "pyracast.accounts.account-state", serialized, algorithm="sha256"
    ).hexdigest()


def lock_account(user_id: Any) -> None:
    account_state(user_id, lock=True)


def identifying() -> bool:
    return _identified.get() is not None


def identified_accounts() -> frozenset[Any]:
    return frozenset(_identified.get() or ())


def code_account(state: dict[str, Any]) -> Any:
    user_id = state.get("user_id")
    return None if user_id is None else str_to_user_id(user_id)


def login_account(stash: dict[str, Any]) -> Any:
    user_pk = stash.get("user_pk")
    return None if user_pk is None else base36_to_int(user_pk)


def own_state(value: dict[str, Any]) -> dict[str, Any]:
    return value


def nested_state(value: dict[str, Any]) -> dict[str, Any]:
    state: dict[str, Any] = value.setdefault("state", {})
    return state


def forget(key: str) -> Callable[[HttpRequest, dict[str, Any]], None]:
    def discard(request: HttpRequest, _value: dict[str, Any]) -> None:
        request.session.pop(key, None)

    return discard


def abort_verification(request: HttpRequest, state: dict[str, Any]) -> None:
    EmailVerificationProcess(request=request, state=state).abort()


def abort_login(request: HttpRequest, _value: dict[str, Any]) -> None:
    clear_login(request)


@dataclass(frozen=True)
class PendingProcess:
    session_key: str
    holder: Callable[[dict[str, Any]], dict[str, Any]]
    account: Callable[[dict[str, Any]], Any]
    discard: Callable[[HttpRequest, dict[str, Any]], None]


# A process that names an account holds while the account's credential state equals the one its
# stamp recorded; any change of that state ends every such process of the account.
PROCESSES = (
    PendingProcess(
        PASSWORD_RESET_VERIFICATION_SESSION_KEY,
        own_state,
        code_account,
        forget(PASSWORD_RESET_VERIFICATION_SESSION_KEY),
    ),
    PendingProcess(
        EMAIL_VERIFICATION_CODE_SESSION_KEY, own_state, code_account, abort_verification
    ),
    PendingProcess(
        REAUTHENTICATION_CODE_SESSION_KEY,
        own_state,
        code_account,
        forget(REAUTHENTICATION_CODE_SESSION_KEY),
    ),
    PendingProcess(LOGIN_SESSION_KEY, nested_state, login_account, abort_login),
)
BY_KEY = {process.session_key: process for process in PROCESSES}


def pending(request: HttpRequest, process: PendingProcess) -> dict[str, Any] | None:
    value = request.session.get(process.session_key)
    return value if isinstance(value, dict) else None


def holds(process: PendingProcess, value: dict[str, Any], *, lock: bool = False) -> bool:
    account = process.account(value)
    if account is None:
        return True
    state = account_state(account, lock=lock)
    return state is not None and process.holder(value).get(STAMP) == [account, state]


def stamp(request: HttpRequest, process: PendingProcess, value: dict[str, Any]) -> None:
    account = process.account(value)
    state = account_state(account, lock=True)
    if state is None:
        process.discard(request, value)
    else:
        process.holder(value)[STAMP] = [account, state]
        request.session.modified = True


def require_authority(request: HttpRequest, session_key: str) -> None:
    # Completions call this inside their transaction, after claiming the address row they verify:
    # it locks the account's rows, so a credential change of the account commits either before
    # the check or after the completion.
    process = BY_KEY[session_key]
    value = pending(request, process)
    if value is None or not holds(process, value, lock=True):
        raise AuthorityChangedError


def continuing(request: HttpRequest, *session_keys: str) -> None:
    # A completion that changed its account's state itself carries the steps it continues over.
    for key in session_keys:
        process = BY_KEY[key]
        if (value := pending(request, process)) is not None and process.account(value) is not None:
            stamp(request, process, value)


def discard_stale(request: HttpRequest) -> None:
    for process in PROCESSES:
        value = pending(request, process)
        if value is not None and not holds(process, value):
            process.discard(request, value)


def stamp_started(request: HttpRequest, identified: set[Any]) -> None:
    # A process rests on the state of an account its starting request locked when it identified
    # it; one naming any other account never holds.
    for process in PROCESSES:
        value = pending(request, process)
        if value is None or STAMP in process.holder(value):
            continue
        account = process.account(value)
        if account is None:
            continue
        if account in identified:
            stamp(request, process, value)
        else:
            process.discard(request, value)


def _refused[R](start: Callable[[], R]) -> R | ImmediateHttpResponse:
    try:
        return start()
    except ImmediateHttpResponse as refusal:
        return refusal


def starting[R](request: HttpRequest, start: Callable[[], R]) -> R:
    # Every request that starts a pending process runs in one transaction from before its input
    # is validated: the lookups that identify an account lock its rows, the processes it starts
    # are stamped from them before the commit, and their code mail leaves only with the commit.
    # A step allauth refuses by raising still commits the work before it.
    identified: set[Any] = set()
    token = _identified.set(identified)
    try:
        with transaction.atomic():
            outcome = _refused(start)
            stamp_started(request, identified)
    finally:
        _identified.reset(token)
    if isinstance(outcome, ImmediateHttpResponse):
        raise outcome
    return outcome


class StartingView(APIView):
    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if request.method != "POST":
            return super().handle(request, *args, **kwargs)
        return starting(request, lambda: super(StartingView, self).handle(request, *args, **kwargs))


class PendingProcessAuthority:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.session.session_key is not None:
            discard_stale(request)
            end_weaker_reauthentication(request)
        return self.get_response(request)
