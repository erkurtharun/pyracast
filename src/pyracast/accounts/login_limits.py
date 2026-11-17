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
FAILED_LOGIN_FROM_CLIENT = "login_failed_ip"


def reserve_client_failure(request: HttpRequest) -> ratelimit.RateLimitUsage | None:
    # A credential check takes its failure slot before it runs, so concurrent checks from one
    # client never exceed the allowance; the caller rolls the slot back when the check succeeds.
    return ratelimit.consume(request, app=APP, action=FAILED_LOGIN_FROM_CLIENT)


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


def claim_daily_lock_notice(request: HttpRequest, key: str) -> bool:
    """Return True once for each daily lock of `key` whose failures all followed the last notice."""
    rates = [part.strip() for part in account_settings.RATE_LIMITS["login_failed"].split(",")]
    daily = max(rates, key=lambda part: ratelimit.parse_rate(part).duration)
    if ratelimit.consume(
        request,
        app=APP,
        action="login_failed",
        config={"login_failed": daily},
        key=key,
        dry_run=True,
    ):
        return False
    rate = ratelimit.parse_rate(daily)
    history: list[float] = cache.get(
        ratelimit.get_cache_key(request, app=APP, action="login_failed", rate=rate, key=key), []
    )
    claim = "pyracast:login-locked-notice:" + hashlib.sha256(key.encode()).hexdigest()
    with ratelimit.cache_lock(claim) as locked:
        # The history is newest first; a claim holds the newest failure already announced.
        announced = cache.get(claim) if locked else None
        if not locked or len(history) < rate.amount or (announced and history[-1] <= announced):
            return False
        cache.set(claim, history[0], rate.duration)
        return True
