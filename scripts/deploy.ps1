param(
    [string]$ConfigPath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'deploy\server.json'),
    [switch]$PackageOnly
)
$ErrorActionPreference = 'Stop'
$taskRepo = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRepo '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw '请先安装项目虚拟环境和 requirements-dev.txt。' }
$taskArguments = @('-X', 'utf8', (Join-Path $PSScriptRoot 'deploy_release.py'))
if ($PackageOnly) { $taskArguments += '--package-only' }
else { $taskArguments += @('--config', $ConfigPath) }
& $taskPython @taskArguments
exit $LASTEXITCODE
