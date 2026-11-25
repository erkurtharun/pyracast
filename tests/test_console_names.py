import pytest
from django.test import Client
from django.urls import reverse
from django.utils import translation
from django.utils.functional import Promise
from django.utils.translation import gettext

from pyracast.accounts.invitations import invite_workspace_member
from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.notifications.models import NotificationDeliveryError, NotificationDeliveryStatus
from pyracast.plants.models import MeasurementBasis
from pyracast.plants.parameters import PlantParameters
from pyracast.plants.sites import submit_site
from pyracast.platform_admin.site import site
from pyracast.regions.models import AuditAction, Region, RegionStatus
from tests.support.admin_setup import admin_system, setup_url, submitted_plant
from tests.support.api import verified_admin
from tests.support.domain import make_system
from tests.support.workspaces import verified_account

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


def page(path: str, language: str, client: Client | None = None) -> str:
    client = client or verified_admin()[0]
    response = client.get(path, HTTP_ACCEPT_LANGUAGE=language)
    assert response.status_code == 200
    return response.content.decode()


@pytest.mark.parametrize(
    ("language", "name", "in_review", "gone"),
    [
        ("en", "Pyracast console", "In review", ("Pyracast operations", "Platform administration")),
        ("tr", "Pyracast konsolu", "İncelemede", ("Pyracast işlemleri", "Platform y")),
    ],
)
def test_the_console_calls_itself_the_pyracast_console(
    language: str, name: str, in_review: str, gone: tuple[str, ...]
) -> None:
    admin_system()
    html = page("/admin/", language)
    assert f"· {name}</title>" in html
    assert f"<span>{name}</span>" in html
    assert f'<p class="eyebrow">{name}</p>' in html
    assert f'aria-label="{name}"' in html
    assert f'<span class="stat-label">{in_review}</span>' in html
    assert '<p class="lead">' not in html
    for old in gone:
        assert old not in html
    with translation.override(language):
        assert {str(site.site_header), str(site.site_title), str(site.index_title)} == {name}


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (
            "en",
            (
                ">Workspace administrator</span>",
                ">Change workspace administrator</a>",
                ">Remove from workspace<",
                ">Withdraw<",
                ">Panel capacity (MWp)</th>",
                '<td data-label="Panel capacity (MWp)">1.5</td>',
                ">Pyracast app</a>",
            ),
        ),
        (
            "tr",
            (
                ">Çal\u0131şma alan\u0131 yöneticisi</span>",
                ">Çal\u0131şma alan\u0131 yöneticisini değiştir</a>",
                ">Çal\u0131şma alan\u0131ndan ç\u0131kar<",
                ">Geri çek<",
                ">Panel kurulu gücü (MWp)</th>",
                '<td data-label="Panel kurulu gücü (MWp)">1,5</td>',
                ">Pyracast uygulamas\u0131</a>",
            ),
        ),
    ],
)
def test_the_workspace_page_names_the_roles_and_their_actions(
    language: str, expected: tuple[str, ...]
) -> None:
    tenant = Tenant.objects.create(name="Solar workspace")
    owner = verified_account("owner", "owner@example.com")
    member = verified_account("member", "member@example.com")
    Membership.objects.create(tenant=tenant, user=owner, role=WorkspaceRole.ADMIN)
    Membership.objects.create(tenant=tenant, user=member)
    submit_site(
        owner,
        tenant.pk,
        PlantParameters(name="Console site", dc_capacity_mw=1.5),
        latitude=41,
        longitude=29,
        timezone="Europe/Istanbul",
    )
    client, operator = verified_admin()
    invite_workspace_member(operator, tenant.pk, "guest@example.com", platform=True)
    html = page(reverse("admin:workspace_tenant", args=(tenant.pk,)), language, client)
    for text in expected:
        assert text in html


