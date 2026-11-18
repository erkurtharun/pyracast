import itertools
from pathlib import Path
from typing import Any

import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def workflow(name: str) -> dict[Any, Any]:
    document: dict[Any, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return document


def ci_jobs() -> dict[str, dict[str, Any]]:
    jobs: dict[str, dict[str, Any]] = workflow("ci.yml")["jobs"]
    return jobs


def release_jobs() -> dict[str, dict[str, Any]]:
    jobs: dict[str, dict[str, Any]] = workflow("release.yml")["jobs"]
    return jobs


def matrix_combinations(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    axes = {key: values for key, values in matrix.items() if key not in {"include", "exclude"}}
    original: list[dict[str, Any]] = []
    for values in itertools.product(*axes.values()) if axes else ():
        combination = dict(zip(axes, values, strict=True))
        if not any(
            all(combination.get(key) == value for key, value in rule.items())
            for rule in matrix.get("exclude", [])
        ):
            original.append(combination)
    added: list[dict[str, Any]] = []
    for included in matrix.get("include", []):
        extended = [
            combination
            for combination in original
            if all(combination[key] == value for key, value in included.items() if key in axes)
        ]
        for combination in extended:
            combination.update(included)
        if not extended:
            added.append(dict(included))
    return [*original, *added]


def job_names(identifier: str, job: dict[str, Any]) -> set[str]:
    matrix = job.get("strategy", {}).get("matrix")
    names: set[str] = set()
    for combination in [{}] if matrix is None else matrix_combinations(matrix):
        name = str(job.get("name", identifier))
        for key, value in combination.items():
            name = name.replace(f"${{{{ matrix.{key} }}}}", str(value))
        if "${{" in name:
            raise ValueError(f"Unsupported expression in CI job name: {name}")
        names.add(name)
    return names


def ci_job_names(jobs: dict[str, Any]) -> set[str]:
    return {name for identifier, job in jobs.items() for name in job_names(identifier, job)}


def step(job: dict[str, Any], name: str) -> dict[str, Any]:
    steps: list[dict[str, Any]] = job["steps"]
    (found,) = [candidate for candidate in steps if candidate.get("name") == name]
    return found


def step_index(job: dict[str, Any], fragment: str) -> int:
    (index,) = [
        position
        for position, candidate in enumerate(job["steps"])
        if fragment in candidate.get("run", "") or fragment in candidate.get("uses", "")
    ]
    return index


def commands(job: dict[str, Any]) -> str:
    return "\n".join(str(candidate.get("run", "")) for candidate in job["steps"])
