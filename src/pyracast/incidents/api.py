from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from pyracast.api.serializers import InputSerializer
from pyracast.incidents import operations
from pyracast.incidents.models import IncidentKind, PlantIncident
from pyracast.plants.api import ScopedView
from pyracast.plants.serializers import AwareDateTime, FiniteFloat


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

    def get_status(self, obj: PlantIncident) -> str:
        return "cancelled" if obj.cancelled_at else "resolved" if obj.ends_at else "active"

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

    @extend_schema(request=IncidentUpdate, responses=IncidentOutput)
    def put(self, request: Request, **kwargs: Any) -> Response:
        scope = self.scope()
        data = IncidentUpdate(data=request.data)
        data.is_valid(raise_exception=True)
        row = operations.save_incident(
            *scope, incident_id=kwargs["incident_id"], **data.validated_data
        )
        return Response(IncidentOutput(row).data)


class ResolveView(ScopedView[PlantIncident]):
    serializer_class = IncidentOutput

    @extend_schema(request=ResolveInput, responses=IncidentOutput)
    def post(self, request: Request, **kwargs: Any) -> Response:
        scope = self.scope()
        data = ResolveInput(data=request.data)
        data.is_valid(raise_exception=True)
        row = operations.finish_incident(
            *scope, incident_id=kwargs["incident_id"], **data.validated_data
        )
        return Response(IncidentOutput(row).data)


class CancelView(ScopedView[PlantIncident]):
    serializer_class = IncidentOutput

    @extend_schema(request=RevisionInput, responses=IncidentOutput)
    def post(self, request: Request, **kwargs: Any) -> Response:
        scope = self.scope()
        data = RevisionInput(data=request.data)
        data.is_valid(raise_exception=True)
        row = operations.finish_incident(
            *scope, incident_id=kwargs["incident_id"], cancel=True, **data.validated_data
        )
        return Response(IncidentOutput(row).data)
