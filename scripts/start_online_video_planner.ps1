param(
    [Parameter(Mandatory = $true)][string]$WorkerConfigPath,
    [Parameter(Mandatory = $true)][string]$CacheRoot
)
$ErrorActionPreference = 'Stop'
$taskRepo = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRepo '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw '请先准备项目 Python 虚拟环境。' }
$taskConfig = (Resolve-Path -LiteralPath $WorkerConfigPath).Path
if (-not $env:SCRIPT_CODEX_EXECUTABLE) {
    $taskCodex = Get-Command codex.exe -ErrorAction SilentlyContinue
    if ($taskCodex) { $env:SCRIPT_CODEX_EXECUTABLE = $taskCodex.Source }
    else {
        $taskCodex = Get-ChildItem (Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin') -Filter codex.exe -Recurse -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $taskCodex) { throw '找不到本机 Codex CLI。' }
        $env:SCRIPT_CODEX_EXECUTABLE = $taskCodex.FullName
    }
}
Push-Location -LiteralPath $taskRepo
try {
    & $taskPython -X utf8 -m image_workbench online-video-planner --worker-config $taskConfig --cache $CacheRoot
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
