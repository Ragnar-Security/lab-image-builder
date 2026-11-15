# Azure backend for build_image.sh (sourced; see the contract in its header).
#
# Every build gets its own resource group, named after the run ID
# (ib-<yyyymmddhhmm>-<16 hex>), in AZ_BUILD_LOCATION and tagged
# image-build=<run ID>, image-build-expires=<yyyymmddhhmm UTC>,
# image-build-owner=<nonce> and image-build-creating=<nonce>, where <nonce>
# is a random 128-bit value made for this run. A fail-closed read refuses a
# group of that name that already exists; then a PUT (`az rest`; Azure
# ignores If-None-Match on resource groups, so a PUT is not create-only)
# carries only the creation token image-build-creating=<nonce>, which no reap
# matches; this run owns the group, and merges on the other three tags, only
# when Azure answered 201 Created and the read-back shows all four (see
# _az_create_group). The build VM (same name), its NIC, OS disk and
# public IP all live in that group, so a destroy or a reap deletes the group,
# and only after re-reading its tags: both require image-build-creating ==
# image-build-owner (the cleanup: == this run's nonce), so a same-name group
# that only received this run's Merge is never deleted. The NIC sits in the
# operator's build subnet (AZ_BUILD_SUBNET_ID), whose NSG admits tcp/22 from
# the build host only; no NIC-level NSG is created.
#
# The image is a specialized image version (no generalize, no sysprep) in the
# operator's Azure Compute Gallery (AZ_GALLERY in AZ_GALLERY_RG), under the
# image definition IMAGE_PREFIX, which is created when missing. The version is
# <yyyy>.<mmdd>.<hhmm> of the build's timestamp (a gallery version is three
# integers) and carries the image-build-recipe and image-build-commit tags.
#
# Config: AZ_SUBSCRIPTION_ID (every call names it; the CLI's default
#         subscription never decides), AZ_BUILD_LOCATION, AZ_BUILD_SUBNET_ID,
#         AZ_GALLERY_RG, AZ_GALLERY.
# Recipe: BASE_AZURE (a marketplace URN publisher:offer:sku:version, version
#         may be `latest`, or a gallery image-version resource ID),
#         AZURE_VM_SIZE, DISK_GB (AZURE_DISK_GB overrides it here: Azure's
#         Windows marketplace images are 127 GB).
#
# The build key:
#   Linux    `builder` is the VM's admin user, created by Azure with the key
#            (--ssh-key-values, a file) and passwordless sudo.
#   Windows  Azure injects no SSH key into Windows. The VM's admin `builder`
#            gets a random password made here, written to the core's per-run
#            temp dir and passed as az's @file, so it is never on a command
#            line, printed or kept. Azure gives that name to the built-in
#            Administrator (RID 500), so there is no separate `builder`
#            account. Then `az vm run-command invoke` runs the GCP backend's
#            OpenSSH setup (install OpenSSH Server, write the key to
#            administrators_authorized_keys, start sshd, open tcp/22), which
#            also renames the RID-500 account back to Administrator and gives
#            it a random password made in the guest. The build logs in as
#            Administrator, as on AWS and GCP.
#   A specialized gallery base (an image this backend built with KEEP_SSH=yes)
#   is created with --specialized. az 2.92 still demands a credential (the
#   run's key, or the generated password) and then leaves it out of the VM,
#   so run-command writes the key (Linux: builder's authorized_keys; Windows:
#   the same OpenSSH setup).
#
# Reboots (setup exit 100) go through `az vm restart`; the core then waits for
# a new boot ID over SSH as on every backend.

# The config keys this backend reads (the core imports only these).
BACKEND_CONFIG_PREFIX=AZ_
# Recipe keys this backend needs beyond the core's required ones.
BACKEND_RECIPE_KEYS="AZURE_VM_SIZE"

_AZ_RUN_ID_RE='^ib-[0-9]{12}-[0-9a-f]{16}$'
# The only error code a gallery read accepts as "does not exist" (with exit
# 3): az 2.92 answers a missing gallery, definition or version with exactly
# ResourceNotFound (seen live, 2026-11-15). Anything else (SubscriptionNotFound,
# AuthorizationFailed, a tenant or parse error, an unknown code) fails the read.
_AZ_GALLERY_ABSENT="ResourceNotFound"
# The resource-group API version of the group PUT.
_AZ_RG_API=2021-04-01
_AZ_GUID_RE='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
_AZ_URN_RE='^[A-Za-z0-9._-]+:[A-Za-z0-9._-]+:[A-Za-z0-9._-]+:[A-Za-z0-9._-]+$'
# Matched case-insensitively (_az_match_ci): Azure resource IDs are.
_AZ_SUBNET_RE='^/subscriptions/([0-9a-f-]{36})/resourceGroups/[a-z0-9._()-]+/providers/Microsoft\.Network/virtualNetworks/[a-z0-9._-]+/subnets/[a-z0-9._-]+$'
_AZ_VERSION_ID_RE='^/subscriptions/([0-9a-f-]{36})/resourceGroups/([a-z0-9._()-]+)/providers/Microsoft\.Compute/galleries/([a-z0-9._]+)/images/([a-z0-9._-]+)/versions/([0-9]+\.[0-9]+\.[0-9]+)$'
# One OpenSSH public key line, with no quote (it is embedded in single-quoted
# PowerShell and sh strings).
_AZ_PUBKEY_RE='^ssh-[a-z0-9-]+ [A-Za-z0-9+/=]+( [A-Za-z0-9@._:+=-]+)?$'
# The run-command scripts print this as their last step; run-command itself
# reports success even when the script failed.
_AZ_READY=image-build-ssh-ready

