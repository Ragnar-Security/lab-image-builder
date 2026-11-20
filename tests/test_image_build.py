"""The image builder uploads a payload (setup, verify, tarballs) into a build VM
and seals it before the image is saved. A sealed image must carry none of the
payload, no usable build key, and no identity shared with the build VM."""

import os
import re
import shlex
import signal
import subprocess
import tarfile
import time
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
IMAGE_BUILD = ROOT / "scripts" / "image_build"
SEAL_LINUX = IMAGE_BUILD / "seal_linux.sh"

LOG_CALL = 'printf "%s %s\\n" "$(basename "$0")" "$*" >> "$CALL_LOG"\n'


# systemctl <verb> <unit>: logged. A unit in FAKE_SYSTEMCTL_MISSING does not
# exist (disable fails as systemd 255 does); one in FAKE_SYSTEMCTL_FAIL fails
# for another reason.
FAKE_SYSTEMCTL = LOG_CALL + (
    'case " ${FAKE_SYSTEMCTL_MISSING:-} " in *" $2 "*)\n'
    '  [ "$1" = disable ] && { echo "Failed to disable unit: Unit file $2 does not exist." >&2; exit 1; } ;;\n'
    'esac\n'
    'case " ${FAKE_SYSTEMCTL_FAIL:-} " in *" $2 "*)\n'
    '  echo "Failed to $1 unit: Access denied" >&2; exit 1 ;;\n'
    'esac\n'
)


def _tool(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _seal_env(tmp_path: Path) -> dict:
    """A temp root laid out like a freshly built VM, plus logging fakes for the
    commands that act on the live system."""
    root = tmp_path / "root"
    for rel in ("tmp/image-build/sub", "home/builder/.ssh", "home/other", "root", "etc/ssh"):
        (root / rel).mkdir(parents=True)
    (root / "tmp/image-build/images.tar").write_text("tarball")
    (root / "tmp/image-build/sub/verify.sh").write_text("solve steps")
    (root / "home/builder/.ssh/authorized_keys").write_text("ssh-ed25519 AAAA build-key\n")
    (root / "etc/machine-id").write_text("0123456789abcdef0123456789abcdef\n")
    (root / "etc/ssh/ssh_host_ed25519_key").write_text("private")
    (root / "etc/ssh/ssh_host_ed25519_key.pub").write_text("public")
    (root / "etc/ssh/sshd_config").write_text("keep me")
    (root / "root/.bash_history").write_text("secret command\n")
    (root / "home/other/.bash_history").write_text("another secret\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("passwd", "cloud-init"):
        _tool(bin_dir, name, LOG_CALL)
    _tool(bin_dir, "systemctl", FAKE_SYSTEMCTL)
    env = dict(os.environ)
    env.update(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        CALL_LOG=str(tmp_path / "calls.log"),
        SEAL_ROOT=str(root),
    )
    return env


def _seal(tmp_path: Path, env: dict, keep_ssh: str):
    return subprocess.run(
        ["bash", str(SEAL_LINUX), keep_ssh] if keep_ssh is not None else ["bash", str(SEAL_LINUX)],
        env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
    )


def _calls(tmp_path: Path) -> str:
    log = tmp_path / "calls.log"
    return log.read_text() if log.exists() else ""


# --- seal_linux.sh ----------------------------------------------------------

def test_seal_linux_removes_payload_and_key(tmp_path):
    env = _seal_env(tmp_path)
    root = Path(env["SEAL_ROOT"])
    result = _seal(tmp_path, env, "no")
    assert result.returncode == 0, result.stderr
    assert not (root / "tmp/image-build").exists()
    assert (root / "home/builder/.ssh/authorized_keys").read_text() == ""
    assert (root / "etc/machine-id").read_text() == ""
    assert not list((root / "etc/ssh").glob("ssh_host_*"))
    assert (root / "etc/ssh/sshd_config").read_text() == "keep me"
    assert (root / "root/.bash_history").read_text() == ""
    assert (root / "home/other/.bash_history").read_text() == ""
    calls = _calls(tmp_path)
    assert "passwd -l builder" in calls
    assert "cloud-init clean --logs" in calls


def test_seal_linux_ssh_off_includes_socket(tmp_path):
    # Ubuntu >= 22.10: ssh.socket owns port 22, so disabling ssh.service alone
    # leaves sshd reachable. Both units are disabled and masked, and the
    # marker both Debian/Ubuntu units honour is written.
    env = _seal_env(tmp_path)
    root = Path(env["SEAL_ROOT"])
    result = _seal(tmp_path, env, "no")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path).splitlines()
    for unit in ("ssh.service", "ssh.socket"):
        assert f"systemctl disable {unit}" in calls, calls
        assert f"systemctl mask {unit}" in calls, calls
    assert (root / "etc/ssh/sshd_not_to_be_run").is_file()


def test_seal_linux_ssh_off_tolerates_missing_unit_only(tmp_path):
    # A base without ssh.socket (pre-22.10) still seals.
    env = _seal_env(tmp_path)
    root = Path(env["SEAL_ROOT"])
    env["FAKE_SYSTEMCTL_MISSING"] = "ssh.socket"
    result = _seal(tmp_path, env, "no")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path).splitlines()
    assert "systemctl mask ssh.service" in calls, calls
    assert "systemctl mask ssh.socket" not in calls, calls
    assert (root / "etc/ssh/sshd_not_to_be_run").is_file()
    # Any other failure fails the seal, so the image is not saved.
    for unit in ("ssh.socket", "ssh.service"):
        env = _seal_env(tmp_path / unit)
        env["FAKE_SYSTEMCTL_FAIL"] = unit
        result = _seal(tmp_path / unit, env, "no")
        assert result.returncode != 0, unit
        assert unit in result.stderr, (unit, result.stderr)


def test_seal_linux_keep_ssh(tmp_path):
    env = _seal_env(tmp_path)
    root = Path(env["SEAL_ROOT"])
    result = _seal(tmp_path, env, "yes")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert "systemctl" not in calls
    assert not (root / "etc/ssh/sshd_not_to_be_run").exists()
    # The account is locked either way: key login still works under UsePAM.
    assert "passwd -l builder" in calls
    assert (root / "home/builder/.ssh/authorized_keys").read_text() == ""
    assert not (root / "tmp/image-build").exists()
    assert (root / "etc/machine-id").read_text() == ""
    assert not list((root / "etc/ssh").glob("ssh_host_*"))


def test_seal_linux_installs_hostkey_regen_dropin(tmp_path):
    env = _seal_env(tmp_path)
    root = Path(env["SEAL_ROOT"])
    result = _seal(tmp_path, env, "no")
    assert result.returncode == 0, result.stderr
    dropin = root / "etc/systemd/system/ssh.service.d/10-regen-hostkeys.conf"
    assert dropin.is_file()
    # The unit's own `ExecStartPre=/usr/sbin/sshd -t` fails without host keys,
    # so the list is reset and the keys are made before sshd checks them.
    assert dropin.read_text().splitlines() == [
        "[Service]",
        "ExecStartPre=",
        "ExecStartPre=/usr/bin/ssh-keygen -A",
        "ExecStartPre=/usr/sbin/sshd -t",
    ]


def test_seal_linux_fails_if_payload_survives(tmp_path):
    env = _seal_env(tmp_path)
    _tool(Path(env["PATH"].split(":")[0]), "rm", "exit 0\n")
    result = _seal(tmp_path, env, "no")
    assert result.returncode == 1
    assert "image-build" in result.stderr


def test_seal_linux_fails_if_stray_payload_file_survives(tmp_path):
    env = _seal_env(tmp_path)
    root = Path(env["SEAL_ROOT"])
    (root / "home/other/setup.sh").write_text("stray")
    result = _seal(tmp_path, env, "no")
    assert result.returncode == 1
    assert "setup.sh" in result.stderr


def test_seal_linux_rejects_bad_keep_ssh(tmp_path):
    env = _seal_env(tmp_path)
    root = Path(env["SEAL_ROOT"])
    for value in ("maybe", "YES", "true", ""):
        result = _seal(tmp_path, env, value)
        assert result.returncode == 2, value
        assert "keep_ssh" in result.stderr
    result = _seal(tmp_path, env, None)
    assert result.returncode == 2
    # Nothing was touched.
    assert (root / "tmp/image-build").exists()
    assert _calls(tmp_path) == ""


# --- build_image.sh ---------------------------------------------------------

BUILD = IMAGE_BUILD / "build_image.sh"
FAKE_BACKENDS = ROOT / "tests" / "fixtures" / "image_build" / "backends"
KEY_MATERIAL = "FAKE-PRIVATE-KEY-MATERIAL"
FULL_SHA = "abc1234def5678901234567890abcdef12345678"

BASE_RECIPE = {
    "IMAGE_PREFIX": "example-lab-linux",
    "OS_FAMILY": "linux",
    "PAYLOAD_DIR": "payload",
    "SETUP": "setup.sh",
    "VERIFY": "verify.sh",
    "BASE_PROXMOX": "base-ubuntu-2404-*",
    "BASE_AWS": "self:base-ubuntu-2404-*",
    "BASE_GCP": "ubuntu-os-cloud/ubuntu-2404-lts-amd64",
    "BASE_FAKE": "fake-base-*",  # the recipe's BASE_<BACKEND>
    "DISK_GB": "20",
    "AWS_INSTANCE_TYPE": "t3.small",
    "GCP_MACHINE_TYPE": "e2-small",
    "PVE_MEMORY_MB": "2048",
    "PVE_CORES": "2",
}

