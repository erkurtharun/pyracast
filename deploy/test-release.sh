#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f /.dockerenv || $EUID != 0 || $$ != 1 || ! -f /source/install-release.sh ]]; then
  echo 'Run as PID 1 in an isolated Docker container with deploy mounted read-only at /source.' >&2
  exit 2
fi
trap 'cat /result /calls >&2' ERR
mkdir -p /mock-bin /fixture/deploy /etc/pyracast /etc/systemd/system /var/lib/pyracast /opt/pyracast/releases /run/lock /units
cat >/host.json <<'JSON'
{"domain":"example.com","acme_email":"ops@example.com","backup_bucket":"pyracast-backups","runtime_secret_version":"1"}
JSON
printf 'name: fixture\n' >/fixture/deploy/compose.yaml
touch /fixture/deploy/runtime_secrets.py /fixture/deploy/bootstrap_database.py
# The installer records and reads the maintenance session with the release's own helpers; the
# python3 mock still answers every backup.py call.
cp /source/__init__.py /source/backup.py /source/host.py /source/operations_log.py \
  /source/reset_database.py /source/install-units.sh /source/release_record.py \
  /source/release_state.py /source/release_environment.py /source/probe_origin_pull.py \
  /fixture/deploy/
cp -R /source/systemd /fixture/deploy/
units=(pyracast-backup.service pyracast-backup.timer pyracast-watchdog.service pyracast-watchdog.timer)
# State model: host.json names the runtime secret "new-runtime"; the serving release staged the
# earlier "old-runtime". /role holds the secret the runtime role's password was last set to, and
# /app-running exists while API and web containers run.
cat >/mock-bin/python3 <<'SH'
#!/bin/bash
if [[ ${1:-} == */runtime_secrets.py ]]; then
  printf 'python3 %s\n' "$*" >>/calls
  staged=/run/pyracast/releases/$(basename "$(realpath -m "$(dirname "$1")/..")")
  mkdir -p "$staged"
  if [[ " $* " == *' --migration '* ]]; then
    printf 'PGUSER=new-owner\nPGPASSWORD=new-password\n' >"$staged/migration.env"
  else
    printf 'PGUSER=new-runtime\n' >"$staged/runtime.env"
    printf 'RABBITMQ_DEFAULT_PASS=new-password\n' >"$staged/broker.env"
    if [[ $MODE == secrets ]]; then exit 52; fi
    printf 'POSTGRES_PASSWORD=new-password\n' >"$staged/database.env"
  fi
elif [[ ${1:-} == */bootstrap_database.py || ${1:-} == */backup.py ]]; then
  printf 'python3 %s\n' "$*" >>/calls
  if [[ ${2:-} == switch-runtime ]]; then
    if [[ $MODE == switch ]]; then exit 59; fi
    if [[ -e /app-running ]]; then touch /switched-while-running; fi
    cp "$4/runtime.env" /role
  fi
  if [[ ${1:-} == */backup.py ]]; then
    test -f /var/lib/pyracast/release-blocked || exit 90
    test ! -e /support-changed || exit 91
    touch /backup-entered
    case "$MODE" in backup|first-backup|resume-failed|maintenance-backup) exit 45 ;; esac
    if [[ $MODE == retry-backup && $RUN == 2 ]]; then exit 45; fi
    if [[ $MODE == killed-backup ]]; then kill -KILL "$PPID"; exit 0; fi
    touch /backup-complete
    release_dir=$(sed -n 's/.* --release-dir \([^ ]*\) .*/\1/p' <<<"$*")
    object=postgres/fixture-prerelease-$RUN.dump
    if [[ $MODE == *record-invalid ]]; then object='postgres/x.dump; touch /record-injected'; fi
    printf '{"object": "%s"}\n' "$object" >"$release_dir/prerelease-backup.json"
  fi
elif [[ ${1:-} == */probe_origin_pull.py ]]; then
  printf 'python3 %s\n' "$*" >>/calls
  test -f /var/lib/pyracast/release-blocked || exit 90
  if [[ $MODE == origin-pull-open ]]; then
    echo 'The public entrypoint 127.0.0.1:443 (example.com) does not enforce origin pulls: it closed the connection during the handshake.' >&2
    exit 1
  fi
else
  exec /usr/local/bin/python3 "$@"
fi
SH
cat >/mock-bin/curl <<'SH'
#!/bin/bash
if [[ "$*" == *metadata.google.internal* ]]; then
  printf '{"access_token":"fixture-token"}\n'
else
  printf 'curl %s\n' "$*" >>/calls
  test -f /var/lib/pyracast/release-blocked || exit 90
  [[ "$*" == *'--resolve example.com:443:127.0.0.1'* ]] || exit 91
  [[ "$*" == *'https://example.com/health/ready/'* ]] || exit 92
  [[ "$*" != *'--insecure'* ]] || exit 93
  case "$MODE" in
    tls) exit 60 ;;
    response) printf 'unavailable\n' ;;
    resume-unready) exit 22 ;;
    *) printf 'ok\n' ;;
  esac
