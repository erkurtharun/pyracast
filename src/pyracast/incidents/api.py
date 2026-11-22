from contextlib import AbstractContextManager
from typing import Any, cast

from django.contrib.auth.models import User
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_field
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from pyracast.api import errors
from pyracast.api.serializers import FiniteFloat, InputSerializer
from pyracast.change_requests.queries import outage_proposal, own_pending_outages
from pyracast.errors import ErrorCode
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


class IncidentCreate(IncidentInput):
    idempotency_key = serializers.UUIDField(required=False)


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
        fields: tuple[str, ...] = (
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


class IncidentListOutput(IncidentOutput):
    pending_request_id = serializers.SerializerMethodField()

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_pending_request_id(self, obj: PlantIncident) -> int | None:
        return cast(dict[int, int], self.context["pending"]).get(obj.pk)

    class Meta(IncidentOutput.Meta):
        fields = (*IncidentOutput.Meta.fields, "pending_request_id")
        read_only_fields = fields


class IncidentConflict(serializers.Serializer[dict[str, Any]]):
    code = serializers.ChoiceField(choices=ErrorCode.choices)
    message = serializers.CharField()
    current = IncidentListOutput()


def outage_rows(
    actor: User, tenant_id: int, plant_id: int, rows: list[PlantIncident]
) -> list[dict[str, Any]]:
    pending = own_pending_outages(actor, tenant_id, plant_id, [row.pk for row in rows])
    return cast(
        list[dict[str, Any]],
        IncidentListOutput(rows, many=True, context={"pending": pending}).data,
    )


def current_on_conflict(actor: User, tenant_id: int) -> AbstractContextManager[None]:
    """A stale outage write answers with the outage it lost to as the actor's outage list shows
    it; the caller is already authorized."""
    return errors.current_on_conflict(
        operations.StaleIncidentError,
        lambda stale: outage_rows(actor, tenant_id, stale.current.plant_id, [stale.current])[0],
    )


class IncidentPage(serializers.Serializer[dict[str, Any]]):
    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    can_propose = serializers.BooleanField()
    proposal_unavailable_reason = serializers.CharField(allow_null=True)
    results = IncidentListOutput(many=True)


class IncidentsView(ScopedView[PlantIncident]):
    serializer_class = IncidentOutput

    @extend_schema(
        operation_id="tenants_regions_plants_outages_list",
        parameters=[
            OpenApiParameter(
                "page", int, description="A page number within the paginated result set."
            )
        ],
        responses=IncidentPage,
    )
    def get(self, request: Request, **_kwargs: Any) -> Response:
        plant, actor, tenant_id = self.plant(), cast(User, request.user), self.kwargs["tenant_id"]
        rows = PlantIncident.objects.filter(plant=plant).order_by("-starts_at", "-id")
        page = cast(list[PlantIncident], self.paginate_queryset(rows))
        response = self.get_paginated_response(outage_rows(actor, tenant_id, plant.pk, page))
        response.data.update(outage_proposal(actor, tenant_id, plant))
        return response

    @extend_schema(request=IncidentCreate, responses={201: IncidentOutput})
    def post(self, request: Request, **_kwargs: Any) -> Response:
        scope = self.scope()
        data = IncidentCreate(data=request.data)
        data.is_valid(raise_exception=True)
        row = operations.create_incident(*scope, **data.validated_data)
        return Response(IncidentOutput(row).data, status=201)


class IncidentView(ScopedView[PlantIncident]):
    serializer_class = IncidentOutput

    @extend_schema(request=IncidentUpdate, responses={200: IncidentOutput, 409: IncidentConflict})
    def put(self, request: Request, **kwargs: Any) -> Response:
        scope = self.scope()
        data = IncidentUpdate(data=request.data)
        data.is_valid(raise_exception=True)
        with current_on_conflict(cast(User, request.user), kwargs["tenant_id"]):
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
        with current_on_conflict(cast(User, request.user), kwargs["tenant_id"]):
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
        with current_on_conflict(cast(User, request.user), kwargs["tenant_id"]):
            row = operations.finish_incident(
                *scope, incident_id=kwargs["incident_id"], cancel=True, **data.validated_data
            )
        return Response(IncidentOutput(row).data)