# The build VM. Every remote command line is kept intact (NUL-separated) and,
# with the boot it ran in, in $CALL_LOG.timeline. A boot-ID read prints
# boot-<n>. A reboot command (or one matching FAKE_SSH_REBOOT_ON) makes the
# boot ID change, but only after FAKE_BOOT_FLIP_AFTER more reads (default 0):
# a VM still going down answers with its old boot.
# FAKE_SSH_FAIL_ON fails every matching call with FAKE_SSH_FAIL_RC, or only
# the first FAKE_SSH_FAIL_TIMES of them. FAKE_SSH_PROBES lists the exit codes
# of successive readiness probes (`exit 0`); its last code repeats. The first
# FAKE_BOOT_FAIL_TIMES boot-ID reads fail, as on a Windows VM that answers SSH
# before the read works. FAKE_BOOT_HANG makes every boot-ID read hang.
# FAKE_CLOCK_STEPS makes a LastBootUpTime read print a new value every time
# within one boot, as it did on a Windows domain controller build.
FAKE_SSH = LOG_CALL + (
    'for a in "$@"; do last="$a"; done; printf "%s\\0" "$last" >> "$CALL_LOG.remote"\n'
    'boot="$CALL_LOG.boot"; pending="$CALL_LOG.reboot-pending"\n'
    '[ -f "$boot" ] || echo 1 > "$boot"\n'
    'case "$last" in *boot_id*|*StartTime*|*LastBootUpTime*)\n'
    '  if [ -f "$pending" ]; then\n'
    '    n=$(cat "$pending")\n'
    '    if [ "$n" -le 0 ]; then echo $(( $(cat "$boot") + 1 )) > "$boot"; rm -f "$pending"\n'
    '    else echo $((n - 1)) > "$pending"; fi\n'
    '  fi ;;\n'
    'esac\n'
    'printf "boot-%s\\t%s\\n" "$(cat "$boot")" "$last" >> "$CALL_LOG.timeline"\n'
    'case "$last" in *"systemctl reboot"*|*Restart-Computer*|*"${FAKE_SSH_REBOOT_ON:-@none@}"*)\n'
    '  echo "${FAKE_BOOT_FLIP_AFTER:-0}" > "$pending" ;;\n'
    'esac\n'
    'case "$last" in *boot_id*|*StartTime*|*LastBootUpTime*)\n'
    '  [ -z "${FAKE_BOOT_HANG:-}" ] || PATH=/usr/bin:/bin exec sleep 30\n'
    '  c="$CALL_LOG.boot-fails"; n=$(cat "$c" 2>/dev/null || echo 0)\n'
    '  if [ "$n" -lt "${FAKE_BOOT_FAIL_TIMES:-0}" ]; then\n'
    '    echo $((n + 1)) > "$c"; echo "the boot-ID read failed" >&2; exit 1\n'
    '  fi\n'
    '  case "$last" in *LastBootUpTime*)\n'
    '    if [ -n "${FAKE_CLOCK_STEPS:-}" ]; then\n'
    '      c="$CALL_LOG.clock-steps"; n=$(( $(cat "$c" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$c"\n'
    '      echo "boot-$(cat "$boot").$n"; exit 0\n'
    '    fi ;;\n'
    '  esac\n'
    '  echo "boot-$(cat "$boot")"; exit 0 ;;\n'
    'esac\n'
    'if [ "$last" = "exit 0" ] && [ -n "${FAKE_SSH_PROBES:-}" ]; then\n'
    '  c="$CALL_LOG.probes"; n=$(cat "$c" 2>/dev/null || echo 0); echo $((n + 1)) > "$c"\n'
    '  i=0; for code in $FAKE_SSH_PROBES; do rc=$code; [ "$i" -eq "$n" ] && break; i=$((i + 1)); done\n'
    '  exit "$rc"\n'
    'fi\n'
    'case "$*" in *"${FAKE_SSH_FAIL_ON:-@none@}"*)\n'
    '  c="$CALL_LOG.ssh-fails"; n=$(cat "$c" 2>/dev/null || echo 0); echo $((n + 1)) > "$c"\n'
    '  [ -n "${FAKE_SSH_FAIL_TIMES:-}" ] && [ "$n" -ge "$FAKE_SSH_FAIL_TIMES" ] || exit "${FAKE_SSH_FAIL_RC:-1}" ;;\n'
    'esac\n'
    # The real sleep, not the harness's fast one: the build must be signalled
    # while this command still runs.
    'case "$*" in *"${FAKE_SSH_SLEEP_ON:-@none@}"*) PATH=/usr/bin:/bin exec sleep 30 ;; esac\n'
    # A command that ignores TERM (the sleep inherits the ignore through exec)
    # and has a child of its own that ignores it too; it records both PIDs.
    'case "$*" in *"${FAKE_SSH_STUBBORN_ON:-@none@}"*)\n'
    '  trap "" TERM; PATH=/usr/bin:/bin sleep 60 & echo $! > "$CALL_LOG.stubborn-child"\n'
    '  echo $$ > "$CALL_LOG.stubborn"; PATH=/usr/bin:/bin exec sleep 60 ;;\n'
    'esac\n'
    "exit 0\n"
)
# Copies the uploaded file (second-to-last argument) into $SCP_SINK.
# FAKE_SCP_FAIL_ON / _RC / _TIMES work as FAKE_SSH_FAIL_ON does.
FAKE_SCP = LOG_CALL + (
    'case "$*" in *"${FAKE_SCP_FAIL_ON:-@none@}"*)\n'
    '  c="$CALL_LOG.scp-fails"; n=$(cat "$c" 2>/dev/null || echo 0); echo $((n + 1)) > "$c"\n'
    '  [ -n "${FAKE_SCP_FAIL_TIMES:-}" ] && [ "$n" -ge "$FAKE_SCP_FAIL_TIMES" ] || exit "${FAKE_SCP_FAIL_RC:-1}" ;;\n'
    'esac\n'
    'n=0; for a in "$@"; do n=$((n + 1)); [ "$n" -eq $(($# - 1)) ] && src="$a"; done\n'
    'mkdir -p "$SCP_SINK" && cp "$src" "$SCP_SINK/"\n'
)
# The core's poll sleeps (SSH readiness, upload retries, reboot waits), cut
# short so a build does not take minutes. Not logged; _fast_sleep logs them.
FAKE_SLEEP = "PATH=/usr/bin:/bin exec sleep 0.01\n"
FAKE_KEYGEN = LOG_CALL + (
    'while [ $# -gt 0 ]; do [ "$1" = -f ] && f="$2"; shift; done\n'
    f"printf '{KEY_MATERIAL}\\n' > \"$f\"\n"
    "printf 'ssh-ed25519 AAAAC3FAKEPUBLIC image-build\\n' > \"$f.pub\"\n"
)
FAKE_GIT = LOG_CALL + (
    '[ "$1" = -C ] && shift 2\n'
    'case "$*" in\n'
    '  "rev-parse --show-toplevel") echo "$FAKE_GIT_TOPLEVEL" ;;\n'
    '  "rev-parse --show-prefix") echo "$FAKE_GIT_PREFIX" ;;\n'
    f'  "rev-parse HEAD") echo {FULL_SHA} ;;\n'
    '  "status --porcelain"*) [ -n "${FAKE_GIT_STATUS:-}" ] && printf "%s\\n" "$FAKE_GIT_STATUS"; exit 0 ;;\n'
    '  *) echo "fake git: unexpected: $*" >&2; exit 1 ;;\n'
    "esac\n"
)
FAKE_CLOUD = LOG_CALL + (
    'case "$1" in\n'
    "  resolve) echo base-1234 ;;\n"
    '  leftovers) for id in ${FAKE_LEFTOVERS:-}; do echo "$id"; done ;;\n'
    # The tag a reaper re-reads: anything in FAKE_UNTAGGED is not a build VM.
    '  tag) case " ${FAKE_UNTAGGED:-} " in *" $2 "*) echo "owner=someone-else" ;;\n'
    '       *) echo "image-build=ib-202601010000-abcd" ;; esac ;;\n'
    "  fail) exit 1 ;;\n"
    "esac\n"
)


def _build_env(tmp_path: Path, os_family: str = "linux", drop=(), **overrides):
    """A course repo holding one recipe and its payload, the fake backend, and
    logging fakes for every command that reaches a build VM or a cloud."""
    repo = tmp_path / "course"
    lab = repo / "labs" / "demo"
    payload = lab / "payload"
    (payload / "packer").mkdir(parents=True)
    (payload / "packer" / "template.pkr.hcl").write_text("packer")
    (payload / "disk.iso").write_text("iso")
    recipe = dict(BASE_RECIPE)
    if os_family == "windows":
        recipe.update(OS_FAMILY="windows", SETUP="setup.ps1", VERIFY="verify.ps1",
                      IMAGE_PREFIX="example-lab-windows")
    recipe.update(overrides)
    for key in drop:
        recipe.pop(key)
    # The payload holds the OS's default scripts; an overridden SETUP/VERIFY
    # names a file that does not exist.
    ext = "ps1" if os_family == "windows" else "sh"
    for name in (f"setup.{ext}", f"verify.{ext}"):
        (payload / name).write_text("echo step\n")
    recipe_path = lab / "recipe.env"
    recipe_path.write_text("".join(f"{k}={shlex.quote(v)}\n" for k, v in recipe.items()))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("ssh", FAKE_SSH), ("scp", FAKE_SCP), ("ssh-keygen", FAKE_KEYGEN),
                       ("git", FAKE_GIT), ("fake-cloud", FAKE_CLOUD), ("sleep", FAKE_SLEEP)):
        _tool(bin_dir, name, body)
    config = tmp_path / "config.env"
    config.write_text("FAKE_REGION='test-1'\n")
    (tmp_path / "tmp").mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("IMAGE_BUILD_", "FAKE_"))}
    env.update(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        CALL_LOG=str(tmp_path / "calls.log"),
        IMAGE_BUILD_CONFIG=str(config),
        IMAGE_BUILD_BACKENDS_DIR=str(FAKE_BACKENDS),
        FAKE_GIT_TOPLEVEL=str(repo),
        FAKE_GIT_PREFIX="labs/demo/",
        TMPDIR=str(tmp_path / "tmp"),
        SCP_SINK=str(tmp_path / "sink"),
    )
    return env, recipe_path


def _build(env: dict, *args):
    return subprocess.run(
        ["bash", str(BUILD), *[str(a) for a in args]],
        env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
    )


def _build_log(tmp_path: Path) -> Path:
    return tmp_path / "course" / "coursework" / "deploy" / "image-build" / "BUILD-LOG.tsv"


def _events(calls: str, setup: str, verify: str, seal: str) -> list:
    """Reduce CALL_LOG to the build's steps: backend calls, the payload upload,
    and the remote setup/verify/seal commands."""
    steps = []
    for line in calls.splitlines():
        word = line.split(" ", 1)[0]
        if word in ("backend_check_config", "backend_resolve_base", "backend_start",
                    "backend_save", "backend_destroy"):
            steps.append(word[len("backend_"):])
        elif word == "scp" and "payload.tgz" in line:
            steps.append("upload")
        elif word == "ssh" and seal in line:
            steps.append("seal")
        elif word == "ssh" and setup in line:
            steps.append("setup")
        elif word == "ssh" and verify in line:
            steps.append("verify")
    return steps


