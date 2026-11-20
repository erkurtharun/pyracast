#!/usr/bin/env bash
set -euo pipefail
umask 077
[[ $# == 5 ]] || { echo 'Usage: smoke.sh BACKEND_IMAGE WEB_IMAGE GATEWAY_IMAGE POSTGRES_IMAGE RABBITMQ_IMAGE' >&2; exit 2; }
export BACKEND_IMAGE=$1 WEB_IMAGE=$2 GATEWAY_IMAGE=$3 POSTGRES_IMAGE=$4 RABBITMQ_IMAGE=$5
root=$(cd "$(dirname "$0")/.." && pwd)
temporary=$(mktemp -d)
cleanup() {
  result=$?
  if (( result != 0 )); then "${compose[@]}" logs --tail 60; fi
  "${compose[@]}" down --volumes --remove-orphans --timeout 30
  rm -rf -- "$temporary"
  exit "$result"
}
export SITE_ADDRESS=localhost ACME_EMAIL=smoke@example.com
export HTTP_BIND=127.0.0.1:18080 HTTPS_BIND=127.0.0.1:18443 LOCAL_HTTPS_BIND=127.0.0.1:18444
export RUNTIME_ENV_FILE="$temporary/runtime.env" BROKER_ENV_FILE="$temporary/broker.env"
export MIGRATION_ENV_FILE="$temporary/migration.env" DATABASE_ENV_FILE="$temporary/database.env"
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
printf 'POSTGRES_PASSWORD=smoke-bootstrap-only\n' > "$DATABASE_ENV_FILE"
printf 'PGUSER=pyracast_owner\nPGPASSWORD=smoke-owner\n' > "$MIGRATION_ENV_FILE"
if command -v cygpath >/dev/null; then
  RUNTIME_ENV_FILE=$(cygpath -m "$RUNTIME_ENV_FILE")
  BROKER_ENV_FILE=$(cygpath -m "$BROKER_ENV_FILE")
  MIGRATION_ENV_FILE=$(cygpath -m "$MIGRATION_ENV_FILE")
  DATABASE_ENV_FILE=$(cygpath -m "$DATABASE_ENV_FILE")
fi
compose=(docker compose -p pyracast-smoke -f "$root/deploy/compose.yaml" -f "$root/deploy/compose.smoke.yaml")
trap cleanup EXIT
"${compose[@]}" --profile release config --quiet
for volume in smoke-certificates smoke-gateway; do
  docker volume create --label com.docker.compose.project=pyracast-smoke \
    --label "com.docker.compose.volume=$volume" "pyracast-smoke_$volume" >/dev/null
done
"${compose[@]}" run --rm -T --no-deps --user 0 --cap-add CHOWN --entrypoint python \
  --volume pyracast-smoke_smoke-certificates:/certificates \
  --volume pyracast-smoke_smoke-gateway:/data api - > "$temporary/ca.pem" <<'PY'
import datetime
import ipaddress
import os
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

now = datetime.datetime.now(datetime.UTC)
ca_key = ec.generate_private_key(ec.SECP256R1())
ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Pyracast ephemeral smoke CA')])
ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
      .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
      .not_valid_before(now - datetime.timedelta(minutes=1))
      .not_valid_after(now + datetime.timedelta(days=1))
      .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
      .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False,
          key_encipherment=False, data_encipherment=False, key_agreement=False,
          key_cert_sign=True, crl_sign=True, encipher_only=None, decipher_only=None), critical=True)
      .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
      .sign(ca_key, hashes.SHA256()))
