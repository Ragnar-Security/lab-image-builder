"""proxmox_base_windows.sh, which builds the Proxmox Windows base template. The
dry run reaches the node through the fake `ssh` of test_image_build_proxmox_base
(a real `sh` with fake node tools on PATH), so every check the script makes
before it changes anything runs as it would on the node.

What these tests hold the script to: `--os` selects the installation image,
the virtio-win driver directory and the template name; `--os ws2022` (and no
`--os`) prints exactly what the script printed before `--os` existed; an
unknown `--os` is a usage error. The guest-side PowerShell is tested in
test_image_build_powershell."""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.test_image_build import IMAGE_BUILD, ROOT
from tests.test_image_build_proxmox_base import FAKE_PVE, HOSTS, NODE_TOOLS, PVE_CONFIG

SCRIPT = IMAGE_BUILD / "proxmox_base_windows.sh"
DATE = "20261113"
GOLDEN_WS2022 = ROOT / "tests" / "fixtures" / "image_build" / "proxmox-base-windows-ws2022-dry-run.txt"
# The ISOs a dry run hashes; their content is fixed, so are their hashes.
ISOS = ("Windows_Server_2022_EVAL.iso", "Windows_Server_2019_EVAL.iso",
        "Windows_Server_2016_EVAL.iso", "virtio-win-0.1.302.iso")


def _env(tmp_path: Path) -> dict:
    bin_dir = tmp_path / "bin"
    node_bin = bin_dir / "node"
    node_bin.mkdir(parents=True)
    fake = bin_dir / "ssh"
    fake.write_text(FAKE_PVE)
    fake.chmod(0o755)
    # genisoimage is only looked up (`command -v`) in a dry run.
    for tool in NODE_TOOLS + ("genisoimage",):
        (node_bin / tool).symlink_to(fake)
    isos = tmp_path / "isos"
    isos.mkdir()
    for name in ISOS:
        (isos / name).write_bytes(name.encode())
    (tmp_path / "pve-state.json").write_text(json.dumps({"guests": {}}))
    (tmp_path / "config.env").write_text(PVE_CONFIG)
    env = dict(os.environ)
    env.update(
        PATH=f"{bin_dir}:{env['PATH']}",
        CALL_LOG=str(tmp_path / "calls.log"),
        PVE_STATE=str(tmp_path / "pve-state.json"),
        PVE_LOG=str(tmp_path / "pve.log"),
        FAKE_PVE_HOSTS=json.dumps(HOSTS),
        FAKE_PVE_NODEBIN=str(node_bin),
        # pvesm path <store>:iso/<file> resolves to isos/<file>.
        FAKE_SNIPPET_DIR=str(isos),
        IMAGE_BUILD_CONFIG=str(tmp_path / "config.env"),
    )
    return env


def _dry_run(tmp_path: Path, *extra):
    return subprocess.run(["bash", str(SCRIPT), "--date", DATE, *extra], env=_env(tmp_path),
                          capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)


def _normalized(tmp_path: Path, stderr: str) -> str:
    """The dry run's output without what differs between runs: the temp
    directory, the run's marker (time and random), the staging PID and
    today's date in the template description."""
    out = stderr.replace(str(tmp_path), "<TMP>")
    out = re.sub(r"proxmox-base-build=[0-9]{14}-[0-9a-f]{8}", "proxmox-base-build=<MARKER>", out)
    out = re.sub(r" built [0-9]{4}-[0-9]{2}-[0-9]{2} by ", " built <TODAY> by ", out)
    return re.sub(r"(-answer)\.[0-9]+\b", r"\1.<PID>", out)


def _changes(tmp_path: Path) -> list:
    log = tmp_path / "pve.log"
    cmds = log.read_text().splitlines() if log.exists() else []
    return [c for c in cmds if c.startswith(("qm create", "qm set", "qm destroy", "qm template",
                                             "qm start", "rm ", "genisoimage"))]


# --- --os in a dry run ------------------------------------------------------------


@pytest.mark.parametrize("args", [(), ("--os", "ws2022")], ids=["default", "ws2022"])
def test_ws2022_dry_run_is_unchanged(tmp_path, args):
    result = _dry_run(tmp_path, *args)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert _normalized(tmp_path, result.stderr) == GOLDEN_WS2022.read_text()
    assert _changes(tmp_path) == []


@pytest.mark.parametrize("os_id, year, drivers, ostype", [
    ("ws2016", "2016", "2k16", "win10"),
    ("ws2019", "2019", "2k19", "win10"),
])
def test_older_server_dry_run(tmp_path, os_id, year, drivers, ostype):
    result = _dry_run(tmp_path, "--os", os_id)
    assert result.returncode == 0, result.stderr
    out = result.stderr
    name = f"base-{os_id}-{DATE}"
    assert f"VM 8000 '{name}' on pve" in out, out
    assert f"would run: qm create 8000 --name {name} --ostype {ostype} " in out, out
    assert f"install:   local:iso/Windows_Server_{year}_EVAL.iso (sha256 " in out, out
    assert f"<Value>Windows Server {year} SERVERSTANDARD</Value>" in out, out
    # The answer file loads the storage and network drivers for this release only.
    paths = re.findall(r"<Path>([D-I]):\\(vioscsi|NetKVM)\\([^\\]+)\\amd64</Path>", out)
    assert len(paths) == 12, paths
    assert {p[2] for p in paths} == {drivers}, paths
    assert "2k22" not in out, out
    assert f"Windows Server {year} Standard Evaluation (Desktop Experience)" in out, out
    assert "would run: rm -f " in out and f"{name}-answer.iso" in out, out
    assert _changes(tmp_path) == []


def test_ws2016_ships_the_pinned_openssh_installer(tmp_path):
    # Server 2016 has no OpenSSH.Server capability; the run's ISO carries the
    # installer for the pinned release. Server 2019 and 2022 do not need it.
    out = _dry_run(tmp_path / "2016", "--os", "ws2016").stderr
    assert re.search(r"would write \S+ from \S+: autounattend.xml .* install-openssh.ps1\n", out), out
    for os_id in ("ws2019", "ws2022"):
        out = _dry_run(tmp_path / os_id, "--os", os_id).stderr
        assert "install-openssh.ps1" not in out, out


@pytest.mark.parametrize("bad", ["ws2003", "WS2016", "2016", "ws2016 ", ""])
def test_unknown_os_is_a_usage_error(tmp_path, bad):
    result = _dry_run(tmp_path, "--os", bad)
    assert result.returncode == 2, result.stderr
    assert "--os must be one of ws2022, ws2019, ws2016" in result.stderr, result.stderr
    assert _changes(tmp_path) == []
    assert not (tmp_path / "pve.log").exists(), "the node was contacted before the usage check"


def test_help_names_os(tmp_path):
    result = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    assert "--os ws2022|ws2019|ws2016" in result.stdout, result.stdout
    # The help is the script's whole header comment, and nothing after it.
    assert "set -euo pipefail" not in result.stdout
    assert result.stdout.rstrip().endswith("<vmid+500>."), result.stdout
