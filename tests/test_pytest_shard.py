import json
import os
import re
import subprocess
import sys
from html import unescape
from pathlib import Path

import pytest

from scripts import check
from scripts.pytest_shard import (
    finish_shard,
    parse_shard,
    read_manifest,
    run_identity,
    selected_nodes,
    validate_shards,
)


def test_coverage_xml_resolves_each_package_initializer_to_one_source(tmp_path: Path) -> None:
    configuration = Path(__file__).resolve().parents[1] / "pyproject.toml"
    files = ["src/pyracast/__init__.py", "deploy/__init__.py"]
    for name in files:
        path = tmp_path / name
        path.parent.mkdir(parents=True)
        path.write_text("ready = True\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os\nfrom coverage import Coverage\n"
            "measurement = Coverage(config_file=os.environ['PYRACAST_COVERAGE_CONFIG'], "
            "data_file=None)\n"
            "data = measurement.get_data()\n"
            "try:\n"
            "    data.add_lines({'src/pyracast/__init__.py': {1}, 'deploy/__init__.py': {1}})\n"
            "    measurement.xml_report(outfile='coverage.xml')\n"
            "finally:\n"
            "    data.close(force=True)\n",
        ],
        cwd=tmp_path,
        env=os.environ | {"PYRACAST_COVERAGE_CONFIG": str(configuration)},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    xml = (tmp_path / "coverage.xml").read_text(encoding="utf-8")
    filenames = re.findall(r'filename="([^"]+)"', xml)
    roots = [tmp_path / unescape(source) for source in re.findall(r"<source>([^<]+)</source>", xml)]
    resolved = [
        {(root / name).resolve() for root in roots if (root / name).is_file()} for name in filenames
    ]
    assert len(filenames) == len(files)
    assert all(len(paths) == 1 for paths in resolved)
    assert set.union(*resolved) == {(tmp_path / name).resolve() for name in files}


@pytest.mark.parametrize("value", ["0/2", "3/2", "1/1", "one/two", "1/2/3"])
def test_invalid_shard_configuration_is_rejected(value: str) -> None:
    with pytest.raises(ValueError, match=r"shard|literal|unpack"):
        parse_shard(value)


def test_partition_is_complete_deterministic_and_keeps_modules_together() -> None:
    nodes = [f"tests/{file}.py::test_{index}" for file in "abcd" for index in range(3)]
    first = selected_nodes(nodes, 1, 2)
    second = selected_nodes(list(reversed(nodes)), 2, 2)
    assert not set(first) & set(second)
    assert sorted(first + second) == sorted(nodes)
    assert first == selected_nodes(list(reversed(nodes)), 1, 2)
    assert len(first) == len(second)
    for file in "abcd":
        assert (
            sum(
                any(node.startswith(f"tests/{file}.py::") for node in shard)
                for shard in (first, second)
            )
            == 1
        )


