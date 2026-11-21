"""The guest-side PowerShell the builder ships to Windows: expand_payload.ps1
(the payload extract on a Windows without tar.exe) and the Windows base's
install-openssh.ps1 (OpenSSH Server on Windows Server 2016, which has no
OpenSSH capability). They run for real, in Windows PowerShell 5.1 where the
host has it (the guest's own) and otherwise in PowerShell 7; a host with
neither skips them. This module imports nothing POSIX-only, so it runs on a
Windows workstation too:

    python -m pytest tests/test_image_build_powershell.py"""

import gzip
import hashlib
import http.server
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
IMAGE_BUILD = ROOT / "scripts" / "image_build"
EXPAND_PAYLOAD = IMAGE_BUILD / "expand_payload.ps1"
INSTALL_OPENSSH = IMAGE_BUILD / "windows_base" / "install-openssh.ps1"
PREPARE = IMAGE_BUILD / "windows_base" / "prepare.ps1"


def _powershell() -> list:
    """Windows PowerShell 5.1 (what the guest runs) when present, else
    PowerShell 7; skips the test on a host with neither."""
    path = shutil.which("powershell.exe")
    if path:
        return [path, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
    path = shutil.which("pwsh")
    if path:
        return [path, "-NoProfile", "-NonInteractive"]
    pytest.skip("no PowerShell on this host")


# --- expand_payload.ps1: the payload extract where Windows has no tar.exe -----
# Windows Server 2016 has no tar.exe. The core then extracts payload.tgz with
# expand_payload.ps1 (.NET only). It must reproduce the payload tree exactly,
# for archives as GNU tar and bsdtar write them, and refuse anything a payload
# never holds instead of writing it.
def _payload_tree(root: Path, long_name: bool = True) -> None:
    """Everything a lab payload holds that an extractor can get wrong."""
    root.mkdir()
    (root / "setup.ps1").write_text("Write-Output setup\r\n")
    (root / "name with spaces").mkdir()
    (root / "name with spaces" / "a file with spaces.txt").write_text("spaces\n")
    # A path longer than ustar's 100-byte name field (GNU 'L', pax path or the
    # ustar prefix), and one file name longer than 100 bytes on its own.
    deep = root.joinpath(*("level%02d-directory" % i for i in range(8)))
    deep.mkdir(parents=True)
    (deep / "deep file.txt").write_text("deep\n")
    if long_name:   # ustar has no way to store a name over 100 bytes
        (root / ("long-name-" + "x" * 100 + ".txt")).write_text("long\n")
    (root / "unicodé ü.txt").write_text("non-ascii name\n", encoding="utf-8")
    (root / "empty.txt").write_bytes(b"")
    (root / "empty dir").mkdir()
    (root / "data").mkdir()
    (root / "data" / "random.bin").write_bytes(os.urandom(3 * 1024 * 1024 + 7))
    # Exactly one block, and one byte over: the padding arithmetic.
    (root / "data" / "block.bin").write_bytes(b"b" * 512)
    (root / "data" / "block1.bin").write_bytes(b"c" * 513)


def _manifest(root: Path) -> dict:
    """relative path -> SHA-256 for files, 'dir' for directories."""
    out = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        out[rel] = "dir" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _python_tgz(src: Path, out: Path, fmt) -> None:
    with tarfile.open(out, "w:gz", format=fmt) as tar:
        tar.add(src, arcname=".")


def _expand(archive: Path, dest: Path):
    dest.mkdir(exist_ok=True)
    return subprocess.run(_powershell() + ["-File", str(EXPAND_PAYLOAD), "-Archive", str(archive),
                                           "-Destination", str(dest)],
                          capture_output=True, text=True, timeout=300)


@pytest.mark.parametrize("fmt", ["tar", "gnu", "pax", "ustar"])
def test_expand_payload_reproduces_the_payload(tmp_path, fmt):
    src = tmp_path / "payload"
    _payload_tree(src, long_name=fmt != "ustar")
    archive = tmp_path / "payload.tgz"
    if fmt == "tar":
        # As the core makes it: tar -C <dir> -czf payload.tgz . on this host.
        if os.name == "nt":
            pytest.skip("the core's own tar runs on the build host (Linux or macOS)")
        subprocess.run(["tar", "-C", str(src), "-czf", str(archive), "."], check=True)
    else:
        _python_tgz(src, archive, {"gnu": tarfile.GNU_FORMAT, "pax": tarfile.PAX_FORMAT,
                                   "ustar": tarfile.USTAR_FORMAT}[fmt])
    dest = tmp_path / "out"
    result = _expand(archive, dest)
    assert result.returncode == 0, result.stdout + result.stderr
    want = _manifest(src)
    assert _manifest(dest) == want
    files = sum(1 for v in want.values() if v != "dir")
    assert f"expand_payload.ps1: {files} files, " in result.stdout, result.stdout
    # A retried extract (the upload retries the step) overwrites in place.
    result = _expand(archive, dest)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _manifest(dest) == want


def _tgz_with(tmp_path: Path, *members) -> Path:
    """An archive holding a regular file 'ok.txt' and then the given members."""
    archive = tmp_path / "bad.tgz"
    with tarfile.open(archive, "w:gz", format=tarfile.GNU_FORMAT) as tar:
        data = b"fine\n"
        info = tarfile.TarInfo("./ok.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return archive


def _member(name, kind=tarfile.REGTYPE, data=b"x", linkname=""):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    return info, (data if kind == tarfile.REGTYPE else None)


@pytest.mark.parametrize("member, why", [
    (_member("./link", tarfile.SYMTYPE, linkname="/etc/passwd"), "symbolic link"),
    (_member("./hard", tarfile.LNKTYPE, linkname="./ok.txt"), "hard link"),
    (_member("./fifo", tarfile.FIFOTYPE), "type"),
    (_member("../escape.txt"), "leaves the destination"),
    (_member("./sub/../../escape.txt"), "leaves the destination"),
    (_member("/abs.txt"), "absolute"),
    (_member("./c:/drive.txt"), "not a valid Windows"),
    (_member("./back\\slash.txt"), "not a valid Windows"),
], ids=["symlink", "hardlink", "fifo", "dotdot", "dotdot-inner", "absolute", "drive", "backslash"])
def test_expand_payload_refuses_what_a_payload_never_holds(tmp_path, member, why):
    archive = _tgz_with(tmp_path, member)
    dest = tmp_path / "out" / "image-build"
    dest.parent.mkdir()
    result = _expand(archive, dest)
    assert result.returncode != 0, result.stdout + result.stderr
    assert why in result.stdout + result.stderr, result.stdout + result.stderr
    # Nothing escaped the destination.
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["image-build"]
    assert not (tmp_path / "escape.txt").exists() and not (tmp_path / "abs.txt").exists()


def test_expand_payload_refuses_a_damaged_archive(tmp_path):
    src = tmp_path / "payload"
    _payload_tree(src)
    good = tmp_path / "good.tgz"
    _python_tgz(src, good, tarfile.GNU_FORMAT)
    raw = bytearray(gzip.decompress(good.read_bytes()))
    # A header whose checksum no longer adds up.
    bad = bytearray(raw)
    bad[0] ^= 0x01
    (tmp_path / "checksum.tgz").write_bytes(gzip.compress(bytes(bad)))
    result = _expand(tmp_path / "checksum.tgz", tmp_path / "out1")
    assert result.returncode != 0 and "checksum" in result.stdout + result.stderr, result.stdout + result.stderr
    # An archive cut short inside a member's data.
    (tmp_path / "short.tgz").write_bytes(gzip.compress(bytes(raw[:len(raw) // 2])))
    result = _expand(tmp_path / "short.tgz", tmp_path / "out2")
    assert result.returncode != 0 and "ends early" in result.stdout + result.stderr, result.stdout + result.stderr


def _damaged_gzip(raw: bytes, how: str) -> bytes:
    data = bytearray(raw)
    if how == "truncated-tail":     # the trailer cut off
        return bytes(data[:-6])
    if how == "truncated-half":     # cut inside the compressed data
        return bytes(data[:len(data) // 2])
    if how == "crc-byte":           # one bit of the trailer's CRC-32
        data[-7] ^= 0x01
    elif how == "isize-byte":       # one bit of the trailer's length
        data[-2] ^= 0x01
    elif how == "body-byte":        # one bit in the middle of the compressed data
        data[len(data) // 2] ^= 0x01
    return bytes(data)


@pytest.mark.parametrize("how", ["truncated-tail", "truncated-half", "crc-byte", "isize-byte", "body-byte"])
def test_expand_payload_refuses_a_damaged_gzip_stream_and_writes_nothing(tmp_path, how):
    # The tar stream inside is intact up to its end-of-archive blocks; only the
    # gzip layer is damaged. Nothing may be extracted.
    src = tmp_path / "payload"
    _payload_tree(src)
    good = tmp_path / "good.tgz"
    _python_tgz(src, good, tarfile.GNU_FORMAT)
    bad = tmp_path / "bad.tgz"
    bad.write_bytes(_damaged_gzip(good.read_bytes(), how))
    dest = tmp_path / "out"
    result = _expand(bad, dest)
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "is not an intact gzip archive" in out, out
    assert list(dest.iterdir()) == [], out
    # The intact archive still extracts.
    assert _expand(good, tmp_path / "ok").returncode == 0


# --- install-openssh.ps1 (Windows Server 2016) -------------------------------------


class _Server:
    """A local HTTP server for one directory, standing in for the release
    host: the script's download, hash check and refusal run for real."""

    def __init__(self, root: Path):
        handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(root), **k)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = "http://127.0.0.1:%d/" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def _fetch(tmp_path: Path, url: str, sha: str):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    result = subprocess.run(_powershell() + ["-File", str(INSTALL_OPENSSH), "-Url", url, "-Sha256", sha,
                                             "-WorkDir", str(work), "-DownloadOnly"],
                            capture_output=True, text=True, timeout=120)
    return result, work


def test_openssh_artifact_with_the_pinned_hash_is_accepted(tmp_path):
    served = tmp_path / "srv"
    served.mkdir()
    payload = os.urandom(256 * 1024)
    (served / "OpenSSH-Win64.msi").write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    with _Server(served) as srv:
        result, work = _fetch(tmp_path, srv.url + "OpenSSH-Win64.msi", sha.upper())
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"sha256 {sha} matches the pin" in result.stdout, result.stdout
    # -DownloadOnly leaves nothing behind.
    assert list(work.iterdir()) == []


def test_openssh_artifact_with_another_hash_is_refused(tmp_path):
    served = tmp_path / "srv"
    served.mkdir()
    (served / "OpenSSH-Win64.msi").write_bytes(b"not the pinned release")
    with _Server(served) as srv:
        result, work = _fetch(tmp_path, srv.url + "OpenSSH-Win64.msi", "0" * 64)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "does not match the pin" in result.stdout + result.stderr, result.stdout + result.stderr
    assert "matches the pin" not in result.stdout.replace("does not match the pin", "")
    # The refused file is not left for anything to install.
    assert list(work.iterdir()) == []


def test_openssh_artifact_that_cannot_be_fetched_is_refused(tmp_path):
    served = tmp_path / "srv"
    served.mkdir()
    with _Server(served) as srv:
        result, work = _fetch(tmp_path, srv.url + "missing.msi", "0" * 64)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "download of " in result.stdout + result.stderr, result.stdout + result.stderr
    assert list(work.iterdir()) == []


def test_openssh_pin_is_one_release_with_a_full_hash():
    text = INSTALL_OPENSSH.read_text()
    urls = re.findall(r"\$Url = '([^']+)'", text)
    shas = re.findall(r"\$Sha256 = '([^']+)'", text)
    assert urls == ["https://github.com/PowerShell/Win32-OpenSSH/releases/download/"
                    "10.0.0.0p2-Preview/OpenSSH-Win64-v10.0.0.0.msi"], urls
    assert len(shas) == 1 and re.fullmatch(r"[0-9a-f]{64}", shas[0]), shas
    # prepare.ps1 uses the installer only where the capability does not exist.
    prepare = PREPARE.read_text()
    assert "install-openssh.ps1" in prepare
    assert "OpenSSH.Server~~~~0.0.1.0" in prepare


@pytest.mark.skipif(os.environ.get("IMAGE_BUILD_NETWORK_TESTS") != "1",
                    reason="fetches the pinned release from GitHub; set IMAGE_BUILD_NETWORK_TESTS=1")
def test_openssh_pin_matches_the_published_release(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    result = subprocess.run(_powershell() + ["-File", str(INSTALL_OPENSSH), "-WorkDir", str(work),
                                             "-DownloadOnly"],
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "matches the pin" in result.stdout, result.stdout


# --- finalize.ps1 (the Windows base's last step before sysprep) -------------------
# Its app cleanup and its sshd stop run on output captured from real Windows
# machines (tests/fixtures/image_build/windows/, each file says when, where and
# how it was captured), replayed into the production functions:
#   - appx-ws2016-14393.json: the Server 2016 base build VM, after `prepare`
#     (its Administrator had logged on once), as SYSTEM, as finalize sees it;
#   - appx-ws2022-20348.json: a fresh clone of the updated Server 2022 base;
#   - sshd-ws2016-14393.json: sshd on the Server 2016 build VM through the
#     post-cleanup reboot and finalize, and on a clone of the new base: the
#     statuses after a start and a stop, Stop-Service on a starting service,
#     and the exception WaitForStatus throws on a timeout.
# Get-AppxPackage, Get-Service and the rest become functions that return the
# captured values (PowerShell resolves a function before a cmdlet of the same
# name); Remove-AppxPackage is generated with exactly the captured parameter
# set, so PowerShell's own binder accepts or refuses -AllUsers as Windows did.
# finalize.ps1 is dot-sourced with -FunctionsOnly, which returns before any
# step; the guard below refuses to load one that would run its steps here.

FINALIZE = IMAGE_BUILD / "windows_base" / "finalize.ps1"
CAPTURED = ROOT / "tests" / "fixtures" / "image_build" / "windows"


def _finalize(tmp_path: Path, body: str):
    text = FINALIZE.read_text()
    guard = text.find("if ($FunctionsOnly) { return }")
    assert "param([switch]$FunctionsOnly)" in text and 0 < guard < text.find("\nStep '"), \
        "finalize.ps1 must return under -FunctionsOnly before its first step"
    tmp_path.mkdir(parents=True, exist_ok=True)
    script = tmp_path / "harness.ps1"
    script.write_text("$ErrorActionPreference = 'Stop'\n"
                      f". '{FINALIZE}' -FunctionsOnly\n" + body, encoding="utf-8")
    result = subprocess.run(_powershell() + ["-File", str(script)], capture_output=True, text=True, timeout=120)
    return result, result.stdout + result.stderr


# The captured inventory as package objects with exactly the properties the
# capture saw (Server 2016's have no NonRemovable), and Remove-AppxPackage with
# exactly the captured parameters; a call is recorded, nothing is removed.
APPX_REPLAY = r'''
$cap = Get-Content -Raw -Encoding UTF8 -LiteralPath '__FIXTURE__' | ConvertFrom-Json
$fields = 'Name', 'PackageFullName', 'SignatureKind', 'InstallLocation', 'IsFramework', 'NonRemovable', 'PackageUserInformation'
$packages = @(foreach ($p in $cap.Packages) {
    $h = [ordered]@{}
    foreach ($f in $fields) { if (@($p.Properties) -contains $f) { $h[$f] = $p.$f } }
    [pscustomobject]$h
})
$provisioned = @($cap.ProvisionedNames)
$common = @([System.Management.Automation.PSCmdlet]::CommonParameters) + @([System.Management.Automation.PSCmdlet]::OptionalCommonParameters)
$params = @($cap.RemoveAppxPackageParameters | Where-Object { $common -notcontains $_.Name } |
    ForEach-Object { if ($_.Type -eq 'System.Management.Automation.SwitchParameter') { "[switch]`$$($_.Name)" } else { "[object]`$$($_.Name)" } })
$script:removed = @()
Invoke-Expression ("function Remove-AppxPackage { [CmdletBinding()] param(" + ($params -join ', ') +
    ") `$script:removed += ('{0} allusers={1}' -f `$Package, [bool]`$PSBoundParameters['AllUsers']) }")
"captured: $($cap.OS), $($packages.Count) packages, -AllUsers $((Get-Command Remove-AppxPackage).Parameters.ContainsKey('AllUsers'))"
'''


def _appx(fixture: str) -> str:
    return APPX_REPLAY.replace("__FIXTURE__", str(CAPTURED / fixture))


def test_finalize_selects_no_server_2016_system_app(tmp_path):
    # Measured on the first ws2016 base build: Microsoft.AAD.BrokerPlugin was
    # taken for a stray package, and Remove-AppxPackage -AllUsers failed.
    result, out = _finalize(tmp_path, _appx("appx-ws2016-14393.json") + r'''
$broker = @($packages | Where-Object Name -eq 'Microsoft.AAD.BrokerPlugin')
"broker: $($broker.Count) installed=$(@($broker.PackageUserInformation | Where-Object InstallState -eq 'Installed').Count) nonremovable-property=$([bool]$broker[0].PSObject.Properties['NonRemovable'])"
$stray = Get-StrayAppxPackage -Packages $packages -ProvisionedNames $provisioned
"stray: $(@($stray).Count)"
Remove-StrayAppxPackage -Stray $stray
"removed: $($script:removed.Count)"
''')
    assert result.returncode == 0, out
    assert "captured: Microsoft Windows Server 2016 Standard Evaluation 10.0.14393" in out, out
    assert "-AllUsers False" in out, out
    # The package that broke run 1 is in the capture, installed for a user,
    # with no NonRemovable property: the case the fix is for.
    assert "broker: 1 installed=1 nonremovable-property=False" in out, out
    assert "stray: 0" in out and "0 package(s) removed" in out and "removed: 0" in out, out


def test_finalize_refuses_removal_without_all_users_on_server_2016(tmp_path):
    # Server 2016's Remove-AppxPackage has no -AllUsers: given a package to
    # remove there, the step fails by name instead of calling it, and the
    # binder refuses -AllUsers exactly as Windows did when it was tried.
    fixture = json.loads((CAPTURED / "appx-ws2016-14393.json").read_text(encoding="utf-8-sig"))
    real_error = fixture["RemoveAllUsersAttempt"]["Message"]
    result, out = _finalize(tmp_path, _appx("appx-ws2016-14393.json") + r'''
$broker = @($packages | Where-Object Name -eq 'Microsoft.AAD.BrokerPlugin')
try { Remove-StrayAppxPackage -Stray $broker; 'no error' } catch { "refused: $($_.Exception.Message)" }
"removed: $($script:removed.Count)"
try { Remove-AppxPackage -Package $broker[0].PackageFullName -AllUsers; 'bound' } catch { "binder: $($_.Exception.Message)" }
''')
    assert result.returncode == 0, out
    broker = next(p for p in fixture["Packages"] if p["Name"] == "Microsoft.AAD.BrokerPlugin")
    assert f"refused: Remove-AppxPackage has no -AllUsers here; remove by hand: {broker['PackageFullName']}" in out, out
    assert "removed: 0" in out, out
    assert f"binder: {real_error}" in out, (real_error, out)


def test_finalize_on_the_captured_server_2022_inventory(tmp_path):
    # The Server 2019/2022 path the fix must not change: NonRemovable decides
    # (every package has it), nothing on a fresh clone is stray, and a package
    # handed to the removal goes to Remove-AppxPackage -AllUsers.
    result, out = _finalize(tmp_path, _appx("appx-ws2022-20348.json") + r'''
"with NonRemovable: $(@($packages | Where-Object { $_.PSObject.Properties['NonRemovable'] }).Count)"
$stray = Get-StrayAppxPackage -Packages $packages -ProvisionedNames $provisioned
"stray: $(@($stray).Count)"
$one = @($packages | Where-Object Name -eq 'Microsoft.AAD.BrokerPlugin')
Remove-StrayAppxPackage -Stray $one
$script:removed | ForEach-Object { "removed: $_" }
''')
    assert result.returncode == 0, out
    assert "captured: Microsoft Windows Server 2022 Standard Evaluation 10.0.20348" in out, out
    n = len(json.loads((CAPTURED / "appx-ws2022-20348.json").read_text(encoding="utf-8-sig"))["Packages"])
    assert f"{n} packages, -AllUsers True" in out and f"with NonRemovable: {n}" in out, out
    assert "stray: 0" in out, out
    assert out.count("removed: ") == 1 and "allusers=True" in out, out


# sshd replayed from sshd-ws2016-14393.json: each Get-Service returns the next
# captured status (the last one repeats); Stop-Service on a status that is
# still pending throws the error the real one threw on the clone, and
# otherwise moves on to the captured statuses after a stop; WaitForStatus reads
# on until the status it wants, or throws the exception (type and message) the
# real ServiceController threw. Set-Service is recorded, Start-Sleep is
# skipped.
SSHD_REPLAY = r'''
Add-Type -AssemblyName System.ServiceProcess
$cap = Get-Content -Raw -Encoding UTF8 -LiteralPath '__FIXTURE__' | ConvertFrom-Json
function Expand($runs) { @(foreach ($r in @($runs)) { for ($i = 0; $i -lt [int]$r[1]; $i++) { "$($r[0])".Split(' ')[0] } }) }
$script:queue = New-Object 'System.Collections.Generic.Queue[string]'
foreach ($s in @(__START__)) { $script:queue.Enqueue($s) }
$script:startType = 'Automatic'
$script:log = @()
function Next-Status { if ($script:queue.Count -gt 1) { $script:queue.Dequeue() } else { $script:queue.Peek() } }
function Get-Service { param([string]$Name)
    $o = [pscustomobject]@{ Name = $Name; Status = (Next-Status); StartType = $script:startType }
    $o | Add-Member -MemberType ScriptMethod -Name WaitForStatus -Value { param($want, $timeout)
        while ($true) {
            if ("$(Next-Status)" -eq "$want") { return }
            if ($script:queue.Count -le 1 -and "$($script:queue.Peek())" -ne "$want") {
                throw (New-Object System.ServiceProcess.TimeoutException $cap.Clone.WaitForStatusTimeout.Message) }
        } }
    $o
}
function Set-Service { param([string]$Name, $StartupType) $script:startType = "$StartupType"; $script:log += "set $StartupType" }
function Stop-Service { param([string]$Name)
    $now = "$($script:queue.Peek())"
    $script:log += "stop while $now"
    if ($now -like '*Pending') { throw $cap.Clone.StopRightAfterStart[-1].Result }
    $script:queue.Clear()
    foreach ($s in (Expand $cap.Clone.AfterStop)) { $script:queue.Enqueue($s) }
}
function Start-Sleep { param($Seconds) }
try { Stop-SshdForSysprep __ARGS__; 'returned' } catch { "failed: $($_.Exception.Message)" }
"final: $($script:queue.Peek()) $($script:startType)"
"log: $($script:log -join '; ')"
'''

SSHD_FIXTURE = CAPTURED / "sshd-ws2016-14393.json"
START = {
    # A fresh start on the clone: StartPending, then Running.
    "starting": "Expand $cap.Clone.AfterStart",
    # The build VM through the post-cleanup reboot until finalize: Running.
    "build": "Expand @($cap.BuildVM.Runs | Where-Object { $_[0] -like 'Running*' })",
    # After a stop on the clone: Stopped.
    "stopped": "Expand $cap.Clone.AfterStop",
    # Still starting, as Stop-Service left it on the clone, and never leaving it.
    "pending": "$cap.Clone.StopRightAfterStart[-1].StatusAfter",
}


def _sshd(tmp_path, start, args="", extra=""):
    body = SSHD_REPLAY.replace("__FIXTURE__", str(SSHD_FIXTURE)).replace("__START__", START[start]) \
        .replace("__ARGS__", args)
    return _finalize(tmp_path, body + extra)


def _captured_sshd():
    return json.loads(SSHD_FIXTURE.read_text(encoding="utf-8-sig"))


def test_captured_sshd_shows_the_failure_finalize_must_avoid():
    clone = _captured_sshd()["Clone"]
    assert clone["Binary"].endswith('Program Files\\OpenSSH\\sshd.exe"'), clone["Binary"]
    assert clone["AfterStart"][0][0] == "StartPending" and clone["AfterStart"][-1][0] == "Running"
    # The same error as base build run 2: a stop while sshd is still starting.
    attempt = clone["StopRightAfterStart"][-1]
    assert attempt["StatusBefore"] == "StartPending" and attempt["StatusAfter"] == "StartPending"
    assert "cannot be stopped due to the following error: Cannot stop sshd service" in attempt["Result"]
    assert clone["WaitForStatusTimeout"]["Type"] == "System.ServiceProcess.TimeoutException"


def test_finalize_waits_out_a_starting_sshd_then_stops_it(tmp_path):
    result, out = _sshd(tmp_path, "starting")
    assert result.returncode == 0, out
    assert "sshd before the stop: StartPending, start type Disabled" in out, out
    assert "returned" in out and "final: Stopped Disabled" in out, out
    assert "log: set Disabled; stop while Running" in out, out


def test_finalize_stops_sshd_as_on_the_build_vm(tmp_path):
    # The line finalize printed live on the rebuilt base's build VM.
    result, out = _sshd(tmp_path, "build")
    assert result.returncode == 0, out
    assert "sshd before the stop: Running, start type Disabled" in out, out
    assert "final: Stopped Disabled" in out and "log: set Disabled; stop while Running" in out, out


def test_finalize_leaves_a_stopped_sshd_stopped(tmp_path):
    result, out = _sshd(tmp_path, "stopped")
    assert result.returncode == 0, out
    assert "final: Stopped Disabled" in out and "log: set Disabled\n" in out, out


def test_finalize_fails_when_sshd_stays_starting(tmp_path):
    # Still starting after the wait: the stop is tried, fails as the real one
    # did, and the step fails (sysprep does not run). WaitForStatus on it
    # throws what the real ServiceController threw.
    clone = _captured_sshd()["Clone"]
    result, out = _sshd(tmp_path, "pending", args="-PendingTimeoutSeconds 1", extra=r'''
try { (Get-Service sshd).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(2)); 'no timeout' }
catch { $e = $_.Exception; while ($e.InnerException) { $e = $e.InnerException }; "wait: $($e.GetType().FullName): $($e.Message)" }
''')
    assert result.returncode == 0, out
    assert f"failed: {clone['StopRightAfterStart'][-1]['Result']}" in out, out
    assert "final: StartPending Disabled" in out and "log: set Disabled; stop while StartPending" in out, out
    timeout = clone["WaitForStatusTimeout"]
    assert f"wait: {timeout['Type']}: {timeout['Message']}" in out, out


# --- seal_windows.ps1: no host key shared between clones ----------------------
# The Windows base ships without host keys and sshd writes any that are
# missing when it starts (prepare.ps1 proves that on the base's own sshd), so
# a base clone has its own. A lab build is a base clone: its sshd wrote the
# build VM's keys, and the seal must delete them in both modes. With -KeepSsh
# every clone of the image would otherwise present the build VM's host key;
# without it each clone still carries the same private keys on disk, served
# the moment anyone enables sshd. Measured on pve2 before this fix: two clones
# of a base clone sealed with -KeepSsh served one and the same host key.

SEAL_WINDOWS = IMAGE_BUILD / "seal_windows.ps1"
HOST_KEYS = ("ssh_host_rsa_key", "ssh_host_rsa_key.pub", "ssh_host_ecdsa_key", "ssh_host_ecdsa_key.pub",
             "ssh_host_ed25519_key", "ssh_host_ed25519_key.pub")


def _seal_windows(tmp_path: Path, body: str):
    """Dot-source seal_windows.ps1 -FunctionsOnly and run `body`. The seal
    changes the machine it runs on (Administrator's password, the DHCP lease),
    so the harness refuses a script that would act before the guard."""
    text = SEAL_WINDOWS.read_text()
    guard = text.find("if ($FunctionsOnly) { return }")
    first = text.find("$sealPaths = ")
    assert "[switch]$FunctionsOnly" in text and 0 < guard < first, \
        "seal_windows.ps1 must return under -FunctionsOnly before it changes anything"
    tmp_path.mkdir(parents=True, exist_ok=True)
    script = tmp_path / "harness.ps1"
    script.write_text("$ErrorActionPreference = 'Stop'\n"
                      f". '{SEAL_WINDOWS}' -FunctionsOnly\n" + body, encoding="utf-8")
    result = subprocess.run(_powershell() + ["-File", str(script)], capture_output=True, text=True, timeout=120)
    return result, result.stdout + result.stderr


def _ssh_dir(tmp_path: Path) -> Path:
    """C:\\ProgramData\\ssh as the build VM leaves it before the seal."""
    ssh = tmp_path / "ssh"
    (ssh / "logs").mkdir(parents=True)
    for name in HOST_KEYS:
        (ssh / name).write_text(f"{name} of the build VM\n")
    (ssh / "sshd_config").write_text("Match Group administrators\n")
    (ssh / "logs" / "sshd.log").write_text("log\n")
    return ssh


def test_seal_windows_deletes_every_host_key(tmp_path):
    ssh = _ssh_dir(tmp_path)
    result, out = _seal_windows(tmp_path, f"Remove-SshHostKey -Directory '{ssh}'\n'done'\n")
    assert result.returncode == 0, out
    assert "done" in out, out
    assert sorted(p.name for p in ssh.iterdir()) == ["logs", "sshd_config"], out
    assert (ssh / "sshd_config").read_text() == "Match Group administrators\n"
    assert (ssh / "logs" / "sshd.log").is_file()


def test_seal_windows_host_keys_without_openssh(tmp_path):
    # An image without OpenSSH has no C:\ProgramData\ssh: nothing to delete.
    result, out = _seal_windows(tmp_path, f"Remove-SshHostKey -Directory '{tmp_path / 'absent'}'\n'done'\n")
    assert result.returncode == 0 and "done" in out, out


def test_seal_windows_fails_when_a_host_key_survives(tmp_path):
    # A key the seal cannot delete fails the seal, so the image is not saved:
    # here Remove-Item deletes nothing (the harness's function shadows the cmdlet).
    ssh = _ssh_dir(tmp_path)
    result, out = _seal_windows(tmp_path, "function Remove-Item { }\n"
                                          f"Remove-SshHostKey -Directory '{ssh}'\n'not reached'\n")
    assert result.returncode != 0, out
    assert "not reached" not in out, out
    assert "still present after removal" in out and "ssh_host_ed25519_key" in out, out
    assert all((ssh / name).is_file() for name in HOST_KEYS)


def test_seal_windows_deletes_host_keys_with_and_without_keep_ssh():
    # One call, at the top level of the script: not inside the `if (-not
    # $KeepSsh)` block, a function or a loop, so it runs in both modes.
    parse = (
        "$ast = [System.Management.Automation.Language.Parser]::ParseFile("
        f"'{SEAL_WINDOWS}', [ref]$null, [ref]$null)\n"
        "$calls = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and "
        "$n.GetCommandName() -eq 'Remove-SshHostKey' }, $true)\n"
        "foreach ($c in $calls) { $p = $c.Parent; $up = @(); while ($p) { $up += $p.GetType().Name; $p = $p.Parent }\n"
        "  'call: ' + $c.Extent.Text + ' | ' + ($up -join '<') }\n")
    result = subprocess.run(_powershell() + ["-Command", parse], capture_output=True, text=True, timeout=120)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    calls = [l for l in out.splitlines() if l.startswith("call: ")]
    assert len(calls) == 1, out
    text, ancestry = calls[0][len("call: "):].split(" | ")
    assert text == "Remove-SshHostKey -Directory 'C:\\ProgramData\\ssh'", text
    # PipelineAst < NamedBlockAst < ScriptBlockAst: a statement of the script itself.
    assert ancestry == "PipelineAst<NamedBlockAst<ScriptBlockAst", ancestry


# A -KeepSsh image deleting its keys in the seal is not enough on Server 2016:
# Win32-OpenSSH 10.0 (the MSI) registers sshd with "restart on failure", sshd
# exits uncleanly when the save shuts the build VM down, the service manager
# restarts it, and sshd writes new host keys into the image (measured on pve2:
# two clones of a -KeepSsh seal of a base-ws2016 clone shared keys written at
# the shutdown). So the seal also leaves sshd Manual and registers a startup
# task that runs a script as SYSTEM on the clone's first boot.

# --- the first-boot script: the clone's own keys first, only then sshd on ----

FIRST_BOOT_MOCKS = r"""
$global:log = @(); $global:sshdStatus = 'Stopped'; $global:startType = 'Manual'
function global:Get-Service { param($Name) [pscustomobject]@{ Status = $global:sshdStatus } }
function global:Set-Service { param($Name, $StartupType) $global:startType = $StartupType; $global:log += "set $Name $StartupType" }
function global:Stop-Service { param($Name, [switch]$Force) $global:sshdStatus = 'Stopped'; $global:log += "stop $Name" }
function global:Start-Service { param($Name)
    if ($global:startFails) { $global:log += "start $Name failed"; throw 'the service did not start' }
    $global:sshdStatus = 'Running'
    $global:log += "start $Name type=$global:startType keys=$(@(Get-ChildItem '__SSH__\ssh_host_*').Count)" }
function global:Unregister-ScheduledTask { param($TaskName, $Confirm)
    if ($global:unregisterFails) { throw 'access denied' }
    $global:log += "unregister $TaskName" }
# Deleting the script, or writing first-boot.log, fails on demand; everything
# else goes to the real cmdlet. eventcreate.exe (the fallback log) is recorded.
function global:Remove-Item { [CmdletBinding()] param([string]$LiteralPath, [switch]$Force, [switch]$Recurse)
    if ($global:removeFails -and $LiteralPath -like '*first-boot-host-keys.ps1') { throw 'the file is in use' }
    Microsoft.PowerShell.Management\Remove-Item @PSBoundParameters }
function global:Add-Content { [CmdletBinding()] param([string]$LiteralPath, [object]$Value)
    if ($global:logFails -and $LiteralPath -like '*first-boot.log') { throw 'the disk is full' }
    Microsoft.PowerShell.Management\Add-Content @PSBoundParameters }
function global:eventcreate.exe { $global:log += "event: $($args[-1] -replace '^\S+ \S+ ', '')" }
"""

# ssh-keygen -A as the clone runs it, played by a script: 'ok' writes three new
# key pairs, 'fail' writes nothing and exits 1, 'image' writes the image's keys
# back, 'partial' writes only an Ed25519 pair.
FAKE_KEYGEN = r"""
$ssh = '__SSH__'
$global:log += "keygen -A sshd=$global:sshdStatus keys=$(@(Get-ChildItem "$ssh\ssh_host_*").Count)"
if ($global:keygenMode -eq 'fail') { [Console]::Error.WriteLine('ssh-keygen: failed'); exit 1 }
if ($global:keygenMode -eq 'image') { Copy-Item '__IMAGE__\*' $ssh; exit 0 }
$types = if ($global:keygenMode -eq 'partial') { @('ed25519') } else { @('rsa', 'ecdsa', 'ed25519') }
foreach ($t in $types) {
    $b = New-Object byte[] 51; (New-Object Random).NextBytes($b)
    Set-Content "$ssh\ssh_host_${t}_key" 'private'
    Set-Content "$ssh\ssh_host_${t}_key.pub" "ssh-$t $([Convert]::ToBase64String($b)) system@clone"
}
exit 0
"""


def _pub_blob() -> str:
    return __import__("base64").b64encode(os.urandom(51)).decode()


def _image_keys(tmp_path: Path) -> Path:
    """C:\\ProgramData\\ssh as a clone of the image finds it: the keys sshd wrote
    when the service manager restarted it at the save's shutdown."""
    ssh = tmp_path / "ssh"
    image = tmp_path / "image-copy"
    ssh.mkdir(parents=True)
    image.mkdir()
    for t in ("rsa", "ecdsa", "ed25519"):
        for d in (ssh, image):
            (d / f"ssh_host_{t}_key").write_text("image private\n")
        blob = _pub_blob()
        for d in (ssh, image):
            (d / f"ssh_host_{t}_key.pub").write_text(f"ssh-{t} {blob} system@image\n")
    (ssh / "sshd_config").write_text("Match Group administrators\n")
    return ssh


def _fingerprints(ssh: Path) -> dict:
    out = {}
    for pub in sorted(ssh.glob("ssh_host_*_key.pub")):
        blob = __import__("base64").b64decode(pub.read_text().split()[1])
        out[pub.name] = "SHA256:" + __import__("base64").b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return out


def _first_boot(tmp_path: Path, runs: list):
    """Write the first-boot script the seal would write, then run it once per
    entry of `runs` (each sets keygen, startFails, unregisterFails, removeFails,
    logFails; `rewrite`
    puts the script back first, as if the task fired again after it had
    removed both), with
    sshd, the service cmdlets and Unregister-ScheduledTask played by recording
    functions. The script runs in its own scope, so the record is global."""
    ssh = tmp_path / "ssh"
    fb = tmp_path / "first-boot"
    fb.mkdir()
    keygen = tmp_path / "ssh-keygen.ps1"
    keygen.write_text(FAKE_KEYGEN.replace("__SSH__", str(ssh)).replace("__IMAGE__", str(tmp_path / "image-copy")),
                      encoding="utf-8")
    script = fb / "first-boot-host-keys.ps1"
    body = FIRST_BOOT_MOCKS.replace("__SSH__", str(ssh)) + (
        f"$text = Get-FirstBootScript -SshDirectory '{ssh}' -Directory '{fb}' "
        f"-TaskName 'image-build-ssh-host-keys' -Keygen '{keygen}'\n"
        f"Set-Content -LiteralPath '{script}' -Value $text -Encoding UTF8\n")
    for i, run in enumerate(runs, 1):
        body += (
            (f"Set-Content -LiteralPath '{script}' -Value $text -Encoding UTF8\n" if run.get("rewrite") else "") +
            f"$global:keygenMode = '{run.get('keygen', 'ok')}'; $global:startFails = ${bool(run.get('start_fails'))}; "
            f"$global:unregisterFails = ${bool(run.get('unregister_fails'))}; "
            f"$global:removeFails = ${bool(run.get('remove_fails'))}; $global:logFails = ${bool(run.get('log_fails'))}\n"
            f"$global:log += '-- run {i}'; $global:LASTEXITCODE = 0\n"
            f"& '{script}'\n"
            f"$global:log += \"exit $LASTEXITCODE; sshd $global:sshdStatus $global:startType; "
            f"script $(Test-Path '{script}')\"\n")
    body += "$global:log | ForEach-Object { \"log: $_\" }\n"
    result, out = _seal_windows(tmp_path / "harness", body)
    log = [l[len("log: "):] for l in out.splitlines() if l.startswith("log: ")]
    return result, out, log, fb


def test_first_boot_makes_the_keys_before_it_turns_sshd_on(tmp_path):
    ssh = _image_keys(tmp_path)
    image = _fingerprints(ssh)
    result, out, log, fb = _first_boot(tmp_path, [{}])
    assert result.returncode == 0, out
    assert log == [
        "-- run 1",
        "set sshd Manual",                        # held off while the keys change
        "keygen -A sshd=Stopped keys=0",          # the image's keys are gone first
        "set sshd Automatic",                     # only after the new keys checked out
        "start sshd type=Automatic keys=6",
        "unregister image-build-ssh-host-keys",
        "exit 0; sshd Running Automatic; script False",
    ], out
    new = _fingerprints(ssh)
    assert sorted(new) == sorted(image) and not set(new.values()) & set(image.values()), (new, image)
    assert sorted((fb / "image-fingerprints").read_text().split()) == sorted(image.values())
    assert sorted((fb / "keys-generated").read_text().split()) == sorted(new.values())
    assert "attempt 1: new host keys" in (fb / "first-boot.log").read_text()


@pytest.mark.parametrize("mode,why", [
    ("fail", "ssh-keygen -A exited 1"),
    ("partial", "ssh-keygen -A wrote no rsa key"),
    ("image", "the new rsa key is the image's"),
])
def test_first_boot_leaves_sshd_off_when_key_generation_fails(tmp_path, mode, why):
    _image_keys(tmp_path)
    result, out, log, fb = _first_boot(tmp_path, [{"keygen": mode}])
    assert result.returncode == 0, out
    # sshd never set Automatic and never started; the task and script stay.
    assert log == ["-- run 1", "set sshd Manual", log[2], "set sshd Manual",
                   "exit 1; sshd Stopped Manual; script True"], out
    assert log[2].startswith("keygen -A sshd=Stopped"), out
    assert why in (fb / "first-boot.log").read_text(), (fb / "first-boot.log").read_text()
    assert not (fb / "keys-generated").exists()


def test_first_boot_after_success_never_touches_the_keys_again(tmp_path):
    # Run 1 makes the keys but sshd fails to start: sshd is left Manual and
    # stopped and the task stays. Run 2 (next boot) only starts sshd. Run 3
    # (the task fired again although it had succeeded) changes nothing.
    _image_keys(tmp_path)
    result, out, log, fb = _first_boot(tmp_path, [{"start_fails": True}, {}, {"rewrite": True}])
    assert result.returncode == 0, out
    assert log == [
        "-- run 1", "set sshd Manual", "keygen -A sshd=Stopped keys=0", "set sshd Automatic",
        "start sshd failed", "set sshd Manual", "exit 1; sshd Stopped Manual; script True",
        "-- run 2", "set sshd Automatic", "start sshd type=Automatic keys=6",
        "unregister image-build-ssh-host-keys", "exit 0; sshd Running Automatic; script False",
        "-- run 3", "set sshd Automatic", "start sshd type=Automatic keys=6",
        "unregister image-build-ssh-host-keys", "exit 0; sshd Running Automatic; script False",
    ], out
    made = (fb / "keys-generated").read_text().split()
    assert sorted(_fingerprints(tmp_path / "ssh").values()) == sorted(made)
    text = (fb / "first-boot.log").read_text()
    assert "attempt 2: keys were made on an earlier attempt; not touched" in text, text
    assert "attempt 3: keys were made on an earlier attempt; not touched" in text, text


# Once sshd runs on the clone's verified keys, cleanup is best effort: no
# failure in it stops sshd or touches the keys (the task may already be gone,
# and then nothing would retry).

SUCCESS = ["-- run 1", "set sshd Manual", "keygen -A sshd=Stopped keys=0", "set sshd Automatic",
           "start sshd type=Automatic keys=6"]


def test_first_boot_unregister_failure_keeps_sshd_and_retries_only_the_cleanup(tmp_path):
    # The task cannot be unregistered: sshd keeps running, the script stays, so
    # the task's next run (next boot) still has something to run. That run makes
    # no keys and only cleans up.
    _image_keys(tmp_path)
    result, out, log, fb = _first_boot(tmp_path, [{"unregister_fails": True}, {}])
    assert result.returncode == 0, out
    assert log == SUCCESS + [
        "exit 0; sshd Running Automatic; script True",
        "-- run 2", "set sshd Automatic", "start sshd type=Automatic keys=6",
        "unregister image-build-ssh-host-keys", "exit 0; sshd Running Automatic; script False",
    ], out
    assert sorted(_fingerprints(tmp_path / "ssh").values()) == sorted((fb / "keys-generated").read_text().split())
    text = (fb / "first-boot.log").read_text()
    assert "the task could not be unregistered (access denied); the script stays" in text, text
    assert "attempt 2: keys were made on an earlier attempt; not touched" in text, text


def test_first_boot_script_delete_failure_keeps_sshd(tmp_path):
    # The task is gone and the script cannot be deleted: sshd keeps running and
    # the script is left, inert (no task runs it; its directory is admin-only).
    _image_keys(tmp_path)
    result, out, log, fb = _first_boot(tmp_path, [{"remove_fails": True}])
    assert result.returncode == 0, out
    assert log == SUCCESS + ["unregister image-build-ssh-host-keys", "exit 0; sshd Running Automatic; script True"], out
    text = (fb / "first-boot.log").read_text()
    assert "task removed; the script could not be deleted (the file is in use) and is inert" in text, text


def test_first_boot_log_failure_keeps_sshd(tmp_path):
    # first-boot.log cannot be written: every line goes to the Application
    # event log instead, and sshd keeps running.
    _image_keys(tmp_path)
    result, out, log, fb = _first_boot(tmp_path, [{"log_fails": True}])
    assert result.returncode == 0, out
    events = [l for l in log if l.startswith("event: ")]
    assert [l for l in log if not l.startswith("event: ")] == SUCCESS + [
        "unregister image-build-ssh-host-keys", "exit 0; sshd Running Automatic; script False"], out
    assert len(events) == 2 and "attempt 1: new host keys" in events[0], out
    assert "attempt 1: sshd Automatic and running; task and script removed" in events[1], out
    assert not (fb / "first-boot.log").exists()


def test_first_boot_gives_up_after_five_attempts(tmp_path):
    _image_keys(tmp_path)
    result, out, log, fb = _first_boot(tmp_path, [{"keygen": "fail"}] * 6)
    assert result.returncode == 0, out
    assert log.count("exit 1; sshd Stopped Manual; script True") == 5, out
    assert sum(1 for l in log if l.startswith("keygen")) == 5, out
    assert log[-4:] == ["-- run 6", "set sshd Manual", "unregister image-build-ssh-host-keys",
                        "exit 1; sshd Stopped Manual; script False"], out
    assert "giving up after 5 attempts" in (fb / "first-boot.log").read_text()


# --- who may change what SYSTEM runs ------------------------------------------
# Every check reads the security descriptor back and judges it with
# Get-NonAdminWriteAccess: generic rights mapped, inherit-only ACEs counted on
# the object itself, deny ACEs NOT subtracted (stricter than Windows' effective
# access, so a write grant is never missed).

PROGRAMDATA_SDDL = ("O:SYG:SYD:PAI(A;OICIIO;GA;;;CO)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)"
                    "(A;CI;DCLCRPCR;;;BU)")   # read on Server 2016 and 2022 (pve2, 2026-11-21)


@pytest.mark.parametrize("sddl,args,want", [
    ("O:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)", "", []),
    ("O:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;0x1200a9;;;BU)", "", []),                  # Users read/execute
    ("O:BAD:P(A;;FA;;;SY)(A;;0x1301bf;;;AU)", "", ["S-1-5-11 has 0x00010116"]),      # Modify
    ("O:BAD:P(A;;GA;;;WD)", "", ["S-1-1-0 has 0x10000000"]),                         # generic all
    ("O:BAD:P(A;;GW;;;BU)", "", ["S-1-5-32-545 has 0x40000000"]),                    # generic write
    ("O:BAD:P(A;;WD;;;AU)(D;;WD;;;AU)", "", ["S-1-5-11 has 0x00040000"]),            # a deny does not excuse
    ("O:BAD:P(A;OICIIO;GA;;;CO)", "", ["S-1-3-0 has 0x10000000"]),                   # inherit-only counts
    ("O:BUD:P(A;;FA;;;SY)", "", ["owner S-1-5-32-545"]),
    ("O:BAD:", "", []),                                                              # empty DACL: no one
    ("O:BA", "", ["no DACL"]),                                                       # null DACL: everyone
    (PROGRAMDATA_SDDL, "-Mask $ParentMask -EffectiveOnly", []),                      # Users cannot delete a child
    (PROGRAMDATA_SDDL, "", ["S-1-3-0 has 0x10000000", "S-1-5-32-545 has 0x00000116"]),
])
def test_non_admin_write_access(tmp_path, sddl, args, want):
    result, out = _seal_windows(tmp_path, f"Get-NonAdminWriteAccess -Sddl '{sddl}' -Label x {args} | "
                                          "ForEach-Object { \"finding: $_\" }\n'end'\n")
    assert result.returncode == 0 and "end" in out, out
    assert [l[len("finding: x: "):] for l in out.splitlines() if l.startswith("finding: ")] == want, out


def _me() -> str:
    out = subprocess.run(_powershell() + ["-Command", "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value"],
                         capture_output=True, text=True, timeout=60)
    return out.stdout.strip()


TASK_MOCKS = r"""
function New-ScheduledTaskAction { param($Execute, $Argument) [pscustomobject]@{ Execute = $Execute; Arguments = $Argument } }
function New-ScheduledTaskTrigger { param([switch]$AtStartup) "AtStartup=$AtStartup" }
function New-ScheduledTaskPrincipal { param($UserId, $LogonType, $RunLevel) [pscustomobject]@{ UserId = 'SYSTEM'; RunLevel = $RunLevel } }
$global:registered = $null
function Register-ScheduledTask { param($TaskName, $Action, $Trigger, $Principal, [switch]$Force)
    $global:registered = [pscustomobject]@{ Actions = @($Action); Principal = $Principal }
    # Its output goes to Out-Null, so the record goes straight to stdout.
    [Console]::Out.WriteLine("log: register $TaskName | $($Action.Execute) $($Action.Arguments) | $Trigger | $($Principal.UserId) $($Principal.RunLevel) | Force=$Force") }
function Get-ScheduledTask { param($TaskName) $global:registered }
function Set-TaskSddl { param($TaskName, $Sddl) "log: task sddl $Sddl" }
function Get-TaskSddl { param($TaskName) '__TASK_SDDL__' }
function Set-Service { param($Name, $StartupType) "log: set $Name $StartupType" }
"""


def _register(tmp_path: Path, extra: str = "", task_sddl: str = "O:BAD:(A;;FA;;;SY)(A;;FA;;;BA)(A;;FR;;;SY)",
              broad_parent: bool = False):
    """Register-SshHostKeyFirstBoot against real NTFS under tmp_path, with the
    Task Scheduler played by functions. This test does not run elevated, so its
    own SID stands in for Administrators (owner and the second full-control
    ACE). The parent directory gets C:\\ProgramData's ACL as Server 2016 and 2022
    ship it (this user for Administrators); `broad_parent` adds an inheritable
    Authenticated Users Modify ACE to it, as a base with a broad C:\\ProgramData
    would. `extra` runs afterwards."""
    me = _me()
    root = tmp_path / "programdata"
    root.mkdir(parents=True)
    task_file = tmp_path / "tasks" / "image-build-ssh-host-keys"
    task_file.parent.mkdir()
    task_file.write_text("<Task/>")
    trusted = f"@($AdminSids + '{me}')"
    body = TASK_MOCKS.replace("__TASK_SDDL__", task_sddl) + (
        f"$a = Get-Acl '{root}'; $a.SetSecurityDescriptorSddlForm('O:{me}D:PAI(A;OICIIO;GA;;;CO)(A;OICI;FA;;;SY)"
        f"(A;OICI;FA;;;{me})(A;OICI;0x1200a9;;;BU)(A;CI;DCLCRPCR;;;BU)', 'Owner, Access'); Set-Acl '{root}' $a\n" +
        (f"icacls '{root}' /grant '*S-1-5-11:(OI)(CI)(M)' | Out-Null\n" if broad_parent else "") +
        # C:\\Windows\\System32\\Tasks and the task's file there: administrators only.
        f"Protect-AdminOnlyPath -Path '{task_file}' -AdminSid '{me}'\n"
        f"Protect-AdminOnlyPath -Path '{task_file.parent}' -AdminSid '{me}'\n"
        "try {\n"
        f"  Register-SshHostKeyFirstBoot -SshDirectory 'C:\\ProgramData\\ssh' -Directory '{root}\\image-build-first-boot' "
        f"-TaskName 'image-build-ssh-host-keys' -Keygen 'C:\\OpenSSH\\ssh-keygen.exe' -AdminSid '{me}' -Trusted {trusted} "
        f"-TaskFile '{task_file}'\n"
        "  'registered'\n"
        "} catch { \"failed: $($_.Exception.Message)\" }\n"
        f"foreach ($p in '{root}\\image-build-first-boot', '{root}\\image-build-first-boot\\first-boot-host-keys.ps1') {{\n"
        "  if (Test-Path $p) { \"sddl: $((Get-Acl $p).Sddl)\" } }\n" + extra)
    try:
        result, out = _seal_windows(tmp_path / "harness", body)
    finally:
        # Hand the tree back to this user so pytest can delete it.
        subprocess.run(["icacls", str(tmp_path), "/reset", "/T", "/C", "/Q"], capture_output=True, timeout=120)
    return result, out, me, root


def test_first_boot_script_and_directory_are_administrators_only(tmp_path):
    result, out, me, root = _register(tmp_path)
    assert result.returncode == 0, out
    assert "registered" in out, out
    script = root / "image-build-first-boot" / "first-boot-host-keys.ps1"
    log = [l[len("log: "):] for l in out.splitlines() if l.startswith("log: ")]
    assert log == [
        f"register image-build-ssh-host-keys | powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass "
        f"-File \"{script}\" | AtStartup=True | SYSTEM Highest | Force=True",
        "task sddl D:P(A;;FA;;;SY)(A;;FA;;;BA)",
        "set sshd Manual",   # only once the task that turns it on is in place and checked
    ], out
    # Read back: owner Administrators (here this user), protected (P), only
    # SYSTEM and Administrators; the directory's ACEs inherited by what it holds.
    sddl = [l[len("sddl: "):] for l in out.splitlines() if l.startswith("sddl: ")]
    assert [(x.split("G:")[0], x.split("D:")[1]) for x in sddl] == [
        (f"O:{me}", f"PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;{me})"),
        (f"O:{me}", f"PAI(A;;FA;;;SY)(A;;FA;;;{me})"),
    ], out


def test_first_boot_directory_drops_a_broad_parent_acl(tmp_path):
    # A base whose C:\ProgramData lets Authenticated Users modify everything
    # below it: the directory is protected, so nothing of that is inherited.
    result, out, me, root = _register(tmp_path, broad_parent=True)
    assert result.returncode == 0, out
    assert "registered" in out, out
    assert all("S-1-5-11" not in l and ";AU)" not in l for l in out.splitlines() if l.startswith("sddl: ")), out


@pytest.mark.parametrize("target", ["image-build-first-boot\\first-boot-host-keys.ps1", "image-build-first-boot"])
def test_first_boot_check_fails_on_an_authenticated_users_modify_ace(tmp_path, target):
    # The seal's own check, run again after an Authenticated Users Modify ACE
    # is added to the script or to its directory, must fail.
    extra = (
        "$t = Join-Path (Split-Path (Get-ChildItem -Recurse -Filter first-boot-host-keys.ps1 '__ROOT__')[0].DirectoryName) '__TARGET__'\n"
        "$grant = if ((Get-Item $t).PSIsContainer) { '*S-1-5-11:(OI)(CI)(M)' } else { '*S-1-5-11:(M)' }\n"
        "icacls $t /grant $grant | Out-Null\n"
        "try { Assert-AdminOnlyPath -Path $t -Trusted @($AdminSids + [Security.Principal.WindowsIdentity]::GetCurrent().User.Value); 'still passes' }\n"
        "catch { \"check: $($_.Exception.Message)\" }\n")
    root = tmp_path / "programdata"
    result, out, me, root = _register(tmp_path, extra.replace("__ROOT__", str(root)).replace("__TARGET__", target))
    assert result.returncode == 0, out
    assert "registered" in out and "still passes" not in out, out
    assert "check: not administrators only:" in out and "S-1-5-11 has 0x00010116" in out, out


@pytest.mark.parametrize("task_sddl,why", [
    ("O:BAD:(A;;FA;;;SY)(A;;FA;;;BA)(A;;0x1f019f;;;AU)", "task image-build-ssh-host-keys: S-1-5-11 has"),
    ("O:BUD:(A;;FA;;;SY)(A;;FA;;;BA)", "task image-build-ssh-host-keys: owner S-1-5-32-545"),
])
def test_register_fails_when_others_can_change_the_task(tmp_path, task_sddl, why):
    result, out, me, root = _register(tmp_path, task_sddl=task_sddl)
    assert result.returncode == 0, out
    assert "failed: not administrators only:" in out and why in out, out
    assert "log: set sshd Manual" not in out, out   # the seal stops before it touches sshd


def test_seal_windows_registers_the_first_boot_step_only_with_keep_ssh():
    # Without -KeepSsh sshd is disabled, so the service manager cannot restart
    # it at the shutdown, and a clone has no sshd to give keys to.
    parse = (
        "$ast = [System.Management.Automation.Language.Parser]::ParseFile("
        f"'{SEAL_WINDOWS}', [ref]$null, [ref]$null)\n"
        "$calls = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and "
        "$n.GetCommandName() -in 'Register-SshHostKeyFirstBoot', 'Set-Service' }, $true)\n"
        "foreach ($c in $calls) { $if = $c.Parent; while ($if -and $if -isnot [System.Management.Automation.Language.IfStatementAst]) { $if = $if.Parent }\n"
        "  $where = 'top'\n"
        "  if ($if) { $i = 0; foreach ($cl in $if.Clauses) { if ($cl.Item2.Extent.StartOffset -le $c.Extent.StartOffset -and $c.Extent.EndOffset -le $cl.Item2.Extent.EndOffset) { $where = 'if ' + $cl.Item1.Extent.Text } }\n"
        "    if ($where -eq 'top' -and $if.ElseClause) { $where = 'else of ' + $if.Clauses[0].Item1.Extent.Text } }\n"
        "  $fn = $c.Parent; while ($fn -and $fn -isnot [System.Management.Automation.Language.FunctionDefinitionAst]) { $fn = $fn.Parent }\n"
        "  if (-not $fn) { 'call: ' + $c.Extent.Text + ' | ' + $where } }\n")
    result = subprocess.run(_powershell() + ["-Command", parse], capture_output=True, text=True, timeout=120)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert [l for l in out.splitlines() if l.startswith("call: ")] == [
        "call: Register-SshHostKeyFirstBoot -SshDirectory 'C:\\ProgramData\\ssh' "
        "-Directory 'C:\\ProgramData\\image-build-first-boot' -TaskName 'image-build-ssh-host-keys' -Keygen $keygen"
        " | if $KeepSsh",
        "call: Set-Service sshd -StartupType Disabled | else of $KeepSsh",
    ], out
