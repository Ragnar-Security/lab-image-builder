# Extract the payload archive (payload.tgz) into the build VM's payload
# directory, with .NET only. build_image.sh uploads and runs this on a Windows
# build VM that has no tar.exe (Windows Server 2016 and older; tar.exe ships
# with Windows from Server 2019), and deletes it with the archive.
#
# It reads what GNU tar and bsdtar write for a payload: regular files and
# directories, GNU long names ('L') and long link names ('K'), pax extended
# headers ('x'; global 'g' headers are skipped) and the ustar name prefix. Each
# header's checksum is verified. It fails closed, exit 1, on anything else: a
# symbolic or hard link, a device, a FIFO, a sparse file, a member whose path
# is absolute, leaves the destination or is not a valid Windows path, and an
# archive that ends early. A file that exists is overwritten, so a retried
# extract finishes the tree. Prints one summary line.
param(
    [Parameter(Mandatory = $true)][string]$Archive,
    [Parameter(Mandatory = $true)][string]$Destination
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
trap {
    [Console]::Error.WriteLine("expand_payload.ps1: $($_.Exception.Message)")
    exit 1
}

$utf8 = New-Object System.Text.UTF8Encoding($false, $true)

# Read exactly $count bytes into $buffer, or fail: a short read is a cut archive.
function Read-Exactly([System.IO.Stream]$stream, [byte[]]$buffer, [int]$count) {
    $offset = 0
    while ($offset -lt $count) {
        $n = $stream.Read($buffer, $offset, $count - $offset)
        if ($n -le 0) { throw "the archive ends early (truncated or damaged)" }
        $offset += $n
    }
}

# A NUL-terminated string field.
function Get-Text([byte[]]$block, [int]$offset, [int]$length) {
    $end = $offset
    while ($end -lt $offset + $length -and $block[$end] -ne 0) { $end++ }
    return $utf8.GetString($block, $offset, $end - $offset)
}

# A numeric field: octal digits, or base-256 when the high bit is set (GNU,
# for values that do not fit).
function Get-Number([byte[]]$block, [int]$offset, [int]$length) {
    if ($block[$offset] -band 0x80) {
        [long]$value = $block[$offset] -band 0x7F
        for ($i = $offset + 1; $i -lt $offset + $length; $i++) { $value = $value * 256 + $block[$i] }
        return $value
    }
    $text = [System.Text.Encoding]::ASCII.GetString($block, $offset, $length).Trim([char]0, ' ')
    if ($text -eq '') { return [long]0 }
    if ($text -notmatch '^[0-7]+$') { throw "a header holds a malformed number '$text'" }
    return [Convert]::ToInt64($text, 8)
}

# Read a member's data: into a file, into memory (for 'L', 'K' and 'x'
# records) or into nothing; then the padding to the next 512-byte block.
function Read-Data([System.IO.Stream]$stream, [long]$size, [System.IO.Stream]$sink) {
    $buffer = New-Object byte[] 65536
    $left = $size
    while ($left -gt 0) {
        $want = [int][Math]::Min([long]$buffer.Length, $left)
        Read-Exactly $stream $buffer $want
        if ($sink) { $sink.Write($buffer, 0, $want) }
        $left -= $want
    }
    $pad = (512 - ($size % 512)) % 512
    if ($pad) { Read-Exactly $stream $buffer $pad }
}

function Read-Record([System.IO.Stream]$stream, [long]$size) {
    if ($size -gt 1MB) { throw "an extended header of $size bytes is too large" }
    $memory = New-Object System.IO.MemoryStream
    Read-Data $stream $size $memory
    return ,$memory.ToArray()
}

# The destination path for a member name, or a refusal.
function Resolve-Member([string]$name, [string]$root) {
    if ($name.StartsWith('/')) { throw "member '$name' has an absolute path" }
    $parts = New-Object System.Collections.Generic.List[string]
    foreach ($part in $name.Split('/')) {
        if ($part -eq '' -or $part -eq '.') { continue }
        if ($part -eq '..') { throw "member '$name' leaves the destination" }
        if ($part.IndexOfAny([char[]]'\:*?"<>|') -ge 0 -or $part -match '[\x00-\x1f]' -or
            $part.EndsWith('.') -or $part.EndsWith(' ')) {
            throw "member '$name' is not a valid Windows path"
        }
        $parts.Add($part)
    }
    if ($parts.Count -eq 0) { return $null }
    return [System.IO.Path]::Combine($root, ($parts -join '\'))
}

$root = [System.IO.Path]::GetFullPath($Destination).TrimEnd('\')
[void][System.IO.Directory]::CreateDirectory($root)
$epoch = New-Object DateTime(1970, 1, 1, 0, 0, 0, [DateTimeKind]::Utc)
$files = 0; $dirs = 0; [long]$bytes = 0

$file = [System.IO.File]::OpenRead((Resolve-Path -LiteralPath $Archive).ProviderPath)
try {
    $gzip = New-Object System.IO.Compression.GZipStream($file, [System.IO.Compression.CompressionMode]::Decompress)
    $header = New-Object byte[] 512
    $longName = $null; $paxPath = $null; $paxSize = $null
    while ($true) {
        Read-Exactly $gzip $header 512
        $zero = $true
        foreach ($b in $header) { if ($b -ne 0) { $zero = $false; break } }
        if ($zero) { break }   # the end-of-archive marker

        # The checksum is the sum of the header's bytes, the checksum field
        # counted as eight spaces.
        [long]$sum = 8 * 32
        foreach ($b in $header) { $sum += $b }
        for ($i = 148; $i -lt 156; $i++) { $sum -= $header[$i] }
        if ((Get-Number $header 148 8) -ne $sum) { throw 'a header checksum does not match (damaged archive)' }

        $type = [char]$header[156]
        $size = Get-Number $header 124 12
        $name = Get-Text $header 0 100
        $magic = [System.Text.Encoding]::ASCII.GetString($header, 257, 8)
        if ($magic -eq "ustar`0" + '00') {
            # POSIX ustar: the name may continue in the prefix field.
            $prefix = Get-Text $header 345 155
            if ($prefix) { $name = "$prefix/$name" }
        }
        # Type flags are case-sensitive ('x' is pax, 'X' is not). No switch
        # here: `continue` inside a switch would not reach this loop.
        if ($type -ceq 'L') {
            $longName = $utf8.GetString((Read-Record $gzip $size)).TrimEnd([char]0)
            continue
        }
        if ($type -ceq 'K' -or $type -ceq 'g') {
            [void](Read-Record $gzip $size)
            continue
        }
        if ($type -ceq 'x') {
            $text = $utf8.GetString((Read-Record $gzip $size))
            # Records: "<length> <key>=<value>\n".
            foreach ($m in [regex]::Matches($text, '(?m)^\d+ ([^=\n]+)=([^\n]*)$')) {
                if ($m.Groups[1].Value -ceq 'path') { $paxPath = $m.Groups[2].Value }
                if ($m.Groups[1].Value -ceq 'size') { $paxSize = [long]$m.Groups[2].Value }
            }
            continue
        }
        if ($paxPath) { $name = $paxPath } elseif ($longName) { $name = $longName }
        if ($null -ne $paxSize) { $size = $paxSize }
        $longName = $null; $paxPath = $null; $paxSize = $null

        $isFile = $type -ceq '0' -or $type -ceq [char]0 -or $type -ceq '7'
        $isDir = $type -ceq '5' -or ($isFile -and $name.EndsWith('/'))
        if (-not $isFile -and -not $isDir) {
            $what = if ($type -ceq '1') { 'a hard link' } elseif ($type -ceq '2') { 'a symbolic link' } else { "of type '$type'" }
            throw "member '$name' is $what; a payload holds only files and directories"
        }
        $path = Resolve-Member $name $root
        if ($isDir) {
            if ($path) { [void][System.IO.Directory]::CreateDirectory($path); $dirs++ }
            Read-Data $gzip $size $null
            continue
        }
        if (-not $path) { throw "member '$name' has no file name" }
        [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($path))
        $out = [System.IO.File]::Create($path)
        try { Read-Data $gzip $size $out } finally { $out.Dispose() }
        [System.IO.File]::SetLastWriteTimeUtc($path, $epoch.AddSeconds((Get-Number $header 136 12)))
        $files++
        $bytes += $size
    }
} finally {
    $file.Dispose()
}
Write-Output "expand_payload.ps1: $files files, $dirs directories, $bytes bytes into $root"
exit 0
