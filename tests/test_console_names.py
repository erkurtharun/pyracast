import pytest
from django.test import Client
from django.urls import reverse
from django.utils import translation
from django.utils.functional import Promise
from django.utils.translation import gettext

from pyracast.accounts.invitations import invite_workspace_member
from pyracast.accounts.models import (
    Membership,
    OutboundMailKind,
    OutboundMailStatus,
    Tenant,
    WorkspaceAdminChange,
    WorkspaceRole,
)
from pyracast.notifications.models import (
    NotificationDeliveryError,
    NotificationDeliveryStatus,
    NotificationSettings,
)
from pyracast.plants.models import MeasurementBasis, PlantEventAction
from pyracast.plants.parameters import PlantParameters
from pyracast.plants.sites import submit_site
from pyracast.platform_admin.site import site
from pyracast.regions.models import AuditAction, LocationChangeStatus, Region, RegionStatus
from pyracast.regions.operations import request_location_change
from tests.support.admin_setup import admin_system, setup_url, submitted_plant
from tests.support.api import verified_admin
from tests.support.domain import make_system
from tests.support.plant_operations import make_plant
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
                '<h2 id="details-title">Plant</h2>',
                ">Edit plant</a>",
                '<h3>Technical details <span class="badge">In review</span></h3>',
                '<span class="step-name">Plant sent for approval</span>',
                "<h3>Forecast email history</h3>",
            ),
            (
                "Company",
                "DC nameplate",
                "Measurement point",
                "Email delivery",
                "Plant details",
                "Edit details",
            ),
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
                '<h2 id="details-title">Santral</h2>',
                ">Santrali düzenle</a>",
                '<h3>Teknik bilgiler <span class="badge">İncelemede</span></h3>',
                '<span class="step-name">Santral onaya gönderildi</span>',
                "<h3>Tahmin e-postas\u0131 geçmişi</h3>",
            ),
            (
                "Şirket",
                "DC kurulu güç",
                "E-posta teslimi",
                "Santral bilgileri",
                "Bilgileri düzenle",
            ),
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


LIMIT_HELP = {
    "en": (
        "Lowering the limit pauses the forecast email of plants whose saved recipient list is "
        "over it. The lists are kept. Once a list is shortened or the limit is raised, the next "
        "forecast emails are sent at their scheduled times."
    ),
    "tr": (
        "S\u0131n\u0131r düşürülürse kay\u0131tl\u0131 al\u0131c\u0131 listesi "
        "s\u0131n\u0131r\u0131 aşan santrallerin tahmin e-postas\u0131 duraklat\u0131l\u0131r. "
        "Listeler korunur. Liste k\u0131salt\u0131ld\u0131ğ\u0131nda veya s\u0131n\u0131r "
        "art\u0131r\u0131ld\u0131ğ\u0131nda sonraki tahmin e-postalar\u0131 planlanan saatlerde "
        "gönderilir."
    ),
}


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
    assert f'<p class="callout">{LIMIT_HELP[language]}</p>' in html
    assert gone not in html


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (
            "en",
            (
                "<h3>Technical details</h3>",
                "<dt>Technical details revision</dt><dd>1</dd>",
            ),
        ),
        (
            "tr",
            (
                "<h3>Teknik bilgiler</h3>",
                "<dt>Teknik bilgilerin revizyonu</dt><dd>1</dd>",
            ),
        ),
    ],
)
def test_an_approved_plant_review_names_its_technical_details_revision(
    monkeypatch: pytest.MonkeyPatch, language: str, expected: tuple[str, ...]
) -> None:
    system, _plant = make_plant(monkeypatch)
    html = page(setup_url(system), language)
    for text in expected:
        assert text in html


