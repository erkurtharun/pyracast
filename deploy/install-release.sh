#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# != 7 || $EUID != 0 ]]; then
  echo 'Usage (root): install-release.sh SHA BACKEND_IMAGE WEB_IMAGE GATEWAY_IMAGE POSTGRES_IMAGE BUNDLE MIGRATION_JSON' >&2
  exit 2
fi
revision=$1
backend=$2
web=$3
gateway=$4
postgres=$5
bundle=$(realpath "$6")
migration=$(realpath "$7")
[[ $revision =~ ^[0-9a-f]{40}$ ]]
[[ $backend =~ ^[a-z0-9.-]+-docker.pkg.dev/[a-z0-9/_-]+@sha256:[0-9a-f]{64}$ ]]
[[ $web =~ ^[a-z0-9.-]+-docker.pkg.dev/[a-z0-9/_-]+@sha256:[0-9a-f]{64}$ ]]
[[ $gateway =~ ^[a-z0-9.-]+-docker.pkg.dev/[a-z0-9/_-]+@sha256:[0-9a-f]{64}$ ]]
[[ $postgres =~ ^[a-z0-9.-]+-docker.pkg.dev/[a-z0-9/_-]+@sha256:[0-9a-f]{64}$ ]]
[[ -f "$bundle/deploy/compose.yaml" && -f "$migration" ]]
exec 9>/run/lock/pyracast-release.lock
# systemd stop takes the same lock; releases must drain containers directly.
flock --timeout 300 9 || { echo 'Timed out waiting for another release, backup or watchdog operation.' >&2; exit 1; }
mkdir -p /run/pyracast
chmod 700 /run/pyracast
session=$(mktemp -d /run/pyracast/release.XXXXXX)
maintenance_started=0
begin_maintenance() {
  maintenance_started=1
  touch /var/lib/pyracast/release-blocked
  sync -f /var/lib/pyracast/release-blocked
  sync -f /var/lib/pyracast
}
cleanup() {
  result=$?
  trap - EXIT
  if (( result != 0 )); then
    if (( maintenance_started == 1 )); then
      begin_maintenance || true
      "${compose[@]}" stop -t 100 gateway api web || true
      "${compose[@]}" stop -t 660 dispatcher || true
      "${compose[@]}" stop -t 660 worker notifications || true
      echo 'Release failed during maintenance. Application remains stopped; inspect backups, support services and migrations before recovery.' >&2
    else
      for name in runtime.env broker.env; do
        if [[ -f "$session/$name" ]]; then cp "$session/$name" "/run/pyracast/$name"; fi
      done
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
python3 - "$release/deploy/release.env" "$backend" "$web" "$gateway" "$postgres" <<'PY'
import json
import pathlib
import re
import sys
config = json.loads(pathlib.Path('/etc/pyracast/host.json').read_text())
values = {
    'BACKEND_IMAGE': sys.argv[2], 'WEB_IMAGE': sys.argv[3],
    'GATEWAY_IMAGE': sys.argv[4], 'POSTGRES_IMAGE': sys.argv[5],
    'SITE_ADDRESS': config['domain'], 'ACME_EMAIL': config['acme_email'],
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
if [[ ! -L /opt/pyracast/current ]]; then
  begin_maintenance
  "${compose[@]}" up -d --no-recreate --wait --wait-timeout 180 postgres
  python3 "$release/deploy/bootstrap_database.py" "$release"
fi
wait_for_database
"${compose[@]}" run --rm --no-deps migrate pyracast migrate --plan
"${compose[@]}" run --rm --no-deps api python -c \
  'import django; django.setup(); from django.db import connection; connection.ensure_connection()'

begin_maintenance
if [[ -L /opt/pyracast/current ]]; then
  old=(docker compose --env-file /opt/pyracast/current/deploy/release.env -f /opt/pyracast/current/deploy/compose.yaml)
  "${old[@]}" stop -t 100 gateway api web
  "${old[@]}" stop -t 660 dispatcher
  "${old[@]}" stop -t 660 worker notifications
fi
# Back up the running database before a new support image can modify its data directory.
python3 "$release/deploy/backup.py" /etc/pyracast/host.json --release-dir "$release" --kind prerelease
"${compose[@]}" up -d postgres
wait_for_database
"${compose[@]}" up -d rabbitmq
"${compose[@]}" run --rm --no-deps migrate
"${compose[@]}" run --rm --no-deps migrate pyracast migrate --check
rm -f /run/pyracast/migration.env

"${compose[@]}" up -d --wait --wait-timeout 180
site_address=$(sed -n 's/^SITE_ADDRESS=//p' "$release/deploy/release.env")
readiness=$(curl --fail --silent --show-error --noproxy '*' \
  --connect-timeout 5 --max-time 10 --retry 18 --retry-delay 5 --retry-max-time 180 \
  --retry-all-errors --resolve "$site_address:443:127.0.0.1" "https://$site_address/health/ready/")
[[ $readiness == ok ]]
# A mail failure must not undo a healthy release; the warning reaches the release log.
if ! "${compose[@]}" exec -T api pyracast check_mail; then
  echo "WARNING: the mail probe failed; run pyracast check_mail and fix mail before customers need it." >&2
fi
ln -sfn "$release" /opt/pyracast/current.next
mv -Tf /opt/pyracast/current.next /opt/pyracast/current
sync -f /opt/pyracast
rm -f /var/lib/pyracast/release-blocked
sync -f /var/lib/pyracast
systemctl enable pyracast.service
systemctl reset-failed pyracast.service
systemctl start pyracast.service
echo "Release $revision is healthy."
