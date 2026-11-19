import ast
import re
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "src" / "pyracast"
CODE = re.compile(r"[a-z][a-z0-9_]*")
VIEW_BASES = {"APIView", "GenericAPIView", "ScopedView", "RequestView"}
ERROR_KEYS = {"code", "detail", "message", "error", "errors"}
# Refusals inside allauth's own flows speak allauth's protocol (`errors[].code` is read by allauth
# and the sign-in screens); PrivateAPIResponses adds the contract keys to every such body.
ALLAUTH_PROTOCOL = {
    "accounts/adapters.py:AccountAdapter.validation_error",
    "accounts/adapters.py:AccountAdapter.pre_authenticate",
    "accounts/adapters.py:AccountAdapter.confirm_email",
    # A completion that failed answers allauth's own answer for a client with nothing pending.
    "accounts/headless.py:ResetPasswordView.post",
    "accounts/headless.py:VerifyEmailView.post",
    "accounts/headless.py:ResendEmailVerificationCodeView.post",
    "accounts/headless.py:ConfirmReauthenticationCodeView.post",
    "accounts/mfa_views.py:AuthenticateView.handle",
    "accounts/headless.py:ManageEmailView.post",
    "accounts/social_views.py:ProviderSignupView.handle",
    # The provider redirect and callback answer with allauth's error redirect, as allauth's own
    # callback does.
    "accounts/social_views.py:RedirectToProviderView.post",
    "accounts/social_views.py:ProviderCallbackView.dispatch",
    "accounts/inputs.py:ChangePasswordInput.clean_current_password",
    "accounts/inputs.py:SecondFactorCode.matching_code",
    "accounts/inputs.py:TOTPActivationInput.clean_code.matching_code",
    "accounts/login_limits.py:check_second_factor",
    "accounts/social.py:SocialAccountAdapter.connect_account",
    "accounts/social.py:SocialAccountAdapter.validate_disconnect",
    "accounts/step_up.py:step_up_required",
}


def modules() -> list[tuple[Path, ast.Module]]:
    return [
        (path, ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(SOURCE.rglob("*.py"))
        if "migrations" not in path.parts
    ]


def name_of(node: ast.expr) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Subscript):
        return name_of(node.value)
    return ""


def root_of(node: ast.expr) -> str:
    while isinstance(node, ast.Attribute | ast.Subscript):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def allauth_views(tree: ast.Module) -> set[str]:
    imported = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("allauth.")
        for alias in node.names
        if (node.module or "").endswith("views") or alias.name == "views"
    }
    imported.update(
        alias.asname or alias.name.partition(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name.startswith("allauth.") and alias.name.endswith(".views")
    )
    return imported


def validation_message(node: ast.Call) -> ast.expr | None:
    if node.args:
        return node.args[0]
    return next((keyword.value for keyword in node.keywords if keyword.arg == "message"), None)


def coded_validation_errors(tree: ast.Module) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and name_of(node.func) == "ValidationError"
        and isinstance(message := validation_message(node), ast.Constant)
        and isinstance(message.value, str)
        and CODE.fullmatch(message.value)
    ]


def translates(handler: ast.ExceptHandler) -> bool:
    last = handler.body[-1]
    if not isinstance(last, ast.Raise):
        return True
    if last.exc is None:
        return False
    if isinstance(last.exc, ast.Name) and last.exc.id == handler.name:
        return any(
            isinstance(node, ast.Name)
            and node.id == handler.name
            and isinstance(node.ctx, ast.Store)
            for statement in handler.body[:-1]
            for node in ast.walk(statement)
        )
    return True


def framework_errors_and_translations(tree: ast.Module) -> list[int]:
    lines = []
    view_bases = VIEW_BASES | allauth_views(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        bases = {name_of(base) for base in node.bases}
        roots = {root_of(base) for base in node.bases}
        if "APIException" in bases:
            lines.append(node.lineno)
        lines.extend(
            item.lineno
            for item in node.body
            if isinstance(item, ast.FunctionDef) and item.name == "handle_exception"
        )
        if (bases | roots) & view_bases:
            lines.extend(
                handler.lineno
                for method in node.body
                if isinstance(method, ast.FunctionDef)
                for handler in ast.walk(method)
                if isinstance(handler, ast.ExceptHandler) and translates(handler)
            )
    return lines


def response_body(node: ast.Call) -> ast.expr | None:
    if node.args:
        return node.args[0]
    return next((keyword.value for keyword in node.keywords if keyword.arg == "data"), None)


def refusing_status(node: ast.Call) -> bool:
    status = next((keyword.value for keyword in node.keywords if keyword.arg == "status"), None)
    if isinstance(status, ast.Attribute):
        constant = re.fullmatch(r"HTTP_(\d{3})_\w+", status.attr)
        return constant is not None and int(constant[1]) >= 400
    return (
        isinstance(status, ast.Constant) and isinstance(status.value, int) and status.value >= 400
    )


def handmade_error_bodies(tree: ast.Module) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = name_of(node.func)
        keywords = {keyword.arg for keyword in node.keywords}
        if name in {"APIResponse", "ErrorResponse"} and (
            name == "ErrorResponse" or "errors" in keywords
        ):
            lines.append(node.lineno)
        if name not in {"Response", "JsonResponse"}:
            continue
        body = response_body(node)
        literal = isinstance(body, ast.Dict) and any(
            isinstance(key, ast.Constant) and key.value in ERROR_KEYS for key in body.keys
        )
        variable = body is not None and not isinstance(body, ast.Dict) and refusing_status(node)
        if literal or variable:
            lines.append(node.lineno)
    return lines


def validation_error_factories(tree: ast.Module) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and name_of(node.func) == "validation_error"
    ]


