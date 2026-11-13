from pathlib import Path

import pytest

from deploy.runtime_secrets import environment_values, prepare_migration, prepare_runtime


@pytest.mark.parametrize(
    "payload", [{}, [], {"BAD=KEY": "x"}, {"KEY": 1}, {"KEY": "secret\nPGUSER=admin"}]
)
def test_secret_rejects_invalid_environment_without_disclosing_payload(payload: object) -> None:
    with pytest.raises(ValueError, match="Secret") as error:
        environment_values(payload)
    assert "secret" not in str(error.value)


def test_runtime_separates_broker_credentials_and_preserves_literals(tmp_path: Path) -> None:
    prepare_runtime(
        {
            "PYRACAST_SECRET_KEY": "literal$VALUE#'value",
            "PGUSER": "runtime",
            "PGPASSWORD": "password",
            "RABBITMQ_DEFAULT_PASS": "broker@pass/$",
        },
        tmp_path,
    )
    runtime = (tmp_path / "runtime.env").read_text()
    broker = (tmp_path / "broker.env").read_text()
    assert "PYRACAST_SECRET_KEY=literal$VALUE#'value\n" in runtime
    assert "amqp://pyracast:broker%40pass%2F%24@rabbitmq:5672/%2F" in runtime
    assert "RABBITMQ_DEFAULT_PASS" not in runtime
    assert "PGPASSWORD" not in broker
    assert "PYRACAST_SECRET_KEY" not in broker


def test_runtime_rejects_missing_credentials(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing required"):
        prepare_runtime({"PGUSER": "runtime"}, tmp_path)
    assert not list(tmp_path.iterdir())


def test_migration_secret_cannot_override_application_configuration(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="only PGUSER and PGPASSWORD"):
        prepare_migration({"PGUSER": "owner", "PGPASSWORD": "pw", "PYRACAST_DEBUG": "1"}, tmp_path)
    prepare_migration({"PGUSER": "owner", "PGPASSWORD": "pw"}, tmp_path)
    assert (tmp_path / "migration.env").read_text() == "PGPASSWORD=pw\nPGUSER=owner\n"
