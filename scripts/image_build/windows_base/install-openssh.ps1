# Install OpenSSH Server on a Windows without the OpenSSH.Server capability
# (Windows Server 2016), for prepare.ps1 of the Windows base
# (proxmox_base_windows.sh --os ws2016). Downloads one pinned Win32-OpenSSH
# release from GitHub over the build network and installs it only if its
# SHA-256 matches the pin; a download that fails or a hash that differs stops
# the build (exit 1) and leaves no installer behind. The MSI installs to
# C:\Program Files\OpenSSH and registers the sshd service; prepare.ps1 then
# configures it exactly as it configures the capability.
#
#   install-openssh.ps1 [-Url U -Sha256 H] [-WorkDir D] [-DownloadOnly]
# -DownloadOnly fetches and checks the artifact, then deletes it (the tests
# use it, with a local URL).
param(
    [string]$Url = 'https://github.com/PowerShell/Win32-OpenSSH/releases/download/10.0.0.0p2-Preview/OpenSSH-Win64-v10.0.0.0.msi',
    [string]$Sha256 = 'ddec9c53864280759cf9f74791cefd387100e3946aa849a1c138a4ed1b96b7d9',
    [string]$WorkDir = 'C:\basebuild',
    [switch]$DownloadOnly
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
trap {
    [Console]::Error.WriteLine("install-openssh.ps1: $($_.Exception.Message)")
    exit 1
}

# Windows PowerShell 5.1 on Server 2016 offers only SSL 3 and TLS 1.0 unless
# told otherwise, and GitHub accepts TLS 1.2 and later.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$msi = Join-Path $WorkDir 'OpenSSH-Win64.msi'
Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath $msi
try {
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $msi
} catch {
    Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath $msi
    throw "download of $Url failed: $($_.Exception.Message)"
}
$got = (Get-FileHash -Algorithm SHA256 -LiteralPath $msi).Hash.ToLowerInvariant()
if ($got -ne $Sha256.ToLowerInvariant()) {
    Remove-Item -Force -LiteralPath $msi
    throw "$Url has sha256 $got, which does not match the pin $($Sha256.ToLowerInvariant()); not installed"
}
Write-Output "fetched $Url sha256 $got matches the pin"
if ($DownloadOnly) {
    Remove-Item -Force -LiteralPath $msi
    exit 0
}

$log = Join-Path $WorkDir 'OpenSSH-Win64.msi.log'
$p = Start-Process -FilePath msiexec.exe -Wait -PassThru `
    -ArgumentList @('/i', "`"$msi`"", '/qn', '/norestart', '/l*v', "`"$log`"")
Remove-Item -Force -LiteralPath $msi
if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) { throw "msiexec exited $($p.ExitCode) (log: $log)" }
$svc = Get-CimInstance Win32_Service -Filter "Name='sshd'"
if (-not $svc) { throw 'the MSI installed no sshd service' }
Write-Output "installed: $($svc.PathName)"
exit 0
