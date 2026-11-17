#!/usr/bin/env bash
set -euo pipefail
umask 077

refuse() {
  echo "$1" >&2
  exit 2
}
[[ $# == 7 && $EUID == 0 ]] || refuse 'Usage (root): install-release.sh SHA BACKEND_IMAGE WEB_IMAGE GATEWAY_IMAGE POSTGRES_IMAGE BUNDLE MIGRATION_JSON'
revision=$1
backend=$2
web=$3
gateway=$4
postgres=$5
bundle=$(realpath "$6")
migration=$(realpath "$7")
[[ $revision =~ ^[0-9a-f]{40}$ ]] || refuse 'Invalid revision; expected a full lowercase commit SHA.'
repository='[a-z0-9-]+-docker\.pkg\.dev/[a-z0-9-]+/[a-z0-9_-]+'
for image in backend web gateway postgres; do
  [[ ${!image} =~ ^${repository}/${image}@sha256:[0-9a-f]{64}$ ]] \
    || refuse "Invalid $image image reference; expected an Artifact Registry digest."
done
[[ -f "$bundle/deploy/compose.yaml" && -f "$migration" ]] || refuse 'Missing release bundle or migration secret.'
exec 9>/run/lock/pyracast-release.lock
# systemd stop takes the same lock; releases must drain containers directly.
flock --timeout 300 9 || { echo 'Timed out waiting for another release, backup or watchdog operation.' >&2; exit 1; }
mkdir -p /run/pyracast
chmod 700 /run/pyracast
session=$(mktemp -d /run/pyracast/release.XXXXXX)
environment_files=(runtime.env broker.env database.env)
maintenance_started=0
promoted=0
prerelease_object=
recovery='python3 /opt/pyracast/current/deploy/reset_database.py /etc/pyracast/host.json'
begin_maintenance() {
  maintenance_started=1
  touch /var/lib/pyracast/release-blocked && sync -f /var/lib/pyracast/release-blocked \
    && sync -f /var/lib/pyracast
}
cleanup() {
  result=$?
  trap - EXIT
  if (( result != 0 )); then
    if (( promoted == 1 )); then
      rm -f /var/lib/pyracast/release-blocked
      echo "Release $revision is serving, but a step after promotion failed; nothing was stopped. Finish with: bash $release/deploy/install-units.sh install $release && systemctl start pyracast.service" >&2
    elif (( maintenance_started == 1 )); then
      blocked=1
      stopped=1
      begin_maintenance || blocked=0
      "${compose[@]}" stop -t 660 dispatcher || stopped=0
      "${compose[@]}" stop -t 660 worker notifications || stopped=0
      "${compose[@]}" stop -t 100 gateway api web || stopped=0
      if (( stopped == 1 )); then
        echo 'Release failed during maintenance. Every stop command succeeded; the application is stopped.' >&2
      else
        echo "Release failed during maintenance. A stop command failed; the application may still run. Check with: ${compose[*]} ps" >&2
      fi
      if (( blocked == 0 )); then
        echo 'Writing /var/lib/pyracast/release-blocked failed; recreate it before recovery.' >&2
      fi
      if [[ -n $prerelease_object ]]; then
        echo "Before migrate, recover with: $recovery abort. In or after migrate, with: $recovery restore --confirm $prerelease_object" >&2
      else
        echo "Recover with: $recovery abort" >&2
      fi
      echo 'See "A release that fails during maintenance" in the deployment guide.' >&2
    else
      for name in "${environment_files[@]}"; do
        if [[ -f "$session/$name" ]]; then cp "$session/$name" "/run/pyracast/$name"; fi
      done
    fi
  fi
  rm -f -- "$migration" /run/pyracast/migration.env
  rm -rf -- "$session"
  exit "$result"
}
trap cleanup EXIT
for name in "${environment_files[@]}"; do
  if [[ -f "/run/pyracast/$name" ]]; then cp "/run/pyracast/$name" "$session/$name"; fi
done

# A rebuild of a live revision has new digests and so its own directory; the live one is never written.
references=$(printf '%s\n' "$backend" "$web" "$gateway" "$postgres" | sha256sum)
release="/opt/pyracast/releases/$revision-${references:0:12}"
if [[ $(readlink -f /opt/pyracast/current) == "$release" ]]; then
  refuse "Release $revision with these images is already live; nothing was changed."
fi
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
bash "$release/deploy/install-units.sh" verify "$release"
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
systemctl enable pyracast.service
systemctl reset-failed pyracast.service
probe_database() {
  "${compose[@]}" run --rm --no-deps "$1" python -c \
    'import django; django.setup(); from django.db import connection; connection.ensure_connection()'
}
wait_for_database() {
  for attempt in {1..12}; do
    if probe_database migrate; then return 0; fi
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
probe_database api

begin_maintenance
if [[ -L /opt/pyracast/current ]]; then
  old=(docker compose --env-file /opt/pyracast/current/deploy/release.env -f /opt/pyracast/current/deploy/compose.yaml)
  # Only the dispatcher publishes work, so the site keeps serving while it and the workers drain.
  "${old[@]}" stop -t 660 dispatcher
  "${old[@]}" stop -t 660 worker notifications
  "${old[@]}" stop -t 100 gateway api web
fi
# Back up the running database before a new support image can modify its data directory.
rm -f "$release/prerelease-backup.json"
python3 "$release/deploy/backup.py" /etc/pyracast/host.json --release-dir "$release" --kind prerelease
sync -f "$release"
prerelease_object=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["object"])' \
  "$release/prerelease-backup.json")
[[ $prerelease_object =~ ^postgres/[A-Za-z0-9/_.-]+\.dump$ ]]
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
ln -sfn "$release" /opt/pyracast/current.next
mv -Tf /opt/pyracast/current.next /opt/pyracast/current
promoted=1
sync -f /opt/pyracast
# The marker keeps the old units from starting until the new ones match the promoted scripts.
bash "$release/deploy/install-units.sh" install "$release"
rm -f /var/lib/pyracast/release-blocked
sync -f /var/lib/pyracast
systemctl start pyracast.service
echo "Release $revision is healthy."
