# Image builder

`build_image.sh` builds a VM image from a recipe on one backend: Proxmox, AWS,
GCP or Azure. It is plain bash plus each platform's own CLI. There is no
Packer, Vagrant, Ansible or Terraform, and no disk conversion (no
`import-image`, no Google image import). Each backend builds from its own
base, so one recipe yields one native image per platform.

This directory is in a public repo. It holds no hosts, account or project IDs,
image IDs or keys. Those go in `config.env`, which is untracked.

## What it does

```
build_image.sh [--dry-run] [--allow-dirty] <recipe.env> <backend>
build_image.sh --reap <backend> [--dry-run]
```

`<backend>` is `proxmox`, `aws`, `gcp` or `azure`. A build runs these steps in order:

1. Read the recipe and the config, and check them. Name the image.
2. Resolve the base image or template for the backend.
3. Start a build VM from it, with a fresh SSH key made for this run, and wait
   until SSH answers three times in a row, 10 seconds apart (a failed answer
   starts the count again).
4. Upload the recipe's payload to the VM, over SSH. Each upload step (create
   the payload directory, copy `payload.tgz`, extract it, remove it) is
   idempotent, so one whose SSH or SCP connection fails with exit 255 is run
   again every 10 seconds, until `IMAGE_BUILD_SSH_WAIT_SECONDS` runs out or
   the build expires (`IMAGE_BUILD_TTL_MINUTES`), whichever comes first; any
   other failure stops the build at once. Setup, verify and seal are never
   retried. See "SSH at first boot".
5. Run the setup script, `SETUP_PASSES` times. After each pass, wait for SSH;
   if the setup asked for a reboot (exit 100), reboot the VM, wait for a new
   boot and then for three answers in a row. See "Rules for setup scripts".
6. Run the verify script. A failure stops the build; nothing is saved.
7. Seal the VM (`seal_linux.sh` or `seal_windows.ps1`). A failure stops the
   build; nothing is saved.
8. Save the image on the backend.
9. Append a line to the build log.
10. Destroy the build VM and print the `templates.yaml` lines.

A trap destroys the build VM on every exit, including an error, Ctrl-C,
SIGTERM and SIGHUP, in any phase (start, SSH wait, upload, setup, verify,
seal, save), and then removes the run's temporary directory (the build key
and `payload.tgz`). If the destroy itself fails, the script says so and tells
you to run `--reap`. Only a build that passed verify and seal is saved, logged
and printed.

The trap holds up when the signal also killed the processes around the
builder:

- It sends TERM to the in-flight child (the setup `ssh`, an upload `scp`)
  and everything under it (found with `pgrep -P`), waits up to
  `IMAGE_BUILD_CHILD_STOP_SECONDS` (default 20), then KILLs whatever is
  left. The destroy's process tree is killed the same way at its bound.
- It removes the run's temporary directory (the build key and
  `payload.tgz`) next, before the destroy: no backend's destroy needs them,
  so they are gone even if the destroy hangs.
- It ignores SIGPIPE, and its first log line tells it whether stderr still
  has a reader. If the signal also killed the reader (a `tee`, or the pipe
  into a log file), stderr moves to `/dev/null`, so neither the trap nor the
  destroy it runs dies writing to the dead pipe. Its messages are then lost;
  the exit code still says the build was stopped.
- It ignores every further HUP, INT and TERM, and so do the tools the destroy
  runs, so a second signal (another Ctrl-C, a signal to the whole process
  group) cannot cut the destroy short. The trap runs once; the destroy is run
  once.
- Because it cannot be interrupted, the destroy is bounded instead: after
  `IMAGE_BUILD_DESTROY_TIMEOUT_SECONDS` (default 900) it is KILLed, and the
  trap prints one line starting `ERROR: the destroy did not finish` that
  names the backend and the build VM to destroy by hand. The exit code stays
  the signal's (or 1 on a failure). Some destroys wait for the deletion to
  finish (an Azure resource-group delete; on AWS, `aws ec2 wait
  instance-terminated`, which gives up after 10 minutes), so do not set the
  bound much lower on those backends.

To stop a build, signal the `build_image.sh` process itself
(`kill -TERM <pid>`). Whether it shares a process group with whatever launched
it depends on the launcher, so a signal to the launcher's group may not reach
it. `kill -9` cannot be trapped: a build stopped that way leaves its build VM
for `--reap`.

**It does not:** edit `templates.yaml` (it prints lines for you to paste),
push anything, touch the portal's database, or copy an image between clouds.
It never overwrites an existing image.

### Output and exit codes

Stdout carries only the `templates.yaml` lines (for `--reap`, the IDs it
reaped). Everything else, including every `DRY-RUN:` line, goes to stderr.

| Exit | Meaning |
|---|---|
| 0 | Success |
| 1 | A build or reap failed while running. This includes errors a backend raises after the config passed: a malformed `BASE_AWS`, `BASE_GCP` or `BASE_AZURE`, no image matching the base, a base of the other OS family (Azure), a malformed `AWS_INSTANCE_TYPE`, a bad public key, a VMID outside the Proxmox block, a failed setup, verify or seal, no SSH (or no new boot after a reboot) within `IMAGE_BUILD_SSH_WAIT_SECONDS`, a destroy that was refused. Once the build has begun (base resolution onwards), every failure is 1, even one where a backend or a tool it ran exited 2. |
| 2 | The core rejected the input before any VM started: bad usage or option, no `pgrep` (except in a dry run), unknown backend, unreadable recipe, a missing or invalid recipe key (`OS_FAMILY`, `KEEP_SSH`, the integer keys including a set `*_DISK_GB` override, `PAYLOAD_DIR`, `SETUP`/`VERIFY`), a missing `BASE_<BACKEND>` key, a recipe key the backend requires (`AZURE_VM_SIZE` on Azure), a recipe outside a git repo, a dirty repo, an invalid `IMAGE_BUILD_TTL_MINUTES`, `IMAGE_BUILD_SSH_WAIT_SECONDS`, `IMAGE_BUILD_CHILD_STOP_SECONDS` or `IMAGE_BUILD_DESTROY_TIMEOUT_SECONDS`, an invalid image name, a config that sets a recipe key, or a config the backend's `backend_check_config` rejects. |
| 129, 130, 143 | Hung up (HUP), interrupted (INT) or terminated (TERM); the trap still ran |

### Image names and the build log

The image is named `<IMAGE_PREFIX>-<sha7>-<yyyymmddhhmm>` (UTC). `sha7` is the
first seven characters of the HEAD commit of the git repo that holds the
recipe. The name may contain only `a-z`, `0-9` and `-`, must start with a
letter, and is at most 63 characters, so `IMAGE_PREFIX` is at most 42.

The recipe's repo must be clean, or the build stops with exit 2. The one
exception is the build log itself (`coursework/deploy/image-build/BUILD-LOG.tsv`,
new or modified), so you can build one recipe on several backends and commit
the log once at the end. Any other change, even next to the log, still
refuses the build. `--allow-dirty` overrides that for a self-test; the commit
is then recorded as `<sha7>-dirty`. Do not use it for a real build.

Each saved image adds one tab-separated line to
`coursework/deploy/image-build/BUILD-LOG.tsv` in the recipe's repo:
`date, backend, image_id, image_name, base_id, recipe, commit, verify`.
A dry run only prints the line.

## Config keys