@pytest.mark.parametrize(
    ("language", "warning"),
    [
        (
            "en",
            "The time zone changes from Europe/Istanbul to UTC: plant days, forecast update "
            "times and forecast email send times follow the new time zone.",
        ),
        (
            "tr",
            "Saat dilimi Europe/Istanbul yerine UTC olur: santral günleri, tahmin güncelleme "
            "saatleri ve tahmin e-postas\u0131 gönderim saatleri yeni saat dilimine göre işler.",
        ),
    ],
)
def test_a_location_review_warns_that_the_time_zone_changes(language: str, warning: str) -> None:
    system = make_system()
    request_location_change(
        system.member,
        system.region.pk,
        system.tenant.pk,
        expected_revision=system.region.revision,
        latitude=41,
        longitude=30,
        timezone="UTC",
    )
    html = page(setup_url(system), language)
    assert f'<p class="callout callout--warn">{warning}</p>' in html


@pytest.mark.parametrize(
    ("language", "expected", "gone"),
    [
        ("en", ("<h1>Edit plant</h1>", "<legend>Technical details</legend>"), "Capacity"),
        ("tr", ("<h1>Santrali düzenle</h1>", "<legend>Teknik bilgiler</legend>"), "Kapasite"),
    ],
)
def test_the_plant_edit_page_names_the_plant_and_its_technical_details(
    monkeypatch: pytest.MonkeyPatch, language: str, expected: tuple[str, ...], gone: str
) -> None:
    system, _plant = make_plant(monkeypatch)
    html = page(reverse("admin:workspace_plant_details", args=(system.region.pk,)), language)
    for text in expected:
        assert text in html
    assert html.count("<legend>") == 3
    assert gone not in html


@pytest.mark.parametrize(
    ("language", "labels", "breadcrumb"),
    [
        (
            "en",
            (
                "Technical details revision",
                "Panel capacity (MWp)",
                "Inverter capacity (MW AC)",
                "Grid export limit (MW)",
                "Mounting",
                "Panel tilt (°)",
                "Panel azimuth (°)",
                "Meter point",
            ),
            "Technical details",
        ),
        (
            "tr",
            (
                "Teknik bilgilerin revizyonu",
                "Panel kurulu gücü (MWp)",
                "İnverter gücü (MW AC)",
                "Şebekeye veriş s\u0131n\u0131r\u0131 (MW)",
                "Montaj",
                "Panel eğimi (°)",
                "Panel azimutu (°)",
                "Ölçüm noktas\u0131",
            ),
            "Teknik bilgiler",
        ),
    ],
)
def test_the_record_browser_names_the_technical_details(
    monkeypatch: pytest.MonkeyPatch, language: str, labels: tuple[str, ...], breadcrumb: str
) -> None:
    _system, plant = make_plant(monkeypatch)
    html = page(
        reverse("admin:plants_plantconfiguration_change", args=(plant.current_configuration_id,)),
        language,
    )
    for label in labels:
        assert f"<label>{label}:</label>" in html
    assert f'<a href="/admin/plants/plantconfiguration/">{breadcrumb}</a>' in html


@pytest.mark.parametrize(
    ("language", "columns", "title"),
    [
        (
            "en",
            ("Workspace administrator", "Previous workspace administrator"),
            "Select workspace administrator change to view",
        ),
        (
            "tr",
            (
                "Çal\u0131şma alan\u0131 yöneticisi",
                "Önceki çal\u0131şma alan\u0131 yöneticisi",
            ),
            "Göstermek için çal\u0131şma alan\u0131 yöneticisi değişikliği seçin",
        ),
    ],
)
def test_the_record_browser_names_the_workspace_administrator_changes(
    language: str, columns: tuple[str, ...], title: str
) -> None:
    system = make_system()
    WorkspaceAdminChange.objects.create(
        tenant=system.tenant, actor=system.admin, administrator=system.member
    )
    html = page(reverse("admin:accounts_workspaceadminchange_changelist"), language)
    for column in columns:
        assert f'">{column}</a></div>' in html
    assert f"<h1>{title}</h1>" in html


