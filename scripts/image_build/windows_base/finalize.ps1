# Build step "finalize" of the Windows base (proxmox_base_windows.sh), run as
# SYSTEM through step.ps1, just before sysprep. Leaves the base with:
#   - no SSH host keys, no authorized keys; sshd disabled until a clone's
#     first-boot setup has finished (SetupComplete.cmd then enables and starts
#     it: before that, the clone still reboots once);
#   - the sysprep answer file (no password in it) and randomize-admin.ps1,
#     which gives each clone's Administrator its own random password;
#   - no automatic logon, empty event logs.
# -FunctionsOnly defines the functions below and returns before any step runs
# (the tests dot-source it that way).
param([switch]$FunctionsOnly)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
function Step($m) { Write-Output "== $m" }
$work = 'C:\basebuild'
$ssh = 'C:\ProgramData\ssh'

# Packages installed for a user but not provisioned (sysprep /generalize
# refuses them, 0x80073CF2) that Windows allows to be removed. The cumulative
# update installs Microsoft Edge (Microsoft.MicrosoftEdge.Stable) for
# Administrator only, which is such a package. Windows Server 2016 (14393)
# has no NonRemovable property on its packages: there, its system apps
# (signature kind System, or installed under the Windows directory, such as
# Microsoft.AAD.BrokerPlugin) are part of Windows, do not stop sysprep and are
# left alone.
function Get-StrayAppxPackage {
    param([object[]]$Packages, [string[]]$ProvisionedNames)
    $sample = @($Packages) | Select-Object -First 1
    $hasNonRemovable = [bool]($sample -and $sample.PSObject.Properties['NonRemovable'])
    @(@($Packages) | Where-Object {
        $removable = if ($hasNonRemovable) { -not $_.NonRemovable } else {
            $_.SignatureKind -ne 'System' -and -not "$($_.InstallLocation)".StartsWith($env:windir, [StringComparison]::OrdinalIgnoreCase)
        }
        -not $_.IsFramework -and $removable -and @($ProvisionedNames) -notcontains $_.Name -and
        ($_.PackageUserInformation | Where-Object { $_.InstallState -eq 'Installed' })
    })
}

# Remove them for every user. Windows Server 2016's Remove-AppxPackage has no
# -AllUsers: a stray package there fails the step by name instead.
function Remove-StrayAppxPackage {
    param([object[]]$Stray)
    $Stray = @($Stray)
    if ($Stray.Count -and -not (Get-Command Remove-AppxPackage).Parameters.ContainsKey('AllUsers')) {
        throw "Remove-AppxPackage has no -AllUsers here; remove by hand: $($Stray.PackageFullName -join ', ')"
    }
    foreach ($p in $Stray) {
        Write-Output "removing $($p.PackageFullName)"
        Remove-AppxPackage -Package $p.PackageFullName -AllUsers
    }
    Write-Output "$($Stray.Count) package(s) removed"
}

# sshd stopped and disabled. Disabled first, so nothing starts it again; then
# a pending state is waited out (up to -PendingTimeoutSeconds), because a
# service that is still starting refuses a stop. On Server 2016 with the
# Win32-OpenSSH MSI a plain Stop-Service failed ("Cannot stop sshd service")
# about two minutes after a boot; the line printed first records the state.
function Stop-SshdForSysprep {
    param([int]$PendingTimeoutSeconds = 300)
    Set-Service -Name sshd -StartupType Disabled
    $svc = Get-Service -Name sshd
    Write-Output "sshd before the stop: $($svc.Status), start type $($svc.StartType)"
    $deadline = (Get-Date).AddSeconds($PendingTimeoutSeconds)
    while ("$((Get-Service -Name sshd).Status)" -like '*Pending' -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 5 }
    if ("$((Get-Service -Name sshd).Status)" -ne 'Stopped') { Stop-Service -Name sshd }
    (Get-Service -Name sshd).WaitForStatus('Stopped', [TimeSpan]::FromMinutes(1))
}

if ($FunctionsOnly) { return }

Step 'no reboot pending (sysprep refuses one)'
$pending = @(
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending',
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired'
) | Where-Object { Test-Path $_ }
if ($pending) { throw "a reboot is pending: $($pending -join ', ')" }

Step 'apps installed for a user but not provisioned (sysprep refuses them, 0x80073CF2)'
$stray = Get-StrayAppxPackage -Packages @(Get-AppxPackage -AllUsers) `
    -ProvisionedNames @(Get-AppxProvisionedPackage -Online | ForEach-Object { $_.DisplayName })
Remove-StrayAppxPackage -Stray $stray

Step 'licence: sysprep /generalize needs a rearm left'
$dlv = (& cscript.exe //nologo C:\Windows\System32\slmgr.vbs /dlv) -join "`n"
Write-Output $dlv
if ($dlv -match 'Remaining Windows rearm count:\s*(\d+)' -and [int]$Matches[1] -lt 1) {
    throw 'no Windows rearm left; sysprep /generalize would fail'
}

Step 'sshd: disabled until first-boot setup finishes; no host or authorized keys'
Stop-SshdForSysprep
Remove-Item -Force -ErrorAction SilentlyContinue "$ssh\ssh_host_*", "$ssh\administrators_authorized_keys"
Get-ChildItem -Path 'C:\Users\*\.ssh\authorized_keys' -Force -ErrorAction SilentlyContinue | Remove-Item -Force
$left = @(Get-ChildItem -Path "$ssh\ssh_host_*", "$ssh\administrators_authorized_keys" -ErrorAction SilentlyContinue)
if ($left.Count) { throw "still present: $($left.FullName -join ', ')" }
Get-Service sshd, QEMU-GA | Format-Table Name, Status, StartType

Step 'SetupComplete.cmd and randomize-admin.ps1'
New-Item -ItemType Directory -Force -Path 'C:\Windows\Setup\Scripts' | Out-Null
# Set-Content writes CRLF line ends, which cmd.exe expects.
Get-Content -Path "$work\SetupComplete.cmd" | Set-Content -Path 'C:\Windows\Setup\Scripts\SetupComplete.cmd' -Encoding Ascii
Copy-Item -Path "$work\randomize-admin.ps1" -Destination 'C:\Windows\Setup\Scripts\randomize-admin.ps1' -Force

Step 'sysprep answer file (no password in it)'
$xml = Get-Content -Raw -Path "$work\sysprep-unattend.xml"
[xml]$xml | Out-Null
if ($xml -match '<(Administrator)?Password>|<Value>') { throw 'sysprep-unattend.xml must not carry a password' }
Set-Content -Path 'C:\Windows\System32\Sysprep\base-unattend.xml' -Value $xml -Encoding UTF8
# Setup's own copies of the install answer file (setup masks their passwords;
# nothing needs them again).
Remove-Item -Force -ErrorAction SilentlyContinue 'C:\Windows\Panther\unattend.xml', 'C:\Windows\Panther\Unattend\unattend.xml', 'C:\Windows\Panther\autounattend.xml'

Step 'no automatic logon'
$winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
Set-ItemProperty -Path $winlogon -Name AutoAdminLogon -Value '0'
foreach ($n in 'DefaultPassword', 'AutoLogonCount') {
    Remove-ItemProperty -Path $winlogon -Name $n -ErrorAction SilentlyContinue
}

Step 'event logs'
# Some logs cannot be cleared; Windows PowerShell turns a redirected native
# stderr line into a terminating error under 'Stop'.
$ErrorActionPreference = 'Continue'
foreach ($log in (& wevtutil.exe el)) { & wevtutil.exe cl "$log" 2>$null }
$ErrorActionPreference = 'Stop'
Write-Output 'finalized'
