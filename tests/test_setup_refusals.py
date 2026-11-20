import pytest

from pyracast.connections.models import SiteBinding
from tests.support.admin_setup import admin_system, setup_input, setup_url
from tests.support.api import verified_admin

pytestmark = pytest.mark.django_db

GENERIC = "The setup could not be applied."


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"capacity_kw": 0}, "Positive finite capacity is required."),
        ({"dc_capacity_kw": 0}, "DC capacity must be finite and positive when specified."),
    ],
)
def test_a_refused_provider_setup_names_its_reason(changes: dict[str, object], reason: str) -> None:
    system = admin_system()
    client, _ = verified_admin()
    bindings = SiteBinding.objects.count()

    response = client.post(setup_url(system), setup_input(system, **changes))

    page = response.content.decode()
    assert response.status_code == 400
    assert reason in page
    assert GENERIC not in page
    assert SiteBinding.objects.count() == bindings
