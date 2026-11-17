import ast
import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "pyracast"
CODE = re.compile(r"[a-z][a-z0-9_]*")
VIEW_BASES = {"APIView", "GenericAPIView", "ScopedView", "RequestView"}


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


def coded_validation_errors(tree: ast.Module) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and name_of(node.func) == "ValidationError"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and CODE.fullmatch(node.args[0].value)
    ]


def framework_errors_and_translations(tree: ast.Module) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        bases = {name_of(base) for base in node.bases}
        if "APIException" in bases:
            lines.append(node.lineno)
        lines.extend(
            item.lineno
            for item in node.body
            if isinstance(item, ast.FunctionDef) and item.name == "handle_exception"
        )
        if bases & VIEW_BASES:
            lines.extend(
                handler.lineno
                for method in node.body
                if isinstance(method, ast.FunctionDef)
                for handler in ast.walk(method)
                if isinstance(handler, ast.ExceptHandler)
            )
    return lines


def handmade_error_bodies(tree: ast.Module) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and name_of(node.func) in {"Response", "JsonResponse"}
        and node.args
        and isinstance(node.args[0], ast.Dict)
        and any(
            isinstance(key, ast.Constant) and key.value in {"code", "detail"}
            for key in node.args[0].keys
        )
    ]


def old_style_sites() -> list[str]:
    return [
        f"{path.relative_to(SOURCE).as_posix()}:{line}"
        for path, tree in modules()
        for line in (
            coded_validation_errors(tree)
            + framework_errors_and_translations(tree)
            + handmade_error_bodies(tree)
        )
    ]


def test_no_error_is_raised_or_answered_outside_the_shared_contract() -> None:
    assert old_style_sites() == []


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