fi
SH
cat >/mock-bin/systemctl <<'SH'
#!/bin/bash
if [[ ${1:-} == is-active ]]; then
  [[ -e /units/$3.started && ! -e /units/$3.ended ]]
  exit
fi
printf 'systemctl %s\n' "$*" >>/calls
if [[ $MODE == final-start && "$*" == 'start pyracast.service' ]]; then exit 53; fi
SH
# A transient unit runs detached from the command that started it; its output goes to the journal.
cat >/mock-bin/systemd-run <<'SH'
#!/bin/bash
unit=
while [[ $1 != -- ]]; do
  if [[ $1 == --unit=* ]]; then unit=${1#--unit=}; fi
  shift
done
shift
[[ -n $unit && ! -e /units/$unit.started ]] || exit 1
setsid bash -c 'touch "/units/$0.started"; "$@" >>"/units/$0.journal" 2>&1; touch "/units/$0.ended"' \
  "$unit" "$@" </dev/null >/dev/null 2>&1 &
for _ in $(seq 1 50); do
  if [[ -e /units/$unit.started ]]; then exit 0; fi
  sleep 0.1
done
exit 1
SH
cat >/mock-bin/journalctl <<'SH'
#!/bin/bash
for argument in "$@"; do
  case $argument in
    --unit=*) unit=${argument#--unit=} ;;
    --cursor-file=*) cursor=${argument#--cursor-file=} ;;
  esac
done
touch "/units/$unit.journal"
shown=$(cat "$cursor" 2>/dev/null || echo 0)
total=$(wc -l <"/units/$unit.journal")
tail -n "+$((shown + 1))" "/units/$unit.journal" | head -n "$((total - shown))"
echo "$total" >"$cursor"
SH
cat >/mock-bin/systemd-analyze <<'SH'
#!/bin/bash
printf 'systemd-analyze %s\n' "$*" >>/calls
if [[ $MODE == units-invalid ]]; then exit 54; fi
SH
cat >/mock-bin/flock <<'SH'
#!/bin/bash
if [[ $MODE == lock-* ]]; then
  printf 'attempt\n' >/lock-attempt
  if [[ $MODE == lock-timeout ]]; then
    exec /usr/bin/flock --timeout 0.2 9
  fi
fi
exec /usr/bin/flock "$@"
SH
cat >/mock-bin/docker <<'SH'
#!/bin/bash
printf 'docker %s\n' "$*" >>/calls
if [[ $1 == login ]]; then cat >/dev/null; exit 0; fi
# A container whose runtime secret the role's password does not match cannot connect.
connects() { cmp -s /role "$1/runtime.env" || exit 58; }
if [[ "$*" == *' up -d --no-recreate --wait --wait-timeout 180 postgres' ]]; then
  test -f /var/lib/pyracast/release-blocked || exit 90
  touch /database-initialized
fi
if [[ "$*" == *' up -d postgres' || "$*" == *' up -d rabbitmq' ]]; then
  test -f /var/lib/pyracast/release-blocked || exit 90
  test -f /backup-complete || exit 91
  touch /support-changed
fi
case "$MODE:$*" in
  preflight:*' --profile release pull --quiet' | redeploy-preflight:*' --profile release pull --quiet') exit 41 ;;
  metadata:*169.254.169.254*) exit 55 ;;
  check-secrets:*' run --rm --no-deps migrate pyracast check_secrets') exit 56 ;;
  resume-failed:*'current/deploy/compose.yaml up -d --wait --wait-timeout 180') exit 57 ;;
  drain:*'current/deploy/compose.yaml stop -t 660 worker notifications') exit 47 ;;
  stop-failed:*' stop -t 660 worker notifications') exit 47 ;;
  deploy-check:*' run --rm --no-deps migrate pyracast check --deploy --fail-level WARNING') exit 48 ;;
  plan:*' run --rm --no-deps migrate pyracast migrate --plan')
    echo 'CommandError: The migration ledger names Pyracast migrations that are not on disk' >&2
    exit 49
    ;;
  support-rabbit:*' up -d rabbitmq' | resume-unready:*' up -d rabbitmq') exit 42 ;;
  support-postgres:*' up -d postgres') exit 46 ;;
  migration:*' run --rm --no-deps migrate' | maintenance-migration:*' run --rm --no-deps migrate' | maintenance-replaced:*' run --rm --no-deps migrate' | maintenance-prepared:*' run --rm --no-deps migrate' | legacy-migration:*' run --rm --no-deps migrate' | retry-*:*' run --rm --no-deps migrate') exit 43 ;;
  health:*'releases/'*' up -d --wait --wait-timeout 180') exit 44 ;;
  cancelled:*' run --rm --no-deps migrate')
    touch /migration-entered
    for _ in $(seq 1 300); do
      if [[ -e /continue ]]; then break; fi
      sleep 0.1
    done
    test -e /continue || exit 70
    ;;
  killed:*' run --rm --no-deps migrate')
    test -f /var/lib/pyracast/release-blocked || exit 90
    touch /migration-entered
    kill -KILL "$PPID"
    ;;