Copy `config.env.example` to `config.env` (untracked) and fill in the section
for your backend. Set `IMAGE_BUILD_CONFIG=/path/to/file` to use another file.
The backend checks its own keys and names any that are missing or malformed.

The file is sourced in a subshell, and only the keys of the backend you build
on come back: names starting `PVE_` (Proxmox), `AWS_`, `GCP_` or `AZ_`
(Azure). Anything else it sets is ignored, so it cannot change the builder's
own state (for example `DRY_RUN`). A config that sets a recipe key
(`DISK_GB`, `AWS_INSTANCE_TYPE`, `GCP_MACHINE_TYPE`, `AZURE_VM_SIZE`,
`PVE_CORES`, a `BASE_*` key, ...) stops the build with exit 2: only the
recipe sets those.

| Backend | Key | Meaning |
|---|---|---|
| Proxmox | `PVE_HOST` | Address of the build node. Reached as root over SSH, key auth. Also the jump host to the build VM. |
| Proxmox | `PVE_NODE` | Node that runs the build VM (default `pve`). Lowercase letters, digits and `-` only, as `templates.yaml` takes it; checked before the build. |
| Proxmox | `PVE_REPLICA_NODE` | Node that receives the template copy (default `pve2`). Same rule as `PVE_NODE`. |
| Proxmox | `PVE_REPLICA_HOST` | Address of the replica node. |
| Proxmox | `PVE_STORAGE` | Storage for the build VM's disk on `PVE_NODE`. |
| Proxmox | `PVE_REPLICA_STORAGE` | Storage for the replica template. |
| Proxmox | `PVE_BUILD_BRIDGE` | Bridge of the provisioning network (has internet). |
| Proxmox | `PVE_BUILD_VLAN` | VLAN tag of that network, 1-4094. |
| AWS | `AWS_BUILD_REGION` | Region. Every call names it; a profile default never decides. |
| AWS | `AWS_BUILD_SUBNET_ID` | A public subnet in the build VPC, not the lab VPC. |
| AWS | `AWS_BUILD_SG_ID` | Security group that admits SSH from the build host only. |
| GCP | `GCP_BUILD_PROJECT` | Project ID. |
| GCP | `GCP_BUILD_ZONE` | Zone, for example `us-east1-b`. |
| GCP | `GCP_BUILD_SUBNET` | Subnet of the build network, separate from the lab network. |
| Azure | `AZ_SUBSCRIPTION_ID` | Subscription ID (a GUID). Every call names it; the CLI's default subscription never decides. |
| Azure | `AZ_BUILD_LOCATION` | Location, for example `eastus`. The build subnet and the gallery must be in it. |
| Azure | `AZ_BUILD_SUBNET_ID` | Resource ID of the build subnet, `/subscriptions/<id>/resourceGroups/<rg>/providers/Microsoft.Network/virtualNetworks/<vnet>/subnets/<subnet>`, in `AZ_SUBSCRIPTION_ID`. Its NSG admits `tcp/22` from the build host only. |
| Azure | `AZ_GALLERY_RG` | Resource group of the Azure Compute Gallery. |
| Azure | `AZ_GALLERY` | Name of the gallery (letters, digits, `.` and `_`). |

Every build and every `--reap` needs `pgrep` (procps on Linux; macOS ships
it). The exit trap uses `pgrep -P` to find everything a stopped command
started, so the builder refuses to start without it (exit 2); a dry run does
not need it.

The Proxmox backend needs `python3` on the machine running the build. The AWS
backend needs the AWS CLI v2 on `PATH`, the GCP backend `gcloud`, and the
Azure backend `az`, logged in (none is checked in a dry run).

`IMAGE_BUILD_TTL_MINUTES` (environment, default 240) sets how long a build's
resources live before `--reap` may remove them.
`IMAGE_BUILD_SSH_WAIT_SECONDS` (environment, default 900) bounds each wait for
the build VM: for SSH after it starts and after each setup pass, for a new
boot after a reboot, and for the payload upload's retries.
`IMAGE_BUILD_BACKENDS_DIR` (environment) changes where `<backend>.sh` is read
from.
`IMAGE_BUILD_CHILD_STOP_SECONDS` (environment, default 20) and
`IMAGE_BUILD_DESTROY_TIMEOUT_SECONDS` (environment, default 900) bound the
exit trap: how long the running command gets after TERM before KILL, and how
long the trap's destroy may run (see "What it does").

## Recipe keys

A recipe is a sourced `.env` file inside a git repo. Only the recipe may set
these keys; values in your shell environment are ignored. It is sourced in a
subshell and only these keys are taken from it: anything else it sets or
defines (a variable such as `DRY_RUN`, a function) has no effect on the build.
Values arrive exactly as written, spaces, quotes and globs included.

| Key | Required | Meaning |
|---|---|---|
| `IMAGE_PREFIX` | yes | Start of the image name. At most 42 characters. |
| `OS_FAMILY` | yes | `linux` or `windows`. |
| `PAYLOAD_DIR` | yes | Directory to upload, relative to the recipe file. |
| `SETUP` | yes | Setup script, relative to `PAYLOAD_DIR`. |
| `VERIFY` | yes | Verify script, relative to `PAYLOAD_DIR`. |
| `BASE_PROXMOX` | yes | Proxmox: a template name glob. The newest match on `PVE_NODE` is used. |
| `BASE_AWS` | yes | AWS: `<owner>:<AMI name glob>`. Owner is `self`, `amazon` or an account ID. The newest available AMI wins. |
| `BASE_GCP` | yes | GCP: `<image project>/<image family>`. |
| `BASE_AZURE` | Azure only | Azure: a marketplace URN `<publisher>:<offer>:<sku>:<version>` (`latest` resolves to the newest version, which the build log records), or a gallery image-version resource ID `/subscriptions/.../galleries/<gallery>/images/<definition>/versions/<X.Y.Z>`. |
| `DISK_GB` | yes | Root disk size in GB. On Proxmox it must equal the base template's disk size: the build checks it and stops otherwise (see "Proxmox"). |
| `AWS_INSTANCE_TYPE` | yes | For example `t3.small`. |
| `GCP_MACHINE_TYPE` | yes | For example `e2-standard-2`. |
| `AZURE_VM_SIZE` | Azure only | For example `Standard_B2s`. |
| `PVE_MEMORY_MB` | yes | Build VM memory on Proxmox. |
| `PVE_CORES` | yes | Build VM cores on Proxmox. |
| `AWS_DISK_GB` | no | Root disk size on AWS only, instead of `DISK_GB`. |
| `GCP_DISK_GB` | no | Boot disk size on GCP only, instead of `DISK_GB`. `windows-cloud/windows-2022` is a 50 GB image, so GCE refuses a smaller disk. |
| `AZURE_DISK_GB` | no | OS disk size on Azure only, instead of `DISK_GB`. Azure's Windows marketplace images are 127 GB. |
| `SETUP_PASSES` | no | How many times to run `SETUP` (default 1). |
| `KEEP_SSH` | no | `yes` or `no` (default `no`). See "Host SSH". |
| `GCP_IMAGE_FAMILY` | no | Family the new GCP image joins (default `IMAGE_PREFIX`). |
| `PAYLOAD_EXCLUDE` | no | Space-separated `tar --exclude` globs applied to the payload. |

