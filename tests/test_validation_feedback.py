import ast
from pathlib import Path

import pytest
from django.utils import translation

from pyracast import providers
from pyracast.admin_forms import validation_failure_message
from pyracast.connections import secrets
from pyracast.connections.contracts import ProviderError
from tests.support.admin_setup import VALIDATION_FAILURES

UNKNOWN_FAILURE = (
    "unrecognized_failure",
    None,
    "Connection validation failed. Check the private credentials and site settings.",
)


def raised_codes(path: Path) -> set[str]:
    codes = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ProviderError"
        ):
            code = node.args[0]
            assert isinstance(code, ast.Constant), ast.unparse(node)
            assert isinstance(code.value, str), ast.unparse(node)
            codes.add(code.value)
    return codes


@pytest.mark.parametrize(("code", "status", "message"), [*VALIDATION_FAILURES, UNKNOWN_FAILURE])
def test_a_failed_validation_names_its_cause_in_english_and_turkish(
    code: str, status: int | None, message: str
) -> None:
    error = ProviderError(code, status_code=status)

    with translation.override("en"):
        assert validation_failure_message(error) == message
    with translation.override("tr"):
        assert validation_failure_message(error) not in {"", message}


def test_every_code_on_the_provider_path_has_its_own_validation_message() -> None:
    codes = raised_codes(Path(providers.__file__)) | raised_codes(Path(secrets.__file__))
    generic = validation_failure_message(ProviderError(UNKNOWN_FAILURE[0]))

    assert codes == {code for code, _, _ in VALIDATION_FAILURES}
    assert generic not in {validation_failure_message(ProviderError(code)) for code in codes}