@pytest.mark.parametrize(
    ("language", "kinds", "statuses"),
    [
        (
            "en",
            (
                "Plant not approved",
                "Workspace administrator role received",
                "Workspace administrator role moved away",
            ),
            ("Preparing", "Will retry", "Not sent", "Not known whether sent"),
        ),
        (
            "tr",
            (
                "Santral onaylanmad\u0131",
                "Çal\u0131şma alan\u0131 yöneticisi rolü al\u0131nd\u0131",
                "Çal\u0131şma alan\u0131 yöneticisi rolü devredildi",
            ),
            (
                "Haz\u0131rlan\u0131yor",
                "Yeniden denenecek",
                "Gönderilmedi",
                "Gönderildiği belli değil",
            ),
        ),
    ],
)
def test_the_mail_records_name_the_plant_review_roles_and_outcomes(
    language: str, kinds: tuple[str, ...], statuses: tuple[str, ...]
) -> None:
    make_system()
    html = page(reverse("admin:notifications_outboundmail_changelist"), language)
    for name in kinds:
        assert f">{name}</a></li>" in html
    for name in statuses:
        assert f">{name}</a></li>" in html
    assert ">Plant rejected<" not in html
    assert ">Administrator role" not in html


@pytest.mark.parametrize(("language", "state"), [("en", "In review"), ("tr", "İncelemede")])
def test_the_record_browser_shows_a_waiting_location_change_in_review(
    language: str, state: str
) -> None:
    system = make_system()
    request_location_change(
        system.member,
        system.region.pk,
        system.tenant.pk,
        expected_revision=system.region.revision,
        latitude=41,
        longitude=30,
        timezone="UTC",
    )
    html = page(reverse("admin:regions_regionlocationchangerequest_changelist"), language)
    assert f'<td class="field-status">{state}</td>' in html


@pytest.mark.parametrize(
    ("language", "name", "label"),
    [
        ("en", "Forecast email settings", "Recipients per plant"),
        ("tr", "Tahmin e-postas\u0131 ayarlar\u0131", "Santral baş\u0131na al\u0131c\u0131"),
    ],
)
def test_the_record_browser_names_the_forecast_email_settings(
    language: str, name: str, label: str
) -> None:
    NotificationSettings.objects.get_or_create(pk=1)
    html = page(reverse("admin:notifications_notificationsettings_change", args=(1,)), language)
    assert f'<li aria-current="page">{name}</li>' in html
    assert f"<h2>{name}</h2>" in html
    assert f"<label>{label}:</label>" in html
    assert f"<div>{LIMIT_HELP[language]}</div>" in html


@pytest.mark.parametrize(
    ("language", "column"),
    [("en", "Requests today (UTC)"), ("tr", "Bugünkü istekler (UTC)")],
)
def test_the_provider_account_list_counts_requests_by_the_utc_day(
    language: str, column: str
) -> None:
    make_system()
    html = page(reverse("admin:workspace_provider_accounts"), language)
    assert f'<th scope="col" class="cell-fit">{column}</th>' in html
    assert f'<td data-label="{column}">' in html


