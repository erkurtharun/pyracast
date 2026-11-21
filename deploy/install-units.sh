#!/usr/bin/env bash
set -euo pipefail

# pyracast.service and the Docker drop-in stay with the startup script, so a release can never
# rewrite the unit that enforces the release block.
if [[ $# != 2 || ! $1 =~ ^(verify|install)$ || ! -d $2/deploy/systemd ]]; then
  echo 'Usage (root): install-units.sh verify|install RELEASE_DIRECTORY' >&2
  exit 2
fi
shipped=$2/deploy/systemd
installed=/etc/systemd/system
units=(pyracast-backup.service pyracast-backup.timer pyracast-rehearsal.service pyracast-rehearsal.timer pyracast-watchdog.service pyracast-watchdog.timer)
systemd-analyze verify "${units[@]/#/$shipped/}"
if [[ $1 == verify ]]; then exit 0; fi
changed=0
for unit in "${units[@]}"; do
  if ! cmp -s "$shipped/$unit" "$installed/$unit"; then
    install -m 0644 "$shipped/$unit" "$installed/.$unit.next"
    mv -f "$installed/.$unit.next" "$installed/$unit"
    changed=1
  fi
done
if (( changed )); then systemctl daemon-reload; fi
systemctl enable --now pyracast-backup.timer pyracast-rehearsal.timer pyracast-watchdog.timer
