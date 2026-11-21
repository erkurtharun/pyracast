#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f /.dockerenv || $EUID != 0 || ! -f /templates/startup.sh.tftpl ]]; then
  echo 'Run as root in a disposable Docker container with the Terraform templates mounted at /templates.' >&2
  exit 2
fi
trap 'cat /result /calls >&2 || true' ERR
device=/dev/disk/by-id/google-pyracast-data
mkdir -p /stub-bin /dev/disk/by-id
[[ -b $device ]] || mknod "$device" b 7 200
host_json='{"domain":"example.com","acme_email":"ops@example.com"}'
pins=(docker-ce=5:29.9.3-1~ubuntu.24.04~noble docker-ce-cli=5:29.9.3-1~ubuntu.24.04~noble
  containerd.io=2.4.2-2~ubuntu.24.04~noble docker-compose-plugin=5.7.1-1~ubuntu.24.04~noble)
grep -Fq "docker_pins='${pins[*]}'" /templates/startup.sh.tftpl
guard_script=$'#!/bin/bash\n[[ -f /state/packages/iptables ]] || exit 1\necho network-guard >>/calls'
guard=$(printf '%s\n' "$guard_script" | base64 -w0)
pinned_key='pub:-:4096:1:8D81803C0EBFCD88:1487788586:::-:::scESA::::::23::0:
fpr:::::::::9DC858229FC7DD38854AE2D88D81803C0EBFCD88:
uid:-::::1487788586::0::Docker Release (CE deb) <docker@docker.com>::::::::::0:
sub:-:4096:1:7EA0A9C3F273FCD8:1487792064::::::s::::::23:
fpr:::::::::D3306A018370199E527AE7997EA0A9C3F273FCD8:'
other_key='pub:-:4096:1:0123456789ABCDEF:1700000000:::-:::scESA::::::23::0:
fpr:::::::::AAAAAAAAAAAAAAAAAAAAAAAA0123456789ABCDEF:'
render() {
  sed -e "s|[\$]{host_config_base64}|$(printf '%s' "$1" | base64 -w0)|" \
    -e "s|[\$]{network_guard_base64}|$guard|" \
    -e "s|[\$]{data_device_name}|pyracast-data|" /templates/startup.sh.tftpl >/rendered.sh
  if grep -qF "\${" /rendered.sh; then echo 'An unrendered placeholder remains.' >&2; exit 1; fi
}
# The stubs keep the host's packages, disk and mounts under /state and log commands to /calls.
cat >/stub-bin/dpkg <<'SH'
#!/bin/bash
echo "${ARCH:-amd64}"
SH
cat >/stub-bin/dpkg-query <<'SH'
#!/bin/bash
[[ -f /state/packages/$2 ]] || exit 1
printf 'Package: %s\nStatus: hold ok installed\nVersion: %s\n' "$2" "$(cat "/state/packages/$2")"
SH
cat >/stub-bin/apt-get <<'SH'
#!/bin/bash
echo "apt-get $*" >>/calls
[[ ! -e /state/apt-fails ]] || exit 100
[[ $1 == install ]] || exit 0
for pin in "$@"; do
  if [[ $pin == -* || $pin == install ]]; then continue; fi
  version=1
  if [[ $pin == *=* ]]; then version=${pin#*=}; fi
  echo "$version" >"/state/packages/${pin%%=*}"
done
SH
cat >/stub-bin/apt-mark <<'SH'
#!/bin/bash
echo "apt-mark $*" >>/calls
SH
cat >/stub-bin/curl <<'SH'
#!/bin/bash
echo "curl $*" >>/calls
while [[ $# -gt 0 ]]; do
  if [[ $1 == -o ]]; then echo key >"$2"; fi
  shift
done
SH
cat >/stub-bin/gpg <<'SH'
#!/bin/bash
cat /state/keyring
SH
cat >/stub-bin/blkid <<'SH'
#!/bin/bash
if [[ " $* " == *' TYPE '* ]]; then
  [[ -s /state/fstype ]] && cat /state/fstype
else
  echo 1111-2222
fi
SH
cat >/stub-bin/wipefs <<'SH'
#!/bin/bash
cat /state/signatures 2>/dev/null || true
SH
cat >/stub-bin/cmp <<'SH'
#!/bin/bash
if [[ " $* " == *' /dev/disk/by-id/google-pyracast-data '* ]]; then
  [[ $(cat /state/head) == zero ]]
else
  exec /usr/bin/cmp "$@"
fi
SH
cat >/stub-bin/mkfs.ext4 <<'SH'
#!/bin/bash
echo "mkfs.ext4 $*" >>/calls
echo ext4 >/state/fstype
SH
cat >/stub-bin/mountpoint <<'SH'
#!/bin/bash
[[ -e /state/mounted ]]
SH
cat >/stub-bin/mount <<'SH'
#!/bin/bash
echo "mount $*" >>/calls
touch /state/mounted
SH
cat >/stub-bin/systemctl <<'SH'
#!/bin/bash
echo "systemctl $*" >>/calls
SH
chmod 755 /stub-bin/*
export PATH=/stub-bin:$PATH

new_host() {
  rm -rf /state /etc/pyracast /etc/docker /etc/systemd/system /etc/apt/keyrings \
    /etc/apt/sources.list.d/docker.sources /usr/local/sbin/pyracast-network-guard \
    /var/lib/pyracast /opt/pyracast
  mkdir -p /state/packages /etc/systemd/system /etc/apt/sources.list.d
  printf 'NAME="Ubuntu"\nID=ubuntu\nVERSION_ID=24.04\n' >/etc/os-release
  printf 'LABEL=cloudimg-rootfs / ext4 discard,errors=remount-ro 0 1\n' >/etc/fstab
  echo zero >/state/head
  printf '%s\n' "$pinned_key" >/state/keyring
}
installed_host() {
  new_host
  for pin in "${pins[@]}"; do echo "${pin#*=}" >"/state/packages/${pin%%=*}"; done
  for name in ca-certificates curl iptables python3; do echo 1 >"/state/packages/$name"; done
  echo ext4 >/state/fstype
  touch /state/mounted
}
boot() {
  : >/calls
  status=0
  bash /rendered.sh >/result 2>&1 || status=$?
}
absent() {
  if grep -q -- "$1" /calls; then
    echo "Unexpected call matching: $1" >&2
    return 1
  fi
}
inodes() {
  stat -c '%n %i' /etc/pyracast/host.json /etc/docker/daemon.json \
    /etc/systemd/system/docker.service.d/pyracast-data.conf /etc/systemd/system/pyracast.service \
    /usr/local/sbin/pyracast-network-guard /etc/fstab /var/lib/pyracast/release.lock
}
no_downloaded_key() {
  [[ -z $(find /tmp -maxdepth 1 -name 'tmp.*' -print -quit) ]]
}
template_file() {
  sed -n "\|^write_if_changed $1 0644 <<'EOF'\$|,/^EOF\$/p" /templates/startup.sh.tftpl \
    | sed -e 1d | head -n -1
}

new_host
render "$host_json"
boot
[[ $status == 0 ]]
[[ $(stat -c %a /var/lib/pyracast/release.lock) == 600 ]]
grep -Fxq "mkfs.ext4 -F $device" /calls
grep -Fxq "$host_json" /etc/pyracast/host.json
[[ $(stat -c %a /etc/pyracast/host.json) == 600 ]]
grep -Fxq 'UUID=1111-2222 /var/lib/pyracast ext4 defaults,nodev,nosuid,nofail 0 2' /etc/fstab
grep -q '^apt-get install --yes --no-install-recommends --allow-change-held-packages docker-ce=' /calls
grep -Fxq 'apt-mark hold docker-ce docker-ce-cli containerd.io docker-compose-plugin' /calls
[[ $(grep -c '^systemctl daemon-reload$' /calls) == 1 ]]
reloaded=$(grep -n '^systemctl daemon-reload$' /calls | cut -d: -f1)
[[ $reloaded -lt $(grep -n '^apt-get install' /calls | head -n 1 | cut -d: -f1) ]]
grep -Fxq 'systemctl restart docker.service' /calls
absent 'start pyracast'
[[ $(grep -c '^network-guard$' /calls) == 1 ]]
guarded=$(grep -n '^network-guard$' /calls | cut -d: -f1)
[[ $guarded -gt $(grep -n '^apt-get install' /calls | tail -n 1 | cut -d: -f1) ]]
[[ ! -e /etc/pyracast/network-guard-pending ]]
no_downloaded_key
echo 'Boot regression passed: first boot'

mkdir -p /opt/pyracast/current/deploy
touch /opt/pyracast/current/deploy/compose.yaml
before=$(inodes)
boot
[[ $status == 0 ]]
[[ $(inodes) == "$before" ]]
absent mkfs
absent apt-get
absent curl
absent daemon-reload
absent 'systemctl restart'
absent network-guard
[[ $(tail -n 1 /calls) == 'systemctl start pyracast.service' ]]
echo 'Boot regression passed: unchanged boot'

render '{"domain":"example.com","acme_email":"ops@example.com","origin_pull_ca":""}'
boot
[[ $status == 0 ]]
grep -q origin_pull_ca /etc/pyracast/host.json
absent daemon-reload
absent 'systemctl restart'
echo 'Boot regression passed: changed host configuration'

installed_host
render "$host_json"
mkdir -p /etc/pyracast /etc/docker /etc/systemd/system/docker.service.d /usr/local/sbin /var/lib/pyracast
printf '%s' "$host_json" >/etc/pyracast/host.json
daemon_json=$(template_file /etc/docker/daemon.json)
data_conf=$(template_file /etc/systemd/system/docker.service.d/pyracast-data.conf)
printf '%s\n' "$daemon_json" >/etc/docker/daemon.json
printf '%s\n' "$data_conf" >/etc/systemd/system/docker.service.d/pyracast-data.conf
printf '%s\n' "$guard_script" >/usr/local/sbin/pyracast-network-guard
printf '[Unit]\nDescription=Pyracast application containers\n' >/etc/systemd/system/pyracast.service
echo 'UUID=1111-2222 /var/lib/pyracast ext4 defaults,nodev,nosuid 0 2' >>/etc/fstab
(umask 022 && : >/var/lib/pyracast/release.lock)
chmod 0644 /var/lib/pyracast/release.lock
host_inode=$(stat -c %i /etc/pyracast/host.json)
lock_inode=$(stat -c %i /var/lib/pyracast/release.lock)
boot
[[ $status == 0 ]]
[[ $(stat -c '%a %i' /var/lib/pyracast/release.lock) == "600 $lock_inode" ]]
[[ $(grep -c ' /var/lib/pyracast ' /etc/fstab) == 1 ]]
grep -Fxq 'UUID=1111-2222 /var/lib/pyracast ext4 defaults,nodev,nosuid,nofail 0 2' /etc/fstab
grep -Fxq 'LABEL=cloudimg-rootfs / ext4 discard,errors=remount-ro 0 1' /etc/fstab
grep -Fxq 'Restart=on-failure' /etc/systemd/system/pyracast.service
[[ $(stat -c %i /etc/pyracast/host.json) == "$host_inode" ]]
[[ $(grep -c '^systemctl daemon-reload$' /calls) == 1 ]]
absent apt-get
absent mkfs
absent network-guard
absent 'systemctl restart'
echo 'Boot regression passed: upgrade of a host the earlier script set up'

# Changed Docker configuration restarts Docker, but never while the release lock is held;
# the next run restarts it although the file it wrote is current by then.
echo '{}' >/etc/docker/daemon.json
exec 8>>/var/lib/pyracast/release.lock
flock 8
boot
[[ $status == 1 ]]
grep -Fq 'The Docker configuration changed while the release lock was held' /result
absent 'systemctl restart'
flock -u 8
boot
[[ $status == 0 ]]
[[ $(grep -c '^systemctl restart docker.service$' /calls) == 1 ]]
boot
[[ $status == 0 ]]
absent 'systemctl restart'
echo 'Boot regression passed: Docker restart under the release lock'

echo '5:29.9.2-1~ubuntu.24.04~noble' >/state/packages/docker-ce
flock 8
boot
[[ $status == 1 ]]
grep -Fq 'The pinned packages changed while the release lock was held' /result
absent apt-get
absent 'systemctl restart'
flock -u 8
boot
[[ $status == 0 ]]
grep -q '^apt-get install --yes --no-install-recommends --allow-change-held-packages docker-ce=' /calls
[[ $(cat /state/packages/docker-ce) == "${pins[0]#*=}" ]]
echo 'Boot regression passed: a package change waits for the release lock'

new_host
touch /state/apt-fails
boot
[[ $status != 0 ]]
absent network-guard
[[ -e /etc/pyracast/network-guard-pending ]]
rm /state/apt-fails
boot
[[ $status == 0 ]]
[[ $(grep -c '^network-guard$' /calls) == 1 ]]
[[ ! -e /etc/pyracast/network-guard-pending ]]
echo 'Boot regression passed: a stopped boot applies the network guard on its retry'

for keyring in "$other_key" "$pinned_key
$other_key"; do
  new_host
  printf '%s\n' "$keyring" >/state/keyring
  boot
  [[ $status == 1 ]]
  grep -Fq 'The Docker repository key does not have its published fingerprint' /result
  [[ ! -e /etc/apt/keyrings/docker.asc ]]
  absent 'docker-ce='
  no_downloaded_key
done
echo 'Boot regression passed: a repository keyring without exactly the published key is refused'

new_host
echo data >/state/head
boot
[[ $status == 1 ]]
grep -Fq 'has no ext4 filesystem but is not empty' /result
absent mkfs
echo 'Boot regression passed: a used disk is refused'

for release in 'ID=ubuntu\nVERSION_ID=22.04' 'ID=ubuntu'; do
  new_host
  printf 'NAME="Ubuntu"\n%b\n' "$release" >/etc/os-release
  boot
  [[ $status == 1 ]]
  grep -Fxq 'This boot script supports Ubuntu 24.04 on amd64 only; nothing was changed.' /result
  [[ ! -e /etc/pyracast/host.json ]]
  [[ ! -s /calls ]]
done
echo 'Boot regression passed: an unsupported host is refused'
