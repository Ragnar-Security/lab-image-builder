"""proxmox_base_rocky.sh, the Rocky Linux 9 base template. It runs on the same
engine as proxmox_base_ubuntu.sh (proxmox_base_lib.sh), against the same fake
build node; these tests hold it to the same cleanup guarantees and to the
parts of the base contract that differ on a RHEL-family guest."""

import subprocess
from pathlib import Path

from tests.test_image_build import IMAGE_BUILD
from tests.test_image_build_proxmox_base import DATE, _cmds, _env, _marker, _state

SCRIPT = IMAGE_BUILD / "proxmox_base_rocky.sh"
NAME = f"base-rocky-9-{DATE}"


def _run(tmp_path: Path, *extra, image=True, **env_extra):
    args = ["bash", str(SCRIPT), "--snippets", "snipstore", "--date", DATE, *extra]
    if image:
        args[2:2] = ["--image", str(tmp_path / "noble.img")]
    return subprocess.run(args, env=_env(tmp_path, None, **env_extra), capture_output=True,
                          text=True, timeout=120, stdin=subprocess.DEVNULL)


def _snippet(tmp_path: Path) -> Path:
    return tmp_path / "snippets" / f"{NAME}-user.yaml"


def test_dry_run_prints_the_rocky_first_boot_and_finalize(tmp_path):
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    out = result.stderr
    assert f"VM 8000 '{NAME}'" in out, out
    assert _state(tmp_path) == {} and not _snippet(tmp_path).exists()
    # The user: builder only, passwordless sudo, no key, no password.
    assert "  - name: builder" in out and 'sudo: "ALL=(ALL) NOPASSWD:ALL"' in out
    assert "lock_passwd: true" in out and "ssh_authorized_keys" not in out
    # RHEL's sshd unit is sshd.service.
    assert "[systemctl, enable, sshd.service]" in out
    assert "systemctl is-enabled sshd.service" in out
    # The builder's key injection and this script's finalize both use
    # guest-exec, which Rocky's qemu-ga allow-list leaves out: a second
    # environment file empties the filter, and SELinux lets the agent act.
    assert "EnvironmentFile=/etc/qemu-ga/image-build.env" in out
    assert "FILTER_RPC_ARGS=--block-rpcs=" in out
    assert "[semanage, permissive, -a, virt_qemu_ga_t]" in out
    # DHCP on every en*/eth* interface from one profile, by name, not MAC.
    assert "multi-connect=3" in out and "interface-name=en*;eth*;" in out
    assert "dhcp-client-id=mac" in out
    # A clone has no datasource: cloud-init stays off after the first boot.
    assert "touch /etc/cloud/cloud-init.disabled" in out
    # The key the builder injects lands in a directory sshd may read.
    assert "restorecon -R" in out
    # Nothing of the Ubuntu base leaks in.
    for word in ("apt-get", "dpkg-query", "netplan", "ssh.socket"):
        assert word not in out, word


def test_apply_builds_template_and_removes_snippet(tmp_path):
    result = _run(tmp_path, "--apply")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "8000\n"
    g = _state(tmp_path)["8000"]
    assert g["template"] == 1 and g["name"] == NAME
    marker = _marker(tmp_path)
    assert marker not in g["description"] and "under construction" not in g["description"]
    assert g["description"].startswith(f"{NAME}: Rocky Linux 9 cloud image noble.img sha256 "), g
    assert "proxmox_base_rocky.sh" in g["description"]
    assert not {"ide2", "cicustom", "ipconfig0"} & set(g["config"]), g["config"]
    assert not _snippet(tmp_path).exists()


def test_default_image_is_the_rocky_9_generic_cloud_image(tmp_path):
    result = _run(tmp_path, image=False)
    assert result.returncode == 1, result.stderr
    assert ("/var/lib/vz/template/iso/Rocky-9-GenericCloud-Base.latest.x86_64.qcow2 not found"
            in result.stderr), result.stderr


def test_template_cpu_type_runs_rhel9(tmp_path):
    # RHEL 9 is built for x86-64-v2; on qm's default CPU (kvm64) its glibc
    # stops init ("Fatal glibc error: CPU does not support x86-64-v2", a
    # kernel panic), so the guest agent never answers. Clones inherit the type.
    result = _run(tmp_path, "--apply")
    assert result.returncode == 0, result.stderr
    create = [c for c in _cmds(tmp_path) if c.startswith("qm create ")]
    assert len(create) == 1 and " --cpu x86-64-v2-AES " in create[0] + " ", create
    assert _state(tmp_path)["8000"]["config"]["cpu"] == "x86-64-v2-AES"


def test_failure_midway_destroys_only_this_runs_vm(tmp_path):
    result = _run(tmp_path, "--apply", FAKE_QM_FAIL_ON="resize")
    assert result.returncode != 0
    assert "8000" not in _state(tmp_path), result.stderr
    assert [c for c in _cmds(tmp_path) if c.startswith("qm destroy")] == ["qm destroy 8000 --purge 1"]
    assert _marker(tmp_path) in result.stderr
    assert not _snippet(tmp_path).exists()


def test_release_is_an_ubuntu_option_only(tmp_path):
    result = _run(tmp_path, "--release", "2404")
    assert result.returncode == 2
    assert "unknown option --release" in result.stderr
