from allauth.account.internal.flows.login import record_authentication
from allauth.mfa.models import Authenticator
from django.contrib.auth.models import User
from django.test import Client, RequestFactory


def verified_admin(*, enforce_csrf_checks: bool = False) -> tuple[Client, User]:
    user = User.objects.create_user(username="operator", is_superuser=True, is_staff=True)
    authenticator = Authenticator.objects.create(user=user, type="totp", data={})
    client = Client(enforce_csrf_checks=enforce_csrf_checks)
    client.force_login(user)
    request = RequestFactory().get("/admin/")
    request.user = user
    request.session = client.session
    record_authentication(request, user, method="mfa", type="totp", id=authenticator.pk)
    request.session.save()
    return client, user
