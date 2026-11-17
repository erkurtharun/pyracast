#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f /.dockerenv || $EUID != 0 || $$ != 1 || ! -f /source/install-release.sh ]]; then
  echo 'Run as PID 1 in an isolated Docker container with deploy mounted read-only at /source.' >&2
  exit 2
fi
trap 'cat /result /calls >&2' ERR
mkdir -p /mock-bin /fixture/deploy /etc/pyracast /etc/systemd/system /var/lib/pyracast /opt/pyracast/releases /run/lock
cat >/etc/pyracast/host.json <<'JSON'
{"domain":"example.com","acme_email":"ops@example.com","backup_bucket":"pyracast-backups"}
JSON
printf 'name: fixture\n' >/fixture/deploy/compose.yaml
touch /fixture/deploy/runtime_secrets.py /fixture/deploy/bootstrap_database.py /fixture/deploy/backup.py
cp /source/install-units.sh /fixture/deploy/
cp -R /source/systemd /fixture/deploy/
units=(pyracast-backup.service pyracast-backup.timer pyracast-watchdog.service pyracast-watchdog.timer)
cat >/mock-bin/python3 <<'SH'
#!/bin/bash
if [[ ${1:-} == */runtime_secrets.py ]]; then
  if [[ " $* " == *' --migration '* ]]; then
    printf 'PGUSER=new-owner\nPGPASSWORD=new-password\n' >/run/pyracast/migration.env
  else
    printf 'PGUSER=new-runtime\n' >/run/pyracast/runtime.env
    printf 'RABBITMQ_DEFAULT_PASS=new-password\n' >/run/pyracast/broker.env
    if [[ $MODE == secrets ]]; then exit 52; fi
    printf 'POSTGRES_PASSWORD=new-password\n' >/run/pyracast/database.env
  fi
elif [[ ${1:-} == */bootstrap_database.py || ${1:-} == */backup.py ]]; then
  printf 'python3 %s\n' "$*" >>/calls
  if [[ ${1:-} == */backup.py ]]; then
    test -f /var/lib/pyracast/release-blocked || exit 90
    test ! -e /support-changed || exit 91
    touch /backup-entered
    if [[ $MODE == backup || $MODE == first-backup ]]; then exit 45; fi
    if [[ $MODE == killed-backup ]]; then kill -KILL "$PPID"; exit 0; fi
    touch /backup-complete
    release_dir=$(sed -n 's/.* --release-dir \([^ ]*\) .*/\1/p' <<<"$*")
    object=postgres/fixture-prerelease.dump
    if [[ $MODE == record-invalid ]]; then object='postgres/x.dump; touch /record-injected'; fi
    printf '{"object": "%s"}\n' "$object" >"$release_dir/prerelease-backup.json"
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
    *) printf 'ok\n' ;;
  esac
fi
SH
cat >/mock-bin/systemctl <<'SH'
#!/bin/bash
printf 'systemctl %s\n' "$*" >>/calls
if [[ $MODE == final-start && "$*" == 'start pyracast.service' ]]; then exit 53; fi
if [[ "$*" == 'show --property=LoadState --value pyracast-backup.service' ]]; then echo loaded; fi
if [[ "$*" == 'is-active --quiet pyracast-backup.service' ]]; then
  if [[ $MODE == backup-busy ]]; then exit 0; else exit 3; fi
fi
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
  deploy-check:*' run --rm --no-deps migrate pyracast check --deploy --fail-level WARNING') exit 48 ;;
  plan:*' run --rm --no-deps migrate pyracast migrate --plan') exit 49 ;;
  support-rabbit:*' up -d rabbitmq') exit 42 ;;
  support-postgres:*' up -d postgres') exit 46 ;;
  drain:*' stop -t 660 worker notifications') exit 47 ;;
  migration:*' run --rm --no-deps migrate' | maintenance-migration:*' run --rm --no-deps migrate') exit 43 ;;
  health:*'releases/'*' up -d --wait --wait-timeout 180') exit 44 ;;
  killed:*' run --rm --no-deps migrate')
    test -f /var/lib/pyracast/release-blocked || exit 90
    touch /migration-entered
    kill -KILL "$PPID"
    ;;
