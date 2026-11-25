import re
from html.parser import HTMLParser
from typing import Any

import pytest
from django.template.loader import render_to_string

URL = "https://testserver/app/w/1/"
MAILS: dict[str, dict[str, Any]] = {
    "account/email/email_confirmation_message.html": {"code": "012345", "code_timeout_minutes": 10},
    "account/email/login_code_message.html": {"code": "012345", "code_timeout_minutes": 10},
    "account/email/password_reset_code_message.html": {
        "code": "012345",
        "code_timeout_minutes": 10,
    },
    "mail/notice.html": {
        "title": "Password changed",
        "sentence": "Your password was changed.",
        "security": True,
        "secure_url": URL,
        "device": "Firefox",
        "ip": "192.0.2.1",
    },
    "mail/test_message.html": {},
    "mail/workspace_invitation.html": {
        "workspace": "Tenant",
        "email": "guest@example.com",
        "action_url": URL,
    },
    "notifications/workspace_event.html": {
        "title": "Plant removed",
        "sentence": "A plant was removed.",
        "reason": "Closed",
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


class Elements(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str], bool]] = []
        self.inside = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        self.elements.append((tag, values, self.inside))
        self.inside = self.inside or "mail-content" in values.get("class", "").split()


def dark_rules(html: str) -> list[tuple[list[str], dict[str, str]]]:
    block = html[html.index("@media (prefers-color-scheme: dark) {") :].split("\n    }\n")[0]
    return [
        (
            [selector.strip() for selector in selectors.split(",")],
            dict(
                (name.strip(), value.replace("!important", "").strip())
                for name, value in (part.split(":") for part in body.split(";") if part.strip())
            ),
        )
        for selectors, body in re.findall(r"\n\s*([^{}@]+?)\s*\{([^{}]*)\}", block)
    ]


def dark(rules: list[tuple[list[str], dict[str, str]]], name: str, prop: str) -> str | None:
    for selectors, body in rules:
        if prop in body and any(f".{name}" in selector.split() for selector in selectors):
            return body[prop]
    return None


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


@pytest.mark.parametrize("template", list(MAILS))
def test_in_dark_mode_every_element_with_a_background_has_its_own_readable_pair(
    template: str,
) -> None:
    html = render_to_string(template, MAILS[template])
    rules = dark_rules(html)
    parser = Elements()
    parser.feed(html)
    backed = [
        (attributes, inside)
        for _, attributes, inside in parser.elements
        if "background-color" in attributes.get("style", "") or "bgcolor" in attributes
    ]
    assert backed
    for attributes, inside in backed:
        classes = attributes.get("class", "").split()
        backgrounds = [dark(rules, name, "background-color") for name in classes]
        background = next((value for value in backgrounds if value), None)
        assert background, attributes
        if inside:
            colour = next(
                (value for name in classes if (value := dark(rules, name, "color"))), None
            )
            assert colour, attributes
            assert contrast(background, colour) >= 4.5, attributes


def test_the_dark_rules_give_the_code_and_the_notice_their_own_pairs() -> None:
    code = render_to_string(
        "account/email/login_code_message.html", {"code": "012345", "code_timeout_minutes": 10}
    )
    rules = dark_rules(code)
    assert '<p class="mail-code"' in code
    assert (dark(rules, "mail-code", "background-color"), dark(rules, "mail-code", "color")) == (
        "#0f1a1f",
        "#e6edf0",
    )
    assert dark(rules, "mail-code", "border-color") == "#2b3f49"
    forecast = render_to_string("notifications/forecast.html", MAILS["notifications/forecast.html"])
    assert '<td class="mail-notice"' in forecast
    assert (
        dark(rules, "mail-notice", "background-color"),
        dark(rules, "mail-notice", "color"),
    ) == ("#fdf6e7", "#172b36")


@pytest.mark.parametrize("template", list(MAILS))
def test_every_heading_paragraph_and_span_names_its_font(template: str) -> None:
    parser = Elements()
    parser.feed(render_to_string(template, MAILS[template]))
    texts = [attributes for tag, attributes, _ in parser.elements if tag in TEXT]
    assert texts
    for attributes in texts:
        assert "font-family:" in attributes.get("style", ""), attributes


def test_the_whole_button_is_the_link_and_at_least_44_pixels_high() -> None:
    parser = Elements()
    parser.feed(
        render_to_string("mail/workspace_invitation.html", MAILS["mail/workspace_invitation.html"])
    )
    index, cell = next(
        (index, attributes)
        for index, (tag, attributes, _) in enumerate(parser.elements)
        if "mail-button" in attributes.get("class", "")
    )
    tag, link, _ = parser.elements[index + 1]
    assert tag == "a"
    cell_style = dict(part.split(":", 1) for part in cell["style"].split(";") if part.strip())
    link_style = dict(part.split(":", 1) for part in link["style"].split(";") if part.strip())
    assert "padding" not in cell_style
    assert cell_style["mso-padding-alt"] == "12px 22px"
    assert cell["bgcolor"] == cell_style["background-color"] == "#09383a"
    assert link_style["display"] == "block"
    assert link_style["padding"] == "12px 22px"
    vertical = int(link_style["padding"].split()[0].removesuffix("px"))
    assert 2 * vertical + int(link_style["line-height"].removesuffix("px")) >= 44
