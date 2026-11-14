from datetime import date
from pathlib import Path

import polib
import pytest
from django.template.loader import get_template
from django.utils import translation

from pyracast.accounts.language import browser_language
from pyracast.admin_forms import ProviderSetupForm
from scripts.translations import compile_catalogs


def test_packaged_catalogs_are_complete_and_compiled() -> None:
    root = Path(__file__).resolve().parents[1]
    assert compile_catalogs(root, check=True) == 0
    templates = root / "src/pyracast/regions/templates"
    for template in templates.rglob("*.html"):
        get_template(template.relative_to(templates).as_posix())


def test_oversized_browser_header_cannot_truncate_a_zero_quality() -> None:
    assert browser_language(" " * 8190 + "tr;q=0") == (None, frozenset({"*"}))


def test_admin_date_control_preserves_html_date_format_in_turkish() -> None:
    with translation.override("tr"):
        widget = ProviderSetupForm.base_fields["installation_date"].widget
        assert 'value="2020-01-01"' in widget.render("installation_date", date(2020, 1, 1))


def test_catalog_compilation_detects_stale_artifacts_and_incomplete_entries(tmp_path: Path) -> None:
    source = tmp_path / "src/example/locale/tr/LC_MESSAGES/django.po"
    source.parent.mkdir(parents=True)
    catalog = polib.POFile()
    catalog.metadata = {"Language": "tr", "Content-Type": "text/plain; charset=UTF-8"}
    catalog.append(polib.POEntry(msgid="Saved", msgstr="Kaydedildi"))
    catalog.save(str(source))
    assert compile_catalogs(tmp_path, check=True) == 1
    assert compile_catalogs(tmp_path, check=False) == 0
    compiled = source.with_suffix(".mo").read_bytes()
    assert compile_catalogs(tmp_path, check=True) == 0
    assert compile_catalogs(tmp_path, check=False) == 0
    assert source.with_suffix(".mo").read_bytes() == compiled
    source.with_suffix(".mo").write_bytes(b"stale")
    assert compile_catalogs(tmp_path, check=True) == 1
    catalog.append(polib.POEntry(msgid="Unknown", msgstr=""))
    catalog.save(str(source))
    assert compile_catalogs(tmp_path, check=False) == 1


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
