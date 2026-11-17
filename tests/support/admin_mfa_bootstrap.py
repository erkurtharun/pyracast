from allauth.account.models import EmailAddress
from django.contrib.auth.models import User

TEST_PASSWORD = "test-only-" + "z" * 40


def administrator() -> User:
    user = User.objects.create_user(
        username="operator",
        email="operator@example.com",
        password=TEST_PASSWORD,
        is_staff=True,
        is_superuser=True,
    )
    EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
    return user
