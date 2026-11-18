import hashlib
import io
import json
import subprocess
import sys
import tarfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import pytest

from deploy import ci_images

SHA = "1" * 40
INDEX = "application/vnd.oci.image.index.v1+json"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"


def test_json_object_rejects_non_string_keys() -> None:
    with pytest.raises(ValueError, match="JSON object keys must be strings"):
        ci_images._object({"name": "image", 1: "invalid"})


def _archive(
    directory: Path, *, revision: str = SHA, defect: str = "", modern: bool = True
) -> tuple[Path, str, str]:
    entries: dict[str, bytes] = {}

    def blob(value: object, media_type: str) -> dict[str, object]:
        content = json.dumps(value, sort_keys=True).encode()
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        entries["blobs/sha256/" + digest[7:]] = content
        return {"mediaType": media_type, "digest": digest, "size": len(content)}

    config = blob(
        {
            "architecture": "amd64",
            "os": "windows" if defect == "platform" else "linux",
            "config": {
                "Labels": {"org.opencontainers.image.revision": revision},
            },
        },
        "application/vnd.oci.image.config.v1+json",
    )
    runtime = blob(
        {
            "schemaVersion": 1 if defect == "manifest-schema" else 2,
            "mediaType": MANIFEST,
            "config": config,
            "layers": [],
        },
        MANIFEST,
    )
    runtime_digest = str(runtime["digest"])
    layers = []
    provenance = "https://slsa.dev/provenance/v1" if modern else "https://slsa.dev/provenance/v0.2"
    for predicate in ("https://spdx.dev/Document", provenance):
        if defect == "missing-sbom" and predicate == "https://spdx.dev/Document":
            continue
        subject = "0" * 64 if defect == "wrong-subject" else runtime_digest[7:]
        layers.append(
            blob(
                {
                    "_type": "https://in-toto.io/Statement/v1",
                    "subject": [
                        {"name": "image", "digest": {"sha256": subject}},
                    ],
                    "predicateType": predicate,
                    "predicate": {},
                },
                "application/json" if defect == "layer-type" else "application/vnd.in-toto+json",
            )
        )
    if defect == "untyped-predicate":
        layers.append(
            blob(
                {"subject": [{"digest": {"sha256": runtime_digest[7:]}}], "predicateType": 7},
                "application/vnd.in-toto+json",
            )
        )
    attestation: dict[str, object] = {
        "schemaVersion": 2,
        "mediaType": MANIFEST,
        "config": blob({}, "application/vnd.oci.empty.v1+json"),
        "layers": layers,
    }
    if modern:
        attestation.update(
            {
                "artifactType": "application/vnd.docker.attestation.manifest.v1+json",
                "subject": {**runtime, "size": 1} if defect == "subject" else runtime.copy(),
            }
        )
    attestation_descriptor = blob(attestation, MANIFEST)
    attestation_descriptor.update(
        {
            "platform": {"architecture": "unknown", "os": "unknown"},
            "annotations": {
                "vnd.docker.reference.type": "attestation-manifest",
                "vnd.docker.reference.digest": (
                    "sha256:" + "0" * 64 if defect == "unbound" else runtime_digest
                ),
            },
        }
    )
    runtime["platform"] = {"architecture": "amd64", "os": "linux"}
    if defect == "manifest-type":
        runtime["mediaType"] = "application/vnd.docker.distribution.manifest.v2+json"
    if defect == "size":
        runtime["size"] = int(str(runtime["size"])) + 1
    if defect == "urls":
        runtime["urls"] = ["https://registry.example/blob"]
    manifests = [runtime, attestation_descriptor]
    if defect == "duplicate-platform":
        manifests.append(runtime)
    root = blob(
        {
            "schemaVersion": 1 if defect == "index-schema" else 2,
            "mediaType": INDEX,
            "manifests": manifests,
        },
        INDEX,
    )
    roots = [root, root] if defect == "two-roots" else [root]
    entries["index.json"] = json.dumps({"schemaVersion": 2, "manifests": roots}).encode()
    if defect != "missing-layout":
        version = "2.0.0" if defect == "layout-version" else "1.0.0"
        entries["oci-layout"] = json.dumps({"imageLayoutVersion": version}).encode()
    if defect == "corrupt-blob":
        entries["blobs/sha256/" + str(config["digest"])[7:]] = b"{}"
    if defect == "missing-blob":
        del entries["blobs/sha256/" + str(config["digest"])[7:]]
    path = directory / "backend.oci.tar"
    with tarfile.open(path, "w") as archive:
        for name in ("blobs", "blobs/sha256"):
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            archive.addfile(member)
        for name, content in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        if defect in {"symlink", "traversal", "duplicate"}:
            member = tarfile.TarInfo(
                {"symlink": "linked", "traversal": "../escape", "duplicate": "index.json"}[defect]
            )
            if defect == "symlink":
                member.type = tarfile.SYMTYPE
                member.linkname = "index.json"
            archive.addfile(member)
    return path, str(root["digest"]), str(config["digest"])


