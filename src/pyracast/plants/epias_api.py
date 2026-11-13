from datetime import timedelta
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
from pyracast.metering.history import PRODUCTION_SOURCES, read_production_day
from pyracast.metering.models import EpiasConnection, EpiasSnapshot
from pyracast.plants.api import ScopedView


class EpiasConnectionInput(serializers.Serializer[Any]):
    provider_plant_id = serializers.IntegerField(min_value=1)
    expected_eic = serializers.RegexField(r"^[A-Za-z0-9-]{16}$")


class EpiasConnectionOutput(serializers.Serializer[Any]):
    connected = serializers.BooleanField()
    provider_plant_id = serializers.IntegerField(allow_null=True)
    settlement_plant_id = serializers.IntegerField(allow_null=True)
    first_settlement_date = serializers.DateField(allow_null=True)
    last_settlement_date = serializers.DateField(allow_null=True)
    eic = serializers.CharField(allow_blank=True)
    name = serializers.CharField(allow_blank=True)
    last_success_at = serializers.DateTimeField(allow_null=True)
    last_error = serializers.CharField(allow_blank=True)
    pending_jobs = serializers.IntegerField()
    archived_days = serializers.IntegerField()


class EpiasDateQuery(serializers.Serializer[Any]):
    date = serializers.DateField(required=False)
    data_source = serializers.ChoiceField(choices=("auto", *PRODUCTION_SOURCES), default="auto")


class EpiasSnapshotOutput(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    configuration_id = serializers.IntegerField()
    collected_at = serializers.DateTimeField()
    available_at = serializers.DateTimeField()
    content_sha256 = serializers.CharField()
    source_kind = serializers.ChoiceField(choices=PRODUCTION_SOURCES)
    normalization_policy = serializers.CharField()
    complete = serializers.BooleanField()


class EpiasReadingOutput(serializers.Serializer[Any]):
    source_kind = serializers.ChoiceField(choices=PRODUCTION_SOURCES, allow_null=True)
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
    source_kind = serializers.ChoiceField(choices=(*PRODUCTION_SOURCES, "mixed"), allow_null=True)
    complete = serializers.BooleanField()
    available_sources = serializers.ListField(
        child=serializers.ChoiceField(choices=PRODUCTION_SOURCES)
    )
    calibration_eligible = serializers.BooleanField()
    calibration_blocker = serializers.CharField(allow_null=True)
    snapshot = EpiasSnapshotOutput(allow_null=True)
    source_snapshots = EpiasSnapshotOutput(many=True)
    readings = EpiasReadingOutput(many=True)


class EpiasConnectionView(ScopedView[EpiasConnection]):
    serializer_class = EpiasConnectionOutput

    @extend_schema(responses=EpiasConnectionOutput)
    def get(self, request: Request, **_kwargs: Any) -> Response:
        result = operations.get_epias_status(self.plant().pk, actor=cast(User, request.user))
        return Response(EpiasConnectionOutput(result).data)

    @extend_schema(request=EpiasConnectionInput, responses=EpiasConnectionOutput)
    def put(self, request: Request, **_kwargs: Any) -> Response:
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
    def delete(self, request: Request, **_kwargs: Any) -> Response:
        operations.disconnect_epias(self.plant().pk, actor=cast(User, request.user))
        return Response(status=204)


class EpiasHistoryView(ScopedView[EpiasSnapshot]):
    serializer_class = EpiasHistoryOutput

    @extend_schema(parameters=[EpiasDateQuery], responses=EpiasHistoryOutput)
    def get(self, request: Request, **_kwargs: Any) -> Response:
        plant = self.plant()
        query = EpiasDateQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        yesterday = timezone.now().astimezone(ZoneInfo("Europe/Istanbul")).date() - timedelta(
            days=1
        )
        day = query.validated_data.get("date", yesterday)
        if day > yesterday:
            raise serializers.ValidationError("Choose a completed day in Europe/Istanbul.")
        result = read_production_day(
            plant,
            actor=cast(User, request.user),
            day=day,
            source_kind=query.validated_data["data_source"],
        )
        return Response(EpiasHistoryOutput(result).data)
