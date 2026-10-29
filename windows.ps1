param(
    [Parameter(Position=0)]
    [ValidateSet('source','build','download-model','configure','run','start','stop','restart','status','install','uninstall','logs','opencode','check-api','warm','test')]
    [string]$Action = 'status',
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$Rest
)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$rootDir = $PSScriptRoot
$pythonExe = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonExe) { throw 'Install Python 3.11 or newer and reopen PowerShell.' }
& $pythonExe.Source -c "import sys; assert sys.version_info >= (3,11), 'Python 3.11+ required'"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($Action -eq 'test') {
    & $pythonExe.Source -m unittest discover -s "$rootDir/server/tests" -v
} elseif ($Action -in @('source','build','download-model')) {
    & $pythonExe.Source "$rootDir/server/logged_run.py" "windows-$Action" -- $pythonExe.Source "$rootDir/server/bootstrap.py" $Action @Rest
} else {
    & $pythonExe.Source "$rootDir/server/logged_run.py" "windows-$Action" -- $pythonExe.Source "$rootDir/server/windows.py" $Action @Rest
}
exit $LASTEXITCODE
