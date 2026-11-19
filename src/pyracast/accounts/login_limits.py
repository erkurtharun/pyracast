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


def reserve_client_failure(request: HttpRequest) -> ratelimit.RateLimitUsage | None:
    return reserve(request, FAILED_LOGIN_FROM_CLIENT)


def reserve_address_failure(request: HttpRequest, key: str) -> ratelimit.RateLimitUsage | None:
    return reserve(request, FAILED_LOGIN, key)


def daily_lock_reached(usage: ratelimit.RateLimitUsage) -> bool:
    """True once for each daily lock of an address: when this finished failure is the last of
    the daily allowance and every failure of that allowance came after the last announced one.

    Only finished failures are recorded here, so a check still in flight (whose reservation
    allauth's history already holds) never completes a lock. A password reset clears allauth's
    history, which drops the earlier failures from the count.
    """
    if not usage.usage:
        return False
    allowance = max(
        ratelimit.parse_rates(account_settings.RATE_LIMITS.get(FAILED_LOGIN)),
        key=lambda rate: rate.duration,
    ).amount
    slot = max(usage.usage, key=lambda item: item.cache_duration)
    record_key = f"{slot.cache_key}:finished"
    with ratelimit.cache_lock(record_key) as locked:
        if not locked:
            return False
        history = set(cache.get(slot.cache_key, []))
        record = cache.get(record_key) or {"failures": [], "announced": 0.0}
        finished = [moment for moment in record["failures"] if moment in history]
        failures = [*finished, slot.timestamp][-allowance:]
        reached = len(failures) == allowance and failures[0] > record["announced"]
        announced = failures[-1] if reached else record["announced"]
        cache.set(record_key, {"failures": failures, "announced": announced}, slot.cache_duration)
    return reached


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
