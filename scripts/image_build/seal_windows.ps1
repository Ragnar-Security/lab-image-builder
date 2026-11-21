# Seal a Windows build VM just before its image is saved.
#
# Removes the uploaded payload, the build key and sshd's host keys, sets
# Administrator to a random 32-character password that is never written
# anywhere, forgets the build VM's DHCP lease, and disables sshd, or with
# -KeepSsh leaves it to a first-boot step that gives each clone its own host
# keys. Exits 1 if the payload directory, a host key or a cached lease is
# left, so the build stops before the image is saved.
# -FunctionsOnly defines the functions below and returns before anything is
# changed (the tests dot-source it that way).
param(
    [switch]$KeepSsh,
    [switch]$FunctionsOnly
)
$ErrorActionPreference = 'Stop'

# Delete sshd's host keys: sshd writes any missing host key when it starts
# (prepare.ps1 of the Windows base proves that on the base's own sshd). The
# build VM's sshd wrote these keys; left in the image, every clone carries the
# same private keys. Throws if one survives.
function Remove-SshHostKey {
    param([string]$Directory)
    if (-not (Test-Path -LiteralPath $Directory)) { return }
    $pattern = Join-Path $Directory 'ssh_host_*'
    foreach ($key in @(Get-ChildItem -Path $pattern -Force -ErrorAction Stop)) {
        Remove-Item -LiteralPath $key.FullName -Force -ErrorAction Stop
    }
    $left = @(Get-ChildItem -Path $pattern -Force -ErrorAction Stop)
    if ($left.Count) {
        throw "still present after removal: $(($left | ForEach-Object { $_.FullName }) -join ', ')"
    }
}

# With -KeepSsh, deleting the keys in the seal is not enough: sshd keeps
# running until the save shuts the VM down, and Win32-OpenSSH 10.0 (the
# Server 2016 base) registers sshd with "restart on failure". sshd exits
# uncleanly at the shutdown, the service manager restarts it, and it writes
# new keys into the image (measured on pve2: two clones then shared them).
# So sshd is left Manual, and a startup task, run as SYSTEM on the clone's
# first boot, deletes whatever keys the image holds, sets sshd Automatic and
# starts it (sshd then writes the clone's own keys), and then removes itself
# and its script. If sshd writes no key the task stays, and the next boot
# tries again.
$FirstBootScript = @'
# First boot of a clone of a KEEP_SSH=yes image, written by seal_windows.ps1:
# this clone's own sshd host keys, then sshd on, then this step removed.
$ErrorActionPreference = 'Stop'
function Remove-SshHostKey {__REMOVE__}
Remove-SshHostKey -Directory '__DIR__'
Set-Service sshd -StartupType Automatic
Start-Service sshd
$key = Join-Path '__DIR__' 'ssh_host_ed25519_key'
for ($i = 0; $i -lt 15 -and -not (Test-Path -LiteralPath $key); $i++) { Start-Sleep -Seconds 1 }
if (-not (Test-Path -LiteralPath $key)) { throw 'sshd wrote no host key' }
Unregister-ScheduledTask -TaskName '__TASK__' -Confirm:$false
Remove-Item -LiteralPath $PSCommandPath -Force
'@

function Register-SshHostKeyFirstBoot {
    param([string]$Directory, [string]$TaskName)
    $file = Join-Path $Directory 'first-boot-host-keys.ps1'
    $text = $FirstBootScript.Replace('__REMOVE__', "${function:Remove-SshHostKey}")
    $text = $text.Replace('__DIR__', $Directory.Replace("'", "''")).Replace('__TASK__', $TaskName.Replace("'", "''"))
    Set-Content -LiteralPath $file -Value $text -Encoding UTF8
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$file`""
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null
    Set-Service sshd -StartupType Manual
}

if ($FunctionsOnly) { return }

# Fail closed: an absent path is fine, a present path that cannot be deleted
# stops the script (non-zero exit) so the image is never saved with it.
$sealPaths = @('C:\image-build', 'C:\ProgramData\ssh\administrators_authorized_keys')
foreach ($p in $sealPaths) {
    if (Test-Path -LiteralPath $p) {
        Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction Stop
    }
}

# 32 characters from an alphabet with every character class, drawn from the
# cryptographic RNG. The value is held in a SecureString and never output.
$alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%^&*-_=+'
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$secure = New-Object System.Security.SecureString
try {
    $byte = New-Object byte[] 1
    $limit = 256 - (256 % $alphabet.Length)   # reject values that would bias the pick
    while ($secure.Length -lt 32) {
        $rng.GetBytes($byte)
        if ($byte[0] -lt $limit) { $secure.AppendChar($alphabet[$byte[0] % $alphabet.Length]) }
    }
} finally {
    $rng.Dispose()
}
Set-LocalUser -Name 'Administrator' -Password $secure

# Forget the DHCP lease. Windows keeps it per interface in the registry and,
# at boot, asks to renew that address rather than discovering one; the clone
# has a new MAC on the same adapter, a DHCP server that is not authoritative
# (pve's dnsmasq) ignores the request, and Windows keeps the unexpired lease.
# Every clone of the image on one VLAN would then share the build VM's
# address. Without these values the next boot starts with DHCPDISCOVER. The
# address in use now is untouched, so the SSH session running this survives
# (`ipconfig /release` would cut it).
$leaseValues = @('DhcpIPAddress', 'DhcpSubnetMask', 'DhcpSubnetMaskOpt', 'DhcpServer',
    'DhcpDefaultGateway', 'DhcpNameServer', 'DhcpDomain', 'DhcpGatewayHardware',
    'DhcpGatewayHardwareCount', 'Lease', 'LeaseObtainedTime', 'LeaseTerminatesTime', 'T1', 'T2')
$interfaces = 'HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces'
foreach ($key in Get-ChildItem -Path $interfaces) {
    foreach ($name in $leaseValues) {
        if ($null -ne $key.GetValue($name)) {
            Remove-ItemProperty -Path $key.PSPath -Name $name -ErrorAction Stop
        }
    }
}

# In both modes. Without -KeepSsh sshd is disabled, so the service manager
# cannot restart it at the shutdown, and a clone's sshd writes its own keys if
# it is ever enabled. The running sshd holds its keys in memory, so this
# session is unaffected.
Remove-SshHostKey -Directory 'C:\ProgramData\ssh'

if ($KeepSsh) {
    Register-SshHostKeyFirstBoot -Directory 'C:\ProgramData\ssh' -TaskName 'image-build-ssh-host-keys'
} else {
    Set-Service sshd -StartupType Disabled
}

foreach ($p in $sealPaths) {
    if (Test-Path -LiteralPath $p) {
        [Console]::Error.WriteLine("seal_windows.ps1: $p still exists after removal")
        exit 1
    }
}
$cached = @(Get-ChildItem -Path $interfaces | Where-Object { $null -ne $_.GetValue('DhcpIPAddress') })
if ($cached.Count) {
    [Console]::Error.WriteLine("seal_windows.ps1: a DHCP lease is still cached for $($cached.PSChildName -join ', ')")
    exit 1
}
