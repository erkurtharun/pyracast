from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.views.decorators.debug import sensitive_variables

from pyracast.accounts.models import Membership, Tenant


@sensitive_variables("password")
def register_company(*, company_name: str, username: str, email: str, password: str) -> User:
    tenant = Tenant(name=company_name.strip())
    tenant.full_clean()
    user = User(
        username=User.normalize_username(username), email=User.objects.normalize_email(email)
    )
    user.full_clean(exclude={"password"})
    try:
        validate_password(password, user)
    except ValidationError as error:
        raise ValidationError({"password": error.messages}) from None
    user.set_password(password)
    try:
        with transaction.atomic():
            user.save(force_insert=True)
            tenant.save(force_insert=True)
            Membership.objects.create(user=user, tenant=tenant)
    except IntegrityError:
        if User.objects.filter(username=user.username).exists():
            raise ValidationError({"username": "This username is already in use."}) from None
        raise
    return user
