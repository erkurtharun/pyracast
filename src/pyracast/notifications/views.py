from typing import Any, cast

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.utils.serializer_helpers import ReturnDict
from rest_framework.views import APIView

from pyracast.db import read_snapshot
from pyracast.notifications.operations import configure_reminders, get_reminder_schedule
from pyracast.notifications.overview import reminder_overview
from pyracast.notifications.serializers import ReminderInput, ReminderOutput


def _saved_answer(
    request: Request, tenant_id: int, region_id: int, *, keep: bool
) -> ReturnDict[Any, Any]:
    data = ReminderInput(data=request.data)
    data.is_valid(raise_exception=True)
    now = timezone.now()
    with transaction.atomic():
        schedule = configure_reminders(
            cast(User, request.user), tenant_id, region_id, now=now, **data.validated_data
        )
        answer = ReminderOutput(reminder_overview(schedule, now, administrator=True)).data
        if not keep:
            transaction.set_rollback(True)
    return answer


class RemindersView(APIView):
    @extend_schema(responses=ReminderOutput)
    def get(self, request: Request, tenant_id: int, region_id: int) -> Response:
        now = timezone.now()
        with read_snapshot():
            schedule, administrator = get_reminder_schedule(
                cast(User, request.user), tenant_id, region_id, now
            )
            overview = reminder_overview(schedule, now, administrator=administrator)
        return Response(ReminderOutput(overview).data)

    @extend_schema(request=ReminderInput, responses=ReminderOutput)
    def put(self, request: Request, tenant_id: int, region_id: int) -> Response:
        return Response(_saved_answer(request, tenant_id, region_id, keep=True))


class ReminderPreviewView(APIView):
    @extend_schema(request=ReminderInput, responses=ReminderOutput)
    def post(self, request: Request, tenant_id: int, region_id: int) -> Response:
        return Response(_saved_answer(request, tenant_id, region_id, keep=False))
