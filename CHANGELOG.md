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
- An Azure backend, `backends/azure.sh` (`build_image.sh <recipe> azure`),
  run live once (2026-11-15, Linux; no Windows image produced yet). Each
  build gets its own resource group. Azure has no create-only call for a
  group (it ignores `If-None-Match` on the PUT), so a fail-closed read
  refuses an existing group first; the PUT carries only a creation token (a
  random 128-bit nonce) that no reap matches, and the group is owned only on
  `201 Created`: only then is it tagged with the run ID, its expiry and the
  same nonce as its owner, and all four are read back before the build goes
  on. The cleanup and the reap delete only a group whose creation token and
  owner nonce agree, so a same-name group that replaced it is never deleted.
  The image is a specialized version (definition `SecurityType=Standard`)
  in an operator's Azure Compute Gallery. On Windows the built-in
  Administrator, which Azure renames to the admin user, is found by SID and
  named `Administrator` again. Config keys `AZ_*`; recipe keys `BASE_AZURE`
  and `AZURE_VM_SIZE`, required only for Azure builds. Reads fail closed:
  only an exact not-found code counts as absent, and a reap refuses an
  expiry that is not a real calendar minute.
- Optional recipe keys `AWS_DISK_GB`, `GCP_DISK_GB` and `AZURE_DISK_GB`
  override `DISK_GB` on that backend only (GCP's Windows images are 50 GB and
  Azure's 127 GB, while Proxmox requires `DISK_GB` to equal its base).
- Two optional parts of the backend contract: `BACKEND_RECIPE_KEYS` (recipe
  keys a backend requires) and `backend_reboot` (a backend's own reboot for
  a setup that exits 100).
- `proxmox_base_windows.sh --os ws2022|ws2019|ws2016` (default `ws2022`,
  unchanged) builds `base-<os>-<yyyymmdd>` from that release's evaluation
  ISO: the image name, the virtio-win driver directory, the default `--iso`
  and the Proxmox `ostype` follow the release. Server 2016 has no OpenSSH
  capability, so its base installs one pinned Win32-OpenSSH MSI
  (`windows_base/install-openssh.ps1`), only if its SHA-256 matches the pin;
  and where .NET offers no TLS 1.2 by default the base turns it on.
- `expand_payload.ps1`: on a Windows build VM without `tar.exe` (Server 2016
  and older) the core extracts the payload with .NET instead. It checks the
  whole gzip stream against its trailer (CRC-32 and length) before writing
  anything, and refuses links, paths outside the payload directory and
  damaged tar headers.
- Proxmox: optional `PVE_BUILD_JUMP_HOST`, the address the build VM is
  reached through over SSH. A build node with no route to the build VLAN
  (here `pve2`) builds by jumping through a node that has one. Unset or
  empty, the jump is `root@PVE_HOST` as before. Checked with the other
  config values before any SSH call.

### Changed

- Run IDs carry 16 random hex digits instead of 4 (`ib-<yyyymmddhhmm>-<16
  hex>`). The Proxmox, AWS and GCP reaps still accept the 4-hex IDs of builds
  started before the change.

### Fixed

- Every clone of a Windows image gets its own SSH host keys. Before, a
  Windows image kept its build VM's keys: every clone of a `KEEP_SSH=yes`
  image presented the same host key, and every clone of any Windows image
  carried the same private host keys on disk. `seal_windows.ps1` now deletes
  `C:\ProgramData\ssh\ssh_host_*` in both modes and fails if one survives.
  With `-KeepSsh` it also leaves sshd `Manual` and registers a SYSTEM
  startup task: on Server 2016 the service manager restarts sshd during the
  save's shutdown, which writes new keys into the image after the seal. On a
  clone's first boot the task makes the clone's own keys with `ssh-keygen -A`,
  checks they exist and differ from the image's, and only then sets sshd
  `Automatic` and starts it; on failure sshd stays `Manual` and stopped and
  the task retries at the next boot (5 attempts). Once sshd runs on the new
  keys, the cleanup (unregister the task, delete the script, log) is best
  effort and never stops sshd. The task's script and
  directory, the task and its file are administrators-only, and the seal
  reads that back and fails otherwise. `seal_linux.sh` already deleted the
  keys in both modes.

- A build stopped by SIGTERM no longer leaves its build VM running when the
  signal also killed the reader of the builder's stderr (a `tee` into a log)
  and the in-flight setup `ssh`. The trap's first log line hit the dead pipe,
  SIGPIPE killed the shell, and the destroy never ran. The trap now ignores
  SIGPIPE and moves stderr to `/dev/null` once a write fails. It kills and
  reaps the in-flight child first, and it ignores further HUP, INT and TERM
  from the moment a signal arrives, so a second signal cannot interrupt the
  destroy. SIGHUP is now trapped like INT and TERM and exits 129. Before, the
  shell's default handling ran the trap but died of the signal, so the exit
  code did not say what stopped the build.

- The exit trap can no longer hang. The in-flight command and everything it
  started get TERM, then KILL after `IMAGE_BUILD_CHILD_STOP_SECONDS` (default
  20). The run's temporary directory (build key, `payload.tgz`) is removed
  before the destroy, so a stuck destroy cannot leave it behind. The destroy
  and everything it started are KILLed after
  `IMAGE_BUILD_DESTROY_TIMEOUT_SECONDS` (default 900), and the trap then names
  the backend and the build VM to destroy by hand. Before, a command or
  destroy that never returned left the builder stoppable only by SIGKILL,
  which also left the build key on disk. The trap finds those processes with
  `pgrep -P`, so a build or `--reap` now needs `pgrep` and refuses to start
  without it (exit 2); a dry run does not.

- A Windows build on a Proxmox clone of `base-ws2022` no longer fails at the
  payload upload when `sshd` answers once at first boot and then drops the
  next connection (exit 255 or a banner-exchange timeout, measured two or
  three times per build). Every wait for SSH now needs three answers in a
  row, 10 seconds apart, including the wait after a reboot once the new boot
  ID shows up. Each idempotent upload step (create the payload directory,
  copy `payload.tgz`, extract it, remove it) is retried on an SSH or SCP exit
  255, every 10 seconds, until `IMAGE_BUILD_SSH_WAIT_SECONDS` runs out or the
  build's expiry arrives, whichever is first; no attempt or sleep runs past
  that deadline. Any other exit fails the build at once, and setup, verify
  and seal are never retried. The extract no longer deletes the archive itself,
  so a retried extract still finds it. Builds no longer need an operator's
  `ssh`/`scp` retry wrapper.

- A Windows setup pass that asks for a reboot no longer lets the next pass
  start in the boot that is going down. The Windows boot ID was
  `LastBootUpTime`, which on a domain controller build read one second later
  right after the reboot command, before the VM went down, and the builder
  took the change for the reboot. The boot ID is now the creation time of the
  System process (PID 4), which no clock step, slew or time zone change moved
  on Windows Server 2022, and which is read with `Get-Process`, without WMI or
  the event log.
