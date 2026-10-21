param(
    [string]$ConfigPath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'deploy\qwen-worker.local.json'),
    [string]$WorkerConfigPath = (Join-Path $env:LOCALAPPDATA 'ImageWorkbench\Tencent\remote-worker.json')
)
$ErrorActionPreference = 'Stop'
$taskRepo = Split-Path -Parent $PSScriptRoot
$taskConfig = (Resolve-Path -LiteralPath $ConfigPath).Path
$taskWorkerConfig = (Resolve-Path -LiteralPath $WorkerConfigPath).Path
$taskPython = Join-Path $taskRepo '.venv\Scripts\python.exe'
$taskRuntime = Join-Path $env:LOCALAPPDATA 'ImageWorkbench\Tencent'
$taskRecord = Join-Path $taskRuntime 'qwen-worker-process.json'
& 'D:\ComfyUI\Qwen21\start_qwen.ps1'
if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw 'ComfyUI startup failed' }
if (Test-Path -LiteralPath $taskRecord) {
    $taskExisting = Get-Content -LiteralPath $taskRecord -Raw -Encoding UTF8 | ConvertFrom-Json
    $taskProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($taskExisting.pid)" -ErrorAction SilentlyContinue
    if ($taskProcess -and $taskProcess.CommandLine.Contains('image_workbench.qwen_worker') -and $taskProcess.CommandLine.Contains($taskConfig)) {
        Write-Output "Qwen website worker is already running: $($taskExisting.pid)"
        return
    }
}
$taskStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$taskStdout = Join-Path $taskRuntime "qwen-$taskStamp.stdout.log"
$taskStderr = Join-Path $taskRuntime "qwen-$taskStamp.stderr.log"
$taskArgs = @('-X','utf8','-u','-m','image_workbench.qwen_worker','--config',('"' + $taskConfig + '"'),'--worker-config',('"' + $taskWorkerConfig + '"'))
$taskProcess = Start-Process -FilePath $taskPython -ArgumentList $taskArgs -WorkingDirectory $taskRepo -WindowStyle Hidden -RedirectStandardOutput $taskStdout -RedirectStandardError $taskStderr -PassThru
@{pid=$taskProcess.Id;started_at=(Get-Date).ToString('o');config=$taskConfig;stdout=$taskStdout;stderr=$taskStderr} | ConvertTo-Json | Set-Content -LiteralPath $taskRecord -Encoding UTF8
Write-Output "Qwen website worker started: $($taskProcess.Id)"
Write-Output 'Website: https://124.156.182.151/qwen-test'
