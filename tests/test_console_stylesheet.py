import re
from pathlib import Path

import pytest

import pyracast

PACKAGE = Path(pyracast.__file__).parent
STATIC = PACKAGE / "regions" / "static" / "pyracast"
TEMPLATES = PACKAGE / "regions" / "templates" / "admin"
COLOUR = re.compile(
    r"#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})(?![\w-])"
    r"|(?<![\w-])(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\(",
    re.IGNORECASE,
)
ROOT = re.compile(r":root \{\n(.*?)\n\}\n", re.DOTALL)
ROOT_SELECTOR = re.compile(r"(?<![\w-]):root\s*\{")
CUSTOM_PROPERTY = re.compile(r"(?<![\w-])(--[\w-]+)\s*:")
COLOUR_ATTRIBUTE = re.compile(
    r"(?<![\w-])(?:style|fill|stroke|color|bgcolor|background|stop-color|flood-color"
    r"""|lighting-color)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
    re.IGNORECASE,
)


def declarations_and_colours(rules: str) -> list[str]:
    return CUSTOM_PROPERTY.findall(rules) + COLOUR.findall(rules)


def stylesheet_problems(stylesheet: str) -> list[str]:
    root = ROOT.match(stylesheet)
    if root is None or len(ROOT_SELECTOR.findall(stylesheet)) != 1:
        return [":root"]
    names = CUSTOM_PROPERTY.findall(root.group(1))
    repeated = sorted({name for name in names if names.count(name) > 1})
    return repeated + declarations_and_colours(stylesheet[root.end() :])


def template_colours(template: str) -> list[str]:
    values = ("".join(groups) for groups in COLOUR_ATTRIBUTE.findall(template))
    return [value for value in values if COLOUR.search(value)]


def root_tokens() -> dict[str, str]:
    stylesheet = (STATIC / "admin-workspace.css").read_text(encoding="utf-8")
    root = ROOT.match(stylesheet)
    assert root is not None
    return dict(re.findall(r"^\s*(--[\w-]+): (.+);$", root.group(1), re.MULTILINE))


def test_console_styles_take_every_colour_from_the_tokens_of_the_stylesheet() -> None:
    stylesheet = (STATIC / "admin-workspace.css").read_text(encoding="utf-8")
    problems = [("admin-workspace.css", problem) for problem in stylesheet_problems(stylesheet)]
    problems += [
        (path.name, problem)
        for path in STATIC.glob("*.css")
        if path.name != "admin-workspace.css"
        for problem in declarations_and_colours(path.read_text(encoding="utf-8"))
    ]
    problems += [
        (path.name, colour)
        for path in TEMPLATES.rglob("*.html")
        for colour in template_colours(path.read_text(encoding="utf-8"))
    ]

    assert problems == []


VALID = (
    ":root {\n    --forest: #09383a;\n    --line: #dfe6df;\n}\n"
    ".rail {\n    background: var(--forest);\n}\n"
)


@pytest.mark.parametrize(
    "stylesheet",
    [
        VALID + ":root {\n    --forest: #000000;\n}\n",
        VALID + ":root{--forest:var(--line)}\n",
        VALID.replace("    --line:", "    --forest: var(--line);\n    --line:"),
        VALID + ".rail {\n    --forest: var(--line);\n}\n",
        VALID + ".rail{--amber :var(--line) !important}\n",
        VALID + ".rail {\n    color: hsl(182 73% 13%);\n}\n",
    ],
    ids=[
        "second root block",
        "second root block without a literal",
        "repeated declaration",
        "redeclaration under another selector",
        "redeclaration in another spacing",
        "colour function in a rule",
    ],
)
def test_a_second_definition_of_a_console_token_is_refused(stylesheet: str) -> None:
    assert stylesheet_problems(VALID) == []
    assert stylesheet_problems(stylesheet) != []


@pytest.mark.parametrize(
    "template",
    [
        "<path fill='#ffb545' d=\"M0 0\"/>",
        '<p style="color: rgb(9 56 58)">x</p>',
        "<td bgcolor=#09383a>x</td>",
        '<stop stop-color="#09383a"/>',
        '<div style="background:hsl(182 73% 13%)">x</div>',
    ],
    ids=[
        "single-quoted attribute",
        "colour function in a style",
        "unquoted attribute",
        "presentation attribute",
        "colour function without spacing",
    ],
)
def test_a_colour_written_in_a_console_template_is_refused(template: str) -> None:
    assert (
        template_colours('<path class="brand-mark-sun" fill="none" stroke="currentColor"/>') == []
    )
    assert template_colours(template) != []


def test_every_property_a_console_stylesheet_reads_is_declared_in_its_root() -> None:
    declared = root_tokens()
    undeclared = sorted(
        (path.name, name)
        for path in STATIC.glob("*.css")
        for name in set(re.findall(r"var\(\s*(--[\w-]+)", path.read_text(encoding="utf-8")))
        if name not in declared
    )

    assert undeclared == []


def test_the_console_tokens_carry_the_brand_kit_colours() -> None:
    tokens = root_tokens()
    rules = (STATIC / "admin-workspace.css").read_text(encoding="utf-8")

    assert {name: tokens[name] for name in ("--forest", "--amber", "--cream")} == {
        "--forest": "#09383a",
        "--amber": "#ffb545",
        "--cream": "#f4f2e8",
    }
    assert ".brand-mark-sun {\n    fill: var(--amber);\n}" in rules
    assert ".brand-mark-bar {\n    fill: var(--cream);\n}" in rules
