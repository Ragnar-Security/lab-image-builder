# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- An image builder, `scripts/image_build/build_image.sh`. It builds a VM image
  from a recipe on Proxmox, AWS or GCP: it starts a build VM from the
  backend's base, uploads the recipe's payload, runs setup and verify, seals
  the VM, saves the image, destroys the build VM and prints the
  `templates.yaml` lines. `--dry-run` prints the commands a build would run
  without running them (with `--reap` it still runs the read-only queries
  and prints the deletes); `--reap <backend>` removes expired build VMs a
  killed build left behind.
  Build resources are tagged `image-build` and `image-build-expires`. Operator values go in the
  untracked `scripts/image_build/config.env`, created from
  `config.env.example`. See `scripts/image_build/README.md`.
  - A setup script that needs a reboot exits 100; the builder reboots the VM
    itself and runs the next step only once the VM's boot ID has changed. An
    SSH session that drops during setup passes only if the boot ID changed.
    `IMAGE_BUILD_SSH_WAIT_SECONDS` (default 900) bounds every wait for SSH.
  - The recipe and `config.env` are sourced in a subshell. Only the recipe
    keys, and only the backend's own config keys (`PVE_`, `AWS_`, `GCP_`),
    are taken from them, so neither can switch off `--dry-run` or change the
    builder's state. A `config.env` that sets a recipe key is refused.
  - `BUILD-LOG.tsv` does not count as a change in the recipe repo's dirty
    check, so one recipe can be built on several backends before the log is
    committed.
  - Exit 2 only means the input was refused before the build began; any
    later failure exits 1.
  - The Linux seal disables and masks both `ssh.service` and `ssh.socket`
    (Ubuntu 22.10+ starts sshd from the socket) and writes
    `/etc/ssh/sshd_not_to_be_run`. It always locks `builder`'s password, and
    its host-key drop-in runs `ssh-keygen -A` before `sshd -t`.
  - On AWS, if `run-instances` fails or is interrupted before the instance ID
    is recorded, the cleanup finds the instance by this run's `image-build`
    tag and terminates it.
  - `scripts/image_build/proxmox_base_ubuntu.sh` builds the Proxmox Linux
    base, `base-ubuntu-2404-<yyyymmdd>`, from the official Ubuntu 24.04 cloud
    image to the README's base-template contract: `builder` with passwordless
    sudo and no key, guest agent and OpenSSH installed, DHCP matched by
    interface name, no host keys, empty machine ID. It is a dry run unless
    given `--apply`, reads `PVE_HOST`, `PVE_NODE`, `PVE_STORAGE`,
    `PVE_BUILD_BRIDGE` and `PVE_BUILD_VLAN` from `config.env`, and builds at
    the lowest free VMID pair in 8000-8499 (+500) on the build network. It
    removes the cloud-init drive before templating and deletes its first-boot
    user-data snippet on every exit, failing loudly if it cannot. A failed or
    interrupted run destroys its half-made VM only when the VM carries the
    run's construction marker. The README explains how to rebuild the base for
    a new point release.
  - On Proxmox, a build stops before cloning unless the recipe's `DISK_GB`
    equals the base template's disk size; the backend does not resize.
  - `scripts/image_build/proxmox_base_windows.sh` builds the Proxmox Windows
    base, `base-ws2022-<yyyymmdd>`, unattended from the Windows Server 2022
    evaluation ISO and virtio-win, with the same dry-run, marker and cleanup
    contract. Each clone gets its own random Administrator password in
    specialize; no answer file keeps a password; `sshd` starts only after a
    clone's first-boot setup.
  - The Windows seal deletes the cached DHCP lease, so Proxmox clones of a
    built Windows image request their own address.
