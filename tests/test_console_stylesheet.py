import re
from pathlib import Path

import pyracast

PACKAGE = Path(pyracast.__file__).parent
STATIC = PACKAGE / "regions" / "static" / "pyracast"
TEMPLATES = PACKAGE / "regions" / "templates" / "admin"
COLOUR = re.compile(
    r"#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})(?![\w-])"
    r"|(?<![\w-])(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\(",
    re.IGNORECASE,
)
ATTRIBUTE_COLOUR = re.compile(
    r'(?:fill|stroke|style|bgcolor|color)="[^"]*#[0-9a-f]{3,8}', re.IGNORECASE
)


def tokens_and_rules() -> tuple[str, str]:
    stylesheet = (STATIC / "admin-workspace.css").read_text(encoding="utf-8")
    root = re.match(r":root \{\n(.*?)\n\}\n", stylesheet, re.DOTALL)
    assert root is not None
    return root.group(1), stylesheet[root.end() :]


def test_console_styles_take_every_colour_from_the_tokens_of_the_stylesheet() -> None:
    _, rules = tokens_and_rules()
    stylesheets = {"admin-workspace.css": rules} | {
        path.name: path.read_text(encoding="utf-8")
        for path in STATIC.glob("*.css")
        if path.name != "admin-workspace.css"
    }

    literals = [
        (name, literal) for name, text in stylesheets.items() for literal in COLOUR.findall(text)
    ]
    literals += [
        (path.name, literal)
        for path in TEMPLATES.rglob("*.html")
        for literal in ATTRIBUTE_COLOUR.findall(path.read_text(encoding="utf-8"))
    ]

    assert literals == []


def test_the_console_tokens_carry_the_brand_kit_colours() -> None:
    tokens, rules = tokens_and_rules()
    values = dict(re.findall(r"^\s*(--[\w-]+): (.+);$", tokens, re.MULTILINE))

    assert {name: values[name] for name in ("--forest", "--amber", "--cream")} == {
        "--forest": "#09383a",
        "--amber": "#ffb545",
        "--cream": "#f4f2e8",
    }
    assert ".brand-mark-sun {\n    fill: var(--amber);\n}" in rules
    assert ".brand-mark-bar {\n    fill: var(--cream);\n}" in rules
