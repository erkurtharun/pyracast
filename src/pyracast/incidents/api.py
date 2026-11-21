from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from drf_spectacular.utils import extend_schema, extend_schema_field
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from pyracast.api.serializers import FiniteFloat, InputSerializer
from pyracast.errors import ConflictError, ErrorCode
from pyracast.incidents import operations
from pyracast.incidents.models import IncidentKind, IncidentStatus, PlantIncident
from pyracast.plants.api import ScopedView
from pyracast.plants.serializers import AwareDateTime


class IncidentInput(InputSerializer):
    kind = serializers.ChoiceField(choices=IncidentKind.choices)
    title = serializers.CharField(max_length=200)
    notes = serializers.CharField(max_length=2000, allow_blank=True, default="")
    starts_at = AwareDateTime()
    ends_at = AwareDateTime(allow_null=True, default=None)
    available_dc_mw = FiniteFloat(min_value=0, allow_null=True, default=None)
    available_inverter_mw = FiniteFloat(min_value=0, allow_null=True, default=None)
    available_export_mw = FiniteFloat(min_value=0, allow_null=True, default=None)


class IncidentUpdate(IncidentInput):
    expected_revision = serializers.IntegerField(min_value=1)


class RevisionInput(InputSerializer):
    expected_revision = serializers.IntegerField(min_value=1)


class ResolveInput(RevisionInput):
    ends_at = AwareDateTime()


class IncidentOutput(serializers.ModelSerializer[PlantIncident]):
    declared_configuration_id = serializers.IntegerField()
    status = serializers.SerializerMethodField()

    @extend_schema_field(serializers.ChoiceField(choices=IncidentStatus.choices))
    def get_status(self, obj: PlantIncident) -> str:
        if obj.cancelled_at:
            return IncidentStatus.CANCELLED
        return IncidentStatus.RESOLVED if obj.ends_at else IncidentStatus.ACTIVE

    class Meta:
        model = PlantIncident
        fields = (
            "id",
            "revision",
            "kind",
            "title",
            "notes",
            "starts_at",
            "ends_at",
            "available_dc_mw",
            "available_inverter_mw",
            "available_export_mw",
            "declared_configuration_id",
            "status",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class IncidentConflict(serializers.Serializer[dict[str, Any]]):
    code = serializers.ChoiceField(choices=ErrorCode.choices)
    message = serializers.CharField()
    current = IncidentOutput()


@contextmanager
def current_on_conflict() -> Iterator[None]:
    """A stale write answers with the version it lost to; the caller is already authorized."""
    try:
        yield
    except operations.StaleIncidentError as error:
        raise ConflictError(
            error.code, details={"current": IncidentOutput(error.current).data}
        ) from error


class IncidentsView(ScopedView[PlantIncident]):
    serializer_class = IncidentOutput

    @extend_schema(responses=IncidentOutput(many=True))
    def get(self, _request: Request, **_kwargs: Any) -> Response:
        rows = PlantIncident.objects.filter(plant=self.plant()).order_by("-starts_at", "-id")
        page = self.paginate_queryset(rows)
        return self.get_paginated_response(IncidentOutput(page, many=True).data)

    @extend_schema(request=IncidentInput, responses={201: IncidentOutput})
    def post(self, request: Request, **_kwargs: Any) -> Response:
        scope = self.scope()
        data = IncidentInput(data=request.data)
        data.is_valid(raise_exception=True)
        row = operations.save_incident(*scope, **data.validated_data)
        return Response(IncidentOutput(row).data, status=201)


class IncidentView(ScopedView[PlantIncident]):
    serializer_class = IncidentOutput

    @extend_schema(request=IncidentUpdate, responses={200: IncidentOutput, 409: IncidentConflict})
    def put(self, request: Request, **kwargs: Any) -> Response:
        scope = self.scope()
        data = IncidentUpdate(data=request.data)
        data.is_valid(raise_exception=True)
        with current_on_conflict():
            row = operations.save_incident(
                *scope, incident_id=kwargs["incident_id"], **data.validated_data
            )
        return Response(IncidentOutput(row).data)


class ResolveView(ScopedView[PlantIncident]):
    serializer_class = IncidentOutput

    @extend_schema(request=ResolveInput, responses={200: IncidentOutput, 409: IncidentConflict})
    def post(self, request: Request, **kwargs: Any) -> Response:
        scope = self.scope()
        data = ResolveInput(data=request.data)
        data.is_valid(raise_exception=True)
        with current_on_conflict():
            row = operations.finish_incident(
                *scope, incident_id=kwargs["incident_id"], **data.validated_data
            )
        return Response(IncidentOutput(row).data)


class CancelView(ScopedView[PlantIncident]):
    serializer_class = IncidentOutput

    @extend_schema(request=RevisionInput, responses={200: IncidentOutput, 409: IncidentConflict})
    def post(self, request: Request, **kwargs: Any) -> Response:
        scope = self.scope()
        data = RevisionInput(data=request.data)
        data.is_valid(raise_exception=True)
        with current_on_conflict():
            row = operations.finish_incident(
                *scope, incident_id=kwargs["incident_id"], cancel=True, **data.validated_data
            )
        return Response(IncidentOutput(row).data)
