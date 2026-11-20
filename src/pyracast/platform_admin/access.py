from typing import TYPE_CHECKING, cast

from django.contrib import auth, messages
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods, require_POST

from pyracast.accounts.language import save_user_language, set_language_cookie
from pyracast.accounts.processes import confirmation
from pyracast.languages import explicit_language
from pyracast.platform_admin.forms import ConfirmationForm
from pyracast.platform_admin.navigation import console_destination

if TYPE_CHECKING:
    from pyracast.platform_admin.site import PlatformAdminSite


@never_cache
@csrf_protect
@require_POST
def logout(request: HttpRequest) -> HttpResponse:
    # Outside the console gate, so a session that lost console access can still end itself.
    auth.logout(request)
    return redirect("/app/")


@require_POST
def language(request: HttpRequest) -> HttpResponse:
    chosen = explicit_language(request.POST.get("language"))
    if chosen is None:
        return HttpResponse(status=400)
    save_user_language(cast(User, request.user), chosen)
    request.LANGUAGE_CODE = chosen
    response = redirect(console_destination(request, request.POST.get("next")))
    set_language_cookie(response, chosen)
    return response


@require_http_methods(["GET", "POST"])
def confirm(request: HttpRequest, *, site: PlatformAdminSite) -> HttpResponse:
    data = request.POST if request.method == "POST" else request.GET
    destination = console_destination(request, data.get("next"))
    form = ConfirmationForm(request.POST if request.method == "POST" else None, user=request.user)
    if request.method == "POST":
        with confirmation(request):
            confirmed = form.is_valid()
            if confirmed:
                form.save()
        if confirmed:
            messages.success(request, _("Confirmed with your authenticator."))
            return redirect(destination)
    return site.confirmation(
        request, form, destination, status=400 if request.method == "POST" else 200
    )
