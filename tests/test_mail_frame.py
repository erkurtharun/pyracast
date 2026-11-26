import re
from collections.abc import Iterator
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from django.template.loader import render_to_string

ROOT = Path(__file__).resolve().parents[1]
URL = "https://testserver/app/w/1/"
MAILS: dict[str, dict[str, Any]] = {
    "account/email/email_confirmation_message.html": {"code": "012345", "code_timeout_minutes": 10},
    "account/email/login_code_message.html": {"code": "012345", "code_timeout_minutes": 10},
    "account/email/password_reset_code_message.html": {
        "code": "012345",
        "code_timeout_minutes": 10,
    },
    "mail/notice.html": {
        "title": "Your password was changed",
        "security": True,
        "time": "15 Nov 2026, 15:00 (UTC+3)",
        "secure_url": URL,
        "device": "Firefox",
        "ip": "192.0.2.1",
    },
    "mail/test_message.html": {},
    "mail/workspace_invitation.html": {
        "workspace": "Tenant",
        "email": "guest@example.com",
        "action_url": URL,
        "role": "Member",
        "expires": "2 Dec 2026, 15:00 (UTC+3)",
    },
    "notifications/workspace_event.html": {
        "title": "Plant removed",
        "sentence": "A plant was removed.",
        "details": (("Reason", "Closed"),),
        "next_step": "Restore it.",
        "action_url": URL,
        "action_label": "Open workspace",
        "footer": "Footer",
    },
    "notifications/forecast.html": {
        "subject": "Plant: forecast for Thu 26 Nov",
        "kind": "Generation forecast",
        "plant": "Plant",
        "days": [
            {"word": "Tomorrow", "date": "Thu 26 Nov 2026", "value": "1.00", "notes": ["Note"]},
            {"word": "Today", "date": "Wed 25 Nov 2026", "value": "", "gap": "Gap", "notes": []},
        ],
        "unit": "MWh",
        "stale": "Newer forecast data was not available.",
        "data_line": "Forecast data: 25 Nov 2026, 16:02 (UTC+3)",
        "attachment_line": "Hourly values are in the attached Excel file.",
        "action_label": "Open forecast in Pyracast",
        "url": URL,
        "footer": "Footer",
    },
}
TEXT = {"h1", "p", "span"}
VOID = {"br", "hr", "img", "input", "link", "meta"}
PAINTED = ("color", "background-color")
HIDING = {"display": "none", "visibility": "hidden", "max-height": "0", "mso-hide": "all"}
FILLERS = frozenset(map(chr, (0x00AD, 0x034F, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF)))
TEXT_LINK = {"color": "#09383a", "font-size": "16px", "text-decoration": "underline"}


def declarations(body: str) -> dict[str, str]:
    return {
        name.strip(): value.strip()
        for name, value in (part.split(":", 1) for part in body.split(";") if part.strip())
    }


@dataclass(eq=False)
class Node:
    tag: str
    attributes: dict[str, str]
    parent: Node | None
    text: bool = False

    @property
    def classes(self) -> set[str]:
        return set(self.attributes.get("class", "").split())

    @property
    def style(self) -> dict[str, str]:
        return declarations(self.attributes.get("style", ""))

    def lineage(self) -> Iterator[Node]:
        node: Node | None = self
        while node is not None:
            yield node
            node = node.parent


