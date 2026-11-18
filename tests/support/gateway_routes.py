import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

ROUTES = Path(__file__).resolve().parents[2] / "deploy" / "routes.yml"
SITE = "pyracast.example"

Route = Callable[[str, str], bool]
Token = str | tuple[str, str]
RULE_TOKEN = re.compile(r"\s*(?:(\|\||&&|!|\(|\))|(Host|Path|PathPrefix)\(`([^`]*)`\))")
MATCHERS: dict[str, Callable[[str], Route]] = {
    "Host": lambda value: lambda host, _path: host == value,
    "Path": lambda value: lambda _host, path: path == value,
    "PathPrefix": lambda value: lambda _host, path: path.startswith(value),
}


def either(left: Route, right: Route) -> Route:
    return lambda host, path: left(host, path) or right(host, path)


def both(left: Route, right: Route) -> Route:
    return lambda host, path: left(host, path) and right(host, path)


def negated(route: Route) -> Route:
    return lambda host, path: not route(host, path)


def rule_disjunction(tokens: list[Token]) -> tuple[Route, list[Token]]:
    route, tokens = rule_conjunction(tokens)
    while tokens[:1] == ["||"]:
        right, tokens = rule_conjunction(tokens[1:])
        route = either(route, right)
    return route, tokens


def rule_conjunction(tokens: list[Token]) -> tuple[Route, list[Token]]:
    route, tokens = rule_term(tokens)
    while tokens[:1] == ["&&"]:
        right, tokens = rule_term(tokens[1:])
        route = both(route, right)
    return route, tokens


def rule_term(tokens: list[Token]) -> tuple[Route, list[Token]]:
    if not tokens:
        raise ValueError("Incomplete router rule.")
    head, rest = tokens[0], tokens[1:]
    if head == "!":
        route, rest = rule_term(rest)
        return negated(route), rest
    if head == "(":
        route, rest = rule_disjunction(rest)
        if rest[:1] != [")"]:
            raise ValueError("Unbalanced router rule.")
        return route, rest[1:]
    if isinstance(head, tuple):
        return MATCHERS[head[0]](head[1]), rest
    raise ValueError(f"Unexpected router rule token: {head}")


def router_rule(rule: str) -> Route:
    tokens: list[Token] = []
    rule = rule.strip()
    position = 0
    while position < len(rule):
        match = RULE_TOKEN.match(rule, position)
        if match is None:
            raise ValueError(f"Unsupported router rule syntax: {rule[position:]}")
        tokens.append(match[1] or (match[2], match[3]))
        position = match.end()
    route, rest = rule_disjunction(tokens)
    if rest:
        raise ValueError("Unbalanced router rule.")
    return route


def routes() -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(ROUTES.read_text(encoding="utf-8"))
    return document


def routers() -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = routes()["http"]["routers"]
    return found


def middleware(name: str) -> dict[str, Any]:
    found: dict[str, Any] = routes()["http"]["middlewares"][name]
    return found


def router_route(name: str) -> Route:
    rule: str = routers()[name]["rule"]
    return router_rule(rule.replace('{{ env "SITE_ADDRESS" }}', SITE))


def backend_routers() -> list[str]:
    return [name for name, router in routers().items() if router["service"] == "backend"]


def routed_router(path: str) -> str | None:
    matching = [name for name in routers() if router_route(name)(SITE, path)]
    if not matching:
        return None
    return max(matching, key=lambda name: int(routers()[name]["priority"]))


def backend_route() -> Route:
    routed = [router_route(name) for name in backend_routers()]
    return lambda host, path: any(route(host, path) for route in routed)
