from typing import Any, cast

from allauth.account.internal.flows.reauthentication import get_reauthentication_flows
from allauth.headless.base.response import ForbiddenResponse, ReauthenticationResponse
from allauth.headless.base.views import APIView
from allauth.headless.internal.restkit.response import APIResponse
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponseBase
from django.utils.translation import gettext_lazy

from pyracast.accounts.security import CONFIRMATION, Factor, has_mfa_evidence, strongest_factor

FLOWS = {
    Factor.MFA: "mfa_reauthenticate",
    Factor.PASSWORD: "reauthenticate",
    Factor.CODE: "reauthenticate_by_code",
}
REFUSALS = {
    Factor.MFA: (
        "mfa_reauthentication_required",
        gettext_lazy("Enter your authenticator code or a recovery code."),
    ),
    Factor.PASSWORD: (
        "reauthentication_required",
        gettext_lazy("Confirm your password to continue."),
    ),
    Factor.CODE: (
        "reauthentication_required",
        gettext_lazy("Confirm with an email code to continue."),
    ),
}


def step_up_required(request: HttpRequest, factor: Factor | None = None) -> APIResponse:
    # The account is read again: a credential change during the request may have changed which
    # factor a new proof needs.
    signed_in = cast(User, request.user)
    user = User.objects.filter(pk=signed_in.pk).first() or signed_in
    factor = factor or strongest_factor(user)
    code, message = REFUSALS[factor]
    flows = [flow for flow in get_reauthentication_flows(user) if flow["id"] == FLOWS[factor]]
    return APIResponse(
        request,
        status=401,
        data={"flows": flows},
        meta={"is_authenticated": True},
        errors=[{"code": code, "message": str(message)}],
    )


def require_recent_mfa(request: HttpRequest) -> APIResponse | None:
    if request.user.is_authenticated and not has_mfa_evidence(request, within=CONFIRMATION):
        return step_up_required(request, Factor.MFA)
    return None


class StepUpAnswers(APIView):
    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        response = super().dispatch(request, *args, **kwargs)
        if isinstance(response, ReauthenticationResponse):
            return step_up_required(request)
        return response


class StrongestFactor(APIView):
    # A reauthentication view proves `factor`; an account holding a stronger one may not use it.
    factor: Factor

    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        user = request.user
        if user.is_authenticated and strongest_factor(user) is not self.factor:
            return ForbiddenResponse(request)
        return super().handle(request, *args, **kwargs)
