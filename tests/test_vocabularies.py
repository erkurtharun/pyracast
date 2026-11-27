import json
import re
from pathlib import Path
from typing import cast, get_args

import pytest
from django.apps import apps
from django.conf import settings
from django.db import IntegrityError, models, transaction
from drf_spectacular.plumbing import deep_import_string

from pyracast.accounts.invitations import invite_workspace_member
from pyracast.accounts.models import (
    OutboundMail,
    OutboundMailError,
    OutboundMailKind,
    UserLanguagePreference,
)
from pyracast.db import violated_constraint
from pyracast.languages import Language
from pyracast.mail.errors import MailFailure
from pyracast.notifications.models import PAUSED_DELIVERY_ERRORS, NotificationDeliveryError
from pyracast.plants.models import CalibrationAssessment
from pyracast.plants.numerics import FitAssessment
from pyracast.regions.models import AuditAction, AuditEvent
from tests.support.domain import make_system
from tests.support.vocabularies import (
    COVERED_APPS,
    FAILURE_CODES,
    FREE_TEXT,
    INPUT_VOCABULARIES,
    STORED_VOCABULARIES,
    InputVocabulary,
    StoredVocabulary,
    assert_database_rejects,
    assert_operation_rejects,
    assert_serializer_rejects,
    derived_check,
)

pytestmark = pytest.mark.django_db(transaction=True)

WEB = Path(__file__).resolve().parents[1] / "web"
OPENAPI = WEB / "openapi.json"
TYPESCRIPT = WEB / "src" / "lib" / "schema.d.ts"


def _stored(vocabulary: StoredVocabulary) -> object:
    return pytest.param(vocabulary, id=vocabulary.id)


def _stored_values() -> list[object]:
    return [
        pytest.param(vocabulary, value, id=f"{vocabulary.id}={value}")
        for vocabulary in STORED_VOCABULARIES
        for value in vocabulary.rejected
    ]


def _input_values(*, serialized: bool = False) -> list[object]:
    return [
        pytest.param(case, value, id=f"{case.id}={value}")
        for case in INPUT_VOCABULARIES
        if case.serializer is not None or not serialized
        for value in case.rejected
    ]


