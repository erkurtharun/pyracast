#!/usr/bin/env bash
set -euo pipefail
[[ $# == 3 ]] || { echo 'Usage: smoke.sh BACKEND_IMAGE WEB_IMAGE GATEWAY_IMAGE' >&2; exit 2; }
export BACKEND_IMAGE=$1 WEB_IMAGE=$2 GATEWAY_IMAGE=$3
root=$(cd "$(dirname "$0")/.." && pwd)
temporary=$(mktemp -d)
cleanup() {
  result=$?
  if (( result != 0 )); then "${compose[@]}" logs --tail 60; fi
  "${compose[@]}" down --volumes --remove-orphans --timeout 30
  rm -rf -- "$temporary"
  exit "$result"
}
export SITE_ADDRESS=localhost ACME_EMAIL=smoke@example.com CLOUD_SQL_CONNECTION_NAME=smoke:local:test
export HTTP_BIND=127.0.0.1:18080 HTTPS_BIND=127.0.0.1:18443
export RUNTIME_ENV_FILE="$temporary/runtime.env" BROKER_ENV_FILE="$temporary/broker.env"
export MIGRATION_ENV_FILE="$temporary/migration.env" CADDY_CONFIG_FILE="$temporary/Caddyfile"
cat > "$RUNTIME_ENV_FILE" <<'ENV'
PYRACAST_SECRET_KEY=container-smoke-only-93e4628185e354a7b06c9d1202375a543cf34c69e8e3233c
PGUSER=pyracast_app
PGPASSWORD=smoke-app
PYRACAST_RABBITMQ_URL=amqp://pyracast:smoke-broker@rabbitmq:5672/%2F
PYRACAST_EMAIL_HOST=smtp.example.com
PYRACAST_FROM_EMAIL=smoke@example.com
PYRACAST_WEATHER_ENABLED=0
ENV
printf 'RABBITMQ_DEFAULT_USER=pyracast\nRABBITMQ_DEFAULT_PASS=smoke-broker\n' > "$BROKER_ENV_FILE"
printf 'PGUSER=pyracast_owner\nPGPASSWORD=smoke-owner\n' > "$MIGRATION_ENV_FILE"
cat > "$CADDY_CONFIG_FILE" <<'CADDY'
{
  local_certs
  skip_install_trust
  admin off
}
localhost {
  tls internal
  import /etc/caddy/routes.caddy
}
CADDY
if command -v cygpath >/dev/null; then
  RUNTIME_ENV_FILE=$(cygpath -m "$RUNTIME_ENV_FILE")
  BROKER_ENV_FILE=$(cygpath -m "$BROKER_ENV_FILE")
  MIGRATION_ENV_FILE=$(cygpath -m "$MIGRATION_ENV_FILE")
  CADDY_CONFIG_FILE=$(cygpath -m "$CADDY_CONFIG_FILE")
fi
compose=(docker compose -p pyracast-smoke -f "$root/deploy/compose.yaml" -f "$root/deploy/compose.smoke.yaml")
trap cleanup EXIT
"${compose[@]}" --profile release config --quiet
"${compose[@]}" run --rm --no-deps --user 0 --cap-add CHOWN --entrypoint sh gateway -c 'chown -R 10001:10001 /data'
"${compose[@]}" run --rm --no-deps --user 0 --cap-add CHOWN --entrypoint sh rabbitmq -c 'chown -R 999:999 /var/lib/rabbitmq'
"${compose[@]}" up -d --wait --wait-timeout 120 sqlproxy rabbitmq
"${compose[@]}" exec -T sqlproxy psql -v ON_ERROR_STOP=1 -U postgres -d pyracast \
  -c 'CREATE ROLE bootstrap_admin LOGIN NOSUPERUSER CREATEDB CREATEROLE; ALTER DATABASE pyracast OWNER TO bootstrap_admin;'
"${compose[@]}" exec -T sqlproxy psql -v ON_ERROR_STOP=1 -U bootstrap_admin -d pyracast < "$root/deploy/bootstrap.sql"
"${compose[@]}" exec -T sqlproxy psql -v ON_ERROR_STOP=1 -U postgres -d pyracast \
  -c "ALTER ROLE pyracast_owner PASSWORD 'smoke-owner'; ALTER ROLE pyracast_app PASSWORD 'smoke-app';"
"${compose[@]}" run --rm --no-deps migrate
"${compose[@]}" run --rm --no-deps migrate pyracast check --deploy --fail-level WARNING
"${compose[@]}" up -d --wait --wait-timeout 180
curl --fail --silent --show-error --insecure https://localhost:18443/ >/dev/null
curl --fail --silent --show-error --insecure https://localhost:18443/static/admin/css/base.css >/dev/null
[[ $(curl --fail --silent --show-error --insecure https://localhost:18443/health/ready/) == ok ]]
# Spoofed client headers must neither downgrade HTTPS nor break the trusted gateway contract.
[[ $(curl --fail --silent --show-error --insecure -H 'X-Real-IP: invalid' -H 'X-Forwarded-Proto: http' \
  -H 'Forwarded: for=invalid' https://localhost:18443/health/ready/) == ok ]]
"${compose[@]}" exec -T api python - <<'PY'
import os
import time
import django
django.setup()
from django.contrib.auth.models import User
from django.db import connection, ProgrammingError
from django.utils import timezone
from pyracast.accounts.models import Membership, Tenant
from pyracast.connections.operations import create_account, validate_binding
from pyracast.forecasts.models import ForecastResult
from pyracast.providers import FixtureProvider
from pyracast.regions.operations import approve_region, configure_binding, submit_region
from pyracast.runtime import make_actor, make_broker
assert os.getuid() != 0
with connection.cursor() as cursor:
    cursor.execute("SELECT has_database_privilege('pyracast_owner', current_database(), 'CREATE')")
    assert cursor.fetchone() == (False,)
try:
    with connection.cursor() as cursor:
        cursor.execute('CREATE TABLE forbidden_runtime_ddl (id int)')
except ProgrammingError:
    pass
else:
    raise SystemExit('Runtime role unexpectedly owns DDL permission')
actor = User.objects.create_superuser(username='container-smoke', email='smoke@example.com')
tenant = Tenant.objects.create(name='Container smoke')
Membership.objects.create(user=actor, tenant=tenant)
region = submit_region(actor, tenant.pk, name='Synthetic Ankara', latitude=39.9,
                       longitude=32.8, timezone='Europe/Istanbul')
account = create_account(actor, provider='fixture', external_account_id='container-smoke')
binding = configure_binding(actor, region.pk, provider_account_id=account.pk,
                            site_id='synthetic', capacity_kw=1000, dc_capacity_kw=1000,
                            capacity_semantics_verified=True)
validate_binding(actor, binding.pk, FixtureProvider(), timezone.now())
job = approve_region(actor, region.pk, binding.pk, timezone.now())
broker = make_broker()
try:
    make_actor(broker).send(job.pk)
finally:
    broker.close()
deadline = time.monotonic() + 90
while time.monotonic() < deadline:
    job.refresh_from_db()
    if job.status == 'succeeded':
        break
    time.sleep(0.5)
assert job.status == 'succeeded', f'Fixture worker job status: {job.status}'
assert ForecastResult.objects.filter(snapshot__job=job).count() == 1
PY
"${compose[@]}" exec -T api pyracast dispatch
"${compose[@]}" stop -t 30 dispatcher worker notifications
echo 'Production containers, TLS routing, static assets, database roles, fixture queue publication and graceful idle shutdown passed.'