All of the keys marked "yes" must be set for every backend, even one you are
not building on; the unused keys are just not read. The recipe must set the
`BASE_*` key of the backend you name, or the build stops with exit 2; so must
a recipe built on Azure set `AZURE_VM_SIZE`, which no other backend needs.
The `*_DISK_GB` overrides exist because one disk size cannot fit every
platform's base: Proxmox requires `DISK_GB` to equal its template
(`base-ws2022` is 40 GB) while GCP and Azure refuse a disk smaller than their
image. Proxmox ignores all three.
`SETUP` and `VERIFY` may use only letters, digits, space and `._+-/`, with no
`..` component, and the files must exist. `SETUP_PASSES`, `DISK_GB`,
`PVE_MEMORY_MB`, `PVE_CORES` and any `*_DISK_GB` that is set must be positive
integers.

The payload is uploaded to `/tmp/image-build` on Linux and `C:\image-build` on
Windows. Setup and verify run as root (Linux, through `sudo`) or in PowerShell
(Windows), with the payload directory as the working directory on Linux.

### Rules for setup scripts

- **Never reboot from a setup script. Exit 100 instead.** Exit 0 means done.
  Exit 100 means done, but the VM needs a reboot: the core reboots it
  (`sudo systemctl reboot` on Linux, `Restart-Computer -Force` on Windows,
  started in the background so the SSH call returns; on Azure the backend's
  `backend_reboot` runs `az vm restart` instead), then waits until SSH
  answers with a boot ID other than the one it recorded before the setup ran
  (Linux `/proc/sys/kernel/random/boot_id`; Windows the last boot time, UTC,
  to the second). Only then does the next pass or verify run, so nothing runs
  against a VM that is still on its way down. No new boot within
  `IMAGE_BUILD_SSH_WAIT_SECONDS` fails the build. Any other exit code fails
  the build.
