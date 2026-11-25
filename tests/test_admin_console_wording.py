import pytest
from django.urls import reverse

from pyracast.accounts.models import Tenant
from pyracast.regions.models import Region
from tests.support.admin_setup import admin_system, setup_url, submitted_plant
from tests.support.api import verified_admin

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


def _plants(language: str) -> str:
    system = admin_system()
    submitted_plant(system)
    Region.objects.create(
        tenant=Tenant.objects.create(name="Other company"),
        name="Paused park",
        latitude=1,
        longitude=0,
        timezone="UTC",
        status="suspended",
    )
    client, _ = verified_admin()
    return client.get("/admin/plants/", HTTP_ACCEPT_LANGUAGE=language).content.decode()


def test_plant_list_counts_and_names_statuses_in_whole_turkish_sentences() -> None:
    html = _plants("tr")
    assert ">2 santral</h2>" in html
    assert "santrals" not in html
    assert ">\u0130ncelemede</span>" in html
    assert ">Ask\u0131ya al\u0131nd\u0131</span>" in html
    assert ">Suspended</span>" not in html


def test_plant_list_counts_and_names_statuses_in_english() -> None:
    html = _plants("en")
    assert ">2 plants</h2>" in html
    assert ">In review</span>" in html
    assert ">Suspended</span>" in html


def test_plant_review_names_the_review_status_from_its_enumeration() -> None:
    system = admin_system()
    Region.objects.filter(pk=system.region.pk).update(status="suspended")
    client, _ = verified_admin()
    html = client.get(setup_url(system), HTTP_ACCEPT_LANGUAGE="tr").content.decode()
    label = "Ask\u0131ya al\u0131nd\u0131"
    assert html.count(f'<span class="badge badge--suspended">{label}</span>') == 1
    assert f'<span class="step-state">{label}</span>' in html


def test_plant_list_states_an_empty_filter_and_the_page_in_one_sentence() -> None:
    admin_system()
    client, _ = verified_admin()
    html = client.get(
        "/admin/plants/?q=no-match&status=approved", HTTP_ACCEPT_LANGUAGE="tr"
    ).content.decode()
    assert "Bu filtrelerle e\u015fle\u015fen santral yok." in html


def test_recipient_limit_is_a_translated_plural() -> None:
    admin_system()
    client, _ = verified_admin()
    html = client.get(
        reverse("admin:workspace_settings"), HTTP_ACCEPT_LANGUAGE="tr"
    ).content.decode()
    assert "<dd>Santral ba\u015f\u0131na 5 al\u0131c\u0131</dd>" in html
    assert "al\u0131c\u0131s" not in html
    english = client.get(reverse("admin:workspace_settings"), HTTP_ACCEPT_LANGUAGE="en").content
    assert b"<dd>5 recipients per plant</dd>" in english
