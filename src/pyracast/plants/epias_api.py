from datetime import date, datetime, time, timedelta
from math import isfinite
from typing import Any, cast
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from pyracast.epias import EpiasError
from pyracast.metering import operations
from pyracast.metering.models import EpiasConnection, EpiasSnapshot
from pyracast.plants.api import ScopedView
from pyracast.plants.models import Plant
from pyracast.plants.operations import configuration_is_current

SOURCES = ("realtime_generation", "settlement_injection")


class EpiasConnectionInput(serializers.Serializer[Any]):
    provider_plant_id = serializers.IntegerField(min_value=1)
    expected_eic = serializers.RegexField(r"^[A-Za-z0-9-]{16}$")
    meter_basis = serializers.ChoiceField(
        choices=("unknown", "net_export", "ac_generation"), default="unknown"
    )


class EpiasConnectionOutput(serializers.Serializer[Any]):
    connected = serializers.BooleanField()
    provider_plant_id = serializers.IntegerField(allow_null=True)
    settlement_plant_id = serializers.IntegerField(allow_null=True)
    first_settlement_date = serializers.DateField(allow_null=True)
    last_settlement_date = serializers.DateField(allow_null=True)
    eic = serializers.CharField(allow_blank=True)
    name = serializers.CharField(allow_blank=True)
    meter_basis = serializers.ChoiceField(choices=("unknown", "net_export", "ac_generation"))
    last_success_at = serializers.DateTimeField(allow_null=True)
    last_error = serializers.CharField(allow_blank=True)
    pending_jobs = serializers.IntegerField()
    archived_days = serializers.IntegerField()


class EpiasDateQuery(serializers.Serializer[Any]):
    date = serializers.DateField(required=False)
    data_source = serializers.ChoiceField(choices=("auto", *SOURCES), default="auto")


