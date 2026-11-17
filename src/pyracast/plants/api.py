from typing import Any, cast

from django.contrib.auth.models import User
from django.db.models import Model, QuerySet
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework.generics import GenericAPIView
from rest_framework.request import Request
from rest_framework.response import Response

from pyracast import trash
from pyracast.accounts.authorization import tenant_membership
from pyracast.plants import operations
from pyracast.plants.automation import automation_status, set_calibration_automation
from pyracast.plants.history import plant_outlook
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    Plant,
)
from pyracast.plants.performance import forecast_performance
from pyracast.plants.production import production_history
from pyracast.plants.production_changes import (
    ProductionChange,
    ProductionRevisionConflictError,
    change_production,
    preview_production,
)
from pyracast.plants.serializers import (
    ActivationOutput,
    CalibrationAutomationInput,
    CalibrationAutomationOutput,
    CalibrationInput,
    CalibrationOutput,
    CalibrationSummaryOutput,
    EmptyInput,
    ForecastQuery,
    ObservationImport,
    ObservationImportOutput,
    PerformanceOutput,
    PerformanceQuery,
    PlantForecastOutput,
    PlantInput,
    PlantOutput,
    ProductionChangesInput,
    ProductionHistoryOutput,
    ProductionPreviewOutput,
    ProductionQuery,
)
from pyracast.regions.models import Region


class ScopedView[ModelT: Model](GenericAPIView[ModelT]):
    def plant_queryset(self) -> QuerySet[Plant]:
        tenant_id = self.kwargs["tenant_id"]
        tenant_membership(cast(User, self.request.user), tenant_id)
        region = get_object_or_404(
            Region, pk=self.kwargs["region_id"], tenant_id=tenant_id, deleted_at__isnull=True
        )
        return (
            Plant.objects.filter(region=region, deleted_at__isnull=True)
            .select_related("current_configuration", "submission")
            .order_by("id")
        )

    def plant(self) -> Plant:
        return get_object_or_404(self.plant_queryset(), pk=self.kwargs["plant_id"])

    def scope(self) -> tuple[User, int, int, int]:
        plant = self.plant()
        return (
            cast(User, self.request.user),
            self.kwargs["tenant_id"],
            self.kwargs["region_id"],
            plant.pk,
        )


class PlantsView(ScopedView[Plant]):
    serializer_class = PlantOutput

    def get_queryset(self) -> QuerySet[Plant]:
        return self.plant_queryset()

    @extend_schema(responses=PlantOutput(many=True))
    def get(self, _request: Request, **_kwargs: Any) -> Response:
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(PlantOutput(page, many=True).data)

    @extend_schema(request=PlantInput, responses={201: PlantOutput})
    def post(self, request: Request, **kwargs: Any) -> Response:
        self.get_queryset()
        data = PlantInput(data=request.data)
        data.is_valid(raise_exception=True)
        plant = operations.create_plant(
            cast(User, request.user),
            kwargs["tenant_id"],
            kwargs["region_id"],
            **data.validated_data,
        )
        return Response(PlantOutput(plant).data, status=201)


class PlantView(ScopedView[Plant]):
    serializer_class = PlantOutput

    @extend_schema(request=PlantInput, responses=PlantOutput)
    def put(self, request: Request, **_kwargs: Any) -> Response:
        scope = self.scope()
        data = PlantInput(data=request.data)
        data.is_valid(raise_exception=True)
        plant = operations.revise_plant(*scope, **data.validated_data)
        return Response(PlantOutput(plant).data)

    @extend_schema(responses={204: None})
    def delete(self, request: Request, **kwargs: Any) -> Response:
        trash.trash_plant(
            cast(User, request.user),
            kwargs["tenant_id"],
            kwargs["region_id"],
            kwargs["plant_id"],
        )
        return Response(status=204)


class ProductionView(ScopedView[Plant]):
    @extend_schema(parameters=[ProductionQuery], responses=ProductionHistoryOutput)
    def get(self, request: Request, **_kwargs: Any) -> Response:
        plant = self.plant()
        data = ProductionQuery(data=request.query_params)
        data.is_valid(raise_exception=True)
        return Response(
            production_history(plant, actor=cast(User, request.user), **data.validated_data)
        )


class ProductionChangesView(ScopedView[Plant]):
    @extend_schema(request=ProductionChangesInput, responses=ObservationImportOutput)
    def post(self, request: Request, **_kwargs: Any) -> Response:
        scope = self.scope()
        data = ProductionChangesInput(data=request.data)
        data.is_valid(raise_exception=True)
        values = data.validated_data
        try:
            result = change_production(
                *scope,
                configuration_id=values["configuration_id"],
                unit=values["unit"],
                changes=[ProductionChange(**row) for row in values["changes"]],
            )
        except ProductionRevisionConflictError as error:
            return Response(
                {"code": "production_revision_conflict", "detail": str(error)}, status=409
            )
        return Response(result)


