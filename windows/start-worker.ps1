# Starts the arq worker (search -> scrape -> LLM extraction -> dedup -> deliver).
# Start the LLM server first (windows\start-llm.ps1) and wait for its /health —
# the worker is the only component that calls the LLM.
#
# NOTE: the worker reads .env at startup. If you switch the LLM model preset
# (start-llm.ps1 -Model ...) while the worker is running, restart the worker.

$ErrorActionPreference = "Stop"
$Repo = Split-Path $PSScriptRoot -Parent
Set-Location $Repo

if (-not (Test-Path ".venv\Scripts\arq.exe")) {
    Write-Host "No .venv found — run windows\setup.ps1 first." -ForegroundColor Red
    exit 1
}

# Friendly preflight: warn (don't block) if the LLM server isn't up yet.
try {
    Invoke-WebRequest -Uri "http://127.0.0.1:8080/health" -UseBasicParsing -TimeoutSec 3 | Out-Null
} catch {
    Write-Host "WARNING: LLM server not responding on http://127.0.0.1:8080/health" -ForegroundColor Yellow
    Write-Host "         Start it with windows\start-llm.ps1 — LLM jobs will fail until it's up."
}

& .\.venv\Scripts\arq.exe app.worker.settings.WorkerSettings
