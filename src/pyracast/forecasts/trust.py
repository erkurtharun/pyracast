from math import isclose, isfinite
from typing import TYPE_CHECKING

from django.utils.translation import gettext_lazy

from pyracast.connections.currency import binding_is_current
from pyracast.connections.models import BindingHealth, PowerBasis, Provider, SiteBinding

if TYPE_CHECKING:
    from django_stubs_ext import StrPromise

PROBLEM_MESSAGES: dict[str, StrPromise] = {
    "binding_not_current": gettext_lazy(
        "The plant is not approved with this reference connection."
    ),
    "target_not_current": gettext_lazy(
        "The target is not the plant's current approved connection."
    ),
    "same_binding": gettext_lazy("The source and the target are the same connection."),
    "account_not_solcast": gettext_lazy("The provider account is not a Solcast account."),
    "account_unattested": gettext_lazy(
        "The provider account has no recorded service authorization."
    ),
    "binding_not_validated": gettext_lazy("The reference connection has not been validated."),
    "target_not_validated": gettext_lazy("The target connection has not been validated."),
    "source_not_validated": gettext_lazy("The source connection was never validated."),
    "source_unhealthy": gettext_lazy("The source connection is not healthy."),
    "capacity_unverified": gettext_lazy("The reference capacity and power basis are not verified."),
    "settings_unverified": gettext_lazy("The provider site settings have not been checked."),
    "power_basis_not_ac": gettext_lazy(
        "The reference connection uses a DC power basis; an AC basis is required."
    ),
    "target_not_ac": gettext_lazy("The target connection must use an AC power basis."),
    "field_differs": gettext_lazy("The connections differ in more than their capacity: %(field)s."),
    "capacity_invalid": gettext_lazy("Both capacities must be finite and positive."),
    "capacity_equal": gettext_lazy("The capacities are equal; there is nothing to rescale."),
    "dc_undeclared": gettext_lazy("Both connections must declare their DC capacity."),
    "dc_ratio_differs": gettext_lazy("The DC to AC ratio differs between the connections."),
}
# Only the declared capacity may differ between the source and the target of a normalization.
EQUIVALENT_FIELDS = (
    "region_id",
    "provider_account_id",
    "site_id",
    "latitude",
    "longitude",
    "timezone",
    "azimuth_degrees",
    "losses_percent",
    "installation_date",
    "power_basis",
)


def problem_messages(problems: tuple[str, ...]) -> list[str]:
    messages = []
    for problem in problems:
        code, _, field = problem.partition(":")
        message = str(PROBLEM_MESSAGES[code])
        messages.append(message % {"field": field} if field else message)
    return messages


def _account_problems(binding: SiteBinding) -> list[str]:
    account = binding.provider_account
    problems = []
    if account.provider != Provider.SOLCAST:
        problems.append("account_not_solcast")
    if not account.authorization_reference or account.authorized_by_id is None:
        problems.append("account_unattested")
    return problems


def _evidence_problems(*bindings: SiteBinding) -> list[str]:
    problems = []
    if not all(binding.capacity_semantics_verified for binding in bindings):
        problems.append("capacity_unverified")
    if not all(binding.settings_verification_reference for binding in bindings):
        problems.append("settings_unverified")
    return problems


def reference_problems(binding: SiteBinding) -> tuple[str, ...]:
    """Why the binding cannot authorize imported reference history; empty when it can."""
    problems = [] if binding_is_current(binding, binding.region) else ["binding_not_current"]
    problems += _account_problems(binding)
    if binding.validated_at is None:
        problems.append("binding_not_validated")
    problems += _evidence_problems(binding)
    if binding.power_basis != PowerBasis.AC:
        problems.append("power_basis_not_ac")
    return tuple(problems)


def _scale_problems(source: SiteBinding, target: SiteBinding) -> list[str]:
    capacities = (source.capacity_kw, target.capacity_kw)
    valid = all(isfinite(value) and value > 0 for value in capacities)
    factor = target.capacity_kw / source.capacity_kw if valid else 0.0
    if not (isfinite(factor) and factor > 0):
        return ["capacity_invalid"]
    if source.capacity_kw == target.capacity_kw:
        return ["capacity_equal"]
    # An undeclared DC capacity leaves the provider's implicit DC/AC ratio in effect.
    if source.dc_capacity_kw is None or target.dc_capacity_kw is None:
        return ["dc_undeclared"]
    if not isclose(
        source.dc_capacity_kw / source.capacity_kw,
        target.dc_capacity_kw / target.capacity_kw,
        rel_tol=1e-12,
    ):
        return ["dc_ratio_differs"]
    return []


def normalization_problems(source: SiteBinding, target: SiteBinding) -> tuple[str, ...]:
    """Why the source history cannot be rescaled onto the target; empty when it can."""
    if source.pk == target.pk:
        return ("same_binding",)
    problems = [] if binding_is_current(target, target.region) else ["target_not_current"]
    problems += _account_problems(target)
    if target.validated_at is None:
        problems.append("target_not_validated")
    if source.validated_at is None:
        problems.append("source_not_validated")
    if source.health != BindingHealth.HEALTHY:
        problems.append("source_unhealthy")
    problems += _evidence_problems(source, target)
    problems += [
        f"field_differs:{field}"
        for field in EQUIVALENT_FIELDS
        if getattr(source, field) != getattr(target, field)
    ]
    if target.power_basis != PowerBasis.AC:
        problems.append("target_not_ac")
    return tuple(problems + _scale_problems(source, target))
