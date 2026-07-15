# One-time project setup on Windows (run from anywhere; operates on the repo
# this script lives in). Safe to re-run.
#
# Prereqs already installed and running (see windows\README.md):
#   * Python 3.11+ on PATH
#   * PostgreSQL 16 service on localhost:5432 (user postgres/postgres)
#     with the pgvector extension files installed
#   * Memurai (Redis) service on localhost:6379

$ErrorActionPreference = "Stop"
$Repo = Split-Path $PSScriptRoot -Parent
Set-Location $Repo

# ---- venv + deps ------------------------------------------------------------
if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtualenv..." -ForegroundColor Cyan
    python -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\pip.exe install -e ".[dev]"
& .\.venv\Scripts\playwright.exe install chromium

# ---- .env ---------------------------------------------------------------------
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    # Replace the publicly-known default Fernet key with a freshly generated one.
    $key = & .\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    (Get-Content ".env" -Raw) -replace "(?m)^SECRET_ENCRYPTION_KEY=.*$", "SECRET_ENCRYPTION_KEY=$key" |
        Set-Content ".env" -NoNewline
    Write-Host "Created .env with a fresh SECRET_ENCRYPTION_KEY." -ForegroundColor Green
} else {
    Write-Host ".env already exists — leaving it untouched." -ForegroundColor Yellow
}

# ---- Database ----------------------------------------------------------------
Write-Host "Checking Postgres..." -ForegroundColor Cyan
$env:PGPASSWORD = "postgres"
$psql = Get-Command psql -ErrorAction SilentlyContinue
if (-not $psql) {
    $guess = "C:\Program Files\PostgreSQL\16\bin\psql.exe"
    if (Test-Path $guess) { $psql = @{ Source = $guess } }
}
if ($psql) {
    $psqlExe = $psql.Source
    $dbExists = & $psqlExe -U postgres -h localhost -tAc "SELECT 1 FROM pg_database WHERE datname='news_notifier'"
    if ($dbExists -ne "1") {
        & $psqlExe -U postgres -h localhost -c "CREATE DATABASE news_notifier;"
        Write-Host "Created database news_notifier." -ForegroundColor Green
    }
    & $psqlExe -U postgres -h localhost -d news_notifier -c "CREATE EXTENSION IF NOT EXISTS vector;"
} else {
    Write-Host "psql not found on PATH — create the DB and extension manually:" -ForegroundColor Yellow
    Write-Host '  psql -U postgres -c "CREATE DATABASE news_notifier;"'
    Write-Host '  psql -U postgres -d news_notifier -c "CREATE EXTENSION vector;"'
}

# ---- Migrations + seed ---------------------------------------------------------
Write-Host "Running migrations..." -ForegroundColor Cyan
& .\.venv\Scripts\alembic.exe upgrade head
& .\.venv\Scripts\python.exe -m app.sources_seed

Write-Host ""
Write-Host "Setup complete. Start the stack (each in its own terminal):" -ForegroundColor Green
Write-Host "  1. windows\start-llm.ps1      (wait until /health is OK)"
Write-Host "  2. windows\start-api.ps1"
Write-Host "  3. windows\start-worker.ps1"
