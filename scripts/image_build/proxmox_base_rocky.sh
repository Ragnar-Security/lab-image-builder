#!/usr/bin/env bash
#
# Create the Proxmox base template for Linux image builds on the RHEL family,
# base-rocky-9-<yyyymmdd>, from the official Rocky Linux 9 GenericCloud image.
# Runs on the build host, next to config.env, and reaches PVE_HOST as root
# over SSH (key auth), as build_image.sh does. Same engine, options and
# guarantees as proxmox_base_ubuntu.sh (proxmox_base_lib.sh).
#
# Usage: scripts/image_build/proxmox_base_rocky.sh [--apply] [options]
#   --image PATH      cloud image on PVE_NODE (default
#                     /var/lib/vz/template/iso/Rocky-9-GenericCloud-Base.latest.x86_64.qcow2)
#   --snippets STORE  storage with the `snippets` content type, for the
#                     first-boot user-data (default local)
#   --vmid N          VMID in 8000-8499 (default: the lowest V with V and
#                     V+500 free, so the replica can sit at V+500)
#   --date YYYYMMDD   date in the name (default today, UTC)
#   --disk SIZE       root disk, e.g. 20G (default 20G); the Proxmox backend
#                     does not resize, so this is every image's disk size
#
# Dry run by default: reads the cluster and prints every command that would
# change it. --apply runs them. It never touches an existing guest: a VMID in
# use, a VMID outside 8000-8499, or a template name already taken is an
# error. Config: the PVE_ keys of config.env (IMAGE_BUILD_CONFIG overrides
# the path); it uses PVE_HOST, PVE_NODE, PVE_STORAGE, PVE_BUILD_BRIDGE and
# PVE_BUILD_VLAN.
#
# The template meets the README's base-template contract:
#   - user builder: passwordless sudo, password locked, no SSH key, and an
#     empty ~/.ssh labelled for sshd (ssh_home_t), so the key the builder
#     injects is readable under SELinux;
#   - openssh-server (sshd.service enabled) and qemu-guest-agent running,
#     with guest-exec allowed: Rocky's /etc/sysconfig/qemu-ga allow-list
#     leaves it out, and the builder injects its key and this script runs its
#     finalize through it. A second environment file empties the filter, and
#     the agent's SELinux domain (virt_qemu_ga_t) is permissive, as the agent
#     is unconfined on Ubuntu. SELinux itself stays enforcing;
#   - DHCP on every en*/eth* interface from one NetworkManager profile
#     matched by name, not MAC (multi-connect, DHCP client ID from the MAC);
#     cloud-init's own network profile removed;
#   - no SSH key, authorized_keys or password anywhere; no host keys
#     (sshd-keygen@.service makes them when sshd starts);
#   - cloud-init cleaned and then disabled (a clone has no datasource),
#     /etc/machine-id empty, no cloud-init drive.
# First boot runs cloud-init from a user-data snippet (packages, user,
# guest-exec); the rest is done through the guest agent; the snippet is
# deleted afterwards.
#
# A run that fails or is interrupted (INT, TERM, HUP) after it began to
# create the VM destroys that VM, but only after the node shows it is not a
# template and its description carries this run's construction marker
# (proxmox-base-build=<time>-<random>, written by qm create itself); any
# other VM at that VMID is named and left alone. The snippet is deleted on
# every exit, and an error names it if the node cannot confirm it is gone.
#
# Then prove the base (README "Base-template contract") with a test clone and
# replicate it: scripts/sync_template.sh <PVE_HOST> <vmid> <replica host>
#   <replica storage> <vmid+500>.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${IMAGE_BUILD_CONFIG:-$SCRIPT_DIR/config.env}"

APPLY=0
IMAGE=/var/lib/vz/template/iso/Rocky-9-GenericCloud-Base.latest.x86_64.qcow2
SNIPPETS=local
VMID=""
DATE="$(date -u +%Y%m%d)"
DISK=20G

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*" >&2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --apply) APPLY=1 ;;
        --dry-run) APPLY=0 ;;
        --image) IMAGE="${2:?--image needs a path}"; shift ;;
        --snippets) SNIPPETS="${2:?--snippets needs a storage}"; shift ;;
        --vmid) VMID="${2:?--vmid needs a VMID}"; shift ;;
        --date) DATE="${2:?--date needs YYYYMMDD}"; shift ;;
        --disk) DISK="${2:?--disk needs a size}"; shift ;;
        -h|--help) sed -n '3,57p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) printf 'ERROR: unknown option %s\n' "$1" >&2; exit 2 ;;
    esac
    shift
done

NAME="base-rocky-9-$DATE"

