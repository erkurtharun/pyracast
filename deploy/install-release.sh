#!/usr/bin/env bash
set -euo pipefail
umask 077

refuse() {
  echo "$1" >&2
  exit 2
}
[[ $# == 8 && $EUID == 0 ]] || refuse 'Usage (root): install-release.sh SHA BACKEND_IMAGE WEB_IMAGE GATEWAY_IMAGE POSTGRES_IMAGE RABBITMQ_IMAGE BUNDLE MIGRATION_JSON'
revision=$1
backend=$2
web=$3
gateway=$4
postgres=$5
rabbitmq=$6
bundle=$(realpath "$7")
migration=$(realpath "$8")
[[ $revision =~ ^[0-9a-f]{40}$ ]] || refuse 'Invalid revision; expected a full lowercase commit SHA.'
repository='[a-z0-9-]+-docker\.pkg\.dev/[a-z0-9-]+/[a-z0-9_-]+'
for image in backend web gateway postgres rabbitmq; do
  [[ ${!image} =~ ^${repository}/${image}@sha256:[0-9a-f]{64}$ ]] \
    || refuse "Invalid $image image reference; expected an Artifact Registry digest."
done
[[ -f "$bundle/deploy/compose.yaml" && -f "$migration" ]] || refuse 'Missing release bundle or migration secret.'
exec 9>>/var/lib/pyracast/release.lock
# systemd stop takes the same lock; releases must drain containers directly.
flock --timeout 300 9 || { echo 'Timed out waiting for another release, backup or watchdog operation; nothing was changed.' >&2; exit 1; }
mkdir -p /run/pyracast
chmod 700 /run/pyracast
session=$(mktemp -d /run/pyracast/release.XXXXXX)
# A marker that already exists belongs to a reset or an earlier failed release: never resume then.
blocked_before=0
if [[ -e /var/lib/pyracast/release-blocked ]]; then blocked_before=1; fi
previous=
if [[ -L /opt/pyracast/current ]]; then previous=$(readlink -f /opt/pyracast/current); fi
old=(docker compose --env-file /opt/pyracast/current/deploy/release.env -f /opt/pyracast/current/deploy/compose.yaml)
maintenance_started=0
resumable=0
switched=0
recorded=0
promoted=0
begin_maintenance() {
  maintenance_started=1
  touch /var/lib/pyracast/release-blocked && sync -f /var/lib/pyracast/release-blocked \
    && sync -f /var/lib/pyracast
}
# With origin pulls required, the public entrypoint of the release in $1 must refuse a handshake
# without the edge's certificate: Traefik closes it instead when it rejects the TLS option.
edge_enforced() {
  if grep -qsx 'ORIGIN_PULL=required' "$1/deploy/release.env"; then
    python3 "$release/deploy/probe_origin_pull.py" "$site_address"
  fi
}
# Probes the release in $1 through its gateway; one with the loopback entrypoint is probed there,
# where the edge's client certificate is never required.
ready() {
  local response port=443 url=https://$site_address/health/ready/
  if grep -qs '^      TRAEFIK_ENTRYPOINTS_LOCAL_ADDRESS: :8444$' "$1/deploy/compose.yaml"; then
    port=8443
    url=https://$site_address:8443/health/ready/
  fi
  response=$(curl --fail --silent --show-error --noproxy '*' \
    --connect-timeout 5 --max-time 10 --retry 18 --retry-delay 5 --retry-max-time 180 \
    --retry-all-errors --resolve "$site_address:$port:127.0.0.1" "$url") || return $?
  [[ $response == ok ]] && edge_enforced "$1"
}
# Sets the runtime role's password to the release's staged runtime secret, the version host.json
# names. Only while no API or web container runs: their next connections would use another one.
switch_runtime() {
  python3 "$release/deploy/bootstrap_database.py" switch-runtime "$release" "$secrets"
}
# Starts the previous release as pyracast.service starts it, with the secrets host.json names, so a
# restart or reboot reproduces what runs. Before migrate the database still fits that release.
resume_previous() {
  python3 /opt/pyracast/current/deploy/runtime_secrets.py /etc/pyracast/host.json \
    && "${old[@]}" up -d --wait --wait-timeout 180 && ready /opt/pyracast/current \
    && rm -f /var/lib/pyracast/release-blocked && sync -f /var/lib/pyracast
}
# The printed recovery commands run the helper of the release in /opt/pyracast/current; one that
# counts the gateway as an application service (main before the notice) refuses restore beside it.
helper_keeps_notice() {
  python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from deploy.reset_database import APPLICATION_SERVICES; sys.exit("gateway" in APPLICATION_SERVICES)' /opt/pyracast/current 2>/dev/null
}
stop_application() {
  "${compose[@]}" stop -t 660 dispatcher || stopped=0
  "${compose[@]}" stop -t 660 worker notifications || stopped=0
  if (( notice == 1 )); then
    "${compose[@]}" up -d --no-deps --wait --wait-timeout 180 maintenance gateway || noticed=0
    "${compose[@]}" stop -t 100 api web || stopped=0
  else
    "${compose[@]}" stop -t 100 gateway maintenance api web || stopped=0
  fi
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
      noticed=1
      notice=0
      resume=0
      if [[ -z $previous ]] || helper_keeps_notice; then notice=1; fi
      begin_maintenance || blocked=0
      stop_application
      if [[ -n $previous ]] && (( switched == 0 && stopped == 1 )) && switch_runtime; then switched=1; fi
      if (( resumable == 1 && recorded == 0 && stopped == 1 && switched == 1 )); then resume=1; fi
      if (( resume == 1 )) && resume_previous; then
        echo "Release $revision failed before migrate; the previous release $previous serves again, started with the secrets /etc/pyracast/host.json names, and the marker is removed." >&2
      else
        if (( resume == 1 )); then
          echo "The previous release $previous did not serve again; it is stopped again." >&2
          stopped=1
          noticed=1
          begin_maintenance || blocked=0
          stop_application
        fi
        if (( stopped == 1 )); then
          echo 'Release failed during maintenance. Every stop command succeeded; the application is stopped.' >&2
        else
          echo "Release failed during maintenance. A stop command failed; the application may still run. Check with: ${compose[*]} ps" >&2
        fi
        if (( notice == 0 )); then
          echo 'The gateway is stopped too: the recovery helper of the release in /opt/pyracast/current refuses to run beside it, so the site is unreachable until recovery.' >&2
        elif (( noticed == 1 )); then
          echo 'The gateway shows the maintenance notice.' >&2
        else
          echo "The maintenance notice did not start. Check with: ${compose[*]} ps gateway maintenance" >&2
        fi
        if (( blocked == 0 )); then
          echo 'Writing /var/lib/pyracast/release-blocked failed; recreate it before recovery.' >&2
        fi
        if [[ -n $previous ]] && (( switched == 0 )); then
          echo "The runtime role's password was not set to the runtime secret that /etc/pyracast/host.json names and recovery starts the release with. Once no application container runs, set it with: python3 $release/deploy/runtime_secrets.py /etc/pyracast/host.json && ${compose[*]} up -d --no-recreate --wait --wait-timeout 180 postgres && python3 $release/deploy/bootstrap_database.py switch-runtime $release $secrets" >&2
        fi
        if [[ -z $previous ]]; then
          echo 'No release served before this one: fix the cause and run the release again.' >&2
        elif ! python3 "$release/deploy/release_record.py" /etc/pyracast/host.json recovery >&2; then
          echo 'The recovery of this maintenance could not be read; see the line above.' >&2
        fi
        echo 'See "A release that fails during maintenance" in the deployment guide.' >&2
      fi
    fi
  fi
  rm -f -- "$migration" "${secrets:-$session}/migration.env"
  rm -rf -- "$session"
  exit "$result"
}
trap cleanup EXIT

# A rebuild of a live revision has new digests and so its own directory; the live one is never written.
references=$(printf '%s\n' "$backend" "$web" "$gateway" "$postgres" "$rabbitmq" | sha256sum)
release="/opt/pyracast/releases/$revision-${references:0:12}"
if [[ $previous == "$release" ]]; then
  refuse "Release $revision with these images is already live; nothing was changed."
fi
secrets="/run/pyracast/releases/${release##*/}"
mkdir -p "$release"
cp -a "$bundle/deploy" "$release/"
chown -R root:root "$release"
chmod -R go-w "$release"
python3 "$release/deploy/release_environment.py" /etc/pyracast/host.json "$release/deploy/release.env" "$secrets" "$backend" "$web" "$gateway" "$postgres" "$rabbitmq"
site_address=$(sed -n 's/^SITE_ADDRESS=//p' "$release/deploy/release.env")
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
# The host firewall must keep containers away from the VM's metadata credentials.
"${compose[@]}" run --rm --no-deps api python -c 'import socket, sys; probe = socket.socket(); probe.settimeout(3); reached = probe.connect_ex(("169.254.169.254", 80)) == 0; sys.exit("Containers reach the metadata server; restore the host network guard.") if reached else socket.getaddrinfo("oauth2.googleapis.com", 443)'
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
fi
python3 "$release/deploy/bootstrap_database.py" converge "$release" "$secrets"
wait_for_database
"${compose[@]}" run --rm --no-deps migrate pyracast migrate --plan
"${compose[@]}" run --rm --no-deps migrate pyracast check_secrets || {
  status=$?
  echo "Release $revision refused before maintenance: check_secrets exited $status with the release's staged secrets, whose keys cannot read every stored secret (the rows are named above). The serving release and its secrets are unchanged; correct the runtime secret version in /etc/pyracast/host.json before pyracast.service restarts." >&2
  exit "$status"
}

begin_maintenance
if [[ -L /opt/pyracast/current ]]; then
  if (( blocked_before == 0 )); then resumable=1; fi
  # Only the dispatcher publishes work, so the site keeps serving while it and the workers drain.
  "${old[@]}" stop -t 660 dispatcher
  "${old[@]}" stop -t 660 worker notifications
  # The previous gateway may depend on api and web; this one answers with the notice once they stop.
  "${compose[@]}" up -d --no-deps --wait --wait-timeout 180 maintenance gateway
  edge_enforced "$release"
  "${compose[@]}" stop -t 100 api web
fi
switch_runtime
switched=1
probe_database api
# Back up the running database before a new support image can modify its data directory.
rm -f "$release/prerelease-backup.json"
python3 "$release/deploy/backup.py" /etc/pyracast/host.json --release-dir "$release" --lock-fd 9 --kind prerelease
sync -f "$release"
"${compose[@]}" up -d postgres
wait_for_database
"${compose[@]}" up -d rabbitmq
# Once per maintenance, before the database changes, the reset helper's session names the object
# that holds what the previous release served; every printed recovery and helper guard reads it.
if [[ -n $previous ]]; then
  python3 "$release/deploy/release_record.py" /etc/pyracast/host.json record "$release/prerelease-backup.json"
  recorded=1
fi
"${compose[@]}" run --rm --no-deps migrate
"${compose[@]}" run --rm --no-deps migrate pyracast migrate --check
# Migrate creates the ledger table with the runtime role's default privileges; converge again.
python3 "$release/deploy/bootstrap_database.py" converge "$release" "$secrets"
rm -f "$secrets/migration.env"

"${compose[@]}" up -d --wait --wait-timeout 180
ready "$release"
# A mail failure must not undo a healthy release; the warning reaches the release log.
if ! "${compose[@]}" exec -T api pyracast check_mail; then
  echo "WARNING: the mail probe failed; run pyracast check_mail and fix mail before customers need it." >&2
fi
ln -sfn "$release" /opt/pyracast/current.next
mv -Tf /opt/pyracast/current.next /opt/pyracast/current
promoted=1
sync -f /opt/pyracast
# The marker keeps the old units from starting until the new ones match the promoted scripts.
bash "$release/deploy/install-units.sh" install "$release"
rm -f /var/lib/pyracast/release-blocked
sync -f /var/lib/pyracast
systemctl start pyracast.service
if python3 "$release/deploy/release_state.py" record "$release" ${previous:+"$previous"}; then
  python3 "$release/deploy/release_state.py" prune || echo "WARNING: some old releases or images were not removed; see the lines above." >&2
else
  echo 'WARNING: /var/lib/pyracast/release-state.json was not written; old releases were not pruned.' >&2
fi
echo "Release $revision is healthy."
