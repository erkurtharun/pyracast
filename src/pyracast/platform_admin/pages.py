from typing import TYPE_CHECKING, cast

from django.contrib import messages
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_safe

from pyracast.notifications.operations import set_recipient_limit
from pyracast.platform_admin.forms import form_data
from pyracast.platform_admin.read_models import (
    operations_context,
    plant_list_context,
    settings_context,
)

if TYPE_CHECKING:
    from pyracast.platform_admin.site import PlatformAdminSite


@require_safe
def plants(request: HttpRequest, *, site: PlatformAdminSite) -> HttpResponse:
    context = plant_list_context(
        request.GET.get("q", "").strip()[:200],
        request.GET.get("status", ""),
        request.GET.get("page", "1"),
        request.GET.get("issue", "")[:80],
    )
    query = request.GET.copy()
    query.pop("page", None)
    context["pagination_query"] = query.urlencode()
    return site.workspace_response(request, "plants", _("Plants"), context)


@require_safe
def operations(request: HttpRequest, *, site: PlatformAdminSite) -> HttpResponse:
    return site.workspace_response(
        request,
        "operations",
        _("Operations"),
        operations_context(request.GET.get("issue", "")[:80]),
    )


@require_http_methods(["GET", "POST"])
def settings(request: HttpRequest, *, site: PlatformAdminSite) -> HttpResponse:
    context = settings_context(form_data(request))
    form = context["settings_form"]
    if request.method == "POST" and form.is_valid():
        try:
            change = set_recipient_limit(
                cast(User, request.user),
                form.cleaned_data["max_recipients"],
                expected=form.cleaned_data["expected_max_recipients"],
            )
        except ValidationError as refusal:
            form.add_error(None, refusal)
        else:
            if change.changed:
                messages.success(
                    request,
                    _("Recipient limit changed from %(previous)s to %(current)s.")
                    % {"previous": change.previous, "current": change.current},
                )
            else:
                messages.info(
                    request,
                    _("The recipient limit is already %(limit)s.") % {"limit": change.current},
                )
            return redirect("admin:workspace_settings")
    return site.workspace_response(
        request,
        "settings",
        _("Settings"),
        context,
        status=400 if request.method == "POST" else 200,
    )
