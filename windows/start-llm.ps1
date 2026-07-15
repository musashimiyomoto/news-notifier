# Starts the local LLM server (llama.cpp, CUDA build) on the GPU.
#
#   .\start-llm.ps1                     # default preset (qwen3-4b)
#   .\start-llm.ps1 -Model qwen3-1.7b   # switch model by preset name
#   .\start-llm.ps1 -List               # show all presets
#
# Requires the CUDA build of llama.cpp:
#   https://github.com/ggml-org/llama.cpp/releases -> llama-<ver>-bin-win-cuda-x64.zip
# unpacked into C:\llama (or set $env:LLAMA_DIR / pass -LlamaDir).
#
# The model file is downloaded from Hugging Face on first start (-hf flag) and
# cached, so later starts are fast.
#
# Each preset knows whether its model is a "thinking" (reasoning) model and
# updates LLM_DISABLE_THINKING in ..\.env to match — the API client sends
# enable_thinking=false only when that flag is true (see app/llm/client.py),
# and sending it to a model whose chat template doesn't accept it can break
# the request. So: switch models with THIS script, not by editing .env by hand.

param(
    [string]$Model = "qwen3-4b",
    [switch]$List,
    # Total context, split evenly across slots: 8192 / 2 slots = 4096 per
    # request, same per-request budget as the original docker-compose setup.
    [int]$CtxSize = 8192,
    # 2 slots so both worker jobs (WorkerSettings.max_jobs=2) can run their
    # extractions concurrently — on GPU parallel requests genuinely add
    # throughput, unlike the 2-core CPU box docker-compose was tuned for.
    # On a 4 GB card use -Parallel 1 -CtxSize 4096 to save VRAM for KV cache.
    [int]$Parallel = 2,
    [int]$Port = 8080,
    [int]$GpuLayers = 99,   # 99 = offload everything to the GPU
    [string]$LlamaDir = $(if ($env:LLAMA_DIR) { $env:LLAMA_DIR } else { "C:\llama" })
)

$ErrorActionPreference = "Stop"

# ---- Model presets (tuned for a 4-6 GB VRAM card) --------------------------
# disableThinking=true  -> reasoning model, suppress its <think> block
# disableThinking=false -> plain instruct model (template may reject the kwarg)
$Presets = [ordered]@{
    "qwen3-1.7b" = @{
        hf = "bartowski/Qwen_Qwen3-1.7B-GGUF:Q4_K_M"
        disableThinking = $true
        vram = "~1.4 GB"
        note = "Fastest; the project's original default. Weakest judgement."
    }
    "llama3.2-3b" = @{
        hf = "bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M"
        disableThinking = $false
        vram = "~2.2 GB"
        note = "Fast, solid instruction following."
    }
    "qwen3-4b" = @{
        hf = "bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF:Q4_K_M"
        disableThinking = $false
        vram = "~2.8 GB"
        note = "DEFAULT. Best quality/speed balance for 4-6 GB VRAM."
    }
    "gemma3-4b" = @{
        hf = "bartowski/google_gemma-3-4b-it-GGUF:Q4_K_M"
        disableThinking = $false
        vram = "~2.8 GB"
        note = "Alternative 4B; strong summarization."
    }
    "qwen3-8b" = @{
        hf = "bartowski/Qwen_Qwen3-8B-GGUF:Q4_K_M"
        disableThinking = $true
        vram = "~5.5 GB"
        note = "Only for 6 GB cards (tight!). Best judgement, slowest."
    }
}

if ($List) {
    Write-Host ""
    Write-Host ("{0,-13} {1,-9} {2}" -f "PRESET", "VRAM", "NOTE") -ForegroundColor Cyan
    foreach ($name in $Presets.Keys) {
        $p = $Presets[$name]
        Write-Host ("{0,-13} {1,-9} {2}" -f $name, $p.vram, $p.note)
    }
    Write-Host ""
    exit 0
}

if (-not $Presets.Contains($Model)) {
    Write-Host "Unknown preset '$Model'. Available:" -ForegroundColor Red
    $Presets.Keys | ForEach-Object { Write-Host "  $_" }
    Write-Host "Or run: .\start-llm.ps1 -List"
    exit 1
}
$Preset = $Presets[$Model]

# ---- Locate llama-server.exe ------------------------------------------------
$LlamaExe = $null
$inPath = Get-Command "llama-server.exe" -ErrorAction SilentlyContinue
if ($inPath) {
    $LlamaExe = $inPath.Source
} elseif (Test-Path (Join-Path $LlamaDir "llama-server.exe")) {
    $LlamaExe = Join-Path $LlamaDir "llama-server.exe"
} else {
    Write-Host "llama-server.exe not found (looked in PATH and '$LlamaDir')." -ForegroundColor Red
    Write-Host "Download the CUDA build:  https://github.com/ggml-org/llama.cpp/releases"
    Write-Host "  -> llama-<version>-bin-win-cuda-x64.zip, unpack to $LlamaDir"
    Write-Host "  (or set `$env:LLAMA_DIR / pass -LlamaDir <path>)"
    exit 1
}

# ---- Sync LLM_DISABLE_THINKING in .env with the chosen model ----------------
$EnvFile = Join-Path (Split-Path $PSScriptRoot -Parent) ".env"
$WantValue = if ($Preset.disableThinking) { "true" } else { "false" }
if (Test-Path $EnvFile) {
    $content = Get-Content $EnvFile -Raw
    if ($content -match "(?m)^LLM_DISABLE_THINKING=") {
        $new = $content -replace "(?m)^LLM_DISABLE_THINKING=.*$", "LLM_DISABLE_THINKING=$WantValue"
    } else {
        $new = $content.TrimEnd() + "`nLLM_DISABLE_THINKING=$WantValue`n"
    }
    if ($new -ne $content) {
        Set-Content -Path $EnvFile -Value $new -NoNewline
        Write-Host "Updated .env: LLM_DISABLE_THINKING=$WantValue (required for '$Model')" -ForegroundColor Yellow
        Write-Host "NOTE: restart the worker if it is already running, it reads .env at startup." -ForegroundColor Yellow
    }
} else {
    Write-Host "WARNING: .env not found next to the project — run windows\setup.ps1 first." -ForegroundColor Yellow
    Write-Host "The worker will need LLM_DISABLE_THINKING=$WantValue for this model."
}

# ---- Go ---------------------------------------------------------------------
Write-Host ""
Write-Host "Model   : $Model  ($($Preset.hf))" -ForegroundColor Green
Write-Host "VRAM    : $($Preset.vram)   GPU layers: $GpuLayers   ctx: $CtxSize ($Parallel slot(s))"
Write-Host "Server  : http://127.0.0.1:$Port  (health: /health)"
Write-Host "First start downloads the model from Hugging Face — be patient."
Write-Host ""

& $LlamaExe `
    -hf $Preset.hf `
    --host 127.0.0.1 `
    --port $Port `
    --n-gpu-layers $GpuLayers `
    --ctx-size $CtxSize `
    --parallel $Parallel `
    --jinja
