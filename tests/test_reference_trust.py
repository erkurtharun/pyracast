from collections.abc import Callable

import pytest
from django.core.exceptions import ValidationError

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.capacity_normalization import normalize_reference_capacity
from pyracast.forecasts.trust import (
    PROBLEM_MESSAGES,
    normalization_problems,
    problem_messages,
    reference_problems,
)
from tests.support.capacity_normalization import scaled_system
from tests.support.historical_reference_import import system_for_import

pytestmark = pytest.mark.django_db(transaction=True)

type Change = Callable[[SiteBinding, SiteBinding], None]


def suspended(source: SiteBinding, target: SiteBinding) -> None:
    target.region.status = "suspended"


def fixture_account(source: SiteBinding, target: SiteBinding) -> None:
    target.provider_account.provider = "fixture"


def unattested(source: SiteBinding, target: SiteBinding) -> None:
    target.provider_account.authorization_reference = ""


def target_unvalidated(source: SiteBinding, target: SiteBinding) -> None:
    target.validated_at = None


def source_unvalidated(source: SiteBinding, target: SiteBinding) -> None:
    source.validated_at = None


def source_unhealthy(source: SiteBinding, target: SiteBinding) -> None:
    source.health = "error"


def capacity_unverified(source: SiteBinding, target: SiteBinding) -> None:
    target.capacity_semantics_verified = False


def settings_unverified(source: SiteBinding, target: SiteBinding) -> None:
    source.settings_verification_reference = ""


def other_site(source: SiteBinding, target: SiteBinding) -> None:
    target.site_id = "another-resource"


def dc_reference(source: SiteBinding, target: SiteBinding) -> None:
    source.power_basis = target.power_basis = "dc"


def equal_capacity(source: SiteBinding, target: SiteBinding) -> None:
    target.capacity_kw = target.dc_capacity_kw = source.capacity_kw


def undeclared_dc(source: SiteBinding, target: SiteBinding) -> None:
    source.dc_capacity_kw = None


def other_dc_ratio(source: SiteBinding, target: SiteBinding) -> None:
    target.dc_capacity_kw = 2000


def nonfinite_capacity(source: SiteBinding, target: SiteBinding) -> None:
    target.capacity_kw = target.dc_capacity_kw = float("inf")


NORMALIZATION_GATES: list[tuple[str, Change]] = [
    ("target_not_current", suspended),
    ("account_not_solcast", fixture_account),
    ("account_unattested", unattested),
    ("target_not_validated", target_unvalidated),
    ("source_not_validated", source_unvalidated),
    ("source_unhealthy", source_unhealthy),
    ("capacity_unverified", capacity_unverified),
    ("settings_unverified", settings_unverified),
    ("field_differs:site_id", other_site),
    ("target_not_ac", dc_reference),
    ("capacity_equal", equal_capacity),
    ("dc_undeclared", undeclared_dc),
    ("dc_ratio_differs", other_dc_ratio),
    ("capacity_invalid", nonfinite_capacity),
]


def loaded(binding: SiteBinding) -> SiteBinding:
    return SiteBinding.objects.select_related("provider_account", "region").get(pk=binding.pk)


def test_an_equivalent_pair_passes_every_normalization_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, target = scaled_system(monkeypatch)

    assert normalization_problems(loaded(system.binding), loaded(target)) == ()


@pytest.mark.parametrize(("code", "change"), NORMALIZATION_GATES)
def test_each_normalization_gate_reports_its_own_problem(
    monkeypatch: pytest.MonkeyPatch, code: str, change: Change
) -> None:
    system, target = scaled_system(monkeypatch)
    source, current = loaded(system.binding), loaded(target)
    current.provider_account = source.provider_account
    current.region = source.region

    change(source, current)

    assert code in normalization_problems(source, current)
    assert code.split(":")[0] in PROBLEM_MESSAGES


def test_normalizing_a_binding_onto_itself_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    system, _ = scaled_system(monkeypatch)
    source = loaded(system.binding)

    assert "same_binding" in normalization_problems(source, source)


def test_the_operation_refuses_with_every_unmet_gate_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, target = scaled_system(monkeypatch)
    SiteBinding.objects.filter(pk=system.binding.pk).update(
        health="error", validated_at=None, validated_credential_revision=None
    )

    with pytest.raises(ValidationError) as refusal:
        normalize_reference_capacity(
            system.admin,
            system.tenant.pk,
            system.region.pk,
            system.binding.pk,
            target.pk,
            "Capacity-only change confirmed",
        )

    assert set(refusal.value.messages) >= {
        str(PROBLEM_MESSAGES["source_unhealthy"]),
        str(PROBLEM_MESSAGES["source_not_validated"]),
    }


IMPORT_GATES: list[tuple[str, Callable[[SiteBinding], None]]] = [
    ("binding_not_current", lambda binding: setattr(binding.region, "status", "suspended")),
    ("account_not_solcast", lambda binding: setattr(binding.provider_account, "provider", "x")),
    (
        "account_unattested",
        lambda binding: setattr(binding.provider_account, "authorized_by_id", None),
    ),
    ("binding_not_validated", lambda binding: setattr(binding, "validated_at", None)),
    ("capacity_unverified", lambda binding: setattr(binding, "capacity_semantics_verified", False)),
    ("power_basis_not_ac", lambda binding: setattr(binding, "power_basis", "dc")),
    (
        "settings_unverified",
        lambda binding: setattr(binding, "settings_verification_reference", ""),
    ),
]


def test_an_authorized_reference_passes_every_import_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)

    assert reference_problems(loaded(system.binding)) == ()


@pytest.mark.parametrize(("code", "change"), IMPORT_GATES)
def test_each_import_gate_reports_its_own_problem(
    monkeypatch: pytest.MonkeyPatch, code: str, change: Callable[[SiteBinding], None]
) -> None:
    system = system_for_import(monkeypatch)
    binding = loaded(system.binding)

    change(binding)

    assert reference_problems(binding) == (code,)
    assert code in PROBLEM_MESSAGES


def test_a_differing_field_is_named_in_its_message() -> None:
    assert problem_messages(("field_differs:site_id", "capacity_equal")) == [
        "The connections differ in more than their capacity: site_id.",
        "The capacities are equal; there is nothing to rescale.",
    ]
