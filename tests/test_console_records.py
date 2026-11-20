import pytest
from django.utils import timezone

from pyracast.regions.models import Region
from tests.support.api import verified_admin
from tests.support.plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True)
PLANT_RECORDS = (
    "regions/region",
    "connections/sitebinding",
    "forecasts/collectionjob",
    "forecasts/requestattempt",
    "plants/plant",
    "plants/plantconfiguration",
)


def test_the_record_browser_leaves_out_every_record_of_a_trashed_plant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _ = make_plant(monkeypatch)
    client, _ = verified_admin()
    listed = {
        path: client.get(f"/admin/{path}/").context["cl"].result_count for path in PLANT_RECORDS
    }
    assert all(listed.values()), listed

    Region.objects.filter(pk=system.region.pk).update(deleted_at=timezone.now())

    hidden = {path: client.get(f"/admin/{path}/").context["cl"] for path in PLANT_RECORDS}
    assert {path: changelist.result_count for path, changelist in hidden.items()} == dict.fromkeys(
        PLANT_RECORDS, 0
    )
    assert {
        path: changelist.full_result_count for path, changelist in hidden.items()
    } == dict.fromkeys(PLANT_RECORDS, 0)
    events = client.get("/admin/regions/auditevent/").context["cl"].result_list
    assert [event.region_id for event in events if event.region_id is not None] == []
    record = client.get(f"/admin/regions/region/{system.region.pk}/change/")
    assert (record.status_code, record["Location"]) == (302, "/admin/")


def test_the_record_browser_keeps_provider_account_events(monkeypatch: pytest.MonkeyPatch) -> None:
    system, _ = make_plant(monkeypatch)
    client, _ = verified_admin()
    Region.objects.filter(pk=system.region.pk).update(deleted_at=timezone.now())

    events = client.get("/admin/regions/auditevent/").context["cl"].result_list

    assert [event.provider_account_id for event in events] != []
