from threading import Lock
from typing import Any, cast

from django.contrib.auth.models import User
from django.db.models import Model, QuerySet
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import Throttled
from rest_framework.generics import GenericAPIView
from rest_framework.request import Request
from rest_framework.response import Response

from pyracast.api.errors import current_on_conflict
from pyracast.change_requests.queries import with_own_pending_requests
from pyracast.plants import operations
from pyracast.plants.activation import (
    activate_calibration,
    activation_blocker,
    reset_calibration,
    selection,
)
from pyracast.plants.automation import automation_status, set_calibration_automation
from pyracast.plants.history import plant_outlook
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    Plant,
)
from pyracast.plants.performance import forecast_performance
from pyracast.plants.plant_days import plant_days
from pyracast.plants.production import production_history
from pyracast.plants.production_changes import (
    AuditContext,
    ObservationInput,
    ProductionChange,
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
    ObservationImport,
    ObservationImportOutput,
    PerformanceOutput,
    PerformanceQuery,
    PlantConflict,
    PlantDaysOutput,
    PlantForecastOutput,
    PlantForecastQuery,
    PlantInput,
    PlantOutput,
    PlantRevisionInput,
    ProductionCommitInput,
    ProductionHistoryOutput,
    ProductionPreviewOutput,
    ProductionQuery,
)
from pyracast.regions.models import Region


class ScopedView[ModelT: Model](GenericAPIView[ModelT]):
    def plant_queryset(self) -> QuerySet[Plant]:
        region = get_object_or_404(
            Region.objects.active(),
            pk=self.kwargs["region_id"],
            tenant_id=self.kwargs["tenant_id"],
        )
        return (
            Plant.objects.filter(region=region)
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
            cast(User, request.user), kwargs["tenant_id"], kwargs["region_id"], data.parameters()
        )
        return Response(PlantOutput(plant).data, status=201)


class PlantView(ScopedView[Plant]):
    serializer_class = PlantOutput

    @extend_schema(request=PlantRevisionInput, responses={200: PlantOutput, 409: PlantConflict})
    def put(self, request: Request, **_kwargs: Any) -> Response:
        scope = self.scope()
        data = PlantRevisionInput(data=request.data)
        data.is_valid(raise_exception=True)
        with current_on_conflict(
            operations.StaleParametersError, lambda stale: PlantOutput(stale.current).data
        ):
            plant = operations.revise_plant(
                *scope,
                data.parameters(),
                expected_revision=data.validated_data["expected_revision"],
            )
        return Response(PlantOutput(plant).data)


class ProductionView(ScopedView[Plant]):
    @extend_schema(parameters=[ProductionQuery], responses=ProductionHistoryOutput)
    def get(self, request: Request, **_kwargs: Any) -> Response:
        plant = self.plant()
        data = ProductionQuery(data=request.query_params)
        data.is_valid(raise_exception=True)
        actor = cast(User, request.user)
        result = with_own_pending_requests(
            actor,
            self.kwargs["tenant_id"],
            plant.pk,
            production_history(plant, actor=actor, **data.validated_data),
        )
        return Response(ProductionHistoryOutput(result).data)


class ProductionChangesView(ScopedView[Plant]):
    @extend_schema(request=ProductionCommitInput, responses=ObservationImportOutput)
    def post(self, request: Request, **_kwargs: Any) -> Response:
        scope = self.scope()
        data = ProductionCommitInput(data=request.data)
        data.is_valid(raise_exception=True)
        values = data.validated_data
        result = change_production(
            *scope,
            configuration_id=values["configuration_id"],
            unit=values["unit"],
            changes=[ProductionChange(**row) for row in values["changes"]],
            audit=AuditContext(
                values.get("origin", "editor"),
                values.get("file_name", ""),
                values.get("file_sha256", ""),
            ),
            confirm_implausible=values["confirm_implausible"],
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
            observations=[ObservationInput(**row) for row in values["observations"]],
        )
        return Response(result)