key = ec.generate_private_key(ec.SECP256R1())
certificate = (x509.CertificateBuilder()
      .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')]))
      .issuer_name(ca.subject).public_key(key.public_key())
      .serial_number(x509.random_serial_number())
      .not_valid_before(now - datetime.timedelta(minutes=1))
      .not_valid_after(now + datetime.timedelta(days=1))
      .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
      .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
          key_encipherment=False, data_encipherment=False, key_agreement=False,
          key_cert_sign=False, crl_sign=False, encipher_only=None, decipher_only=None), critical=True)
      .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
      .add_extension(x509.SubjectAlternativeName([
          x509.DNSName('localhost'), x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
      .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
      .sign(ca_key, hashes.SHA256()))
for name, content in (
    ('localhost.pem', certificate.public_bytes(serialization.Encoding.PEM)),
    ('localhost-key.pem', key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption())),
):
    path = Path('/certificates') / name
    path.write_bytes(content)
    path.chmod(0o400)
    os.chown(path, 10001, 10001)
os.chown('/data', 10001, 10001)
print(ca.public_bytes(serialization.Encoding.PEM).decode(), end='')
PY
"${compose[@]}" run --rm --no-deps --user 0 --cap-add CHOWN --entrypoint sh rabbitmq -c 'chown -R 999:999 /var/lib/rabbitmq'
"${compose[@]}" run --rm --no-deps --user 0 --cap-add CHOWN --entrypoint sh postgres -c 'chown -R 70:70 /var/lib/postgresql'
"${compose[@]}" up -d --wait --wait-timeout 120 postgres rabbitmq
mkdir -p "$temporary/deploy"
"${compose[@]}" config > "$temporary/deploy/compose.yaml"
: > "$temporary/deploy/release.env"
cp "$root/deploy/bootstrap.sql" "$temporary/deploy/bootstrap.sql"
bootstrap=(uv run --no-project --python 3.15.2 python "$root/deploy/bootstrap_database.py")
"${bootstrap[@]}" converge "$temporary" "$temporary"
"${bootstrap[@]}" converge "$temporary" "$temporary"
"${bootstrap[@]}" switch-runtime "$temporary" "$temporary"
runtime_connects() {
  "${compose[@]}" run --rm --no-deps api python -c \
    'import django; django.setup(); from django.db import connection; connection.ensure_connection()'
}
runtime_connects
# Password changes log nothing even while the server logs, samples and parses every statement.
superuser=("${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 -U postgres -d pyracast)
logging=(log_statement log_min_duration_statement log_transaction_sample_rate debug_print_parse)
"${superuser[@]}" -c "ALTER SYSTEM SET log_statement = 'all'" \
  -c 'ALTER SYSTEM SET log_min_duration_statement = 0' \
  -c 'ALTER SYSTEM SET log_transaction_sample_rate = 1' \
  -c 'ALTER SYSTEM SET debug_print_parse = on' -c 'SELECT pg_reload_conf()'
for attempt in {1..10}; do
  if [[ $("${superuser[@]}" -Atc 'SHOW log_statement') == all ]]; then break; fi
  (( attempt < 10 )) && sleep 1
done
"${superuser[@]}" -Atc "SELECT 'smoke-logged-probe'"
"${superuser[@]}" -Atc 'SELECT count(*) FROM pg_stat_statements' >/dev/null
mkdir "$temporary/rotated"
printf 'PGUSER=pyracast_app\nPGPASSWORD=smoke-rotated\n' > "$temporary/rotated/runtime.env"
printf 'PGUSER=pyracast_owner\nPGPASSWORD=smoke-owner-rotated\n' > "$temporary/rotated/migration.env"
"${bootstrap[@]}" converge "$temporary" "$temporary/rotated"
"${bootstrap[@]}" converge "$temporary" "$temporary"
"${bootstrap[@]}" switch-runtime "$temporary" "$temporary/rotated"
if runtime_connects; then exit 1; fi
"${bootstrap[@]}" switch-runtime "$temporary" "$temporary"
for name in "${logging[@]}"; do "${superuser[@]}" -c "ALTER SYSTEM RESET $name"; done
"${superuser[@]}" -c 'SELECT pg_reload_conf()'
runtime_connects
database_log=$("${compose[@]}" logs --no-log-prefix postgres)
[[ $database_log == *smoke-logged-probe* ]]
[[ $database_log != *SCRAM-SHA-256* && $database_log != *smoke-rotated* ]]
[[ $database_log != *smoke-owner-rotated* && $database_log != *'ALTER ROLE pyracast_'* ]]
"${compose[@]}" run --rm --no-deps migrate
"${bootstrap[@]}" converge "$temporary" "$temporary"
"${compose[@]}" run --rm --no-deps migrate pyracast check --deploy --fail-level WARNING
no_broker=("${compose[@]}" run --rm -T --no-deps --env PYRACAST_RABBITMQ_URL=)
if "${no_broker[@]}" migrate pyracast check --deploy --fail-level WARNING; then exit 1; fi
refused=0
missing_broker=$("${no_broker[@]}" worker 2>&1) || refused=$?
[[ $refused != 0 && $missing_broker == *PYRACAST_RABBITMQ_URL* ]]
"${compose[@]}" up -d --wait --wait-timeout 180
# The ephemeral CA has no revocation service; Schannel still verifies its chain and hostname.
tls_curl=(curl --silent --show-error --ssl-revoke-best-effort --cacert "$temporary/ca.pem")
secure_curl=("${tls_curl[@]}" --fail)
"${secure_curl[@]}" https://localhost:18443/ >/dev/null
untrusted_status=0
curl --silent --ssl-revoke-best-effort --output /dev/null https://localhost:18443/ || untrusted_status=$?
[[ $untrusted_status == 60 ]]
"${secure_curl[@]}" https://localhost:18443/static/admin/css/base.css >/dev/null
[[ $("${secure_curl[@]}" https://localhost:18443/health/ready/) == ok ]]
[[ $("${secure_curl[@]}" https://localhost:18444/health/ready/) == ok ]]
# The gateway owns the transport headers; each appears once with exactly this value.
exact_headers() {
  local headers expected
  headers=$("${secure_curl[@]}" --output /dev/null --dump-header - "https://localhost:18443$1")
  headers=${headers,,}
  headers=${headers//$'\r'/}
  for expected in \
    'strict-transport-security: max-age=31536000' \
    'x-content-type-options: nosniff' \
    'permissions-policy: camera=(), microphone=(), geolocation=()' \
    'cross-origin-opener-policy: same-origin' \
    'referrer-policy: same-origin'; do
    [[ $(grep -c "^${expected%%:*}:" <<<"$headers") == 1 ]]
    grep -qxF "$expected" <<<"$headers"
  done
  if grep -q '^server:' <<<"$headers"; then return 1; fi
}
exact_headers /
exact_headers /app/
exact_headers /api/session/
exact_headers /admin/login/
exact_headers /health/live/
[[ $(curl --silent --show-error --output /dev/null --write-out '%{redirect_url}' \
  http://localhost:18080/) == https://localhost:18443/ ]]
# Each router refuses one byte over its body limit; the API answers with its error body.
too_large() {
  head -c "$1" /dev/zero | "${tls_curl[@]}" --data-binary @- --output "$temporary/too-large" \
    --write-out '%{http_code}' "https://localhost:18443$2"
}
[[ $(too_large 2621441 /api/session/) == 413 ]]
[[ $(<"$temporary/too-large") == '{"code": "request_too_large", "message": "The request is too large to be accepted."}' ]]
[[ $(too_large 2621441 /api/auth/browser/v1/config) == 413 ]]
[[ $(<"$temporary/too-large") == '{"code": "request_too_large", "message": "The request is too large to be accepted."}' ]]
[[ $(too_large 8388609 /admin/login/) == 413 ]]
[[ $(too_large 1048577 /health/ready/) == 413 ]]
[[ $(too_large 1048577 /) == 413 ]]
[[ $("${secure_curl[@]}" -H 'X-Real-IP: invalid' -H 'X-Forwarded-Proto: http' \
  -H 'X-Forwarded-For: invalid' -H 'Forwarded: for=invalid' https://localhost:18443/health/ready/) == ok ]]
[[ $("${secure_curl[@]}" -H 'X_Real_IP: invalid' -H 'X_Forwarded_Proto: http' \
  -H 'X_Forwarded_For: invalid' https://localhost:18443/health/ready/) == ok ]]
"${secure_curl[@]}" -H 'X-Real-IP: invalid' -H 'X_Real_IP: invalid' \
  -H 'X.Real.IP: invalid' -H 'X-Pyracast-Client-IP: 127.0.0.1' \
  -H 'X-Forwarded-Proto: http' -H 'X_Forwarded_Proto: http' \
  -H 'X.Forwarded.Proto: http' \
  -H 'X-Forwarded-For: invalid' -H 'X_Forwarded_For: invalid' \
  -H 'X.Forwarded.For: invalid' \
  -H 'Forwarded: for=invalid' https://localhost:18443/__smoke/headers \
  | "${compose[@]}" exec -T api python -c '
import ipaddress, json, sys
headers = {key.lower(): value for key, value in json.load(sys.stdin).items()}
assert headers["x-forwarded-proto"] == "https", headers
ipaddress.ip_address(headers["x-real-ip"])
for address in headers["x-forwarded-for"].split(","):
    ipaddress.ip_address(address.strip())
assert not {"forwarded", "x_real_ip", "x_forwarded_proto", "x_forwarded_for",
            "x.real.ip", "x.forwarded.proto", "x.forwarded.for",
            "x-pyracast-client-ip"} & headers.keys(), headers
'
# The internal network gives the probe an actual socket peer in a trusted Cloudflare range.
cloudflare_headers=$("${compose[@]}" exec -T --env "SMOKE_CA=$(cat "$temporary/ca.pem")" header-probe python - <<'PY'
import http.client
import os
import socket
import ssl
context = ssl.create_default_context(cadata=os.environ['SMOKE_CA'])
with socket.create_connection(('173.245.48.2', 8443), timeout=10) as connection:
    with context.wrap_socket(connection, server_hostname='localhost') as secure:
        secure.sendall(b'GET /__smoke/headers HTTP/1.1\r\nHost: localhost\r\n'
                       b'X-Forwarded-For: 127.0.0.1, 198.51.100.7\r\n'
                       b'X-Real-IP: 127.0.0.1\r\nCF-Connecting-IP: 127.0.0.1\r\n'
                       b'Connection: close\r\n\r\n')
        response = http.client.HTTPResponse(secure)
        response.begin()
        assert response.status == 200, response.status
        print(response.read().decode())
PY
)
printf '%s' "$cloudflare_headers" | "${compose[@]}" exec -T api python -c '
import json
import sys
import django
django.setup()
from django.http import HttpResponse
from django.test import RequestFactory
from pyracast.proxy import TrustedProxyHeaders
headers = {key.lower(): value for key, value in json.load(sys.stdin).items()}
assert headers["x-forwarded-for"].split(",")[-1].strip() == "173.245.48.3", headers
request = RequestFactory().get("/", REMOTE_ADDR="172.30.80.2", **{
    "HTTP_" + key.upper().replace("-", "_"): value for key, value in headers.items()})
TrustedProxyHeaders(lambda request: HttpResponse())(request)
assert request.META["HTTP_X_PYRACAST_CLIENT_IP"] == "198.51.100.7", request.META
assert "HTTP_CF_CONNECTING_IP" not in request.META
'
# Each router keeps its own rate and in-flight budget for each client, keyed by the edge's address;
# a held request is one whose body never arrives.
"${compose[@]}" exec -T --env "SMOKE_CA=$(cat "$temporary/ca.pem")" \
  --env RATE_AVERAGE=20 --env RATE_BURST=100 --env SIGN_IN_AVERAGE=0.5 --env SIGN_IN_BURST=20 --env IN_FLIGHT=32 header-probe python - <<'PY'
import http.client
import os
import socket
import ssl
import time
context = ssl.create_default_context(cadata=os.environ['SMOKE_CA'])
ROUTERS = {
    'backend': '/health/live/',
    'api': '/api/session/',
    'authentication': '/api/auth/browser/v1/config',
    'admin': '/admin',
    'frontend': '/robots.txt',
}

def connect():
    connection = http.client.HTTPSConnection('localhost', 8443, timeout=10)
    connection.sock = context.wrap_socket(
        socket.create_connection(('173.245.48.2', 8443), timeout=10), server_hostname='localhost')
    return connection

def statuses(path, client, count):
    connection = connect()
    seen = []
    for _ in range(count):
        connection.request('GET', path, headers={'Host': 'localhost', 'X-Forwarded-For': client})
        response = connection.getresponse()
        response.read()
        seen.append(response.status)
        if response.will_close:
            connection.close()
            connection = connect()
    connection.close()
    return seen

def admitted_before_refusal(path, client):
    connection = connect()
    admitted = 0
    started = time.monotonic()
    while True:
        connection.request('GET', path, headers={'Host': 'localhost', 'X-Forwarded-For': client})
        response = connection.getresponse()
        response.read()
        if response.status == 429:
            break
        assert response.status < 500 and admitted < 1000, (path, response.status, admitted)
        admitted += 1
        if response.will_close:
            connection.close()
            connection = connect()
    connection.close()
    return admitted, time.monotonic() - started

for router, path in ROUTERS.items():
    prefix = 'SIGN_IN' if router == 'authentication' else 'RATE'
    burst, average = int(os.environ[f'{prefix}_BURST']), float(os.environ[f'{prefix}_AVERAGE'])
    admitted, elapsed = admitted_before_refusal(path, '198.51.100.21')
    assert burst <= admitted <= burst + average * elapsed + 1, (router, admitted, elapsed)
assert statuses('/health/live/', '198.51.100.22', 1) == [200]

def held(path, client):
    secure = context.wrap_socket(
        socket.create_connection(('173.245.48.2', 8443), timeout=10), server_hostname='localhost')
    secure.sendall(f'POST {path} HTTP/1.1\r\nHost: localhost\r\nX-Forwarded-For: {client}\r\n'
                   'Content-Type: application/json\r\nContent-Length: 2\r\n\r\n'.encode())
    return secure

def eventually(path, client, refused):
    deadline = time.monotonic() + 10
    while ((status := statuses(path, client, 1)[0]) == 429) != refused and time.monotonic() < deadline:
        time.sleep(0.1)
    return status

# The authentication router's rate refuses one client before its in-flight budget fills.
held_paths = [path for router, path in ROUTERS.items() if router != 'authentication']
holding = []
for path in held_paths:
    assert statuses(path, '198.51.100.25', 1) != [429], path
    holding.append([held(path, '198.51.100.25') for _ in range(int(os.environ['IN_FLIGHT']))])
    assert eventually(path, '198.51.100.25', refused=True) == 429, path
assert statuses('/api/session/', '198.51.100.26', 1) == [200]
for path, sockets in zip(held_paths, holding):
    sockets.pop().close()
    assert eventually(path, '198.51.100.25', refused=False) != 429, path
    for secure in sockets:
        secure.close()
PY
[[ $(docker inspect --format '{{.Config.User}}' "$("${compose[@]}" ps -q gateway)") == 10001:10001 ]]
"${compose[@]}" exec -T api python -c '
import django
django.setup()
from django.utils import translation
with translation.override("tr"):
    assert translation.gettext("Continue to sign in") == "Oturum a\u00e7maya devam et"
'
"${compose[@]}" exec -T api python - <<'PY'
import os
import time
import django
import psycopg
django.setup()
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.db import connection, ProgrammingError
from django.utils import timezone
from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.connections.operations import create_account
from pyracast.forecasts.validation import validate_binding
from pyracast.forecasts.models import ForecastResult
from pyracast.providers import FixtureProvider
from pyracast.regions.operations import approve_region, configure_binding, submit_region
from pyracast.background.broker import make_broker
from pyracast.forecasts.runtime import make_actor
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
try:
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO django_migrations (app, name, applied) VALUES ('smoke', 'smoke', now())")
except ProgrammingError as error:
    assert getattr(error.__cause__, 'sqlstate', None) == '42501', error
else:
    raise SystemExit('Runtime role can write the migration ledger')
try:
    psycopg.connect(host='postgres', dbname='pyracast', user='postgres',
                    password='smoke-bootstrap-only', connect_timeout=5).close()
except psycopg.OperationalError as error:
    assert 'no pg_hba.conf entry' in str(error), error
else:
    raise SystemExit('The PostgreSQL superuser can sign in over TCP')
actor = User.objects.create_superuser(username='container-smoke', email='smoke@example.com',
                                      password='container-smoke-password')
EmailAddress.objects.create(user=actor, email=actor.email, primary=True, verified=True)
tenant = Tenant.objects.create(name='Container smoke')
Membership.objects.create(user=actor, tenant=tenant, role=WorkspaceRole.ADMIN)
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
cookies="$temporary/cookies"
csrf_token=$("${secure_curl[@]}" --cookie-jar "$cookies" https://localhost:18443/api/session/ \
  | "${compose[@]}" exec -T api python -c '
import json, sys
import django
django.setup()
from pyracast.accounts.language import LANGUAGES
session = json.load(sys.stdin)
assert set(session) == {"user", "csrf_token", "language", "expires_at"}, session
assert session["user"] is None and session["language"] in LANGUAGES, session
assert session["expires_at"] is None, session
assert isinstance(session["csrf_token"], str) and session["csrf_token"], session
print(session["csrf_token"])
')
[[ $("${tls_curl[@]}" --cookie "$cookies" --cookie-jar "$cookies" \
  -H "X-CSRFToken: $csrf_token" -H 'Origin: https://localhost:18443' \
  --referer https://localhost:18443/app/ -H 'Content-Type: application/json' \
  --data '{"email":"smoke@example.com","password":"not-the-container-smoke-password"}' \
  --output "$temporary/sign-in.json" --write-out '%{http_code}' \
  https://localhost:18443/api/auth/browser/v1/auth/login) == 400 ]]
"${compose[@]}" exec -T api python -c '
import json, sys
errors = json.load(sys.stdin)["errors"]
assert [error["code"] for error in errors] == ["email_password_mismatch"], errors
' < "$temporary/sign-in.json"
[[ $("${secure_curl[@]}" https://localhost:18443/app/) == *'<title>Pyracast workspace'* ]]
uv run --no-project --python 3.15.2 python - "$temporary" "$root" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from deploy.backup import dump_database, restore_archive
release = Path(sys.argv[1])
dump_database(release, release / 'database.dump')
restore_archive(release, release / 'database.dump', 'local-smoke')
PY
"${compose[@]}" exec -T api pyracast dispatch
"${compose[@]}" stop -t 30 dispatcher
"${compose[@]}" stop -t 30 worker notifications
# Each drained and exited by itself; a container stopped by SIGKILL exits with 137.
for service in dispatcher worker notifications; do
  [[ $(docker inspect --format '{{.State.ExitCode}}' "$("${compose[@]}" ps -aq "$service")") == 0 ]]
done

# The smoke substitutes bucket upload, runtime secrets, systemctl, public readiness and Windows locking.
"${compose[@]}" exec -T api python - <<'PY'
import django
django.setup()
from django.contrib.auth.models import User
User.objects.create_user(username='reset-smoke', email='reset-smoke@example.com',
                         password='reset-smoke-sign-in-4c1e9a')
PY
cat > "$temporary/reset_tool.py" <<'PY'
import contextlib
import importlib.util
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from deploy import reset_database
temporary = Path(sys.argv[2])

def upload(_bucket, name, archive):
    target = temporary / 'bucket' / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(archive.read_bytes())

def site_ready(_host, attempts):
    result = subprocess.run(
        ['curl', '--silent', '--show-error', '--fail', '--ssl-revoke-best-effort',
         '--cacert', str(temporary / 'ca.pem'), '--retry', str(attempts), '--retry-delay', '2',
         '--retry-all-errors', 'https://localhost:18443/health/ready/'],
        capture_output=True, text=True, check=False)
    return result.returncode == 0 and result.stdout.strip() == 'ok'

reset_database.upload_backup = upload
reset_database.site_ready = site_ready
reset_database.write_runtime_secrets = lambda _host: None
run = subprocess.run
subprocess.run = lambda command, *args, **kwargs: (
    subprocess.CompletedProcess(command, 0) if command[0] == 'systemctl'
    else run(command, *args, **kwargs))
if importlib.util.find_spec('fcntl') is None:
    reset_database.release_lock = lambda _host: contextlib.nullcontext()
reset_database.load_host = lambda config: reset_database.Host(
    config=config, release=temporary, bucket='smoke-backups', runtime_version='smoke',
    marker=temporary / 'release-blocked', lock=temporary / 'release.lock',
    state=temporary / 'reset', environment=temporary)
raise SystemExit(reset_database.main([str(temporary / 'host.json'), *sys.argv[3:]]))
PY
reset_tool=(uv run --no-project --python 3.15.2 python "$temporary/reset_tool.py" "$root" "$temporary")
application_running() {
  "${compose[@]}" ps --services --status running api web dispatcher worker notifications
}
ready() { [[ $("${secure_curl[@]}" https://localhost:18443/health/ready/) == ok ]]; }
"${reset_tool[@]}" prepare
[[ -f "$temporary/release-blocked" ]]
[[ -z $(application_running) ]]
[[ $("${tls_curl[@]}" -H 'Accept: text/html' --dump-header "$temporary/notice.headers" \
  --output "$temporary/notice.html" --write-out '%{http_code}' https://localhost:18443/app/) == 503 ]]
grep -qi '^retry-after: 60' "$temporary/notice.headers"
grep -qF '<title>Pyracast maintenance</title>' "$temporary/notice.html"
[[ $("${tls_curl[@]}" -H 'Accept-Language: en' --output "$temporary/notice.json" \
  --write-out '%{http_code}' https://localhost:18443/api/session/) == 503 ]]
[[ $(<"$temporary/notice.json") == '{"code": "maintenance", "message": "Pyracast is being updated. Try again in a minute; your data is safe.", "retry_after": 60}' ]]
[[ $("${tls_curl[@]}" -H 'Accept-Language: tr' https://localhost:18443/api/session/) == *'"message": "Pyracast g\u00fcncelleniyor.'* ]]
"${reset_tool[@]}" abort
[[ ! -e "$temporary/release-blocked" ]]
ready
"${reset_tool[@]}" prepare | tee "$temporary/prepare.log"
object=$(sed -n 's|^  Object:  gs://smoke-backups/||p' "$temporary/prepare.log")
[[ $object == postgres/*-prereset-*.dump ]]
cmp "$temporary/bucket/$object" "$temporary/reset/${object##*/}"
solcast=${object%.dump}.solcast.jsonl.gz
grep -qxF "  Solcast export: gs://smoke-backups/$solcast" "$temporary/prepare.log"
grep -qxF '  Solcast snapshots: 0' "$temporary/prepare.log"
cmp "$temporary/bucket/$solcast" "$temporary/reset/${solcast##*/}"
[[ -z $(gzip -dc "$temporary/reset/${solcast##*/}") ]]
"${reset_tool[@]}" reset --confirm "$object"
[[ -f "$temporary/release-blocked" ]]
refused=0
"${reset_tool[@]}" abort || refused=$?
[[ $refused == 2 ]]
[[ $("${compose[@]}" exec -T postgres psql -X -U postgres -d pyracast -Atc \
  "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'") == 0 ]]
# The installer's pre-release dump and migrate run from zero under the restricted owner.
"${compose[@]}" exec -T postgres pg_dump -U postgres -d pyracast --format=custom --compress=zstd:3 > "$temporary/empty.dump"
[[ -s "$temporary/empty.dump" ]]
"${compose[@]}" run --rm --no-deps migrate
"${compose[@]}" run --rm --no-deps migrate pyracast migrate --check
"${compose[@]}" up -d --wait --wait-timeout 180
ready
"${compose[@]}" exec -T api python - <<'PY'
import django
django.setup()
from django.db import connection, ProgrammingError
from pyracast.forecasts.models import ForecastResult
assert not ForecastResult.objects.exists()
try:
    with connection.cursor() as cursor:
        cursor.execute('CREATE TABLE forbidden_runtime_ddl (id int)')
except ProgrammingError:
    pass
else:
    raise SystemExit('Runtime role unexpectedly owns DDL permission after the reset')
PY
# A release that fails after maintenance began leaves the application stopped; restore recovers it.
"${compose[@]}" stop -t 660 dispatcher
"${compose[@]}" stop -t 660 worker notifications
"${compose[@]}" stop -t 100 api web
"${reset_tool[@]}" restore --confirm "$object"
[[ ! -e "$temporary/release-blocked" ]]
ready
"${compose[@]}" exec -T api python - <<'PY'
import django
django.setup()
from django.contrib.auth.models import User
from django.db import connection, ProgrammingError
from pyracast.forecasts.models import ForecastResult
assert User.objects.get(username='reset-smoke').check_password('reset-smoke-sign-in-4c1e9a')
assert ForecastResult.objects.exists()
with connection.cursor() as cursor:
    cursor.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public' "
                   "AND tableowner <> 'pyracast_owner'")
    assert cursor.fetchone() == (0,)
try:
    with connection.cursor() as cursor:
        cursor.execute('CREATE TABLE forbidden_runtime_ddl (id int)')
except ProgrammingError:
    pass
else:
    raise SystemExit('Runtime role unexpectedly owns DDL permission after the restore')
PY
# Authenticated Origin Pulls: once the host names the edge's authority, a handshake needs its certificate.
"${compose[@]}" run --rm -T --no-deps --entrypoint python api - > "$temporary/edge.json" <<'PY'
import datetime
import json
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

now = datetime.datetime.now(datetime.UTC)

def issue(name):
    authority_key = ec.generate_private_key(ec.SECP256R1())
    authority_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f'{name} CA')])
    authority = (x509.CertificateBuilder().subject_name(authority_name).issuer_name(authority_name)
          .public_key(authority_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - datetime.timedelta(minutes=1))
          .not_valid_after(now + datetime.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .sign(authority_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    certificate = (x509.CertificateBuilder()
          .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
          .issuer_name(authority.subject).public_key(key.public_key())
          .serial_number(x509.random_serial_number())
          .not_valid_before(now - datetime.timedelta(minutes=1))
          .not_valid_after(now + datetime.timedelta(days=1))
          .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
          .sign(authority_key, hashes.SHA256()))
    return (authority.public_bytes(serialization.Encoding.PEM).decode(),
            certificate.public_bytes(serialization.Encoding.PEM).decode(),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()).decode())

authority, certificate, key = issue('Pyracast smoke edge')
_, stranger_certificate, stranger_key = issue('Pyracast smoke stranger')
print(json.dumps({'authority': authority, 'certificate': certificate, 'key': key,
                  'stranger_certificate': stranger_certificate, 'stranger_key': stranger_key}))
PY
for part in authority certificate key stranger_certificate stranger_key; do
  jq -er ".$part" "$temporary/edge.json" > "$temporary/edge-$part.pem"
done
chmod 644 "$temporary/edge-authority.pem"
edge_authority="$temporary/edge-authority.pem"
if command -v cygpath >/dev/null; then edge_authority=$(cygpath -m "$edge_authority"); fi
edge=(--cert "$temporary/edge-certificate.pem" --key "$temporary/edge-key.pem")
stranger=(--cert "$temporary/edge-stranger_certificate.pem" --key "$temporary/edge-stranger_key.pem")
# curl reports a handshake the gateway refuses as 35 (connect) or 56 (receive), never as an HTTP error.
refused_handshake() {
  local status=0
  "${secure_curl[@]}" --http1.1 --output /dev/null "$@" https://localhost:18443/health/ready/ 2>/dev/null \
    || status=$?
  [[ $status == 35 || $status == 56 ]]
}
ORIGIN_PULL=required ORIGIN_PULL_CA="$edge_authority" \
  "${compose[@]}" up -d --no-deps --wait --wait-timeout 120 gateway
[[ $("${secure_curl[@]}" --retry 10 --retry-delay 1 --retry-all-errors \
  "${edge[@]}" https://localhost:18443/health/ready/) == ok ]]
refused_handshake
refused_handshake "${stranger[@]}"
[[ $("${secure_curl[@]}" "${edge[@]}" https://localhost:18443/health/ready/) == ok ]]
# Host-local probes keep working without the edge's certificate on the loopback entrypoint.
[[ $("${secure_curl[@]}" https://localhost:18444/health/ready/) == ok ]]
probe_origin_pull=(uv run --no-project --python 3.15.2 python
  "$root/deploy/probe_origin_pull.py" localhost --port 18443 --cafile "$temporary/ca.pem")
"${probe_origin_pull[@]}" --attempts 1
# An authority Traefik cannot load leaves the public routers unbuilt; only the loopback one serves.
printf -- '-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n' > "$temporary/edge-invalid.pem"
chmod 644 "$temporary/edge-invalid.pem"
invalid_authority="$temporary/edge-invalid.pem"
if command -v cygpath >/dev/null; then invalid_authority=$(cygpath -m "$invalid_authority"); fi
ORIGIN_PULL=required ORIGIN_PULL_CA="$invalid_authority" \
  "${compose[@]}" up -d --no-deps --wait --wait-timeout 120 gateway
[[ $("${secure_curl[@]}" --retry 10 --retry-delay 1 --retry-all-errors \
  https://localhost:18444/health/ready/) == ok ]]
refused=0
observed=$("${probe_origin_pull[@]}" --attempts 3 --interval 1 2>&1) || refused=$?
[[ $refused == 1 && $observed == *'closed the connection during the handshake'* ]]
refused_handshake "${edge[@]}"
"${compose[@]}" up -d --no-deps --wait --wait-timeout 120 gateway
ready
echo 'Production containers, TLS routing, exact gateway headers, body, rate and in-flight limits, static assets, Turkish catalogs, session and sign-in API, workspace app shell, database roles and access rules, fixture queue publication, the broker URL refusal, graceful idle shutdown with a clean exit, the maintenance notice, database reset and restore, and Authenticated Origin Pulls with an accepted and a rejected authority passed.'