def _remote_commands(calls: str, tool: str = "ssh") -> list:
    return [line for line in calls.splitlines() if line.startswith(tool + " ")]


def test_dry_run_runs_no_mutation(tmp_path):
    env, recipe = _build_env(tmp_path)
    result = _build(env, "--dry-run", recipe, "fake")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert _remote_commands(calls, "ssh") == []
    assert _remote_commands(calls, "scp") == []
    assert _remote_commands(calls, "fake-cloud") == []
    for printed in ("DRY-RUN: ssh ", "DRY-RUN: scp ", "DRY-RUN: tar ",
                    "DRY-RUN: fake-cloud start", "DRY-RUN: fake-cloud save",
                    "DRY-RUN: fake-cloud destroy", "DRY-RUN: fake-cloud resolve"):
        assert printed in result.stderr, printed
    # The same steps as a real build, in the same order.
    assert _events(calls, "setup.sh", "verify.sh", "seal_linux.sh") == [
        "check_config", "resolve_base", "start", "save", "destroy"]
    assert "sudo bash /tmp/image-build/seal_linux.sh no" in result.stderr
    # The boot-ID read and the reboot a setup exit 100 would trigger are shown.
    assert "DRY-RUN: ssh " in result.stderr and "cat /proc/sys/kernel/random/boot_id" in result.stderr
    assert "nohup sudo systemctl reboot" in result.stderr
    assert not _build_log(tmp_path).exists()
    assert result.stdout.rstrip("\n").splitlines()[-1].startswith("fake: {image: ")


def test_order_of_operations(tmp_path):
    env, recipe = _build_env(tmp_path, SETUP_PASSES="2")
    result = _build(env, recipe, "fake")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert _events(calls, "setup.sh", "verify.sh", "seal_linux.sh") == [
        "check_config", "resolve_base", "start", "upload", "setup", "setup",
        "verify", "seal", "save", "destroy"]
    # Setup exited 0: no reboot.
    assert "reboot" not in "".join(_remote_lines(tmp_path))
    lines = _build_log(tmp_path).read_text().splitlines()
    assert lines[0] == "date\tbackend\timage_id\timage_name\tbase_id\trecipe\tcommit\tverify"
    assert len(lines) == 2
    date, backend, image_id, image_name, base_id, recipe_rel, commit, verify = lines[1].split("\t")
    assert backend == "fake"
    assert image_id == f"img-{image_name}"
    assert image_name.startswith("example-lab-linux-abc1234-")
    assert base_id == "base-1234"
    assert "backend_resolve_base fake-base-*" in calls
    assert "backend_start ib-" in calls and " base-1234 " in calls
    assert recipe_rel == "labs/demo/recipe.env"
    assert commit == "abc1234"
    assert verify == "pass"
    assert date.endswith("Z")
    # Stdout ends with the templates.yaml lines.
    assert result.stdout.rstrip("\n").splitlines()[-1] == f"fake: {{image: {image_id}, disk_gb: 20}}"
    # The per-run key's path may appear; its material never does.
    for text in (result.stdout, result.stderr, calls, _build_log(tmp_path).read_text()):
        assert KEY_MATERIAL not in text


def test_ssh_options_and_jump(tmp_path):
    env, recipe = _build_env(tmp_path)
    env["FAKE_JUMP"] = "root@203.0.113.5"
    result = _build(env, recipe, "fake")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    for line in _remote_commands(calls, "ssh") + _remote_commands(calls, "scp"):
        for opt in ("-o StrictHostKeyChecking=no", "-o UserKnownHostsFile=/dev/null",
                    "-o ConnectTimeout=10", "-J root@203.0.113.5", "-i "):
            assert opt in line, (opt, line)
    env2, recipe2 = _build_env(tmp_path / "nojump")
    result = _build(env2, recipe2, "fake")
    assert result.returncode == 0, result.stderr
    assert all(" -J " not in line for line in _remote_commands(_calls(tmp_path / "nojump")))


def test_failed_verify_destroys_and_saves_nothing(tmp_path):
    env, recipe = _build_env(tmp_path)
    env["FAKE_SSH_FAIL_ON"] = "verify.sh"
    result = _build(env, recipe, "fake")
    assert result.returncode == 1
    steps = _events(_calls(tmp_path), "setup.sh", "verify.sh", "seal_linux.sh")
    assert "destroy" in steps
    assert "save" not in steps
    assert "seal" not in steps
    assert not _build_log(tmp_path).exists()


def test_failed_seal_saves_nothing(tmp_path):
    env, recipe = _build_env(tmp_path)
    env["FAKE_SSH_FAIL_ON"] = "seal_linux.sh"
    result = _build(env, recipe, "fake")
    assert result.returncode == 1
    steps = _events(_calls(tmp_path), "setup.sh", "verify.sh", "seal_linux.sh")
    assert steps[-1] == "destroy"
    assert "save" not in steps
    assert not _build_log(tmp_path).exists()


def test_failed_save_destroys(tmp_path):
    env, recipe = _build_env(tmp_path)
    env["FAKE_FAIL"] = "backend_save"
    result = _build(env, recipe, "fake")
    assert result.returncode == 1
    assert "backend_destroy vm-" in _calls(tmp_path)
    assert not _build_log(tmp_path).exists()


def _fast_sleep(tmp_path: Path) -> None:
    """The core's 10 s poll sleep, shortened for the reboot-wait tests."""
    _tool(tmp_path / "bin", "sleep", LOG_CALL + "PATH=/usr/bin:/bin exec sleep 0.1\n")


def _timeline(tmp_path: Path) -> list:
    """(boot, remote command line) for every SSH command, in order."""
    log = tmp_path / "calls.log.timeline"
    return [tuple(l.split("\t", 1)) for l in log.read_text().splitlines()] if log.exists() else []


def _is_boot_read(command: str) -> bool:
    return any(s in command for s in ("boot_id", "StartTime", "LastBootUpTime"))


def test_setup_exit_100_core_reboots_and_waits_for_new_boot(tmp_path):
    for os_family, setup, verify, reboot in (
            ("linux", "setup.sh", "verify.sh", "nohup sudo systemctl reboot"),
            ("windows", "setup.ps1", "verify.ps1", "Restart-Computer -Force")):
        sub = tmp_path / os_family
        env, recipe = _build_env(sub, os_family)
        _fast_sleep(sub)
        # The VM keeps answering with its old boot ID for three polls.
        env.update(FAKE_SSH_FAIL_ON=setup, FAKE_SSH_FAIL_RC="100", FAKE_BOOT_FLIP_AFTER="3")
        result = _build(env, recipe, "fake")
        assert result.returncode == 0, result.stderr
        timeline = _timeline(sub)
        commands = [c for _b, c in timeline]
        i_setup = next(i for i, c in enumerate(commands) if setup in c)
        i_reboot = next(i for i, c in enumerate(commands) if reboot in c)
        i_verify = next(i for i, c in enumerate(commands) if verify in c)
        assert i_setup < i_reboot < i_verify, commands
        # The setup did not reboot; the core did, once.
        assert sum(reboot in c for c in commands) == 1, commands
        # Verify ran only in the new boot, after the polls that still saw the old one.
        assert timeline[i_verify][0] == "boot-2", timeline
        polls = [b for b, c in timeline[i_reboot + 1:i_verify] if _is_boot_read(c)]
        assert polls[:3] == ["boot-1"] * 3 and "boot-2" in polls, timeline
        assert all(b == "boot-1" for b, _c in timeline[:i_reboot + 1]), timeline


def test_setup_session_drop_requires_a_new_boot(tmp_path):
    # Exit 255 (session dropped) and the VM rebooted: the build goes on.
    env, recipe = _build_env(tmp_path / "rebooted")
    _fast_sleep(tmp_path / "rebooted")
    env.update(FAKE_SSH_FAIL_ON="setup.sh", FAKE_SSH_FAIL_RC="255", FAKE_SSH_REBOOT_ON="setup.sh",
               FAKE_BOOT_FLIP_AFTER="2")
    result = _build(env, recipe, "fake")
    assert result.returncode == 0, result.stderr
    timeline = _timeline(tmp_path / "rebooted")
    verify = [b for b, c in timeline if "verify.sh" in c]
    assert verify == ["boot-2"], timeline
    assert not any("systemctl reboot" in c for _b, c in timeline)

    # Exit 255 but the boot ID never changes: the session dropped without a
    # reboot, so the build stops when the wait runs out.
    env, recipe = _build_env(tmp_path / "same")
    _fast_sleep(tmp_path / "same")
    env.update(FAKE_SSH_FAIL_ON="setup.sh", FAKE_SSH_FAIL_RC="255", IMAGE_BUILD_SSH_WAIT_SECONDS="2")
    result = _build(env, recipe, "fake")
    assert result.returncode == 1, result.stderr
    assert "SSH dropped during setup but the VM did not reboot" in result.stderr
    steps = _events(_calls(tmp_path / "same"), "setup.sh", "verify.sh", "seal_linux.sh")
    assert "verify" not in steps and "save" not in steps
    assert steps[-1] == "destroy"

    # Any other exit code fails the build at once.
    env2, recipe2 = _build_env(tmp_path / "fail")
    env2.update(FAKE_SSH_FAIL_ON="setup.sh", FAKE_SSH_FAIL_RC="3")
    result = _build(env2, recipe2, "fake")
    assert result.returncode == 1
    steps = _events(_calls(tmp_path / "fail"), "setup.sh", "verify.sh", "seal_linux.sh")
    assert "verify" not in steps and "save" not in steps
    assert steps[-1] == "destroy"


