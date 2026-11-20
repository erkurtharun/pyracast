import re
from pathlib import Path

from deploy import host, runtime_secrets

ROOT = Path(__file__).resolve().parents[1]
TERRAFORM = ROOT / "infra" / "terraform"
TEMPLATE = (TERRAFORM / "templates" / "startup.sh.tftpl").read_text(encoding="utf-8")
PLACEHOLDERS = {"host_config_base64", "network_guard_base64", "data_device_name"}


def block(opening: str) -> str:
    start = TEMPLATE.index(opening)
    end = TEMPLATE.index("\nfi\n", start)
    return TEMPLATE[start:end]


def unit(path: str) -> dict[str, list[str]]:
    match = re.search(
        rf"write_if_changed {re.escape(path)} 0644 <<'EOF'\n(.*?)\nEOF", TEMPLATE, re.S
    )
    assert match, path
    values: dict[str, list[str]] = {}
    for line in match[1].splitlines():
        if "=" in line and not line.startswith(("#", "[")):
            key, value = line.split("=", 1)
            values.setdefault(key, []).append(value)
    return values


def test_terraform_renders_the_boot_script_from_its_named_placeholders_only() -> None:
    main = (TERRAFORM / "main.tf").read_text(encoding="utf-8")
    passed = re.search(
        r'templatefile\("\$\{path\.module\}/templates/startup\.sh\.tftpl", \{\n(.*?)\n    \}\)',
        main,
        re.S,
    )

    assert set(re.findall(r"\$\{([^}]*)\}", TEMPLATE)) == PLACEHOLDERS
    assert "%{" not in TEMPLATE
    assert passed
    assert set(re.findall(r"^      ([a-z_0-9]+)\s*=", passed[1], re.M)) == PLACEHOLDERS
    assert re.search(r"^      data_device_name\s+= local\.data_device_name$", passed[1], re.M)
    assert re.search(r"^    device_name = local\.data_device_name$", main, re.M)


def test_an_unsupported_host_is_refused_before_anything_changes() -> None:
    check = TEMPLATE.index("if [[ $ID != ubuntu || $VERSION_ID != 24.04 ||")

    assert ". /etc/os-release\n" in TEMPLATE
    assert "$(dpkg --print-architecture) != amd64" in TEMPLATE
    assert check < TEMPLATE.index("write_if_changed /")
    assert check < TEMPLATE.index("install -d")
    assert check < TEMPLATE.index("apt-get")


def test_the_host_configuration_is_written_before_packages_and_only_when_it_changed() -> None:
    written = TEMPLATE.index('write_if_changed /etc/pyracast/host.json 0600 <<<"$host_json"')

    assert "host_json=$(printf '%s' '${host_config_base64}' | base64 --decode)" in TEMPLATE
    assert written < TEMPLATE.index("apt-get")
    assert ">/etc/pyracast/host.json" not in TEMPLATE
    assert not re.search(r"cat >/(etc|usr)/", TEMPLATE)


def test_files_are_replaced_atomically_and_only_when_their_content_differs() -> None:
    function = TEMPLATE[TEMPLATE.index("write_if_changed() {") :]
    function = function[: function.index("\n}\n")]

    assert 'if [[ -f $1 && $(cat "$1") == "$content" ]]; then' in function
    assert 'staged=$(mktemp "$1.XXXXXX")' in function
    assert 'mv -f "$staged" "$1"' in function
    assert "changed=1" in function


def test_packages_are_installed_only_while_a_pinned_version_is_missing() -> None:
    install = block("if ! pinned_packages_installed; then")
    outside = TEMPLATE.replace(install, "")

    assert "apt-get" not in outside
    assert "docker-ce=5:29.9.3-1~ubuntu.24.04~noble" in TEMPLATE
    assert install.index("apt-get update") < install.index("apt-get install")
    assert "apt-mark hold docker-ce docker-ce-cli containerd.io docker-compose-plugin" in install


def test_the_docker_repository_key_is_trusted_only_with_its_published_fingerprint() -> None:
    install = block("if ! pinned_packages_installed; then")
    verified = install.index("!= 9DC858229FC7DD38854AE2D88D81803C0EBFCD88 ]]; then")

    assert "gpg --show-keys --with-colons" in install
    assert verified < install.index("/etc/apt/keyrings/docker.asc")


def test_docker_restarts_only_for_its_own_changed_configuration_while_no_release_runs() -> None:
    restart = block("if (( docker_changed )); then")

    assert TEMPLATE.count("systemctl restart") == 1
    assert "systemctl restart docker.service" in restart
    lock = host.RELEASE_LOCK.as_posix()
    assert restart.index(f"flock --nonblock {lock} true") < restart.index("systemctl restart")
    assert "restart pyracast" not in TEMPLATE


def test_systemd_is_reloaded_only_after_a_unit_or_the_mount_table_changed() -> None:
    assert TEMPLATE.count("systemctl daemon-reload") == 1
    assert "if (( units_changed || docker_changed || mounts_changed )); then" in TEMPLATE


def test_the_data_disk_mounts_without_blocking_boot_and_services_still_require_it() -> None:
    entry = 'entry="UUID=$data_uuid /var/lib/pyracast ext4 defaults,nodev,nosuid,nofail 0 2"'

    assert entry in TEMPLATE
    assert '!($1 == uuid && $2 == "/var/lib/pyracast")' in TEMPLATE
    assert unit("/etc/systemd/system/docker.service.d/pyracast-data.conf")["RequiresMountsFor"] == [
        "/var/lib/pyracast"
    ]
    assert unit("/etc/systemd/system/pyracast.service")["RequiresMountsFor"] == [
        "/var/lib/pyracast"
    ]


def test_only_a_disk_without_any_signature_and_with_a_zeroed_head_is_formatted() -> None:
    formatting = block("if [[ -z $filesystem ]]; then")

    assert "wipefs --no-act --noheadings --output TYPE" in formatting
    assert 'cmp --silent --bytes=1048576 "$data_device" /dev/zero' in formatting
    assert formatting.index("cmp --silent") < formatting.index("mkfs.ext4")


def test_the_application_unit_retries_a_failed_start_within_a_start_limit() -> None:
    application = unit("/etc/systemd/system/pyracast.service")

    assert application["Restart"] == ["on-failure"]
    assert application["RestartSec"] == ["30s"]
    assert application["StartLimitIntervalSec"] == ["900"]
    assert application["StartLimitBurst"] == ["5"]
    assert int(application["TimeoutStartSec"][0]) >= runtime_secrets.RETRY_SECONDS + 180 + 30


def test_a_failure_names_the_line_it_stopped_at() -> None:
    assert "trap 'echo \"Pyracast boot configuration stopped at line $LINENO" in TEMPLATE
