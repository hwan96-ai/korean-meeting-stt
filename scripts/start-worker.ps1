param(
    [string]$Python = "$PSScriptRoot\..\.venv-worker\Scripts\python.exe",
    [string]$WorkerHome,
    [switch]$Once
)
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$previousWorkerHome = $env:MEETING_WORKER_HOME
if (-not (Test-Path -LiteralPath $Python)) { throw 'Python 환경을 설치하고 requirements-worker.txt를 설치하세요.' }
Push-Location -LiteralPath $project
try {
    if ($WorkerHome) { $env:MEETING_WORKER_HOME = $WorkerHome }
    $workerArgs = @('-m', 'local_worker', 'run')
    if ($Once) { $workerArgs += '--once' }
    & $Python @workerArgs
    exit $LASTEXITCODE
} finally {
    Pop-Location
    if ($null -eq $previousWorkerHome) { Remove-Item Env:MEETING_WORKER_HOME -ErrorAction SilentlyContinue }
    else { $env:MEETING_WORKER_HOME = $previousWorkerHome }
}
