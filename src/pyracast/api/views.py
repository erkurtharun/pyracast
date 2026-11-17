from typing import Any, cast

from django.contrib.auth.models import User
from django.db.models import Count, F, Q, QuerySet
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.generics import GenericAPIView
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from pyracast import trash
from pyracast.accounts.authorization import tenant_membership
from pyracast.accounts.models import Tenant
from pyracast.api.serializers import (
    ForecastOutput,
    ForecastQuery,
    LocationChangeInput,
    LocationChangeOutput,
    TenantOutput,
)
from pyracast.change_requests.models import ChangeRequest
from pyracast.db import read_snapshot
from pyracast.forecasts.history import regional_outlook
from pyracast.regions.models import Region, RegionLocationChangeRequest
from pyracast.regions.operations import request_location_change


def member_region(actor: User, tenant_id: int, region_id: int) -> Region:
    tenant_membership(actor, tenant_id)
    return get_object_or_404(Region, pk=region_id, tenant_id=tenant_id, deleted_at__isnull=True)


class TenantsView(GenericAPIView[Tenant]):
    serializer_class = TenantOutput

    def get_queryset(self) -> QuerySet[Tenant]:
        return (
            Tenant.objects.filter(membership__user=cast(User, self.request.user))
            .annotate(role=F("membership__role"), current_membership_id=F("membership__pk"))
            .order_by("id")
        )

    @extend_schema(responses=TenantOutput(many=True))
    def get(self, _request: Request) -> Response:
        page = list(self.paginate_queryset(self.get_queryset()) or [])
        membership_ids = [row.__dict__["current_membership_id"] for row in page]
        counters = (
            ChangeRequest.objects.filter(
                plant__region__tenant_id__in=[row.pk for row in page],
                plant__deleted_at__isnull=True,
                plant__region__deleted_at__isnull=True,
            )
            .values("plant__region__tenant_id")
            .annotate(
                pending=Count("pk", filter=Q(status="pending")),
                unread=Count(
                    "pk",
                    filter=Q(
                        proposed_by_id=self.request.user.pk,
                        proposer_membership_id__in=membership_ids,
                        status__in=("approved", "rejected"),
                        outcome_seen_at__isnull=True,
                    ),
                ),
            )
        )
        by_tenant = {row["plant__region__tenant_id"]: row for row in counters}
        for row in page:
            counts = by_tenant.get(row.pk)
            row.__dict__["pending_review_count"] = (
                (counts["pending"] if counts else 0) if row.__dict__["role"] == "admin" else None
            )
            row.__dict__["unread_outcome_count"] = counts["unread"] if counts else 0
        return self.get_paginated_response(TenantOutput(page, many=True).data)


class RegionView(APIView):
    @extend_schema(responses={204: None})
    def delete(self, request: Request, tenant_id: int, region_id: int) -> Response:
        trash.trash_region(cast(User, request.user), tenant_id, region_id)
        return Response(status=204)


class LocationChangeRequestsView(GenericAPIView[RegionLocationChangeRequest]):
    serializer_class = LocationChangeOutput

    def get_region(self) -> Region:
        return member_region(
            cast(User, self.request.user), self.kwargs["tenant_id"], self.kwargs["region_id"]
        )

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


class ForecastView(APIView):
    @extend_schema(parameters=[ForecastQuery], responses=ForecastOutput)
    def get(self, request: Request, tenant_id: int, region_id: int) -> Response:
        with read_snapshot():
            region = member_region(cast(User, request.user), tenant_id, region_id)
            query = ForecastQuery(data=request.query_params)
            query.is_valid(raise_exception=True)
            result = regional_outlook(
                region,
                query.validated_data["date"],
                query.validated_data["data_source"],
                timezone.now(),
            )
        return Response(result)
