import hashlib
import json
from datetime import timedelta
from typing import Any

from django.core.cache import caches
from django.db.models import Model
from django.test import Client

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.incidents.models import IncidentRevision, PlantIncident
from pyracast.incidents.operations import save_incident
from pyracast.irradiance import local_day_bounds
from pyracast.plants.models import CalibrationRun, ForecastIssuance, Plant, PlantConfiguration
from pyracast.regions.models import Region
from tests.support.domain import NOW, System
from tests.support.forecast_history import DAY, TODAY, add_history, usable_calibration

SOURCES = ("auto", "forecast", "estimated_actuals", "archived_forecast")
DAYS = (-367, -366, -2, -1, 0, 1, 14, 15)
CUTOFFS = {"none": None, "hour-ago": NOW - timedelta(hours=1), "ahead": NOW + timedelta(seconds=1)}
FAMILIES: dict[str, type[Model]] = {
    "snapshot": ForecastSnapshot,
    "binding": SiteBinding,
    "configuration": PlantConfiguration,
    "calibration": CalibrationRun,
    "issuance": ForecastIssuance,
    "plant": Plant,
    "region": Region,
    "incident": PlantIncident,
    "revision": IncidentRevision,
}
IDENTIFIERS = {
    "snapshot_id": "snapshot",
    "source_snapshot_id": "snapshot",
    "source_snapshot_ids": "snapshot",
    "snapshot_ids": "snapshot",
    "binding_id": "binding",
    "configuration_id": "configuration",
    "current_configuration_id": "configuration",
    "declared_configuration_id": "configuration",
    "calibration_id": "calibration",
    "issuance_id": "issuance",
    "plant_id": "plant",
    "region_id": "region",
    "incident_id": "incident",
    "revision_id": "revision",
    "incident_revision_ids": "revision",
}
PARENT_IDENTIFIERS = {"configuration": "configuration", "incidents": "incident"}


def outlook_history(system: System, plant: Plant) -> None:
    """Every source behind the reads around today, an outage reported for yesterday and one
    still active today."""
    usable_calibration(system, plant)
    add_history(system, plant, range(2))
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    for day, hours in ((DAY, (9, 12)), (TODAY, (8, None))):
        start, _ = local_day_bounds(day, system.binding.timezone)
        save_incident(
            *scope,
            kind="panels",
            title=f"Outage {day}",
            notes="",
            starts_at=start + timedelta(hours=hours[0]),
            ends_at=None if hours[1] is None else start + timedelta(hours=hours[1]),
            available_dc_mw=65,
            available_inverter_mw=None,
            available_export_mw=None,
        )


def labels() -> dict[str, dict[int, str]]:
    return {
        family: {
            pk: f"{family}-{index}"
            for index, pk in enumerate(
                model._default_manager.order_by("pk").values_list("pk", flat=True)
            )
        }
        for family, model in FAMILIES.items()
    }


def canonical(value: Any, names: dict[str, dict[int, str]], key: str = "", parent: str = "") -> Any:
    """The answer with database ids replaced by fixture labels, which do not depend on the
    sequences of the test database."""
    if isinstance(value, dict):
        return {name: canonical(item, names, name, key) for name, item in value.items()}
    if isinstance(value, list):
        return [canonical(item, names, key, parent) for item in value]
    if isinstance(value, bool) or not isinstance(value, int):
        if key == "binding_revision" and isinstance(value, str):
            binding, revision = value.split(":")
            return f"{names['binding'][int(binding)]}:{revision}"
        return value
    family = IDENTIFIERS.get(key) or (PARENT_IDENTIFIERS.get(parent) if key == "id" else None)
    if family is not None:
        return names[family][value]
    if key == "id" or key.endswith(("_id", "_ids")):
        raise AssertionError(f"unlabelled identifier {key}")
    return value


def summary(status: int, body: dict[str, Any]) -> str:
    if status != 200:
        return f"{status} {body['code']}"
    result = body["result"] or {}
    policy = result.get("provenance", {}).get("selection_policy")
    reason = body["unavailable_reason"] or policy
    return f"200 {body['source_kind']} {body['freshness']} {reason}"


def answer(client: Client, url: str, query: dict[str, str], names: dict[str, Any]) -> str:
    caches["throttle"].clear()
    response = client.get(url, query)
    body = response.json()
    text = json.dumps(canonical(body, names), sort_keys=True, separators=(",", ":"))
    return f"{summary(response.status_code, body)} {hashlib.sha256(text.encode()).hexdigest()[:16]}"


def plant_reads() -> dict[str, dict[str, str]]:
    reads = {}
    for source in SOURCES:
        for offset in DAYS:
            for cutoff, as_of in CUTOFFS.items():
                query = {
                    "date": str(TODAY + timedelta(days=offset)),
                    "data_source": source,
                    "resolution_minutes": "60",
                }
                if as_of is not None:
                    query["as_of"] = as_of.isoformat()
                reads[f"{source} {offset:+d} {cutoff}"] = query
    return reads | boundary_reads(
        {
            "resolution 15": {"resolution_minutes": "15"},
            "naive as_of": {"as_of": NOW.replace(tzinfo=None).isoformat()},
        }
    )


def region_reads() -> dict[str, dict[str, str]]:
    reads = {
        f"{source} {offset:+d}": {
            "date": str(TODAY + timedelta(days=offset)),
            "data_source": source,
        }
        for source in SOURCES
        for offset in DAYS
    }
    return reads | boundary_reads({})


def boundary_reads(extra: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    day = {"date": str(DAY)}
    reads = {
        "default source": day,
        "unknown source": day | {"data_source": "unknown"},
        "empty date": {"date": ""},
        "unknown key": day | {"other": "1"},
    }
    return reads | {name: day | values for name, values in extra.items()}


def answers(client: Client, url: str, reads: dict[str, dict[str, str]]) -> dict[str, str]:
    names = labels()
    return {name: answer(client, url, query, names) for name, query in reads.items()}