# Facts of the base, read by backend_start and used again by backend_save.
_AZ_BASE_OS=""
_AZ_BASE_STATE=""
_AZ_BASE_GEN=""
# This run's image-build-owner tag, set by backend_start.
_AZ_NONCE=""

# Print the regex's capture groups, one per line, when $1 matches $2 ignoring
# case; non-zero otherwise. In a subshell, so nocasematch does not leak.
_az_match_ci() {
  (
    shopt -s nocasematch
    [[ "$1" =~ $2 ]] || exit 1
    printf '%s\n' "${BASH_REMATCH[@]:1}"
  )
}

_az_lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# Is $1 a real UTC calendar minute, yyyymmddhhmm, from year 2000 on? (A
# 12-digit string such as 000000000000 or 202602300000 compares as long
# expired, so the reap must never take digits alone for an expiry.)
_az_valid_minute() {
  local y m d hh mm dim
  [[ "$1" =~ ^([0-9]{4})([0-9]{2})([0-9]{2})([0-9]{2})([0-9]{2})$ ]] || return 1
  y=$((10#${BASH_REMATCH[1]})) m=$((10#${BASH_REMATCH[2]})) d=$((10#${BASH_REMATCH[3]}))
  hh=$((10#${BASH_REMATCH[4]})) mm=$((10#${BASH_REMATCH[5]}))
  [ "$y" -ge 2000 ] && [ "$m" -ge 1 ] && [ "$m" -le 12 ] && [ "$d" -ge 1 ] \
    && [ "$hh" -le 23 ] && [ "$mm" -le 59 ] || return 1
  case "$m" in
    2) if [ $((y % 4)) -eq 0 ] && { [ $((y % 100)) -ne 0 ] || [ $((y % 400)) -eq 0 ]; }; then
         dim=29
       else
         dim=28
       fi ;;
    4|6|9|11) dim=30 ;;
    *) dim=31 ;;
  esac
  [ "$d" -le "$dim" ]
}

# Run one read through `query`: `_az_read <placeholder> <what> <absent codes>
# <cmd...>`. Sets _AZ_OUT to its output (CRs removed) and _AZ_FOUND to 1; or
# _AZ_FOUND to 0 when az exits 3 and every error code it prints is one of
# <absent codes> (space-separated, exact). Any other failure dies. In a dry
# run, the placeholder @absent reads as not found.
_AZ_OUT=""
_AZ_FOUND=0
_az_read() {
  local placeholder="$1" what="$2" absent=" $3 " err rc=0 codes code ok
  shift 3
  err="$(mktemp "${TMPDIR:-/tmp}/az-read.XXXXXX")"
  _AZ_OUT="$(query "$placeholder" "$@" 2>"$err")" || rc=$?
  _AZ_OUT="${_AZ_OUT//$'\r'/}"
  if [ "$rc" -eq 0 ]; then
    cat "$err" >&2
    rm -f "$err"
    _AZ_FOUND=1
    if [ "$DRY_RUN" = 1 ] && [ "${QUERY_LIVE:-0}" != 1 ] && [ "$_AZ_OUT" = @absent ]; then
      _AZ_OUT=""
      _AZ_FOUND=0
    fi
    return 0
  fi
  codes="$(tr -d '\r' < "$err" | sed -n -e 's/^ERROR: (\([A-Za-z0-9._]*\)).*/\1/p' -e 's/^Code: \([A-Za-z0-9._]*\)$/\1/p' | sort -u)"
  ok=0
  if [ "$rc" -eq 3 ] && [ -n "$codes" ]; then
    ok=1
    for code in $codes; do
      [[ "$absent" == *" $code "* ]] || ok=0
    done
  fi
  if [ "$ok" = 1 ]; then
    rm -f "$err"
    _AZ_OUT=""
    _AZ_FOUND=0
    return 0
  fi
  cat "$err" >&2
  rm -f "$err"
  die "azure: cannot read $what (az exit $rc${codes:+, error ${codes//$'\n'/ }}); not treated as absent"
}

backend_check_config() {
  local rc=0 sub
  if ! [[ "${AZ_SUBSCRIPTION_ID:-}" =~ $_AZ_GUID_RE ]]; then
    echo "azure: AZ_SUBSCRIPTION_ID must be a subscription ID, a GUID (got '${AZ_SUBSCRIPTION_ID:-}')" >&2
    rc=1
  fi
  if ! [[ "${AZ_BUILD_LOCATION:-}" =~ ^[a-z][a-z0-9]+$ ]]; then
    echo "azure: AZ_BUILD_LOCATION must be a location name such as eastus (got '${AZ_BUILD_LOCATION:-}')" >&2
    rc=1
  fi
  if sub="$(_az_match_ci "${AZ_BUILD_SUBNET_ID:-}" "$_AZ_SUBNET_RE")"; then
    # A NIC joins a subnet of its own subscription only.
    if [ "$(_az_lower "$sub")" != "$(_az_lower "${AZ_SUBSCRIPTION_ID:-}")" ]; then
      echo "azure: AZ_BUILD_SUBNET_ID is in subscription $sub, not AZ_SUBSCRIPTION_ID" >&2
      rc=1
    fi
  else
    echo "azure: AZ_BUILD_SUBNET_ID must be a subnet resource ID, /subscriptions/.../virtualNetworks/<vnet>/subnets/<subnet> (got '${AZ_BUILD_SUBNET_ID:-}')" >&2
    rc=1
  fi
  if ! [[ "${AZ_GALLERY_RG:-}" =~ ^[A-Za-z0-9._()-]{0,89}[A-Za-z0-9_()-]$ ]]; then
    echo "azure: AZ_GALLERY_RG must be a resource group name (got '${AZ_GALLERY_RG:-}')" >&2
    rc=1
  fi
  if ! [[ "${AZ_GALLERY:-}" =~ ^[A-Za-z0-9][A-Za-z0-9._]{0,79}$ ]]; then
    echo "azure: AZ_GALLERY must be a gallery name: letters, digits, '.' and '_' (got '${AZ_GALLERY:-}')" >&2
    rc=1
  fi
  # Unset under --reap (no recipe). Checked here so a bad size fails before
  # anything is created.
  if [ -n "${AZURE_VM_SIZE:-}" ] && ! [[ "$AZURE_VM_SIZE" =~ ^[A-Za-z][A-Za-z0-9_]{1,63}$ ]]; then
    echo "azure: AZURE_VM_SIZE must look like Standard_B2s (got '$AZURE_VM_SIZE')" >&2
    rc=1
  fi
  if [ "$DRY_RUN" != 1 ] && ! command -v az >/dev/null 2>&1; then
    echo "azure: the Azure CLI (az) is not on PATH" >&2
    rc=1
  fi
  return "$rc"
}

# A URN -> the same URN with `latest` resolved to the newest version.
# A gallery image-version ID -> itself, once it is seen provisioned.
backend_resolve_base() {
  local pattern="$1" ver out rc=0 parts=()
  if [[ "$pattern" =~ $_AZ_URN_RE ]]; then
    ver="${pattern##*:}"
    if [ "$ver" = latest ]; then
      ver=0.0.0
    fi
    out="$(query "$ver" az vm image show --subscription "$AZ_SUBSCRIPTION_ID" \
      --location "$AZ_BUILD_LOCATION" --urn "$pattern" --query name --output tsv)" || rc=$?
    out="${out//$'\r'/}"
    if [ "$rc" -ne 0 ] || ! [[ "$out" =~ ^[A-Za-z0-9._-]+$ ]]; then
      die "azure: no marketplace image $pattern in $AZ_BUILD_LOCATION (BASE_AZURE=$pattern)"
    fi
    printf '%s\n' "${pattern%:*}:$out"
  elif out="$(_az_match_ci "$pattern" "$_AZ_VERSION_ID_RE")"; then
    read -r -d '' -a parts <<< "$out" || true
    out="$(query Succeeded az sig image-version show --subscription "${parts[0]}" \
      --resource-group "${parts[1]}" --gallery-name "${parts[2]}" \
      --gallery-image-definition "${parts[3]}" --gallery-image-version "${parts[4]}" \
      --query provisioningState --output tsv)" || rc=$?
    out="${out//$'\r'/}"
    if [ "$rc" -ne 0 ] || [ "$out" != Succeeded ]; then
      die "azure: no usable gallery image version $pattern (state '${out}', az exit $rc; BASE_AZURE=$pattern)"
    fi
    printf '%s\n' "$pattern"
  else
    die "azure: BASE_AZURE must be a marketplace URN publisher:offer:sku:version or a gallery image-version ID /subscriptions/.../galleries/<gallery>/images/<definition>/versions/<X.Y.Z> (got '$pattern')"
  fi
}

# Set _AZ_BASE_OS (Linux|Windows), _AZ_BASE_STATE (Generalized|Specialized)
# and _AZ_BASE_GEN (V1|V2) for a resolved base. A marketplace image is
# generalized; a gallery image's facts are its definition's.
_az_base_facts() {
  local base="$1" ostype row parts=() os state gen
  ostype=Linux
  if [ "$OS_FAMILY" = windows ]; then
    ostype=Windows
  fi
  if [[ "$base" =~ $_AZ_URN_RE ]]; then
    row="$(query "$ostype"$'\t'V2 az vm image show --subscription "$AZ_SUBSCRIPTION_ID" \
      --location "$AZ_BUILD_LOCATION" --urn "$base" \
      --query '[[osDiskImage.operatingSystem, hyperVGeneration]]' --output tsv)"
    IFS=$'\t' read -r os gen <<< "${row//$'\r'/}"
    state=Generalized
  else
    row="$(_az_match_ci "$base" "$_AZ_VERSION_ID_RE")" || die "azure: base '$base' is neither a URN nor a gallery image-version ID"
    read -r -d '' -a parts <<< "$row" || true
    row="$(query "$ostype"$'\t'Generalized$'\t'V2 az sig image-definition show \
      --subscription "${parts[0]}" --resource-group "${parts[1]}" --gallery-name "${parts[2]}" \
      --gallery-image-definition "${parts[3]}" \
      --query '[[osType, osState, hyperVGeneration]]' --output tsv)"
    IFS=$'\t' read -r os state gen <<< "${row//$'\r'/}"
  fi
  # An image that names no generation is generation 1.
  case "$gen" in
    V1|V2) ;;
    ''|None) gen=V1 ;;
    *) die "azure: base $base has Hyper-V generation '$gen', not V1 or V2" ;;
  esac
  case "$state" in
    Generalized|Specialized) ;;
    *) die "azure: base $base has OS state '$state', not Generalized or Specialized" ;;
  esac
  [ "$os" = "$ostype" ] || die "azure: base $base is a '$os' image; the recipe's OS_FAMILY is $OS_FAMILY"
  _AZ_BASE_OS="$os"
  _AZ_BASE_STATE="$state"
  _AZ_BASE_GEN="$gen"
}