@pytest.mark.parametrize("modern", [True, False])
def test_oci_archive_records_exact_attested_build(tmp_path: Path, modern: bool) -> None:
    path, digest, config_digest = _archive(tmp_path, modern=modern)
    assert ci_images.inspect_archive(path, SHA, digest) == {
        "digest": digest,
        "config_digest": config_digest,
        "archive_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


@pytest.mark.parametrize(
    ("defect", "message"),
    [
        ("missing-sbom", "Missing provenance or SBOM"),
        ("wrong-subject", "In-toto subject"),
        ("unbound", "Unbound OCI"),
        ("duplicate-platform", "one linux/amd64"),
        ("corrupt-blob", "blob digest mismatch"),
        ("missing-blob", "Missing OCI blob"),
        ("symlink", "Unsafe OCI"),
        ("traversal", "Unsafe OCI"),
        ("duplicate", "Duplicate OCI"),
        ("missing-layout", "Missing OCI layout"),
        ("layout-version", "Invalid OCI layout version"),
        ("two-roots", "Expected one OCI image index"),
        ("index-schema", "Invalid OCI image index"),
        ("manifest-type", "Expected an OCI image manifest"),
        ("manifest-schema", "Invalid OCI manifest"),
        ("size", "OCI blob size mismatch"),
        ("urls", "External OCI blobs are not permitted"),
        ("platform", "Runtime configuration platform mismatch"),
        ("subject", "OCI attestation subject mismatch"),
        ("layer-type", "Expected an in-toto attestation"),
    ],
)
def test_oci_archive_rejects_tampering(tmp_path: Path, defect: str, message: str) -> None:
    path, digest, _ = _archive(tmp_path, defect=defect)
    with pytest.raises(ValueError, match=message):
        ci_images.inspect_archive(path, SHA, digest)


def test_oci_archive_rejects_another_revision_and_build_output(tmp_path: Path) -> None:
    path, digest, _ = _archive(tmp_path, revision="2" * 40)
    with pytest.raises(ValueError, match="Runtime revision mismatch"):
        ci_images.inspect_archive(path, SHA, digest)
    with pytest.raises(ValueError, match="Build output digest mismatch"):
        ci_images.inspect_archive(path, SHA, "sha256:" + "0" * 64)


@pytest.mark.parametrize("mutation", ["sha", "run_id", "run_attempt", "archive", "digest"])
def test_cli_rechecks_trusted_identity_and_archive_bytes(tmp_path: Path, mutation: str) -> None:
    path, digest, _ = _archive(tmp_path)
    content = path.read_bytes()
    for name in ("backend", "web", "gateway", "postgres"):
        (tmp_path / f"{name}.oci.tar").write_bytes(content)
        (tmp_path / f"{name}.digest").write_text(digest + "\n")
    command = [sys.executable, "deploy/ci_images.py", "prepare", str(tmp_path), SHA, "42", "3"]
    subprocess.run(command, check=True)  # noqa: S603
    command[2] = "verify"
    subprocess.run(command, check=True)  # noqa: S603
    if mutation == "archive":
        with path.open("ab") as stream:
            stream.write(b"tampered")
    elif mutation == "digest":
        (tmp_path / "backend.digest").write_text("sha256:" + "0" * 64)
    else:
        manifest = tmp_path / "images.json"
        values = json.loads(manifest.read_text())
        values[mutation] = "2" * 40 if mutation == "sha" else 99
        manifest.write_text(json.dumps(values))
    result = subprocess.run(command, check=False, capture_output=True, text=True)  # noqa: S603
    assert result.returncode != 0
    assert "mismatch" in result.stderr


def test_oci_archive_ignores_an_attestation_layer_without_a_predicate_type(tmp_path: Path) -> None:
    path, digest, _ = _archive(tmp_path, defect="untyped-predicate")

    assert ci_images.inspect_archive(path, SHA, digest)["digest"] == digest


@pytest.mark.parametrize(
    ("check", "value", "message"),
    [
        (ci_images._object, [], "Expected a JSON object"),
        (ci_images._objects, {}, "Expected a JSON array"),
        (ci_images._digest, "sha256:" + "0" * 63, "Invalid OCI digest"),
        (ci_images._digest, None, "Invalid OCI digest"),
    ],
)
def test_oci_documents_reject_values_of_the_wrong_shape(
    check: Callable[[object], object], value: object, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        check(value)


class Archive:
    def __init__(self, member: tarfile.TarInfo) -> None:
        self.member = member

    def __iter__(self) -> Iterator[tarfile.TarInfo]:
        return iter([self.member])

    def getmember(self, _name: str) -> tarfile.TarInfo:
        return self.member

    def extractfile(self, _member: tarfile.TarInfo) -> None:
        return None


def test_oci_documents_are_read_only_from_bounded_regular_files() -> None:
    oversized = tarfile.TarInfo("index.json")
    oversized.size = 128 * 1024 * 1024 + 1
    blob = tarfile.TarInfo("blobs/sha256/" + "0" * 64)

    with pytest.raises(ValueError, match="Oversized OCI JSON document"):
        ci_images._json(cast(tarfile.TarFile, Archive(oversized)), "index.json")
    with pytest.raises(ValueError, match="Missing OCI document"):
        ci_images._json(cast(tarfile.TarFile, Archive(tarfile.TarInfo("index.json"))), "index.json")
    with pytest.raises(ValueError, match="Missing OCI blob data"):
        ci_images._check_entries(cast(tarfile.TarFile, Archive(blob)))


def test_the_ci_image_record_needs_a_full_commit_and_positive_run_identity(tmp_path: Path) -> None:
    for sha, run_id, run_attempt in [("1" * 39, 1, 1), (SHA, 0, 1), (SHA, 1, 0)]:
        with pytest.raises(ValueError, match="Invalid CI identity"):
            ci_images.record(tmp_path, sha, run_id, run_attempt)


def test_verify_accepts_only_the_record_that_prepare_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, digest, _ = _archive(tmp_path)
    for name in ci_images.IMAGES:
        (tmp_path / f"{name}.oci.tar").write_bytes(path.read_bytes())
        (tmp_path / f"{name}.digest").write_text(digest + "\n")

    def command(step: str) -> None:
        monkeypatch.setattr(sys, "argv", ["ci_images.py", step, str(tmp_path), SHA, "42", "3"])
        ci_images.main()

    command("prepare")
    command("verify")
    manifest = tmp_path / "images.json"
    record = json.loads(manifest.read_text(encoding="utf-8"))
    assert (record["sha"], record["run_id"], record["run_attempt"]) == (SHA, 42, 3)
    manifest.write_text(json.dumps({**record, "run_attempt": 4}), encoding="utf-8")
    with pytest.raises(ValueError, match="CI image record mismatch"):
        command("verify")
