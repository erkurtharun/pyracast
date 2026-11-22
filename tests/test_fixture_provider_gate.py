import pytest
from django.conf import LazySettings
from django.core.exceptions import ValidationError

from pyracast.connections.contracts import ProviderError
from pyracast.connections.operations import create_account
from pyracast.providers import FixtureProvider, adapter_for
from tests.support.domain import make_system

pytestmark = pytest.mark.django_db(transaction=True)


def test_the_fixture_provider_answers_only_under_its_setting(settings: LazySettings) -> None:
    system = make_system()
    settings.PYRACAST_FIXTURE_PROVIDER = True
    assert isinstance(adapter_for(system.binding), FixtureProvider)

    settings.PYRACAST_FIXTURE_PROVIDER = False

    with pytest.raises(ProviderError) as refusal:
        adapter_for(system.binding)
    assert refusal.value.code == "fixture_provider_disabled"


def test_a_fixture_account_cannot_be_created_without_the_setting(
    settings: LazySettings,
) -> None:
    system = make_system()
    settings.PYRACAST_FIXTURE_PROVIDER = False

    with pytest.raises(ValidationError):
        create_account(system.admin, provider="fixture", external_account_id="another-fixture")
