#!/usr/bin/env bash
#
# Create the Proxmox base template for Linux image builds,
# base-ubuntu-<release>-<yyyymmdd>, from the official Ubuntu cloud image of
# that release (2404 by default; 2204 and 2004 for labs whose packages need
# an older release). Runs on the build host, next to config.env, and reaches
# PVE_HOST as root over SSH (key auth), as build_image.sh does.
#
# Usage: scripts/image_build/proxmox_base_ubuntu.sh [--apply] [options]
#   --release R       2004, 2204 or 2404 (default 2404): Ubuntu 20.04, 22.04
#                     or 24.04
#   --image PATH      cloud image on PVE_NODE (default
#                     /var/lib/vz/template/iso/<codename>-server-cloudimg-amd64.img,
#                     codename focal, jammy or noble from --release)
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
#   - user builder: passwordless sudo, password locked, no SSH key;
#   - openssh-server (enabled: ssh.socket on 2404, ssh.service on 2004 and
#     2204, the unit that owns port 22 on each) and qemu-guest-agent installed;
#   - DHCP on any en*/eth* interface (netplan matched by name, not MAC:
#     a clone has a new MAC and no cloud-init drive), cloud-init's own
#     network config disabled;
#   - no SSH key, authorized_keys or password anywhere; no host keys (a
#     drop-in runs ssh-keygen -A when ssh.service starts, as the seal's does);
#   - cloud-init cleaned, /etc/machine-id empty, no cloud-init drive.
# First boot runs cloud-init from a user-data snippet (packages, user); the
# rest is done through the guest agent; the snippet is deleted afterwards.
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
RELEASE=2404
IMAGE=""
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
        --release) RELEASE="${2?--release needs 2004, 2204 or 2404}"; shift ;;
        --image) IMAGE="${2:?--image needs a path}"; shift ;;
        --snippets) SNIPPETS="${2:?--snippets needs a storage}"; shift ;;
        --vmid) VMID="${2:?--vmid needs a VMID}"; shift ;;
        --date) DATE="${2:?--date needs YYYYMMDD}"; shift ;;
        --disk) DISK="${2:?--disk needs a size}"; shift ;;
        -h|--help) sed -n '3,52p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) printf 'ERROR: unknown option %s\n' "$1" >&2; exit 2 ;;
    esac
    shift
done

# Ubuntu 22.10 moved port 22 to ssh.socket (socket activation). On 20.04 and
# 22.04 ssh.service owns it, and enabling ssh.socket there as well makes the
# two units conflict at boot.
case "$RELEASE" in
    2004) VERSION=20.04; CODENAME=focal; SSH_UNIT=ssh.service ;;
    2204) VERSION=22.04; CODENAME=jammy; SSH_UNIT=ssh.service ;;
    2404) VERSION=24.04; CODENAME=noble; SSH_UNIT=ssh.socket ;;
    *) die "--release must be one of 2004 2204 2404 (got '$RELEASE')" ;;
esac
IMAGE="${IMAGE:-/var/lib/vz/template/iso/$CODENAME-server-cloudimg-amd64.img}"

NAME="base-ubuntu-$RELEASE-$DATE"

# --- first-boot user-data: no key, no password -----------------------------
# @RELEASE@ and @SSH_UNIT@ are filled in from --release; the rest is literal.
fill() { sed -e "s/@RELEASE@/$RELEASE/g" -e "s/@SSH_UNIT@/$SSH_UNIT/g"; }
user_data() {
    fill <<'EOF'
#cloud-config
# base-ubuntu-@RELEASE@ first boot (proxmox_base_ubuntu.sh). No keys, no passwords.
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
runcmd:
  - [systemctl, enable, @SSH_UNIT@]
  - [systemctl, start, qemu-guest-agent]
EOF
}

# --- finalize, run as root in the guest through the agent ------------------
finalize_script() {
    fill <<'EOF'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
step() { printf '== %s\n' "$*"; }

step "contract: builder, sudo, packages"
id builder
if id ubuntu >/dev/null 2>&1; then echo "user ubuntu exists; the base must hold builder only" >&2; exit 1; fi
runuser -u builder -- sudo -n true
echo "builder: passwordless sudo ok"
dpkg-query -W -f='${Package} ${Version}\n' qemu-guest-agent openssh-server
systemctl is-active qemu-guest-agent
systemctl is-enabled @SSH_UNIT@

step "network: DHCP on any en*/eth* interface, not keyed to this MAC"
cat > /etc/netplan/01-image-build-dhcp.yaml <<'NETPLAN'
# Written by proxmox_base_ubuntu.sh. Matched by name, not MAC: every clone
# has a new MAC and no cloud-init drive.
network:
  version: 2
  ethernets:
    en-any:
      match:
        name: "en*"
      dhcp4: true
      dhcp-identifier: mac
    eth-any:
      match:
        name: "eth*"
      dhcp4: true
      dhcp-identifier: mac
NETPLAN
chmod 600 /etc/netplan/01-image-build-dhcp.yaml
rm -f /etc/netplan/50-cloud-init.yaml
printf 'network: {config: disabled}\n' > /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg
netplan generate
ls -l /etc/netplan/

step "ssh: no host keys; a clone generates its own when ssh.service starts"
mkdir -p /etc/systemd/system/ssh.service.d
printf '[Service]\nExecStartPre=\nExecStartPre=/usr/bin/ssh-keygen -A\nExecStartPre=/usr/sbin/sshd -t\n' \
  > /etc/systemd/system/ssh.service.d/10-regen-hostkeys.conf
systemctl daemon-reload
rm -f /etc/ssh/ssh_host_*

step "no keys, no passwords"
find / -xdev \( -name authorized_keys -o -name authorized_keys2 \) -print -delete
passwd -l root
passwd -l builder
passwd -S builder
passwd -S root

step "identity and logs"
apt-get clean
cloud-init clean --logs
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

BASE_LABEL="Ubuntu $VERSION"
# shellcheck source=proxmox_base_lib.sh
source "$SCRIPT_DIR/proxmox_base_lib.sh"