# The Windows run-command script: the GCP backend's startup script (OpenSSH,
# the key, tcp/22), the RID-500 account renamed back to Administrator with a
# password made in the guest, plus the completion marker.
_az_windows_ssh_script() {
  printf "\$buildKey = '%s'\n" "$1"
  cat <<'PS1'
$ErrorActionPreference = 'Stop'

$cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'
if ($cap.State -ne 'Installed') {
    Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' | Out-Null
}
# The first start creates C:\ProgramData\ssh, sshd_config and the host keys.
Start-Service -Name sshd
Set-Service -Name sshd -StartupType Automatic

# sshd ignores administrators_authorized_keys unless only Administrators and
# SYSTEM can access it (SIDs, so a localised group name does not matter).
$auth = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
Set-Content -LiteralPath $auth -Value $buildKey -Encoding ascii
icacls.exe $auth /inheritance:r /grant '*S-1-5-32-544:F' /grant '*S-1-5-18:F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw "icacls failed on $auth" }

# Azure renames the built-in Administrator (RID 500) to the VM's admin user
# (builder) at provisioning, so no account is called Administrator (seen live,
# 2026-11-15). Name it Administrator again, as on AWS and GCP: the build logs
# in as Administrator, and the seal and the payloads use that name. Then give
# it a random password, generated here and never output (the seal replaces it
# again), and enable it. A specialized base this backend built already has an
# enabled Administrator and keeps it as it is.
$admin = Get-LocalUser | Where-Object { $_.SID.Value.EndsWith('-500') }
if (-not $admin) { throw 'no built-in administrator (RID 500) found' }
$renamed = $admin.Name -ne 'Administrator'
if ($renamed) { Rename-LocalUser -SID $admin.SID -NewName 'Administrator' }
if ($renamed -or -not $admin.Enabled) {
    $alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%^&*-_=+'
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $secure = New-Object System.Security.SecureString
    try {
        $byte = New-Object byte[] 1
        $limit = 256 - (256 % $alphabet.Length)
        while ($secure.Length -lt 32) {
            $rng.GetBytes($byte)
            if ($byte[0] -lt $limit) { $secure.AppendChar($alphabet[$byte[0] % $alphabet.Length]) }
        }
    } finally {
        $rng.Dispose()
    }
    Set-LocalUser -Name 'Administrator' -Password $secure
    Enable-LocalUser -Name 'Administrator'
}

if (-not (Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' `
        -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
}
PS1
  printf "Write-Output '%s'\n" "$_AZ_READY"
}

# The Linux run-command script, for a specialized base only: it already has
# `builder` (passwordless sudo, an emptied authorized_keys) and sshd on.
_az_linux_ssh_script() {
  printf "set -eu\nkey='%s'\n" "$1"
  cat <<'SH'
id builder >/dev/null
install -d -m 0700 -o builder -g builder /home/builder/.ssh
printf '%s\n' "$key" > /home/builder/.ssh/authorized_keys
chown builder:builder /home/builder/.ssh/authorized_keys
chmod 0600 /home/builder/.ssh/authorized_keys
SH
  printf "echo '%s'\n" "$_AZ_READY"
}

# Write the key into the build VM through `az vm run-command invoke`, from a
# script file (@file), and check the script reached its last line.
_az_run_ssh_setup() {
  local rg="$1" vm="$2" key="$3" dir="$4" script command out
  if [ "$OS_FAMILY" = windows ]; then
    script="$dir/azure-ssh-setup.ps1"
    command=RunPowerShellScript
    _az_windows_ssh_script "$key" > "$script"
  else
    script="$dir/azure-ssh-setup.sh"
    command=RunShellScript
    _az_linux_ssh_script "$key" > "$script"
  fi
  out="$(mutate az vm run-command invoke --subscription "$AZ_SUBSCRIPTION_ID" \
    --resource-group "$rg" --name "$vm" --command-id "$command" --scripts "@$script" \
    --query 'value[].message' --output tsv)"
  if [ "$DRY_RUN" = 1 ]; then
    return 0
  fi
  if [[ "$out" != *"$_AZ_READY"* ]]; then
    printf '%s\n' "$out" >&2
    die "azure: the SSH setup run through run-command on $vm did not finish (output above)"
  fi
}

# Create this run's resource group and own it (BUILD_VM_ID) only when Azure
# answers 201 Created. There is no create-only primitive for a resource
# group: `az group create` is a create-or-update, and Azure ignores
# If-None-Match (and If-Match) on a resource-group PUT, so a PUT on an
# existing group answers 200 and replaces its tags (seen live, 2026-11-15; a
# group has no etag). So:
#   1. A fail-closed read first: only exit 3 with exactly
#      ResourceGroupNotFound lets the build go on; a group that exists is
#      refused, unchanged, and any other error stops the build.
#   2. The PUT, through `az rest` with --debug so the HTTP status can be read
#      from the log (the log stays in a file, which is deleted: it holds a
#      prefix of the access token). If-None-Match: * is still sent, in case
#      Azure ever honours it (then: 412).
# Residual race: a group created under this name between the read and the
# PUT (a second or two). The name carries 64 random bits and is not
# published before the PUT, so that is a 2^-64 coincidence or someone who can
# read the build host and already holds resourceGroups/write. If it happens,
# that group's tags are replaced by the creation token below, the PUT answers
# 200, and the build stops: nothing is owned, deleted or reaped; its other
# tags are lost, its resources untouched.
#
# The PUT is non-reapable by construction: its only tag is the creation
# token image-build-creating=<this run's 128-bit nonce>, with no image-build,
# expiry or owner tag, so whatever it leaves behind (a group Azure created or
# upserted while az reported an error, an unreadable status) no reap can ever
# list or delete. No path needs a compensating write to become safe. The
# Merge after the 201 leaves the token in place; ownership, the cleanup and
# the reap all require image-build-creating == image-build-owner == the
# nonce, so a same-name group that replaced this one before the Merge (Azure
# group IDs are name-based, so it would receive the Merge) never has it.
#   201          created by this run. Only then are image-build,
#                image-build-expires and image-build-owner merged on, read
#                back exactly, and BUILD_VM_ID set. If the merge or the
#                read-back fails, nothing is deleted: a parsed 201 is
#                history (the group could have been replaced since, or the
#                201 misread), so the build stops and names the group with
#                the command to delete it by hand. An empty resource group
#                costs nothing.
#   412          exists already (only if Azure starts honouring the header):
#                nothing changed, nothing owned.
#   other error, 200, unknown status
#                nothing owned, nothing deleted, no further write. A 200 means
#                the race above happened: an existing group's tags were
#                replaced with image-build-creating; the error names the group.
#
# The status is read only from this request's own response: the
# `Response status:` line that follows a `Request URL:` line naming exactly
# this group's URI with method PUT. Any other request's status (a token
# fetch, say) is ignored; none, or more than one such line, is "unknown".
_az_put_status() {
  awk -v want="$2" '
    /Request URL: / { mine = index($0, want) > 0; put = 0; next }
    /Request method: / { put = mine && index($0, "PUT") > 0; next }
    /Response status: / {
      if (put) { s = $0; sub(/.*Response status: /, "", s); gsub(/[^0-9]/, "", s); n++ }
      mine = 0; put = 0
    }
    END { if (n == 1 && s ~ /^[0-9][0-9][0-9]$/) print s }' "$1"
}

_az_create_group() {
  local rg="$1" dir="$2" url body log rc=0 status errors trc=0 rrc=0 back="" tag="" expires="" owner="" creating=""
  url="/subscriptions/$AZ_SUBSCRIPTION_ID/resourcegroups/$rg?api-version=$_AZ_RG_API"
  body="{\"location\":\"$AZ_BUILD_LOCATION\",\"tags\":{\"image-build-creating\":\"$_AZ_NONCE\"}}"
  # Step 1 (see above): refuse a group that already exists, without writing.
  _az_read @absent "resource group $rg" ResourceGroupNotFound \
    az group show --subscription "$AZ_SUBSCRIPTION_ID" --name "$rg" --query id --output tsv
  if [ "$_AZ_FOUND" = 1 ]; then
    die "azure: resource group $rg already exists; not used, not changed"
  fi
  if [ "$DRY_RUN" = 1 ]; then
    mutate az rest --method put --url "$url" --headers 'If-None-Match=*' --body "$body" --output none
  else
    log="$dir/az-rest-debug.log"
    mutate az rest --method put --url "$url" --headers 'If-None-Match=*' --body "$body" \
      --output none --debug >/dev/null 2>"$log" || rc=$?
    status="$(_az_put_status "$log" "$url")"
    errors="$(grep '^ERROR' "$log" || true)"
    rm -f "$log"
    if [ -n "$errors" ]; then
      printf '%s\n' "$errors" >&2
    fi
    if [ "$rc" -ne 0 ] && [ "$status" = 412 ]; then
      die "azure: resource group $rg already exists; not used, not changed"
    fi
    if [ "$rc" -ne 0 ] || [ "$status" != 201 ]; then
      die "azure: creating resource group $rg answered az exit $rc, HTTP ${status:-unknown}, not a confirmed 201 Created; this run owns nothing and changes nothing more. If the group exists now it carries only image-build-creating (a 200 means Azure replaced the tags of a group that already existed), so no reap will touch it; check it by hand."
    fi
  fi

  # Created by this run: make it this run's, and prove it before owning it.
  # The read-back requires this run's creation token, which only this run's
  # PUT wrote: a group that replaced it since the 201 fails here.
  mutate az tag update --subscription "$AZ_SUBSCRIPTION_ID" \
    --resource-id "/subscriptions/$AZ_SUBSCRIPTION_ID/resourceGroups/$rg" --operation Merge \
    --tags "image-build=$RUN_ID" "image-build-expires=$BUILD_EXPIRES_UTC" "image-build-owner=$_AZ_NONCE" \
    --output none >&2 || trc=$?
  if [ "$trc" -eq 0 ]; then
    set +e
    back="$(set -e; _az_group_tags "$rg" "$RUN_ID"$'\t'"$BUILD_EXPIRES_UTC"$'\t'"$_AZ_NONCE"$'\t'"$_AZ_NONCE"
            [ "$_AZ_FOUND" = 1 ] || exit 1
            printf '%s\n' "$_AZ_OUT")"
    rrc=$?
    set -e
    IFS=$'\t' read -r tag expires owner creating <<< "$back"
    if [ "$rrc" -eq 0 ] && _az_is_ours "$tag" "$expires" "$owner" "$creating"; then
      BUILD_VM_ID="$rg"
      return 0
    fi
  fi
  die "azure: resource group $rg answered 201 Created, but the tag update or the read-back did not confirm it as this run's group (tag update exit $trc, read-back exit $rrc, image-build='${tag:-}', image-build-creating='${creating:-}'). This run does not own it and deletes nothing: the 201 is no proof the group is still the one it made. Check it, then delete it by hand: az group delete --subscription $AZ_SUBSCRIPTION_ID --name $rg"
}