@pytest.mark.parametrize(
    ("language", "expected", "gone"),
    [
        (
            "en",
            (
                "<dt>Workspace</dt>",
                "<dt>Location</dt>",
                "<dt>Time zone</dt>",
                "<dt>Panel capacity (MWp)</dt><dd>4.2</dd>",
                "<dt>Inverter capacity (MW AC)</dt><dd>4.0</dd>",
                "<dt>Grid export limit (MW)</dt><dd>3.8</dd>",
                "<dt>Meter point</dt><dd>Energy delivered to the grid</dd>",
                '<span class="badge">In review</span>',
                '<span class="step-name">Plant sent for approval</span>',
                "<h3>Forecast emails</h3>",
            ),
            ("Company", "DC nameplate", "Measurement point", "Email delivery"),
        ),
        (
            "tr",
            (
                "<dt>Çal\u0131şma alan\u0131</dt>",
                "<dt>Konum</dt>",
                "<dt>Saat dilimi</dt>",
                "<dt>Panel kurulu gücü (MWp)</dt><dd>4,2</dd>",
                "<dt>İnverter gücü (MW AC)</dt><dd>4,0</dd>",
                "<dt>Şebekeye veriş s\u0131n\u0131r\u0131 (MW)</dt><dd>3,8</dd>",
                "<dt>Ölçüm noktas\u0131</dt><dd>Şebekeye verilen enerji</dd>",
                '<span class="badge">İncelemede</span>',
                '<span class="step-name">Santral onaya gönderildi</span>',
                "<h3>Tahmin e-postalar\u0131</h3>",
            ),
            ("Şirket", "DC kurulu güç", "E-posta teslimi"),
        ),
    ],
)
def test_the_plant_review_names_the_technical_details(
    language: str, expected: tuple[str, ...], gone: tuple[str, ...]
) -> None:
    system = admin_system()
    submitted_plant(system)
    html = page(setup_url(system), language)
    for text in expected:
        assert text in html
    for text in gone:
        assert text not in html


@pytest.mark.parametrize(
    ("language", "status", "label", "note"),
    [
        ("en", RegionStatus.SUSPENDED, "Suspended", "This plant is not approved."),
        ("en", RegionStatus.REJECTED, "Not approved", "This plant is not approved."),
        ("tr", RegionStatus.SUSPENDED, "Ask\u0131ya al\u0131nd\u0131", "Bu santral onayl\u0131"),
        ("tr", RegionStatus.REJECTED, "Onaylanmad\u0131", "Bu santral onayl\u0131"),
    ],
)
def test_the_plant_review_shows_its_decision_once_without_a_layout_note(
    language: str, status: str, label: str, note: str
) -> None:
    system = admin_system()
    Region.objects.filter(pk=system.region.pk).update(status=status)
    html = page(setup_url(system), language)
    assert f'<span class="badge badge--{status}">{label}</span>' in html
    assert note not in html


@pytest.mark.parametrize(
    ("language", "label", "note"),
    [
        ("en", "Approved", "This plant is approved."),
        ("tr", "Onayland\u0131", "Bu santral onayl\u0131"),
    ],
)
def test_an_approved_plant_review_carries_no_layout_note(
    language: str, label: str, note: str
) -> None:
    system = make_system(approved=True)
    html = page(setup_url(system), language)
    assert f'<span class="badge badge--approved">{label}</span>' in html
    assert note not in html


@pytest.mark.parametrize(
    ("language", "heading", "gone"),
    [
        ("en", "Forecast email", "Platform-wide limits."),
        ("tr", "Tahmin e-postas\u0131", "Platform genelindeki s\u0131n\u0131rlar."),
    ],
)
def test_the_settings_page_names_the_forecast_email(language: str, heading: str, gone: str) -> None:
    admin_system()
    html = page(reverse("admin:workspace_settings"), language)
    assert f'<h2 id="reminders-title">{heading}</h2>' in html
    assert gone not in html


