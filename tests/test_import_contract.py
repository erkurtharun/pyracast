import ast
from collections.abc import Iterator
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "pyracast"

# Lower layers never import higher ones; the allowlists below only shrink.
LAYERS: tuple[tuple[str, ...], ...] = (
    (
        "collection_window",
        "evaluation",
        "event_details",
        "intervals",
        "irradiance",
        "languages",
        "local_time",
        "provider_retry",
        "shutdown",
        "solar",
        "weather_data",
    ),
    ("background", "db", "encryption", "environment", "errors", "health", "schema_objects"),
    ("epias", "mail", "maintenance", "outbound", "providers", "weather"),
    ("accounts",),
    ("regions",),
    ("connections",),
    ("calculation", "forecasts"),
    ("plants",),
    ("incidents", "metering"),
    ("change_requests", "notifications"),
    ("trash",),
    ("orchestration",),
    ("http",),
    ("platform_admin",),
    ("project",),
)
PURE = LAYERS[0]
FRAMEWORKS = ("django", "rest_framework", "dramatiq", "httpx", "allauth")
HTTP_MODULES = (
    "api",
    "views",
    "serializers",
    "urls",
    "auth_urls",
    "middleware",
    "headless",
    "proxy",
)
PROJECT_MODULES = {"settings", "urls", "wsgi", "cli", "production"}
ORCHESTRATION_MODULES = {
    "forecasts.monitoring",
    "forecasts.management.commands.check_delivery_health",
    "forecasts.management.commands.dispatch",
    "forecasts.management.commands.worker",
}

# Each entry is an import against the layer order that is not resolved yet, with its reason.
BACK_EDGES: dict[tuple[str, str], str] = {
    ("accounts.account_mail", "notifications.models"): "account mail goes through the outbox",
    ("accounts.adapters", "notifications.intents"): "account mail goes through the outbox",
    ("accounts.adapters", "notifications.models"): "account mail goes through the outbox",
    ("accounts.invitations", "notifications.intents"): "account mail goes through the outbox",
    ("accounts.invitations", "notifications.models"): "account mail goes through the outbox",
    (
        "accounts.operations",
        "notifications.recipients",
    ): "member removal and administrator change call notifications and plants explicitly",
    (
        "accounts.operations",
        "plants.automation",
    ): "member removal and administrator change call notifications and plants explicitly",
    ("accounts.session_lock", "api.errors"): "the busy-session answer reuses the API error body",
    (
        "accounts.stored_secrets",
        "connections.contracts",
    ): "secret re-encryption walks provider credentials",
    (
        "accounts.stored_secrets",
        "connections.models",
    ): "secret re-encryption walks provider credentials",
    (
        "accounts.stored_secrets",
        "connections.secrets",
    ): "secret re-encryption walks provider credentials",
    (
        "connections.contracts",
        "forecasts.models",
    ): "provider data carries the snapshot weather vocabulary",
    ("forecasts.acquisition", "plants.forecast_sources"): "snapshot replay lives in plants",
    (
        "forecasts.historical_import",
        "plants.preparation",
    ): "an import prepares calibrations of its plants",
    ("forecasts.historical_upload", "plants.models"): "an upload previews the plants it feeds",
    ("forecasts.history", "plants.forecast_sources"): "snapshot replay lives in plants",
    ("forecasts.intraday", "plants.forecast_sources"): "snapshot replay lives in plants",
    (
        "forecasts.jobs",
        "plants.forecast_archive",
    ): "publication archives plant issuances and requests calibration",
    (
        "forecasts.jobs",
        "plants.models",
    ): "publication archives plant issuances and requests calibration",
    (
        "forecasts.jobs",
        "plants.preparation",
    ): "publication archives plant issuances and requests calibration",
    (
        "forecasts.snapshot_restore",
        "plants.preparation",
    ): "a restore prepares calibrations of its plants",
    ("plants.incident_forecasts", "incidents.evidence"): "plant analytics read incidents",
    ("plants.incident_policy", "incidents.evidence"): "plant analytics read incidents",
    ("plants.measurements", "incidents.evidence"): "plant analytics read metering and incidents",
    ("plants.measurements", "metering.models"): "plant analytics read metering and incidents",
    ("plants.operations", "metering.models"): "plant configuration schedules metering",
    ("plants.operations", "metering.scheduler"): "plant configuration schedules metering",
    ("plants.preparation", "metering.models"): "plant analytics read metering",
    ("plants.production", "metering.models"): "the production read returns serialized rows",
    ("plants.production", "plants.serializers"): "the production read returns serialized rows",
    ("providers", "connections.contracts"): "adapters take the binding row",
    ("providers", "connections.models"): "adapters take the binding row",
    ("providers", "connections.secrets"): "adapters take the binding row",
    (
        "regions.operations",
        "connections.models",
    ): "binding configuration writes the provider binding of a region",
}

