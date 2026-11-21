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
from pyracast.background.failures import queue_full
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
    LEASE_LOST = "lease_lost"


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
    """Leases each due account until `now + LEASE`; the account's message carries that lease."""
    with transaction.atomic():
        due = list(
            ProviderAccount.objects.select_for_update(skip_locked=True)
            .filter(revalidation_due_at__lte=now)
            .order_by("revalidation_due_at", "pk")
            .values_list("pk", flat=True)
        )
        ProviderAccount.objects.filter(pk__in=due).update(revalidation_due_at=now + LEASE)
    return due


def _take_lease(account_id: int, claim: datetime, now: datetime) -> ProviderAccount | None:
    # Another run, a newer dispatch, a back-off or a key replacement has moved the due time.
    with transaction.atomic():
        account = (
            ProviderAccount.objects.select_for_update()
            .filter(pk=account_id, revalidation_due_at=claim)
            .first()
        )
        if account is None:
            return None
        # Strictly later than the claim, so a duplicate message with the same claim never matches.
        account.revalidation_due_at = max(now + LEASE, claim + timedelta(microseconds=1))
        account.save(update_fields=("revalidation_due_at",))
    return account


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


def _lost(account: ProviderAccount) -> RevalidationOutcome | None:
    revision, lease = ProviderAccount.objects.values_list(
        "credential_revision", "revalidation_due_at"
    ).get(pk=account.pk)
    if revision != account.credential_revision:
        return RevalidationOutcome.STOPPED
    if lease != account.revalidation_due_at:
        return RevalidationOutcome.LEASE_LOST
    return None


def _key_replaced(account: ProviderAccount) -> bool:
    return not ProviderAccount.objects.filter(
        pk=account.pk, credential_revision=account.credential_revision
    ).exists()


def _finish(
    account: ProviderAccount, outcome: RevalidationOutcome, due: datetime | None = None
) -> RevalidationOutcome:
    # A key replaced meanwhile keeps the due time its replacement set.
    ProviderAccount.objects.filter(
        pk=account.pk,
        credential_revision=account.credential_revision,
        revalidation_due_at=account.revalidation_due_at,
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
    claim: datetime,
    adapter_for: Callable[[SiteBinding], ProviderAdapter],
) -> RevalidationOutcome:
    """Validates each approved plant of the account with its current key, as the operator who
    replaced it, within the request budget left after the account's forecasts. `claim` is the
    lease the dispatcher took for this run; a run whose lease moved on does nothing."""
    clock = Clock(now)
    account = _take_lease(account_id, claim, now)
    if account is None:
        return RevalidationOutcome.LEASE_LOST
    bindings = list(stale_bindings(account))
    operator = _replacing_operator(account) if bindings else None
    for index, binding in enumerate(bindings):
        if lost := _lost(account):
            return lost
        moment = clock.now()
        if index and clock.elapsed() >= LAST_REQUEST_START.total_seconds():
            # The next dispatch claims the account again; the actor's time limit is never met.
            return _finish(account, RevalidationOutcome.WAITING, moment)
        try:
            if operator is None:
                raise PermissionDenied
            validate_binding(
                operator,
                binding.pk,
                adapter_for(binding),
                moment,
                credential_revision=account.credential_revision,
            )
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
            # The newer key is revalidated by the run its replacement queued.
            if _key_replaced(account):
                return RevalidationOutcome.STOPPED
    return _finish(account, RevalidationOutcome.DONE)


@fresh_connections
def deliver_revalidation(account_id: int, lease: str | None = None) -> None:
    # A message without the lease of its dispatch runs nothing; the lease ends and is sent again.
    outcome = (
        RevalidationOutcome.LEASE_LOST
        if lease is None
        else revalidate_account(
            account_id,
            timezone.now(),
            claim=datetime.fromisoformat(lease),
            adapter_for=providers.adapter_for,
        )
    )
    logger.info("provider_revalidation account_id=%s outcome=%s", account_id, outcome)


def dispatch_revalidations(actor: Actor[[int], None]) -> int:
    if queue_full(actor, logger):
        return 0
    now = timezone.now()
    due = claim_due_revalidations(now)
    lease = now + LEASE
    for index, account_id in enumerate(due):
        try:
            actor.send_with_options(args=(account_id,), kwargs={"lease": lease.isoformat()})
        except Exception:
            # An unpublished claim is due again at the next dispatch instead of after its lease.
            ProviderAccount.objects.filter(pk__in=due[index:], revalidation_due_at=lease).update(
                revalidation_due_at=now
            )
            raise
    return len(due)
