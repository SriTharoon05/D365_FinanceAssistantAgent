[CmdletBinding()]
param([switch]$Mock)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$BackendDir = Join-Path $RepoRoot 'backend'
$FrontendDir = Join-Path $RepoRoot 'frontend'
$Python = Join-Path $BackendDir '.venv\Scripts\python.exe'

if (-not (Test-Path $Python) -or -not (Test-Path (Join-Path $FrontendDir 'node_modules'))) {
    & (Join-Path $PSScriptRoot 'setup.ps1')
}
& $Python (Join-Path $PSScriptRoot 'check_backend.py') --check-python --existing-venv
if ($LASTEXITCODE -ne 0) { throw 'Python 3.13.3 is required before starting the development servers.' }
foreach ($Directory in @($BackendDir, $FrontendDir)) {
    if (-not (Test-Path (Join-Path $Directory '.env'))) {
        Copy-Item (Join-Path $Directory '.env.example') (Join-Path $Directory '.env')
    }
}

$PreviousMockMode = $env:D365_MOCK_MODE
if ($Mock) {
    $env:D365_MOCK_MODE = 'true'
    Write-Host 'DEMO MODE: finance records are synthetic; no Dynamics 365 requests are made.'
}

$BackendProcess = $null
try {
    & $Python (Join-Path $PSScriptRoot 'check_backend.py') --check-port
    if ($LASTEXITCODE -ne 0) { throw 'Backend port is unavailable.' }
    Push-Location $BackendDir
    try {
        & $Python -m alembic upgrade head
        if ($LASTEXITCODE -ne 0) { throw 'Database migration failed.' }
    } finally { Pop-Location }

    $BackendProcess = Start-Process -FilePath $Python -ArgumentList @(
        '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000', '--reload'
    ) -WorkingDirectory $BackendDir -NoNewWindow -PassThru

    & $Python (Join-Path $PSScriptRoot 'check_backend.py')
    if ($LASTEXITCODE -ne 0) { throw 'Backend did not become healthy. Check its startup logs.' }
    $BackendProcess.Refresh()
    if ($BackendProcess.HasExited) { throw 'Backend exited during startup. Check its logs.' }
    Write-Host 'Frontend: http://localhost:5173 | API: http://localhost:8000 | Swagger: http://localhost:8000/docs'
    Push-Location $FrontendDir
    try {
        & npm.cmd run dev -- --host localhost --port 5173 --strictPort
        if ($LASTEXITCODE -ne 0) { throw 'Frontend development server failed.' }
    } finally { Pop-Location }
} finally {
    if ($BackendProcess -and -not $BackendProcess.HasExited) {
        # Stop this script's backend and the Uvicorn reload worker it created.
        & taskkill.exe /PID $BackendProcess.Id /T /F | Out-Null
    }
    $env:D365_MOCK_MODE = $PreviousMockMode
}