# Each entry is a function-level pyracast import that breaks a cycle, with its reason.
FUNCTION_IMPORTS: dict[tuple[str, str], str] = {
    (
        "accounts.operations",
        "notifications.recipients",
    ): "member removal and administrator change call notifications and plants explicitly",
    (
        "accounts.operations",
        "plants.automation",
    ): "member removal and administrator change call notifications and plants explicitly",
    ("forecasts.actuals", "forecasts.history"): "breaks an import cycle",
    (
        "forecasts.historical_import",
        "plants.preparation",
    ): "an import prepares calibrations of its plants",
    (
        "forecasts.jobs",
        "plants.forecast_archive",
    ): "publication archives plant issuances and requests calibration",
    (
        "forecasts.jobs",
        "plants.preparation",
    ): "publication archives plant issuances and requests calibration",
    ("metering.jobs", "plants.preparation"): "breaks an import cycle",
    ("metering.operations", "metering.scheduler"): "breaks an import cycle",
    ("plants.current_forecast", "plants.automatic_forecast"): "breaks an import cycle",
    ("plants.forecast_sources", "forecasts.intraday"): "breaks an import cycle",
    ("plants.forecasting", "plants.forecast_archive"): "breaks an import cycle",
    ("plants.forecasting", "plants.incident_policy"): "breaks an import cycle",
    ("plants.history", "plants.incident_forecasts"): "breaks an import cycle",
    ("plants.history", "plants.production_diagnostics"): "breaks an import cycle",
    ("plants.operations", "metering.models"): "plant configuration schedules metering",
    ("plants.operations", "metering.scheduler"): "plant configuration schedules metering",
    ("plants.preparation", "plants.automation"): "plant analytics read metering",
    ("trash", "change_requests.models"): "a purge removes change requests",
}


def _layer(module: str) -> str:
    parts = module.split(".")
    if module in PROJECT_MODULES:
        return "project"
    if parts[0] == "platform_admin":
        return "platform_admin"
    if module in ORCHESTRATION_MODULES or ".management.commands." in module:
        return "orchestration"
    if parts[0] == "api" or parts[-1] in HTTP_MODULES or parts[-1].endswith(("_api", "_views")):
        return "http"
    return parts[0]


def _rank(module: str) -> int:
    return next(index for index, names in enumerate(LAYERS) if _layer(module) in names)


def _modules() -> Iterator[tuple[str, ast.Module]]:
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE).with_suffix("")
        if "migrations" in relative.parts or relative.name == "apps":
            continue
        parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
        if parts:
            yield ".".join(parts), ast.parse(path.read_text(encoding="utf-8"))


def _names(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    # Each name may be a submodule; _internal trims it to the longest module that exists.
    return [f"{node.module}.{alias.name}" for alias in node.names]


def _imports(tree: ast.Module) -> Iterator[tuple[str, bool]]:
    def walk(node: ast.AST, nested: bool) -> Iterator[tuple[str, bool]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and ast.unparse(child.test) == "TYPE_CHECKING":
                continue
            if isinstance(child, ast.Import | ast.ImportFrom):
                yield from ((name, nested) for name in _names(child))
            yield from walk(child, nested or isinstance(child, ast.FunctionDef))

    return walk(tree, False)


def _internal(tree: ast.Module, modules: set[str]) -> Iterator[tuple[str, bool]]:
    for name, nested in _imports(tree):
        if name.startswith("pyracast."):
            parts = name.removeprefix("pyracast.").split(".")
            while len(parts) > 1 and ".".join(parts) not in modules:
                parts.pop()
            yield ".".join(parts), nested


def back_edges() -> set[tuple[str, str]]:
    modules = dict(_modules())
    return {
        (module, imported)
        for module, tree in modules.items()
        for imported, _nested in _internal(tree, set(modules))
        if _rank(module) < _rank(imported)
    }


def function_imports() -> set[tuple[str, str]]:
    modules = dict(_modules())
    return {
        (module, imported)
        for module, tree in modules.items()
        for imported, nested in _internal(tree, set(modules))
        if nested
    }


def framework_imports_in_pure_modules() -> set[tuple[str, str]]:
    return {
        (module, name)
        for module, tree in _modules()
        if _layer(module) in PURE
        for name, _nested in _imports(tree)
        if name.split(".")[0] in FRAMEWORKS
    }


def test_a_module_imported_from_its_package_keeps_its_own_layer() -> None:
    tree = ast.parse("from pyracast.forecasts import views\nfrom pyracast import errors\n")
    imported = {name for name, _nested in _internal(tree, {"forecasts", "forecasts.views"})}
    assert imported == {"forecasts.views", "errors"}


def test_every_module_belongs_to_a_layer() -> None:
    known = {name for names in LAYERS for name in names}
    assert {_layer(module) for module, _tree in _modules()} <= known


def test_imports_follow_the_layer_order_except_listed_back_edges() -> None:
    assert back_edges() == set(BACK_EDGES)


def test_function_level_imports_are_only_the_listed_cycle_breakers() -> None:
    assert function_imports() == set(FUNCTION_IMPORTS)


def test_pure_modules_import_no_framework() -> None:
    assert framework_imports_in_pure_modules() == set()