- **A dropped session must have been a reboot.** If the setup's SSH session
  ends with exit 255, the core waits the same way and goes on only if the boot
  ID changed; otherwise the build fails ("SSH dropped during setup but the VM
  did not reboot").
- **The VM must come back at the same address.** The core does not re-resolve
  it after a reboot.
- A dry run prints the boot-ID read and the reboot command an exit 100 would
  run, and runs neither.
- **Ubuntu clears `/tmp` at boot.** If `/tmp/image-build` has vanished, the
  core uploads the payload again before the next setup pass and before verify.
  Do not depend on state written under `/tmp` surviving a reboot.
- **The payload never ships.** Setup, verify (which holds solve steps) and any
  tarballs are deleted by the seal. The seal exits 1 if it finds them still
  present, and the build then saves nothing.

### SSH at first boot

One SSH answer does not mean SSH is up. On Proxmox clones of `base-ws2022`,
`sshd` (started by `SetupComplete.cmd`) answered the builder's first probe
and then failed the next connection, with exit 255 or a timeout during the
banner exchange, two or three times in each build, all within the first
seconds after boot; probes from 28 seconds after boot onwards all succeeded.
So every wait for SSH needs three answers in a row: after start, after a
setup pass that exits 0, and after a reboot (once the new boot ID shows up,
within the same `IMAGE_BUILD_SSH_WAIT_SECONDS`). The upload, which runs first,
retries only an exit 255, only for its own idempotent steps, and only until
`IMAGE_BUILD_SSH_WAIT_SECONDS` runs out or the build's expiry
(`BUILD_EXPIRES_UTC`, the start of that minute) arrives, whichever is first:
past the expiry `--reap` may destroy the build VM. No attempt starts after
that deadline, the sleep before a retry is cut short to end at it, and each
attempt's SSH `ConnectTimeout` (10 s) is cut to the time left. The retries are
logged (`payload upload: ... lost its SSH connection (exit 255); retrying in
10s`). An exit 255 during setup still means "the VM rebooted, or the build
fails"; verify and seal fail on any non-zero exit. Linux builds take the same
path: it costs them two extra probes (20 seconds) per wait and nothing else.

## Host SSH: `KEEP_SSH`

| Image is | Set | Effect |
|---|---|---|
| Shipped to students | `KEEP_SSH=no` (default) | Host SSH ships off. |
| Used as the BASE of a later build (for example a Docker-host image built upon) | `KEEP_SSH=yes` | Host SSH stays on, so the next build can log in. |

On Linux, `seal_linux.sh <yes|no>` always removes the payload, empties
`/home/builder/.ssh/authorized_keys`, runs `passwd -l builder`, runs
`cloud-init clean --logs` when cloud-init is present, empties
`/etc/machine-id`, deletes the host keys, and empties shell history. Locking
`builder`'s password does not stop the next build on a `KEEP_SSH=yes` base:
with `UsePAM yes` (Ubuntu's default) sshd still accepts a public key for a
password-locked account.

With `keep_ssh=no` it also turns host SSH off. Since Ubuntu 22.10 `ssh.socket`
owns port 22 and starts `ssh.service` on demand, so the seal disables and
masks both units (a unit that does not exist, such as `ssh.socket` on an older
base, is skipped; any other `systemctl` failure fails the seal) and writes
`/etc/ssh/sshd_not_to_be_run`, which both units also honour. RHEL's `sshd`
unit is not handled.

Whenever it deletes the host keys it installs a systemd drop-in
(`ssh.service.d/10-regen-hostkeys.conf`) that resets `ExecStartPre=` and runs
`ssh-keygen -A` and then `sshd -t`. The reset matters: the unit's own
`ExecStartPre=/usr/sbin/sshd -t` would otherwise run first and fail with no
host keys. A clone therefore gets new host keys on a base without cloud-init.

On Windows, `seal_windows.ps1` disables the `sshd` service unless `-KeepSsh`
is passed (which the core does for `KEEP_SSH=yes`).

## Windows recipes

- Windows images differ in size per platform, so set the disk per backend:
  `DISK_GB=40` for Proxmox (`base-ws2022`), `GCP_DISK_GB=50`
  (`windows-cloud/windows-2022`), `AZURE_DISK_GB=127` (the Windows Server
  marketplace images); on AWS the root disk must be at least 30
  (`AWS_DISK_GB` if `DISK_GB` is smaller).
- `seal_windows.ps1` deletes `C:\image-build` and
  `C:\ProgramData\ssh\administrators_authorized_keys`, then sets the local
  `Administrator` password to a random 32-character value generated in the
  guest. The value is held in a `SecureString` and is never printed or stored.
- It also deletes the cached DHCP lease values under
  `HKLM\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces\*`
  (`DhcpIPAddress`, `Lease*`, `T1`, `T2` and the rest). On Proxmox, whose
  save shuts the VM down without another boot, the image's clones then start
  with `DHCPDISCOVER` and each takes its own address. Without this, a clone
  (new MAC, same adapter) asks to renew the build VM's address; pve's dnsmasq
  is not authoritative and ignores that, Windows keeps the unexpired lease,
  and every clone on one VLAN shares one address. The AWS save reboots the
  instance after the seal, which caches a lease again, so AWS images carry
  one; EC2's DHCP gives each instance its own address regardless. The
  address in use is untouched, so the seal's SSH session survives
  (`ipconfig /release` would cut it). The seal exits 1 if a lease is still
  cached.
- **Sysprep is not run.** Whether and how to run it is to be decided per cloud
  during the first Windows build. On Azure the image is therefore published
  as a *specialized* gallery image (see "Azure").
- The seal does not touch the firewall. The `tcp/22` allow rule that the build
  created stays enabled, even when `sshd` is disabled.
- A Windows command given to the core's remote runner may not contain a double
  quote character.

## The backend contract

Each `backends/<name>.sh` is sourced into the core. It sets
`BACKEND_CONFIG_PREFIX` (for example `AWS_`), the prefix of every config key
it reads (only those come back from `config.env`), and must define these
functions. The full text is in the header of `build_image.sh`.

| Function | Job |
|---|---|
| `backend_check_config` | Exit non-zero, naming the missing key, if the config lacks what the backend needs. Runs in a subshell. |
| `backend_resolve_base <pattern>` | Print exactly one base ID, from the recipe's `BASE_<BACKEND>`. |
| `backend_start <run_id> <base_id> <pubkey_file>` | Start the build VM with the key. Set `BUILD_VM_ID` as soon as anything exists, then `BUILD_SSH_HOST`, `BUILD_SSH_USER`, `BUILD_SSH_JUMP`. |
| `backend_save <image_name>` | Save the image and set `IMAGE_ID`. Clear `BUILD_VM_ID` if the build VM itself became the image. |
| `backend_destroy` | Idempotent. Destroys only this run's build VM: `BUILD_VM_ID`, or (AWS) what carries this run's tag if the create call failed before the ID was recorded. |
| `backend_leftovers` | Print one ID per line: tagged, expired build VMs only. Reads through `query`. |
| `backend_destroy_id <id>` | Re-check the tag through `query`, then destroy through `mutate`. |
| `backend_templates_yaml <image_id>` | Print the `templates.yaml` lines. |

Two optional parts, used by Azure only so far:

| Optional | Job |
|---|---|
| `BACKEND_RECIPE_KEYS` | Space-separated recipe keys (each one of the core's optional keys) this backend requires. A build whose recipe lacks one stops with exit 2. Azure sets `AZURE_VM_SIZE`. |
| `backend_reboot` | Reboot the build VM when a setup pass exits 100, instead of the core's in-guest reboot command. The core then waits for a new boot ID over SSH as always. |

Every function runs with `errexit` in force. A step that may fail must be
guarded inside the backend. Backends reach a platform only through two
helpers: `mutate` for anything that creates, changes or deletes, and `query`
for reads. That is what makes `--dry-run` safe.

## Tags and labels

Every build resource carries two tags (labels on GCP):

| Key | Value |
|---|---|
| `image-build` | The run ID, `ib-<yyyymmddhhmm>-<16 hex>` (64 random bits; builds started before 2026-11-14 used 4 hex, which the Proxmox, AWS and GCP reaps still accept) |
| `image-build-expires` | UTC `yyyymmddhhmm`, `IMAGE_BUILD_TTL_MINUTES` after the build started (default 4 hours) |

On Proxmox both go in the VM description, as two space-separated words. On
Azure they go on the build's own resource group (and on the VM in it). What
the saved image carries depends on the backend:

| Backend | The saved image carries |
|---|---|
| Proxmox | The description is replaced by `image=<image-name>` and nothing else: no build tags, no recipe or commit tag. The replica copy has the same description. |
| AWS | The AMI and its snapshots carry `image-build-recipe` and `image-build-commit` tags. |
| GCP | The image carries `image-build-recipe` and `image-build-commit` labels, and a description naming the recipe, commit and run ID. |
| Azure | The gallery image version carries `image-build-recipe` and `image-build-commit` tags. |

The image has no expiry tag, because the product does not expire.

**Build resources never carry `lab-playground-site`.** The multi-backend
portal's AWS and GCP backends (branch `feature/multi-backend-vms`, PR #80, not
yet merged; see "Safety model and ownership" in its `docs/CLOUD_BACKENDS.md`)
tag the instances and other taggable resources they create with
`lab-playground-site=<LAB_SITE_ID>`, and filter inventory, reuse and deletes
by it. Its GCP networks carry no labels; they are identified by their
`lp-<site-id>-...` names instead. A build resource never carries the tag, so
the portal never lists or deletes a build instance. The builder, for its part,
selects only `image-build`-tagged build VMs when it cleans up or reaps; it
never deletes a saved image. On Proxmox the existing sweeper
(`app/orphan_reaper.py`) reaps untracked guests whose description has an
`Expires:` line past its grace period; a build description holds
`image-build=` and `image-build-expires=` words and no `Expires:` line, so the
sweeper keeps it.

### What a destroy or a reap will delete

A resource is deleted only after the backend re-reads its tag immediately
before the delete.

- **Cleanup of the current build** (the trap, and `backend_destroy`) requires
  the `image-build` value to equal this run's `RUN_ID`. Otherwise it refuses.
  On AWS, if `run-instances` failed or was interrupted before the instance ID
  was recorded, the cleanup lists the instances in `AWS_BUILD_REGION` tagged
  `image-build=<RUN_ID>` (pending, running, stopping or stopped) and
  terminates each through the same check, so another run's instance is never
  touched. On Azure the unit is the build's resource group, and the build
  owns it only if Azure answered its group PUT with `201 Created` (see
  "Azure"); the cleanup deletes it only while its name, `image-build`,
  `image-build-expires`, `image-build-owner` and `image-build-creating` all
  still match this run (the last two must both be the run's 128-bit nonce).
- **`--reap`** requires all of these:
  - the `image-build` value matches `ib-<12 digits>-<16 hex>` (or the older
    `<4 hex>`; Azure accepts 16 only) exactly;
  - the resource has one `image-build` tag (AWS tags and GCP labels hold one
    value per key; on Proxmox the description may hold only one
    `image-build=` word);
  - `image-build-expires` is 12 digits and earlier than now (UTC);
  - on GCP, the label also equals the instance's own name;
  - on Azure, the resource is a resource group whose name equals the tag,
    whose `image-build-owner` and `image-build-creating` are one and the same
    32-hex nonce (the creation token, below), and whose `image-build-expires`
    is a real UTC calendar minute (`000000000000`, `202613010000` and
    `202602300000` are refused, not read as long expired);
  - on Proxmox, also: the VM is on `PVE_NODE`, its VMID is in `8000`-`8999`,
    and it is not a template.

A resource that has the tag but fails these checks is never deleted. On AWS
and Azure, one whose `image-build-expires` is missing or malformed is reported
and left out of the list (on Azure, so is one without a valid nonce). One that
is listed but whose tag is not a run ID is refused at the delete, and
`--reap` reports it and exits 1. On Azure, a read counts as "the group does
not exist" only when `az` exits 3 with exactly `ResourceGroupNotFound`;
`SubscriptionNotFound`, an authorization or tenant error, an unknown code or
an empty successful answer fails the destroy instead.

**Deleting an Azure resource group deletes everything in it.** The builder
only ever deletes a group it tagged with its own run ID, whose name is that
run ID; never put anything else in an `ib-...` group.

## Backends

### Proxmox

The build full-clones the newest template matching `BASE_PROXMOX` on
`PVE_NODE` (by the date or timestamp in the last hyphen-separated field of its
name, VMID below 9000). It starts the clone, injects a per-run SSH key through
the guest agent, and reaches it over SSH through `root@PVE_HOST`. After seal,
it shuts the VM down, renames it to the image name, turns the build VM itself
into the template, and copies that to `PVE_REPLICA_NODE` with
`scripts/sync_template.sh`.

**Disk size.** The backend does not resize, so every Proxmox image has the
base template's disk size. `DISK_GB` must equal it: before cloning, the build
reads the base's `scsi0` size and stops (exit 1, nothing cloned) if it
differs from `DISK_GB`. Set `DISK_GB` to the base's size, or build a base of
the wanted size: `proxmox_base_ubuntu.sh --disk` sets it. The size may be
written in K, M, G or T (`20480M` is 20 GiB); it matches `DISK_GB` only when
it is exactly that many GiB, so `20.5G` never does.

**VMIDs.**

| Role | Node | VMID |
|---|---|---|
| Build VM and template | `PVE_NODE` | `8000`-`8499` (the lowest value whose replica ID is also free) |
| Replica | `PVE_REPLICA_NODE` | build VMID + 500, so `8500`-`8999` |

**Never create or clone a guest at VMID 9000 or above.** The portal's VMID
allocator takes the cluster's highest VMID as a permanent high-water mark
(`app/proxmox_service.py`): it allocates above the larger of the cluster's
current highest VMID and the highest value it has stored before. One guest at
9000+ therefore moves every later portal VMID above it, and once the portal has
allocated above it, deleting the guest does not undo that. The backend refuses any
VMID outside its block, and `sync_template.sh`'s optional fifth argument
(`target_vmid`) accepts only `8500`-`8999`.

`--reap proxmox` lists only non-template VMs on `PVE_NODE` in `8000`-`8999`
that meet the criteria in "What a destroy or a reap will delete". The
node-side destroy repeats the tag check before `qm stop` and again before
`qm destroy`, and refuses a template. It treats a VM as already gone only
when `qm config` says exactly that its config file does not exist,
`/etc/pve/nodes` is readable, and no node holds the VMID. Any other failure
to read the config (the cluster filesystem down, say) fails the destroy.

The clone command itself writes the build tags. While a clone is still
running the new guest may not carry them yet (Proxmox holds it locked, as
`lock: clone`); a destroy that finds no tag refuses, so an interrupted clone
can need a hand cleanup and is never destroyed blindly.

**A template left by a failed save must be removed by hand.** If the build VM
became a template but the conversion call reported failure, setting its
`image=<name>` description failed (it then still carries the build tags), or
the copy to the replica failed, the build is not logged. The error message
names the VMID: delete it with `qm destroy <vmid> --purge` on the node, or
finish the save with the commands the error prints.

**Base-template contract.** The template named by `BASE_PROXMOX` must have:

- a user `builder` with passwordless `sudo`;
- OpenSSH server running;
- `qemu-guest-agent` running (the build waits up to 5 minutes for it);
- DHCP on its network interface;
- **no SSH key baked in.** The build writes a per-run key into
  `/home/builder/.ssh/authorized_keys` through the guest agent, and the seal
  empties that file.

For a Windows base, the user is `Administrator` and the guest agent must run.
OpenSSH Server must be installed, and the `sshd` service must be running and
set to start automatically by the time SSH answers. The backend only injects
the key, through the guest agent, into
`C:\ProgramData\ssh\administrators_authorized_keys`; it never installs or
starts `sshd`. `proxmox_base_windows.sh` builds such a base (below).

#### Building the Linux base: `proxmox_base_ubuntu.sh`

`scripts/image_build/proxmox_base_ubuntu.sh` builds a Linux base that meets
this contract, `base-ubuntu-2404-<yyyymmdd>`, from the official Ubuntu 24.04
cloud image. It runs on the build host, reads the same `config.env`
(`IMAGE_BUILD_CONFIG` overrides the path), and reaches `root@PVE_HOST` over
SSH as the builder does. It uses five keys: `PVE_HOST`, `PVE_NODE`,
`PVE_STORAGE` (the template's disk), `PVE_BUILD_BRIDGE` and `PVE_BUILD_VLAN`.

```
scripts/image_build/proxmox_base_ubuntu.sh [--image PATH] [--snippets STORE]   # dry run
scripts/image_build/proxmox_base_ubuntu.sh --apply [--image PATH] [--snippets STORE]
```

**It is a dry run by default.** Without `--apply` it runs only the read-only
checks and prints every command that would change the cluster, the user-data
and the in-guest finalize script. Options: `--image` (the cloud image on
`PVE_NODE`, default
`/var/lib/vz/template/iso/noble-server-cloudimg-amd64.img`), `--snippets` (a
storage with the `snippets` content type, default `local`), `--vmid`,
`--date` (default today, UTC) and `--disk` (default `20G`; the backend does
not resize, so this is every image's disk size).

**VMID.** The lowest `V` in `8000`-`8499` with both `V` and `V+500` free
anywhere in the cluster, so the replica can sit at `V+500`. `--vmid` is
accepted only in `8000`-`8499` and only when both IDs are free. The script
refuses a VMID in use, a name already taken (one base per day), and a
`PVE_NODE` with less than 16 GiB of available memory. It never touches an
existing guest.

**Build network.** The VM's `net0` is on `PVE_BUILD_BRIDGE` tagged
`PVE_BUILD_VLAN`, the builder's provisioning network (VLAN 20 in the
reference deployment, never a student lab VLAN). It needs DHCP and internet
access there: the first boot upgrades the image and installs packages.

**What it does.** It writes a first-boot user-data snippet (no key, no
password) to the snippets storage, creates the VM, imports the cloud image as
its disk and boots it with a cloud-init drive pointing at the snippet.
cloud-init creates `builder` and installs `qemu-guest-agent` and
`openssh-server`. The rest runs as root through the guest agent: a netplan
file that gives DHCP to any `en*`/`eth*` interface, matched by name rather
than MAC (a clone has a new MAC), with cloud-init's own network config
disabled; no SSH host keys (a drop-in runs `ssh-keygen -A` when `ssh.service`
starts); every `authorized_keys` file deleted and every password locked;
`cloud-init clean --logs` and an empty `/etc/machine-id`. It then shuts the
VM down, **removes the cloud-init drive** (`ide2`, `cicustom`, `ipconfig0`;
a clone that kept it would replay its config), turns the VM into a template
and **deletes the snippet**. The template's description records the image
file name and its SHA-256.

**Failure.** `qm create` writes a construction marker into the VM's
description (`proxmox-base-build=<time>-<random>`, unique to the run), and
the marker stays there until the VM is a template. If the run fails, or is
interrupted with `INT`, `TERM` or `HUP`, after it began to create the VM, it
asks the node for that VMID and destroys the VM only when the node shows it
is not a template, carries the base's name and carries this run's marker,
checked again between `qm stop` and `qm destroy`. Any other guest at that
VMID is reported and left alone; the error names the VMID and the marker, so
a VM the run could not destroy (a lost connection, say) can be identified
and removed by hand. The snippet is deleted on every exit; if the node cannot
confirm it is gone, the run exits non-zero with an error naming its path.

**Rebuilding for a new point release.** Ubuntu refreshes the 24.04 cloud
image for each point release and between them for security updates. On
`PVE_NODE`, replace the image at the `--image` path with the current one and
check it against Ubuntu's published `SHA256SUMS` (the `noble/current/`
directory keeps the file name the script expects):

```
cd /var/lib/vz/template/iso
wget -O noble-server-cloudimg-amd64.img \
  https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img
wget -O SHA256SUMS https://cloud-images.ubuntu.com/noble/current/SHA256SUMS
grep ' \*noble-server-cloudimg-amd64.img$' SHA256SUMS | sha256sum -c -
```

Then run the script, dry run first, then with `--apply`. The new base gets
today's date in its name and the next free VMID pair. Prove it before any
build uses it: full-clone it to a test VMID in the block, boot it, check the
contract (guest agent reports an address, `builder` has passwordless `sudo`,
`sshd` runs, no `authorized_keys`, a new machine ID), and destroy the clone.
Copy it to the replica node with
`scripts/sync_template.sh <PVE_HOST> <vmid> <PVE_REPLICA_HOST> <PVE_REPLICA_STORAGE> <vmid+500>`.
Builds whose `BASE_PROXMOX` glob matches the new name use it from then on,
since the newest match wins. Delete the old base (both copies) by hand once
nothing needs it, as in "Deleting an old image".

#### Building the Windows base: `proxmox_base_windows.sh`

`scripts/image_build/proxmox_base_windows.sh` builds `base-ws2022-<yyyymmdd>`:
Windows Server 2022 Standard Evaluation (Desktop Experience), installed
unattended from the evaluation ISO onto a 40 G virtio-scsi `scsi0` (so
Windows recipes set `DISK_GB=40`). It runs where `proxmox_base_ubuntu.sh`
runs, reads the same five `PVE_` keys, and is a dry run unless given
`--apply`. The guest-side files are in `scripts/image_build/windows_base/`.

```
scripts/image_build/proxmox_base_windows.sh [--iso VOLID] [--virtio-iso VOLID]   # dry run
scripts/image_build/proxmox_base_windows.sh --apply [--vmid N] [--skip-updates]
```

Options: `--iso` (default `local:iso/Windows_Server_2022_EVAL.iso`),
`--virtio-iso` (default `local:iso/virtio-win-0.1.302.iso`; pass both when
the ISOs sit on another storage), `--iso-store` (a storage with the `iso`
content type for the run's own answer-file ISO, default `local`), `--vmid`,
`--date`, `--disk` (default `40G`) and `--skip-updates`. VMID choice, the
16 GiB memory floor, the construction marker and the cleanup on failure
work as for the Linux base.

**What it does.** It writes a per-run ISO holding `autounattend.xml` (with a
random install password) and the guest scripts, creates the VM (SeaBIOS,
`virtio-scsi-single`, the RTC in UTC because Windows runs in UTC, the
installation, virtio-win and answer ISOs as CD drives) and boots it. Setup
loads the virtio-scsi and NetKVM drivers from the virtio-win ISO, installs,
and logs on once automatically; that logon installs the virtio drivers and
the QEMU guest agent. Everything else runs as SYSTEM through the agent, each
long step as a scheduled task (`windows_base/step.ps1`) so the agent's
command timeout does not apply:

1. `prepare.ps1`: installs OpenSSH Server (`Add-WindowsCapability`, which
   needs internet), turns password login off, leaves the default shell as
   `cmd.exe` (the core's Windows commands expect it), opens `tcp/22` on every
   firewall profile, and proves that `sshd` writes new host keys when they are
   missing.
2. `windows_update.ps1`, in rounds until no update is left, rebooting when
   one asks (`--skip-updates` skips this; the description then says so).
   After every reboot the script waits for the guest to settle: Windows
   servicing restarts it once more on its own about 40 s after a
   cumulative-update reboot, and a step started before that would be killed.
   A step that a reboot kills anyway is detected (the boot identity changed)
   and run again.
3. `cleanup.ps1`: DISM component cleanup with `/ResetBase`, the update cache
   emptied, a retrim attempted (Windows answers "Not Supported" on this
   disk; the step goes on). Then reboots until none is pending.
4. `finalize.ps1`: no reboot pending and a licence rearm left (sysprep needs
   both); every app installed for a user but not provisioned removed (the
   cumulative update installs Microsoft Edge that way, and sysprep then fails
   with `0x80073CF2`); `sshd` stopped and **disabled**, host keys and
   authorized keys deleted; `SetupComplete.cmd` and `randomize-admin.ps1`
   installed; the sysprep answer file written, with **no password in it**;
   setup's copies of the install answer file deleted; automatic logon off;
   event logs cleared.
5. `ipconfig /release`, then `sysprep /generalize /oobe /shutdown /mode:vm`.
   Without the release every clone boots with the template's cached DHCP
   lease: pve's dnsmasq is not authoritative, so it ignores a request for an
   address leased to another MAC, and Windows keeps an unexpired lease when
   the gateway answers, so all clones on one VLAN shared one address. A
   sysprep that ends without shutting the VM down fails the run with
   `setuperr.log`. The script then removes the CD drives, sets 2 cores and
   4096 MB (the Windows recipe's values), makes it a template and deletes the
   answer-file ISO.

**Why `sshd` is disabled in the template.** A clone's first boot runs
Windows setup's specialize pass, reboots once, then runs OOBE unattended.
If `sshd` answered before that reboot, the core could start the setup script
and have it cut by setup's own reboot, and an exit 255 followed by a new boot
ID reads as a setup that rebooted on purpose. `SetupComplete.cmd` runs once,
as SYSTEM, after that reboot and after OOBE: it enables and starts `sshd`
(which writes the clone's own host keys), deletes the sysprep answer file,
`randomize-admin.ps1` and itself. So the first SSH answer comes from a clone
that has finished setup.

**Administrator password.** No answer file holds one. In the specialize
pass the answer file runs `C:\Windows\Setup\Scripts\randomize-admin.ps1` as
SYSTEM, which gives that clone's Administrator a 32-character password from
the cryptographic RNG, held in a `SecureString` and never output. Every
clone therefore has its own unrecorded password; the builder logs in with a
key, and the seal sets yet another.

**Licence.** Evaluation edition. The base was activated (180 days) before
sysprep, which resets that: a fresh clone starts in a 10-day initial grace
period (`slmgr /dlv`), and `slmgr /ato` on a clone returned `0x80041008` on
2026-11-14, so online activation of a clone is not proven. Built images are
not sysprepped and keep their build VM's licence state; check `slmgr /dlv`
on a built image before relying on it for a term.

Prove it as for the Linux base: full-clone it to a test VMID, boot it on
the build VLAN, inject a key as the backend does, log in as `Administrator`
over SSH through `root@PVE_HOST`, read `LastBootUpTime`, destroy the clone;
then copy it to the replica node with `sync_template.sh`.

### AWS

The build runs one instance from the newest available AMI matching `BASE_AWS`,
in `AWS_BUILD_SUBNET_ID`, with a public IPv4 address, IMDSv2 required, and a
`DISK_GB` (or `AWS_DISK_GB`) gp3 root volume that is deleted on termination. The key goes in as
user data: on Linux, cloud-init creates `builder` with passwordless sudo and the
key; on Windows, EC2Launch runs a script that installs OpenSSH Server, writes
`administrators_authorized_keys`, starts `sshd` and opens `tcp/22`. The image
is made with `create-image`, which reboots the instance, and is waited on
until it is `available`.

The instance and its root volume carry the two build tags. The AMI and its
snapshots carry `image-build-recipe` and `image-build-commit`.
`--reap aws` scans `AWS_BUILD_REGION` only. `templates.yaml` gets
`aws: {image_id, instance_type, disk_gb}`.

### GCP

The build runs one instance, named after the run ID, from the newest
non-deprecated image in the `BASE_GCP` family. It has an external IP, no
service account and no scopes. On Linux the key goes in as `ssh-keys` metadata
for `builder`, with OS Login and project-wide keys turned off for the instance.
On Windows a startup script installs OpenSSH Server, writes the key, enables
the built-in `Administrator` with a random password generated in the guest,
starts `sshd` and opens `tcp/22`. The instance is stopped, then the image is
made from its boot disk and joins the `GCP_IMAGE_FAMILY` family. The build
labels are on the instance; the image carries `image-build-recipe` and
`image-build-commit` labels.

**`--reap gcp` scans all zones of the project**, not only `GCP_BUILD_ZONE`. See
"What a destroy or a reap will delete" for the criteria. IDs are printed as
`<zone>/<instance>`.
`templates.yaml` gets `gcp: {image, machine_type, disk_gb}`. The boot disk is
`GCP_DISK_GB` when set, else `DISK_GB`.

### Azure

**Tested against a fake `az`, and run live once** (2026-11-15, `eastus`,
`az` 2.92.0): Linux builds, the reboot protocol, a failed verify, a killed
build plus `--reap`, and a build from a specialized gallery base all passed
with the fixes that run found (below). No Windows image has been produced on
Azure yet; see "Open questions" below before relying on one.

Each build gets its own resource group, named after the run ID
(`ib-<yyyymmddhhmm>-<16 hex>`), in `AZ_BUILD_LOCATION` and tagged
`image-build`, `image-build-expires`, `image-build-owner` and
`image-build-creating`; the last two hold the same 128-bit nonce made for
the run.

**There is no create-only primitive for a resource group.** `az group
create` is a create-or-update, and Azure ignores `If-None-Match: *` (and
`If-Match`) on a resource-group PUT: a PUT on an existing group answers
`200` and replaces all its tags (seen live; a resource group has no etag).
So the backend:

1. Reads the group first, fail-closed: only `az` exit 3 with exactly
   `ResourceGroupNotFound` lets the build go on. A group that exists is
   refused and left unchanged; any other error stops the build.
2. Sends `az rest --method put` on the group's URI with `--debug` (and still
   `If-None-Match: *`, in case Azure ever honours it), reads the HTTP status
   from the debug log (kept in the run's temp directory and deleted at once:
   it holds a prefix of the access token), and owns the group only on
   `201 Created`. The status counts only if it is the one `Response status:`
   line that follows a `Request URL:` line naming exactly this group's URI
   with method `PUT`; a status from any other request (a token fetch, say)
   is ignored, and none or more than one such line reads as unknown.

**The residual race** is a group created under the same name between the
read and the PUT, a second or two. The name carries 64 random bits and is
not published before the PUT, so that takes a 2⁻⁶⁴ coincidence, or someone
who can read the build host and already holds `resourceGroups/write` in the
subscription (and could retag anything anyway). If it happens, that group's
tags are replaced by the creation token, the PUT answers `200`, and the
build stops: the group is never owned, deleted or reaped; its other tags
are lost, its resources untouched.

**The PUT is non-reapable by construction.** Its only tag is the creation
token `image-build-creating=<nonce>`: no `image-build`, no expiry, no owner
tag, so nothing it leaves behind can ever be listed or deleted by `--reap`,
whatever Azure or `az` did. No outcome needs a second, compensating write to
be safe.

**The creation token proves continuity.** A `201` proves the group was
created, not that it is still the same group a moment later: Azure group IDs
are names, so a group deleted and recreated under the same name before the
tag Merge would receive the Merge, owner nonce included. Only this run's PUT
ever writes the token, and the Merge leaves it in place, so ownership, the
cleanup and the reap all require `image-build-creating == image-build-owner
== nonce`. A replacement that received only the Merge lacks the token and is
never owned, deleted or reaped.

| PUT answer | Result |
|---|---|
| `201` | Created by this run. Only now does the build merge on `image-build`, `image-build-expires` and `image-build-owner` (`az tag update --operation Merge`), read all four tags back, and, if they are exactly this run's with the creation token intact, own the group (deleted at the end). If the tag write fails or the read-back does not show this run's four tags, **nothing is deleted**: a parsed `201` is history (the group could have been replaced since, or the status misread), so the build stops and names the group with the `az group delete` command for a hand cleanup. An empty resource group costs nothing; a group without both nonces is invisible to `--reap`. |
| `200` | The race above: an existing group's tags were replaced with `image-build-creating`. Nothing owned, written again or deleted; the build stops and names the group; check it by hand. |
| `412` | Not seen live (Azure ignores the header); kept as a defence. The group exists; nothing was changed; the build stops. |
| Another error, or no readable status | Nothing owned, written again or deleted; the build stops and names the group. A group Azure created while `az` reported an error is empty and carries only `image-build-creating`; delete it by hand. |

Every tag read uses a list-of-lists query (`--query '[[a, b]]'`): with
`-o tsv`, `az` prints a flat list `[a, b]` one value per line, and a list of
lists as one tab-separated row. A missing tag prints `None`, which the
backend reads as empty.

The VM (same name) is created in it from `BASE_AZURE`, size `AZURE_VM_SIZE`,
OS disk `AZURE_DISK_GB` or `DISK_GB`, security type `Standard`, with its NIC
in `AZ_BUILD_SUBNET_ID`, **no NIC-level NSG** (`--nsg ""`; `az vm create`
would otherwise open `tcp/22` to the internet) and a Standard static public
IP in the same group. Every `az` call names `--subscription`, except the
group PUT, whose URI names it. A static public IP is kept across
`az vm restart`.

- **Linux:** Azure creates the admin user `builder` with the run's public key
  (`--ssh-key-values`, a file) and passwordless sudo.
- **Windows:** Azure injects no SSH key into Windows. The admin user `builder`
  gets a random password generated on the build host, written to the run's
  temp directory (mode 600) and passed as `--admin-password @<file>`, so it
  is never on a command line, printed, logged or kept (the temp directory is
  deleted on exit). Azure gives that name to the built-in Administrator
  (RID 500): there is no separate `builder` account, and no account called
  `Administrator`. Then `az vm run-command invoke --command-id
  RunPowerShellScript` runs the GCP backend's OpenSSH script: it installs
  OpenSSH Server, writes the key to `administrators_authorized_keys`, finds
  the RID-500 account by its SID, renames it back to `Administrator` (the
  seal and the payloads use that name) and gives it a fresh random password
  generated in the guest and never output, starts `sshd` and opens
  `tcp/22`. The build logs in as `Administrator`. Azure Windows images
  therefore contain no `builder` account.
- **run-command reports success even when the script failed**, so each
  script prints `image-build-ssh-ready` as its last step and the build fails
  without it.
- **A specialized gallery base** (an image this backend built, with
  `KEEP_SSH=yes`) is created with `--specialized`. `az` 2.92 still demands
  a credential then (an SSH key, or a password on Windows) and leaves it out
  of the VM, so the build passes the run's key or generated password anyway,
  and run-command writes the key: `RunShellScript` into `builder`'s
  `authorized_keys` on Linux, the same OpenSSH script on Windows. Such a
  base must have been built with `KEEP_SSH=yes`, or `sshd` is off.

A setup that exits 100 is rebooted with `az vm restart`. **Save:** the
backend refuses an existing version of the same name (it never overwrites),
checks the image definition `IMAGE_PREFIX` in the gallery (it must be
`Specialized`, of the recipe's OS type and the base's Hyper-V generation) or
creates it (`--publisher image-build --offer <IMAGE_PREFIX> --sku
<IMAGE_PREFIX> --features SecurityType=Standard`: without it `az` 2.92
marks a V2 definition `TrustedLaunchSupported`), **deallocates the VM**, then publishes a **specialized**
image version from it (`az sig image-version create --virtual-machine`): no
generalize, no sysprep, as on AWS and Proxmox. The version is
`<yyyy>.<mmdd>.<hhmm>` of the build timestamp as integers (a build at
`202611140905` is `2026.1114.905`), because a gallery version must be three
integers; two builds of one prefix in the same minute collide, and the
second is refused. It carries `image-build-recipe` and `image-build-commit`
tags. `IMAGE_ID` is the version's resource ID. A version whose create failed
or did not reach `Succeeded` is left in place and not logged, and the error
prints the `az sig image-version delete` command for it. The two existence
reads before the save fail closed: a version or definition counts as absent
only when `az` exits 3 with exactly `ResourceNotFound` (the only code `az`
2.92 returns for a missing gallery, definition or version); any other error,
and a successful answer that is empty or malformed (a version ID that is
not this version's, a definition without a valid OS type, state and
generation), stops the build before the VM is deallocated. Publishing a
version takes about 8 to 10 minutes of a 12-minute Linux build.

**What a sealed Azure image keeps.**

- **The Azure VM agent** (`walinuxagent` on Linux, the guest agent on
  Windows) stays installed and running. Anyone with
  `Microsoft.Compute/virtualMachines/runCommand/action` on a VM made from
  the image runs code in it as root or SYSTEM. This is the same class as
  the SSM agent on AWS and the guest agent on GCP: decide whether the
  portal's Azure identity ever holds that right on lab VMs.
- **The build VM's hostname.** A specialized image is never re-provisioned,
  so every clone keeps it (`ib-<yyyymmddhhmm>-<16 hex>` on Linux, the
  15-character `ib-<ddhhmm>-<4 hex>` computer name on Windows).

`--reap azure` lists the subscription's resource groups that carry an
`image-build` tag. IDs are group names. `templates.yaml` gets
`azure: {image, vm_size, disk_gb}`.

**One-time setup** (by an operator, once per subscription):

1. A resource group for the gallery, and an Azure Compute Gallery in it, both
   in `AZ_BUILD_LOCATION`: `az group create`, then `az sig create
   --resource-group <AZ_GALLERY_RG> --gallery-name <AZ_GALLERY>`.
2. A build VNet and subnet in `AZ_BUILD_LOCATION`, separate from any lab
   network, with an NSG on the subnet whose only inbound allow beyond the
   defaults is `tcp/22` from the build host's public address. The subnet
   needs outbound internet (setup installs packages).
3. The build identity's RBAC: **Contributor on `AZ_GALLERY_RG`** (definitions
   and versions); on the subscription, the right to create resource groups
   and to manage what is in them (Contributor on the subscription is the
   simple form; a custom role with `Microsoft.Resources/subscriptions/
   resourceGroups/write|read|delete` plus compute and network write on
   `ib-*` groups is tighter, and an Azure Policy can require the
   `image-build` tag on every group that role creates); and **`Microsoft.
   Network/virtualNetworks/subnets/join/action`** on the build subnet
   (Network Contributor on the subnet, or a custom role with that action).
4. `az login` (or a service principal) on the build host, and the
   subscription ID in `config.env`.

**What the live run proved** (2026-11-15): `latest` resolves and the base's
Hyper-V generation reads correctly; `az` accepts `--security-type Standard`,
`--nsg ""` and `@file` values; the Linux `builder` has passwordless sudo;
the subnet NSG alone admits SSH to the Standard public IP; run-command
installs OpenSSH and its output carries the marker in `value[].message`; a
deallocated VM publishes as a specialized version through
`--virtual-machine`, and a VM from it boots with SSH and the same
`builder`; `az vm restart` gives a new boot ID; a missing group answers exit
3 `ResourceGroupNotFound`, a missing gallery item exit 3 `ResourceNotFound`,
an unknown subscription or an authorization failure exit 1 (fail closed);
`az rest --debug` logs `Request URL:`, `Request method: 'PUT'` and
`Response status:` as the backend parses them; `az tag update --operation
Merge` works; `az group list --tag image-build` filters on the key alone.

**Open questions:**

- a Windows image built and booted end to end on Azure (the live Windows
  builds reached verify; none was kept), including what a clone's
  Administrator password and hostname are;
- the fixes found live, as this branch carries them, re-checked live;
- marketplace images that need plan terms accepted: not handled;
- Trusted Launch: VMs are created `Standard` and definitions with
  `SecurityType=Standard`; a subscription that forbids Standard VMs would
  need both changed.

## Dry run and reaping

**Always run `--dry-run` first.** It prints every command that would reach a VM
or a cloud as a `DRY-RUN:` line on stderr and runs none of them. Reads print
their command and return a placeholder, so IDs in a dry run are placeholders.
It still validates the recipe, the config and the repo state, and names the
image.

```
scripts/image_build/build_image.sh --dry-run path/to/recipe.env aws
scripts/image_build/build_image.sh path/to/recipe.env aws
```

**`--reap <backend>`** removes build VMs that a killed build left behind
(`kill -9`, a lost laptop, a power cut). It lists the backend's leftovers:
expired build VMs that meet the criteria in "What a destroy or a reap will
delete". For each one the backend re-checks the tag, then
destroys it. It prints `destroyed <id>` for each, and exits 1 if any could not
be destroyed. With `--dry-run`, `--reap` still runs the read-only queries for
real, because the plan needs the real list, and prints `would destroy <id>` and
the destroy commands instead of running them. Run `--reap <backend>
--dry-run` first, as for a build. `--reap` takes no recipe and no
`--allow-dirty`.

A VM stays safe from `--reap` until its expiry (default 4 hours after the
build started), so do not reap a build you are still running.

## Deleting an old image

The builder never deletes images. Keep one previous image per machine (one
recipe on one backend), and delete the older ones by hand once
`templates.yaml` no longer points at them. Check the ID before you run
anything; deletion is not reversible.

**Proxmox.** A build makes two templates. Destroy both:

```
ssh root@<PVE_HOST> qm destroy <vmid> --purge
ssh root@<PVE_REPLICA_HOST> qm destroy <vmid+500> --purge
```

The image ID the build printed is `<node>:<vmid>,<replica node>:<vmid+500>`.

**AWS.** List the snapshots first, then deregister the AMI and delete each
snapshot:

```
aws ec2 describe-images --region <region> --image-ids <ami-id> \
  --query 'Images[0].BlockDeviceMappings[].Ebs.SnapshotId' --output text
aws ec2 deregister-image --region <region> --image-id <ami-id>
aws ec2 delete-snapshot --region <region> --snapshot-id <snap-id>
```

**GCP.**

```
gcloud compute images delete <image-name> --project <project>
```

**Azure.** The image ID the build printed is the version's resource ID:

```
az sig image-version delete --ids <image-version-id>
```

The image definition stays; delete it with `az sig image-definition delete`
once it holds no version.