def _exported() -> dict[str, type[models.TextChoices]]:
    overrides = cast(dict[str, object], settings.SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"])
    resolved = {
        name: deep_import_string(path) for name, path in overrides.items() if isinstance(path, str)
    }
    return {
        name: choices
        for name, choices in resolved.items()
        if isinstance(choices, type) and issubclass(choices, models.TextChoices)
    }


def test_every_vocabulary_column_of_the_covered_apps_is_registered() -> None:
    declared = {
        (model, field.name)
        for label in COVERED_APPS
        for model in apps.get_app_config(label).get_models()
        for field in model._meta.concrete_fields
        if field.choices
    }
    assert declared == {
        (vocabulary.model, vocabulary.field)
        for vocabulary in STORED_VOCABULARIES
        if not vocabulary.key
    }


def test_every_text_column_without_an_enumeration_of_the_covered_apps_is_classified() -> None:
    undeclared = {
        (model._meta.label, field.name)
        for label in COVERED_APPS
        for model in apps.get_app_config(label).get_models()
        for field in model._meta.concrete_fields
        if isinstance(field, models.CharField) and not field.choices
    }
    assert undeclared == FREE_TEXT | FAILURE_CODES


@pytest.mark.parametrize("vocabulary", [_stored(vocabulary) for vocabulary in STORED_VOCABULARIES])
def test_stored_vocabulary_is_checked_by_a_constraint_derived_from_its_enumeration(
    vocabulary: StoredVocabulary,
) -> None:
    assert vocabulary.choices
    assert derived_check(vocabulary) is not None


@pytest.mark.parametrize(
    "vocabulary",
    [_stored(vocabulary) for vocabulary in STORED_VOCABULARIES if vocabulary.published],
)
def test_stored_vocabulary_is_exported_by_name_to_the_api_schema(
    vocabulary: StoredVocabulary,
) -> None:
    exported = [
        name for name, choices in _exported().items() if choices.choices == vocabulary.choices
    ]
    assert len(exported) == 1
    values = [value for value, _ in vocabulary.choices]
    components = json.loads(OPENAPI.read_text(encoding="utf-8"))["components"]["schemas"]
    assert components[exported[0]]["enum"] == values
    union = re.search(rf"\b{exported[0]}:([^;]+);", TYPESCRIPT.read_text(encoding="utf-8"))
    assert union is not None
    assert re.findall(r'"([^"]*)"', union[1]) == values


@pytest.mark.parametrize(
    "vocabulary",
    [_stored(vocabulary) for vocabulary in STORED_VOCABULARIES if not vocabulary.published],
)
def test_operator_vocabulary_stays_out_of_the_customer_schema(
    vocabulary: StoredVocabulary,
) -> None:
    assert all(choices.choices != vocabulary.choices for choices in _exported().values())


def test_collection_jobs_are_described_by_their_enumerations() -> None:
    job = json.loads(OPENAPI.read_text(encoding="utf-8"))["components"]["schemas"][
        "CollectionJobOutput"
    ]["properties"]
    assert job["endpoint"] == {"$ref": "#/components/schemas/CollectionEndpointEnum"}
    assert job["status"] == {"$ref": "#/components/schemas/CollectionJobStatusEnum"}


@pytest.mark.parametrize(("vocabulary", "value"), _stored_values())
def test_database_rejects_values_outside_the_vocabulary(
    vocabulary: StoredVocabulary, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = vocabulary.row(monkeypatch)
    assert_database_rejects(row, vocabulary.assignment(row, value))


@pytest.mark.parametrize(("case", "value"), _input_values(serialized=True))
def test_serializer_rejects_values_outside_the_vocabulary(
    case: InputVocabulary, value: str
) -> None:
    assert_serializer_rejects(case, value)


@pytest.mark.parametrize(("case", "value"), _input_values())
def test_operation_rejects_values_outside_the_vocabulary(
    case: InputVocabulary, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert_operation_rejects(case, case.operation(monkeypatch), value)


def refusal(row: models.Model, **values: object) -> str | None:
    try:
        with transaction.atomic():
            type(row)._default_manager.filter(pk=row.pk).update(**values)
    except IntegrityError as error:
        return violated_constraint(error)
    return None


def test_the_database_stores_every_audit_action_mail_kind_and_mail_error_and_no_other() -> None:
    system = make_system()
    invite_workspace_member(system.member, system.tenant.pk, "guest@example.com")
    event = (
        AuditEvent.objects.filter(region=system.region)
        .exclude(action=AuditAction.REGION_CREATED)
        .earliest("pk")
    )
    mail = OutboundMail.objects.get()

    assert {
        action: refusal(event, action=action) for action in AuditAction.values
    } == dict.fromkeys(AuditAction.values)
    assert {
        kind: refusal(
            mail,
            kind=kind,
            invitation=mail.invitation if kind == OutboundMailKind.WORKSPACE_INVITATION else None,
        )
        for kind in OutboundMailKind.values
    } == dict.fromkeys(OutboundMailKind.values)
    assert {
        error: refusal(mail, last_error=error) for error in OutboundMailError.values
    } == dict.fromkeys(OutboundMailError.values)
    assert refusal(event, action="xx") == "auditevent_action_chk"
    assert refusal(mail, kind="xx", invitation=None) == "outboundmail_kind_chk"
    assert refusal(mail, last_error="xx") == "outboundmail_last_error_chk"


def test_every_mail_failure_can_be_stored_on_a_reminder_delivery() -> None:
    assert set(MailFailure) <= set(NotificationDeliveryError)
    assert set(PAUSED_DELIVERY_ERRORS) <= set(NotificationDeliveryError)


def test_every_fitted_assessment_can_be_stored_on_a_calibration_run() -> None:
    assert set(get_args(FitAssessment)) == set(CalibrationAssessment)


def test_supported_languages_have_one_value_set() -> None:
    field = UserLanguagePreference._meta.get_field("language")
    assert list(field.choices or ()) == list(settings.LANGUAGES)
    assert get_args(Language) == tuple(code for code, _ in settings.LANGUAGES)
