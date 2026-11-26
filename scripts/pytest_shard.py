import json
import math
import os
import platform
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from statistics import fmean

import pytest

DURATIONS = Path(__file__).resolve().parents[1] / "tests" / "shard-durations.json"


def parse_shard(value: str) -> tuple[int, int]:
    index, count = (int(part) for part in value.split("/"))
    if not 1 <= index <= count or count < 2:
        raise ValueError("A shard must be INDEX/COUNT with 1 <= INDEX <= COUNT and COUNT >= 2")
    return index, count


def recorded_durations(system: str) -> dict[str, float]:
    recorded: object = json.loads(DURATIONS.read_text(encoding="utf-8"))
    if not isinstance(recorded, dict):
        raise ValueError(f"Invalid test durations: {DURATIONS}")
    figures: object = recorded.get(system, {})
    if not isinstance(figures, dict) or not all(
        isinstance(module, str) and type(seconds) in {int, float} and 0 <= seconds < math.inf
        for module, seconds in figures.items()
    ):
        raise ValueError(f"Invalid {system} test durations: {DURATIONS}")
    return {module: float(seconds) for module, seconds in figures.items()}


def balanced_shards(
    modules: Iterable[str], durations: Mapping[str, float], count: int
) -> list[list[str]]:
    unknown = fmean(durations.values()) if durations else 1.0
    weights = {module: durations.get(module, unknown) for module in set(modules)}
    shards: list[list[str]] = [[] for _ in range(count)]
    loads = [0.0] * count
    for module in sorted(weights, key=lambda name: (-weights[name], name)):
        target = min(range(count), key=lambda slot: (loads[slot], len(shards[slot]), slot))
        shards[target].append(module)
        loads[target] += weights[module]
    return [sorted(shard) for shard in shards]


def selected_nodes(
    nodes: list[str], index: int, count: int, durations: Mapping[str, float]
) -> list[str]:
    groups: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        groups[node.split("::", 1)[0]].append(node)
    modules = balanced_shards(groups, durations, count)[index - 1]
    return sorted(node for module in modules for node in groups[module])


def run_identity() -> dict[str, str]:
    # Shards of different attempts of one run combine: a partial re-run repeats only failed shards.
    return {
        "sha": os.environ.get("GITHUB_SHA", "local"),
        "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
        "platform": platform.system(),
        "python": platform.python_version(),
    }


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    value = os.environ.get("PYRACAST_TEST_SHARD")
    if value is None:
        return
    index, count = parse_shard(value)
    nodes = sorted(item.nodeid for item in items)
    selected = selected_nodes(nodes, index, count, recorded_durations(platform.system()))
    if not selected:
        raise pytest.UsageError("The test shard is empty")
    selected_set = set(selected)
    deselected = [item for item in items if item.nodeid not in selected_set]
    items[:] = [item for item in items if item.nodeid in selected_set]
    config.hook.pytest_deselected(items=deselected)
    manifest = {
        "index": index,
        "count": count,
        "collected": nodes,
        "selected": selected,
        "identity": run_identity(),
    }
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    output = Path(os.environ["PYRACAST_TEST_SHARD_OUTPUT"])
    (output / f"collection-{worker}.json").write_text(json.dumps(manifest), encoding="utf-8")


def read_manifest(path: Path) -> dict[str, object]:
    value: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Invalid shard manifest: {path}")
    return value


def finish_shard(output: Path) -> None:
    manifests = [read_manifest(path) for path in sorted(output.glob("collection-*.json"))]
    if not manifests or any(manifest != manifests[0] for manifest in manifests):
        raise ValueError("Test workers did not collect the same shard")
    (output / "manifest.json").write_text(json.dumps(manifests[0]), encoding="utf-8")


def validate_shards(outputs: list[Path]) -> list[Path]:
    manifests = [read_manifest(output / "manifest.json") for output in outputs]
    count = len(outputs)
    if count < 2 or {manifest.get("index") for manifest in manifests} != set(range(1, count + 1)):
        raise ValueError("A complete, unique set of test shards is required")
    collected = manifests[0].get("collected")
    if (
        not isinstance(collected, list)
        or not collected
        or not all(isinstance(node, str) for node in collected)
        or collected != sorted(set(collected))
    ):
        raise ValueError("Invalid collected test inventory")
    durations = recorded_durations(platform.system())
    coverage_files = []
    for output, manifest in zip(outputs, manifests, strict=True):
        index = manifest["index"]
        if (
            type(index) is not int
            or manifest.get("count") != count
            or manifest.get("identity") != run_identity()
            or manifest.get("collected") != collected
            or manifest.get("selected") != selected_nodes(collected, index, count, durations)
        ):
            raise ValueError("Test shard provenance or partition does not match")
        coverage_file = output / "coverage.dat"
        if not coverage_file.is_file() or not coverage_file.stat().st_size:
            raise ValueError("A test shard has no coverage data")
        coverage_files.append(coverage_file)
    return coverage_files