def test_windows_boot_id_holds_when_last_boot_time_moves(tmp_path):
    # The VM's LastBootUpTime moves on every read within one boot, as it did on
    # a domain controller build, where a change read just after the reboot
    # command was taken for a reboot. The boot ID the builder reads must stay
    # the same until the VM really reboots.
    env, recipe = _build_env(tmp_path / "reboot", "windows")
    _fast_sleep(tmp_path / "reboot")
    env.update(FAKE_SSH_FAIL_ON="setup.ps1", FAKE_SSH_FAIL_RC="100", FAKE_BOOT_FLIP_AFTER="3",
               FAKE_CLOCK_STEPS="1")
    result = _build(env, recipe, "fake")
    assert result.returncode == 0, result.stderr
    timeline = _timeline(tmp_path / "reboot")
    commands = [c for _b, c in timeline]
    i_reboot = next(i for i, c in enumerate(commands) if "Restart-Computer -Force" in c)
    i_verify = next(i for i, c in enumerate(commands) if "verify.ps1" in c)
    assert timeline[i_verify][0] == "boot-2", timeline
    polls = [b for b, c in timeline[i_reboot + 1:i_verify] if _is_boot_read(c)]
    assert polls[:3] == ["boot-1"] * 3, timeline
    assert "the VM rebooted (boot ID boot-1 -> boot-2)" in result.stderr

    # The session drops without a reboot: an unchanged boot ID is no reboot,
    # so the build stops when the wait runs out.
    env, recipe = _build_env(tmp_path / "same", "windows")
    _fast_sleep(tmp_path / "same")
    env.update(FAKE_SSH_FAIL_ON="setup.ps1", FAKE_SSH_FAIL_RC="255", FAKE_CLOCK_STEPS="1",
               IMAGE_BUILD_SSH_WAIT_SECONDS="2")
    result = _build(env, recipe, "fake")
    assert result.returncode == 1, result.stderr
    assert "SSH dropped during setup but the VM did not reboot" in result.stderr
    steps = _events(_calls(tmp_path / "same"), "setup.ps1", "verify.ps1", "seal_windows.ps1")
    assert "verify" not in steps and "save" not in steps
    assert steps[-1] == "destroy"


def test_boot_id_read_before_setup_retries_until_it_answers(tmp_path):
    # SSH is up, but the first two boot-ID reads fail: the build waits and goes on.
    env, recipe = _build_env(tmp_path, "windows")
    _fast_sleep(tmp_path)
    env.update(FAKE_BOOT_FAIL_TIMES="2")
    result = _build(env, recipe, "fake")
    assert result.returncode == 0, result.stderr
    assert "(attempt 1); retrying in 10s" in result.stderr
    assert "(attempt 2); retrying in 10s" in result.stderr
    assert "(attempt 3)" not in result.stderr
    timeline = _timeline(tmp_path)
    commands = [c for _b, c in timeline]
    i_setup = next(i for i, c in enumerate(commands) if "setup.ps1" in c)
    # Setup ran only after a boot-ID read succeeded (the third read).
    assert sum(_is_boot_read(c) for c in commands[:i_setup]) == 3, commands


def test_boot_id_read_before_setup_gives_up_when_the_wait_runs_out(tmp_path):
    env, recipe = _build_env(tmp_path, "windows")
    _fast_sleep(tmp_path)
    env.update(FAKE_BOOT_FAIL_TIMES="100000", IMAGE_BUILD_SSH_WAIT_SECONDS="2")
    result = _build(env, recipe, "fake")
    assert result.returncode == 1, result.stderr
    assert "cannot read the boot ID of" in result.stderr and "attempts over 2s" in result.stderr
    steps = _events(_calls(tmp_path), "setup.ps1", "verify.ps1", "seal_windows.ps1")
    assert "setup" not in steps and "save" not in steps
    assert steps[-1] == "destroy"


def test_ssh_wait_seconds_checked(tmp_path):
    for i, bad in enumerate(("0", "abc", "-5", "08")):
        env, recipe = _build_env(tmp_path / str(i))
        env["IMAGE_BUILD_SSH_WAIT_SECONDS"] = bad
        result = _build(env, "--dry-run", recipe, "fake")
        assert result.returncode == 2, bad
        assert "IMAGE_BUILD_SSH_WAIT_SECONDS" in result.stderr


def test_exit_2_only_before_any_vm_started(tmp_path):
    # A backend step that exits 2 once the build is under way is a build
    # failure (1), not a rejected input.
    for fn in ("backend_resolve_base", "backend_start", "backend_save"):
        env, recipe = _build_env(tmp_path / fn)
        env.update(FAKE_FAIL=fn, FAKE_FAIL_RC="2")
        result = _build(env, recipe, "fake")
        assert result.returncode == 1, (fn, result.stderr)
    # A config the backend rejects is still 2.
    env, recipe = _build_env(tmp_path / "check")
    env.update(FAKE_FAIL="backend_check_config", FAKE_FAIL_RC="2")
    assert _build(env, recipe, "fake").returncode == 2


def _append(path: Path, text: str) -> None:
    path.write_text(path.read_text() + text)


def test_recipe_cannot_touch_core_state(tmp_path):
    # DRY_RUN=0 in a recipe does not turn a dry run live.
    env, recipe = _build_env(tmp_path / "dry")
    _append(recipe, "DRY_RUN=0\nQUERY_LIVE=1\n")
    result = _build(env, "--dry-run", recipe, "fake")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path / "dry")
    assert _remote_commands(calls, "fake-cloud") == []
    assert _remote_commands(calls, "ssh") == [] and _remote_commands(calls, "scp") == []
    assert "DRY-RUN: fake-cloud start" in result.stderr

    # STARTED/BUILD_VM_ID from a recipe do not make the trap destroy anything.
    env, recipe = _build_env(tmp_path / "started")
    _append(recipe, "STARTED=1\nBUILD_VM_ID=vm-victim\n")
    Path(env["IMAGE_BUILD_CONFIG"]).write_text("# nothing configured\n")
    result = _build(env, recipe, "fake")
    assert result.returncode == 2, result.stderr
    calls = _calls(tmp_path / "started")
    assert "backend_destroy" not in calls and "vm-victim" not in calls

    # A recipe cannot replace the core's helpers.
    env, recipe = _build_env(tmp_path / "func")
    _append(recipe, "log() { echo HIJACKED >&2; }\nmutate() { echo HIJACKED >&2; }\n")
    result = _build(env, recipe, "fake")
    assert result.returncode == 0, result.stderr
    assert "HIJACKED" not in result.stderr
    assert "[image-build] run ib-" in result.stderr
    assert "fake-cloud start" in _calls(tmp_path / "func")


def test_recipe_values_round_trip(tmp_path):
    # Spaces, quotes, $ and a glob survive exactly; nothing is split or expanded.
    value = """fake base-* 'q' "dq" $HOME \\ *"""
    env, recipe = _build_env(tmp_path, BASE_FAKE=value, PAYLOAD_EXCLUDE="packer *.iso")
    result = _build(env, "--dry-run", recipe, "fake")
    assert result.returncode == 0, result.stderr
    assert f"backend_resolve_base {value}\n" in _calls(tmp_path)
    assert f"DRY-RUN: fake-cloud resolve {value}\n" in result.stderr
    tar_line = [l for l in result.stderr.splitlines() if l.startswith("DRY-RUN: tar ")][0]
    assert "--exclude=packer" in tar_line.split() and "--exclude=*.iso" in tar_line.split()


def test_config_cannot_touch_core_state_or_recipe_keys(tmp_path):
    config_ok = "FAKE_REGION='test-1'\n"
    # DRY_RUN=0 in the config does not turn a dry run live.
    env, recipe = _build_env(tmp_path / "dry")
    Path(env["IMAGE_BUILD_CONFIG"]).write_text(config_ok + "DRY_RUN=0\nQUERY_LIVE=1\n")
    result = _build(env, "--dry-run", recipe, "fake")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path / "dry")
    assert _remote_commands(calls, "fake-cloud") == []
    assert _remote_commands(calls, "ssh") == [] and _remote_commands(calls, "scp") == []

    # STARTED/BUILD_VM_ID from the config do not make the trap destroy anything.
    env, recipe = _build_env(tmp_path / "started")
    Path(env["IMAGE_BUILD_CONFIG"]).write_text("STARTED=1\nBUILD_VM_ID=vm-victim\n")
    result = _build(env, recipe, "fake")
    assert result.returncode == 2, result.stderr
    calls = _calls(tmp_path / "started")
    assert "backend_destroy" not in calls and "vm-victim" not in calls

    # Only the recipe sets recipe keys: a config that sets one is refused.
    for key, value in (("DISK_GB", "999"), ("KEEP_SSH", "yes"), ("BASE_FAKE", "other-*"),
                       ("AWS_INSTANCE_TYPE", "t3.large")):
        env, recipe = _build_env(tmp_path / key)
        Path(env["IMAGE_BUILD_CONFIG"]).write_text(config_ok + f"{key}={value}\n")
        result = _build(env, "--dry-run", recipe, "fake")
        assert result.returncode == 2, (key, result.stderr)
        assert key in result.stderr, (key, result.stderr)
        assert "backend_start" not in _calls(tmp_path / key)

    # The backend's own keys still arrive, exactly.
    env, recipe = _build_env(tmp_path / "value")
    Path(env["IMAGE_BUILD_CONFIG"]).write_text("FAKE_REGION='a b * \"c\"'\n")
    result = _build(env, "--dry-run", recipe, "fake")
    assert result.returncode == 0, result.stderr
    assert 'backend_check_config region=a b * "c"' in _calls(tmp_path / "value")


# A destroy that, like an SSH session relaying the backend's output, fails if
# it cannot write to its stderr. FAKE_DESTROY_SLEEP makes it take that long,
# after marking $CALL_LOG.destroying; $CALL_LOG.destroyed gets one line per
# destroy that ran to the end. FAKE_DESTROY_HANG makes it hang (with TERM
# ignored, as the trap leaves it) after recording its PID and what was left in
# TMPDIR when it started.
FAKE_CLOUD_DESTROY = (
    'if [ "$1" = destroy ]; then\n'
    '  echo "fake-cloud: destroying $2" >&2 || exit 1\n'
    '  if [ -n "${FAKE_DESTROY_HANG:-}" ]; then\n'
    '    ls -A "$TMPDIR" > "$CALL_LOG.tmp-at-destroy"\n'
    '    PATH=/usr/bin:/bin sleep 60 & echo $! > "$CALL_LOG.hung-child"\n'
    '    echo $$ > "$CALL_LOG.hung"; PATH=/usr/bin:/bin exec sleep 60\n'
    '  fi\n'
    '  if [ -n "${FAKE_DESTROY_SLEEP:-}" ]; then\n'
    '    : > "$CALL_LOG.destroying"; PATH=/usr/bin:/bin sleep "$FAKE_DESTROY_SLEEP"\n'
    '  fi\n'
    '  echo "$2" >> "$CALL_LOG.destroyed"\n'
    'fi\n'
)
SIGNAL_RC = {signal.SIGTERM: 143, signal.SIGINT: 130, signal.SIGHUP: 129}