@pytest.mark.parametrize(
    ("language", "labels"),
    [
        (
            "en",
            {
                RegionStatus.PENDING.label: "In review",
                RegionStatus.REJECTED.label: "Not approved",
                MeasurementBasis.NET_EXPORT.label: "Energy delivered to the grid",
                MeasurementBasis.AC_GENERATION.label: "Inverter output",
                AuditAction.REGION_TRASHED.label: "Plant moved to recycle bin",
                AuditAction.COLLECTION_SCHEDULE_UPDATED.label: "Forecast update times changed",
                AuditAction.REMINDER_SCHEDULE_UPDATED.label: "Forecast email updated",
                NotificationDeliveryStatus.PENDING.label: "Preparing",
                NotificationDeliveryStatus.FAILED.label: "Not sent",
                NotificationDeliveryStatus.UNCERTAIN.label: "Not known whether sent",
                NotificationDeliveryError.DEFINITION_TRASHED.label: "Plant in recycle bin",
            },
        ),
        (
            "tr",
            {
                RegionStatus.PENDING.label: "İncelemede",
                RegionStatus.REJECTED.label: "Onaylanmad\u0131",
                MeasurementBasis.NET_EXPORT.label: "Şebekeye verilen enerji",
                MeasurementBasis.AC_GENERATION.label: "İnverter ç\u0131k\u0131ş\u0131ndaki üretim",
                AuditAction.REGION_TRASHED.label: "Santral geri dönüşüm kutusuna taş\u0131nd\u0131",
                AuditAction.COLLECTION_SCHEDULE_UPDATED.label: "Tahmin güncelleme saatleri değişti",
                AuditAction.REMINDER_SCHEDULE_UPDATED.label: "Tahmin e-postas\u0131 güncellendi",
                NotificationDeliveryStatus.PENDING.label: "Haz\u0131rlan\u0131yor",
                NotificationDeliveryStatus.FAILED.label: "Gönderilmedi",
                NotificationDeliveryStatus.UNCERTAIN.label: "Gönderildiği belli değil",
                NotificationDeliveryError.DEFINITION_TRASHED.label: (
                    "Santral geri dönüşüm kutusunda"
                ),
            },
        ),
    ],
)
def test_console_states_use_the_product_names(language: str, labels: dict[Promise, str]) -> None:
    with translation.override(language):
        assert [str(label) for label in labels] == list(labels.values())


CATALOGUE = {
    "Member: sees the plants and sends change requests": (
        "Üye: santralleri görür ve değişiklik talebi gönderir"
    ),
    "Invite the workspace administrator first.": (
        "Önce çal\u0131şma alan\u0131 yöneticisini davet edin."
    ),
    "New role of %(member)s: Member": "%(member)s için yeni rol: Üye",
    "Ask the workspace administrator to correct the saved recipient addresses.": (
        "Kay\u0131tl\u0131 al\u0131c\u0131 adreslerini çal\u0131şma alan\u0131 "
        "yöneticisi düzeltmeli."
    ),
    "Not known whether sent": "Gönderildiği belli değil",
    "Plant, location or workspace": "Santral, konum veya çal\u0131şma alan\u0131",
    "This limit pauses the forecast email of these plants:": (
        "Bu s\u0131n\u0131r şu santrallerin tahmin e-postas\u0131n\u0131 duraklat\u0131r:"
    ),
    "They no longer receive the forecast emails of this workspace.": (
        "Bu çal\u0131şma alan\u0131n\u0131n tahmin e-postalar\u0131n\u0131 art\u0131k almaz."
    ),
    "Review the forecast update times and collection settings.": (
        "Tahmin güncelleme saatlerini ve veri toplama ayarlar\u0131n\u0131 inceleyin."
    ),
    "No workspaces yet.": "Henüz çal\u0131şma alan\u0131 yok.",
    "Each account's key and daily request budget are shared by every plant that uses it.": (
        "Her hesab\u0131n anahtar\u0131 ve günlük istek bütçesi, onu kullanan "
        "tüm santrallerce paylaş\u0131l\u0131r."
    ),
    "JSON or CSV, up to 7 MiB.": "JSON veya CSV, en fazla 7 MiB.",
}


@pytest.mark.parametrize(("english", "turkish"), CATALOGUE.items())
def test_the_console_catalogue_translates_the_product_names(english: str, turkish: str) -> None:
    with translation.override("tr"):
        assert gettext(english) == turkish
