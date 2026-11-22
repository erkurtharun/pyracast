import datetime
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from deploy import host_contract, release_environment
from deploy.host_contract import HostConfigError

ROOT = Path(__file__).resolve().parents[1]
TERRAFORM = ROOT / "infra" / "terraform"
AUTHORITY = (ROOT / "deploy" / "origin-pull-off.pem").read_text(encoding="utf-8")
HOST = {
    "project_id": "pyracast-production",
    "runtime_secret_id": "projects/pyracast-production/secrets/pyracast-runtime",
    "runtime_secret_version": "projects/pyracast-production/secrets/pyracast-runtime/versions/3",
    "database_secret_version": "projects/pyracast-production/secrets/pyracast-database/versions/1",
    "backup_bucket": "pyracast-production-pyracast-backups",
    "domain": "pyracast.app",
    "acme_email": "ops@pyracast.app",
    "origin_pull_ca": "",
}


def host(**changes: Any) -> dict[str, Any]:
    values = {**HOST, **changes}
    return {key: value for key, value in values.items() if value is not None}


def host_with(key: str, value: object) -> dict[str, Any]:
    return host(**{key: value})


# The keys of the host.json that Terraform wrote before the optional daily tier existed.
BEFORE_DAILY_TIER = frozenset(HOST)
REQUIRED = frozenset(setting.key for setting in host_contract.SETTINGS if setting.required)
OPTIONAL = host_contract.KEYS - REQUIRED


def terraform_host_config() -> tuple[set[str], dict[str, bool]]:
    written = re.search(
        r"host_config_base64 = base64encode\(jsonencode\(merge\(\{(.*?)\}, "
        r"\{ for key, value in \{(.*?)\} : key => value if var\.[a-z_]+ \}\)\)\)",
        (TERRAFORM / "main.tf").read_text(encoding="utf-8"),
        re.S,
    )
    assert written
    always = set(re.findall(r"^\s*([a-z_]+)\s*=", written[1], re.M))
    conditional = {
        key: value == "true" for key, value in re.findall(r"([a-z_]+) = (true|false)", written[2])
    }
    assert set(re.findall(r"([a-z_]+)\s*=", written[2])) == set(conditional)
    return always, conditional


def test_the_host_configuration_terraform_writes_is_the_host_contract() -> None:
    always, conditional = terraform_host_config()

    assert always | set(conditional) == host_contract.KEYS
    assert always >= REQUIRED
    assert conditional
    assert set(conditional) <= OPTIONAL
    assert host_contract.parse({**HOST, **conditional}).daily_backups is True


def test_without_the_daily_tier_terraform_writes_the_keys_the_host_holds_today() -> None:
    always, _ = terraform_host_config()

    assert always == BEFORE_DAILY_TIER


@pytest.mark.parametrize(
    "keys",
    [
        pytest.param(BEFORE_DAILY_TIER, id="today"),
        pytest.param(BEFORE_DAILY_TIER - {"origin_pull_ca"}, id="before-origin-pulls"),
    ],
)
def test_a_host_configuration_written_before_the_daily_tier_passes_the_contract(
    keys: frozenset[str],
) -> None:
    config = host_contract.parse({key: HOST[key] for key in keys})

    assert config.daily_backups is False
    assert config.origin_pull_ca == ""


# The image helper reads container image configurations, not the host's.
IMAGE_HELPER = ROOT / "deploy" / "ci_images.py"


def host_reads() -> tuple[set[str], set[str]]:
    indexed: set[str] = set()
    defaulted: set[str] = set()
    for path in [*(ROOT / "deploy").glob("*.py"), ROOT / "deploy" / "install-release.sh"]:
        source = path.read_text(encoding="utf-8")
        indexed |= set(re.findall(r"\bconfig\[[\"']([a-z_]+)[\"']\]", source))
        if path != IMAGE_HELPER:
            defaulted |= set(re.findall(r"\bconfig\.get\([\"']([a-z_]+)[\"']", source))
    return indexed, defaulted


def test_every_key_a_host_helper_reads_is_a_host_setting() -> None:
    indexed, defaulted = host_reads()

    assert {"backup_bucket", "runtime_secret_version"} <= indexed <= REQUIRED
    assert {"daily_backups"} <= defaulted <= OPTIONAL


def test_only_optional_settings_have_a_default() -> None:
    assert {
        setting.key: setting.default for setting in host_contract.SETTINGS if not setting.required
    } == {"origin_pull_ca": "", "daily_backups": False}


@pytest.mark.parametrize("value", [True, False])
def test_the_daily_tier_is_a_json_boolean(value: bool) -> None:
    assert host_contract.parse(host(daily_backups=value)).daily_backups is value