def _wait_for(predicate, proc, what: str, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while not predicate():
        assert proc.poll() is None, f"the build exited (rc {proc.returncode}) before {what}"
        assert time.time() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


def _signalled_build(tmp_path: Path, sigs, *, log_reader_dead: bool, group: bool,
                     during_destroy=(), env_extra=None, ready=None, started="setup.sh"):
    """Start a build in its own process group, as a launcher running it in the
    background would, and send `sigs` once setup is running on the build VM.
    log_reader_dead: stderr is a pipe whose only reader is gone before the
    signal (a killed `tee`). group: each signal goes to the whole process group,
    so it also reaches the in-flight setup `ssh`. during_destroy: signals sent
    to the group while the trap's destroy is running. env_extra: more
    environment. ready: a file (next to CALL_LOG) to wait for as well.
    started: the remote command that must be in flight before the signal.
    Returns (exit code, CALL_LOG text, stderr text or None, seconds from the
    first signal to the exit)."""
    env, recipe = _build_env(tmp_path)
    env["FAKE_SSH_SLEEP_ON"] = "setup.sh"
    if during_destroy:
        env["FAKE_DESTROY_SLEEP"] = "2"
    env.update(env_extra or {})
    cloud = tmp_path / "bin" / "fake-cloud"
    cloud.write_text(cloud.read_text() + FAKE_CLOUD_DESTROY)
    err_path = tmp_path / "stderr.txt"
    err_file = None if log_reader_dead else err_path.open("w")
    proc = subprocess.Popen(["bash", str(BUILD), str(recipe), "fake"], env=env,
                            stdout=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                            stderr=subprocess.PIPE if log_reader_dead else err_file,
                            start_new_session=True)
    try:
        _wait_for(lambda: started in "".join(_remote_commands(_calls(tmp_path))),
                  proc, f"{started} started")
        if ready:
            _wait_for(Path(env["CALL_LOG"] + ready).exists, proc, ready)
        if log_reader_dead:
            proc.stderr.close()
        signalled = time.time()
        for sig in sigs:
            if group:
                os.killpg(proc.pid, sig)
            else:
                proc.send_signal(sig)
        if during_destroy:
            destroying = Path(env["CALL_LOG"] + ".destroying")
            _wait_for(destroying.exists, proc, "the trap's destroy started")
            for sig in during_destroy:
                os.killpg(proc.pid, sig)
        rc = proc.wait(timeout=30)
        elapsed = time.time() - signalled
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        if err_file:
            err_file.close()
    return rc, _calls(tmp_path), None if log_reader_dead else err_path.read_text(), elapsed


def _alive(pid_file: Path) -> bool:
    """Whether the process whose PID the file holds is still running (a zombie
    is not)."""
    pid = int(pid_file.read_text())
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except FileNotFoundError:
            return False
        if state == "Z":
            return False
        time.sleep(0.05)
    return True


def _assert_destroyed_once(tmp_path: Path, calls: str) -> None:
    steps = _events(calls, "setup.sh", "verify.sh", "seal_linux.sh")
    assert steps[-1] == "destroy", steps
    assert "save" not in steps and "verify" not in steps, steps
    assert steps.count("destroy") == 1, steps
    assert len(_remote_commands(calls, "fake-cloud destroy")) == 1, calls
    destroyed = tmp_path / "calls.log.destroyed"
    assert destroyed.exists(), "the build VM's destroy did not run to the end"
    assert len(destroyed.read_text().splitlines()) == 1
    # The run's key and payload archive are gone.
    assert list((tmp_path / "tmp").iterdir()) == []


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP],
                         ids=["TERM", "INT", "HUP"])
def test_signal_during_setup_destroys_once(tmp_path, sig):
    rc, calls, stderr, _ = _signalled_build(tmp_path, [sig], log_reader_dead=False, group=False)
    assert rc == SIGNAL_RC[sig], stderr
    _assert_destroyed_once(tmp_path, calls)
    assert "destroying build VM vm-ib-" in stderr
    assert "FAILED during setup pass 1/1" in stderr


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP],
                         ids=["TERM", "INT", "HUP"])
def test_signal_with_log_reader_dead_destroys_once(tmp_path, sig):
    # The signal reached the whole process group: the in-flight setup ssh, and
    # the reader of the builder's stderr (gone before the trap writes a line).
    rc, calls, _, _ = _signalled_build(tmp_path, [sig], log_reader_dead=True, group=True)
    assert rc == SIGNAL_RC[sig]
    _assert_destroyed_once(tmp_path, calls)


