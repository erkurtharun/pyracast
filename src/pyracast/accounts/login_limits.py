import time
from collections.abc import Callable
from typing import Any

from allauth.account import app_settings as account_settings
from allauth.account.adapter import get_adapter
from allauth.core import context
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.core.internal import ratelimit
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.http import HttpRequest

from pyracast.accounts.audit import SecurityEvent, record_security_event

APP = "allauth.account"
FAILED_LOGIN = "login_failed"
FAILED_LOGIN_FROM_CLIENT = "login_failed_ip"
# The longest a request runs: gunicorn's `timeout` (deploy/gunicorn_conf.py), which the gateway's
# response header timeout equals. An older reservation belongs to a check that never ended.
REQUEST_CEILING_SECONDS = 90


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
    # A mail that spent the window and failed or may not have arrived lets the person ask again at
    # once: only the shortest per-address window is cleared, the longer ones still count the mail.
    # The rates are per key, so no request is needed.
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


def _daily_rate() -> ratelimit.Rate | None:
    rates = ratelimit.parse_rates(account_settings.RATE_LIMITS.get(FAILED_LOGIN))
    return max(rates, key=lambda rate: rate.duration, default=None)


def _history_key(request: HttpRequest, key: str, rate: ratelimit.Rate) -> str:
    return ratelimit.get_cache_key(request, app=APP, action=FAILED_LOGIN, rate=rate, key=key)


def failure_history(request: HttpRequest, key: str) -> str | None:
    """The cache key of the daily failure history of an attempted address."""
    rate = _daily_rate()
    return None if rate is None else _history_key(request, key, rate)


def _finished_key(cache_key: str, moment: float) -> str:
    return f"{cache_key}:finished:{moment!r}"


def _finished(cache_key: str, moments: list[float]) -> list[bool]:
    found = cache.get_many([_finished_key(cache_key, moment) for moment in moments])
    return [_finished_key(cache_key, moment) in found for moment in moments]


def _forget_unfinished(history_key: str, duration: float) -> None:
    cutoff = time.time() - REQUEST_CEILING_SECONDS

    def unfinished(history: list[float]) -> set[float]:
        old = [moment for moment in history if moment < cutoff]
        if not old:
            return set()
        return {
            moment
            for moment, done in zip(old, _finished(history_key, old), strict=True)
            if not done
        }

    if not unfinished(cache.get(history_key, [])):
        return
    with ratelimit.cache_lock(history_key) as locked:
        history: list[float] = cache.get(history_key, []) if locked else []
        if forgotten := unfinished(history):
            kept = [moment for moment in history if moment not in forgotten]
            cache.set(history_key, kept, duration)


def reserve_address_failure(request: HttpRequest, key: str) -> ratelimit.RateLimitUsage | None:
    # A reservation is not a failure: one that never finished stops counting toward the lock
    # once it is older than any request.
    rate = _daily_rate()
    if rate is not None:
        _forget_unfinished(_history_key(request, key, rate), rate.duration)
    return reserve(request, FAILED_LOGIN, key)


def finish_failure(usage: ratelimit.RateLimitUsage) -> str | None:
    """Records the reserved check as a finished failure and returns its daily history key."""
    if not usage.usage:
        return None
    slot = max(usage.usage, key=lambda item: item.cache_duration)
    cache.set(_finished_key(slot.cache_key, slot.timestamp), True, slot.cache_duration)
    return slot.cache_key


def locked_on_finished_failures(history_key: str) -> bool:
    """Whether the finished failures in a daily history fill the allowance of its address.

    Every entry counts, however many share a time; a check still in flight never counts, and a
    password reset clears the history, so failures after it form a new lock.
    """
    rate = _daily_rate()
    if rate is None:
        return False
    since = time.time() - rate.duration
    history = [moment for moment in cache.get(history_key, []) if moment > since]
    return len(history) >= rate.amount and sum(_finished(history_key, history)) >= rate.amount


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