# A password Azure accepts (12-123 characters, three character classes), in a
# file only the caller can read. 24 random bytes, base64: 32 characters.
_az_admin_password_file() {
  local file="$1" pw
  pw="$(head -c 24 /dev/urandom | base64 | tr -d '\n')"
  [ "${#pw}" -eq 32 ] || die "azure: could not generate the admin password"
  ( umask 077 && printf '%s-Aa1' "$pw" > "$file" )
}

backend_start() {
  local run_id="$1" base="$2" pubkey="$3" key dir rg vm stamp host
  local -a args tags
  if [ "$run_id" != "$RUN_ID" ] || ! [[ "$run_id" =~ $_AZ_RUN_ID_RE ]]; then
    die "azure: run ID '$run_id' is not a valid build resource group name"
  fi
  _az_valid_minute "$BUILD_EXPIRES_UTC" || die "azure: expiry '$BUILD_EXPIRES_UTC' is not a valid UTC yyyymmddhhmm"
  key="$(cat "$pubkey")"
  [[ "$key" =~ $_AZ_PUBKEY_RE ]] || die "azure: public key $pubkey is not a single OpenSSH key line"
  _az_base_facts "$base"
  rg="$run_id"
  vm="$run_id"
  # The core's per-run temp dir (removed on exit) holds the script and
  # password files.
  dir="$(dirname "$pubkey")"
  tags=("image-build=$RUN_ID" "image-build-expires=$BUILD_EXPIRES_UTC")
  _AZ_NONCE="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
  [[ "$_AZ_NONCE" =~ ^[0-9a-f]{32}$ ]] || die "azure: could not generate the ownership nonce"

  _az_create_group "$rg" "$dir"

  # --nsg "": no NIC-level NSG (az would create one open to tcp/22 from
  # anywhere); the build subnet's NSG decides. The Standard public IP is
  # closed until an NSG admits traffic.
  args=(vm create --subscription "$AZ_SUBSCRIPTION_ID" --resource-group "$rg" --name "$vm"
        --location "$AZ_BUILD_LOCATION" --image "$base" --size "$AZURE_VM_SIZE"
        --os-disk-size-gb "${AZURE_DISK_GB:-$DISK_GB}" --subnet "$AZ_BUILD_SUBNET_ID" --nsg ""
        --public-ip-address "$vm-ip" --public-ip-sku Standard
        --public-ip-address-allocation static --security-type Standard
        --tags "${tags[@]}")
  # az (2.92, live 2026-11-15) validates an admin credential even with
  # --specialized ("An RSA key file or key value must be supplied", or a
  # password prompt on Windows), then leaves osProfile out of a specialized
  # VM's template: the credential is required but never reaches the VM. So it
  # is passed in both cases; for a specialized base, run-command writes the key.
  if [ "$OS_FAMILY" = windows ]; then
    _az_admin_password_file "$dir/azure-admin-password"
    args+=(--admin-username builder --admin-password "@$dir/azure-admin-password"
           --authentication-type password)
    if [ "$_AZ_BASE_STATE" != Specialized ]; then
      # A Windows computer name is at most 15 characters: ib-<ddhhmm>-<last 4 hex>.
      stamp="${run_id:3:12}"
      args+=(--computer-name "ib-${stamp:6:6}-${run_id: -4}")
    fi
  else
    args+=(--admin-username builder --authentication-type ssh --ssh-key-values "$pubkey")
  fi
  if [ "$_AZ_BASE_STATE" = Specialized ]; then
    args+=(--specialized)
  fi
  args+=(--output none)
  mutate az "${args[@]}" >&2
  rm -f "$dir/azure-admin-password"

  if [ "$_AZ_BASE_STATE" = Specialized ] || [ "$OS_FAMILY" = windows ]; then
    _az_run_ssh_setup "$rg" "$vm" "$key" "$dir"
  fi

  host="$(query 192.0.2.30 az vm show --subscription "$AZ_SUBSCRIPTION_ID" --resource-group "$rg" \
    --name "$vm" --show-details --query publicIps --output tsv)"
  host="${host//$'\r'/}"
  [[ "$host" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "azure: VM $vm has no public IPv4 address (got '$host')"
  BUILD_SSH_HOST="$host"
  if [ "$OS_FAMILY" = windows ]; then
    BUILD_SSH_USER=Administrator
  else
    BUILD_SSH_USER=builder
  fi
  BUILD_SSH_JUMP=""
}

# Optional hook: the core calls it when a setup pass exits 100.
backend_reboot() {
  [ -n "$BUILD_VM_ID" ] || die "azure: no build VM to restart"
  mutate az vm restart --subscription "$AZ_SUBSCRIPTION_ID" --resource-group "$BUILD_VM_ID" \
    --name "$BUILD_VM_ID" --output none >&2
}

# The VM is deallocated, then published as a specialized version of the
# definition IMAGE_PREFIX. An existing version is never overwritten.
backend_save() {
  local name="$1" rg="$BUILD_VM_ID" stamp ver ostype version_id vm_id row="" os state gen rc=0
  local -a gallery
  [ -n "$rg" ] || die "azure: no build VM to save"
  stamp="${name##*-}"
  [[ "$stamp" =~ ^[0-9]{12}$ ]] || die "azure: image name $name does not end in a yyyymmddhhmm timestamp"
  # yyyy.mmdd.hhmm as integers: 202611140905 -> 2026.1114.905.
  ver="$((10#${stamp:0:4})).$((10#${stamp:4:4})).$((10#${stamp:8:4}))"
  ostype=Linux
  if [ "$OS_FAMILY" = windows ]; then
    ostype=Windows
  fi
  gallery=(--subscription "$AZ_SUBSCRIPTION_ID" --resource-group "$AZ_GALLERY_RG"
           --gallery-name "$AZ_GALLERY" --gallery-image-definition "$IMAGE_PREFIX")
  version_id="/subscriptions/$AZ_SUBSCRIPTION_ID/resourceGroups/$AZ_GALLERY_RG/providers/Microsoft.Compute/galleries/$AZ_GALLERY/images/$IMAGE_PREFIX/versions/$ver"
  vm_id="/subscriptions/$AZ_SUBSCRIPTION_ID/resourceGroups/$rg/providers/Microsoft.Compute/virtualMachines/$rg"

  # Found means found: the read must return this version's own ID. Only an
  # exact not-found code lets the save go on.
  _az_read @absent "gallery image version $version_id" "$_AZ_GALLERY_ABSENT" \
    az sig image-version show "${gallery[@]}" --gallery-image-version "$ver" --query id --output tsv
  if [ "$_AZ_FOUND" = 1 ]; then
    if [ "$(_az_lower "$_AZ_OUT")" = "$(_az_lower "$version_id")" ]; then
      die "azure: gallery image version $version_id already exists; the builder never overwrites an image"
    fi
    die "azure: reading gallery image version $version_id succeeded but returned '$_AZ_OUT', not its ID; cannot tell whether it exists, not saved"
  fi

  _az_read @absent "image definition $IMAGE_PREFIX" "$_AZ_GALLERY_ABSENT" \
    az sig image-definition show "${gallery[@]}" \
    --query '[[osType, osState, hyperVGeneration]]' --output tsv
  if [ "$_AZ_FOUND" = 1 ]; then
    IFS=$'\t' read -r os state gen <<< "$_AZ_OUT"
    if ! [[ "$os" =~ ^(Linux|Windows)$ && "$state" =~ ^(Generalized|Specialized)$ && "$gen" =~ ^V[12]$ ]]; then
      die "azure: reading image definition $IMAGE_PREFIX in $AZ_GALLERY succeeded but returned '$_AZ_OUT', not its OS type, state and generation; not saved"
    fi
    if [ "$os" != "$ostype" ] || [ "$state" != Specialized ] || [ "$gen" != "$_AZ_BASE_GEN" ]; then
      die "azure: image definition $IMAGE_PREFIX in $AZ_GALLERY is $os/$state/$gen; this build needs $ostype/Specialized/$_AZ_BASE_GEN"
    fi
  else
    mutate az sig image-definition create "${gallery[@]}" --location "$AZ_BUILD_LOCATION" \
      --publisher image-build --offer "$IMAGE_PREFIX" --sku "$IMAGE_PREFIX" \
      --os-type "$ostype" --os-state Specialized --hyper-v-generation "$_AZ_BASE_GEN" \
      --features SecurityType=Standard --output none >&2
  fi

  # Deallocated, so the OS disk is consistent when it is captured.
  mutate az vm deallocate --subscription "$AZ_SUBSCRIPTION_ID" --resource-group "$rg" --name "$rg" \
    --output none >&2
  mutate az sig image-version create "${gallery[@]}" --gallery-image-version "$ver" \
    --location "$AZ_BUILD_LOCATION" --virtual-machine "$vm_id" \
    --tags "image-build-recipe=$IMAGE_BUILD_RECIPE" "image-build-commit=$IMAGE_BUILD_COMMIT" \
    --output none >&2 || rc=$?
  if [ "$rc" -eq 0 ]; then
    row="$(query Succeeded az sig image-version show "${gallery[@]}" --gallery-image-version "$ver" \
      --query provisioningState --output tsv)" || rc=$?
    row="${row//$'\r'/}"
  fi
  if [ "$rc" -ne 0 ] || [ "$row" != Succeeded ]; then
    die "azure: gallery image version $version_id was not confirmed (az exit $rc, state '${row:-}'); it is not recorded and may exist. Check it, then delete it by hand: az sig image-version delete --subscription $AZ_SUBSCRIPTION_ID --resource-group $AZ_GALLERY_RG --gallery-name $AZ_GALLERY --gallery-image-definition $IMAGE_PREFIX --gallery-image-version $ver"
  fi
  IMAGE_ID="$version_id"
}

# Reads a resource group's image-build, image-build-expires,
# image-build-owner and image-build-creating tags into _AZ_OUT as one
# tab-separated row (the query is a list of lists: `-o tsv` prints a flat
# list one value per line); a missing tag, which az prints as None, reads as
# empty. _AZ_FOUND=0 only when az reports exactly ResourceGroupNotFound.
_az_group_tags() {
  _az_read "$2" "resource group $1" ResourceGroupNotFound \
    az group show --subscription "$AZ_SUBSCRIPTION_ID" --name "$1" \
    --query '[[tags."image-build", tags."image-build-expires", tags."image-build-owner", tags."image-build-creating"]]' \
    --output tsv
  _AZ_OUT="$(_az_none_empty <<< "$_AZ_OUT")"
}

# az -o tsv prints a missing value as None: make each such field empty.
_az_none_empty() {
  awk 'BEGIN { FS = OFS = "\t" } { for (i = 1; i <= NF; i++) if ($i == "None") $i = ""; print }'
}

# Is this group this run's, right now? Its tags ($1-$4: image-build,
# image-build-expires, image-build-owner, image-build-creating) are exactly
# this run's, and both the creation token and the owner tag are this run's
# nonce. The creation token is set only by this run's create-only PUT, so a
# same-name group made by anyone else (even one that later received this
# run's Merge) lacks it.
_az_is_ours() {
  [ "$1" = "$RUN_ID" ] && [ "$2" = "$BUILD_EXPIRES_UTC" ] && [[ "$_AZ_NONCE" =~ ^[0-9a-f]{32}$ ]] \
    && [ "$3" = "$_AZ_NONCE" ] && [ "$4" = "$_AZ_NONCE" ]
}

# Only a group this run created (HTTP 201) is in BUILD_VM_ID. It is deleted
# only while _az_is_ours still holds.
backend_destroy() {
  local tag expires owner creating
  [ -n "$BUILD_VM_ID" ] || return 0
  [ "$BUILD_VM_ID" = "$RUN_ID" ] || die "azure: build group '$BUILD_VM_ID' is not named after run $RUN_ID; not deleted"
  _az_group_tags "$BUILD_VM_ID" "$RUN_ID"$'\t'"$BUILD_EXPIRES_UTC"$'\t'"$_AZ_NONCE"$'\t'"$_AZ_NONCE"
  if [ "$_AZ_FOUND" = 1 ]; then
    IFS=$'\t' read -r tag expires owner creating <<< "$_AZ_OUT"
    if ! _az_is_ours "$tag" "$expires" "$owner" "$creating"; then
      die "azure: resource group $BUILD_VM_ID carries image-build='$tag' image-build-expires='$expires', and its image-build-owner and image-build-creating are not both this run's nonce; not deleted"
    fi
    mutate az group delete --subscription "$AZ_SUBSCRIPTION_ID" --name "$BUILD_VM_ID" --yes \
      --output none >&2
  fi
  BUILD_VM_ID=""
}

# A reapable group: its name is a run ID, its image-build tag equals its name,
# its image-build-owner and image-build-creating are one and the same 32-hex
# nonce (the creation token of the run that made it), and image-build-expires
# is a real UTC minute in the past. $1-$4 are the tags, $5 the name; prints
# why not.
_az_reapable() {
  local tag="$1" expires="$2" owner="$3" creating="$4" name="$5" now
  now="$(date -u +%Y%m%d%H%M)"
  if ! [[ "$name" =~ $_AZ_RUN_ID_RE ]] || [ "$tag" != "$name" ]; then
    echo "its name and image-build='$tag' are not one build run ID (ib-<12 digits>-<16 hex>)"
  elif ! [[ "$owner" =~ ^[0-9a-f]{32}$ ]] || [ "$creating" != "$owner" ]; then
    echo "image-build-owner='$owner' and image-build-creating='$creating' are not one creation token"
  elif ! _az_valid_minute "$expires"; then
    echo "image-build-expires='$expires' is not a valid UTC yyyymmddhhmm"
  elif [ "$expires" -ge "$now" ]; then
    echo "image-build-expires=$expires is not past (now $now)"
  fi
}

# Resource groups that _az_reapable accepts. A group that carries the
# image-build tag but fails is reported and left alone.
backend_leftovers() {
  local rows name tag expires owner creating why
  rows="$(query "" az group list --subscription "$AZ_SUBSCRIPTION_ID" --tag image-build \
    --query '[].[name, tags."image-build", tags."image-build-expires", tags."image-build-owner", tags."image-build-creating"]' \
    --output tsv)"
  # One tab-separated row per group (a list of lists); a missing tag is None.
  rows="$(_az_none_empty <<< "${rows//$'\r'/}")"
  while IFS=$'\t' read -r name tag expires owner creating; do
    [ -n "$name" ] || continue
    why="$(_az_reapable "$tag" "$expires" "$owner" "$creating" "$name")"
    if [ -z "$why" ]; then
      printf '%s\n' "$name"
    elif [[ "$why" != *"is not past"* ]]; then
      echo "azure: resource group $name: $why; left alone" >&2
    fi
  done <<< "$rows"
}

backend_destroy_id() {
  local name="$1" tag expires owner creating why
  [[ "$name" =~ $_AZ_RUN_ID_RE ]] || die "azure: '$name' is not a build resource group name; not deleted"
  _az_group_tags "$name" "$name"$'\t'202601010000$'\t'00000000000000000000000000000000$'\t'00000000000000000000000000000000
  if [ "$_AZ_FOUND" = 0 ]; then
    echo "azure: resource group $name no longer exists" >&2
    return 0
  fi
  IFS=$'\t' read -r tag expires owner creating <<< "$_AZ_OUT"
  why="$(_az_reapable "$tag" "$expires" "$owner" "$creating" "$name")"
  [ -z "$why" ] || die "azure: resource group $name: $why; not an expired build group, not deleted"
  mutate az group delete --subscription "$AZ_SUBSCRIPTION_ID" --name "$name" --yes --output none >&2
}

backend_templates_yaml() {
  printf 'azure: {image: %s, vm_size: %s, disk_gb: %s}\n' "$1" "$AZURE_VM_SIZE" "${AZURE_DISK_GB:-$DISK_GB}"
}