def test_signals_during_the_trap_do_not_stop_the_destroy(tmp_path):
    # A second TERM (and an INT and a HUP) while the trap's destroy runs: the
    # destroy still finishes, it runs once, and the exit is the first signal's.
    rc, calls, _, _ = _signalled_build(
        tmp_path, [signal.SIGTERM], log_reader_dead=True, group=True,
        during_destroy=[signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
    assert rc == 143
    _assert_destroyed_once(tmp_path, calls)


def test_failed_setup_destroys_once(tmp_path):
    # The ordinary failure path: no signal, exit 1, one destroy.
    env, recipe = _build_env(tmp_path)
    _fail(env, "ssh", "setup.sh", 1)
    cloud = tmp_path / "bin" / "fake-cloud"
    cloud.write_text(cloud.read_text() + FAKE_CLOUD_DESTROY)
    result = _build(env, recipe, "fake")
    assert result.returncode == 1, result.stderr
    _assert_destroyed_once(tmp_path, _calls(tmp_path))
    assert "destroying build VM vm-ib-" in result.stderr
    assert "FAILED during setup pass 1/1 (exit 1); no image was recorded" in result.stderr


def test_term_ignoring_command_is_killed_within_the_bound(tmp_path):
    # The in-flight setup ssh ignores TERM (the signal reached the whole group,
    # and the trap sends its own): after IMAGE_BUILD_CHILD_STOP_SECONDS it is
    # KILLed, and the destroy and the cleanup still run.
    rc, calls, stderr, elapsed = _signalled_build(
        tmp_path, [signal.SIGTERM], log_reader_dead=False, group=True, ready=".stubborn",
        env_extra={"FAKE_SSH_SLEEP_ON": "@none@", "FAKE_SSH_STUBBORN_ON": "setup.sh",
                   "IMAGE_BUILD_CHILD_STOP_SECONDS": "2"})
    assert rc == 143, stderr
    assert 1 <= elapsed < 15, elapsed
    # Neither the command nor the child it started outlives the build.
    assert not _alive(tmp_path / "calls.log.stubborn")
    assert not _alive(tmp_path / "calls.log.stubborn-child")
    assert "the running command ignored TERM for 2s; killed it" in stderr
    _assert_destroyed_once(tmp_path, calls)


def test_signal_during_a_hung_boot_id_read_destroys_within_the_bound(tmp_path):
    # The boot-ID read before setup hangs (WMI never answers), and TERM reaches
    # only the builder, not the ssh. The read runs through run_bg, so the trap
    # runs at once instead of after the 30 s hang.
    rc, calls, stderr, elapsed = _signalled_build(
        tmp_path, [signal.SIGTERM], log_reader_dead=False, group=False, started="boot_id",
        env_extra={"FAKE_BOOT_HANG": "1"})
    assert rc == 143, stderr
    assert elapsed < 15, elapsed
    assert "FAILED during setup pass 1/1 (exit 143)" in stderr
    _assert_destroyed_once(tmp_path, calls)


def test_hung_destroy_is_killed_and_named(tmp_path):
    # A destroy that hangs is killed after IMAGE_BUILD_DESTROY_TIMEOUT_SECONDS.
    # The trap names the backend and the VM to destroy by hand, the run's key
    # and payload were already gone when the destroy started, and the exit is
    # still the signal's.
    rc, calls, stderr, elapsed = _signalled_build(
        tmp_path, [signal.SIGTERM], log_reader_dead=False, group=False,
        env_extra={"FAKE_DESTROY_HANG": "1", "IMAGE_BUILD_DESTROY_TIMEOUT_SECONDS": "2"})
    assert rc == 143, stderr
    assert 1 <= elapsed < 15, elapsed
    assert not _alive(tmp_path / "calls.log.hung")
    assert not _alive(tmp_path / "calls.log.hung-child")
    run_id = re.search(r"run (ib-\d{12}-[0-9a-f]{16})", stderr).group(1)
    assert ("ERROR: the destroy did not finish within 2s and was killed. "
            f"Destroy it by hand: fake build VM vm-{run_id}") in stderr
    assert "FAILED during setup pass 1/1 (exit 143)" in stderr
    assert not (tmp_path / "calls.log.destroyed").exists()
    assert (tmp_path / "calls.log.tmp-at-destroy").read_text() == ""
    assert list((tmp_path / "tmp").iterdir()) == []


def test_pgrep_required_before_anything_starts(tmp_path):
    # Without pgrep the trap could not find a stopped command's descendants,
    # so a build or a reap is refused before it creates or destroys anything.
    # A dry run needs no pgrep.
    env, recipe = _build_env(tmp_path)
    no_pgrep = tmp_path / "no-pgrep"
    no_pgrep.mkdir()
    for system_dir in ("/usr/bin", "/bin"):
        for name in os.listdir(system_dir):
            target = Path(system_dir) / name
            if name != "pgrep" and not (no_pgrep / name).exists() and os.access(target, os.X_OK):
                (no_pgrep / name).symlink_to(target)
    fakes = env["PATH"].split(":", 1)[0]
    env["PATH"] = f"{fakes}:{no_pgrep}"
    for args in ((recipe, "fake"), ("--reap", "fake")):
        result = _build(env, *args)
        assert result.returncode == 2, (args, result.stderr)
        assert "pgrep is required" in result.stderr, args
        assert "backend_" not in _calls(tmp_path), args
    result = _build(env, "--dry-run", recipe, "fake")
    assert result.returncode == 0, result.stderr


def test_cleanup_bounds_must_be_positive_integers(tmp_path):
    for key in ("IMAGE_BUILD_CHILD_STOP_SECONDS", "IMAGE_BUILD_DESTROY_TIMEOUT_SECONDS"):
        for value in ("0", "x", "-5", "08"):
            env, recipe = _build_env(tmp_path / f"{key}{value}")
            env[key] = value
            result = _build(env, "--dry-run", recipe, "fake")
            assert result.returncode == 2, (key, value, result.stderr)
            assert key in result.stderr, (key, value)
            assert "backend_" not in _calls(tmp_path / f"{key}{value}")


def test_dirty_tree_refused(tmp_path):
    env, recipe = _build_env(tmp_path)
    env["FAKE_GIT_STATUS"] = " M labs/demo/payload/setup.sh"
    result = _build(env, recipe, "fake")
    assert result.returncode == 2
    assert "dirty" in result.stderr
    assert "backend_" not in _calls(tmp_path)
    result = _build(env, "--dry-run", recipe, "fake")
    assert result.returncode == 2
    result = _build(env, "--allow-dirty", recipe, "fake")
    assert result.returncode == 0, result.stderr
    assert _build_log(tmp_path).read_text().splitlines()[1].split("\t")[6] == "abc1234-dirty"


def _git(repo: Path, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
        check=True, capture_output=True, text=True).stdout


def test_build_log_alone_does_not_dirty_the_repo(tmp_path):
    # One recipe is built on several backends before the log is committed: the
    # log a build appends must not make the next build's repo "dirty". Real
    # git, so the pathspec is proven against git itself (and from a recipe in
    # a subdirectory, where a cwd-relative pathspec would miss).
    env, recipe = _build_env(tmp_path)
    (tmp_path / "bin" / "git").unlink()
    repo = tmp_path / "course"
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "recipe")
    sha7 = _git(repo, "rev-parse", "HEAD")[:7]
    log = _build_log(tmp_path)

    def build_ok():
        result = _build(env, recipe, "fake")
        assert result.returncode == 0, result.stderr
        assert log.read_text().splitlines()[-1].split("\t")[6] == sha7

    build_ok()                       # creates the log (untracked)
    build_ok()                       # an untracked log alone is clean
    _git(repo, "add", "coursework")
    _git(repo, "commit", "-q", "-m", "log")
    sha7 = _git(repo, "rev-parse", "HEAD")[:7]
    build_ok()                       # appends: the log is now modified
    build_ok()                       # a modified log alone is clean

    # Anything else still refuses, even next to the log.
    for other in ("labs/demo/payload/setup.sh", "coursework/deploy/image-build/notes.txt"):
        path = repo / other
        before = path.read_text() if path.exists() else None
        path.write_text("changed\n")
        result = _build(env, recipe, "fake")
        assert result.returncode == 2, (other, result.stderr)
        assert "dirty" in result.stderr
        if before is None:
            path.unlink()
        else:
            path.write_text(before)


def test_recipe_missing_key_refused(tmp_path):
    env, recipe = _build_env(tmp_path, drop=("VERIFY",))
    env["VERIFY"] = "verify.sh"  # an inherited value must not stand in for the recipe's
    result = _build(env, "--dry-run", recipe, "fake")
    assert result.returncode == 2
    assert "VERIFY" in result.stderr
    assert "backend_" not in _calls(tmp_path)


def test_recipe_bad_values_refused(tmp_path):
    for i, (key, value) in enumerate((("OS_FAMILY", "bsd"), ("KEEP_SSH", "maybe"),
                                      ("SETUP_PASSES", "0"), ("DISK_GB", "big"),
                                      ("SETUP", "missing.sh"), ("PAYLOAD_DIR", "nowhere"),
                                      ("PAYLOAD_DIR", "/etc"), ("SETUP", "../setup.sh"),
                                      ("SETUP", "it's.sh"), ("VERIFY", "/tmp/verify.sh"))):
        env, recipe = _build_env(tmp_path / str(i), **{key: value})
        result = _build(env, "--dry-run", recipe, "fake")
        assert result.returncode == 2, (key, result.stderr)
        assert key in result.stderr, (key, result.stderr)


def test_usage_errors(tmp_path):
    env, recipe = _build_env(tmp_path)
    assert _build(env).returncode == 2
    assert _build(env, recipe).returncode == 2
    assert _build(env, "--bogus", recipe, "fake").returncode == 2
    result = _build(env, "--dry-run", recipe, "nosuch")
    assert result.returncode == 2
    assert "nosuch" in result.stderr
    assert _build(env, "--dry-run", recipe, "../fake").returncode == 2


def test_backend_config_missing_refused(tmp_path):
    env, recipe = _build_env(tmp_path)
    Path(env["IMAGE_BUILD_CONFIG"]).write_text("# nothing configured\n")
    result = _build(env, "--dry-run", recipe, "fake")
    assert result.returncode == 2
    assert "FAKE_REGION" in result.stderr
    assert "backend_start" not in _calls(tmp_path)
    env["IMAGE_BUILD_CONFIG"] = str(tmp_path / "absent.env")
    assert _build(env, "--dry-run", recipe, "fake").returncode == 2


def test_image_name_shape(tmp_path):
    env, recipe = _build_env(tmp_path)
    result = _build(env, "--dry-run", recipe, "fake")
    assert result.returncode == 0, result.stderr
    save = [l for l in _calls(tmp_path).splitlines() if l.startswith("backend_save ")]
    name = save[0].split(" ", 1)[1]
    assert re.fullmatch(r"example-lab-linux-[0-9a-f]{7}-[0-9]{12}", name), name
    assert len(name) <= 63
    for i, prefix in enumerate(("a" * 43, "Example-upper", "example_underscore")):
        env, recipe = _build_env(tmp_path / str(i), IMAGE_PREFIX=prefix)
        result = _build(env, "--dry-run", recipe, "fake")
        assert result.returncode == 2, prefix
        assert "backend_start" not in _calls(tmp_path / str(i))
    env, recipe = _build_env(tmp_path / "max", IMAGE_PREFIX="a" * 42)
    assert _build(env, "--dry-run", recipe, "fake").returncode == 0


def test_ttl_changes_expiry(tmp_path):
    def run_and_minutes(sub, ttl):
        env, recipe = _build_env(tmp_path / sub)
        if ttl is not None:
            env["IMAGE_BUILD_TTL_MINUTES"] = ttl
        result = _build(env, "--dry-run", recipe, "fake")
        assert result.returncode == 0, result.stderr
        start = [l for l in _calls(tmp_path / sub).splitlines() if l.startswith("backend_start ")][0]
        run_id, _base, _pub, expires = start.split(" ")[1:5]
        m = re.fullmatch(r"ib-([0-9]{12})-[0-9a-f]{16}", run_id)
        assert m, run_id
        began = datetime.strptime(m.group(1), "%Y%m%d%H%M")
        ends = datetime.strptime(expires.removeprefix("expires="), "%Y%m%d%H%M")
        return (ends - began).total_seconds() / 60
    assert run_and_minutes("default", None) == 240
    assert run_and_minutes("short", "60") == 60
    for i, bad in enumerate(("0", "abc", "-5", "08", "0060")):
        env, recipe = _build_env(tmp_path / f"bad{i}")
        env["IMAGE_BUILD_TTL_MINUTES"] = bad
        assert _build(env, "--dry-run", recipe, "fake").returncode == 2, bad


def test_windows_remote_commands(tmp_path):
    env, recipe = _build_env(tmp_path, "windows")
    result = _build(env, recipe, "fake")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    ssh = _remote_commands(calls, "ssh")
    assert any(l.endswith(' powershell -NoProfile -ExecutionPolicy Bypass -File "C:\\image-build\\setup.ps1"')
               for l in ssh), ssh
    assert any(l.endswith(' powershell -NoProfile -ExecutionPolicy Bypass -File "C:\\image-build\\verify.ps1"')
               for l in ssh), ssh
    assert any(l.endswith(" powershell -NoProfile -ExecutionPolicy Bypass -File C:\\image-build\\seal_windows.ps1")
               for l in ssh), ssh
    scp = _remote_commands(calls, "scp")
    assert any(l.endswith(" Administrator@192.0.2.10:C:/image-build/payload.tgz") for l in scp), scp
    assert any(l.endswith(" Administrator@192.0.2.10:C:/image-build/seal_windows.ps1") for l in scp), scp
    assert _events(calls, "setup.ps1", "verify.ps1", "seal_windows.ps1") == [
        "check_config", "resolve_base", "start", "upload", "setup", "verify", "seal", "save", "destroy"]
    assert "sudo" not in "".join(ssh)

    env, recipe = _build_env(tmp_path / "keep", "windows", KEEP_SSH="yes")
    assert _build(env, recipe, "fake").returncode == 0
    ssh = _remote_commands(_calls(tmp_path / "keep"))
    assert any(l.endswith(" -File C:\\image-build\\seal_windows.ps1 -KeepSsh") for l in ssh), ssh


def test_linux_remote_commands(tmp_path):
    env, recipe = _build_env(tmp_path)
    assert _build(env, recipe, "fake").returncode == 0
    calls = _calls(tmp_path)
    ssh = _remote_commands(calls)
    assert any(l.endswith(" builder@192.0.2.10 sudo bash -c 'cd /tmp/image-build && bash '\\''./setup.sh'\\'''")
               for l in ssh), ssh
    assert any(l.endswith(" builder@192.0.2.10 sudo bash /tmp/image-build/seal_linux.sh no") for l in ssh), ssh
    scp = _remote_commands(calls, "scp")
    assert any(l.endswith(" builder@192.0.2.10:/tmp/image-build/payload.tgz") for l in scp), scp
    assert any(f"{IMAGE_BUILD / 'seal_linux.sh'} builder@192.0.2.10:/tmp/image-build/seal_linux.sh" in l
               for l in scp), scp
    env, recipe = _build_env(tmp_path / "keep", KEEP_SSH="yes")
    assert _build(env, recipe, "fake").returncode == 0
    assert any(l.endswith(" sudo bash /tmp/image-build/seal_linux.sh yes")
               for l in _remote_commands(_calls(tmp_path / "keep")))


def test_payload_exclude(tmp_path):
    env, recipe = _build_env(tmp_path, PAYLOAD_EXCLUDE="packer *.iso")
    result = _build(env, "--dry-run", recipe, "fake")
    assert result.returncode == 0, result.stderr
    tar_line = [l for l in result.stderr.splitlines() if l.startswith("DRY-RUN: tar ")][0]
    assert "--exclude=packer" in tar_line.split()
    assert "--exclude=*.iso" in tar_line.split()
    result = _build(env, recipe, "fake")
    assert result.returncode == 0, result.stderr
    with tarfile.open(tmp_path / "sink" / "payload.tgz") as tar:
        names = {n.removeprefix("./") for n in tar.getnames()}
    assert "setup.sh" in names and "verify.sh" in names
    assert not any(n.startswith("packer") or n.endswith(".iso") for n in names), names


def test_per_run_key_removed(tmp_path):
    for sub, fail_on in (("pass", None), ("fail", "verify.sh")):
        env, recipe = _build_env(tmp_path / sub)
        if fail_on:
            env["FAKE_SSH_FAIL_ON"] = fail_on
        result = _build(env, recipe, "fake")
        assert result.returncode == (1 if fail_on else 0), result.stderr
        keygen = [l for l in _calls(tmp_path / sub).splitlines() if l.startswith("ssh-keygen ")]
        assert len(keygen) == 1 and "-t ed25519" in keygen[0] and "-N " in keygen[0]
        key = Path(keygen[0].split(" -f ")[1].split()[0])
        assert key.is_relative_to(tmp_path / sub / "tmp")
        assert not key.parent.exists()
        assert list((tmp_path / sub / "tmp").iterdir()) == []


def test_reap_destroys_each_leftover(tmp_path):
    env, _recipe = _build_env(tmp_path)
    env["FAKE_LEFTOVERS"] = "vm-old-1 vm-old-2"
    result = _build(env, "--reap", "fake")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert "backend_destroy_id vm-old-1" in calls
    assert "backend_destroy_id vm-old-2" in calls
    assert "fake-cloud destroy vm-old-1" in calls
    assert "fake-cloud destroy vm-old-2" in calls
    assert "backend_start" not in calls


def test_reap_dry_run_destroys_nothing(tmp_path):
    env, _recipe = _build_env(tmp_path)
    env["FAKE_LEFTOVERS"] = "vm-old-1 vm-old-2"
    result = _build(env, "--reap", "fake", "--dry-run")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    # The leftover listing is read live; nothing is destroyed.
    assert "fake-cloud leftovers" in calls
    assert "fake-cloud destroy" not in calls
    assert "vm-old-1" in result.stdout and "vm-old-2" in result.stdout
    assert "DRY-RUN: fake-cloud destroy vm-old-1" in result.stderr
    env["FAKE_LEFTOVERS"] = ""
    result = _build(env, "--dry-run", "--reap", "fake")
    assert result.returncode == 0, result.stderr
    assert "nothing to reap" in result.stderr


def test_payload_uploaded_again_when_reboot_cleared_tmp(tmp_path):
    # Ubuntu empties /tmp at boot: a setup that rebooted took the payload with it.
    env, recipe = _build_env(tmp_path, SETUP_PASSES="2")
    env.update(FAKE_SSH_FAIL_ON="test -d /tmp/image-build", FAKE_SSH_FAIL_RC="1")
    result = _build(env, recipe, "fake")
    assert result.returncode == 0, result.stderr
    assert _events(_calls(tmp_path), "setup.sh", "verify.sh", "seal_linux.sh") == [
        "check_config", "resolve_base", "start", "upload", "setup", "upload", "setup",
        "upload", "verify", "seal", "save", "destroy"]
    # A probe that cannot connect is a failure, not a missing payload.
    env2, recipe2 = _build_env(tmp_path / "down", SETUP_PASSES="2")
    env2.update(FAKE_SSH_FAIL_ON="test -d /tmp/image-build", FAKE_SSH_FAIL_RC="255")
    result = _build(env2, recipe2, "fake")
    assert result.returncode == 1
    assert "save" not in _events(_calls(tmp_path / "down"), "setup.sh", "verify.sh", "seal_linux.sh")


def _remote_lines(tmp_path: Path) -> list:
    """Each remote command line exactly as ssh received it."""
    log = tmp_path / "calls.log.remote"
    return log.read_text().split("\0")[:-1] if log.exists() else []


def test_script_paths_with_spaces_linux(tmp_path):
    env, recipe = _build_env(tmp_path, SETUP="my steps/set up.sh", VERIFY="check it.sh")
    payload = recipe.parent / "payload"
    (payload / "my steps").mkdir()
    (payload / "my steps" / "set up.sh").write_text('echo setup > "$MARKER_DIR/setup ran"\n')
    (payload / "check it.sh").write_text('echo verify > "$MARKER_DIR/verify ran"\n')
    result = _build(env, recipe, "fake")
    assert result.returncode == 0, result.stderr
    remote = _remote_lines(tmp_path)
    commands = [c for c in remote if "set up.sh" in c] + [c for c in remote if "check it.sh" in c]
    assert len(commands) == 2, remote
    # Run each line as the VM's login shell would, against the local payload.
    markers = tmp_path / "markers"
    markers.mkdir()
    for command in commands:
        assert command.startswith("sudo bash -c "), command
        local = command.removeprefix("sudo ").replace("/tmp/image-build", str(payload))
        ran = subprocess.run(["sh", "-c", local], env={**os.environ, "MARKER_DIR": str(markers)},
                             capture_output=True, text=True, timeout=30)
        assert ran.returncode == 0, (command, ran.stderr)
    assert (markers / "setup ran").read_text() == "setup\n"
    assert (markers / "verify ran").read_text() == "verify\n"


def test_script_paths_with_spaces_windows(tmp_path):
    env, recipe = _build_env(tmp_path, "windows", SETUP="my steps/set up.ps1", VERIFY="check it.ps1")
    payload = recipe.parent / "payload"
    (payload / "my steps").mkdir()
    (payload / "my steps" / "set up.ps1").write_text("Write-Output setup\n")
    (payload / "check it.ps1").write_text("Write-Output verify\n")
    result = _build(env, recipe, "fake")
    assert result.returncode == 0, result.stderr
    remote = _remote_lines(tmp_path)
    assert ('powershell -NoProfile -ExecutionPolicy Bypass -File "C:\\image-build\\my steps\\set up.ps1"'
            in remote), remote
    assert ('powershell -NoProfile -ExecutionPolicy Bypass -File "C:\\image-build\\check it.ps1"'
            in remote), remote


def test_reap_refuses_mismatched_tag(tmp_path):
    env, _recipe = _build_env(tmp_path)
    env.update(FAKE_LEFTOVERS="vm-old-1 vm-stranger", FAKE_UNTAGGED="vm-stranger")
    result = _build(env, "--reap", "fake")
    assert result.returncode == 1
    calls = _calls(tmp_path)
    assert "fake-cloud tag vm-stranger" in calls
    assert "fake-cloud destroy vm-stranger" not in calls
    assert "fake-cloud destroy vm-old-1" in calls
    assert "vm-stranger" in result.stderr
    result = _build(env, "--reap", "fake", "--dry-run")
    assert result.returncode == 1
    assert "DRY-RUN: fake-cloud destroy vm-old-1" in result.stderr
    assert "DRY-RUN: fake-cloud destroy vm-stranger" not in result.stderr


def test_backend_midway_failure_not_masked(tmp_path):
    # A backend step that fails, followed by steps that succeed, must still fail
    # the call: the core never runs a backend function where set -e is ignored.
    def run(sub, fn, *args):
        env, recipe = _build_env(tmp_path / sub)
        env.update(FAKE_MIDFAIL=fn, FAKE_LEFTOVERS="vm-old-1")
        result = _build(env, *[recipe if a == "RECIPE" else a for a in args])
        return result, _calls(tmp_path / sub)

    result, calls = run("check", "backend_check_config", "--dry-run", "RECIPE", "fake")
    assert result.returncode == 2
    assert "backend_resolve_base" not in calls

    result, calls = run("resolve", "backend_resolve_base", "RECIPE", "fake")
    assert result.returncode == 1
    assert "backend_start" not in calls

    result, calls = run("start", "backend_start", "RECIPE", "fake")
    assert result.returncode == 1
    steps = _events(calls, "setup.sh", "verify.sh", "seal_linux.sh")
    assert "upload" not in steps and "save" not in steps
    assert steps[-1] == "destroy"

    result, calls = run("save", "backend_save", "RECIPE", "fake")
    assert result.returncode == 1
    assert "fake-cloud save" not in calls
    assert _events(calls, "setup.sh", "verify.sh", "seal_linux.sh")[-1] == "destroy"
    assert not _build_log(tmp_path / "save").exists()
    assert result.stdout == ""

    result, calls = run("leftovers", "backend_leftovers", "--reap", "fake")
    assert result.returncode == 1
    assert "backend_destroy_id" not in calls

    result, calls = run("destroy_id", "backend_destroy_id", "--reap", "fake")
    assert result.returncode == 1
    assert "fake-cloud destroy" not in calls


# --- first-boot SSH race -----------------------------------------------------
# A Windows clone's sshd can answer once in its first seconds after boot and
# then fail the next connection with exit 255 (measured on Proxmox clones of
# base-ws2022). The core waits until SSH answers several times in a row, and
# retries only the idempotent payload-upload steps, only on exit 255.

UPLOAD_STEPS = {
    "linux": (("ssh", "mkdir -p /tmp/image-build"),
              ("scp", ":/tmp/image-build/payload.tgz"),
              ("ssh", "tar -xzf /tmp/image-build/payload.tgz"),
              ("ssh", "rm -f /tmp/image-build/payload.tgz")),
    "windows": (("ssh", "New-Item -ItemType Directory -Force -Path C:\\image-build"),
                ("scp", ":C:/image-build/payload.tgz"),
                ("ssh", "tar.exe -xzf C:\\image-build\\payload.tgz"),
                ("ssh", "Remove-Item -Force C:\\image-build\\payload.tgz")),
}
SCRIPTS = {"linux": ("setup.sh", "verify.sh", "seal_linux.sh"),
           "windows": ("setup.ps1", "verify.ps1", "seal_windows.ps1")}
RETRY_LINE = "(exit 255); retrying"


def _fail(env: dict, tool: str, marker: str, rc: int, times=None) -> None:
    prefix = {"ssh": "FAKE_SSH", "scp": "FAKE_SCP"}[tool]
    env.update({f"{prefix}_FAIL_ON": marker, f"{prefix}_FAIL_RC": str(rc)})
    if times is not None:
        env[f"{prefix}_FAIL_TIMES"] = str(times)


def _matching(calls: str, tool: str, marker: str) -> list:
    return [l for l in _remote_commands(calls, tool) if marker in l]


def test_upload_step_retried_on_255(tmp_path):
    for os_family, steps in UPLOAD_STEPS.items():
        setup, verify, seal = SCRIPTS[os_family]
        for i, (tool, marker) in enumerate(steps):
            sub = tmp_path / f"{os_family}-{i}"
            env, recipe = _build_env(sub, os_family)
            _fail(env, tool, marker, 255, times=2)
            result = _build(env, recipe, "fake")
            assert result.returncode == 0, (marker, result.stderr)
            calls = _calls(sub)
            tries = _matching(calls, tool, marker)
            assert len(tries) == 3, (marker, tries)
            assert result.stderr.count(RETRY_LINE) == 2, result.stderr
            events = _events(calls, setup, verify, seal)
            assert [e for e in events if e != "upload"] == [
                "check_config", "resolve_base", "start", "setup", "verify", "seal", "save", "destroy"], events
            assert events.index("upload") < events.index("setup"), events
            if "tar" in marker:
                # A retried extract must find its archive again: it never
                # deletes the archive itself.
                assert all("rm -f" not in t and "Remove-Item" not in t for t in tries), tries


def test_upload_gives_up_after_deadline(tmp_path):
    for os_family, steps in UPLOAD_STEPS.items():
        setup, verify, seal = SCRIPTS[os_family]
        for i, (tool, marker) in enumerate(steps[:2]):
            sub = tmp_path / f"{os_family}-{i}"
            env, recipe = _build_env(sub, os_family)
            _fast_sleep(sub)
            _fail(env, tool, marker, 255)
            env["IMAGE_BUILD_SSH_WAIT_SECONDS"] = "2"
            started = time.monotonic()
            result = _build(env, recipe, "fake")
            assert result.returncode == 1, result.stderr
            assert time.monotonic() - started < 30
            assert "giving up" in result.stderr, result.stderr
            calls = _calls(sub)
            assert len(_matching(calls, tool, marker)) >= 2
            events = _events(calls, setup, verify, seal)
            assert "setup" not in events and "save" not in events, events
            assert events[-1] == "destroy", events


def test_upload_failure_other_than_255_not_retried(tmp_path):
    for os_family, steps in UPLOAD_STEPS.items():
        setup, verify, seal = SCRIPTS[os_family]
        for i, (tool, marker) in enumerate(steps):
            sub = tmp_path / f"{os_family}-{i}"
            env, recipe = _build_env(sub, os_family)
            # It would succeed if tried again.
            _fail(env, tool, marker, 1, times=1)
            result = _build(env, recipe, "fake")
            assert result.returncode == 1, result.stderr
            calls = _calls(sub)
            assert len(_matching(calls, tool, marker)) == 1, marker
            assert RETRY_LINE not in result.stderr
            assert "payload upload: " in result.stderr and "(exit 1)" in result.stderr, result.stderr
            events = _events(calls, setup, verify, seal)
            assert "setup" not in events and "save" not in events, events
            assert events[-1] == "destroy", events


def test_setup_verify_seal_never_retried(tmp_path):
    # The upload hits the race (and is retried once); a later exit 255 is not
    # retried. Setup, verify and seal are not idempotent.
    for os_family, steps in UPLOAD_STEPS.items():
        setup, verify, seal = SCRIPTS[os_family]
        (dir_tool, dir_marker), (scp_tool, scp_marker) = steps[0], steps[1]
        cases = [(f"ssh-{n}", ("ssh", s), (scp_tool, scp_marker))
                 for n, s in zip(("setup", "verify", "seal"), (setup, verify, seal))]
        # The copy of the seal script into the VM is part of the seal.
        cases.append(("scp-seal", ("scp", seal), (dir_tool, dir_marker)))
        for name, (tool, marker), (race_tool, race_marker) in cases:
            # No marker in the directory name: the key path is in every ssh line.
            sub = tmp_path / f"{os_family}-{name}"
            env, recipe = _build_env(sub, os_family)
            _fail(env, race_tool, race_marker, 255, times=1)
            _fail(env, tool, marker, 255)
            env["IMAGE_BUILD_SSH_WAIT_SECONDS"] = "2"
            result = _build(env, recipe, "fake")
            assert result.returncode == 1, (name, result.stderr)
            calls = _calls(sub)
            assert len(_matching(calls, race_tool, race_marker)) == 2, (name, calls)
            assert len(_matching(calls, tool, marker)) == 1, (name, _matching(calls, tool, marker))
            assert result.stderr.count(RETRY_LINE) == 1, (name, result.stderr)
            events = _events(calls, setup, verify, seal)
            assert "save" not in events and events[-1] == "destroy", (name, events)


def test_wait_ssh_needs_consecutive_successes(tmp_path):
    for os_family, steps in UPLOAD_STEPS.items():
        sub = tmp_path / os_family
        env, recipe = _build_env(sub, os_family)
        # Answers, drops, answers twice, drops, then stays up.
        env["FAKE_SSH_PROBES"] = "0 255 0 0 255 0 0 0"
        result = _build(env, recipe, "fake")
        assert result.returncode == 0, result.stderr
        remote = _remote_lines(sub)
        first_upload = next(i for i, c in enumerate(remote) if steps[0][1] in c)
        # The upload starts only after three answers in a row.
        assert remote[:first_upload] == ["exit 0"] * 8, remote
        assert "answered 3 times in a row" in result.stderr, result.stderr

    # Never three in a row: the build stops when the wait runs out.
    sub = tmp_path / "unstable"
    env, recipe = _build_env(sub, "windows")
    _fast_sleep(sub)
    env.update(FAKE_SSH_PROBES="0 0 255", IMAGE_BUILD_SSH_WAIT_SECONDS="2")
    result = _build(env, recipe, "fake")
    assert result.returncode == 1, result.stderr
    assert "no stable SSH" in result.stderr, result.stderr
    events = _events(_calls(sub), *SCRIPTS["windows"])
    assert "upload" not in events and "setup" not in events, events
    assert events[-1] == "destroy", events


def test_new_boot_needs_stable_ssh_before_next_pass(tmp_path):
    # After a reboot the VM answers with its new boot ID, then drops one
    # connection: the next pass starts only after three answers in a row.
    for os_family in ("linux", "windows"):
        setup, verify, _seal = SCRIPTS[os_family]
        sub = tmp_path / os_family
        env, recipe = _build_env(sub, os_family, SETUP_PASSES="2")
        _fail(env, "ssh", setup, 100, times=1)
        # Three for the first wait; then ok, fail, ok, ok, ok after the reboot.
        env["FAKE_SSH_PROBES"] = "0 0 0 0 255 0 0 0"
        result = _build(env, recipe, "fake")
        assert result.returncode == 0, result.stderr
        timeline = _timeline(sub)
        commands = [c for _b, c in timeline]
        setups = [i for i, c in enumerate(commands) if setup in c]
        assert len(setups) == 2, commands
        new_boot = next(i for i, (b, c) in enumerate(timeline) if b == "boot-2" and _is_boot_read(c))
        probes = [c for c in commands[new_boot + 1:setups[1]] if c == "exit 0"]
        assert probes == ["exit 0"] * 5, commands
        assert timeline[setups[1]][0] == "boot-2", timeline
        tail = result.stderr[result.stderr.index("the VM rebooted"):]
        assert "answered 1 time(s), then failed; counting again" in tail, result.stderr
        assert "answered 3 times in a row" in tail, result.stderr


def test_upload_retry_stops_at_build_expiry(tmp_path):
    # IMAGE_BUILD_SSH_WAIT_SECONDS outlasts the build's TTL: the retries stop
    # at the expiry, and no attempt starts after it.
    for os_family in ("linux", "windows"):
        setup, verify, seal = SCRIPTS[os_family]
        sub = tmp_path / os_family
        env, recipe = _build_env(sub, os_family)
        bin_dir = sub / "bin"
        # The clock reads 55 s into a minute, so a 1-minute TTL expires in 5 s.
        now = int(time.time())
        _tool(bin_dir, "date", 'if [ "$*" = "-u +%s" ]; then echo "$FAKE_NOW"; exit 0; fi\n'
                               'PATH=/usr/bin:/bin exec date "$@"\n')
        # Sleeps shorter than the 10 s poll (a clamped one) are real.
        _tool(bin_dir, "sleep", LOG_CALL + 'case "$1" in [0-9]) PATH=/usr/bin:/bin exec sleep "$1" ;; esac\n'
                                           'PATH=/usr/bin:/bin exec sleep 0.01\n')
        tool, marker = UPLOAD_STEPS[os_family][1]
        _fail(env, tool, marker, 255)
        env.update(FAKE_NOW=str(now - now % 60 + 55), IMAGE_BUILD_TTL_MINUTES="1",
                   IMAGE_BUILD_SSH_WAIT_SECONDS="30")
        started = time.monotonic()
        result = _build(env, recipe, "fake")
        assert result.returncode == 1, result.stderr
        assert time.monotonic() - started < 15, result.stderr
        assert "stopped at the build's expiry" in result.stderr, result.stderr
        assert "IMAGE_BUILD_TTL_MINUTES=1" in result.stderr, result.stderr
        calls = _calls(sub).splitlines()
        tries = [i for i, l in enumerate(calls) if l.startswith("scp ") and marker in l]
        assert len(tries) == 1, calls
        # The attempt's connect timeout and the sleep after it fit in what was left.
        timeout = int(re.search(r"ConnectTimeout=(\d+)", calls[tries[0]]).group(1))
        assert 1 <= timeout <= 5, calls[tries[0]]
        sleeps = [i for i, l in enumerate(calls) if l.startswith("sleep ")]
        assert int(calls[sleeps[-1]].split()[1]) <= 5, calls
        assert tries[-1] < sleeps[-1], calls
        events = _events("\n".join(calls), setup, verify, seal)
        assert "setup" not in events and events[-1] == "destroy", events
