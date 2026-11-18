from typing import cast

from django.contrib.auth.models import User
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from pyracast.api.serializers import InputSerializer
from pyracast.forecasts.models import CollectionEndpoint, CollectionJobError, CollectionJobStatus
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


class CollectionJobOutput(serializers.Serializer[dict[str, object]]):
    id = serializers.IntegerField()
    endpoint = serializers.ChoiceField(choices=CollectionEndpoint.choices)
    status = serializers.ChoiceField(choices=CollectionJobStatus.choices)
    scheduled_slot = serializers.DateTimeField()
    available_at = serializers.DateTimeField()
    retry_until = serializers.DateTimeField(allow_null=True)
    attempts = serializers.IntegerField()
    last_error = serializers.ChoiceField(choices=CollectionJobError.choices, allow_blank=True)


class CollectionScheduleOutput(CollectionScheduleInput):
    timezone = serializers.CharField()
    effective_from = serializers.DateTimeField()
    last_success_at = serializers.DateTimeField(allow_null=True)
    next_collection_at = serializers.DateTimeField(allow_null=True)
    recent_jobs = CollectionJobOutput(many=True)
    history_requests_reserved_per_day = serializers.IntegerField()
    history_local_time = serializers.CharField()
    history_retry_until_local = serializers.CharField()
    history_retry_minutes = serializers.IntegerField()
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
