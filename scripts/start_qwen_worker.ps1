param(
    [string]$ConfigPath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'deploy\qwen-worker.local.json'),
    [string]$WorkerConfigPath = (Join-Path $env:LOCALAPPDATA 'ImageWorkbench\Tencent\remote-worker.json'),
    [switch]$Check,
    [switch]$Once
)
$ErrorActionPreference = 'Stop'
$taskRepo = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRepo '.venv\Scripts\python.exe'
$taskArguments = @('-X', 'utf8', '-u', '-m', 'image_workbench.qwen_worker', '--config', (Resolve-Path -LiteralPath $ConfigPath).Path)
if ($Check) { $taskArguments += '--check' }
else { $taskArguments += @('--worker-config', (Resolve-Path -LiteralPath $WorkerConfigPath).Path) }
if ($Once) { $taskArguments += '--once' }
Push-Location -LiteralPath $taskRepo
try {
    & $taskPython @taskArguments
    $taskExitCode = $LASTEXITCODE
} finally { Pop-Location }
exit $taskExitCode