esac
SH
chmod 755 /mock-bin/*
export PATH=/mock-bin:$PATH
revision=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
repository=us-docker.pkg.dev/project/repo
backend_image=$repository/backend@sha256:0000000000000000000000000000000000000000000000000000000000000000
rebuilt_backend_image=$repository/backend@sha256:4444444444444444444444444444444444444444444444444444444444444444
web_image=$repository/web@sha256:2222222222222222222222222222222222222222222222222222222222222222
gateway_image=$repository/gateway@sha256:3333333333333333333333333333333333333333333333333333333333333333
postgres_image=$repository/postgres@sha256:1111111111111111111111111111111111111111111111111111111111111111

release_directory() {
  printf '/opt/pyracast/releases/%s-%s' "$revision" "$(printf '%s\n' "$@" | sha256sum | cut -c1-12)"
}
new_release=$(release_directory "$backend_image" "$web_image" "$gateway_image" "$postgres_image")
previous_build=$(release_directory "$rebuilt_backend_image" "$web_image" "$gateway_image" "$postgres_image")

run_release() {
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

for MODE in preflight deploy-check plan secrets units-invalid image live-artifact redeploy-preflight drain backup support-postgres support-rabbit migration health tls response success first-success redeploy final-start first-backup killed-backup killed lock-wait lock-timeout maintenance-success maintenance-migration backup-busy record-invalid; do
  export MODE
  rm -rf /opt/pyracast/current /opt/pyracast/current.next /opt/pyracast/releases/* /run/pyracast
  rm -f /var/lib/pyracast/release-blocked /migration-entered /backup-entered /backup-complete /support-changed /database-initialized
  # deploy/reset_database.py leaves its session identity in the marker and the site stopped.
  if [[ $MODE == maintenance-* ]]; then printf 'reset-session\n' >/var/lib/pyracast/release-blocked; fi
  mkdir -p /opt/pyracast/releases/old/deploy /run/pyracast
  live=/opt/pyracast/releases/old
  case "$MODE" in
    first-*) live= ;;
    redeploy*) live=$previous_build ;;
    live-artifact) live=$new_release ;;
  esac
  if [[ -n $live ]]; then
    mkdir -p "$live/deploy"
    printf 'BACKEND_IMAGE=%s\n' "$rebuilt_backend_image" >"$live/deploy/release.env"
    cp "$live/deploy/release.env" /live-release.env
    ln -s "$live" /opt/pyracast/current
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
  printf 'PGUSER=old-runtime\n' >/run/pyracast/runtime.env
  printf 'RABBITMQ_DEFAULT_PASS=old-password\n' >/run/pyracast/broker.env
  printf 'POSTGRES_PASSWORD=old-password\n' >/run/pyracast/database.env
  printf '{"PGUSER":"owner","PGPASSWORD":"password"}\n' >/migration.json
  : >/calls
  if run_release >/result 2>&1; then
    result=0
  else
    result=$?
  fi
  if [[ $MODE == lock-timeout ]]; then
    test "$result" == 1
    grep -Fxq 'Timed out waiting for another release, backup or watchdog operation.' /result
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
    else
      test -f /backup-entered
      test ! -e /support-changed
    fi
    test -f /var/lib/pyracast/release-blocked
    test "$(readlink /opt/pyracast/current)" == /opt/pyracast/releases/old
    # SIGKILL bypasses EXIT cleanup, so safety must already be durable before migration.
    test -f /migration.json
    test -f /run/pyracast/migration.env
  else
    test ! -e /migration.json
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
      preflight|deploy-check|plan|secrets|units-invalid|redeploy-preflight)
        case "$MODE" in
          preflight|redeploy-preflight) test "$result" == 41 ;;
          deploy-check) test "$result" == 48 ;;
          plan) test "$result" == 49 ;;
          secrets) test "$result" == 52 ;;
          units-invalid) test "$result" == 54 ;;
        esac
        test "$(readlink /opt/pyracast/current)" == "$live"
        cmp /live-release.env "$live/deploy/release.env"
        test "$new_release" != "$live"
        test ! -e /var/lib/pyracast/release-blocked
        grep -Fxq "POSTGRES_IMAGE=$postgres_image" "$new_release/deploy/release.env"
        grep -Fxq 'PGUSER=old-runtime' /run/pyracast/runtime.env
        grep -Fxq 'RABBITMQ_DEFAULT_PASS=old-password' /run/pyracast/broker.env
        grep -Fxq 'POSTGRES_PASSWORD=old-password' /run/pyracast/database.env
        test ! -e /backup-entered
        absent ' stop '
        units_untouched
        ;;
      drain|backup|first-backup|backup-busy|record-invalid|support-postgres|support-rabbit|migration|maintenance-migration|health|tls|response)
        case "$MODE" in
          drain) test "$result" == 47 ;;
          backup|first-backup) test "$result" == 45 ;;
          support-postgres) test "$result" == 46 ;;
          support-rabbit) test "$result" == 42 ;;
          migration) test "$result" == 43 ;;
          maintenance-migration)
            test "$result" == 43
            grep -Fxq reset-session /var/lib/pyracast/release-blocked
            ;;
          health) test "$result" == 44 ;;
          tls) test "$result" == 60 ;;
          response|backup-busy) test "$result" == 1 ;;
          record-invalid)
            test "$result" == 1
            grep -Fq "$new_release/prerelease-backup.json is unreadable" /result
            if grep -Fq 'record-injected' /result; then exit 1; fi
            ;;
        esac
        test -f /var/lib/pyracast/release-blocked
        grep -Fq 'reset_database.py /etc/pyracast/host.json abort' /result
        case "$MODE" in
          drain|backup|first-backup|backup-busy|record-invalid)
            if grep -q 'restore --confirm' /result; then exit 1; fi
            ;;
          *) grep -Fq 'host.json restore --confirm postgres/fixture-prerelease.dump' /result ;;
        esac
        if [[ $MODE == drain ]]; then
          grep -Fq 'A stop command failed' /result
        else
          grep -Fq 'Every stop command succeeded' /result
        fi
        grep -q 'stop -t 660 dispatcher' /calls
        grep -q 'stop -t 660 worker notifications' /calls
        grep -q 'stop -t 100 gateway api web' /calls
        if [[ $MODE == first-* ]]; then
          test ! -e /opt/pyracast/current
          test -f /database-initialized
        else
          test "$(readlink /opt/pyracast/current)" == /opt/pyracast/releases/old
        fi
        absent 'current/deploy/compose.yaml up -d --wait'
        if [[ $MODE == backup-busy ]]; then
          grep -Fq 'pyracast-backup.service is still active' /result
          test ! -e /backup-entered
        fi
        if [[ $MODE == backup || $MODE == first-backup || $MODE == drain || $MODE == backup-busy ]]; then
          test ! -e /support-changed
        fi
        units_untouched
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
      success|first-success|redeploy|lock-wait|maintenance-success)
        test "$result" == 0
        test "$(readlink /opt/pyracast/current)" == "$new_release"
        test ! -e /var/lib/pyracast/release-blocked
        grep -Fxq "POSTGRES_IMAGE=$postgres_image" "$new_release/deploy/release.env"
        grep -q 'curl .*https://example.com/health/ready/' /calls
        units_installed
        if [[ $MODE == redeploy ]]; then
          cmp /live-release.env "$previous_build/deploy/release.env"
          absent daemon-reload
        else
          grep -Fxq 'systemctl daemon-reload' /calls
        fi
        test "$(tail -n 1 /calls)" == 'systemctl start pyracast.service'
        awk -v first="$([[ $MODE == first-success ]] && echo 1 || echo 0)" '
          /^systemd-analyze verify / { verified=NR }
          /^systemctl enable pyracast.service$/ { enabled=NR }
          /^systemctl reset-failed pyracast.service$/ { if (!enabled) exit 1; reset=NR }
          / up -d --no-recreate --wait --wait-timeout 180 postgres$/ { if (!verified || !reset) exit 1 }
          / stop -t 660 dispatcher$/ { if (!verified || !reset) exit 1; producer=NR }
          / stop -t 660 worker notifications$/ { if (!producer) exit 1; workers=NR }
          / stop -t 100 gateway api web$/ { if (!workers) exit 1; drained=NR }
          /^systemctl stop pyracast-backup.service$/ { if (!first && !drained) exit 1; stopped=NR }
          /^systemctl is-active --quiet pyracast-backup.service$/ { if (!stopped) exit 1; inactive=NR }
          / --kind prerelease$/ { if (!inactive) exit 1; backup=NR }
          / up -d postgres$/ { if (!backup) exit 1; postgres=NR }
          / up -d rabbitmq$/ { if (!postgres) exit 1; broker=NR }
          / run --rm --no-deps migrate$/ { if (!broker) exit 1; migration=NR }
          /^curl / { if (!migration) exit 1; ready=NR }
          /^systemctl enable --now / { if (!ready) exit 1; installed=NR }
          /^systemctl start pyracast.service$/ { if (!installed) exit 1 }
          END { if (!reset || !postgres || !backup || !broker || !migration || !ready || !installed) exit 1 }
        ' /calls
        ;;
    esac
  fi
  echo "Release regression passed: $MODE (status $result)"
done