def enclosing_names(tree: ast.Module) -> dict[int, str]:
    names: dict[int, str] = {}

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                inner = (*scope, child.name)
            if hasattr(child, "lineno") and inner:
                names.setdefault(child.lineno, ".".join(inner))
            visit(child, inner)

    visit(tree, ())
    return names


def old_style_sites() -> list[str]:
    sites: list[str] = []
    for path, tree in modules():
        names = enclosing_names(tree)
        lines = (
            coded_validation_errors(tree)
            + framework_errors_and_translations(tree)
            + handmade_error_bodies(tree)
            + validation_error_factories(tree)
        )
        sites.extend(
            f"{path.relative_to(SOURCE).as_posix()}:{names.get(line, '<module>')}" for line in lines
        )
    return sites


def test_no_error_is_raised_or_answered_outside_the_shared_contract() -> None:
    assert sorted(set(old_style_sites()) - ALLAUTH_PROTOCOL) == []


def test_every_allauth_protocol_exception_is_still_a_site() -> None:
    assert sorted(ALLAUTH_PROTOCOL - set(old_style_sites())) == []


def test_the_scan_recognises_each_old_style_shape() -> None:
    sample = ast.parse(
        "from rest_framework.exceptions import APIException\n"
        "class Conflict(APIException):\n"
        "    pass\n"
        "class View(APIView):\n"
        "    def handle_exception(self, exc):\n"
        "        return Response({'code': 'x'}, status=409)\n"
        "    def post(self, request):\n"
        "        try:\n"
        "            raise ValidationError('location_unchanged')\n"
        "        except ValidationError:\n"
        "            return JsonResponse({'detail': 'x'})\n"
    )
    assert coded_validation_errors(sample) == [9]
    assert sorted(framework_errors_and_translations(sample)) == [2, 5, 10]
    assert sorted(handmade_error_bodies(sample)) == [6, 11]


def test_the_scan_recognises_allauth_and_indirect_shapes() -> None:
    sample = ast.parse(
        "from allauth.headless.account import views\n"
        "from allauth.headless.mfa.views import AuthenticateView\n"
        "class Login(views.LoginView):\n"
        "    def post(self, request):\n"
        "        try:\n"
        "            pass\n"
        "        except KeyError:\n"
        "            return APIResponse(request, status=400, errors=[{'message': 'x'}])\n"
        "class Second(AuthenticateView):\n"
        "    def post(self, request):\n"
        "        try:\n"
        "            pass\n"
        "        except KeyError:\n"
        "            raise\n"
        "def answer(request, body):\n"
        "    Response(data={'message': 'x'})\n"
        "    Response(body, status=409)\n"
        "    JsonResponse({'errors': []})\n"
        "    ErrorResponse(request, input=body)\n"
        "    Response(body)\n"
        "    raise get_adapter().validation_error('incorrect_code')\n"
    )
    assert framework_errors_and_translations(sample) == [7]
    assert sorted(handmade_error_bodies(sample)) == [8, 16, 17, 18, 19]
    assert validation_error_factories(sample) == [21]
    assert enclosing_names(sample)[8] == "Login.post"


@pytest.mark.parametrize(
    ("import_line", "base"),
    [
        ("import allauth.headless.account.views as views", "views.LoginView"),
        ("import allauth.headless.account.views", "allauth.headless.account.views.LoginView"),
    ],
)
def test_the_scan_recognises_direct_allauth_view_imports(import_line: str, base: str) -> None:
    sample = ast.parse(
        f"{import_line}\n"
        f"class Login({base}):\n"
        "    def post(self, request):\n"
        "        try:\n"
        "            pass\n"
        "        except KeyError:\n"
        "            return Response({'message': 'x'}, status=400)\n"
    )
    assert framework_errors_and_translations(sample) == [6]


def test_the_scan_keeps_an_explicit_reraise_of_the_caught_error() -> None:
    sample = ast.parse(
        "class View(APIView):\n"
        "    def post(self, request):\n"
        "        try:\n"
        "            pass\n"
        "        except KeyError as error:\n"
        "            raise error\n"
    )
    assert framework_errors_and_translations(sample) == []


def test_the_scan_recognises_a_replaced_caught_error() -> None:
    sample = ast.parse(
        "class View(APIView):\n"
        "    def post(self, request):\n"
        "        try:\n"
        "            pass\n"
        "        except KeyError as error:\n"
        "            error = ValueError('replacement')\n"
        "            raise error\n"
    )
    assert framework_errors_and_translations(sample) == [5]


def test_the_scan_recognises_status_constants_and_keyword_codes() -> None:
    sample = ast.parse(
        "def answer(request, body):\n"
        "    Response(body, status=status.HTTP_400_BAD_REQUEST)\n"
        "    Response(body, status=status.HTTP_200_OK)\n"
        "    raise ValidationError(message='unsupported_language')\n"
        "    raise ValidationError(message='Choose a supported language.')\n"
    )
    assert handmade_error_bodies(sample) == [2]
    assert coded_validation_errors(sample) == [4]
