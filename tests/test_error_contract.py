import http.client
import json
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from email.message import Message
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.contrib.sessions.exceptions import SessionInterrupted
from django.db import OperationalError
from django.test import Client, override_settings
from django.utils import timezone
from psycopg.errors import DeadlockDetected, LockNotAvailable, QueryCanceled
from rest_framework.exceptions import APIException, Throttled

from pyracast import maintenance
from pyracast.accounts import session_lock
from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.api.session import SessionView
from pyracast.connections.contracts import ProviderError
from pyracast.connections.operations import QuotaExceededError, RequestDeferredError
from tests.support.auth_lifecycle import PASSWORD

pytestmark = pytest.mark.django_db(databases=["default", "auth_cache"])
OPENAPI = Path(__file__).resolve().parents[1] / "web" / "openapi.json"
ALLAUTH = "/api/auth/browser/v1/"

type Trigger = Callable[[pytest.MonkeyPatch, str], Any]


@dataclass(frozen=True)
class Case:
    status: int
    en: str
    tr: str
    trigger: Trigger
    fields: tuple[str, ...] = ()
    retry_after: int | None = None
    request_id: bool = False
    authenticate: str | None = None


def language(value: str) -> dict[str, str]:
    return {"Accept-Language": value}


def raising(factory: Callable[[], BaseException]) -> Trigger:
    def trigger(monkeypatch: pytest.MonkeyPatch, accept: str) -> Any:
        def fail(*args: object, **kwargs: object) -> None:
            raise factory()

        monkeypatch.setattr(SessionView, "get", fail)
        return Client(raise_request_exception=False).get("/api/session/", headers=language(accept))

    return trigger


def domain(kind: str, code: str, **options: Any) -> Trigger:
    def factory() -> BaseException:
        from pyracast import errors

        error_type: type[BaseException] = getattr(errors, kind)
        return error_type(errors.ErrorCode(code), **options)

    return raising(factory)


def database(cause: type[Exception]) -> Trigger:
    def factory() -> BaseException:
        error = OperationalError("private database detail")
        error.__cause__ = cause("private cause detail")
        return error

    return raising(factory)


def signed_in(role: str | None) -> tuple[Client, Tenant]:
    tenant = Tenant.objects.create(name="Workspace")
    user = User.objects.create_user(username="person", email="person@example.com")
    if role is not None:
        Membership.objects.create(tenant=tenant, user=user, role=role)
    client = Client()
    client.force_login(user)
    return client, tenant


def members_page(role: str | None) -> Trigger:
    def trigger(monkeypatch: pytest.MonkeyPatch, accept: str) -> Any:
        client, tenant = signed_in(role)
        return client.get(f"/api/tenants/{tenant.pk}/members/", headers=language(accept))

    return trigger


def invalid_language(monkeypatch: pytest.MonkeyPatch, accept: str) -> Any:
    return Client().post(
        "/api/session/language/",
        {"language": "xx"},
        content_type="application/json",
        headers=language(accept),
    )


def too_many_fields(monkeypatch: pytest.MonkeyPatch, accept: str) -> Any:
    with override_settings(DATA_UPLOAD_MAX_NUMBER_FIELDS=3):
        return Client(raise_request_exception=False).post(
            "/api/auth/browser/v1/auth/provider/redirect",
            {f"field{index}": "x" for index in range(4)},
            headers=language(accept),
        )


def interrupted_session(monkeypatch: pytest.MonkeyPatch, accept: str) -> Any:
    @contextmanager
    def interrupted(session_key: str) -> Iterator[None]:
        yield
        raise SessionInterrupted("The session serialization lock was lost.")

    monkeypatch.setattr(session_lock, "serialize_session", interrupted)
    client = Client()
    client.cookies["sessionid"] = "a" * 32
    return client.get("/api/session/", headers=language(accept))


def busy(session_key: str) -> Iterator[None]:
    raise session_lock.SessionBusyError


def busy_session(monkeypatch: pytest.MonkeyPatch, accept: str) -> Any:
    monkeypatch.setattr(session_lock, "serialize_session", busy)
    client = Client()
    client.cookies["sessionid"] = "a" * 32
    return client.get("/api/session/", headers=language(accept))


@dataclass(frozen=True)
class NoticeResponse:
    status_code: int
    headers: Message
    content: bytes

    def __getitem__(self, name: str) -> str:
        value = self.headers[name]
        if value is None:
            raise KeyError(name)
        return value

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self.headers


def maintenance_notice(monkeypatch: pytest.MonkeyPatch, accept: str) -> Any:
    server = maintenance.server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        connection.request("GET", "/api", headers={"Accept": "*/*", **language(accept)})
        answer = connection.getresponse()
        return NoticeResponse(answer.status, answer.headers, answer.read())
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def anonymous_tenants(monkeypatch: pytest.MonkeyPatch, accept: str) -> Any:
    return Client().get("/api/tenants/", headers=language(accept))


def missing_csrf_token(monkeypatch: pytest.MonkeyPatch, accept: str) -> Any:
    return Client(enforce_csrf_checks=True).post(
        "/api/session/language/",
        {"language": "tr"},
        content_type="application/json",
        headers=language(accept),
    )


def privileged_enrollment(monkeypatch: pytest.MonkeyPatch, accept: str) -> Any:
    staff = User.objects.create_user(username="operator", is_staff=True)
    client = Client()
    client.force_login(staff)
    return client.get("/api/auth/browser/v1/account/authenticators/totp", headers=language(accept))


