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

if (-not (Test-Path $Python)) {
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
        throw 'Install Python 3.12 with the Windows Python launcher before running setup.'
    }
    & py -3.12 -m venv (Join-Path $BackendDir '.venv')
    Assert-ExitCode 'Virtual environment creation'
}

& $Python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'
Assert-ExitCode 'Python version check'
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
