import hashlib
from collections.abc import Callable
from functools import partial
from typing import Any

from allauth.account import app_settings as account_settings
from allauth.account.adapter import get_adapter
from allauth.core import context
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.core.internal import ratelimit
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpRequest

from pyracast.accounts.audit import SecurityEvent, record_security_event

APP = "allauth.account"
FAILED_LOGIN = "login_failed"
FAILED_LOGIN_FROM_CLIENT = "login_failed_ip"


def reserve(
    request: HttpRequest, action: str, key: str | None = None
) -> ratelimit.RateLimitUsage | None:
    # A check takes its failure slots before it runs, so concurrent checks never exceed an
    # allowance. A refused attempt keeps none of the slots it took (allauth's consume keeps the
    # earlier rates); the caller releases them when the check succeeds.
    reservation = ratelimit.RateLimitUsage(usage=[])
    rates: str = account_settings.RATE_LIMITS.get(action) or ""
    for rate in filter(None, (part.strip() for part in rates.split(","))):
        usage = ratelimit.consume(request, app=APP, action=action, config={action: rate}, key=key)
        if usage is None:
            reservation.rollback()
            return None
        reservation.usage.extend(usage.usage)
    return reservation


def spent(request: HttpRequest, action: str, **scope: Any) -> bool:
    # Whether a budget refuses now, without using it: a start asks before it locks an account and
    # the budget is still used where allauth uses it.
    return ratelimit.consume(request, app=APP, action=action, dry_run=True, **scope) is None


def throttled(request: HttpRequest) -> ImmediateHttpResponse:
    return ImmediateHttpResponse(ratelimit.respond_429(request))


def release_code_resend(address: str) -> None:
    # A code mail whose delivery is unknown lets the person ask for a new code at once: only the
    # shortest per-address window is cleared, the longer ones still count the mail that may have
    # arrived. The rates are per key, so no request is needed.
    rates = (account_settings.RATE_LIMITS.get("confirm_email") or "").split(",")
    if parts := [part.strip() for part in rates if part.strip()]:
        shortest = min(parts, key=lambda rate: ratelimit.parse_rate(rate).duration)
        ratelimit.clear(
            HttpRequest(),
            app=APP,
            action="confirm_email",
            config={"confirm_email": shortest},
            key=address.lower(),
        )


def reserve_client_failure(request: HttpRequest) -> ratelimit.RateLimitUsage | None:
    return reserve(request, FAILED_LOGIN_FROM_CLIENT)


def reserve_address_failure(request: HttpRequest, key: str) -> ratelimit.RateLimitUsage | None:
    return reserve(request, FAILED_LOGIN, key)


def _finished_key(cache_key: str, moment: float) -> str:
    return f"{cache_key}:finished:{moment!r}"


def record_finished_failure(
    usage: ratelimit.RateLimitUsage, announce: Callable[[str], None]
) -> None:
    """Calls `announce` with the key of a daily lock of an address once every hit of the daily
    allowance is a finished failure and the oldest came after the last announced lock.

    Each finished failure is its own entry, so no lock is contested and every finishing
    failure of a lock reaches the same decision; `announce` stores at most one notice per key.
    The lock is recorded as announced once the notice has committed. A check still in flight (its
    hit already in allauth's history) never completes a lock, and a password reset clears the
    history, so failures after it form a new lock.
    """
    if not usage.usage:
        return
    allowance = max(
        ratelimit.parse_rates(account_settings.RATE_LIMITS.get(FAILED_LOGIN)),
        key=lambda rate: rate.duration,
    ).amount
    slot = max(usage.usage, key=lambda item: item.cache_duration)
    cache.set(_finished_key(slot.cache_key, slot.timestamp), True, slot.cache_duration)
    history: list[float] = cache.get(slot.cache_key, [])
    if len(history) < allowance:
        return
    finished = cache.get_many([_finished_key(slot.cache_key, moment) for moment in history])
    announced_key = f"{slot.cache_key}:announced"
    if len(finished) < len(history) or min(history) <= cache.get(announced_key, 0.0):
        return
    newest = max(history)
    announce(hashlib.sha256(f"{slot.cache_key}:{newest!r}".encode()).hexdigest())
    transaction.on_commit(partial(cache.set, announced_key, newest, slot.cache_duration))


def check_second_factor(
    user: User,
    check: Callable[[], str],
    *,
    refusal: str = "too_many_login_attempts",
    per_client: bool = True,
) -> str:
    # Replaces allauth's per-user code limiter (`mfa.base.internal.flows.check_rate_limit`) on the
    # same key and rates, because its consume keeps the 10-minute hit when the daily rate refuses.
    # A success clears the user's key, as allauth does.
    request = context.request
    key = f"mfa-auth-user-{user.pk}"
    client = reserve_client_failure(request) if per_client else ratelimit.RateLimitUsage(usage=[])
    account = reserve(request, FAILED_LOGIN, key) if client is not None else None
    if client is None or account is None:
        if client is not None:
            client.rollback()
        record_security_event(SecurityEvent.SECOND_FACTOR_LOCKED, request=request, user_id=user.pk)
        raise get_adapter().validation_error(refusal)
    try:
        code = check()
    except ValidationError as error:
        if error.code != "incorrect_code":
            client.rollback()
            account.rollback()
        raise
    client.rollback()
    ratelimit.clear(request, app=APP, action=FAILED_LOGIN, key=key)
    return code