def forged_proxy_headers(monkeypatch: pytest.MonkeyPatch, accept: str) -> Any:
    with override_settings(PYRACAST_TRUST_PROXY=True, PYRACAST_TRUSTED_PROXY_CIDRS=["10.0.0.0/24"]):
        return Client(REMOTE_ADDR="10.0.0.7").get(
            "/api/session/",
            HTTP_X_FORWARDED_PROTO="https",
            HTTP_X_REAL_IP="not-an-address",
            headers=language(accept),
        )


def unknown_path(monkeypatch: pytest.MonkeyPatch, accept: str) -> Any:
    return Client().get("/api/does-not-exist/", headers=language(accept))


def unsupported_method(monkeypatch: pytest.MonkeyPatch, accept: str) -> Any:
    return Client().put("/api/session/", headers=language(accept))


def unsupported_format(monkeypatch: pytest.MonkeyPatch, accept: str) -> Any:
    return Client().get("/api/session/", headers={"Accept": "application/xml", **language(accept)})


def form_encoded(monkeypatch: pytest.MonkeyPatch, accept: str) -> Any:
    return Client().post("/api/session/language/", {"language": "tr"}, headers=language(accept))


def status_only(status: int) -> Callable[[], BaseException]:
    def factory() -> BaseException:
        error = APIException()
        error.status_code = status
        return error

    return factory


def deferred_provider() -> BaseException:
    return RequestDeferredError(timezone.now() + timedelta(seconds=30, milliseconds=500))


