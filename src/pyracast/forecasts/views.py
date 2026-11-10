from typing import cast

from django.contrib.auth.models import User
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from pyracast.api.serializers import InputSerializer
from pyracast.forecasts.schedule import (
    MAX_COLLECTION_TIMES,
    configure_schedule,
    get_schedule,
    schedule_payload,
)


class CollectionScheduleInput(InputSerializer):
    enabled = serializers.BooleanField()
    times = serializers.ListField(
        child=serializers.CharField(max_length=5), max_length=MAX_COLLECTION_TIMES
    )


class CollectionScheduleOutput(CollectionScheduleInput):
    timezone = serializers.CharField()
    effective_from = serializers.DateTimeField()
    daily_request_limit = serializers.IntegerField()
    used_today = serializers.IntegerField()
    remaining_today = serializers.IntegerField()
    history_requests_per_day = serializers.IntegerField()
    max_forecast_times = serializers.IntegerField()


class CollectionScheduleView(APIView):
    @extend_schema(responses=CollectionScheduleOutput)
    def get(self, request: Request, tenant_id: int, region_id: int) -> Response:
        now = timezone.now()
        schedule = get_schedule(cast(User, request.user), tenant_id, region_id, now)
        return Response(schedule_payload(schedule, now))

    @extend_schema(request=CollectionScheduleInput, responses=CollectionScheduleOutput)
    def put(self, request: Request, tenant_id: int, region_id: int) -> Response:
        data = CollectionScheduleInput(data=request.data)
        data.is_valid(raise_exception=True)
        now = timezone.now()
        schedule = configure_schedule(
            cast(User, request.user), tenant_id, region_id, now=now, **data.validated_data
        )
        return Response(schedule_payload(schedule, now))
