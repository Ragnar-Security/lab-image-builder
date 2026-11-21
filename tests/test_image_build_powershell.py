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