class EpiasSnapshotOutput(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    configuration_id = serializers.IntegerField()
    collected_at = serializers.DateTimeField()
    available_at = serializers.DateTimeField()
    content_sha256 = serializers.CharField()
    source_kind = serializers.ChoiceField(choices=SOURCES)
    normalization_policy = serializers.CharField()
    complete = serializers.BooleanField()


class EpiasReadingOutput(serializers.Serializer[Any]):
    source_kind = serializers.ChoiceField(choices=SOURCES, allow_null=True)
    snapshot_id = serializers.IntegerField(allow_null=True)
    provider_timestamp = serializers.DateTimeField(allow_null=True)
    period_start = serializers.DateTimeField()
    period_end = serializers.DateTimeField()
    reported = serializers.BooleanField()
    energy_mwh = serializers.FloatField(source="total_mwh", allow_null=True)
    solar_mwh = serializers.FloatField(source="sun_mwh", allow_null=True)


class EpiasHistoryOutput(serializers.Serializer[Any]):
    date = serializers.DateField()
    timezone = serializers.CharField()
    unit = serializers.CharField()
    source_kind = serializers.ChoiceField(choices=(*SOURCES, "mixed"), allow_null=True)
    complete = serializers.BooleanField()
    available_sources = serializers.ListField(child=serializers.ChoiceField(choices=SOURCES))
    calibration_eligible = serializers.BooleanField()
    calibration_blocker = serializers.CharField(allow_null=True)
    snapshot = EpiasSnapshotOutput(allow_null=True)
    source_snapshots = EpiasSnapshotOutput(many=True)
    readings = EpiasReadingOutput(many=True)


def _intervals(snapshot: EpiasSnapshot | None, day: date) -> list[dict[str, object]]:
    if snapshot is None or not snapshot.readings:
        return []
    indexed = {datetime.fromisoformat(row["provider_timestamp"]): row for row in snapshot.readings}
    midnight = datetime.combine(day, time(), ZoneInfo("Europe/Istanbul"))
    rows = []
    for hour in range(24):
        start = midnight + timedelta(hours=hour)
        reading = indexed.get(start)
        rows.append(
            {
                "source_kind": snapshot.source_kind if reading else None,
                "snapshot_id": snapshot.pk if reading else None,
                "provider_timestamp": reading["provider_timestamp"] if reading else None,
                "period_start": start,
                "period_end": start + timedelta(hours=1),
                "reported": reading is not None,
                "total_mwh": reading["total_mwh"] if reading else None,
                "sun_mwh": reading["sun_mwh"] if reading else None,
            }
        )
    return rows


def _valid_energy(row: dict[str, object]) -> bool:
    value = row["total_mwh"]
    if value is None:
        return False
    try:
        energy = float(str(value))
    except TypeError, ValueError, OverflowError:
        return False
    return isfinite(energy) and energy >= 0


def _automatic_intervals(
    by_source: dict[str, EpiasSnapshot], day: date
) -> tuple[list[dict[str, object]], list[EpiasSnapshot]]:
    candidates = [
        (snapshot, _intervals(snapshot, day))
        for source in reversed(SOURCES)
        if (snapshot := by_source.get(source)) is not None and snapshot.readings
    ]
    if not candidates:
        return [], []
    rows = []
    used: set[int] = set()
    for hour in range(24):
        for snapshot, intervals in candidates:
            row = intervals[hour]
            if _valid_energy(row):
                rows.append(row)
                used.add(snapshot.pk)
                break
        else:
            missing = next(
                (intervals[hour] for _, intervals in candidates if intervals[hour]["reported"]),
                candidates[0][1][hour],
            )
            rows.append(
                {
                    **missing,
                    "total_mwh": None,
                    "sun_mwh": None,
                    "source_kind": None,
                    "snapshot_id": None,
                }
            )
    return rows, [snapshot for snapshot, _ in candidates if snapshot.pk in used]


def _calibration_blocker(plant: Plant, snapshot: EpiasSnapshot) -> str | None:
    configuration = plant.current_configuration
    if configuration is None or snapshot.configuration_id != configuration.pk:
        return "configuration_mismatch"
    if not configuration_is_current(configuration):
        return "configuration_unavailable"
    if configuration.measurement_basis != "net_export":
        return "measurement_basis_mismatch"
    connection = EpiasConnection.objects.get(pk=snapshot.connection_id)
    if not connection.enabled:
        return "source_disconnected"
    return None


class EpiasConnectionView(ScopedView):
    @extend_schema(responses=EpiasConnectionOutput)
    def get(self, request: Request, **kwargs: Any) -> Response:
        result = operations.get_epias_status(self.plant().pk, actor=cast(User, request.user))
        return Response(EpiasConnectionOutput(result).data)

    @extend_schema(request=EpiasConnectionInput, responses=EpiasConnectionOutput)
    def put(self, request: Request, **kwargs: Any) -> Response:
        plant = self.plant()
        data = EpiasConnectionInput(data=request.data)
        data.is_valid(raise_exception=True)
        actor = cast(User, request.user)
        try:
            operations.connect_epias(plant.pk, actor=actor, **data.validated_data)
        except EpiasError:
            return Response(
                {
                    "code": "epias_unavailable",
                    "detail": "EPIAS connection is currently unavailable. Please try again later.",
                },
                status=502,
            )
        return Response(
            EpiasConnectionOutput(operations.get_epias_status(plant.pk, actor=actor)).data
        )

    @extend_schema(responses={204: None})
    def delete(self, request: Request, **kwargs: Any) -> Response:
        operations.disconnect_epias(self.plant().pk, actor=cast(User, request.user))
        return Response(status=204)


class EpiasHistoryView(ScopedView):
    @extend_schema(parameters=[EpiasDateQuery], responses=EpiasHistoryOutput)
    def get(self, request: Request, **kwargs: Any) -> Response:
        plant = self.plant()
        query = EpiasDateQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        yesterday = timezone.now().astimezone(ZoneInfo("Europe/Istanbul")).date() - timedelta(
            days=1
        )
        day = query.validated_data.get("date", yesterday)
        if day > yesterday:
            raise serializers.ValidationError("Choose a completed day in Europe/Istanbul.")
        snapshots = operations.read_epias_history(
            plant.pk, actor=cast(User, request.user), start_day=day, end_day=day, source_kind=None
        )
        by_source = {snapshot.source_kind: snapshot for snapshot in snapshots}
        available = [
            source
            for source, snapshot in by_source.items()
            if any(row["total_mwh"] is not None for row in snapshot.readings)
        ]
        selected = query.validated_data["data_source"]
        if selected == "auto":
            readings, used_snapshots = _automatic_intervals(by_source, day)
            snapshot = used_snapshots[0] if used_snapshots else None
            selected = (
                "mixed"
                if len(used_snapshots) > 1
                else (snapshot.source_kind if snapshot is not None else None)
            )
        else:
            snapshot = by_source.get(selected)
            readings = _intervals(snapshot, day)
            used_snapshots = (
                [snapshot]
                if snapshot is not None and any(_valid_energy(row) for row in readings)
                else []
            )
        blocker = next(
            (
                reason
                for source_snapshot in used_snapshots
                if (reason := _calibration_blocker(plant, source_snapshot)) is not None
            ),
            None if used_snapshots else "production_unavailable",
        )
        result = {
            "date": day,
            "timezone": "Europe/Istanbul",
            "unit": "MWh",
            "source_kind": selected,
            "complete": len(readings) == 24 and all(_valid_energy(row) for row in readings),
            "available_sources": available,
            "calibration_eligible": blocker is None,
            "calibration_blocker": blocker,
            "snapshot": snapshot,
            "source_snapshots": used_snapshots,
            "readings": readings,
        }
        return Response(EpiasHistoryOutput(result).data)
