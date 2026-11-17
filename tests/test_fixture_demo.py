import pytest
from django.conf import LazySettings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError

from pyracast.accounts.models import Tenant
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.management.commands import fixture_demo
from pyracast.forecasts.models import CollectionJob, ForecastResult
from pyracast.plants.models import ForecastIssuance, Plant
from pyracast.regions.models import Region

pytestmark = pytest.mark.django_db


def test_fixture_demo(settings: LazySettings) -> None:
    with pytest.raises(CommandError, match="PYRACAST_DEBUG"):
        call_command("fixture_demo", "--username", "demo")
    settings.DEBUG = True
    with pytest.raises(CommandError, match="superuser"):
        call_command("fixture_demo", "--username", "demo")
    User.objects.create_user(username="demo", is_superuser=True)
    path = call_command("fixture_demo", "--username", "demo")
    assert "/forecast/?date=" in path
    assert ForecastResult.objects.get().payload["complete"] is True
    reference = SiteBinding.objects.get()
    assert reference.capacity_kw == reference.dc_capacity_kw == 1000
    assert reference.azimuth_degrees == reference.tilt_degrees == reference.losses_percent == 0


@pytest.fixture
def demo_user(settings: LazySettings) -> User:
    settings.DEBUG = True
    return User.objects.create_user(username="demo", is_superuser=True)


def test_fixture_demo_ends_on_a_plant_forecast(demo_user: User) -> None:
    path = call_command("fixture_demo", "--username", "demo")

    plant = Plant.objects.get()
    assert f"/plants/{plant.pk}/forecast/?date=" in path
    assert ForecastIssuance.objects.filter(plant=plant).exists()


def test_fixture_demo_reruns_and_reports_the_existing_demo(demo_user: User) -> None:
    first = call_command("fixture_demo", "--username", "demo")

    second = call_command("fixture_demo", "--username", "demo")

    assert second == first
    assert Region.objects.count() == Plant.objects.count() == 1
    assert ForecastResult.objects.count() == 1


def test_fixture_demo_resumes_after_an_interrupted_run(
    demo_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(*args: object, **kwargs: object) -> bool:
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(fixture_demo, "collect", interrupted)
        with pytest.raises(KeyboardInterrupt):
            call_command("fixture_demo", "--username", "demo")

    call_command("fixture_demo", "--username", "demo")

    assert CollectionJob.objects.get().status == "succeeded"
    assert ForecastResult.objects.count() == 1


def test_each_superuser_gets_an_own_demo_workspace(demo_user: User) -> None:
    User.objects.create_user(username="second", is_superuser=True)

    call_command("fixture_demo", "--username", "demo")
    call_command("fixture_demo", "--username", "second")

    assert Tenant.objects.count() == 2
    assert Plant.objects.count() == 2


def test_fixture_demo_reports_a_refused_step_as_a_command_error(
    demo_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(*args: object, **kwargs: object) -> Plant:
        raise ValidationError("plant_limit_reached")

    monkeypatch.setattr(fixture_demo, "create_plant", refused)

    with pytest.raises(CommandError, match=r"stopped: .*plant_limit_reached"):
        call_command("fixture_demo", "--username", "demo")


@pytest.mark.parametrize(("status", "succeeds"), [("succeeded", True), ("retry", False)])
def test_fixture_demo_reports_a_collection_that_did_not_publish(
    demo_user: User, monkeypatch: pytest.MonkeyPatch, status: str, succeeds: bool
) -> None:
    def deferred(job_id: int, *args: object, **kwargs: object) -> bool:
        CollectionJob.objects.filter(pk=job_id).update(status=status)
        return False

    monkeypatch.setattr(fixture_demo, "collect", deferred)

    if succeeds:
        assert "/forecast/?date=" in call_command("fixture_demo", "--username", "demo")
    else:
        with pytest.raises(CommandError, match="retry"):
            call_command("fixture_demo", "--username", "demo")
