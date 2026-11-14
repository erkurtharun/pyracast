import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from deploy import ci_images

SHA = "1" * 40
INDEX = "application/vnd.oci.image.index.v1+json"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"


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
            "os": "linux",
            "config": {
                "Labels": {"org.opencontainers.image.revision": revision},
            },
        },
        "application/vnd.oci.image.config.v1+json",
    )
    runtime = blob(
        {"schemaVersion": 2, "mediaType": MANIFEST, "config": config, "layers": []}, MANIFEST
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
                "subject": runtime.copy(),
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
    manifests = [runtime, attestation_descriptor]
    if defect == "duplicate-platform":
        manifests.append(runtime)
    root = blob({"schemaVersion": 2, "mediaType": INDEX, "manifests": manifests}, INDEX)
    entries["index.json"] = json.dumps({"schemaVersion": 2, "manifests": [root]}).encode()
    entries["oci-layout"] = b'{"imageLayoutVersion":"1.0.0"}'
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
