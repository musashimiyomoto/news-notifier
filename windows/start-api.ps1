# Starts the FastAPI server on http://localhost:8000 (demo UI at /demo).
# Run windows\setup.ps1 once before the first start.

$ErrorActionPreference = "Stop"
$Repo = Split-Path $PSScriptRoot -Parent
Set-Location $Repo

if (-not (Test-Path ".venv\Scripts\uvicorn.exe")) {
    Write-Host "No .venv found — run windows\setup.ps1 first." -ForegroundColor Red
    exit 1
}

& .\.venv\Scripts\uvicorn.exe app.api.main:app --reload
