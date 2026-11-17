from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from django.db import connections
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver

from pyracast.regions.operations import submit_region
from tests.support.budgets import assert_scale_invariant, query_budget
from tests.support.domain import NOW, System
from tests.support.plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])

DAY = NOW.date().isoformat()
PLANT = "api/tenants/<int:tenant_id>/regions/<int:region_id>/plants/<int:plant_id>/"
REGION = "api/tenants/<int:tenant_id>/regions/<int:region_id>/"

POLLED = {
    "api/tenants/": ("", 18),
    "api/tenants/<int:tenant_id>/sites/": ("", 19),
    "api/tenants/<int:tenant_id>/change-requests/": ("scope=pending&plant_id={plant}", 19),
    f"{REGION}forecast/": (f"date={DAY}&data_source=forecast", 24),
    f"{REGION}collection-schedule/": ("", 22),
    f"{REGION}plants/": ("", 19),
    f"{PLANT}forecast/": (f"date={DAY}&resolution_minutes=60&data_source=forecast", 29),
    f"{PLANT}production/": (f"start_date={DAY}&end_date={DAY}&native_intervals=true", 31),
    f"{PLANT}calibrations/": ("", 19),
    f"{PLANT}calibration/automation/": ("", 19),
}
NOT_POLLED = frozenset(
    {
        "api/tenants/<int:tenant_id>/change-requests/<int:request_id>/",
        "api/tenants/<int:tenant_id>/members/",
        "api/tenants/<int:tenant_id>/invitations/",
        "api/tenants/<int:tenant_id>/trash/regions/",
        f"{REGION}location-change-requests/",
        f"{REGION}reminders/",
        f"{PLANT}outages/",
        f"{PLANT}production-source/",
        f"{PLANT}performance/",
        f"{PLANT}calibrations/<int:calibration_id>/",
    }
)


def _routes(patterns: list[URLPattern | URLResolver], prefix: str = "") -> Iterator[str]:
    for pattern in patterns:
        route = prefix + str(pattern.pattern)
        if isinstance(pattern, URLResolver):
            yield from _routes(pattern.url_patterns, route)
        elif hasattr(getattr(pattern.callback, "view_class", None), "get"):
            yield route


@pytest.mark.parametrize("alias", ["default", "auth_cache"])
def test_statements_through_every_connection_count_against_the_budget(alias: str) -> None:
    with (
        pytest.raises(AssertionError, match="1 queries exceed the budget of 0"),
        query_budget(0),
        connections[alias].cursor() as cursor,
    ):
        cursor.execute("SELECT 1")


def test_every_tenant_read_route_is_classified_as_polled_or_not() -> None:
    reads = {route for route in _routes(get_resolver().url_patterns) if "tenants/" in route}

    assert reads == set(POLLED) | NOT_POLLED
    assert not set(POLLED) & NOT_POLLED


@dataclass(frozen=True)
class Workspace:
    client: Client
    system: System
    plant_id: int

    def url(self, route: str, query: str) -> str:
        path = (
            route.replace("<int:tenant_id>", str(self.system.tenant.pk))
            .replace("<int:region_id>", str(self.system.region.pk))
            .replace("<int:plant_id>", str(self.plant_id))
        )
        return f"/{path}?{query.format(plant=self.plant_id)}"


@pytest.fixture
def workspace(monkeypatch: pytest.MonkeyPatch) -> Workspace:
    system, plant = make_plant(monkeypatch)
    client = Client()
    client.force_login(system.member)
    return Workspace(client, system, plant.pk)


@pytest.mark.parametrize("route", sorted(POLLED))
def test_polled_reads_stay_within_their_statement_budget(workspace: Workspace, route: str) -> None:
    query, ceiling = POLLED[route]
    url = workspace.url(route, query)
    workspace.client.get(url)

    with query_budget(ceiling) as captured:
        response = workspace.client.get(url)

    assert response.status_code == 200, response.content
    assert not [statement for statement in captured if "FOR UPDATE" in statement]


def test_site_list_reads_do_not_grow_with_the_number_of_sites(workspace: Workspace) -> None:
    system = workspace.system
    added = 0

    def seed(count: int) -> None:
        nonlocal added
        for _ in range(count):
            added += 1
            submit_region(
                system.member,
                system.tenant.pk,
                name=f"Site {added}",
                latitude=41 + added / 10,
                longitude=30,
                timezone="Europe/Istanbul",
            )

    url = workspace.url("api/tenants/<int:tenant_id>/sites/", "")
    assert_scale_invariant(seed, lambda: workspace.client.get(url), small=1, large=4)
