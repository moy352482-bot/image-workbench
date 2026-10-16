param(
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [switch]$Once,
    [string]$ConfirmStopped
)
$ErrorActionPreference = 'Stop'
$taskRepo = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRepo '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw '请先安装项目 Python 虚拟环境和 requirements-web.txt。' }
$taskConfig = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $taskConfig.server -or -not $taskConfig.worker_token -or -not $taskConfig.cache_root) {
    throw '配置必须包含 server、worker_token、cache_root。'
}
$taskOriginalToken = $env:IMAGE_WORKBENCH_WORKER_TOKEN
$taskExitCode = 0
Push-Location -LiteralPath $taskRepo
try {
    $env:IMAGE_WORKBENCH_WORKER_TOKEN = $taskConfig.worker_token
    $taskArguments = @('-X', 'utf8', '-m', 'image_workbench', 'remote-worker',
        '--server', $taskConfig.server, '--cache-root', $taskConfig.cache_root)
    if ($Once) { $taskArguments += '--once' }
    if ($ConfirmStopped) { $taskArguments += @('--confirm-stopped', $ConfirmStopped) }
    & $taskPython @taskArguments
    $taskExitCode = $LASTEXITCODE
} finally {
    $env:IMAGE_WORKBENCH_WORKER_TOKEN = $taskOriginalToken
    Pop-Location
}
exit $taskExitCode
