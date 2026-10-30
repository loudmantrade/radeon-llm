param(
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$SetupArguments
)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'Use setup.command on macOS; setup.ps1 requires Windows.'
}
function Find-SetupPython {
    foreach ($name in @('python', 'python3')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command -and $command.Source -notlike '*WindowsApps*') {
            & $command.Source -c 'import sys; sys.exit(sys.version_info < (3,11))' 2>$null
            if ($LASTEXITCODE -eq 0) { return $command.Source }
        }
    }
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        $resolved = & $launcher.Source -3 -c 'import sys; assert sys.version_info >= (3,11); print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0) { return $resolved.Trim() }
    }
    return $null
}
$pythonExe = Find-SetupPython
if (-not $pythonExe) {
    if ($SetupArguments | Where-Object { $_ -in @('--plan','--check','--no-deps') }) {
        throw 'Python 3.11+ is missing. Install it before using this option.'
    }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw 'Install Python 3.11+ or Microsoft App Installer (winget), then rerun setup.ps1.'
    }
    & winget install --id Python.Python.3.11 --exact --source winget --scope user --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) { throw "Python installation failed (exit $LASTEXITCODE)." }
    $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User') + ';' + $env:Path
    $pythonExe = Find-SetupPython
    if (-not $pythonExe) { throw 'Python installed; reopen PowerShell and rerun setup.ps1.' }
}
& $pythonExe "$PSScriptRoot/setup.py" @SetupArguments
exit $LASTEXITCODE
