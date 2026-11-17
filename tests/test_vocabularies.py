import json
from pathlib import Path
from typing import cast, get_args

import pytest
from django.apps import apps
from django.conf import settings
from django.db import models
from drf_spectacular.plumbing import deep_import_string

from pyracast.accounts.language import Language
from pyracast.accounts.models import UserLanguagePreference
from tests.support.vocabularies import (
    COVERED_APPS,
    FAILURE_CODES,
    FREE_TEXT,
    INPUT_VOCABULARIES,
    STORED_VOCABULARIES,
    UNCHECKED_VOCABULARIES,
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
    assert declared == {(vocabulary.model, vocabulary.field) for vocabulary in STORED_VOCABULARIES}


def test_every_text_column_without_an_enumeration_of_the_covered_apps_is_classified() -> None:
    undeclared = {
        (model._meta.label, field.name)
        for label in COVERED_APPS
        for model in apps.get_app_config(label).get_models()
        for field in model._meta.concrete_fields
        if isinstance(field, models.CharField) and not field.choices
    }
    assert undeclared == FREE_TEXT | FAILURE_CODES | UNCHECKED_VOCABULARIES


@pytest.mark.parametrize("vocabulary", [_stored(vocabulary) for vocabulary in STORED_VOCABULARIES])
def test_stored_vocabulary_is_checked_by_a_constraint_derived_from_its_enumeration(
    vocabulary: StoredVocabulary,
) -> None:
    assert vocabulary.choices
    assert derived_check(vocabulary) is not None


@pytest.mark.parametrize("vocabulary", [_stored(vocabulary) for vocabulary in STORED_VOCABULARIES])
def test_stored_vocabulary_is_exported_by_name_to_the_api_schema(
    vocabulary: StoredVocabulary,
) -> None:
    exported = [
        name for name, choices in _exported().items() if choices.choices == vocabulary.choices
    ]
    assert len(exported) == 1
    components = json.loads(OPENAPI.read_text(encoding="utf-8"))["components"]["schemas"]
    declarations = " ".join(TYPESCRIPT.read_text("utf-8").split()).replace(": | ", ": ")
    if not vocabulary.customer_api:
        assert exported[0] not in components
        assert f" {exported[0]}:" not in declarations
        return
    values = [value for value, _ in vocabulary.choices]
    assert components[exported[0]]["enum"] == values
    union = " | ".join(json.dumps(value) for value in values)
    assert f" {exported[0]}: {union};" in declarations


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
    assert_database_rejects(vocabulary.row(monkeypatch), vocabulary.field, value)


@pytest.mark.parametrize(("case", "value"), _input_values(serialized=True))
def test_serializer_rejects_values_outside_the_vocabulary(
    case: InputVocabulary, value: str
) -> None:
    assert_serializer_rejects(case, value)


@pytest.mark.parametrize(("case", "value"), _input_values())
def test_operation_rejects_values_outside_the_vocabulary(
    case: InputVocabulary, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert_operation_rejects(case.operation(monkeypatch), value)


def test_supported_languages_have_one_value_set() -> None:
    field = UserLanguagePreference._meta.get_field("language")
    assert list(field.choices or ()) == list(settings.LANGUAGES)
    assert get_args(Language) == tuple(code for code, _ in settings.LANGUAGES)
