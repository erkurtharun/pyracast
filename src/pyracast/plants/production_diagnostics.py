from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pyracast.plants.incident_policy import incidents_unchanged
from pyracast.plants.measurements import production_identity_current
from pyracast.plants.models import CalibrationRun, CalibrationStatus, PlantConfiguration
from pyracast.plants.numerics import DERATING_POLICY, POLICY_VERSION
from pyracast.plants.transfer import recorded_transfer


def production_diagnostic(outlook: dict[str, Any], known_at: datetime) -> dict[str, object] | None:
    normal: dict[str, Any] | None = outlook.get("result")
    if normal is None or outlook["source_kind"] != "forecast":
        return None
    configuration = PlantConfiguration.objects.select_related("binding").get(
        pk=outlook["configuration_id"], plant_id=outlook["plant_id"]
    )
    zone = configuration.binding.timezone
    run = (
        CalibrationRun.objects.select_related("configuration", "binding")
        .filter(
            plant_id=configuration.plant_id,
            configuration=configuration,
            binding_id=configuration.binding_id,
            status__in=(
                CalibrationStatus.READY,
                CalibrationStatus.REJECTED,
                CalibrationStatus.INSUFFICIENT,
            ),
            created_at__lte=known_at,
            cutoff_at__lte=known_at,
            completed_at__lte=known_at,
            result__policy_version=POLICY_VERSION,
        )
        .order_by("-completed_at", "-pk")
        .first()
    )
    assessment = run.result.get("derating") if run is not None else None
    if (
        run is None
        or not isinstance(assessment, dict)
        or assessment.get("policy") != DERATING_POLICY
        or assessment.get("status") not in {"suspected", "normal", "insufficient"}
        or assessment.get("baseline_factor") != normal["factor"]
        or recorded_transfer(run) != normal["model_version"]
        or not production_identity_current(run)
        or not incidents_unchanged(run, known_at)
    ):
        return None
    latest_day = assessment.get("latest_matched_day")
    if not isinstance(latest_day, str):
        return None
    try:
        latest = date.fromisoformat(latest_day)
    except ValueError:
        return None
    today = known_at.astimezone(ZoneInfo(zone)).date()
    if latest >= today:
        return None
    return {
        **assessment,
        "evaluated_at": known_at.isoformat(),
        "calibration_id": run.pk,
        "evidence_state": "stale" if latest < today - timedelta(days=2) else "current",
    }