esac
case "$*" in
  *' stop -t 100 api web' | *' stop -t 100 gateway maintenance api web') rm -f /app-running ;;
  *'current/deploy/compose.yaml up -d --wait --wait-timeout 180')
    connects "$PREVIOUS_SECRETS"
    cp "$PREVIOUS_SECRETS/runtime.env" /resumed-with
    touch /app-running
    ;;
  *' up -d --wait --wait-timeout 180') connects "$RELEASE_SECRETS"; touch /app-running ;;
  *' run --rm --no-deps api python -c import django'*) connects "$RELEASE_SECRETS" ;;
esac
SH
chmod 755 /mock-bin/*
export PATH=/mock-bin:$PATH
revision=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
repository=us-docker.pkg.dev/project/repo
backend_image=$repository/backend@sha256:0000000000000000000000000000000000000000000000000000000000000000
rebuilt_backend_image=$repository/backend@sha256:4444444444444444444444444444444444444444444444444444444444444444
retired_backend_image=$repository/backend@sha256:5555555555555555555555555555555555555555555555555555555555555555
web_image=$repository/web@sha256:2222222222222222222222222222222222222222222222222222222222222222
gateway_image=$repository/gateway@sha256:3333333333333333333333333333333333333333333333333333333333333333
postgres_image=$repository/postgres@sha256:1111111111111111111111111111111111111111111111111111111111111111

release_directory() {
  printf '/opt/pyracast/releases/%s-%s' "$revision" "$(printf '%s\n' "$@" | sha256sum | cut -c1-12)"
}
new_release=$(release_directory "$backend_image" "$web_image" "$gateway_image" "$postgres_image")
previous_build=$(release_directory "$rebuilt_backend_image" "$web_image" "$gateway_image" "$postgres_image")

run_cancelled_release() {
  local stage=/run/pyracast-release-$revision-1-1 follower
  mkdir -p "$stage/bundle"
  cp -R /fixture/deploy "$stage/bundle/"
  cp /source/install-release.sh /source/release_unit.py "$stage/bundle/deploy/"
  mv /migration.json "$stage/migration.json"
  /usr/local/bin/python3 "$stage/bundle/deploy/release_unit.py" start "$stage" "$revision" \
    "${images[@]}" >/follow-cancelled 2>&1 &
  follower=$!
  for _ in $(seq 1 300); do
    if [[ -e /migration-entered ]]; then break; fi
    sleep 0.1
  done
  test -e /migration-entered
  kill -KILL "$follower"
  wait "$follower" || true
  touch /continue
  for _ in $(seq 1 600); do
    if [[ -e $stage/status ]]; then break; fi
    sleep 0.1
  done
  /usr/local/bin/python3 "$stage/bundle/deploy/release_unit.py" follow "$stage"
}

run_release() {
  if [[ $MODE == cancelled ]]; then
    run_cancelled_release
    return
  fi
  if [[ $MODE != lock-* ]]; then
    bash /source/install-release.sh "$revision" "${images[@]}" /fixture /migration.json
    return
  fi
  /usr/local/bin/python3 - "$MODE" /source/install-release.sh "$revision" "${images[@]}" /fixture /migration.json <<'PY'
import os
import select
import subprocess
import sys

os.mkfifo('/lock-attempt')
attempt = os.open('/lock-attempt', os.O_RDWR | os.O_NONBLOCK)
holder = subprocess.Popen(
    ['/usr/bin/flock', '/run/lock/pyracast-release.lock', 'bash', '-c',
     "printf 'locked\\n'; read -r release"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
)
installer = None
try:
    assert holder.stdout is not None and holder.stdin is not None
    assert select.select([holder.stdout], [], [], 10)[0], 'Lock holder did not start'
    assert holder.stdout.readline() == 'locked\n'
    installer = subprocess.Popen(['bash', *sys.argv[2:]])
    assert select.select([attempt], [], [], 10)[0], 'Installer did not attempt the lock'
    assert os.read(attempt, 100) == b'attempt\n'
    if sys.argv[1] == 'lock-wait':
        try:
            status = installer.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        else:
            raise AssertionError(f'Installer exited while the shared lock was held: {status}')
        holder.stdin.write('release\n')
        holder.stdin.flush()
        assert holder.wait(timeout=5) == 0
    status = installer.wait(timeout=30)
finally:
    os.close(attempt)
    os.unlink('/lock-attempt')
    if installer is not None and installer.poll() is None:
        installer.kill()
        installer.wait(timeout=5)
    if holder.poll() is None:
        assert holder.stdin is not None
        holder.stdin.write('release\n')
        holder.stdin.flush()
        holder.wait(timeout=5)
raise SystemExit(status)
PY
}

absent() {
  if grep -q -- "$1" /calls; then
    echo "Unexpected call matching: $1" >&2
    return 1
  fi
}

units_untouched() {
  for unit in "${units[@]}"; do grep -Fxq 'previous unit' "/etc/systemd/system/$unit"; done
  absent daemon-reload
}

units_installed() {
  for unit in "${units[@]}"; do cmp "/source/systemd/$unit" "/etc/systemd/system/$unit"; done
  grep -Fxq 'host-owned unit' /etc/systemd/system/pyracast.service
  grep -Fxq 'systemctl enable --now pyracast-backup.timer pyracast-watchdog.timer' /calls
}

tool='python3 /opt/pyracast/current/deploy/reset_database.py /etc/pyracast/host.json'
session=/var/lib/pyracast/reset/session.json
# The maintenance session holds the marker's identity and names $1 as the database's recovery point.
session_names() {
  /usr/local/bin/python3 - "$session" /var/lib/pyracast/release-blocked "$1" <<'PY'
import json
import sys
from pathlib import Path
session = json.loads(Path(sys.argv[1]).read_text())
marker = Path(sys.argv[2]).read_text().strip()
assert marker, 'The marker holds no identity'
assert session == {'marker': marker, 'object': sys.argv[3], 'release': 'old', 'replaced': True}, session
PY
}
for MODE in preflight deploy-check plan secrets units-invalid metadata check-secrets image live-artifact redeploy-preflight drain backup support-postgres support-rabbit resume-failed resume-unready stop-failed switch maintenance-backup migration legacy-migration health tls response success prune first-success redeploy final-start first-backup killed-backup killed lock-wait lock-timeout maintenance-success maintenance-migration maintenance-replaced maintenance-prepared maintenance-record-invalid record-invalid retry-migration retry-backup cancelled origin-pull origin-pull-open; do
  export MODE RUN=1
  cp /host.json /etc/pyracast/host.json
  if [[ $MODE == origin-pull* ]]; then
    /usr/local/bin/python3 - /etc/pyracast/host.json /source/origin-pull-off.pem <<'PY'
import json
import sys
from pathlib import Path
host = Path(sys.argv[1])
authority = Path(sys.argv[2]).read_text()
host.write_text(json.dumps({**json.loads(host.read_text()), 'origin_pull_ca': authority}))
PY
  fi
  rm -rf /opt/pyracast/current /opt/pyracast/current.next /opt/pyracast/releases/* /run/pyracast /var/lib/pyracast/reset
  rm -f /var/lib/pyracast/release-blocked /var/lib/pyracast/release-state.json /migration-entered /backup-entered /backup-complete /support-changed /database-initialized
  rm -f /role /app-running /resumed-with /switched-while-running /first-session.json
  rm -rf /units /continue /follow-cancelled /run/pyracast-release-*
  mkdir /units
  # deploy/reset_database.py leaves its session identity in the marker and the site stopped.
  if [[ $MODE == maintenance-* ]]; then printf 'reset-session\n' >/var/lib/pyracast/release-blocked; fi
  if [[ $MODE == maintenance-replaced || $MODE == maintenance-prepared ]]; then
    mkdir -p /var/lib/pyracast/reset
    replaced=false
    if [[ $MODE == maintenance-replaced ]]; then replaced=true; fi
    printf '{"marker": "reset-session", "object": "postgres/reset-archive.dump", "release": "old", "replaced": %s}\n' "$replaced" \
      >"$session"
  fi
  mkdir -p /opt/pyracast/releases/old/deploy /run/pyracast
  if [[ $MODE == prune ]]; then
    mkdir -p /opt/pyracast/releases/older/deploy /run/pyracast/releases/older
    printf 'BACKEND_IMAGE=%s\nWEB_IMAGE=%s\n' "$retired_backend_image" "$web_image" \
      >/opt/pyracast/releases/older/deploy/release.env
  fi
  live=/opt/pyracast/releases/old
  case "$MODE" in
    first-*) live= ;;
    redeploy*) live=$previous_build ;;
    live-artifact) live=$new_release ;;
  esac
  export RELEASE_SECRETS=/run/pyracast/releases/${new_release##*/}
  export PREVIOUS_SECRETS=/run/pyracast/releases/${live##*/}
  if [[ -n $live ]]; then
    mkdir -p "$live/deploy" "$PREVIOUS_SECRETS"
    printf 'BACKEND_IMAGE=%s\n' "$rebuilt_backend_image" >"$live/deploy/release.env"
    cp "$live/deploy/release.env" /live-release.env
    touch "$live/deploy/runtime_secrets.py"
    ln -s "$live" /opt/pyracast/current
    # The previous release's recovery helper; the first upgrade's (main at 7c7c272) counts the gateway.
    helper_services='"dispatcher", "worker", "notifications", "api", "web"'
    if [[ $MODE == legacy-* ]]; then helper_services+=', "gateway"'; fi
    printf 'APPLICATION_SERVICES = frozenset({%s})\n' "$helper_services" >"$live/deploy/reset_database.py"
    printf 'PGUSER=old-runtime\n' >"$PREVIOUS_SECRETS/runtime.env"
    printf 'RABBITMQ_DEFAULT_PASS=old-password\n' >"$PREVIOUS_SECRETS/broker.env"
    printf 'POSTGRES_PASSWORD=old-password\n' >"$PREVIOUS_SECRETS/database.env"
    cp "$PREVIOUS_SECRETS/runtime.env" /role
    if [[ $MODE != maintenance-* ]]; then touch /app-running; fi
  fi
  for unit in "${units[@]}"; do
    if [[ $MODE == redeploy ]]; then
      cp "/source/systemd/$unit" "/etc/systemd/system/$unit"
    else
      printf 'previous unit\n' >"/etc/systemd/system/$unit"
    fi
  done
  printf 'host-owned unit\n' >/etc/systemd/system/pyracast.service
  images=("$backend_image" "$web_image" "$gateway_image" "$postgres_image")
  if [[ $MODE == image ]]; then images[2]=$web_image; fi
  printf '{"PGUSER":"owner","PGPASSWORD":"password"}\n' >/migration.json
  : >/calls
  if run_release >/result 2>&1; then
    result=0
  else
    result=$?
  fi
  if [[ $MODE == retry-* ]]; then
    # The first run failed in migrate; a second release runs in the same maintenance.
    test "$result" == 43
    session_names postgres/fixture-prerelease-1.dump
    cp "$session" /first-session.json
    rm -f /migration-entered /backup-entered /backup-complete /support-changed
    printf '{"PGUSER":"owner","PGPASSWORD":"password"}\n' >/migration.json
    RUN=2
    : >/calls
    if run_release >/result 2>&1; then
      result=0
    else
      result=$?
    fi
  fi
  test ! -e /switched-while-running
  if [[ $MODE == lock-timeout ]]; then
    test "$result" == 1
    grep -Fxq 'Timed out waiting for another release, backup or watchdog operation; nothing was changed.' /result
    test ! -e /var/lib/pyracast/release-blocked
    test -f /migration.json
    test ! -s /calls
    test "$(readlink /opt/pyracast/current)" == /opt/pyracast/releases/old
  elif [[ $MODE == image ]]; then
    test "$result" == 2
    grep -Fxq 'Invalid gateway image reference; expected an Artifact Registry digest.' /result
    test ! -s /calls
    [[ $(ls /opt/pyracast/releases) == old ]]
  elif [[ $MODE == killed || $MODE == killed-backup ]]; then
    test "$result" == 137
    if [[ $MODE == killed ]]; then
      test -f /migration-entered
      # The session names this run's dump before migrate starts, and the recovery reads it.
      session_names postgres/fixture-prerelease-1.dump
      /usr/local/bin/python3 "$new_release/deploy/release_record.py" /etc/pyracast/host.json recovery >/recovery
      grep -Fxq "Release a forward fix, or recover with: $tool restore --confirm postgres/fixture-prerelease-1.dump" /recovery
    else
      test -f /backup-entered
      test ! -e /support-changed
      test ! -e "$session"
    fi
    test -f /var/lib/pyracast/release-blocked
    test "$(readlink /opt/pyracast/current)" == /opt/pyracast/releases/old
    # The switch preceded the kill; the serving release's own staged files are still the earlier ones.
    cmp /role "$RELEASE_SECRETS/runtime.env"
    grep -Fxq 'PGUSER=old-runtime' "$PREVIOUS_SECRETS/runtime.env"
    # SIGKILL bypasses EXIT cleanup, so safety must already be durable before migration.
    test -f /migration.json
    test -f "$RELEASE_SECRETS/migration.env"
  else
    test ! -e /migration.json
    test ! -e "$RELEASE_SECRETS/migration.env"
    test ! -e /run/pyracast/migration.env
    case "$MODE" in
      live-artifact)
        test "$result" == 2
        grep -Fq "Release $revision with these images is already live" /result
        test ! -s /calls
        test "$(readlink /opt/pyracast/current)" == "$new_release"
        cmp /live-release.env "$new_release/deploy/release.env"
        test ! -e /var/lib/pyracast/release-blocked
        ;;
      preflight|deploy-check|plan|secrets|units-invalid|metadata|check-secrets|redeploy-preflight)
        case "$MODE" in
          preflight|redeploy-preflight) test "$result" == 41 ;;
          deploy-check) test "$result" == 48 ;;
          metadata) test "$result" == 55 ;;
          check-secrets) test "$result" == 56 ;;
          plan)
            test "$result" == 49
            grep -Fq 'not on disk' /result
            grep -q 'migrate pyracast migrate --plan$' /calls
            ;;
          secrets) test "$result" == 52 ;;
          units-invalid) test "$result" == 54 ;;
        esac
        test "$(readlink /opt/pyracast/current)" == "$live"
        cmp /live-release.env "$live/deploy/release.env"
        test "$new_release" != "$live"
        test ! -e /var/lib/pyracast/release-blocked
        grep -Fxq "POSTGRES_IMAGE=$postgres_image" "$new_release/deploy/release.env"
        # Nothing the serving release reads changed: its staged files, the role's password, its containers.
        grep -Fxq 'PGUSER=old-runtime' "$PREVIOUS_SECRETS/runtime.env"
        grep -Fxq 'RABBITMQ_DEFAULT_PASS=old-password' "$PREVIOUS_SECRETS/broker.env"
        grep -Fxq 'POSTGRES_PASSWORD=old-password' "$PREVIOUS_SECRETS/database.env"
        cmp /role "$PREVIOUS_SECRETS/runtime.env"
        test -e /app-running
        test ! -e /backup-entered
        absent ' stop '
        absent '--kind prerelease'
        absent 'current/deploy/'
        absent 'switch-runtime'
        units_untouched
        ;;
      drain|backup|record-invalid|support-postgres|support-rabbit|origin-pull-open)
        case "$MODE" in
          drain) test "$result" == 47 ;;
          backup) test "$result" == 45 ;;
          support-postgres) test "$result" == 46 ;;
          support-rabbit) test "$result" == 42 ;;
          record-invalid|origin-pull-open) test "$result" == 1 ;;
        esac
        if [[ $MODE == origin-pull-open ]]; then
          probed=$(grep -n "^python3 $new_release/deploy/probe_origin_pull.py example.com$" /calls | cut -d: -f1)
          edge=$(grep -n "$new_release/deploy/compose.yaml up -d --no-deps --wait --wait-timeout 180 maintenance gateway$" /calls | head -n 1 | cut -d: -f1)
          test "$probed" -gt "$edge"
          test "$probed" -lt "$(grep -n 'switch-runtime' /calls | cut -d: -f1)"
          absent '--kind prerelease'
        fi
        test ! -e /var/lib/pyracast/release-blocked
        test "$(readlink /opt/pyracast/current)" == /opt/pyracast/releases/old
        # The previous release runs as a service start would run it: host.json's secret, which the
        # runtime role's password matches.
        grep -Fxq 'PGUSER=new-runtime' /resumed-with
        cmp /role "$PREVIOUS_SECRETS/runtime.env"
        cmp /role "$RELEASE_SECRETS/runtime.env"
        test "$(grep -c 'switch-runtime' /calls)" == 1
        stopped=$(grep -n ' stop -t 100 api web$' /calls | tail -n 1 | cut -d: -f1)
        rendered=$(grep -n 'current/deploy/runtime_secrets.py /etc/pyracast/host.json$' /calls | cut -d: -f1)
        resumed=$(grep -n 'current/deploy/compose.yaml up -d --wait --wait-timeout 180$' /calls | cut -d: -f1)
        test "$stopped" -lt "$rendered"
        test "$rendered" -lt "$resumed"
        test "$(grep -n '^curl ' /calls | tail -n 1 | cut -d: -f1)" -gt "$resumed"
        if grep -q 'reset_database.py' /result; then exit 1; fi
        absent ' run --rm --no-deps migrate$'
        absent 'release_state.py'
        test ! -e "$session"
        test ! -e /record-injected
        if grep -Fq 'record-injected' /result; then exit 1; fi
        units_untouched
        ;;
      first-backup|resume-failed|resume-unready|stop-failed|switch|maintenance-backup|maintenance-record-invalid|migration|maintenance-migration|maintenance-replaced|maintenance-prepared|legacy-migration|health|tls|response|retry-*)
        case "$MODE" in
          first-backup|resume-failed|maintenance-backup|retry-backup) test "$result" == 45 ;;
          maintenance-record-invalid)
            test "$result" == 1
            test ! -e /record-injected
            if grep -Fq 'record-injected' /result; then exit 1; fi
            ;;
          resume-unready) test "$result" == 42 ;;
          stop-failed) test "$result" == 47 ;;
          switch) test "$result" == 59 ;;
          migration|maintenance-migration|maintenance-replaced|maintenance-prepared|legacy-migration|retry-migration) test "$result" == 43 ;;
          health) test "$result" == 44 ;;
          tls) test "$result" == 60 ;;
          response) test "$result" == 1 ;;
        esac
        test -f /var/lib/pyracast/release-blocked
        test ! -e /app-running
        # The recovery is the one the session of this maintenance gives; exactly one is printed.
        case "$MODE" in
          first-*)
            grep -Fq 'No release served before this one' /result
            if grep -q 'reset_database.py' /result; then exit 1; fi
            ;;
          maintenance-replaced|maintenance-prepared)
            session_names postgres/reset-archive.dump
            grep -Fxq "Release a forward fix, or recover with: $tool restore --confirm postgres/reset-archive.dump" /result
            ;;
          migration|maintenance-migration|legacy-migration|health|tls|response|retry-*)
            session_names postgres/fixture-prerelease-1.dump
            grep -Fxq "Release a forward fix, or recover with: $tool restore --confirm postgres/fixture-prerelease-1.dump" /result
            ;;
          *)
            test ! -e "$session"
            grep -Fxq "The database is unchanged in this maintenance. Recover with: $tool abort" /result
            ;;
        esac
        if [[ $MODE != first-* ]]; then test "$(grep -c 'reset_database.py' /result)" == 1; fi
        if [[ $MODE == retry-* ]]; then
          # The second run kept the first run's recovery point; the helper refuses the second dump.
          cmp /first-session.json "$session"
          refused=0
          /usr/local/bin/python3 "$new_release/deploy/reset_database.py" /etc/pyracast/host.json \
            restore --confirm postgres/fixture-prerelease-2.dump 2>/refused || refused=$?
          test "$refused" == 2
          grep -Fq "restore --confirm postgres/fixture-prerelease-1.dump" /refused
          cmp /first-session.json "$session"
        fi
        case "$MODE" in
          stop-failed|switch)
            # A failed stop leaves the password alone; a failed switch leaves it as it was.
            grep -Fxq 'PGUSER=old-runtime' /role
            grep -Fxq 'PGUSER=old-runtime' "$PREVIOUS_SECRETS/runtime.env"
            if [[ $MODE == stop-failed ]]; then absent 'switch-runtime'; fi
            ;;
          *) cmp /role "$RELEASE_SECRETS/runtime.env" ;;
        esac
        case "$MODE" in
          resume-failed|resume-unready)
            grep -q 'current/deploy/compose.yaml up -d --wait --wait-timeout 180$' /calls
            cmp /role "$PREVIOUS_SECRETS/runtime.env"
            ;;
          *)
            absent 'current/deploy/compose.yaml up -d --wait'
            absent 'current/deploy/runtime_secrets.py'
            ;;
        esac
        grep -q "$new_release/deploy/compose.yaml up -d --no-deps --wait --wait-timeout 180 maintenance gateway$" /calls
        grep -q 'stop -t 660 dispatcher' /calls
        grep -q 'stop -t 660 worker notifications' /calls
        grep -q 'stop -t 100 api web$' /calls
        if [[ $MODE == legacy-* ]]; then
          grep -q "$new_release/deploy/compose.yaml stop -t 100 gateway maintenance api web$" /calls
        else
          absent 'stop .*gateway'
        fi
        if [[ $MODE == first-* ]]; then
          test ! -e /opt/pyracast/current
          test -f /database-initialized
        else
          test "$(readlink /opt/pyracast/current)" == /opt/pyracast/releases/old
        fi
        if [[ $MODE == maintenance-* ]]; then grep -Fxq reset-session /var/lib/pyracast/release-blocked; fi
        if [[ $MODE == first-backup || $MODE == maintenance-backup ]]; then
          test ! -e /support-changed
        fi
        absent 'release_state.py'
        units_untouched
        if [[ $MODE == stop-failed ]]; then
          # The printed command sets the password from durable files only, as after a reboot.
          command=$(sed -n 's/^.*Once no application container runs, set it with: //p' /result)
          test -n "$command"
          rm -rf /run/pyracast
          bash -c "$command"
          cmp /role "$RELEASE_SECRETS/runtime.env"
          grep -Fxq 'PGUSER=new-runtime' /role
        fi
        ;;
      final-start)
        test "$result" == 53
        test "$(readlink /opt/pyracast/current)" == "$new_release"
        test ! -e /var/lib/pyracast/release-blocked
        test "$(grep -c ' stop -t ' /calls)" == 3
        grep -Fq "Release $revision is serving" /result
        grep -Fq "Finish with: bash $new_release/deploy/install-units.sh install $new_release && systemctl start pyracast.service" /result
        units_installed
        ;;
      success|prune|first-success|redeploy|lock-wait|maintenance-success|cancelled|origin-pull)
        test "$result" == 0
        if [[ $MODE == origin-pull ]]; then
          grep -Fxq 'ORIGIN_PULL=required' "$new_release/deploy/release.env"
          cmp /source/origin-pull-off.pem "$new_release/deploy/origin-pull-ca.pem"
          mapfile -t probes < <(grep -n "^python3 $new_release/deploy/probe_origin_pull.py example.com$" /calls | cut -d: -f1)
          test "${#probes[@]}" == 2
          test "${probes[0]}" -gt "$(grep -n ' up -d --no-deps --wait --wait-timeout 180 maintenance gateway$' /calls | cut -d: -f1)"
          test "${probes[0]}" -lt "$(grep -n ' stop -t 100 api web$' /calls | cut -d: -f1)"
          test "${probes[1]}" -gt "$(grep -n '^curl ' /calls | tail -n 1 | cut -d: -f1)"
        else
          absent 'probe_origin_pull.py'
        fi
        if [[ $MODE == cancelled ]]; then
          if grep -Fq "Release $revision is healthy." /follow-cancelled; then exit 1; fi
          grep -Fxq "Release $revision is healthy." /result
          test -e "/units/pyracast-release-$revision-1-1.service.ended"
          test ! -e "/run/pyracast-release-$revision-1-1/migration.json"
          grep -Fxq 0 "/run/pyracast-release-$revision-1-1/status"
        fi
        test "$(readlink /opt/pyracast/current)" == "$new_release"
        test ! -e /var/lib/pyracast/release-blocked
        grep -Fxq "POSTGRES_IMAGE=$postgres_image" "$new_release/deploy/release.env"
        for name in runtime broker database migration; do
          grep -Fxq "${name^^}_ENV_FILE=$RELEASE_SECRETS/$name.env" "$new_release/deploy/release.env"
        done
        grep -Fxq 'PGUSER=new-runtime' "$RELEASE_SECRETS/runtime.env"
        cmp /role "$RELEASE_SECRETS/runtime.env"
        test ! -e "$RELEASE_SECRETS/migration.env"
        if [[ -n $live ]]; then grep -Fxq 'PGUSER=old-runtime' "$PREVIOUS_SECRETS/runtime.env"; fi
        kept=$live
        /usr/local/bin/python3 - "$new_release" "$kept" <<'PY'
