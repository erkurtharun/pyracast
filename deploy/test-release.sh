#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f /.dockerenv || $EUID != 0 || $$ != 1 || ! -f /source/install-release.sh ]]; then
  echo 'Run as PID 1 in an isolated Docker container with deploy mounted read-only at /source.' >&2
  exit 2
fi
trap 'cat /result /calls >&2' ERR
mkdir -p /mock-bin /fixture/deploy /etc/pyracast /var/lib/pyracast /opt/pyracast/releases /run/lock
cat >/etc/pyracast/host.json <<'JSON'
{"domain":"example.com","acme_email":"ops@example.com","cloud_sql_connection_name":"project:region:database"}
JSON
printf 'name: fixture\n' >/fixture/deploy/compose.yaml
touch /fixture/deploy/runtime_secrets.py
cat >/mock-bin/python3 <<'SH'
#!/bin/bash
if [[ ${1:-} == */runtime_secrets.py ]]; then
  if [[ " $* " == *' --migration '* ]]; then
    printf 'PGUSER=new-owner\nPGPASSWORD=new-password\n' >/run/pyracast/migration.env
  else
    printf 'PGUSER=new-runtime\n' >/run/pyracast/runtime.env
    printf 'RABBITMQ_DEFAULT_PASS=new-password\n' >/run/pyracast/broker.env
  fi
else
  exec /usr/local/bin/python3 "$@"
fi
SH
cat >/mock-bin/curl <<'SH'
#!/bin/bash
printf '{"access_token":"fixture-token"}\n'
SH
cat >/mock-bin/systemctl <<'SH'
#!/bin/bash
printf 'systemctl %s\n' "$*" >>/calls
SH
cat >/mock-bin/docker <<'SH'
#!/bin/bash
printf 'docker %s\n' "$*" >>/calls
if [[ $1 == login ]]; then cat >/dev/null; exit 0; fi
case "$MODE:$*" in
  preflight:*' --profile release pull --quiet') exit 41 ;;
  pre-migration:*' up -d rabbitmq') exit 42 ;;
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

for MODE in preflight pre-migration migration health success killed; do
  export MODE
  rm -rf /opt/pyracast/current /opt/pyracast/current.next /opt/pyracast/releases/* /run/pyracast
  rm -f /var/lib/pyracast/release-blocked /migration-entered
  mkdir -p /opt/pyracast/releases/old/deploy /run/pyracast
  ln -s /opt/pyracast/releases/old /opt/pyracast/current
  printf 'PGUSER=old-runtime\n' >/run/pyracast/runtime.env
  printf 'RABBITMQ_DEFAULT_PASS=old-password\n' >/run/pyracast/broker.env
  printf '{"PGUSER":"owner","PGPASSWORD":"password"}\n' >/migration.json
  : >/calls
  if bash /source/install-release.sh "$revision" "$image" "$image" "$image" /fixture /migration.json >/result 2>&1; then
    result=0
  else
    result=$?
  fi
  if [[ $MODE == killed ]]; then
    test "$result" == 137
    test -f /migration-entered
    test -f /var/lib/pyracast/release-blocked
    test "$(readlink /opt/pyracast/current)" == /opt/pyracast/releases/old
    # SIGKILL bypasses EXIT cleanup, so safety must already be durable before migration.
    test -f /migration.json
    test -f /run/pyracast/migration.env
  else
    test ! -e /migration.json
    test ! -e /run/pyracast/migration.env
    case "$MODE" in
      preflight)
        test "$result" == 41
        test "$(readlink /opt/pyracast/current)" == /opt/pyracast/releases/old
        test ! -e /var/lib/pyracast/release-blocked
        ! grep -q ' stop ' /calls
        ;;
      pre-migration)
        test "$result" == 42
        test "$(cat /run/pyracast/runtime.env)" == PGUSER=old-runtime
        test "$(cat /run/pyracast/broker.env)" == RABBITMQ_DEFAULT_PASS=old-password
        grep -q 'current/deploy/compose.yaml up -d --wait' /calls
        test ! -e /var/lib/pyracast/release-blocked
        ;;
      migration|health)
        if [[ $MODE == migration ]]; then test "$result" == 43; else test "$result" == 44; fi
        test -f /var/lib/pyracast/release-blocked
        grep -q 'stop -t 660 dispatcher worker notifications' /calls
        grep -q 'stop -t 100 gateway api web' /calls
        test "$(readlink /opt/pyracast/current)" == /opt/pyracast/releases/old
        ! grep -q 'current/deploy/compose.yaml up -d --wait' /calls
        ;;
      success)
        test "$result" == 0
        test "$(readlink /opt/pyracast/current)" == "/opt/pyracast/releases/$revision"
        test ! -e /var/lib/pyracast/release-blocked
        awk '
          / stop -t 660 worker notifications$/ { drained=NR }
          / up -d sqlproxy$/ { if (!drained) exit 1; proxy=NR }
          END { if (!proxy) exit 1 }
        ' /calls
        ;;
    esac
  fi
  echo "Release regression passed: $MODE (status $result)"
done
