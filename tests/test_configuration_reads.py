import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "pyracast"
# Django's entry points name their settings module before Django reads any setting.
ENTRY_POINTS = {"cli.py", "wsgi.py"}


def environment_uses(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv", "environb"}) or (
            isinstance(node, ast.ImportFrom)
            and node.module == "os"
            and {alias.name for alias in node.names} & {"environ", "getenv", "environb"}
        ):
            found.append(f"{path.relative_to(SOURCE).as_posix()}:{node.lineno}")
    return found


def entry_point_setting(path: Path) -> bool:
    return 'os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pyracast.settings")' in path.read_text(
        encoding="utf-8"
    )


def test_only_the_settings_module_reads_the_environment() -> None:
    uses = [
        use
        for path in sorted(SOURCE.rglob("*.py"))
        if path.name != "settings.py" or path.parent != SOURCE
        if not (path.parent == SOURCE and path.name in ENTRY_POINTS and entry_point_setting(path))
        for use in environment_uses(path)
    ]

    assert uses == []


def test_the_entry_points_only_name_the_settings_module() -> None:
    for name in ENTRY_POINTS:
        path = SOURCE / name
        assert entry_point_setting(path)
        assert len(environment_uses(path)) == 1
