from dataclasses import asdict, dataclass
from datetime import date
from enum import StrEnum

from django.contrib.auth.models import User
from django.db import transaction
from django.utils.translation import gettext
from django.views.decorators.debug import sensitive_variables

from pyracast.accounts.authorization import require_platform_admin
from pyracast.connections.models import PowerBasis, Provider, ProviderAccount, SiteBinding
from pyracast.connections.operations import create_account, rotate_secret
from pyracast.errors import ConflictError, ErrorCode
from pyracast.regions.models import Region
from pyracast.regions.operations import configure_binding


@dataclass(frozen=True)
class BindingSpec:
    site_id: str
    capacity_kw: float
    installation_date: date
    settings_verification_reference: str
    capacity_semantics_verified: bool
    dc_capacity_kw: float | None = None
    power_basis: str = PowerBasis.AC
    azimuth_degrees: float = 0
    losses_percent: float = 0


@dataclass(frozen=True)
class NewAccount:
    external_account_id: str
    api_key: str
    daily_quota: int
    authorization_reference: str


@dataclass(frozen=True)
class ExistingAccount:
    account_id: int
    credential_revision: int
    replacement_key: str = ""


class SetupOutcome(StrEnum):
    SAVED = "saved"
    KEY_REPLACED = "key_replaced"
    UNCHANGED = "unchanged"


@sensitive_variables()
def save_provider_setup(
    actor: User,
    region_id: int,
    *,
    expected_revision: int,
    expected_binding_id: int | None,
    account: NewAccount | ExistingAccount,
    binding: BindingSpec,
) -> SetupOutcome:
    """Account, key and binding change together or not at all, and only on the setup the
    operator saw."""
    require_platform_admin(actor)
    with transaction.atomic():
        region = Region.objects.active().select_for_update().get(pk=region_id)
        if region.revision != expected_revision:
            raise ConflictError(
                ErrorCode.CONFLICT, gettext("The plant changed. Reload this page before saving.")
            )
        current_id = (
            SiteBinding.objects.filter(region=region, region_revision=region.revision)
            .values_list("pk", flat=True)
            .first()
        )
        if current_id != expected_binding_id:
            raise ConflictError(
                ErrorCode.CONFLICT,
                gettext("The provider setup changed. Reload this page before saving."),
            )
        account_id, key_replaced = _account(actor, account)
        saved = configure_binding(
            actor, region.pk, provider_account_id=account_id, **asdict(binding)
        )
    if saved.pk != expected_binding_id:
        return SetupOutcome.SAVED
    return SetupOutcome.KEY_REPLACED if key_replaced else SetupOutcome.UNCHANGED


@sensitive_variables()
def _account(actor: User, account: NewAccount | ExistingAccount) -> tuple[int, bool]:
    if isinstance(account, NewAccount):
        created = create_account(
            actor,
            provider=Provider.SOLCAST,
            external_account_id=account.external_account_id,
            api_key=account.api_key,
            daily_quota=account.daily_quota,
            authorization_reference=account.authorization_reference,
            require_new=True,
        )
        return created.pk, False
    existing = ProviderAccount.objects.select_for_update().get(
        pk=account.account_id, provider=Provider.SOLCAST, enabled=True
    )
    if existing.credential_revision != account.credential_revision:
        raise ConflictError(
            ErrorCode.CONFLICT, gettext("The account changed. Reload this page before saving.")
        )
    if account.replacement_key:
        rotate_secret(actor, existing.pk, account.replacement_key)
    return existing.pk, bool(account.replacement_key)
