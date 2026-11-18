import argparse
import re
from pathlib import Path

import polib

_PLACEHOLDERS = re.compile(r"(?<!%)%\(([^)]+)\)")


def compile_catalogs(root: Path, *, check: bool) -> int:
    for source in sorted(root.glob("src/**/locale/*/LC_MESSAGES/*.po")):
        catalog = polib.pofile(str(source), check_for_duplicates=True)
        if catalog.untranslated_entries() or catalog.fuzzy_entries():
            print(f"Incomplete translation catalog: {source}")
            return 1
        for entry in catalog:
            expected = set(_PLACEHOLDERS.findall(entry.msgid + entry.msgid_plural))
            translations = entry.msgstr_plural.values() if entry.msgid_plural else [entry.msgstr]
            if any(set(_PLACEHOLDERS.findall(text)) != expected for text in translations):
                print(f"Translation placeholders differ: {source}: {entry.msgid}")
                return 1
        compiled = catalog.to_binary()
        if not check:
            source.with_suffix(".mo").write_bytes(compiled)
    if not check:
        for target in root.glob("src/**/locale/*/LC_MESSAGES/*.mo"):
            if not target.with_suffix(".po").exists():
                target.unlink()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    return compile_catalogs(Path(__file__).resolve().parents[1], check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
