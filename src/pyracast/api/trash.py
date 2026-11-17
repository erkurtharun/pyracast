from datetime import datetime
from typing import Any, cast

from django.contrib.auth.models import User
from django.db.models import QuerySet
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.generics import GenericAPIView
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from pyracast import trash
from pyracast.accounts.authorization import tenant_membership
from pyracast.api.serializers import RegionOutput
from pyracast.plants.models import Plant
from pyracast.plants.serializers import EmptyInput, PlantOutput
from pyracast.regions.models import Region


def _purge_after(item: Region | Plant) -> datetime:
    return cast(datetime, item.deleted_at) + trash.RETENTION


class TrashedRegionOutput(serializers.ModelSerializer[Region]):
    deleted_at = serializers.DateTimeField()
    purge_after = serializers.SerializerMethodField()
    can_restore = serializers.SerializerMethodField()

    def get_purge_after(self, region: Region) -> datetime:
        return _purge_after(region)

    def get_can_restore(self, region: Region) -> bool:
        return timezone.now() < _purge_after(region)

    class Meta:
        model = Region
        fields = ("id", "name", "deleted_at", "purge_after", "can_restore")
        read_only_fields = fields


class TrashedPlantOutput(serializers.ModelSerializer[Plant]):
    deleted_at = serializers.DateTimeField()
    region_id = serializers.IntegerField()
    region_name = serializers.CharField(source="region.name")
    parent_deleted = serializers.SerializerMethodField()
    purge_after = serializers.SerializerMethodField()
    can_restore = serializers.SerializerMethodField()

    def get_parent_deleted(self, plant: Plant) -> bool:
        return plant.region.deleted_at is not None

    def get_purge_after(self, plant: Plant) -> datetime:
        return _purge_after(plant)

    def get_can_restore(self, plant: Plant) -> bool:
        return plant.region.deleted_at is None and timezone.now() < _purge_after(plant)

    class Meta:
        model = Plant
        fields = (
            "id",
            "name",
            "region_id",
            "region_name",
            "parent_deleted",
            "deleted_at",
            "purge_after",
            "can_restore",
        )
        read_only_fields = fields


class TrashedRegionsView(GenericAPIView[Region]):
    serializer_class = TrashedRegionOutput

    def get_queryset(self) -> QuerySet[Region]:
        tenant_id = self.kwargs["tenant_id"]
        tenant_membership(cast(User, self.request.user), tenant_id)
        return Region.objects.filter(tenant_id=tenant_id, deleted_at__isnull=False).order_by(
            "deleted_at", "id"
        )

    @extend_schema(responses=TrashedRegionOutput(many=True))
    def get(self, _request: Request, **_kwargs: Any) -> Response:
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(TrashedRegionOutput(page, many=True).data)


class TrashedPlantsView(GenericAPIView[Plant]):
    serializer_class = TrashedPlantOutput

    def get_queryset(self) -> QuerySet[Plant]:
        tenant_id = self.kwargs["tenant_id"]
        tenant_membership(cast(User, self.request.user), tenant_id)
        return (
            Plant.objects.filter(region__tenant_id=tenant_id, deleted_at__isnull=False)
            .select_related("region")
            .order_by("deleted_at", "id")
        )

    @extend_schema(responses=TrashedPlantOutput(many=True))
    def get(self, _request: Request, **_kwargs: Any) -> Response:
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(TrashedPlantOutput(page, many=True).data)


class RestoreRegionView(APIView):
    @extend_schema(request=EmptyInput, responses=RegionOutput)
    def post(self, request: Request, tenant_id: int, region_id: int) -> Response:
        data = EmptyInput(data=request.data)
        data.is_valid(raise_exception=True)
        region = trash.restore_region(cast(User, request.user), tenant_id, region_id)
        return Response(RegionOutput(region).data)


class RestorePlantView(APIView):
    @extend_schema(request=EmptyInput, responses=PlantOutput)
    def post(self, request: Request, tenant_id: int, region_id: int, plant_id: int) -> Response:
        data = EmptyInput(data=request.data)
        data.is_valid(raise_exception=True)
        plant = trash.restore_plant(cast(User, request.user), tenant_id, region_id, plant_id)
        return Response(PlantOutput(plant).data)
