# Shell integration for PowerShell, the shell of a Windows host. The terminal daemon starts PowerShell
# with -NoExit and a command that dot-sources this file after the user's profile, so the profile is
# read exactly as without the daemon. It adds the same marks as the other shells: OSC 133 A where the
# prompt starts and B where it ends, C when a command starts, D with its exit status when it ends,
# OSC 633 E with the command line and OSC 7 with the directory, each with k=<nonce>.
#
# It is tested with pwsh on Linux, and has not yet run under a daemon on Windows, which is where it is
# meant to work; so it is careful to leave the prompt working if anything in it fails. The user's
# prompt function is kept and called; PSReadLine's line reader is wrapped, not replaced.

if ($Global:__DsiLoaded) { return }
$Global:__DsiLoaded = $true
$Global:__DsiNonce = $env:DAEDALUS_SI_NONCE
Remove-Item Env:DAEDALUS_SI_NONCE -ErrorAction SilentlyContinue
if (-not $Global:__DsiNonce) { return }

# [char]27 and [char]7 rather than `e and `a: Windows PowerShell 5.1 knows neither escape.
$Global:__DsiEsc = [char]27
$Global:__DsiBel = [char]7
$Global:__DsiRan = $false
$Global:__DsiUserPrompt = $function:prompt

function Global:__Dsi-Mark([string]$Body) {
    "$Global:__DsiEsc]$Body;k=$Global:__DsiNonce$Global:__DsiBel"
}

# An OSC 633 value: a backslash doubled, and ';' and every control character as \xNN.
function Global:__Dsi-Escape([string]$Value) {
    if ($Value.Length -gt 4096) { $Value = $Value.Substring(0, 4096) }
    $out = [System.Text.StringBuilder]::new()
    foreach ($c in $Value.ToCharArray()) {
        if ($c -eq '\') { [void]$out.Append('\\') }
        elseif ($c -eq ';' -or [int]$c -lt 32 -or [int]$c -eq 127) { [void]$out.Append(('\x{0:x2}' -f [int]$c)) }
        else { [void]$out.Append($c) }
    }
    $out.ToString()
}

function Global:prompt {
    # Both first: anything run here changes them.
    $ok = $?
    $code = $Global:LASTEXITCODE
    $out = ''
    try {
        if ($Global:__DsiRan) {
            $exit = if ($ok) { 0 } elseif ($code) { $code } else { 1 }
            $out += __Dsi-Mark "133;D;$exit"
            $Global:__DsiRan = $false
        }
        $location = Get-Location
        if ($location.Provider.Name -eq 'FileSystem') {
            $path = $location.ProviderPath -replace '\\', '/'
            if (-not $path.StartsWith('/')) { $path = '/' + $path }
            $out += "$Global:__DsiEsc]7;file://$([System.Net.Dns]::GetHostName())$([System.Uri]::EscapeUriString($path))$Global:__DsiBel"
        }
        $out += __Dsi-Mark '133;A'
    } catch { }
    $Global:LASTEXITCODE = $code
    if ($Global:__DsiUserPrompt) { $out += & $Global:__DsiUserPrompt } else { $out += "PS $($executionContext.SessionState.Path.CurrentLocation)> " }
    $out += __Dsi-Mark '133;B'
    $Global:LASTEXITCODE = $code
    $out
}

# C and E come from PSReadLine, which reads the line: its reader is wrapped so the line it returns is
# reported just before PowerShell runs it. Without PSReadLine there are prompts and D, but no C.
if (Get-Command PSConsoleHostReadLine -ErrorAction SilentlyContinue) {
    $Global:__DsiReadLine = $function:PSConsoleHostReadLine
    function Global:PSConsoleHostReadLine {
        $line = & $Global:__DsiReadLine
        if ($line.Trim().Length -gt 0) {
            $Global:__DsiRan = $true
            [Console]::Write((__Dsi-Mark "633;E;$(__Dsi-Escape $line)") + (__Dsi-Mark '133;C'))
        }
        $line
    }
}
