from typing import Any, cast

from django.contrib.auth.models import User
from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema, extend_schema_field
from rest_framework import serializers
from rest_framework.generics import GenericAPIView
from rest_framework.request import Request
from rest_framework.response import Response

from pyracast.api.serializers import RegionInput, RegionOutput
from pyracast.plants.models import Plant
from pyracast.plants.serializers import PlantInput, PlantOutput
from pyracast.plants.sites import site_queryset, submit_site
from pyracast.regions.models import Region


class SiteInput(RegionInput, PlantInput):
    pass


class SiteOutput(RegionOutput):
    plant = serializers.SerializerMethodField()

    class Meta:
        model = Region
        fields = (*RegionOutput.Meta.fields, "plant")
        read_only_fields = fields

    @extend_schema_field(PlantOutput(allow_null=True))
    def get_plant(self, region: Region) -> dict[str, Any] | None:
        plants = cast(list[Plant], region.__dict__["active_plants"])
        return dict(PlantOutput(plants[0]).data) if plants else None


class SitesView(GenericAPIView[Region]):
    serializer_class = SiteOutput

    def get_queryset(self) -> QuerySet[Region]:
        return site_queryset(self.kwargs["tenant_id"])

    @extend_schema(responses=SiteOutput(many=True))
    def get(self, request: Request, tenant_id: int) -> Response:
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(SiteOutput(page, many=True).data)

    @extend_schema(request=SiteInput, responses={201: SiteOutput})
    def post(self, request: Request, tenant_id: int) -> Response:
        data = SiteInput(data=request.data)
        data.is_valid(raise_exception=True)
        site = submit_site(cast(User, request.user), tenant_id, **data.validated_data)
        return Response(SiteOutput(site).data, status=201)
