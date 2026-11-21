import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone
from dramatiq import Actor

from pyracast import providers
from pyracast.background.actor import fresh_connections
from pyracast.background.clock import Clock
from pyracast.background.deadlines import ACTORS, LAST_REQUEST_START
from pyracast.background.failures import queue_full
from pyracast.connections.contracts import ProviderAdapter, ProviderError
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.budget import approved_bindings
from pyracast.forecasts.models import CollectionSchedule
from pyracast.forecasts.quota import (
    ForecastBudgetReservedError,
    QuotaExceededError,
    RequestDeferredError,
)
from pyracast.forecasts.timing import forecast_timing, next_forecast_slot
from pyracast.forecasts.validation import refuses_binding, validate_binding
from pyracast.regions.models import AuditAction, AuditEvent

logger = logging.getLogger(__name__)

ACTOR = "revalidate_provider_account"
LEASE = ACTORS[ACTOR].lease
RETRY_DELAY = timedelta(minutes=15)
# Slots sit on whole minutes, so a run due this long after a slot stays in the slot's UTC day.
AFTER_THE_SLOT = timedelta(seconds=30)


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
        approved_bindings(account)
        .exclude(validated_credential_revision=account.credential_revision)
        .order_by("validated_at", "pk")
    )


def _after_the_next_slot(account: ProviderAccount, moment: datetime) -> datetime:
    """A reserve refused now may fit right after the account's next slot: a waiting plant's own
    need falls there when its earlier due slot is superseded, and what a collecting plant used
    and still needs stays the same across its slot. At the latest on the next UTC day."""
    due = _next_utc_day(moment)
    for binding in approved_bindings(account).select_related("region", "provider_account"):
        timing = forecast_timing(
            binding, CollectionSchedule.objects.filter(region_id=binding.region_id).first()
        )
        slot = (
            None
            if timing is None
            else next_forecast_slot(
                moment, binding.region.timezone, timing.times, timing.effective_from
            )
        )
        if slot is not None:
            due = min(due, slot + AFTER_THE_SLOT)
    return due


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
    replaced it, within the requests the current plants and its own forecasts leave. `claim` is
    the lease the dispatcher took for this run; a run whose lease moved on does nothing."""
    clock = Clock(now)
    account = _take_lease(account_id, claim, now)
    if account is None:
        return RevalidationOutcome.LEASE_LOST
    bindings = list(stale_bindings(account))
    operator = _replacing_operator(account) if bindings else None
    first_reserved: datetime | None = None
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
        except ForecastBudgetReservedError:
            # A later plant may need less than this one; the run tries it in the same order.
            if first_reserved is None:
                first_reserved = moment
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
    if first_reserved is not None:
        # The room a refusal waits for comes after the slot following it, which later plants of
        # the run may already have passed.
        due = max(clock.now(), _after_the_next_slot(account, first_reserved))
        return _finish(account, RevalidationOutcome.WAITING, due)
    return _finish(account, RevalidationOutcome.DONE)


def queue_revalidation(binding_id: int, now: datetime) -> None:
    """Makes the account's run due now when this approved binding awaits revalidation with the
    account's key and no run is due earlier. The caller holds the binding's region lock (lock
    order region, then account); a run holding the lease loses it, and the next dispatch takes
    the plant."""
    account_id = SiteBinding.objects.values_list("provider_account_id", flat=True).get(
        pk=binding_id
    )
    with transaction.atomic():
        account = ProviderAccount.objects.select_for_update().get(pk=account_id)
        if stale_bindings(account).filter(pk=binding_id).exists():
            ProviderAccount.objects.filter(
                Q(revalidation_due_at__isnull=True) | Q(revalidation_due_at__gt=now),
                pk=account.pk,
            ).update(revalidation_due_at=now)


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