class PerformanceView(ScopedView[Plant]):
    @extend_schema(parameters=[PerformanceQuery], responses=PerformanceOutput)
    def get(self, request: Request, **_kwargs: Any) -> Response:
        plant = self.plant()
        data = PerformanceQuery(data=request.query_params)
        data.is_valid(raise_exception=True)
        return Response(PerformanceOutput(forecast_performance(plant, **data.validated_data)).data)


class CalibrationsView(ScopedView[CalibrationRun]):
    serializer_class = CalibrationSummaryOutput

    @extend_schema(responses=CalibrationSummaryOutput(many=True))
    def get(self, _request: Request, **_kwargs: Any) -> Response:
        plant = self.plant()
        rows = (
            CalibrationRun.objects.filter(plant=plant)
            .defer("dataset_manifest")
            .order_by("-created_at", "-id")
        )
        page = self.paginate_queryset(rows)
        context = {"selection": selection(plant)}
        return self.get_paginated_response(
            CalibrationSummaryOutput(page, many=True, context=context).data
        )

    @extend_schema(request=CalibrationInput, responses={202: CalibrationOutput})
    def post(self, request: Request, **_kwargs: Any) -> Response:
        scope = self.scope()
        data = CalibrationInput(data=request.data)
        data.is_valid(raise_exception=True)
        run = operations.request_calibration(*scope, **data.validated_data)
        return Response(_detail(self.plant(), run), status=202)


def _detail(plant: Plant, run: CalibrationRun) -> Any:
    blocker = activation_blocker(plant, plant.current_configuration, run, timezone.now())
    context = {"selection": selection(plant), "blocker": blocker}
    return CalibrationOutput(run, context=context).data


class CalibrationView(ScopedView[CalibrationRun]):
    serializer_class = CalibrationOutput

    @extend_schema(responses=CalibrationOutput)
    def get(self, _request: Request, **kwargs: Any) -> Response:
        plant = self.plant()
        run = get_object_or_404(CalibrationRun, plant=plant, pk=kwargs["calibration_id"])
        return Response(_detail(plant, run))


class ActivateView(ScopedView[CalibrationActivation]):
    serializer_class = ActivationOutput

    @extend_schema(request=EmptyInput, responses=ActivationOutput)
    def post(self, request: Request, **kwargs: Any) -> Response:
        scope = self.scope()
        data = EmptyInput(data=request.data)
        data.is_valid(raise_exception=True)
        activation = activate_calibration(*scope, kwargs["calibration_id"])
        return Response(ActivationOutput(activation).data)


class ResetView(ScopedView[CalibrationActivation]):
    serializer_class = ActivationOutput

    @extend_schema(request=EmptyInput, responses=ActivationOutput)
    def post(self, request: Request, **_kwargs: Any) -> Response:
        scope = self.scope()
        data = EmptyInput(data=request.data)
        data.is_valid(raise_exception=True)
        return Response(ActivationOutput(reset_calibration(*scope)).data)


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


class PlantForecastView(ScopedView[Plant]):
    @extend_schema(parameters=[PlantForecastQuery], responses=PlantForecastOutput)
    def get(self, request: Request, **_kwargs: Any) -> Response:
        scope = self.scope()
        data = PlantForecastQuery(data=request.query_params)
        data.is_valid(raise_exception=True)
        values = data.validated_data
        result = plant_outlook(
            *scope,
            target_date=values["date"],
            resolution_minutes=values["resolution_minutes"],
            source=values["data_source"],
            forecast_id=values.get("forecast_id"),
        )
        return Response(PlantForecastOutput(result).data)


# One days read at a time leaves the process's other thread (deploy/gunicorn_conf.py) to the rest.
_days_read = Lock()
DAYS_READ_RETRY_SECONDS = 5


class PlantDaysView(ScopedView[Plant]):
    @extend_schema(responses=PlantDaysOutput)
    def get(self, _request: Request, **_kwargs: Any) -> Response:
        if not _days_read.acquire(blocking=False):
            raise Throttled(wait=DAYS_READ_RETRY_SECONDS)
        try:
            data = PlantDaysOutput(plant_days(*self.scope())).data
        finally:
            _days_read.release()
        return Response(data)
