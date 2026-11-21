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

# SIDs trusted with a SYSTEM-run script: SYSTEM, Administrators, TrustedInstaller.
$AdminSids = @('S-1-5-18', 'S-1-5-32-544', 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')
# Rights that let a principal change, replace or delete a file or directory,
# or grant itself that: WriteData/AddFile, AppendData/AddSubdirectory,
# WriteEA, DeleteChild, WriteAttributes, DELETE, WRITE_DAC, WRITE_OWNER, and
# the unmapped GENERIC_ALL and GENERIC_WRITE an ACE may carry.
$WriteMask = 0x2 -bor 0x4 -bor 0x10 -bor 0x40 -bor 0x100 -bor 0x10000 -bor 0x40000 -bor 0x80000 -bor 0x10000000 -bor 0x40000000
# On the parent directory, what lets a principal delete or rename a child (or
# grant itself that). C:\ProgramData lets Users create entries, not delete them.
$ParentMask = 0x40 -bor 0x40000 -bor 0x80000 -bor 0x10000000

# Findings for one security descriptor (SDDL): an owner that is not trusted
# (an owner may always rewrite the DACL), no DACL at all, an ACE type it cannot
# read, and every allow ACE for any other SID holding a right in $Mask. Deny
# ACEs are not subtracted, so this is stricter than Windows' effective access:
# a finding may in fact be denied, a grant is never missed. -EffectiveOnly
# skips inherit-only ACEs (they do not apply to the object itself).
function Get-NonAdminWriteAccess {
    param([string]$Sddl, [string]$Label, [int]$Mask = $WriteMask, [switch]$EffectiveOnly, [string[]]$Trusted = $AdminSids)
    $sd = New-Object System.Security.AccessControl.RawSecurityDescriptor $Sddl
    if ($sd.Owner -and $Trusted -notcontains $sd.Owner.Value) { "${Label}: owner $($sd.Owner.Value)" }
    if ($null -eq $sd.DiscretionaryAcl) { "${Label}: no DACL"; return }
    foreach ($ace in $sd.DiscretionaryAcl) {
        if ($ace -isnot [System.Security.AccessControl.KnownAce]) { "${Label}: unreadable ACE $($ace.AceType)"; continue }
        if ($ace.AceQualifier -ne [System.Security.AccessControl.AceQualifier]::AccessAllowed) { continue }
        if ($EffectiveOnly -and ([int]$ace.AceFlags -band [int][System.Security.AccessControl.AceFlags]::InheritOnly)) { continue }
        $sid = $ace.SecurityIdentifier.Value
        $hit = $ace.AccessMask -band $Mask
        if ($hit -and $Trusted -notcontains $sid) { '{0}: {1} has 0x{2:x8}' -f $Label, $sid, $hit }
    }
}

# Fail unless only trusted principals can change $Path or remove it from its parent.
function Assert-AdminOnlyPath {
    param([string]$Path, [string[]]$Trusted = $AdminSids)
    $parent = Split-Path -Parent $Path
    $findings = @(Get-NonAdminWriteAccess -Sddl (Get-Acl -LiteralPath $Path).Sddl -Label $Path -Trusted $Trusted) +
        @(Get-NonAdminWriteAccess -Sddl (Get-Acl -LiteralPath $parent).Sddl -Label "$parent (parent)" -Mask $ParentMask -EffectiveOnly -Trusted $Trusted)
    if ($findings.Count) { throw "not administrators only: $($findings -join '; ')" }
}

# Owner Administrators ($AdminSid), a protected DACL (nothing inherited): SYSTEM
# and Administrators full control, inherited by a directory's children; no one
# else. (The tests, which do not run elevated, pass their own SID as $AdminSid.)
function Protect-AdminOnlyPath {
    param([string]$Path, [string]$AdminSid = 'S-1-5-32-544')
    $inherit = if ((Get-Item -LiteralPath $Path -Force).PSIsContainer) { 'OICI' } else { '' }
    $acl = Get-Acl -LiteralPath $Path
    $acl.SetSecurityDescriptorSddlForm("O:${AdminSid}D:P(A;$inherit;FA;;;SY)(A;$inherit;FA;;;$AdminSid)",
        [System.Security.AccessControl.AccessControlSections]'Owner, Access')
    Set-Acl -LiteralPath $Path -AclObject $acl
}

# The task's own security descriptor, through the Task Scheduler API (what the
# scheduler enforces on anyone who would change or delete the task).
function Get-TaskSddl {
    param([string]$TaskName)
    $s = New-Object -ComObject Schedule.Service; $s.Connect()
    $s.GetFolder('\').GetTask($TaskName).GetSecurityDescriptor(5)   # owner + DACL
}
function Set-TaskSddl {
    param([string]$TaskName, [string]$Sddl)
    $s = New-Object -ComObject Schedule.Service; $s.Connect()
    $s.GetFolder('\').GetTask($TaskName).SetSecurityDescriptor($Sddl, 0)
}

# With -KeepSsh, deleting the keys in the seal is not enough: sshd keeps
# running until the save shuts the VM down, and Win32-OpenSSH 10.0 (the
# Server 2016 base) registers sshd with "restart on failure". sshd exits
# uncleanly at the shutdown, the service manager restarts it, and it writes
# new keys into the image (measured on pve2: two clones then shared them).
# So sshd is left Manual, and a startup task runs this script as SYSTEM on
# the clone's first boot. It records the image's key fingerprints, deletes the
# keys, makes new ones with `ssh-keygen -A` (sshd stopped), checks that every
# type exists and differs from the image's, records success, and only then
# sets sshd Automatic and starts it. On any failure sshd is left Manual and
# stopped, the failure is logged, and the task stays for the next boot, up to
# 5 attempts. After a success the keys are never touched again. On success the
# task is unregistered first and the script deleted second: a task left
# behind would point at a path, while a script left behind is inert in a
# directory only administrators can write.
$FirstBootScript = @'
# First boot of a clone of a KEEP_SSH=yes image, written by seal_windows.ps1:
# this clone's own sshd host keys, then sshd on, then this step removed.
$ErrorActionPreference = 'Stop'
$ssh = '__SSH__'; $dir = '__DIR__'; $task = '__TASK__'; $keygen = '__KEYGEN__'
$maxAttempts = 5
$types = 'rsa', 'ecdsa', 'ed25519'
$done = Join-Path $dir 'keys-generated'
$imageKeys = Join-Path $dir 'image-fingerprints'
$attempts = Join-Path $dir 'attempts'
function Log($m) { Add-Content -LiteralPath (Join-Path $dir 'first-boot.log') -Value ('{0:u} {1}' -f (Get-Date).ToUniversalTime(), $m) }
function Fingerprint($pub) {
    $blob = [Convert]::FromBase64String(((Get-Content -LiteralPath $pub -TotalCount 1) -split '\s+')[1])
    'SHA256:' + [Convert]::ToBase64String([Security.Cryptography.SHA256]::Create().ComputeHash($blob)).TrimEnd('=')
}
function Remove-SshHostKey {__REMOVE__}
function Stop-Sshd {
    Set-Service sshd -StartupType Manual
    if ((Get-Service sshd).Status -ne 'Stopped') { Stop-Service sshd -Force }
}
try {
    $n = 1 + [int](@(Get-Content -LiteralPath $attempts -ErrorAction SilentlyContinue) | Select-Object -First 1)
    Set-Content -LiteralPath $attempts -Value $n
    if ($n -gt $maxAttempts) {
        Log "giving up after $maxAttempts attempts; sshd stays Manual and stopped"
        Stop-Sshd
        Unregister-ScheduledTask -TaskName $task -Confirm:$false
        Remove-Item -LiteralPath $PSCommandPath -Force
        exit 1
    }
    if (Test-Path -LiteralPath $done) {
        Log "attempt ${n}: keys were made on an earlier attempt; not touched"
    } else {
        Stop-Sshd
        $old = @(Get-ChildItem -Path (Join-Path $ssh 'ssh_host_*_key.pub') -Force | ForEach-Object { Fingerprint $_.FullName })
        if ($old.Count) { Add-Content -LiteralPath $imageKeys -Value $old }
        Remove-SshHostKey -Directory $ssh
        $ErrorActionPreference = 'Continue'   # Windows PowerShell turns native stderr into errors
        $out = & $keygen -A 2>&1
        $rc = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        if ($rc -ne 0) { throw "ssh-keygen -A exited ${rc}: $out" }
        $image = @(Get-Content -LiteralPath $imageKeys -ErrorAction SilentlyContinue)
        $new = foreach ($t in $types) {
            $pub = Join-Path $ssh "ssh_host_${t}_key.pub"
            if (-not (Test-Path -LiteralPath $pub) -or -not (Test-Path -LiteralPath (Join-Path $ssh "ssh_host_${t}_key"))) {
                throw "ssh-keygen -A wrote no $t key"
            }
            $fp = Fingerprint $pub
            if ($image -contains $fp) { throw "the new $t key is the image's ($fp)" }
            $fp
        }
        Set-Content -LiteralPath $done -Value $new
        Log "attempt ${n}: new host keys $($new -join ' ')"
    }
    Set-Service sshd -StartupType Automatic
    Start-Service sshd
    Unregister-ScheduledTask -TaskName $task -Confirm:$false
    Remove-Item -LiteralPath $PSCommandPath -Force
    Log "attempt ${n}: sshd Automatic and running; task and script removed"
} catch {
    Log "attempt ${n} failed: $($_.Exception.Message)"
    try { Stop-Sshd } catch { Log "and sshd could not be left Manual and stopped: $($_.Exception.Message)" }
    exit 1
}
'@

# The first-boot script for these paths, as text.
function Get-FirstBootScript {
    param([string]$SshDirectory, [string]$Directory, [string]$TaskName, [string]$Keygen)
    $text = $FirstBootScript.Replace('__REMOVE__', "${function:Remove-SshHostKey}")
    foreach ($pair in @(@('__SSH__', $SshDirectory), @('__DIR__', $Directory), @('__TASK__', $TaskName), @('__KEYGEN__', $Keygen))) {
        $text = $text.Replace($pair[0], $pair[1].Replace("'", "''"))
    }
    $text
}

# Write the first-boot script into $Directory, which only SYSTEM and
# Administrators can change (checked, as is the script), register the task
# that runs it as SYSTEM at startup with a security descriptor that only they
# can change (checked through the Task Scheduler and on the task's file), and
# leave sshd Manual. Any check that fails stops the seal.
function Register-SshHostKeyFirstBoot {
    param(
        [string]$SshDirectory, [string]$Directory, [string]$TaskName, [string]$Keygen,
        [string]$AdminSid = 'S-1-5-32-544', [string[]]$Trusted = $AdminSids,
        [string]$TaskFile = "$env:windir\System32\Tasks\$TaskName"
    )
    if (Test-Path -LiteralPath $Directory) { Remove-Item -LiteralPath $Directory -Recurse -Force }
    New-Item -ItemType Directory -Path $Directory | Out-Null
    Protect-AdminOnlyPath -Path $Directory -AdminSid $AdminSid
    $file = Join-Path $Directory 'first-boot-host-keys.ps1'
    Set-Content -LiteralPath $file -Encoding UTF8 -Value (Get-FirstBootScript -SshDirectory $SshDirectory `
        -Directory $Directory -TaskName $TaskName -Keygen $Keygen)
    Protect-AdminOnlyPath -Path $file -AdminSid $AdminSid
    Assert-AdminOnlyPath -Path $Directory -Trusted $Trusted
    Assert-AdminOnlyPath -Path $file -Trusted $Trusted

    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$file`""
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null
    Set-TaskSddl -TaskName $TaskName -Sddl 'D:P(A;;FA;;;SY)(A;;FA;;;BA)'
    $task = Get-ScheduledTask -TaskName $TaskName
    $actions = @($task.Actions)
    if ($task.Principal.UserId -notin 'SYSTEM', 'NT AUTHORITY\SYSTEM' -or "$($task.Principal.RunLevel)" -ne 'Highest' -or
        $actions.Count -ne 1 -or $actions[0].Execute -ne 'powershell.exe' -or $actions[0].Arguments -notlike "*-File `"$file`"") {
        throw "task $TaskName does not run $file as SYSTEM"
    }
    $findings = @(Get-NonAdminWriteAccess -Sddl (Get-TaskSddl -TaskName $TaskName) -Label "task $TaskName" -Trusted $Trusted)
    if ($findings.Count) { throw "not administrators only: $($findings -join '; ')" }
    Assert-AdminOnlyPath -Path $TaskFile -Trusted $Trusted
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
    $keygen = Join-Path (Split-Path -Parent (Get-CimInstance Win32_Service -Filter "Name='sshd'").PathName.Trim('"')) 'ssh-keygen.exe'
    Register-SshHostKeyFirstBoot -SshDirectory 'C:\ProgramData\ssh' -Directory 'C:\ProgramData\image-build-first-boot' -TaskName 'image-build-ssh-host-keys' -Keygen $keygen
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
