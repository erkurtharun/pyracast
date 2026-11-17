import hashlib
from collections.abc import Callable

from allauth.account import app_settings as account_settings
from allauth.account.adapter import get_adapter
from allauth.core import context
from allauth.core.internal import ratelimit
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.http import HttpRequest

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


def reserve_client_failure(request: HttpRequest) -> ratelimit.RateLimitUsage | None:
    return reserve(request, FAILED_LOGIN_FROM_CLIENT)


def reserve_address_failure(request: HttpRequest, key: str) -> ratelimit.RateLimitUsage | None:
    return reserve(request, FAILED_LOGIN, key)


def check_second_factor(check: Callable[[], str]) -> str:
    reservation = reserve_client_failure(context.request)
    if reservation is None:
        raise get_adapter().validation_error("too_many_login_attempts")
    try:
        code = check()
    except ValidationError as error:
        if error.code != "incorrect_code":
            reservation.rollback()
        raise
    reservation.rollback()
    return code


def _daily_rate() -> str:
    rates: list[str] = [
        part.strip() for part in account_settings.RATE_LIMITS[FAILED_LOGIN].split(",")
    ]
    return max(rates, key=lambda part: ratelimit.parse_rate(part).duration)


def _finished_failures_key(key: str) -> str:
    return "pyracast:login-failures:" + hashlib.sha256(key.encode()).hexdigest()


def record_address_failure(key: str, reservation: ratelimit.RateLimitUsage) -> None:
    # The daily slot of a check that ended as a failure; slots of checks in flight never count
    # towards a lock notice.
    daily = max(reservation.usage, key=lambda usage: usage.cache_duration, default=None)
    if daily is None:
        return
    record = _finished_failures_key(key)
    with ratelimit.cache_lock(record) as locked:
        if locked:
            recent = [
                timestamp
                for timestamp in cache.get(record, [])
                if timestamp > daily.timestamp - daily.cache_duration
            ]
            cache.set(record, [daily.timestamp, *recent], daily.cache_duration)


def claim_daily_lock_notice(request: HttpRequest, key: str) -> bool:
    """Return True once per daily lock of `key` by finished failures after the last notice."""
    daily = _daily_rate()
    if ratelimit.consume(
        request,
        app=APP,
        action=FAILED_LOGIN,
        config={FAILED_LOGIN: daily},
        key=key,
        dry_run=True,
    ):
        return False
    rate = ratelimit.parse_rate(daily)
    history: list[float] = cache.get(
        ratelimit.get_cache_key(request, app=APP, action=FAILED_LOGIN, rate=rate, key=key), []
    )
    finished = set(cache.get(_finished_failures_key(key), []))
    failures = [timestamp for timestamp in history if timestamp in finished]
    claim = "pyracast:login-locked-notice:" + hashlib.sha256(key.encode()).hexdigest()
    with ratelimit.cache_lock(claim) as locked:
        # The history is newest first; a claim holds the newest failure already announced.
        announced = cache.get(claim) if locked else None
        if not locked or len(failures) < rate.amount or (announced and failures[-1] <= announced):
            return False
        cache.set(claim, failures[0], rate.duration)
        return True