def write_shard(root: Path, index: int, nodes: list[str]) -> Path:
    output = root / str(index)
    output.mkdir()
    manifest = {
        "index": index,
        "count": 2,
        "identity": run_identity(),
        "collected": nodes,
        "selected": selected_nodes(nodes, index, 2),
    }
    (output / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (output / "coverage.dat").write_bytes(b"coverage")
    return output


@pytest.mark.parametrize(
    "defect", ["missing", "duplicate", "overlap", "inventory", "identity", "run", "coverage"]
)
def test_aggregate_rejects_incomplete_or_unrelated_shards(tmp_path: Path, defect: str) -> None:
    nodes = ["tests/a.py::test_first", "tests/b.py::test_second"]
    outputs = [write_shard(tmp_path, index, nodes) for index in (1, 2)]
    manifest = read_manifest(outputs[1] / "manifest.json")
    if defect == "missing":
        outputs.pop()
    elif defect == "duplicate":
        outputs[1] = outputs[0]
    elif defect == "overlap":
        manifest["selected"] = nodes
    elif defect == "inventory":
        manifest["collected"] = ["tests/c.py::test_unexpected"]
    elif defect == "identity":
        manifest["identity"] = {**run_identity(), "sha": "another-commit"}
    elif defect == "run":
        manifest["identity"] = {**run_identity(), "run_id": "another-run"}
    else:
        (outputs[1] / "coverage.dat").unlink()
    if defect not in {"missing", "duplicate"}:
        (outputs[1] / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match=r"shard|inventory"):
        validate_shards(outputs)


def test_shards_from_different_attempts_of_one_run_combine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nodes = ["tests/a.py::test_first", "tests/b.py::test_second"]
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_RUN_ID", "7001")
    outputs = []
    for index, attempt in ((1, "1"), (2, "2")):
        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", attempt)
        outputs.append(write_shard(tmp_path, index, nodes))

    assert validate_shards(outputs) == [output / "coverage.dat" for output in outputs]


def test_workers_with_different_collections_cannot_publish_a_manifest(tmp_path: Path) -> None:
    (tmp_path / "collection-gw0.json").write_text('{"selected": ["a"]}', encoding="utf-8")
    (tmp_path / "collection-gw1.json").write_text('{"selected": ["b"]}', encoding="utf-8")
    with pytest.raises(ValueError, match="same shard"):
        finish_shard(tmp_path)
    assert not (tmp_path / "manifest.json").exists()


def test_check_script_keeps_its_direct_entrypoint() -> None:
    project = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "scripts/check.py", "--help"],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--combine-shards" in result.stdout


def test_real_xdist_shards_merge_at_the_unchanged_project_floor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Path(__file__).resolve().parents[1]
    outer_data = tmp_path / "outer.dat"
    outer_data.write_bytes(b"outer coverage must be preserved")
    monkeypatch.setenv("COVERAGE_FILE", str(outer_data))
    (tmp_path / "scripts").mkdir()
    monkeypatch.setattr(check, "__file__", str(tmp_path / "scripts" / "check.py"))
    (tmp_path / "pyproject.toml").write_text(
        '[tool.coverage.run]\nbranch = true\nrelative_files = true\nsource = ["sample"]\n'
        "[tool.coverage.report]\nfail_under = 100\n",
        encoding="utf-8",
    )
    (tmp_path / "sample.py").write_text(
        "def choose(value):\n    if value:\n        return 1\n    return 2\n",
        encoding="utf-8",
    )
    for name, value, expected in (("a", True, 1), ("b", False, 2)):
        (tmp_path / f"test_{name}.py").write_text(
            "from sample import choose\ndef test_choice():\n"
            f"    assert choose({value}) == {expected}\n",
            encoding="utf-8",
        )
    environment = os.environ.copy()
    environment.pop("PYTEST_ADDOPTS", None)
    for name in list(environment):
        if name.startswith(("COV_CORE_", "COVERAGE_")):
            environment.pop(name)
    environment.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONPATH=str(project))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    outputs = []
    for index in (1, 2):
        output = tmp_path / f"shard-{index}"
        output.mkdir()
        environment.update(
            PYRACAST_TEST_SHARD=f"{index}/2",
            PYRACAST_TEST_SHARD_OUTPUT=str(output),
            COVERAGE_FILE=str(output / "coverage.dat"),
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "pytest_cov",
                "-p",
                "xdist.plugin",
                "-p",
                "scripts.pytest_shard",
                "-n",
                "2",
                "--cov=sample",
                "--cov-report=",
                "--cov-fail-under=0",
                "-q",
            ],
            cwd=tmp_path,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        finish_shard(output)
        outputs.append(output)
    assert len(validate_shards(outputs)) == 2
    # A valid inventory cannot conceal missing executed coverage from one shard.
    full_data = (outputs[1] / "coverage.dat").read_bytes()
    (outputs[1] / "coverage.dat").write_bytes((outputs[0] / "coverage.dat").read_bytes())
    arguments = ["--combine-shards", *(str(output) for output in outputs)]
    monkeypatch.setenv("COVERAGE_FILE", str(tmp_path / "combined.dat"))
    assert check.main(arguments) == 2
    (outputs[1] / "coverage.dat").write_bytes(full_data)
    assert check.main(arguments) == 0
    assert (tmp_path / "coverage.xml").is_file()
    assert outer_data.read_bytes() == b"outer coverage must be preserved"