CASES: dict[str, Case] = {
    "invalid_request": Case(
        400,
        "Check the highlighted values and try again.",
        "İşaretli değerleri kontrol edip tekrar deneyin.",
        invalid_language,
        fields=("language",),
    ),
    "bad_request": Case(
        400, "The request could not be processed.", "İstek işlenemedi.", too_many_fields
    ),
    "session_ended": Case(
        400,
        "Your session ended. Sign in again.",
        "Oturumunuz sona erdi. Yeniden oturum aç\u0131n.",
        interrupted_session,
    ),
    "not_authenticated": Case(
        401,
        "Sign in to continue.",
        "Devam etmek için oturum aç\u0131n.",
        anonymous_tenants,
        authenticate="Session",
    ),
    "csrf_failed": Case(
        403,
        "The page expired. Reload it and try again.",
        "Sayfan\u0131n süresi doldu. Sayfay\u0131 yenileyip tekrar deneyin.",
        missing_csrf_token,
    ),
    "permission_denied": Case(
        403,
        "You do not have permission for this action.",
        "Bu işlem için yetkiniz yok.",
        privileged_enrollment,
    ),
    "invalid_proxy_headers": Case(
        403, "The request could not be verified.", "İstek doğrulanamad\u0131.", forged_proxy_headers
    ),
    "membership_required": Case(
        403,
        "You are not a member of this workspace.",
        "Bu çal\u0131şma alan\u0131n\u0131n üyesi değilsiniz.",
        members_page(None),
    ),
    "workspace_admin_required": Case(
        403,
        "Workspace administrator required.",
        "Çal\u0131şma alan\u0131 yöneticisi yetkisi gereklidir.",
        members_page(WorkspaceRole.VIEWER),
    ),
    "admin_edits_directly": Case(
        403,
        "Workspace members submit requests; administrators edit directly.",
        "Çal\u0131şma alan\u0131 üyeleri talep gönderir; yöneticiler doğrudan düzenleme yapar.",
        domain("ForbiddenError", "admin_edits_directly"),
    ),
    "platform_admin_required": Case(
        403,
        "Platform administrator required.",
        "Platform yöneticisi yetkisi gereklidir.",
        domain("ForbiddenError", "platform_admin_required"),
    ),
    "invitation_email_unverified": Case(
        403,
        "Verify the invited email address before accepting.",
        "Daveti kabul etmeden önce davet edilen e-posta adresini doğrulay\u0131n.",
        domain("ForbiddenError", "invitation_email_unverified"),
    ),
    "not_found": Case(
        404, "The requested item was not found.", "İstenen öğe bulunamad\u0131.", unknown_path
    ),
    "method_not_allowed": Case(
        405,
        "This action is not available here.",
        "Bu işlem burada kullan\u0131lamaz.",
        unsupported_method,
    ),
    "not_acceptable": Case(
        406,
        "The requested response format is not available.",
        "İstenen yan\u0131t biçimi kullan\u0131lam\u0131yor.",
        unsupported_format,
    ),
    "unsupported_media_type": Case(
        415, "Send the request as JSON.", "İsteği JSON olarak gönderin.", form_encoded
    ),
    "conflict": Case(
        409,
        "The request conflicts with the current state. Reload and try again.",
        "İstek mevcut durumla çak\u0131ş\u0131yor. Sayfay\u0131 yenileyip tekrar deneyin.",
        raising(status_only(409)),
    ),
    "throttled": Case(
        429,
        "Too many requests. Wait a moment and try again.",
        "Çok fazla istek gönderildi. Biraz bekleyip tekrar deneyin.",
        raising(lambda: Throttled(wait=7)),
        retry_after=7,
    ),
    "session_busy": Case(
        429,
        "Another request from this session is still running. Try again.",
        "Bu oturumdan gönderilen başka bir istek hâlâ sürüyor. Tekrar deneyin.",
        busy_session,
        retry_after=1,
    ),
    "invitation_rate_limited": Case(
        429,
        "Too many invitations were sent. Wait a minute and try again.",
        "Çok fazla davet gönderildi. Bir dakika bekleyip tekrar deneyin.",
        domain("DomainError", "invitation_rate_limited", status=429, retry_after=60),
        retry_after=60,
    ),
    "provider_throttled": Case(
        429,
        "The forecast provider is limiting requests. Try again later.",
        "Tahmin sağlay\u0131c\u0131s\u0131 istekleri s\u0131n\u0131rl\u0131yor. Daha sonra "
        "tekrar deneyin.",
        raising(deferred_provider),
        retry_after=31,
    ),
    "quota_exhausted": Case(
        429,
        "The forecast provider's daily quota is used up. Try again tomorrow.",
        "Tahmin sağlay\u0131c\u0131s\u0131n\u0131n günlük kotas\u0131 doldu. Yar\u0131n tekrar "
        "deneyin.",
        raising(QuotaExceededError),
    ),
    "server_error": Case(
        500,
        "The server could not complete this request.",
        "Sunucu bu isteği tamamlayamad\u0131.",
        raising(lambda: RuntimeError("private failure detail")),
        request_id=True,
    ),
    "provider_unavailable": Case(
        503,
        "The forecast provider is unavailable. Try again later.",
        "Tahmin sağlay\u0131c\u0131s\u0131na ulaş\u0131lam\u0131yor. Daha sonra tekrar deneyin.",
        raising(lambda: ProviderError("timeout")),
    ),
    "busy": Case(
        503,
        "The workspace is busy. Try again in a moment.",
        "Çal\u0131şma alan\u0131 meşgul. Biraz sonra tekrar deneyin.",
        database(LockNotAvailable),
        retry_after=1,
    ),
    "database_conflict": Case(
        503,
        "The change collided with another change. Try again.",
        "Değişiklik başka bir değişiklikle çak\u0131şt\u0131. Tekrar deneyin.",
        database(DeadlockDetected),
        retry_after=1,
    ),
    "request_timeout": Case(
        503,
        "The request took too long. Try again later.",
        "İstek çok uzun sürdü. Daha sonra tekrar deneyin.",
        database(QueryCanceled),
        request_id=True,
    ),
    "provider_authentication_failed": Case(
        503,
        "The forecast provider rejected the stored credentials. Contact your administrator.",
        "Tahmin sağlay\u0131c\u0131s\u0131 kay\u0131tl\u0131 kimlik bilgilerini reddetti. "
        "Yöneticinize başvurun.",
        raising(lambda: RequestDeferredError(None)),
    ),
    "maintenance": Case(
        503,
        "Pyracast is being updated. Try again in a minute; your data is safe.",
        "Pyracast güncelleniyor. Bir dakika sonra yeniden deneyin; verileriniz güvende.",
        maintenance_notice,
        retry_after=60,
    ),
    "forecast_sources_changed": Case(
        503,
        "The forecast sources changed while loading. Try again.",
        "Tahmin kaynaklar\u0131 yüklenirken değişti. Tekrar deneyin.",
        domain("BusyError", "forecast_sources_changed"),
        retry_after=1,
    ),
    "configuration_unavailable": Case(
        400,
        "Plant configuration is unavailable. Complete setup and wait for administrator approval.",
        "Santral yap\u0131land\u0131rmas\u0131 kullan\u0131lam\u0131yor. Kurulumu "
        "tamamlay\u0131p yönetici onay\u0131n\u0131 bekleyin.",
        domain("DomainError", "configuration_unavailable"),
    ),
    "location_already_has_plant": Case(
        400,
        "This location already has an active plant. Open the existing plant.",
        "Bu konumda zaten etkin bir santral var. Mevcut santrali aç\u0131n.",
        domain("DomainError", "location_already_has_plant"),
    ),
    "plant_submission_required": Case(
        400,
        "Complete the plant's technical details before requesting approval.",
        "Onay istemeden önce santralin teknik bilgilerini tamamlay\u0131n.",
        domain("DomainError", "plant_submission_required"),
    ),
    "estimated_actuals_unavailable": Case(
        400,
        "Matching historical estimates are unavailable. Choose a day with collected data.",
        "Eşleşen geçmiş tahminleri yok. Veri toplanm\u0131ş bir gün seçin.",
        domain("DomainError", "estimated_actuals_unavailable"),
    ),
    "location_unchanged": Case(
        400,
        "Enter a location different from the current location.",
        "Mevcut konumdan farkl\u0131 bir konum girin.",
        domain("DomainError", "location_unchanged"),
    ),
    "location_already_exists": Case(
        400,
        "This location is already in your company's portfolio. Open the existing plant.",
        "Bu konum şirketinizin portföyünde zaten var. Mevcut santrali aç\u0131n.",
        domain("DomainError", "location_already_exists"),
    ),
    "calculation_out_of_range": Case(
        400,
        "This forecast could not be calculated. Choose another day or contact your administrator.",
        "Bu tahmin hesaplanamad\u0131. Başka bir gün seçin veya yöneticinizle iletişime geçin.",
        domain("DomainError", "calculation_out_of_range"),
    ),
    "trash_retention_expired": Case(
        400,
        "The 30-day restore period has ended.",
        "30 günlük geri yükleme süresi doldu.",
        domain("DomainError", "trash_retention_expired"),
    ),
    "date_out_of_range": Case(
        400,
        "Choose a date within the available forecast range.",
        "Kullan\u0131labilir tahmin aral\u0131ğ\u0131nda bir tarih seçin.",
        domain("DomainError", "date_out_of_range"),
    ),
    "completed_day_required": Case(
        400,
        "Choose a completed local day for historical estimates.",
        "Geçmiş tahminleri için tamamlanm\u0131ş bir yerel gün seçin.",
        domain("DomainError", "completed_day_required"),
    ),
    "invalid_epias_plant": Case(
        400,
        "Enter a positive EPİAŞ plant ID and the plant's EIC.",
        "Pozitif bir EPİAŞ santral kimliği ve santralin EIC kodunu girin.",
        domain("DomainError", "invalid_epias_plant"),
    ),
    "epias_plant_identity_mismatch": Case(
        400,
        "The EPİAŞ plant ID and EIC did not match. Check both identifiers.",
        "EPİAŞ santral kimliği ile EIC eşleşmedi. Her iki kimliği denetleyin.",
        domain("DomainError", "epias_plant_identity_mismatch"),
    ),
    "incident_closed": Case(
        400,
        "This outage is already closed.",
        "Bu kesinti zaten kapat\u0131lm\u0131ş.",
        domain("DomainError", "incident_closed"),
    ),
    "incident_overlap": Case(
        400,
        "Outage periods must not overlap.",
        "Kesinti dönemleri birbiriyle çak\u0131şmamal\u0131d\u0131r.",
        domain("DomainError", "incident_overlap", field="starts_at"),
        fields=("starts_at",),
    ),
    "incident_starts_in_future": Case(
        400,
        "The outage start must not be in the future.",
        "Kesinti başlang\u0131c\u0131 gelecekte olamaz.",
        domain("DomainError", "incident_starts_in_future", field="starts_at"),
        fields=("starts_at",),
    ),
    "incident_ends_before_start": Case(
        400,
        "The outage end must be after its start.",
        "Kesinti bitişi başlang\u0131c\u0131ndan sonra olmal\u0131d\u0131r.",
        domain("DomainError", "incident_ends_before_start", field="ends_at"),
        fields=("ends_at",),
    ),
    "incident_ends_in_future": Case(
        400,
        "The outage end must not be in the future.",
        "Kesinti bitişi gelecekte olamaz.",
        domain("DomainError", "incident_ends_in_future", field="ends_at"),
        fields=("ends_at",),
    ),
    "incident_capacity_exceeded": Case(
        400,
        "Available capacity must not exceed the configured capacity.",
        "Kullan\u0131labilir kapasite yap\u0131land\u0131r\u0131lm\u0131ş kapasiteyi "
        "aşmamal\u0131d\u0131r.",
        domain("DomainError", "incident_capacity_exceeded", field="available_dc_mw"),
        fields=("available_dc_mw",),
    ),
    "invitation_unavailable": Case(
        400,
        "The invitation is invalid or expired.",
        "Davet geçersiz veya süresi dolmuş.",
        domain("DomainError", "invitation_unavailable"),
    ),
    "invitation_member_exists": Case(
        400,
        "This account already belongs to the workspace.",
        "Bu hesap zaten çal\u0131şma alan\u0131n\u0131n üyesi.",
        domain("DomainError", "invitation_member_exists"),
    ),
    "workspace_admin_not_removable": Case(
        400,
        "The workspace administrator cannot be removed.",
        "Çal\u0131şma alan\u0131 yöneticisi ç\u0131kar\u0131lamaz.",
        domain("DomainError", "workspace_admin_not_removable"),
    ),
    "member_unavailable": Case(
        400,
        "Choose an active member of this workspace.",
        "Bu çal\u0131şma alan\u0131n\u0131n etkin bir üyesini seçin.",
        domain("DomainError", "member_unavailable"),
    ),
    "region_revision_changed": Case(
        409,
        "The plant location changed. Reopen the plant and try again.",
        "Santral konumu değişti. Santrali yeniden aç\u0131p tekrar deneyin.",
        domain("ConflictError", "region_revision_changed"),
    ),
    "location_change_already_pending": Case(
        409,
        "Another location request is pending. Reopen the editor to see it.",
        "Bekleyen başka bir konum talebi var. Görmek için düzenleyiciyi yeniden aç\u0131n.",
        domain("ConflictError", "location_change_already_pending"),
    ),
    "location_change_already_reviewed": Case(
        409,
        "This location request has already been reviewed. Refresh the page.",
        "Bu konum talebi zaten değerlendirilmiş. Sayfay\u0131 yenileyin.",
        domain("ConflictError", "location_change_already_reviewed"),
    ),
    "incident_revision_changed": Case(
        409,
        "The outage changed. Reload the list and try again.",
        "Kesinti kayd\u0131 değişti. Listeyi yeniden yükleyip tekrar deneyin.",
        domain("ConflictError", "incident_revision_changed"),
    ),
    "idempotency_key_reused": Case(
        409,
        "Use a new request key for changed input.",
        "Değiştirilen bilgiler için yeni bir talep anahtar\u0131 kullan\u0131n.",
        domain("ConflictError", "idempotency_key_reused"),
    ),
    "pending_request_limit": Case(
        409,
        "Resolve or withdraw pending requests before submitting more.",
        "Yeni talep göndermeden önce bekleyen talepleri sonuçland\u0131r\u0131n veya geri çekin.",
        domain("ConflictError", "pending_request_limit"),
    ),
    "production_revision_changed": Case(
        409,
        "Production changed. Reload the latest values before saving.",
        "Üretim verileri değişti. Kaydetmeden önce güncel değerleri yeniden yükleyin.",
        domain("ConflictError", "production_revision_changed"),
    ),
    "request_already_closed": Case(
        409,
        "The request already has a different outcome.",
        "Talep zaten farkl\u0131 bir sonuçla kapat\u0131ld\u0131.",
        domain("ConflictError", "request_already_closed"),
    ),
    "request_conflict": Case(
        409,
        "This request no longer matches the plant. Review it again.",
        "Bu talep art\u0131k santralle uyuşmuyor. Yeniden inceleyin.",
        domain("ConflictError", "request_conflict"),
    ),
    "review_context_changed": Case(
        409,
        "The review context changed. Refresh and review again.",
        "İnceleme verileri değişti. Sayfay\u0131 yenileyip yeniden inceleyin.",
        domain("ConflictError", "review_context_changed"),
    ),
    "workspace_admin_changed": Case(
        409,
        "The workspace administrator changed. Reload before saving.",
        "Çal\u0131şma alan\u0131 yöneticisi değişti. Kaydetmeden önce sayfay\u0131 yenileyin.",
        domain("ConflictError", "workspace_admin_changed"),
    ),
}


