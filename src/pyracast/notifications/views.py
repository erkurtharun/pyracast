from typing import cast

from django.contrib.auth.models import User
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from pyracast.notifications.operations import configure_reminders, get_reminder_schedule
from pyracast.notifications.overview import reminder_overview
from pyracast.notifications.serializers import ReminderInput, ReminderOutput


class RemindersView(APIView):
    @extend_schema(responses=ReminderOutput)
    def get(self, request: Request, tenant_id: int, region_id: int) -> Response:
        now = timezone.now()
        schedule, administrator = get_reminder_schedule(
            cast(User, request.user), tenant_id, region_id, now
        )
        overview = reminder_overview(schedule, now, administrator=administrator)
        return Response(ReminderOutput(overview).data)

    @extend_schema(request=ReminderInput, responses=ReminderOutput)
    def put(self, request: Request, tenant_id: int, region_id: int) -> Response:
        data = ReminderInput(data=request.data)
        data.is_valid(raise_exception=True)
        now = timezone.now()
        schedule = configure_reminders(
            cast(User, request.user), tenant_id, region_id, now=now, **data.validated_data
        )
        return Response(ReminderOutput(reminder_overview(schedule, now, administrator=True)).data)