@pytest.mark.parametrize(
    ("language", "invites", "gone"),
    [
        (
            "en",
            "Pyracast invites this address to become the workspace administrator.",
            "Offered while",
        ),
        (
            "tr",
            "Pyracast bu adresi çal\u0131şma alan\u0131 yöneticisi olmaya davet eder.",
            "yokken sunulur",
        ),
    ],
)
def test_the_workspace_administrator_invitation_says_who_is_invited(
    language: str, invites: str, gone: str
) -> None:
    client, _operator = verified_admin()
    tenant = Tenant.objects.create(name="Empty workspace")
    created = page(reverse("admin:workspace_tenant_new"), language, client)
    invitation = page(
        reverse("admin:workspace_invitation_new", args=(tenant.pk,)), language, client
    )
    assert invites in created
    assert 'name="role"' in invitation
    assert gone not in invitation


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
                NotificationDeliveryStatus.RETRY.label: "Will retry",
                NotificationDeliveryError.DEFINITION_TRASHED.label: "Plant in recycle bin",
                LocationChangeStatus.PENDING.label: "In review",
                LocationChangeStatus.WITHDRAWN.label: "Withdrawn",
                OutboundMailKind.PLANT_REJECTED.label: "Plant not approved",
                OutboundMailKind.WORKSPACE_ADMIN_ASSIGNED.label: (
                    "Workspace administrator role received"
                ),
                OutboundMailStatus.RETRY.label: "Will retry",
                OutboundMailStatus.FAILED.label: "Not sent",
                PlantEventAction.CONFIGURATION_CREATED.label: "Technical details revision created",
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
                NotificationDeliveryStatus.RETRY.label: "Yeniden denenecek",
                NotificationDeliveryError.DEFINITION_TRASHED.label: (
                    "Santral geri dönüşüm kutusunda"
                ),
                LocationChangeStatus.PENDING.label: "İncelemede",
                LocationChangeStatus.WITHDRAWN.label: "Geri çekildi",
                OutboundMailKind.PLANT_REJECTED.label: "Santral onaylanmad\u0131",
                OutboundMailKind.WORKSPACE_ADMIN_ASSIGNED.label: (
                    "Çal\u0131şma alan\u0131 yöneticisi rolü al\u0131nd\u0131"
                ),
                OutboundMailStatus.RETRY.label: "Yeniden denenecek",
                OutboundMailStatus.FAILED.label: "Gönderilmedi",
                PlantEventAction.CONFIGURATION_CREATED.label: (
                    "Teknik bilgilerin yeni revizyonu oluşturuldu"
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
    "Plant saved.": "Santral kaydedildi.",
    "Plant saved. The plant is back in review with its corrected location. Set up its provider "
    "connection again.": (
        "Santral kaydedildi. Santral düzeltilen konumuyla yeniden incelemede. Sağlay\u0131c\u0131 "
        "bağlant\u0131s\u0131n\u0131 yeniden kurun."
    ),
    "Show recent and open work": "Yak\u0131n zamandaki ve aç\u0131k işleri göster",
    "This limit pauses the forecast email of the plants listed above. Confirm to save it.": (
        "Bu s\u0131n\u0131r yukar\u0131daki santrallerin tahmin e-postas\u0131n\u0131 "
        "duraklat\u0131r. Kaydetmek için onaylay\u0131n."
    ),
    "Review the recorded error. The person can request an account email again. The workspace "
    "administrator can send an invitation again; while the workspace has none, Pyracast sends "
    "it.": (
        "Kaydedilen hatay\u0131 inceleyin. Hesap e-postas\u0131n\u0131 kişi yeniden isteyebilir. "
        "Daveti çal\u0131şma alan\u0131 yöneticisi yeniden gönderebilir; çal\u0131şma alan\u0131 "
        "yöneticisi yokken daveti Pyracast gönderir."
    ),
    "They may have arrived and are never resent. The person can request an account email "
    "again. The workspace administrator can send an invitation again; while the workspace has "
    "none, Pyracast sends it.": (
        "Ulaşm\u0131ş olabilirler ve yeniden gönderilmezler. Hesap e-postas\u0131n\u0131 kişi "
        "yeniden isteyebilir. Daveti çal\u0131şma alan\u0131 yöneticisi yeniden gönderebilir; "
        "çal\u0131şma alan\u0131 yöneticisi yokken daveti Pyracast gönderir."
    ),
    "Plant time zone": "Santralin saat dilimi",
    "File time zone": "Dosyan\u0131n saat dilimi",
    "File time zone, only for timestamps without an offset": (
        "Dosyan\u0131n saat dilimi (dosyada belirtilmemişse)"
    ),
    "Use these only if the file does not declare its time zone or interval. An existing "
    "timestamp offset is preserved.": (
        "Yaln\u0131zca dosyada eksikse doldurun. Dosyadaki saat farklar\u0131 korunur."
    ),
    "Naive timestamps require an explicit file time zone.": (
        "Dosyada saat fark\u0131 belirtilmemiş. Dosyan\u0131n saat dilimini seçin."
    ),
    "Selected time zone conflicts with the file's time zone.": (
        "Seçilen saat dilimi dosyadaki saat dilimiyle uyuşmuyor."
    ),
    "Naive source timestamps were interpreted using the selected time zone.": (
        "Saat fark\u0131 bulunmayan kay\u0131tlar seçilen saat dilimine göre yorumland\u0131."
    ),
}


@pytest.mark.parametrize(("english", "turkish"), CATALOGUE.items())
def test_the_console_catalogue_translates_the_product_names(english: str, turkish: str) -> None:
    with translation.override("tr"):
        assert gettext(english) == turkish