class Tree(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.nodes: list[Node] = []
        self.open: list[Node] = []
        self.feed(html)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(
            tag, {name: value or "" for name, value in attrs}, self.open[-1] if self.open else None
        )
        self.nodes.append(node)
        if tag not in VOID:
            self.open.append(node)

    def handle_endtag(self, tag: str) -> None:
        names = [node.tag for node in self.open]
        if tag in names:
            del self.open[len(names) - 1 - names[::-1].index(tag) :]

    def handle_data(self, data: str) -> None:
        if data.strip() and self.open:
            self.open[-1].text = True


@dataclass(frozen=True)
class Rule:
    selector: tuple[tuple[str, ...], ...]
    prop: str
    value: str
    important: bool
    order: int

    @property
    def rank(self) -> tuple[int, int, int]:
        parts = [part for compound in self.selector for part in compound]
        classes = sum(part.startswith(".") for part in parts)
        return classes, sum(part[0].isalpha() for part in parts), self.order

    def matches(self, node: Node) -> bool:
        *outer, last = self.selector
        if not fits(last, node):
            return False
        # One shared iterator: each outer compound must match above the previous one's match.
        ancestors = node.lineage()
        next(ancestors)
        return all(
            any(fits(compound, ancestor) for ancestor in ancestors) for compound in outer[::-1]
        )


def fits(compound: tuple[str, ...], node: Node) -> bool:
    return all(
        part == "*" or part == node.tag or (part.startswith(".") and part[1:] in node.classes)
        for part in compound
    )


def dark_rules(html: str) -> list[Rule]:
    block = html[html.index("@media (prefers-color-scheme: dark) {") :].split("\n    }\n")[0]
    rules: list[Rule] = []
    for selectors, body in re.findall(r"\n\s*([^{}@]+?)\s*\{([^{}]*)\}", block):
        for selector in selectors.split(","):
            compounds = tuple(
                tuple(re.findall(r"\*|\.[\w-]+|[a-z][\w-]*", compound))
                for compound in selector.split()
            )
            for prop, value in declarations(body).items():
                rules.append(
                    Rule(
                        compounds,
                        prop,
                        value.removesuffix("!important").strip(),
                        value.endswith("!important"),
                        len(rules),
                    )
                )
    return rules


def declared(node: Node, prop: str, rules: list[Rule]) -> tuple[str, str]:
    """The value the node itself shows for the property and where it comes from, by the
    cascade: important rules, then the inline style, then other rules, then `bgcolor`."""
    matched = sorted(
        (rule for rule in rules if rule.prop == prop and rule.matches(node)),
        key=lambda rule: rule.rank,
    )
    important = [rule for rule in matched if rule.important]
    if important:
        return important[-1].value, "rule"
    if prop in node.style:
        return node.style[prop], "inline"
    if matched:
        return matched[-1].value, "rule"
    if prop == "background-color" and "bgcolor" in node.attributes:
        return node.attributes["bgcolor"], "attribute"
    return "", ""


def shown(node: Node, prop: str, rules: list[Rule]) -> str:
    """Colour is inherited; the background is that of the nearest painted ancestor."""
    return next(value for each in node.lineage() if (value := declared(each, prop, rules)[0]))


def in_head(node: Node) -> bool:
    return any(each.tag == "head" for each in node.lineage())


def luminance(colour: str) -> float:
    channels = [int(colour[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    red, green, blue = (
        value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    )
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(first: str, second: str) -> float:
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def frame_templates() -> set[str]:
    return {
        path.relative_to(
            next(folder for folder in path.parents if folder.name == "templates")
        ).as_posix()
        for path in (ROOT / "src").glob("**/templates/**/*.html")
        if '{% extends "mail/base.html" %}' in path.read_text(encoding="utf-8")
    }


def test_every_mail_in_the_frame_is_checked() -> None:
    assert set(MAILS) == frame_templates()


@pytest.mark.parametrize("template", list(MAILS))
def test_in_dark_mode_every_dark_rule_wins_over_the_inline_colours(template: str) -> None:
    html = render_to_string(template, MAILS[template])
    rules = dark_rules(html)
    nodes = Tree(html).nodes
    assert any("background-color" in node.style for node in nodes)
    for node in nodes:
        for prop in PAINTED:
            ruled = prop in node.style and any(
                rule.prop == prop and rule.matches(node) for rule in rules
            )
            backed = prop == "background-color" and (
                prop in node.style or "bgcolor" in node.attributes
            )
            if ruled or backed:
                assert declared(node, prop, rules)[1] == "rule", (prop, node.attributes)


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("template", list(MAILS))
def test_every_visible_text_is_readable_on_its_background(template: str, dark: bool) -> None:
    html = render_to_string(template, MAILS[template])
    rules = dark_rules(html) if dark else []
    texts = [node for node in Tree(html).nodes if node.text and not in_head(node)]
    assert texts
    for node in texts:
        pair = shown(node, "background-color", rules), shown(node, "color", rules)
        assert contrast(*pair) >= 4.5, (pair, node.tag, node.attributes)


def test_the_dark_rules_reach_the_code_the_notice_text_and_the_button_link() -> None:
    code = render_to_string(
        "account/email/login_code_message.html", MAILS["account/email/login_code_message.html"]
    )
    forecast = render_to_string("notifications/forecast.html", MAILS["notifications/forecast.html"])
    invitation = render_to_string(
        "mail/workspace_invitation.html", MAILS["mail/workspace_invitation.html"]
    )
    expected = {
        (code, "p", "mail-code"): ("#0f1a1f", "#e6edf0"),
        (forecast, "td", "mail-notice"): ("#fdf6e7", "#172b36"),
        (forecast, "strong", "mail-notice"): ("#fdf6e7", "#172b36"),
        (invitation, "a", "mail-button"): ("#d7ebe6", "#09383a"),
    }
    for (html, tag, name), pair in expected.items():
        rules = dark_rules(html)
        (node,) = [
            node
            for node in Tree(html).nodes
            if node.tag == tag
            and node.text
            and any(name in each.classes for each in node.lineage())
        ]
        assert (shown(node, "background-color", rules), shown(node, "color", rules)) == pair, tag


@pytest.mark.parametrize("template", list(MAILS))
def test_no_mail_carries_hidden_text_or_invisible_filler(template: str) -> None:
    html = render_to_string(template, MAILS[template])
    hiding = [
        node.attributes
        for node in Tree(html).nodes
        if any(node.style.get(prop) == value for prop, value in HIDING.items())
    ]
    assert hiding == []
    assert FILLERS.isdisjoint(unescape(html))


@pytest.mark.parametrize("template", list(MAILS))
def test_every_mail_links_at_most_twice_and_only_into_the_application(template: str) -> None:
    nodes = Tree(render_to_string(template, MAILS[template])).nodes
    hrefs = [node.attributes["href"] for node in nodes if node.tag == "a"]
    assert len(hrefs) <= 2
    assert all(href.startswith("https://testserver/app/") for href in hrefs), hrefs


@pytest.mark.parametrize(
    "template", ["notifications/forecast.html", "notifications/workspace_event.html"]
)
def test_a_notification_mail_links_with_one_underlined_text_link(template: str) -> None:
    nodes = Tree(render_to_string(template, MAILS[template])).nodes
    (link,) = [node for node in nodes if node.tag == "a"]
    assert not any("mail-button" in node.classes for node in link.lineage())
    assert link.attributes["href"] == URL
    assert {prop: link.style[prop] for prop in TEXT_LINK} == TEXT_LINK


@pytest.mark.parametrize("template", list(MAILS))
def test_every_heading_paragraph_and_span_names_its_font(template: str) -> None:
    texts = [
        node for node in Tree(render_to_string(template, MAILS[template])).nodes if node.tag in TEXT
    ]
    assert texts
    for node in texts:
        assert "font-family" in node.style, node.attributes


def test_the_whole_button_is_the_link_and_at_least_44_pixels_high() -> None:
    nodes = Tree(
        render_to_string("mail/workspace_invitation.html", MAILS["mail/workspace_invitation.html"])
    ).nodes
    index, cell = next(
        (index, node) for index, node in enumerate(nodes) if "mail-button" in node.classes
    )
    link = nodes[index + 1]
    assert link.tag == "a"
    assert link.parent is cell
    assert "padding" not in cell.style
    assert cell.style["mso-padding-alt"] == "12px 22px"
    assert cell.attributes["bgcolor"] == cell.style["background-color"] == "#09383a"
    assert link.style["display"] == "block"
    assert link.style["padding"] == "12px 22px"
    vertical = int(link.style["padding"].split()[0].removesuffix("px"))
    assert 2 * vertical + int(link.style["line-height"].removesuffix("px")) >= 44
