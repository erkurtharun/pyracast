import pytest

from pyracast.notifications.models import NotificationSettings
from tests.support.browser import (
    Gateway,
    run_spec,
    seed_accounts,
    seed_forecast_days,
    seed_other_workspace,
    seed_review,
)

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


def test_an_mfa_administrator_reviews_a_plant_and_changes_settings_under_csp(
    web_gateway: Gateway,
) -> None:
    accounts = seed_accounts()
    seed_forecast_days(accounts.system)
    review = seed_review(accounts, seed_other_workspace())

    run_spec(
        "admin-csp.spec.ts",
        web_gateway,
        BROWSER_ADMIN_EMAIL=accounts.system.admin.email,
        BROWSER_ADMIN_PASSWORD=accounts.password,
        BROWSER_ADMIN_TOTP_SEED=accounts.admin_totp,
        BROWSER_REVIEW_REGION=str(review.region.pk),
    )

    review.region.refresh_from_db()
    review.location_change.refresh_from_db()
    assert review.region.status == "approved"
    assert review.location_change.status == "rejected"
    assert NotificationSettings.objects.get().max_recipients == 6