class ProductionPreviewView(ScopedView[Plant]):
    @extend_schema(request=ObservationImport, responses=ProductionPreviewOutput)
    def post(self, request: Request, **_kwargs: Any) -> Response:
        scope = self.scope()
        data = ObservationImport(data=request.data)
        data.is_valid(raise_exception=True)
        values = data.validated_data
        result = preview_production(
            *scope,
            configuration_id=values["configuration_id"],
            unit=values["unit"],
            observations=[operations.ObservationInput(**row) for row in values["observations"]],
        )
        return Response(result)


class PerformanceView(ScopedView[Plant]):
    @extend_schema(parameters=[PerformanceQuery], responses=PerformanceOutput)
    def get(self, request: Request, **_kwargs: Any) -> Response:
        plant = self.plant()
        data = PerformanceQuery(data=request.query_params)
        data.is_valid(raise_exception=True)
        return Response(forecast_performance(plant, **data.validated_data))


class CalibrationsView(ScopedView[CalibrationRun]):
    serializer_class = CalibrationSummaryOutput

    @extend_schema(responses=CalibrationSummaryOutput(many=True))
    def get(self, _request: Request, **_kwargs: Any) -> Response:
        rows = (
            CalibrationRun.objects.filter(plant=self.plant())
            .defer("dataset_manifest")
            .order_by("-created_at", "-id")
        )
        page = self.paginate_queryset(rows)
        return self.get_paginated_response(CalibrationSummaryOutput(page, many=True).data)

    @extend_schema(request=CalibrationInput, responses={202: CalibrationOutput})
    def post(self, request: Request, **_kwargs: Any) -> Response:
        scope = self.scope()
        data = CalibrationInput(data=request.data)
        data.is_valid(raise_exception=True)
        run = operations.request_calibration(*scope, **data.validated_data)
        return Response(CalibrationOutput(run).data, status=202)


class CalibrationView(ScopedView[CalibrationRun]):
    serializer_class = CalibrationOutput

    @extend_schema(responses=CalibrationOutput)
    def get(self, _request: Request, **kwargs: Any) -> Response:
        run = get_object_or_404(CalibrationRun, plant=self.plant(), pk=kwargs["calibration_id"])
        return Response(CalibrationOutput(run).data)


class ActivateView(ScopedView[CalibrationActivation]):
    serializer_class = ActivationOutput

    @extend_schema(request=EmptyInput, responses=ActivationOutput)
    def post(self, request: Request, **kwargs: Any) -> Response:
        scope = self.scope()
        data = EmptyInput(data=request.data)
        data.is_valid(raise_exception=True)
        activation = operations.activate_calibration(*scope, kwargs["calibration_id"])
        return Response(ActivationOutput(activation).data)


class ResetView(ScopedView[CalibrationActivation]):
    serializer_class = ActivationOutput

    @extend_schema(request=EmptyInput, responses=ActivationOutput)
    def post(self, request: Request, **_kwargs: Any) -> Response:
        scope = self.scope()
        data = EmptyInput(data=request.data)
        data.is_valid(raise_exception=True)
        return Response(ActivationOutput(operations.reset_calibration(*scope)).data)


class CalibrationAutomationView(ScopedView[Plant]):
    @extend_schema(responses=CalibrationAutomationOutput)
    def get(self, _request: Request, **_kwargs: Any) -> Response:
        return Response(CalibrationAutomationOutput(automation_status(self.plant())).data)

    @extend_schema(request=CalibrationAutomationInput, responses=CalibrationAutomationOutput)
    def patch(self, request: Request, **_kwargs: Any) -> Response:
        scope = self.scope()
        data = CalibrationAutomationInput(data=request.data)
        data.is_valid(raise_exception=True)
        plant = set_calibration_automation(*scope, **data.validated_data)
        return Response(CalibrationAutomationOutput(automation_status(plant)).data)


class ForecastView(ScopedView[Plant]):
    @extend_schema(parameters=[ForecastQuery], responses=PlantForecastOutput)
    def get(self, request: Request, **_kwargs: Any) -> Response:
        scope = self.scope()
        data = ForecastQuery(data=request.query_params)
        data.is_valid(raise_exception=True)
        values = data.validated_data
        result = plant_outlook(
            *scope,
            target_date=values["date"],
            resolution_minutes=values["resolution_minutes"],
            as_of=values.get("as_of"),
            source=values["data_source"],
        )
        return Response(result)
