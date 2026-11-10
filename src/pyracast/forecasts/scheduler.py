from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from pyracast.forecasts.jobs import binding_is_current
from pyracast.forecasts.models import CollectionJob, CollectionSchedule
from pyracast.notifications.schedule import local_slot
from pyracast.regions.models import Region


def enqueue_due(now: datetime, cutoff_hour: int | None = None) -> list[int]:
    if now.tzinfo is None or (cutoff_hour is not None and not 0 <= cutoff_hour <= 23):
        raise ValidationError("An aware timestamp and valid cutoff hour are required.")
    cutoff: time = settings.PYRACAST_CUTOFF_LOCAL if cutoff_hour is None else time(cutoff_hour)
    jobs: list[int] = []
    for region_id in Region.objects.filter(status="approved").values_list("pk", flat=True):
        with transaction.atomic():
            region = (
                Region.objects.select_for_update(of=("self",))
                .select_related("approved_binding__provider_account")
                .get(pk=region_id)
            )
            binding = region.approved_binding
            if binding is None or binding.validated_at is None or not binding_is_current(binding):
                continue
            first_slot = (
                CollectionJob.objects.filter(binding=binding, endpoint="forecasts")
                .order_by("scheduled_slot")
                .values_list("scheduled_slot", flat=True)
                .first()
            )
            if first_slot is None:
                continue
            schedule = CollectionSchedule.objects.filter(region=region).first()
            if schedule is not None and not schedule.enabled:
                continue
            times = schedule.times if schedule else [cutoff.strftime("%H:%M")]
            times = times[: max(binding.provider_account.daily_quota - 1, 0)]
            effective_from = max(first_slot, schedule.effective_from) if schedule else first_slot
            zone = ZoneInfo(region.timezone)
            today = now.astimezone(zone).date()
            days: tuple[date, ...] = (today - timedelta(days=1), today)
            if schedule is None:
                today_slot = local_slot(today, cutoff.strftime("%H:%M"), zone)
                days = (today - timedelta(days=1),) if today_slot and today_slot > now else (today,)
            for day in days:
                slots = [
                    slot
                    for clock in times
                    if (slot := local_slot(day, clock, zone)) is not None and slot >= effective_from
                ]
                for slot in slots:
                    if slot > now or slot < effective_from:
                        continue
                    job, _ = CollectionJob.objects.get_or_create(
                        binding=binding,
                        endpoint="forecasts",
                        scheduled_slot=slot,
                        defaults={"target_date": day + timedelta(days=1), "available_at": now},
                    )
                    CollectionJob.objects.filter(pk=job.pk, status="stale").update(
                        status="pending", available_at=now, lease_token=None, lease_expires_at=None
                    )
                    jobs.append(job.pk)
                    if slot == slots[0]:
                        history, _ = CollectionJob.objects.select_for_update().get_or_create(
                            binding=binding,
                            endpoint="estimated_actuals",
                            target_date=day - timedelta(days=1),
                            defaults={"scheduled_slot": slot, "available_at": now},
                        )
                        CollectionJob.objects.filter(
                            pk=history.pk,
                            status="stale",
                            scheduled_slot__lt=effective_from,
                            forecastsnapshot__isnull=True,
                        ).update(scheduled_slot=slot)
                        CollectionJob.objects.filter(
                            pk=history.pk, status="stale", scheduled_slot__gte=effective_from
                        ).update(
                            status="pending",
                            available_at=now,
                            lease_token=None,
                            lease_expires_at=None,
                        )
                        jobs.append(history.pk)
    return jobs
