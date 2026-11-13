#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# != 6 || $EUID != 0 ]]; then
  echo 'Usage (root): install-release.sh SHA BACKEND_IMAGE WEB_IMAGE GATEWAY_IMAGE BUNDLE MIGRATION_JSON' >&2
  exit 2
fi
revision=$1
backend=$2
web=$3
gateway=$4
bundle=$(realpath "$5")
migration=$(realpath "$6")
[[ $revision =~ ^[0-9a-f]{40}$ ]]
[[ $backend =~ ^[a-z0-9.-]+-docker.pkg.dev/[a-z0-9/_-]+@sha256:[0-9a-f]{64}$ ]]
[[ $web =~ ^[a-z0-9.-]+-docker.pkg.dev/[a-z0-9/_-]+@sha256:[0-9a-f]{64}$ ]]
[[ $gateway =~ ^[a-z0-9.-]+-docker.pkg.dev/[a-z0-9/_-]+@sha256:[0-9a-f]{64}$ ]]
[[ -f "$bundle/deploy/compose.yaml" && -f "$migration" ]]
exec 9>/run/lock/pyracast-release.lock
flock -n 9 || { echo 'Another release is running.' >&2; exit 1; }
mkdir -p /run/pyracast
chmod 700 /run/pyracast
session=$(mktemp -d /run/pyracast/release.XXXXXX)
old_stopped=0
migration_started=0
cleanup() {
  result=$?
  trap - EXIT
  if (( result != 0 && old_stopped == 1 )); then
    if (( migration_started == 1 )); then
      touch /var/lib/pyracast/release-blocked
      "${compose[@]}" stop -t 660 dispatcher worker notifications || true
      "${compose[@]}" stop -t 100 gateway api web || true
      echo 'Release failed after migration started. Application stopped; inspect migrations before recovery.' >&2
    elif [[ -L /opt/pyracast/current ]]; then
      for name in runtime.env broker.env; do
        if [[ -f "$session/$name" ]]; then cp "$session/$name" "/run/pyracast/$name"; fi
      done
      "${old[@]}" up -d --wait --wait-timeout 180 || true
    fi
  fi
  rm -f -- "$migration" /run/pyracast/migration.env
  rm -rf -- "$session"
  exit "$result"
}
trap cleanup EXIT
for name in runtime.env broker.env; do
  if [[ -f "/run/pyracast/$name" ]]; then cp "/run/pyracast/$name" "$session/$name"; fi
done

release="/opt/pyracast/releases/$revision"
mkdir -p "$release"
cp -a "$bundle/deploy" "$release/"
chown -R root:root "$release"
chmod -R go-w "$release"
python3 - "$release/deploy/release.env" "$backend" "$web" "$gateway" <<'PY'
import json
import pathlib
import re
import sys
config = json.loads(pathlib.Path('/etc/pyracast/host.json').read_text())
values = {
    'BACKEND_IMAGE': sys.argv[2], 'WEB_IMAGE': sys.argv[3],
    'GATEWAY_IMAGE': sys.argv[4],
    'SITE_ADDRESS': config['domain'], 'ACME_EMAIL': config['acme_email'],
    'CLOUD_SQL_CONNECTION_NAME': config['cloud_sql_connection_name'],
}
if any(not re.fullmatch(r'[a-zA-Z0-9@.:/_+%-]+', value) for value in values.values()):
    raise SystemExit('Invalid nonsecret deployment configuration.')
pathlib.Path(sys.argv[1]).write_text(''.join(f'{key}={value}\n' for key, value in values.items()))
PY
python3 "$release/deploy/runtime_secrets.py" /etc/pyracast/host.json
python3 "$release/deploy/runtime_secrets.py" /etc/pyracast/host.json --migration "$migration"

registry=${backend%%/*}
token=$(curl --fail --silent --show-error --header 'Metadata-Flavor: Google' \
  http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
export DOCKER_CONFIG
DOCKER_CONFIG="$session/docker"
mkdir "$DOCKER_CONFIG"
printf '%s' "$token" | docker login -u oauth2accesstoken --password-stdin "https://$registry"
unset token
compose=(docker compose --env-file "$release/deploy/release.env" -f "$release/deploy/compose.yaml")
"${compose[@]}" --profile release config --quiet
"${compose[@]}" --profile release pull --quiet
"${compose[@]}" run --rm --no-deps migrate pyracast check --deploy --fail-level WARNING
wait_for_database() {
  for attempt in {1..12}; do
    if "${compose[@]}" run --rm --no-deps migrate python -c \
      'import django; django.setup(); from django.db import connection; connection.ensure_connection()'; then
      return 0
    fi
    if (( attempt == 12 )); then return 1; fi
    sleep 5
  done
}
# Keep the existing proxy alive until workers have drained.
"${compose[@]}" up -d --no-recreate sqlproxy
wait_for_database
"${compose[@]}" run --rm --no-deps migrate pyracast migrate --plan
"${compose[@]}" run --rm --no-deps api python -c \
  'import django; django.setup(); from django.db import connection; connection.ensure_connection()'

# Stop producers first, then drain workers before schema changes.
if [[ -L /opt/pyracast/current ]]; then
  old=(docker compose --env-file /opt/pyracast/current/deploy/release.env -f /opt/pyracast/current/deploy/compose.yaml)
  old_stopped=1
  "${old[@]}" stop -t 660 dispatcher
  "${old[@]}" stop -t 660 worker notifications
  "${old[@]}" stop -t 100 gateway api web
fi
"${compose[@]}" up -d sqlproxy
wait_for_database
"${compose[@]}" up -d rabbitmq
old_stopped=1
migration_started=1
touch /var/lib/pyracast/release-blocked
sync -f /var/lib/pyracast/release-blocked
"${compose[@]}" run --rm --no-deps migrate
"${compose[@]}" run --rm --no-deps migrate pyracast migrate --check
rm -f /run/pyracast/migration.env

"${compose[@]}" up -d --wait --wait-timeout 180
ln -sfn "$release" /opt/pyracast/current.next
mv -Tf /opt/pyracast/current.next /opt/pyracast/current
sync -f /opt/pyracast
rm -f /var/lib/pyracast/release-blocked
sync -f /var/lib/pyracast
systemctl enable pyracast.service
systemctl reset-failed pyracast.service
systemctl start pyracast.service
echo "Release $revision is healthy."
