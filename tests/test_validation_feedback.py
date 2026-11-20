import ast
from pathlib import Path

import pytest
from django.utils import translation

from pyracast import providers
from pyracast.connections import secrets
from pyracast.connections.contracts import ProviderError
from pyracast.platform_admin.forms import validation_failure_message
from tests.support.admin_setup import VALIDATION_FAILURES

UNKNOWN_FAILURE = (
    "unrecognized_failure",
    None,
    "Connection validation failed. Check the private credentials and site settings.",
)
REJECTED_KEY_TR = (
    "Sağlay\u0131c\u0131 API anahtar\u0131n\u0131 reddetti. "
    "Bu hesab\u0131n güncel anahtar\u0131n\u0131 girip yeniden doğrulay\u0131n."
)
UNREADABLE_ANSWER_TR = (
    "Sağlay\u0131c\u0131n\u0131n yan\u0131t\u0131 okunamad\u0131. "
    "Doğrulamay\u0131 daha sonra tekrar deneyin."
)
UNSUPPORTED_ACCOUNT_TR = "Bu hesap burada doğrulanamaz. Bir Solcast hesab\u0131 seçin."
PROVIDER_ERROR_TR = (
    "Sağlay\u0131c\u0131 bir hatayla yan\u0131t verdi. Doğrulamay\u0131 daha sonra tekrar deneyin."
)
REFUSED_REQUEST_TR = (
    "Sağlay\u0131c\u0131 isteği kabul etmedi. "
    "Kaynak kimliğini ve tesis ayarlar\u0131n\u0131 denetleyip yeniden doğrulay\u0131n."
)
TURKISH_FAILURES: dict[tuple[str, int | None], str] = {
    ("provider_request_failed", 401): REJECTED_KEY_TR,
    ("provider_request_failed", 403): REJECTED_KEY_TR,
    ("provider_request_failed", 404): (
        "Sağlay\u0131c\u0131 bu API anahtar\u0131yla "
        "bu kaynak kimliğine sahip bir tesis bulamad\u0131. "
        "Kaynak kimliğini ve anahtar\u0131n, tesisin sahibi olan hesaba ait olduğunu denetleyin."
    ),
    ("provider_request_failed", 429): (
        "Sağlay\u0131c\u0131 istekleri s\u0131n\u0131rl\u0131yor. "
        "Yeniden doğrulamadan önce bekleyin."
    ),
    ("provider_request_failed", None): (
        "Sağlay\u0131c\u0131ya ulaş\u0131lamad\u0131. "
        "Birkaç dakika sonra doğrulamay\u0131 tekrar deneyin."
    ),
    ("provider_request_failed", 500): PROVIDER_ERROR_TR,
    ("provider_request_failed", 503): PROVIDER_ERROR_TR,
    ("provider_request_failed", 400): REFUSED_REQUEST_TR,
    ("provider_request_failed", 302): REFUSED_REQUEST_TR,
    ("invalid_site_id", None): (
        "Kaynak kimliği sağlay\u0131c\u0131n\u0131n kullanmad\u0131ğ\u0131 karakterler içeriyor. "
        "Yaln\u0131zca harf, rakam, k\u0131sa çizgi ve alt çizgi kullan\u0131n."
    ),
    ("invalid_provider_secret", None): (
        "Kay\u0131tl\u0131 API anahtar\u0131 okunam\u0131yor. "
        "Anahtar\u0131 yeniden girip doğrulay\u0131n."
    ),
    ("invalid_provider_schema", None): UNREADABLE_ANSWER_TR,
    ("invalid_provider_interval", None): UNREADABLE_ANSWER_TR,
    ("invalid_provider_endpoint", None): UNREADABLE_ANSWER_TR,
    ("unsafe_provider_response", None): UNREADABLE_ANSWER_TR,
    ("unsupported_provider", None): UNSUPPORTED_ACCOUNT_TR,
    ("fixture_account_required", None): UNSUPPORTED_ACCOUNT_TR,
    ("unrecognized_failure", None): (
        "Bağlant\u0131 doğrulanamad\u0131. "
        "Özel kimlik bilgilerini ve tesis ayarlar\u0131n\u0131 denetleyin."
    ),
}


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


@pytest.mark.parametrize(
    ("code", "status", "message", "turkish_message"),
    [
        (code, status, message, TURKISH_FAILURES[code, status])
        for code, status, message in [*VALIDATION_FAILURES, UNKNOWN_FAILURE]
    ],
)
def test_a_failed_validation_names_its_cause_in_english_and_turkish(
    code: str, status: int | None, message: str, turkish_message: str
) -> None:
    error = ProviderError(code, status_code=status)

    with translation.override("en"):
        assert validation_failure_message(error) == message
    with translation.override("tr"):
        assert validation_failure_message(error) == turkish_message


def test_every_code_on_the_provider_path_has_its_own_validation_message() -> None:
    codes = raised_codes(Path(providers.__file__)) | raised_codes(Path(secrets.__file__))
    generic = validation_failure_message(ProviderError(UNKNOWN_FAILURE[0]))

    assert codes == {code for code, _, _ in VALIDATION_FAILURES}
    assert generic not in {validation_failure_message(ProviderError(code)) for code in codes}
