param(
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [string]$WorkerConfigPath,
    [string]$DataRoot,
    [switch]$Once,
    [switch]$Check
)
$ErrorActionPreference = 'Stop'
$taskRepo = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRepo '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw '请先准备项目 Python 虚拟环境。' }
$taskConfigFile = (Resolve-Path -LiteralPath $ConfigPath).Path
$taskConfig = Get-Content -LiteralPath $taskConfigFile -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $taskConfig.comfy_url -or -not $taskConfig.workflow_dir -or -not $taskConfig.cache_root) {
    throw '配置必须包含 comfy_url、workflow_dir、cache_root。'
}
$taskOriginalToken = $env:IMAGE_WORKBENCH_WORKER_TOKEN
$taskExitCode = 0
Push-Location -LiteralPath $taskRepo
try {
    if ($WorkerConfigPath) {
        $taskWorkerConfig = Get-Content -LiteralPath $WorkerConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if (-not $taskWorkerConfig.worker_token) { throw 'WorkerConfigPath 中缺少 worker_token。' }
        $env:IMAGE_WORKBENCH_WORKER_TOKEN = $taskWorkerConfig.worker_token
    }
    if ($taskConfig.server -and -not $Check -and -not $env:IMAGE_WORKBENCH_WORKER_TOKEN) {
        throw '远程执行器需要 IMAGE_WORKBENCH_WORKER_TOKEN 或 WorkerConfigPath。'
    }
    $taskArguments = @('-X', 'utf8', '-m', 'image_workbench')
    if ($DataRoot) { $taskArguments += @('--data-root', $DataRoot) }
    $taskArguments += @('h3-worker', '--config', $taskConfigFile)
    if ($Once) { $taskArguments += '--once' }
    if ($Check) { $taskArguments += '--check' }
    & $taskPython @taskArguments
    $taskExitCode = $LASTEXITCODE
} finally {
    $env:IMAGE_WORKBENCH_WORKER_TOKEN = $taskOriginalToken
    Pop-Location
}
exit $taskExitCode
