#!/usr/bin/env bash
set -euo pipefail
umask 077
[[ $# == 4 ]] || { echo 'Usage: smoke.sh BACKEND_IMAGE WEB_IMAGE GATEWAY_IMAGE POSTGRES_IMAGE' >&2; exit 2; }
export BACKEND_IMAGE=$1 WEB_IMAGE=$2 GATEWAY_IMAGE=$3 POSTGRES_IMAGE=$4
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
export HTTP_BIND=127.0.0.1:18080 HTTPS_BIND=127.0.0.1:18443
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
uv run --no-project --python 3.15.2 python "$root/deploy/bootstrap_database.py" "$temporary" --environment-directory "$temporary"
# The second run proves that bootstrapping an initialized database changes nothing.
uv run --no-project --python 3.15.2 python "$root/deploy/bootstrap_database.py" "$temporary" --environment-directory "$temporary"
"${compose[@]}" run --rm --no-deps migrate
"${compose[@]}" run --rm --no-deps migrate pyracast check --deploy --fail-level WARNING
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
headers=$("${secure_curl[@]}" --head https://localhost:18443/)
[[ ${headers,,} == *'strict-transport-security: max-age=31536000'* ]]
[[ ${headers,,} == *'x-content-type-options: nosniff'* ]]
[[ ${headers,,} == *'referrer-policy: same-origin'* ]]
[[ $(curl --silent --show-error --output /dev/null --write-out '%{redirect_url}' \
  http://localhost:18080/) == https://localhost:18443/ ]]
[[ $(head -c 8388609 /dev/zero | "${tls_curl[@]}" \
  --data-binary @- --output /dev/null --write-out '%{http_code}' \
  https://localhost:18443/health/ready/) == 413 ]]
[[ $("${secure_curl[@]}" -H 'X-Real-IP: invalid' -H 'X-Forwarded-Proto: http' \
  -H 'X-Forwarded-For: invalid' -H 'Forwarded: for=invalid' https://localhost:18443/health/ready/) == ok ]]
[[ $("${secure_curl[@]}" -H 'X_Real_IP: invalid' -H 'X_Forwarded_Proto: http' \
  -H 'X_Forwarded_For: invalid' https://localhost:18443/health/ready/) == ok ]]
"${secure_curl[@]}" -H 'X-Real-IP: invalid' -H 'X_Real_IP: invalid' \
  -H 'X.Real.IP: invalid' \
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
            "x.real.ip", "x.forwarded.proto", "x.forwarded.for"} & headers.keys(), headers
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
from pyracast.accounts.middleware import TrustedProxyHeaders
headers = {key.lower(): value for key, value in json.load(sys.stdin).items()}
assert headers["x-forwarded-for"].split(",")[-1].strip() == "173.245.48.3", headers
request = RequestFactory().get("/", REMOTE_ADDR="172.30.80.2", **{
    "HTTP_" + key.upper().replace("-", "_"): value for key, value in headers.items()})
TrustedProxyHeaders(lambda request: HttpResponse())(request)
assert request.META["HTTP_X_REAL_IP"] == "198.51.100.7", request.META
assert "HTTP_CF_CONNECTING_IP" not in request.META
'
[[ $(docker inspect --format '{{.Config.User}}' "$("${compose[@]}" ps -q gateway)") == 10001:10001 ]]
"${compose[@]}" exec -T api python - <<'PY'
import os
import time
import django
django.setup()
from allauth.account.models import EmailAddress
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
actor = User.objects.create_superuser(username='container-smoke', email='smoke@example.com',
                                      password='container-smoke-password')
EmailAddress.objects.create(user=actor, email=actor.email, primary=True, verified=True)
tenant = Tenant.objects.create(name='Container smoke')
Membership.objects.create(user=actor, tenant=tenant, role=Membership.Role.ADMIN)
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
assert set(session) == {"user", "csrf_token", "language"}, session
assert session["user"] is None and session["language"] in LANGUAGES, session
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
"${compose[@]}" stop -t 30 dispatcher worker notifications
echo 'Production containers, TLS routing, static assets, session and sign-in API, workspace app shell, database roles, fixture queue publication and graceful idle shutdown passed.'