# --- first-boot user-data: no key, no password -----------------------------
# write_files and runcmd run as root under cloud-init, before this script's
# first guest-exec, which waits for them (it polls `cloud-init status`).
user_data() {
    cat <<'EOF'
#cloud-config
# base-rocky-9 first boot (proxmox_base_rocky.sh). No keys, no passwords.
users:
  - name: builder
    gecos: image builder
    shell: /bin/bash
    lock_passwd: true
    sudo: "ALL=(ALL) NOPASSWD:ALL"
disable_root: true
ssh_pwauth: false
package_update: true
package_upgrade: true
packages:
  - qemu-guest-agent
  - openssh-server
  - policycoreutils-python-utils
write_files:
  - path: /etc/qemu-ga/image-build.env
    permissions: "0644"
    content: |
      # proxmox_base_rocky.sh: every RPC allowed (an empty block list), as on
      # Ubuntu. Overrides FILTER_RPC_ARGS from /etc/sysconfig/qemu-ga.
      FILTER_RPC_ARGS=--block-rpcs=
  - path: /etc/systemd/system/qemu-guest-agent.service.d/10-image-build-exec.conf
    permissions: "0644"
    content: |
      # proxmox_base_rocky.sh: the image builder injects its per-run key with
      # guest-exec, which Rocky's allow-list leaves out. A later
      # EnvironmentFile wins for the same variable.
      [Service]
      EnvironmentFile=/etc/qemu-ga/image-build.env
runcmd:
  - [semanage, permissive, -a, virt_qemu_ga_t]
  - [systemctl, enable, sshd.service]
  - [systemctl, daemon-reload]
  - [systemctl, restart, qemu-guest-agent]
EOF
}

# --- finalize, run as root in the guest through the agent ------------------
finalize_script() {
    cat <<'EOF'
set -euo pipefail
step() { printf '== %s\n' "$*"; }

step "release"
cat /etc/rocky-release
getenforce

step "contract: builder, sudo, packages"
id builder
if id rocky >/dev/null 2>&1; then echo "user rocky exists; the base must hold builder only" >&2; exit 1; fi
runuser -u builder -- sudo -n true
echo "builder: passwordless sudo ok"
rpm -q qemu-guest-agent openssh-server policycoreutils-python-utils
systemctl is-active qemu-guest-agent
systemctl is-enabled sshd.service
semanage permissive -l | grep -x 'virt_qemu_ga_t'

step "network: DHCP on any en*/eth* interface, not keyed to this MAC"
rm -f /etc/NetworkManager/system-connections/cloud-init-*.nmconnection \
      /etc/sysconfig/network-scripts/ifcfg-eth* /etc/sysconfig/network-scripts/ifcfg-en*
cat > /etc/NetworkManager/system-connections/image-build-dhcp.nmconnection <<NMCONN
# Written by proxmox_base_rocky.sh. Matched by name, not MAC: every clone
# has a new MAC and no cloud-init drive. multi-connect=3 activates this one
# profile on every matching interface at once.
[connection]
id=image-build-dhcp
uuid=$(cat /proc/sys/kernel/random/uuid)
type=ethernet
autoconnect=true
multi-connect=3

[match]
interface-name=en*;eth*;

[ipv4]
method=auto
dhcp-client-id=mac

[ipv6]
method=auto
NMCONN
chmod 600 /etc/NetworkManager/system-connections/image-build-dhcp.nmconnection
printf 'network: {config: disabled}\n' > /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg
nmcli connection reload
nmcli -t -f NAME,TYPE connection show | grep -x 'image-build-dhcp:802-3-ethernet'
ls -l /etc/NetworkManager/system-connections/

step "ssh: no host keys; a clone's sshd-keygen@.service makes its own"
systemctl list-unit-files 'sshd-keygen@.service' --no-legend
rm -f /etc/ssh/ssh_host_*

step "no keys, no passwords"
find / -xdev \( -name authorized_keys -o -name authorized_keys2 \) -print -delete
install -d -m 700 -o builder -g builder /home/builder/.ssh
passwd -l root
passwd -l builder
passwd -S builder
passwd -S root

step "SELinux labels of what this script and cloud-init wrote"
restorecon -R /etc /home/builder
ls -Zd /home/builder/.ssh

step "identity and logs"
dnf clean all
cloud-init clean --logs
touch /etc/cloud/cloud-init.disabled
truncate -s 0 /etc/machine-id
if [ -e /var/lib/dbus/machine-id ] && [ ! -L /var/lib/dbus/machine-id ]; then rm -f /var/lib/dbus/machine-id; fi
for f in /root/.bash_history /home/*/.bash_history; do
  if [ -f "$f" ]; then truncate -s 0 "$f"; fi
done
fstrim -av || true
sync
echo finalized
EOF
}

BASE_LABEL="Rocky Linux 9"
# shellcheck source=proxmox_base_lib.sh
source "$SCRIPT_DIR/proxmox_base_lib.sh"
