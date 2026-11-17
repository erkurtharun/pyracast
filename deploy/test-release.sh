#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f /.dockerenv || $EUID != 0 || $$ != 1 || ! -f /source/install-release.sh ]]; then
  echo 'Run as PID 1 in an isolated Docker container with deploy mounted read-only at /source.' >&2
  exit 2
fi
trap 'cat /result /calls >&2' ERR
mkdir -p /mock-bin /fixture/deploy /etc/pyracast /var/lib/pyracast /opt/pyracast/releases /run/lock
cat >/etc/pyracast/host.json <<'JSON'
{"domain":"example.com","acme_email":"ops@example.com","backup_bucket":"pyracast-backups"}
JSON
printf 'name: fixture\n' >/fixture/deploy/compose.yaml
touch /fixture/deploy/runtime_secrets.py /fixture/deploy/bootstrap_database.py /fixture/deploy/backup.py
cat >/mock-bin/python3 <<'SH'
#!/bin/bash
if [[ ${1:-} == */runtime_secrets.py ]]; then
  if [[ " $* " == *' --migration '* ]]; then
    printf 'PGUSER=new-owner\nPGPASSWORD=new-password\n' >/run/pyracast/migration.env
  else
    printf 'PGUSER=new-runtime\n' >/run/pyracast/runtime.env
    printf 'RABBITMQ_DEFAULT_PASS=new-password\n' >/run/pyracast/broker.env
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
  preflight:*' --profile release pull --quiet') exit 41 ;;
  deploy-check:*' run --rm --no-deps migrate pyracast check --deploy --fail-level WARNING') exit 48 ;;
  plan:*' run --rm --no-deps migrate pyracast migrate --plan') exit 49 ;;
  support-rabbit:*' up -d rabbitmq') exit 42 ;;
  support-postgres:*' up -d postgres') exit 46 ;;
  drain:*' stop -t 660 worker notifications') exit 47 ;;
  migration:*' run --rm --no-deps migrate') exit 43 ;;
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
image=us-docker.pkg.dev/project/repo/backend@sha256:0000000000000000000000000000000000000000000000000000000000000000
postgres_image=us-docker.pkg.dev/project/repo/postgres@sha256:1111111111111111111111111111111111111111111111111111111111111111

run_release() {
  if [[ $MODE != lock-* ]]; then
    bash /source/install-release.sh "$revision" "$image" "$image" "$image" "$postgres_image" /fixture /migration.json
    return
  fi
  /usr/local/bin/python3 - "$MODE" /source/install-release.sh "$revision" "$image" "$image" "$image" "$postgres_image" /fixture /migration.json <<'PY'
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

for MODE in preflight deploy-check plan drain backup support-postgres support-rabbit migration health tls response success first-success first-backup killed-backup killed lock-wait lock-timeout; do
  export MODE
  rm -rf /opt/pyracast/current /opt/pyracast/current.next /opt/pyracast/releases/* /run/pyracast
  rm -f /var/lib/pyracast/release-blocked /migration-entered /backup-entered /backup-complete /support-changed /database-initialized
  mkdir -p /opt/pyracast/releases/old/deploy /run/pyracast
  if [[ $MODE != first-* ]]; then ln -s /opt/pyracast/releases/old /opt/pyracast/current; fi
  printf 'PGUSER=old-runtime\n' >/run/pyracast/runtime.env
  printf 'RABBITMQ_DEFAULT_PASS=old-password\n' >/run/pyracast/broker.env
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
      preflight|deploy-check|plan)
        case "$MODE" in
          preflight) test "$result" == 41 ;;
          deploy-check) test "$result" == 48 ;;
          plan) test "$result" == 49 ;;
        esac
        test "$(readlink /opt/pyracast/current)" == /opt/pyracast/releases/old
        test ! -e /var/lib/pyracast/release-blocked
        grep -Fxq "POSTGRES_IMAGE=$postgres_image" "/opt/pyracast/releases/$revision/deploy/release.env"
        grep -Fxq 'PGUSER=old-runtime' /run/pyracast/runtime.env
        grep -Fxq 'RABBITMQ_DEFAULT_PASS=old-password' /run/pyracast/broker.env
        test ! -e /backup-entered
        ! grep -q ' stop ' /calls
        ;;
      drain|backup|first-backup|support-postgres|support-rabbit|migration|health|tls|response)
        case "$MODE" in
          drain) test "$result" == 47 ;;
          backup|first-backup) test "$result" == 45 ;;
          support-postgres) test "$result" == 46 ;;
          support-rabbit) test "$result" == 42 ;;
          migration) test "$result" == 43 ;;
          health) test "$result" == 44 ;;
          tls) test "$result" == 60 ;;
          response) test "$result" == 1 ;;
        esac
        test -f /var/lib/pyracast/release-blocked
        grep -q 'stop -t 100 gateway api web' /calls
        grep -q 'stop -t 660 dispatcher' /calls
        grep -q 'stop -t 660 worker notifications' /calls
        if [[ $MODE == first-* ]]; then
          test ! -e /opt/pyracast/current
          test -f /database-initialized
        else
          test "$(readlink /opt/pyracast/current)" == /opt/pyracast/releases/old
        fi
        ! grep -q 'current/deploy/compose.yaml up -d --wait' /calls
        if [[ $MODE == backup || $MODE == first-backup || $MODE == drain ]]; then
          test ! -e /support-changed
        fi
        ;;
      success|first-success|lock-wait)
        test "$result" == 0
        test "$(readlink /opt/pyracast/current)" == "/opt/pyracast/releases/$revision"
        test ! -e /var/lib/pyracast/release-blocked
        grep -Fxq "POSTGRES_IMAGE=$postgres_image" "/opt/pyracast/releases/$revision/deploy/release.env"
        grep -q 'curl .*https://example.com/health/ready/' /calls
        awk -v first="$([[ $MODE == first-success ]] && echo 1 || echo 0)" '
          / stop -t 100 gateway api web$/ { ingress=NR }
          / stop -t 660 dispatcher$/ { if (!ingress) exit 1; producer=NR }
          / stop -t 660 worker notifications$/ { if (!producer) exit 1; drained=NR }
          / --kind prerelease$/ { if (!first && !drained) exit 1; backup=NR }
          / up -d postgres$/ { if (!backup) exit 1; postgres=NR }
          / up -d rabbitmq$/ { if (!postgres) exit 1; broker=NR }
          / run --rm --no-deps migrate$/ { if (!broker) exit 1; migration=NR }
          END { if (!postgres || !backup || !broker || !migration) exit 1 }
        ' /calls
        ;;
    esac
  fi
  echo "Release regression passed: $MODE (status $result)"
done
