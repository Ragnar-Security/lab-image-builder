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
