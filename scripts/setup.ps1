[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$BackendDir = Join-Path $RepoRoot 'backend'
$FrontendDir = Join-Path $RepoRoot 'frontend'
$Python = Join-Path $BackendDir '.venv\Scripts\python.exe'

function Assert-ExitCode([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed with exit code $LASTEXITCODE." }
}

if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
    throw 'Install Node.js 22+ with npm before running setup.'
}

if ((Test-Path (Join-Path $BackendDir '.venv')) -and -not (Test-Path $Python)) {
    throw 'backend/.venv is incomplete. Stop servers, remove only backend/.venv, and rerun setup with Python 3.13.3.'
}

if (-not (Test-Path $Python)) {
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
        throw 'Install Python 3.13.3 with the Windows Python launcher before running setup.'
    }
    & py -3.13 (Join-Path $PSScriptRoot 'check_backend.py') --check-python
    Assert-ExitCode 'Python 3.13.3 version check'
    & py -3.13 -m venv (Join-Path $BackendDir '.venv')
    Assert-ExitCode 'Virtual environment creation'
}

& $Python (Join-Path $PSScriptRoot 'check_backend.py') --check-python --existing-venv
Assert-ExitCode 'Python 3.13.3 virtual environment check'
& $Python -m pip install --require-hashes -r (Join-Path $BackendDir 'requirements.lock')
Assert-ExitCode 'Backend dependency installation'

foreach ($Directory in @($BackendDir, $FrontendDir)) {
    $EnvFile = Join-Path $Directory '.env'
    if (-not (Test-Path $EnvFile)) {
        Copy-Item (Join-Path $Directory '.env.example') $EnvFile
    }
}

Push-Location $BackendDir
try {
    & $Python -m alembic upgrade head
    Assert-ExitCode 'Database migration'
} finally { Pop-Location }

Push-Location $FrontendDir
try {
    & npm.cmd ci
    Assert-ExitCode 'Frontend dependency installation'
} finally { Pop-Location }

Write-Host 'Setup complete. Add credentials to backend/.env, or start with scripts/dev.ps1 -Mock.'
