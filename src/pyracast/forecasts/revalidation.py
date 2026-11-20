import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F, QuerySet
from django.utils import timezone
from dramatiq import Actor

from pyracast import providers
from pyracast.background.actor import fresh_connections
from pyracast.background.clock import Clock
from pyracast.background.deadlines import ACTORS, LAST_REQUEST_START
from pyracast.connections.contracts import ProviderAdapter, ProviderError
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.quota import QuotaExceededError, RequestDeferredError
from pyracast.forecasts.validation import refuses_binding, validate_binding
from pyracast.regions.models import AuditAction, AuditEvent, RegionStatus, active_region

logger = logging.getLogger(__name__)

ACTOR = "revalidate_provider_account"
LEASE = ACTORS[ACTOR].lease
RETRY_DELAY = timedelta(minutes=15)


class RevalidationOutcome(StrEnum):
    DONE = "done"
    WAITING = "waiting"
    KEY_REJECTED = "key_rejected"
    STOPPED = "stopped"


def stale_bindings(account: ProviderAccount) -> QuerySet[SiteBinding]:
    """Approved plants of the account still validated with an earlier key, longest validated
    first."""
    return (
        SiteBinding.objects.filter(
            active_region("region__"),
            provider_account=account,
            region__status=RegionStatus.APPROVED,
            region__approved_binding=F("pk"),
            region__revision=F("region_revision"),
        )
        .exclude(validated_credential_revision=account.credential_revision)
        .order_by("validated_at", "pk")
    )


def claim_due_revalidations(now: datetime) -> list[int]:
    """Leases each due account so that the dispatcher sends it once per lease."""
    with transaction.atomic():
        due = list(
            ProviderAccount.objects.select_for_update(skip_locked=True)
            .filter(revalidation_due_at__lte=now)
            .order_by("revalidation_due_at", "pk")
            .values_list("pk", flat=True)
        )
        ProviderAccount.objects.filter(pk__in=due).update(revalidation_due_at=now + LEASE)
    return due


def _replacing_operator(account: ProviderAccount) -> User | None:
    event = (
        AuditEvent.objects.filter(
            provider_account=account,
            action=AuditAction.PROVIDER_SECRET_ROTATED,
            revision=account.credential_revision,
        )
        .select_related("actor")
        .order_by("-pk")
        .first()
    )
    return event.actor if event else None


def _finish(
    account: ProviderAccount, outcome: RevalidationOutcome, due: datetime | None = None
) -> RevalidationOutcome:
    # A key replaced meanwhile keeps the due time its replacement set.
    ProviderAccount.objects.filter(
        pk=account.pk, credential_revision=account.credential_revision
    ).update(revalidation_due_at=due)
    return outcome


def _next_utc_day(moment: datetime) -> datetime:
    return datetime.combine(
        moment.astimezone(UTC).date() + timedelta(days=1), datetime.min.time(), UTC
    )


def revalidate_account(
    account_id: int,
    now: datetime,
    *,
    adapter_for: Callable[[SiteBinding], ProviderAdapter],
) -> RevalidationOutcome:
    """Validates each approved plant of the account with its current key, as the operator who
    replaced it, within the request budget left after the account's forecasts."""
    clock = Clock(now)
    account = ProviderAccount.objects.get(pk=account_id)
    bindings = list(stale_bindings(account))
    operator = _replacing_operator(account) if bindings else None
    for index, binding in enumerate(bindings):
        moment = clock.now()
        if index and clock.elapsed() >= LAST_REQUEST_START.total_seconds():
            # The next dispatch claims the account again; the actor's time limit is never met.
            return _finish(account, RevalidationOutcome.WAITING, moment)
        try:
            if operator is None:
                raise PermissionDenied
            validate_binding(operator, binding.pk, adapter_for(binding), moment)
        except PermissionDenied:
            return _finish(account, RevalidationOutcome.STOPPED)
        except RequestDeferredError as deferred:
            if deferred.available_at is None:
                return _finish(account, RevalidationOutcome.KEY_REJECTED)
            return _finish(account, RevalidationOutcome.WAITING, deferred.available_at)
        except QuotaExceededError:
            return _finish(account, RevalidationOutcome.WAITING, _next_utc_day(moment))
        except ProviderError as error:
            if error.status_code == 401:
                return _finish(account, RevalidationOutcome.KEY_REJECTED)
            if not refuses_binding(error):
                return _finish(account, RevalidationOutcome.WAITING, moment + RETRY_DELAY)
        except ValidationError:
            continue
    return _finish(account, RevalidationOutcome.DONE)


@fresh_connections
def deliver_revalidation(account_id: int) -> None:
    outcome = revalidate_account(account_id, timezone.now(), adapter_for=providers.adapter_for)
    logger.info("provider_revalidation account_id=%s outcome=%s", account_id, outcome)


def dispatch_revalidations(actor: Actor[[int], None]) -> int:
    due = claim_due_revalidations(timezone.now())
    for account_id in due:
        actor.send(account_id)
    return len(due)
