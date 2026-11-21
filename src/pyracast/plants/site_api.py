from typing import Any, cast

from django.contrib.auth.models import User
from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema, extend_schema_field
from rest_framework import serializers
from rest_framework.generics import GenericAPIView
from rest_framework.request import Request
from rest_framework.response import Response

from pyracast.api.schema import LOCATION_REFUSAL
from pyracast.api.serializers import RegionInput
from pyracast.notifications.overview import reminder_summaries
from pyracast.notifications.serializers import ReminderSummaryOutput
from pyracast.plants.operations import PlantEditBlock
from pyracast.plants.serializers import PlantInput, PlantOutput
from pyracast.plants.sites import Site, SiteAttention, site_queryset, sites, submit_site
from pyracast.regions.models import Region, RegionStatus


class SiteInput(RegionInput, PlantInput):
    def location(self) -> dict[str, Any]:
        return {name: self.validated_data[name] for name in ("latitude", "longitude", "timezone")}


class SiteOutput(serializers.Serializer[Site]):
    id = serializers.IntegerField(source="region.id", read_only=True)
    name = serializers.CharField(source="region.name", read_only=True)
    latitude = serializers.FloatField(source="region.latitude", read_only=True)
    longitude = serializers.FloatField(source="region.longitude", read_only=True)
    timezone = serializers.CharField(source="region.timezone", read_only=True)
    revision = serializers.IntegerField(source="region.revision", read_only=True)
    status = serializers.ChoiceField(
        source="region.status", choices=RegionStatus.choices, read_only=True
    )
    reviewed_at = serializers.DateTimeField(
        source="region.reviewed_at", allow_null=True, read_only=True
    )
    review_reason = serializers.CharField(
        source="region.review_reason", allow_null=True, read_only=True
    )
    plant = PlantOutput(allow_null=True, read_only=True)
    attention = serializers.ChoiceField(
        choices=SiteAttention.choices,
        allow_null=True,
        read_only=True,
        help_text="The first thing the plant needs from the customer; none when nothing.",
    )
    edit_blocked_reason = serializers.ChoiceField(
        choices=PlantEditBlock.choices,
        allow_null=True,
        read_only=True,
        help_text="Why the plant's technical details cannot change now; none when they can.",
    )
    reminder = serializers.SerializerMethodField()

    @extend_schema_field(ReminderSummaryOutput(allow_null=True))
    def get_reminder(self, site: Site) -> dict[str, Any] | None:
        summary = self.context.get("reminders", {}).get(site.region.pk)
        return None if summary is None else dict(ReminderSummaryOutput(summary).data)


class SitesView(GenericAPIView[Region]):
    def get_queryset(self) -> QuerySet[Region]:
        return site_queryset(self.kwargs["tenant_id"])

    @extend_schema(responses=SiteOutput(many=True))
    def get(self, request: Request, tenant_id: int) -> Response:
        page = cast(list[Region], self.paginate_queryset(self.get_queryset()))
        reminders = reminder_summaries(tenant_id, [region.pk for region in page])
        rows = serializers.ListSerializer(
            sites(page), child=SiteOutput(), context={"reminders": reminders}
        )
        return self.get_paginated_response(rows.data)

    @extend_schema(request=SiteInput, responses={201: SiteOutput, 400: LOCATION_REFUSAL})
    def post(self, request: Request, tenant_id: int) -> Response:
        data = SiteInput(data=request.data)
        data.is_valid(raise_exception=True)
        region = submit_site(
            cast(User, request.user), tenant_id, data.parameters(), **data.location()
        )
        return Response(SiteOutput(sites([region])[0]).data, status=201)
