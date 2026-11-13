import time
from typing import Any

from allauth.account import app_settings
from allauth.account.internal.flows.login import get_authentication_records
from allauth.headless.internal.restkit.response import APIResponse
from allauth.headless.mfa.views import ManageRecoveryCodesView as AllauthRecoveryCodesView
from allauth.mfa.models import Authenticator
from django.http import HttpRequest, HttpResponseBase


def recently_reauthenticated_with_mfa(request: HttpRequest) -> bool:
    user = request.user
    if not user.is_authenticated:
        return False
    now = time.time()
    authenticator_ids = []
    for record in get_authentication_records(request, user):
        at = record.get("at")
        if (
            record.get("method") == "mfa"
            and record.get("type") in {"totp", "recovery_codes"}
            and record.get("reauthenticated")
            and isinstance(at, (int, float))
            and 0 <= now - at < app_settings.REAUTHENTICATION_TIMEOUT
        ):
            authenticator_ids.append(record.get("id"))
    return Authenticator.objects.filter(user=user, pk__in=authenticator_ids).exists()


class ManageRecoveryCodesView(AllauthRecoveryCodesView):
    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        user = request.user
        if (user.is_staff or user.is_superuser) and not recently_reauthenticated_with_mfa(request):
            return APIResponse(
                request,
                status=401,
                meta={"is_authenticated": True},
                errors=[
                    {
                        "code": "mfa_reauthentication_required",
                        "message": (
                            "Verify an authenticator before managing administrator recovery codes."
                        ),
                    }
                ],
            )
        return super().handle(request, *args, **kwargs)