import json
import sys
with open('/var/lib/pyracast/release-state.json') as stream:
    state = json.load(stream)
previous = sys.argv[2].rsplit('/', 1)[-1] if sys.argv[2] else None
assert state == {'current': sys.argv[1].rsplit('/', 1)[-1], 'previous': previous}, state
PY
        if [[ -n $kept ]]; then test -d "$kept/deploy"; fi
        if [[ $MODE == prune ]]; then
          test ! -e /opt/pyracast/releases/older
          test ! -e /run/pyracast/releases/older
          grep -Fxq "docker image rm $retired_backend_image" /calls
          absent "docker image rm $web_image"
        else
          absent 'docker image rm'
        fi
        grep -q 'curl .*https://example.com/health/ready/' /calls
        absent 'current/deploy/runtime_secrets.py'
        absent 'current/deploy/compose.yaml up -d --wait'
        units_installed
        if [[ $MODE == redeploy ]]; then
          cmp /live-release.env "$previous_build/deploy/release.env"
          absent daemon-reload
        else
          grep -Fxq 'systemctl daemon-reload' /calls
        fi
        if [[ $MODE != prune ]]; then test "$(tail -n 1 /calls)" == 'systemctl start pyracast.service'; fi
        awk -v first="$([[ $MODE == first-success ]] && echo 1 || echo 0)" '
          /^systemd-analyze verify / { verified=NR }
          /^systemctl enable pyracast.service$/ { enabled=NR }
          /^systemctl reset-failed pyracast.service$/ { if (!enabled) exit 1; reset=NR }
          / up -d --no-recreate --wait --wait-timeout 180 postgres$/ { if (!verified || !reset) exit 1 }
          / stop -t 660 dispatcher$/ { if (!verified || !reset) exit 1; producer=NR }
          / stop -t 660 worker notifications$/ { if (!producer) exit 1; workers=NR }
          / up -d --no-deps --wait --wait-timeout 180 maintenance gateway$/ { if (!workers) exit 1; edge=NR }
          / stop -t 100 api web$/ { if (!edge) exit 1; drained=NR }
          /169\.254\.169\.254/ { if (!verified) exit 1; guarded=NR }
          /bootstrap_database\.py converge / {
            if (!converged) { if (producer || backup) exit 1; converged=NR }
            else { if (!checked) exit 1; reconverged=NR }
          }
          / run --rm --no-deps migrate pyracast check_secrets$/ { if (!converged || producer) exit 1; secrets=NR }
          /bootstrap_database\.py switch-runtime / { if (!secrets || (!first && !drained) || backup) exit 1; switched=NR }
          / run --rm --no-deps api python -c import django/ { if (!switched) exit 1; probed=NR }
          / --kind prerelease$/ { if (!probed || !guarded || (!first && !drained)) exit 1; backup=NR }
          / up -d postgres$/ { if (!backup) exit 1; postgres=NR }
          / up -d rabbitmq$/ { if (!postgres) exit 1; broker=NR }
          / run --rm --no-deps migrate$/ { if (!broker) exit 1; migration=NR }
          / migrate pyracast migrate --check$/ { if (!migration) exit 1; checked=NR }
          /^curl / { if (!reconverged) exit 1; ready=NR }
          /^systemctl enable --now / { if (!ready) exit 1; installed=NR }
          /^systemctl start pyracast.service$/ { if (!installed) exit 1 }
          END { if (!reset || !postgres || !switched || !backup || !broker || !migration || !ready || !installed || !reconverged) exit 1 }
        ' /calls
        ;;
    esac
  fi
  echo "Release regression passed: $MODE (status $result)"
done
