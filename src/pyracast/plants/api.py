from typing import Any, cast

from django.contrib.auth.models import User
from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework.generics import GenericAPIView
from rest_framework.request import Request
from rest_framework.response import Response

from pyracast import trash
from pyracast.accounts.operations import require_member
from pyracast.plants import operations
from pyracast.plants.history import plant_outlook
from pyracast.plants.models import CalibrationRun, Plant, ProductionObservation
from pyracast.plants.performance import forecast_performance
from pyracast.plants.production import production_history
from pyracast.plants.serializers import (
    ActivationOutput,
    CalibrationInput,
    CalibrationOutput,
    CalibrationSummaryOutput,
    EmptyInput,
    ForecastQuery,
    ObservationImport,
    ObservationImportOutput,
    ObservationOutput,
    PerformanceOutput,
    PerformanceQuery,
    PlantForecastOutput,
    PlantInput,
    PlantOutput,
    ProductionHistoryOutput,
    ProductionQuery,
)
from pyracast.regions.models import Region


class ScopedView(GenericAPIView[Plant]):
    serializer_class = PlantOutput

    def get_queryset(self) -> QuerySet[Plant]:
        tenant_id = self.kwargs["tenant_id"]
        require_member(cast(User, self.request.user), tenant_id)
        region = get_object_or_404(
            Region, pk=self.kwargs["region_id"], tenant_id=tenant_id, deleted_at__isnull=True
        )
        return (
            Plant.objects.filter(region=region, deleted_at__isnull=True)
            .select_related("current_configuration")
            .order_by("id")
        )

    def plant(self) -> Plant:
        return get_object_or_404(self.get_queryset(), pk=self.kwargs["plant_id"])

    def scope(self) -> tuple[User, int, int, int]:
        plant = self.plant()
        return (
            cast(User, self.request.user),
            self.kwargs["tenant_id"],
            self.kwargs["region_id"],
            plant.pk,
        )


class PlantsView(ScopedView):
    @extend_schema(responses=PlantOutput(many=True))
    def get(self, request: Request, **kwargs: Any) -> Response:
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


class PlantView(ScopedView):
    @extend_schema(responses=PlantOutput)
    def get(self, request: Request, **kwargs: Any) -> Response:
        return Response(PlantOutput(self.plant()).data)

    @extend_schema(request=PlantInput, responses=PlantOutput)
    def put(self, request: Request, **kwargs: Any) -> Response:
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


class ObservationsView(ScopedView):
    @extend_schema(responses=ObservationOutput(many=True))
    def get(self, request: Request, **kwargs: Any) -> Response:
        rows = ProductionObservation.objects.filter(plant=self.plant()).order_by("starts_at", "id")
        page = self.paginate_queryset(cast(Any, rows))
        return self.get_paginated_response(ObservationOutput(page, many=True).data)

    @extend_schema(request=ObservationImport, responses={201: ObservationImportOutput})
    def post(self, request: Request, **kwargs: Any) -> Response:
        scope = self.scope()
        data = ObservationImport(data=request.data)
        data.is_valid(raise_exception=True)
        values = data.validated_data
        rows = [operations.ObservationInput(**row) for row in values["observations"]]
        result = operations.import_observations(
            *scope,
            configuration_id=values["configuration_id"],
            unit=values["unit"],
            observations=rows,
        )
        return Response(result, status=201)


class ProductionView(ScopedView):
    @extend_schema(parameters=[ProductionQuery], responses=ProductionHistoryOutput)
    def get(self, request: Request, **kwargs: Any) -> Response:
        plant = self.plant()
        data = ProductionQuery(data=request.query_params)
        data.is_valid(raise_exception=True)
        return Response(production_history(plant, **data.validated_data))


class PerformanceView(ScopedView):
    @extend_schema(parameters=[PerformanceQuery], responses=PerformanceOutput)
    def get(self, request: Request, **kwargs: Any) -> Response:
        plant = self.plant()
        data = PerformanceQuery(data=request.query_params)
        data.is_valid(raise_exception=True)
        return Response(forecast_performance(plant, **data.validated_data))


class CalibrationsView(ScopedView):
    @extend_schema(responses=CalibrationSummaryOutput(many=True))
    def get(self, request: Request, **kwargs: Any) -> Response:
        rows = (
            CalibrationRun.objects.filter(plant=self.plant())
            .defer("dataset_manifest")
            .order_by("-created_at", "-id")
        )
        page = self.paginate_queryset(cast(Any, rows))
        return self.get_paginated_response(CalibrationSummaryOutput(page, many=True).data)

    @extend_schema(request=CalibrationInput, responses={202: CalibrationOutput})
    def post(self, request: Request, **kwargs: Any) -> Response:
        scope = self.scope()
        data = CalibrationInput(data=request.data)
        data.is_valid(raise_exception=True)
        run = operations.request_calibration(*scope, **data.validated_data)
        return Response(CalibrationOutput(run).data, status=202)


class CalibrationView(ScopedView):
    @extend_schema(responses=CalibrationOutput)
    def get(self, request: Request, **kwargs: Any) -> Response:
        run = get_object_or_404(CalibrationRun, plant=self.plant(), pk=kwargs["calibration_id"])
        return Response(CalibrationOutput(run).data)


class ActivateView(ScopedView):
    @extend_schema(request=EmptyInput, responses=ActivationOutput)
    def post(self, request: Request, **kwargs: Any) -> Response:
        scope = self.scope()
        data = EmptyInput(data=request.data)
        data.is_valid(raise_exception=True)
        activation = operations.activate_calibration(*scope, kwargs["calibration_id"])
        return Response(ActivationOutput(activation).data)


class ResetView(ScopedView):
    @extend_schema(request=EmptyInput, responses=ActivationOutput)
    def post(self, request: Request, **kwargs: Any) -> Response:
        scope = self.scope()
        data = EmptyInput(data=request.data)
        data.is_valid(raise_exception=True)
        return Response(ActivationOutput(operations.reset_calibration(*scope)).data)


class ForecastView(ScopedView):
    @extend_schema(parameters=[ForecastQuery], responses=PlantForecastOutput)
    def get(self, request: Request, **kwargs: Any) -> Response:
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