def test_a_complete_host_configuration_loads_typed(tmp_path: Path) -> None:
    path = tmp_path / "host.json"
    path.write_text(json.dumps(host(origin_pull_ca=None)), encoding="utf-8")

    config = host_contract.load(path)

    assert config == host_contract.HostConfig(**HOST, daily_backups=False)


def test_a_named_origin_pull_authority_is_kept_as_certificates() -> None:
    config = host_contract.parse(host(origin_pull_ca=AUTHORITY.strip()))

    assert config.origin_pull_ca == AUTHORITY.strip() + "\n"


def envelope(body: str) -> str:
    return f"-----BEGIN CERTIFICATE-----\n{body}\n-----END CERTIFICATE-----\n"


def issued_authority() -> str:
    now = datetime.datetime.now(datetime.UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Pyracast edge authority")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode()


KEY = (
    ec.generate_private_key(ec.SECP256R1())
    .private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    .decode()
)
LINES = AUTHORITY.splitlines()
TRUNCATED = "\n".join([*LINES[: len(LINES) // 2], LINES[-1]]) + "\n"


@pytest.mark.parametrize(
    "authority",
    [
        pytest.param(AUTHORITY + AUTHORITY, id="two-certificates"),
        pytest.param(AUTHORITY + "\n" + AUTHORITY, id="two-certificates-and-a-blank-line"),
        pytest.param(issued_authority(), id="issued-authority"),
    ],
)
def test_an_edge_authority_of_x509_certificates_is_accepted(authority: str) -> None:
    assert host_contract.parse(host(origin_pull_ca=authority)).origin_pull_ca == (
        authority.strip() + "\n"
    )


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(envelope("AAAA"), id="pem-envelope-around-bytes-that-are-not-a-certificate"),
        pytest.param(TRUNCATED, id="truncated-certificate"),
        pytest.param(KEY.replace("PRIVATE KEY", "CERTIFICATE"), id="private-key-as-certificate"),
        pytest.param(KEY, id="private-key"),
        pytest.param(AUTHORITY + envelope("AAAA"), id="certificate-then-non-certificate"),
        pytest.param(envelope(""), id="empty-certificate"),
        pytest.param("\n", id="empty-bundle"),
        pytest.param("not a certificate", id="text"),
        pytest.param(AUTHORITY.replace("CERTIFICATE", "PRIVATE KEY"), id="key-envelope"),
        pytest.param(
            AUTHORITY + "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n",
            id="certificate-then-key",
        ),
        pytest.param(AUTHORITY.replace("\n", "\n$", 1), id="foreign-character"),
        pytest.param(1, id="number"),
    ],
)
def test_an_edge_authority_that_is_not_only_x509_certificates_is_refused(value: object) -> None:
    with pytest.raises(HostConfigError, match="origin_pull_ca"):
        host_contract.parse(host(origin_pull_ca=value))


@pytest.mark.parametrize(
    ("values", "refusal"),
    [
        pytest.param([], "Host configuration must be a JSON object of settings.", id="not-object"),
        pytest.param(
            host_with("runtime_secret_version", None),
            "Host setting runtime_secret_version is missing; it is required.",
            id="missing",
        ),
        pytest.param(
            host(region="europe-west1"),
            "Host setting region is unknown; remove it or add it to deploy/host_contract.py.",
            id="unknown",
        ),
        pytest.param(
            host(domain="https://pyracast.app"),
            "Host setting domain must be a lowercase DNS hostname.",
            id="domain",
        ),
        pytest.param(
            host(acme_email="ops@example.com\nX=1"),
            "Host setting acme_email must be an email address.",
            id="email",
        ),
        pytest.param(
            host_with(
                "runtime_secret_version", "projects/p/secrets/pyracast-runtime/versions/latest"
            ),
            "Host setting runtime_secret_version must be a numeric version of runtime_secret_id.",
            id="latest",
        ),
        pytest.param(
            host_with(
                "runtime_secret_version",
                "projects/pyracast-production/secrets/pyracast-database/versions/1",
            ),
            "Host setting runtime_secret_version must be a numeric version of runtime_secret_id.",
            id="other-secret",
        ),
        pytest.param(
            host(database_secret_version=1),
            "Host setting database_secret_version must be a numeric Secret Manager version.",
            id="number",
        ),
        pytest.param(
            host(backup_bucket="Backups"),
            "Host setting backup_bucket must be a Cloud Storage bucket name.",
            id="bucket",
        ),
        pytest.param(
            host(project_id="p"),
            "Host setting project_id must be a Google Cloud project ID.",
            id="project",
        ),
        pytest.param(
            host_with("runtime_secret_id", "pyracast-runtime"),
            "Host setting runtime_secret_id must be a Secret Manager secret name.",
            id="secret-id",
        ),
        pytest.param(
            host(origin_pull_ca="-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"),
            "Host setting origin_pull_ca must hold only PEM X.509 certificates of the edge's "
            "authority.",
            id="authority",
        ),
        pytest.param(
            host(origin_pull_ca=["certificate"]),
            "Host setting origin_pull_ca must hold only PEM X.509 certificates of the edge's "
            "authority.",
            id="authority-type",
        ),
        pytest.param(
            {**HOST, "origin_pull_ca": None},
            "Host setting origin_pull_ca must hold only PEM X.509 certificates of the edge's "
            "authority.",
            id="authority-null",
        ),
        pytest.param(
            {**HOST, "domain": None},
            "Host setting domain must be a lowercase DNS hostname.",
            id="null",
        ),
        pytest.param(
            host(daily_backups="true"),
            "Host setting daily_backups must be a JSON boolean.",
            id="daily-backups-text",
        ),
        pytest.param(
            host(daily_backups=1),
            "Host setting daily_backups must be a JSON boolean.",
            id="daily-backups-number",
        ),
        pytest.param(
            {**HOST, "daily_backups": None},
            "Host setting daily_backups must be a JSON boolean.",
            id="daily-backups-null",
        ),
        pytest.param(
            {**HOST, "region\nX": "europe-west1"},
            "Host setting 'region\\nX' is unknown; remove it or add it to deploy/host_contract.py.",
            id="unknown-control-character",
        ),
    ],
)
def test_an_unknown_ill_typed_or_missing_setting_is_refused_in_one_line(
    values: object, refusal: str
) -> None:
    with pytest.raises(HostConfigError) as refused:
        host_contract.parse(values)

    assert str(refused.value) == refusal
    assert "\n" not in str(refused.value)


@pytest.mark.parametrize(
    ("content", "refusal"),
    [
        (None, "Host configuration {path} cannot be read"),
        ("{", "Host configuration {path} is not valid JSON."),
    ],
)
def test_an_unreadable_host_configuration_is_refused_in_one_line(
    tmp_path: Path, content: str | None, refusal: str
) -> None:
    path = tmp_path / "host.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")

    with pytest.raises(HostConfigError) as refused:
        host_contract.load(path)

    assert str(refused.value).startswith(refusal.format(path=path))
    assert "\n" not in str(refused.value)


def compose_variables() -> dict[str, bool]:
    compose = (ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
    found: dict[str, bool] = {}
    for name, operator in re.findall(r"\$\{([A-Z_]+)(:[?-])?", compose):
        found[name] = found.get(name, False) or operator == ":?"
    return found


def test_the_compose_file_reads_exactly_the_release_environment_the_contract_names() -> None:
    variables = compose_variables()

    assert set(variables) == {
        *host_contract.RELEASE_KEYS,
        host_contract.ORIGIN_PULL_CA_KEY,
        *host_contract.COMPOSE_DEFAULTS,
    }
    assert {name for name, required in variables.items() if required} <= set(
        host_contract.RELEASE_KEYS
    )


def test_the_release_environment_renders_every_release_key_the_contract_names(
    tmp_path: Path,
) -> None:
    images = [
        f"us-docker.pkg.dev/p/r/{name}@sha256:" + "0" * 64
        for name in ("backend", "web", "gateway", "postgres", "rabbitmq")
    ]
    config = host_contract.parse(host(origin_pull_ca=AUTHORITY))

    values = release_environment.render(config, "/run/pyracast/releases/r", images, tmp_path)

    assert list(values) == [*host_contract.RELEASE_KEYS, host_contract.ORIGIN_PULL_CA_KEY]


def test_the_runtime_secret_carries_only_settings_the_application_reads() -> None:
    settings = (ROOT / "src" / "pyracast" / "settings.py").read_text(encoding="utf-8")
    read = set(re.findall(r'os\.environ\.get\(\s*"([A-Z_]+)"', settings))
    providers = {
        f"PYRACAST_{provider}_{part}"
        for provider in ("GOOGLE", "MICROSOFT")
        for part in ("CLIENT_ID", "CLIENT_SECRET")
    }
    assert 'os.environ.get(f"PYRACAST_{_provider.upper()}_CLIENT_ID"' in settings
    environment = yaml.safe_load((ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8"))
    compose_set = set(environment["services"]["api"]["environment"])
    rendered = {"PYRACAST_RABBITMQ_URL"}

    assert ((read | providers) - compose_set - rendered) | {
        "RABBITMQ_DEFAULT_PASS"
    } == host_contract.RUNTIME_SETTINGS
    assert host_contract.RUNTIME_REQUIRED <= host_contract.RUNTIME_SETTINGS
    assert all(
        name.startswith(host_contract.APPLICATION_PREFIXES)
        for name in host_contract.RUNTIME_SETTINGS - {"RABBITMQ_DEFAULT_PASS"}
    )
