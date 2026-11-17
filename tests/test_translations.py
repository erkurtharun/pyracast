from datetime import date
from pathlib import Path
from typing import Any

import polib
import pytest
import yaml
from django.template.loader import get_template
from django.utils import translation

from pyracast.accounts.language import browser_language
from pyracast.admin_forms import ProviderSetupForm
from scripts.translations import compile_catalogs

ROOT = Path(__file__).resolve().parents[1]


def ci_jobs() -> dict[str, dict[str, Any]]:
    jobs: dict[str, dict[str, Any]] = yaml.safe_load(
        (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    )["jobs"]
    return jobs


def test_packaged_catalogs_are_complete_and_compiled() -> None:
    assert compile_catalogs(ROOT, check=True) == 0
    templates = ROOT / "src/pyracast/regions/templates"
    for template in templates.rglob("*.html"):
        get_template(template.relative_to(templates).as_posix())


def test_every_turkish_catalog_translates_in_the_test_run() -> None:
    sources = sorted(ROOT.glob("src/**/locale/tr/LC_MESSAGES/django.po"))

    assert sources
    with translation.override("tr"):
        for source in sources:
            entry = polib.pofile(str(source))[0]
            assert entry.msgstr != entry.msgid, source
            assert translation.gettext(entry.msgid) == entry.msgstr, source


def dockerfile_stages(path: Path) -> list[list[str]]:
    stages: list[list[str]] = []
    for line in path.read_text(encoding="utf-8").replace("\\\n", " ").splitlines():
        instruction = " ".join(line.split())
        if instruction.upper().startswith("FROM "):
            stages.append([])
        if stages and instruction and not instruction.startswith("#"):
            stages[-1].append(instruction)
    return stages


def image_dockerfiles() -> list[Path]:
    builds = [
        step["with"]
        for step in ci_jobs()["deployment"]["steps"]
        if step.get("uses", "").startswith("docker/build-push-action@")
    ]
    assert builds
    return [ROOT / build.get("file", f"{build['context']}/Dockerfile") for build in builds]


def test_every_django_image_compiles_the_catalogs_before_installing_the_project() -> None:
    builds = []
    for dockerfile in image_dockerfiles():
        for stage in dockerfile_stages(dockerfile):
            installs = [
                index
                for index, instruction in enumerate(stage)
                if "uv sync" in instruction and "--no-install-project" not in instruction
            ]
            if installs:
                builds.append((dockerfile, stage, installs[0]))

    assert [dockerfile.relative_to(ROOT).as_posix() for dockerfile, *_ in builds] == ["Dockerfile"]
    for dockerfile, stage, install in builds:
        compiles = [
            index
            for index, instruction in enumerate(stage)
            if instruction.endswith("python -m django compilemessages")
        ]
        assert compiles, dockerfile
        assert stage.index("COPY src ./src") < compiles[0] < install, dockerfile
        assert any(
            "apt-get install" in instruction and " gettext" in instruction
            for instruction in stage[: compiles[0]]
        ), dockerfile


def test_compiled_catalogs_never_come_from_the_source_tree() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    image_context = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert "*.mo" in ignored
    assert image_context.index("**/*.mo") > image_context.index("!src/**")


def test_every_ci_job_that_runs_pytest_directly_compiles_the_catalogs_first() -> None:
    checked = []
    for name, job in ci_jobs().items():
        commands = [step.get("run", "") for step in job["steps"]]
        for index, command in enumerate(commands):
            if " -m pytest" in command:
                checked.append(name)
                assert any(
                    earlier.endswith("python scripts/translations.py")
                    for earlier in commands[:index]
                ), name

    assert checked


def test_oversized_browser_header_cannot_truncate_a_zero_quality() -> None:
    assert browser_language(" " * 8190 + "tr;q=0") == (None, frozenset({"*"}))


def test_admin_date_control_preserves_html_date_format_in_turkish() -> None:
    with translation.override("tr"):
        widget = ProviderSetupForm.base_fields["installation_date"].widget
        assert 'value="2020-01-01"' in widget.render("installation_date", date(2020, 1, 1))


def test_catalog_check_validates_sources_without_writing_and_compile_writes_them(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src/example/locale/tr/LC_MESSAGES/django.po"
    source.parent.mkdir(parents=True)
    compiled = source.with_suffix(".mo")
    catalog = polib.POFile()
    catalog.metadata = {"Language": "tr", "Content-Type": "text/plain; charset=UTF-8"}
    catalog.append(polib.POEntry(msgid="Saved", msgstr="Kaydedildi"))
    catalog.save(str(source))
    assert compile_catalogs(tmp_path, check=True) == 0
    assert not compiled.exists()
    assert compile_catalogs(tmp_path, check=False) == 0
    assert [(entry.msgid, entry.msgstr) for entry in polib.mofile(str(compiled))] == [
        ("Saved", "Kaydedildi")
    ]
    first = compiled.read_bytes()
    assert compile_catalogs(tmp_path, check=False) == 0
    assert compiled.read_bytes() == first
    catalog.append(polib.POEntry(msgid="Unknown", msgstr=""))
    catalog.save(str(source))
    assert compile_catalogs(tmp_path, check=True) == 1
    assert compile_catalogs(tmp_path, check=False) == 1


@pytest.mark.parametrize("rename", [False, True])
def test_compilation_removes_generated_catalogs_without_a_source(
    tmp_path: Path, rename: bool
) -> None:
    source = tmp_path / "src/example/locale/tr/LC_MESSAGES/django.po"
    source.parent.mkdir(parents=True)
    catalog = polib.POFile()
    catalog.append(polib.POEntry(msgid="Saved", msgstr="Kaydedildi"))
    catalog.save(str(source))
    assert compile_catalogs(tmp_path, check=False) == 0
    compiled = source.with_suffix(".mo")
    assert compiled.exists()
    renamed = source.with_name("messages.po")
    if rename:
        source.rename(renamed)
    else:
        source.unlink()

    assert compile_catalogs(tmp_path, check=True) == 0
    assert compiled.exists()
    assert not renamed.with_suffix(".mo").exists()
    assert compile_catalogs(tmp_path, check=False) == 0
    assert not compiled.exists()
    if rename:
        assert [
            (entry.msgid, entry.msgstr) for entry in polib.mofile(str(renamed.with_suffix(".mo")))
        ] == [("Saved", "Kaydedildi")]


@pytest.mark.parametrize("flags", [["fuzzy"], []])
def test_untranslated_or_fuzzy_catalog_cannot_replace_artifact(
    tmp_path: Path, flags: list[str]
) -> None:
    source = tmp_path / "src/example/locale/tr/LC_MESSAGES/django.po"
    source.parent.mkdir(parents=True)
    catalog = polib.POFile()
    catalog.append(polib.POEntry(msgid="Saved", msgstr="Kaydedildi" if flags else "", flags=flags))
    catalog.save(str(source))
    assert compile_catalogs(tmp_path, check=False) == 1
    assert not source.with_suffix(".mo").exists()


def test_catalog_compilation_rejects_changed_interpolation_parameters(tmp_path: Path) -> None:
    source = tmp_path / "src/example/locale/tr/LC_MESSAGES/django.po"
    source.parent.mkdir(parents=True)
    catalog = polib.POFile()
    catalog.append(polib.POEntry(msgid="Forecast for %(plant)s", msgstr="%(user)s tahmini"))
    catalog.save(str(source))
    assert compile_catalogs(tmp_path, check=False) == 1
    assert not source.with_suffix(".mo").exists()
    catalog[0].msgstr = "%(plant)s tahmini"
    catalog.save(str(source))
    assert compile_catalogs(tmp_path, check=False) == 0
