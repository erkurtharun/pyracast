from typing import Any, cast

from django.contrib.auth.models import User
from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.generics import GenericAPIView
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from pyracast import trash
from pyracast.accounts.models import Membership
from pyracast.api.serializers import (
    ForecastOutput,
    ForecastQuery,
    LocationChangeInput,
    LocationChangeOutput,
    TenantOutput,
)
from pyracast.change_requests.policy import workspace_counters
from pyracast.db import read_snapshot
from pyracast.forecasts.outlook import regional_outlook
from pyracast.plants.serializers import EmptyInput
from pyracast.regions.models import Region, RegionLocationChangeRequest
from pyracast.regions.operations import request_location_change, withdraw_location_change


def tenant_region(tenant_id: int, region_id: int) -> Region:
    return get_object_or_404(Region.objects.active(), pk=region_id, tenant_id=tenant_id)


class TenantsView(GenericAPIView[Membership]):
    def get_queryset(self) -> QuerySet[Membership]:
        return (
            Membership.objects.filter(user=cast(User, self.request.user))
            .select_related("tenant")
            .order_by("tenant_id")
        )

    @extend_schema(responses=TenantOutput(many=True))
    def get(self, request: Request) -> Response:
        page = list(self.paginate_queryset(self.get_queryset()) or [])
        counters = workspace_counters(cast(User, request.user), page)
        rows = [
            {
                "id": membership.tenant_id,
                "name": membership.tenant.name,
                "role": membership.role,
                "pending_review_count": counters[membership.tenant_id][0],
                "unread_outcome_count": counters[membership.tenant_id][1],
            }
            for membership in page
        ]
        return self.get_paginated_response(TenantOutput(cast(Any, rows), many=True).data)


class RegionView(APIView):
    @extend_schema(responses={204: None})
    def delete(self, request: Request, tenant_id: int, region_id: int) -> Response:
        trash.trash_region(cast(User, request.user), tenant_id, region_id)
        return Response(status=204)


class LocationChangeRequestsView(GenericAPIView[RegionLocationChangeRequest]):
    serializer_class = LocationChangeOutput

    def get_region(self) -> Region:
        return tenant_region(self.kwargs["tenant_id"], self.kwargs["region_id"])

    def get_queryset(self) -> QuerySet[RegionLocationChangeRequest]:
        return RegionLocationChangeRequest.objects.filter(region=self.get_region()).order_by("-id")

    @extend_schema(responses=LocationChangeOutput(many=True))
    def get(self, _request: Request, **_kwargs: Any) -> Response:
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(LocationChangeOutput(page, many=True).data)

    @extend_schema(request=LocationChangeInput, responses={201: LocationChangeOutput})
    def post(self, request: Request, tenant_id: int, region_id: int) -> Response:
        self.get_region()
        data = LocationChangeInput(data=request.data)
        data.is_valid(raise_exception=True)
        change = request_location_change(
            cast(User, request.user), region_id, tenant_id, **data.validated_data
        )
        return Response(LocationChangeOutput(change).data, status=201)


class LocationChangeWithdrawView(APIView):
    @extend_schema(request=EmptyInput, responses=LocationChangeOutput)
    def post(self, request: Request, tenant_id: int, region_id: int, request_id: int) -> Response:
        tenant_region(tenant_id, region_id)
        data = EmptyInput(data=request.data)
        data.is_valid(raise_exception=True)
        change = withdraw_location_change(
            cast(User, request.user), tenant_id, region_id, request_id
        )
        return Response(LocationChangeOutput(change).data)


class RegionForecastView(APIView):
    @extend_schema(parameters=[ForecastQuery], responses=ForecastOutput)
    def get(self, request: Request, tenant_id: int, region_id: int) -> Response:
        with read_snapshot():
            region = tenant_region(tenant_id, region_id)
            query = ForecastQuery(data=request.query_params)
            query.is_valid(raise_exception=True)
            result = regional_outlook(
                region,
                query.validated_data["date"],
                query.validated_data["data_source"],
                timezone.now(),
            )
        return Response(ForecastOutput(result).data)
