import argparse
import hashlib
import json
import re
import tarfile
from pathlib import Path

IMAGES = ("backend", "web", "gateway", "postgres", "rabbitmq")
INDEX = "application/vnd.oci.image.index.v1+json"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"
PROVENANCE = {"https://slsa.dev/provenance/v0.2", "https://slsa.dev/provenance/v1"}


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError("JSON object keys must be strings")
        result[key] = item
    return result


def _objects(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ValueError("Expected a JSON array")
    return [_object(item) for item in value]


def _json(archive: tarfile.TarFile, name: str) -> dict[str, object]:
    member = archive.getmember(name)
    if member.size > 128 * 1024 * 1024:
        raise ValueError("Oversized OCI JSON document")
    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError("Missing OCI document")
    with stream:
        return _object(json.load(stream))


def _digest(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("Invalid OCI digest")
    return value


def _blob(archive: tarfile.TarFile, descriptor: dict[str, object]) -> str:
    digest = _digest(descriptor.get("digest"))
    name = "blobs/sha256/" + digest[7:]
    try:
        member = archive.getmember(name)
    except KeyError as error:
        raise ValueError("Missing OCI blob") from error
    if type(descriptor.get("size")) is not int or descriptor["size"] != member.size:
        raise ValueError("OCI blob size mismatch")
    if descriptor.get("urls"):
        raise ValueError("External OCI blobs are not permitted")
    return name


def _manifest(archive: tarfile.TarFile, descriptor: dict[str, object]) -> dict[str, object]:
    if descriptor.get("mediaType") != MANIFEST:
        raise ValueError("Expected an OCI image manifest")
    manifest = _json(archive, _blob(archive, descriptor))
    if manifest.get("schemaVersion") != 2 or manifest.get("mediaType") != MANIFEST:
        raise ValueError("Invalid OCI manifest")
    _blob(archive, _object(manifest.get("config")))
    for layer in _objects(manifest.get("layers")):
        _blob(archive, layer)
    return manifest


def _check_entries(archive: tarfile.TarFile) -> None:
    names: set[str] = set()
    for member in archive:
        if member.name in names:
            raise ValueError("Duplicate OCI archive entry")
        names.add(member.name)
        if member.isdir() and member.name in {"blobs", "blobs/sha256"}:
            continue
        if not member.isfile() or not re.fullmatch(
            r"oci-layout|index\.json|blobs/sha256/[0-9a-f]{64}", member.name
        ):
            raise ValueError("Unsafe OCI archive entry")
        if member.name.startswith("blobs/"):
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("Missing OCI blob data")
            with stream:
                digest = hashlib.sha256()
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != member.name.removeprefix("blobs/sha256/"):
                raise ValueError("OCI blob digest mismatch")
    if not {"oci-layout", "index.json"} <= names:
        raise ValueError("Missing OCI layout")


def _check_attestations(
    archive: tarfile.TarFile, descriptors: list[dict[str, object]], runtime: dict[str, object]
) -> None:
    predicates: set[str] = set()
    for descriptor in descriptors:
        if descriptor is runtime:
            continue
        annotations = _object(descriptor.get("annotations"))
        if descriptor.get("platform") != {"architecture": "unknown", "os": "unknown"} or (
            annotations.get("vnd.docker.reference.type") != "attestation-manifest"
            or annotations.get("vnd.docker.reference.digest") != runtime["digest"]
        ):
            raise ValueError("Unbound OCI attestation")
        attestation = _manifest(archive, descriptor)
        if "subject" in attestation and _object(attestation["subject"]) != {
            key: runtime[key] for key in ("mediaType", "digest", "size")
        }:
            raise ValueError("OCI attestation subject mismatch")
        for layer in _objects(attestation["layers"]):
            if layer.get("mediaType") != "application/vnd.in-toto+json":
                raise ValueError("Expected an in-toto attestation")
            statement = _json(archive, _blob(archive, layer))
            subjects = _objects(statement.get("subject"))
            expected_subject = {"sha256": _digest(runtime["digest"])[7:]}
            if not subjects or any(
                _object(item.get("digest")) != expected_subject for item in subjects
            ):
                raise ValueError("In-toto subject mismatch")
            predicate = statement.get("predicateType")
            if isinstance(predicate, str):
                predicates.add(predicate)
    if "https://spdx.dev/Document" not in predicates or not predicates & PROVENANCE:
        raise ValueError("Missing provenance or SBOM attestation")


def inspect_archive(path: Path, sha: str, expected_digest: str) -> dict[str, str]:
    with path.open("rb") as archive_stream:
        archive_sha256 = hashlib.file_digest(archive_stream, "sha256").hexdigest()
    with tarfile.open(path, "r:") as archive:
        _check_entries(archive)
        if _json(archive, "oci-layout") != {"imageLayoutVersion": "1.0.0"}:
            raise ValueError("Invalid OCI layout version")
        roots = _objects(_json(archive, "index.json").get("manifests"))
        if len(roots) != 1 or roots[0].get("mediaType") != INDEX:
            raise ValueError("Expected one OCI image index")
        if roots[0].get("digest") != expected_digest:
            raise ValueError("Build output digest mismatch")
        index = _json(archive, _blob(archive, roots[0]))
        if index.get("schemaVersion") != 2 or index.get("mediaType") != INDEX:
            raise ValueError("Invalid OCI image index")
        descriptors = _objects(index.get("manifests"))
        runtime = [
            item
            for item in descriptors
            if item.get("platform")
            == {
                "architecture": "amd64",
                "os": "linux",
            }
        ]
        if len(runtime) != 1:
            raise ValueError("Expected one linux/amd64 runtime image")
        image = _manifest(archive, runtime[0])
        config_descriptor = _object(image["config"])
        config = _json(archive, _blob(archive, config_descriptor))
        labels = _object(_object(config.get("config")).get("Labels"))
        if labels.get("org.opencontainers.image.revision") != sha:
            raise ValueError("Runtime revision mismatch")
        if config.get("os") != "linux" or config.get("architecture") != "amd64":
            raise ValueError("Runtime configuration platform mismatch")
        _check_attestations(archive, descriptors, runtime[0])
    return {
        "digest": expected_digest,
        "config_digest": _digest(config_descriptor["digest"]),
        "archive_sha256": archive_sha256,
    }


def record(directory: Path, sha: str, run_id: int, run_attempt: int) -> dict[str, object]:
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or run_id < 1 or run_attempt < 1:
        raise ValueError("Invalid CI identity")
    images = {
        name: inspect_archive(
            directory / f"{name}.oci.tar", sha, (directory / f"{name}.digest").read_text().strip()
        )
        for name in IMAGES
    }
    return {"format": 1, "sha": sha, "run_id": run_id, "run_attempt": run_attempt, "images": images}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "verify"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("sha")
    parser.add_argument("run_id", type=int)
    parser.add_argument("run_attempt", type=int)
    args = parser.parse_args()
    actual = record(args.directory, args.sha, args.run_id, args.run_attempt)
    manifest = args.directory / "images.json"
    if args.command == "prepare":
        manifest.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    elif json.loads(manifest.read_text(encoding="utf-8")) != actual:
        raise ValueError("CI image record mismatch")


if __name__ == "__main__":
    main()
