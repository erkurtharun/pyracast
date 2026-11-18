import pytest

from pyracast.forecasts.models import CollectionSchedule
from pyracast.notifications.models import ReminderSchedule
from pyracast.plants.models import CalibrationRun, Plant, ProductionObservation
from tests.support.browser import (
    Gateway,
    run_spec,
    seed_accounts,
    seed_forecast_days,
    seed_other_workspace,
)

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


def test_a_member_configures_plants_production_and_forecast_mail(web_gateway: Gateway) -> None:
    accounts = seed_accounts()
    system = accounts.system
    days = seed_forecast_days(system)
    other = seed_other_workspace()

    run_spec(
        "workspace.spec.ts",
        web_gateway,
        BROWSER_EMAIL=system.member.email,
        BROWSER_PASSWORD=accounts.password,
        BROWSER_NOW=days.now.isoformat(),
        BROWSER_DATE=days.target.isoformat(),
        BROWSER_TODAY=days.today.isoformat(),
        BROWSER_HISTORY_DATE=days.history.isoformat(),
        BROWSER_OTHER_TENANT=str(other.tenant.pk),
        BROWSER_OTHER_REGION=str(other.region.pk),
    )

    plant = Plant.objects.select_related("current_configuration").get(
        region=system.region, name="Browser plant"
    )
    configuration = plant.current_configuration
    assert configuration is not None
    assert configuration.dc_capacity_mw == 130
    assert configuration.inverter_capacity_mw == 100
    assert configuration.export_limit_mw is None
    assert configuration.revision == 2
    assert configuration.mounting_type == "fixed"
    assert configuration.surface_tilt == 33
    assert configuration.surface_azimuth == 180
    system.region.refresh_from_db()
    assert system.region.deleted_at is None
    revisions = list(ProductionObservation.objects.filter(plant=plant).order_by("id"))
    assert [row.energy_mwh for row in revisions] == [1.25, 2.5, 2.5, 1.25]
    assert [row.is_reverted for row in revisions] == [False, False, True, False]
    assert [row.is_current for row in revisions] == [False, False, False, True]
    assert [row.supersedes_id for row in revisions] == [None, *(row.pk for row in revisions[:-1])]
    observation = revisions[-1]
    assert observation.energy_mwh == 1.25
    assert observation.configuration_id != plant.current_configuration_id
    assert observation.configuration.revision == 1
    assert not CalibrationRun.objects.filter(plant=plant).exists()
    schedule = CollectionSchedule.objects.get(region=system.region)
    assert schedule.enabled
    assert schedule.times == ["09:00", "16:00"]
    reminder = ReminderSchedule.objects.get(configured_by=system.member, region=system.region)
    assert reminder.enabled
    assert reminder.recipients == ["forecast-one@example.test", "forecast-two@example.test"]
    assert reminder.times == ["10:00"]