def test_the_cases_cover_the_closed_error_vocabulary() -> None:
    from pyracast.errors import ErrorCode

    assert {code.value for code in ErrorCode} == set(CASES)


def test_every_code_has_turkish_text_distinct_from_english() -> None:
    assert [code for code, case in CASES.items() if case.tr == case.en or not case.tr] == []


@pytest.mark.parametrize("accept", ["en", "tr"])
@pytest.mark.parametrize("code", sorted(CASES))
def test_every_error_reaches_the_client_as_one_localized_body(
    code: str, accept: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = CASES[code]
    response = case.trigger(monkeypatch, accept)
    assert response.status_code == case.status
    assert response["Content-Type"] == "application/json"
    assert "no-store" in response["Cache-Control"]
    body = json.loads(response.content)
    expected: dict[str, object] = {"code": code, "message": case.en if accept == "en" else case.tr}
    if case.fields:
        assert set(body["fields"]) == set(case.fields)
        assert all(isinstance(text, str) and text for text in body["fields"].values())
        expected["fields"] = body["fields"]
    if case.retry_after is not None:
        expected["retry_after"] = case.retry_after
        assert response["Retry-After"] == str(case.retry_after)
    else:
        assert "Retry-After" not in response
    if case.request_id:
        assert re.fullmatch(r"[0-9a-f]{32}", body["request_id"])
        expected["request_id"] = body["request_id"]
    if case.authenticate is not None:
        assert response["WWW-Authenticate"] == case.authenticate
    assert body == expected


def test_domain_error_fields_carry_the_localized_message(monkeypatch: pytest.MonkeyPatch) -> None:
    response = domain("DomainError", "incident_overlap", field="starts_at")(monkeypatch, "tr")
    assert json.loads(response.content)["fields"] == {
        "starts_at": "Kesinti dönemleri birbiriyle çak\u0131şmamal\u0131d\u0131r."
    }


def test_serializer_field_and_form_errors_keep_their_places() -> None:
    client, tenant = signed_in(WorkspaceRole.ADMIN)
    path = f"/api/tenants/{tenant.pk}/invitations/"
    invalid = client.post(path, {"email": "not-an-address"}, content_type="application/json")
    assert invalid.status_code == 400
    assert invalid.json() == {
        "code": "invalid_request",
        "message": "Check the highlighted values and try again.",
        "fields": {"email": "Enter a valid email address."},
    }
    unknown = client.post(
        path, {"email": "new@example.com", "unexpected": True}, content_type="application/json"
    )
    assert unknown.status_code == 400
    assert unknown.json() == {
        "code": "invalid_request",
        "message": "Unknown fields are not accepted.",
    }


def test_nested_serializer_errors_name_the_item_path() -> None:
    from rest_framework import serializers

    from pyracast.api.errors import api_error

    class Item(serializers.Serializer[dict[str, object]]):
        value = serializers.IntegerField()

    class Payload(serializers.Serializer[dict[str, object]]):
        items = Item(many=True)

    payload = Payload(data={"items": [{"value": 1}, {"value": "x"}]})
    assert not payload.is_valid()
    error = api_error(serializers.ValidationError(payload.errors), None, "-")
    assert error.code == "invalid_request"
    assert error.fields == {"items.1.value": "A valid integer is required."}


def test_drf_field_errors_keep_whole_sentences_in_every_form() -> None:
    from rest_framework import serializers

    from pyracast.api.errors import api_error

    error = api_error(
        serializers.ValidationError(
            {
                "email": "Enter a valid email address.",
                "name": ["Too long.", "Not unique."],
                "site": {"timezone": "Unknown zone."},
            }
        ),
        None,
        "-",
    )
    assert error.fields == {
        "email": "Enter a valid email address.",
        "name": "Too long. Not unique.",
        "site.timezone": "Unknown zone.",
    }
    sentence = api_error(serializers.ValidationError("Choose a plant."), None, "-")
    assert (sentence.message, sentence.fields) == ("Choose a plant.", None)


@pytest.mark.parametrize("lazy", [False, True])
def test_django_validation_errors_keep_their_field_keys_and_translated_text(lazy: bool) -> None:
    from django.core.exceptions import ValidationError
    from django.utils import translation
    from django.utils.translation import gettext_lazy

    from pyracast.api.errors import api_error

    text = "An IANA timezone is required."
    required = gettext_lazy(text) if lazy else text
    with translation.override("tr"):
        error = api_error(ValidationError({"timezone": [required]}), None, "-")
        assert error.body() == {
            "code": "invalid_request",
            "message": "İşaretli değerleri kontrol edip tekrar deneyin.",
            "fields": {"timezone": "Geçerli bir IANA saat dilimi girin."},
        }
        sentence = api_error(ValidationError(required), None, "-")
        assert sentence.body() == {
            "code": "invalid_request",
            "message": "Geçerli bir IANA saat dilimi girin.",
        }


def test_drf_session_csrf_refusal_is_coded() -> None:
    tenant = Tenant.objects.create(name="Workspace")
    user = User.objects.create_user(username="person", email="person@example.com")
    Membership.objects.create(tenant=tenant, user=user, role=WorkspaceRole.ADMIN)
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    response = client.post(
        f"/api/tenants/{tenant.pk}/invitations/",
        {"email": "new@example.com"},
        content_type="application/json",
        headers={"Accept-Language": "tr"},
    )
    assert response.status_code == 403
    assert response.json() == {
        "code": "csrf_failed",
        "message": "Sayfan\u0131n süresi doldu. Sayfay\u0131 yenileyip tekrar deneyin.",
    }


def test_csrf_refusal_outside_the_api_keeps_the_html_page() -> None:
    response = Client(enforce_csrf_checks=True).post("/admin/login/", {"username": "x"})
    assert response.status_code == 403
    assert response["Content-Type"].startswith("text/html")


NAVIGATION_ROUTES = (
    f"{ALLAUTH}auth/provider/redirect",
    "/api/accounts/google/login/callback/",
    "/api/accounts/microsoft/login/callback/",
)


@pytest.mark.parametrize("path", NAVIGATION_ROUTES)
def test_csrf_refusal_on_a_provider_navigation_returns_to_the_sign_in_callback(path: str) -> None:
    client = Client(enforce_csrf_checks=True)
    data = {"provider": "google", "process": "login"}
    navigation = client.post(path, data, HTTP_SEC_FETCH_MODE="navigate")
    assert navigation.status_code == 302
    assert navigation["Location"] == "https://testserver/app/?social_callback=1&error=expired_form"
    fetched = client.post(path, data, HTTP_SEC_FETCH_MODE="cors", headers=language("tr"))
    assert fetched.status_code == 403
    assert fetched.json() == {"code": "csrf_failed", "message": CASES["csrf_failed"].tr}


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
@pytest.mark.parametrize("mode", ["navigate", "cors"])
def test_csrf_refusal_on_every_other_api_route_is_the_coded_body(mode: str) -> None:
    client = Client(enforce_csrf_checks=True)
    client.force_login(User.objects.create_user(username="person"))
    refusals = {}
    for path in sorted(set(api_routes()) - set(NAVIGATION_ROUTES)):
        response = client.post(
            path,
            {},
            content_type="application/json",
            headers={"Accept-Language": "tr", "Sec-Fetch-Mode": mode},
        )
        json_body = response["Content-Type"] == "application/json"
        refusals[path] = (response.status_code, response.json() if json_body else None)
    assert len(refusals) > 30
    expected = (403, {"code": "csrf_failed", "message": CASES["csrf_failed"].tr})
    assert {path: refusal for path, refusal in refusals.items() if refusal != expected} == {}


def test_busy_session_navigation_reloads_once_then_explains() -> None:
    client = Client()
    client.cookies["sessionid"] = "a" * 32
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(session_lock, "serialize_session", busy)
        first = client.get("/admin/", HTTP_SEC_FETCH_MODE="navigate")
        second = client.get("/admin/?session_retry=1", HTTP_SEC_FETCH_MODE="navigate")
        filtered = client.get("/admin/?q=solar", HTTP_SEC_FETCH_MODE="navigate")
        reload = client.post("/admin/", HTTP_SEC_FETCH_MODE="navigate")
    assert first.status_code == 503
    assert first["Content-Type"].startswith("text/html")
    assert "no-store" in first["Cache-Control"]
    assert first["Refresh"] == "1; url=/admin/?session_retry=1"
    assert second.status_code == 503
    assert "Refresh" not in second
    assert second["Content-Type"].startswith("text/html")
    assert filtered["Refresh"] == "1; url=/admin/?q=solar&session_retry=1"
    assert reload.status_code == 429
    assert reload.json()["code"] == "session_busy"


def test_busy_session_api_navigation_keeps_the_json_body() -> None:
    client = Client()
    client.cookies["sessionid"] = "a" * 32
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(session_lock, "serialize_session", busy)
        response = client.get("/api/session/", HTTP_SEC_FETCH_MODE="navigate")
    assert response.status_code == 429
    assert response["Content-Type"] == "application/json"
    assert response["Retry-After"] == "1"
    assert response.json()["code"] == "session_busy"


def api_routes(patterns: list[Any] | None = None, prefix: str = "") -> Iterator[str]:
    from django.urls import URLResolver, get_resolver

    for entry in get_resolver().url_patterns if patterns is None else patterns:
        route = prefix + re.sub(r"<(?:\w+:)?\w+>", "1", str(entry.pattern))
        if isinstance(entry, URLResolver):
            yield from api_routes(entry.url_patterns, route)
        elif route.startswith("api/"):
            yield f"/{route}"


MALFORMED_BODIES = (b"{", b"[]", b"null", b'"text"', b"\xff")


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
@pytest.mark.parametrize("accept", ["en", "tr"])
@pytest.mark.parametrize("content", MALFORMED_BODIES)
@pytest.mark.parametrize("path", [f"{ALLAUTH}auth/login", "/api/session/language/"])
def test_malformed_or_non_object_json_is_a_localized_refusal(
    path: str, content: bytes, accept: str
) -> None:
    response = Client().generic(
        "POST", path, content, content_type="application/json", headers=language(accept)
    )
    assert response.status_code == 400
    assert response["Content-Type"] == "application/json"
    body = response.json()
    assert body["code"] in {"bad_request", "invalid_request"}
    english = Client().generic(
        "POST", path, content, content_type="application/json", headers=language("en")
    )
    if accept == "tr":
        assert body["message"] != english.json()["message"]
    if path.startswith(ALLAUTH):
        assert body == {"code": "bad_request", "message": getattr(CASES["bad_request"], accept)}


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
@pytest.mark.parametrize(
    ("method", "path", "content", "status"),
    [
        ("GET", f"{ALLAUTH}auth/email/verify", b"", 405),
        ("POST", f"{ALLAUTH}account/email", b"{", 400),
    ],
)
def test_replaced_framework_refusals_keep_the_response_metadata(
    method: str, path: str, content: bytes, status: int
) -> None:
    from pyracast.accounts.language import save_user_language
    from pyracast.languages import LANGUAGE_COOKIE

    user = User.objects.create_user(username="person", email="person@example.com")
    save_user_language(user, "tr")
    client = Client()
    client.force_login(user)
    response = client.generic(method, path, content, content_type="application/json")
    assert response.status_code == status
    assert response["Content-Type"] == "application/json"
    assert response.json()["message"] == CASES[response.json()["code"]].tr
    assert response["Content-Language"] == "tr"
    assert {"Accept-Language", "Cookie"} <= {value.strip() for value in response["Vary"].split(",")}
    assert response.cookies[LANGUAGE_COOKIE].value == "tr"


def carries_the_contract(body: Any) -> bool:
    if not isinstance(body, dict) or body.get("code") not in CASES:
        return False
    message = body.get("message")
    return isinstance(message, str) and bool(message) and message != CASES[body["code"]].en


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
@pytest.mark.parametrize("signed_in", [False, True])
def test_every_api_route_answers_every_error_with_the_contract_body(signed_in: bool) -> None:
    client = Client(raise_request_exception=False)
    if signed_in:
        client.force_login(User.objects.create_user(username="person"))
    failures = {}
    requests = 0
    for path in sorted(api_routes()):
        for method, content in (
            ("GET", b""),
            ("POST", b"{}"),
            ("PROPFIND", b""),
            *(("POST", body) for body in MALFORMED_BODIES),
        ):
            response = client.generic(
                method,
                path,
                content,
                content_type="application/json",
                headers={"Accept-Language": "tr"},
            )
            requests += 1
            if response.status_code < 400:
                continue
            json_body = response["Content-Type"] == "application/json"
            body = response.json() if json_body else None
            if not carries_the_contract(body):
                failures[f"{method} {path} {content!r}"] = (response.status_code, body)
    assert requests > 300
    assert failures == {}


def allauth_login(client: Client, accept: str, payload: dict[str, str]) -> Any:
    return client.post(
        f"{ALLAUTH}auth/login", payload, content_type="application/json", headers=language(accept)
    )


def invalid_login_input(accept: str) -> Any:
    return allauth_login(Client(), accept, {})


def wrong_password(accept: str) -> Any:
    User.objects.create_user(username="person", email="person@example.com", password=PASSWORD)
    return allauth_login(
        Client(), accept, {"email": "person@example.com", "password": f"{PASSWORD}-wrong"}
    )


def signed_out_session(accept: str) -> Any:
    return Client().get(f"{ALLAUTH}auth/session", headers=language(accept))


def provider_signup_without_a_pending_login(accept: str) -> Any:
    return Client().post(
        f"{ALLAUTH}auth/provider/signup",
        {},
        content_type="application/json",
        headers=language(accept),
    )


def repeated_login(accept: str) -> Any:
    from django.conf import settings

    client = Client()
    payload = {"email": "nobody@example.com", "password": "secret-1"}
    with override_settings(ACCOUNT_RATE_LIMITS={**settings.ACCOUNT_RATE_LIMITS, "login": "1/m/ip"}):
        allauth_login(client, accept, payload)
        return allauth_login(client, accept, payload)


def missing_authenticator(accept: str) -> Any:
    client = Client()
    client.force_login(User.objects.create_user(username="person", email="person@example.com"))
    return client.get(f"{ALLAUTH}account/authenticators/totp", headers=language(accept))


ALLAUTH_FAILURES: dict[str, tuple[Callable[[str], Any], int, str]] = {
    "invalid_login_input": (invalid_login_input, 400, "invalid_request"),
    "wrong_password": (wrong_password, 400, "invalid_request"),
    "signed_out_session": (signed_out_session, 401, "not_authenticated"),
    "provider_signup_without_a_pending_login": (
        provider_signup_without_a_pending_login,
        409,
        "conflict",
    ),
    "repeated_login": (repeated_login, 429, "throttled"),
    "missing_authenticator": (missing_authenticator, 404, "not_found"),
}


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
@pytest.mark.parametrize("failure", sorted(ALLAUTH_FAILURES))
def test_allauth_failures_carry_the_contract_beside_their_own_keys(failure: str) -> None:
    trigger, status, code = ALLAUTH_FAILURES[failure]
    bodies = {}
    for accept in ("en", "tr"):
        User.objects.all().delete()
        response = trigger(accept)
        assert response.status_code == status
        assert response["Content-Type"] == "application/json"
        body = response.json()
        assert set(body) - {"code", "message", "fields", "retry_after", "request_id"} <= {
            "status",
            "errors",
            "data",
            "meta",
        }
        assert body["status"] == status
        assert body["code"] == code
        errors = body.get("errors", [])
        assert all(set(error) <= {"message", "code", "param"} for error in errors)
        expected_message = errors[0]["message"] if errors else getattr(CASES[code], accept)
        assert body["message"] == expected_message
        fields: dict[str, str] = {}
        for error in errors:
            if "param" in error:
                fields[error["param"]] = " ".join(
                    filter(None, [fields.get(error["param"]), error["message"]])
                )
        assert body.get("fields") == (fields or None)
        if "Retry-After" in response:
            assert body["retry_after"] == int(response["Retry-After"])
        else:
            assert "retry_after" not in body
        bodies[accept] = body
    assert bodies["tr"]["message"] != bodies["en"]["message"]
    assert set(bodies["tr"].get("fields", {}).values()).isdisjoint(
        bodies["en"].get("fields", {}).values()
    )


def test_allauth_flow_data_survives_beside_the_contract() -> None:
    body = signed_out_session("tr").json()
    assert body["meta"] == {"is_authenticated": False}
    assert {"id": "login"} in body["data"]["flows"]
    assert (body["code"], body["message"]) == ("not_authenticated", CASES["not_authenticated"].tr)


@pytest.mark.parametrize(
    "path",
    [
        "/api/auth/browser/v1/auth/email/verify",
        "/api/auth/browser/v1/auth/password/reset",
        "/api/accounts/google/login/callback/",
    ],
)
def test_method_guards_answer_the_localized_contract_body(path: str) -> None:
    method = "POST" if "callback" in path else "GET"
    response = Client().generic(method, path, headers={"Accept-Language": "tr"})
    assert response.status_code == 405
    assert response["Content-Type"] == "application/json"
    assert response["Allow"] == ("GET" if method == "POST" else "POST")
    assert response.json() == {
        "code": "method_not_allowed",
        "message": CASES["method_not_allowed"].tr,
    }


def test_drf_permission_classes_answer_coded_refusals() -> None:
    client, tenant = signed_in(WorkspaceRole.VIEWER)
    viewer = client.post(
        f"/api/tenants/{tenant.pk}/invitations/",
        {"email": "new@example.com"},
        content_type="application/json",
    )
    assert viewer.status_code == 403
    assert viewer.json()["code"] == "workspace_admin_required"
    outsider = Client()
    outsider.force_login(User.objects.create_user(username="outsider"))
    refused = outsider.post(
        f"/api/tenants/{tenant.pk}/regions/1/plants/1/change-requests/production/",
        {},
        content_type="application/json",
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "membership_required"


def test_malformed_json_is_a_bad_request() -> None:
    response = Client().post("/api/session/language/", "{", content_type="application/json")
    assert response.status_code == 400
    assert response.json() == {
        "code": "bad_request",
        "message": "The request could not be processed.",
    }


def test_framework_errors_without_a_mapping_are_logged_server_errors(
    pyracast_logs: pytest.LogCaptureFixture,
) -> None:
    from pyracast.api.errors import api_error

    error = api_error(APIException(), None, "-")
    assert (error.status, error.code) == (500, "server_error")
    assert [record.levelname for record in pyracast_logs.records] == ["ERROR"]
    unhinted = api_error(Throttled(), None, "-")
    assert (unhinted.status, unhinted.retry_after) == (429, None)


def test_django_form_errors_become_the_message() -> None:
    from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
    from django.utils.translation import gettext_lazy

    from pyracast.api.errors import api_error

    error = api_error(
        ValidationError(
            {
                NON_FIELD_ERRORS: [gettext_lazy("Unsupported outage kind.")],
                "title": [gettext_lazy("A title of at most 200 characters is required.")],
            }
        ),
        None,
        "-",
    )
    assert error.body() == {
        "code": "invalid_request",
        "message": "Unsupported outage kind.",
        "fields": {"title": "A title of at most 200 characters is required."},
    }


def test_error_vocabulary_and_body_are_exported_to_openapi() -> None:
    from pyracast.errors import ErrorCode

    schema = json.loads(OPENAPI.read_text(encoding="utf-8"))
    components = schema["components"]["schemas"]
    assert components["ErrorCode"]["enum"] == [code.value for code in ErrorCode]
    error = components["Error"]
    assert error["required"] == ["code", "message"]
    assert error["properties"]["code"] == {"$ref": "#/components/schemas/ErrorCode"}
    assert set(error["properties"]) == {"code", "message", "fields", "retry_after", "request_id"}
