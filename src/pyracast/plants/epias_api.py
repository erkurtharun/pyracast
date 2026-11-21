from typing import Any, cast

from django.contrib.auth.models import User
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from pyracast.metering import operations
from pyracast.metering.models import EpiasConnection, EpiasHealth, EpiasJobError, EpiasState
from pyracast.plants.api import ScopedView


class EpiasConnectionOutput(serializers.Serializer[Any]):
    state = serializers.ChoiceField(choices=EpiasState.choices)
    connected = serializers.BooleanField()
    disconnected_at = serializers.DateTimeField(allow_null=True)
    health = serializers.ChoiceField(choices=EpiasHealth.choices, allow_null=True)
    complete_through = serializers.DateField(allow_null=True)
    failed_days = serializers.IntegerField()
    provider_plant_id = serializers.IntegerField(allow_null=True)
    settlement_plant_id = serializers.IntegerField(allow_null=True)
    first_settlement_date = serializers.DateField(allow_null=True)
    last_settlement_date = serializers.DateField(allow_null=True)
    eic = serializers.CharField(allow_blank=True)
    name = serializers.CharField(allow_blank=True)
    last_success_at = serializers.DateTimeField(allow_null=True)
    last_error = serializers.ChoiceField(choices=EpiasJobError.choices, allow_blank=True)
    pending_jobs = serializers.IntegerField()
    archived_days = serializers.IntegerField()


class EpiasConnectionView(ScopedView[EpiasConnection]):
    serializer_class = EpiasConnectionOutput

    @extend_schema(responses=EpiasConnectionOutput)
    def get(self, request: Request, **_kwargs: Any) -> Response:
        result = operations.get_epias_status(self.plant().pk, actor=cast(User, request.user))
        return Response(EpiasConnectionOutput(result).data)
