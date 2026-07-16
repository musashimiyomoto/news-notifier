# Running on Windows with an NVIDIA GPU, no Docker

Every component runs natively; the GPU is used for the LLM (llama.cpp CUDA
build). No project code is modified — the scripts in this folder only launch
what the root README describes.

## Components

| Component | Replaces this Docker service | Port |
|---|---|---|
| Postgres + pgvector | EDB installer + pgvector extension (Windows service) | 5432 |
| Redis | [Memurai](https://www.memurai.com/get-memurai) — native Redis-compatible server (Windows service) | 6379 |
| LLM | `llama-server.exe` (CUDA) via `start-llm.ps1` — **on the GPU** | 8080 |
| API | `start-api.ps1` (uvicorn) | 8000 |
| Worker | `start-worker.ps1` (arq) | — |

## Installation (one-time)

1. **Python 3.11+** — https://www.python.org/downloads/windows/ (check *Add python.exe to PATH*).
2. **NVIDIA driver** — https://www.nvidia.com/download/index.aspx. No CUDA Toolkit needed: the CUDA build of llama.cpp bundles the runtime. Verify with `nvidia-smi`.
3. **PostgreSQL 16** — https://www.enterprisedb.com/downloads/postgres-postgresql-downloads. Superuser password: `postgres`, port 5432 (matches the default `DATABASE_URL`).
4. **pgvector** — grab the Windows build for PG16 from https://github.com/pgvector/pgvector/releases (or build it per the [instructions](https://github.com/pgvector/pgvector#windows)) and copy:
   - `vector.dll` → `C:\Program Files\PostgreSQL\16\lib\`
   - `vector.control`, `vector--*.sql` → `C:\Program Files\PostgreSQL\16\share\extension\`
5. **Memurai Developer** — https://www.memurai.com/get-memurai. Runs as a service on 6379 after install. Verify: `memurai-cli ping` → `PONG`.
6. **llama.cpp CUDA** — from https://github.com/ggml-org/llama.cpp/releases take `llama-<version>-bin-win-cuda-x64.zip`, unpack to `C:\llama` (different path — set `$env:LLAMA_DIR` or pass `-LlamaDir`).
7. Set up the project (venv, dependencies, Chromium for Playwright, `.env` with a fresh key, database, migrations, seeds):

   ```powershell
   powershell -ExecutionPolicy Bypass -File windows\setup.ps1
   ```

## Starting (every time)

Postgres and Memurai are already running as services. Open three PowerShell windows:

```powershell
# 1 — LLM on the GPU (first start downloads the model from Hugging Face)
windows\start-llm.ps1
# wait until http://127.0.0.1:8080/health returns OK

# 2 — API  → http://localhost:8000  (UI: /ui)
windows\start-api.ps1

# 3 — worker
windows\start-worker.ps1
```

## Choosing a model

Switch models with a single parameter — the presets are tuned for a
**4–6 GB VRAM** card (all of them except `qwen3-8b` fit on the GPU entirely,
with headroom for the context):

```powershell
windows\start-llm.ps1 -List               # show presets
windows\start-llm.ps1 -Model qwen3-4b     # start a specific model
```

| Preset | Model | VRAM | When to pick it |
|---|---|---|---|
| `qwen3-4b` | Qwen3-4B-Instruct-2507 Q4_K_M | ~2.8 GB | **Default.** Best quality/speed balance for 4–6 GB. On the GPU it runs many times faster than 1.7B did on CPU. |
| `qwen3-1.7b` | Qwen3-1.7B Q4_K_M | ~1.4 GB | Maximum speed; the project's original default (tuned for a weak CPU). Weaker at credibility judgement. |
| `llama3.2-3b` | Llama-3.2-3B-Instruct Q4_K_M | ~2.2 GB | Fast alternative to the 4B, follows the JSON schema well. |
| `gemma3-4b` | Gemma-3-4B-it Q4_K_M | ~2.8 GB | Alternative to Qwen3-4B, strong at summarization. |
| `qwen3-8b` | Qwen3-8B Q4_K_M | ~5.5 GB | 6 GB cards only, and it's tight. Strongest judgement, but slower — and if VRAM runs out, some layers spill to the CPU. |

The script keeps `LLM_DISABLE_THINKING` in `.env` in sync with the chosen
model (Qwen3-1.7B/8B have a reasoning mode that must be suppressed;
Instruct models don't). **So switch models through the script, not by
hand-editing `.env`**, and restart the worker after switching — it reads
`.env` at startup.

Picking advice: start with `qwen3-4b`. If extraction feels slow, try
`llama3.2-3b`; if you want sharper scoring and have 6 GB, try `qwen3-8b` —
but watch `nvidia-smi`: once VRAM overflows, speed drops off a cliff.

## GPU tuning

The project defaults were tuned for a weak CPU box; on a GPU some of them can
be relaxed. `start-llm.ps1` already handles the GPU side itself:
`--parallel 2` (two concurrent requests — paired with the worker's
`max_jobs=2`) and `--ctx-size 8192` (4096 per slot). On a 4 GB card, if VRAM
is tight: `-Parallel 1 -CtxSize 4096`.

Optionally in `.env` (restart the worker after editing):

```ini
# A GPU chews through a long prompt in seconds, not minutes — you can feed
# more of each article for a more accurate judgement (default 4000):
EXTRACTION_MAX_CHARS=6000
```

`LLM_REQUEST_TIMEOUT_SECONDS=600` can stay as is: on a GPU an extraction takes
seconds and the timeout simply never fires, while the headroom for the first
cold start (loading the model into VRAM) does no harm.

## Troubleshooting

- **PowerShell refuses to run .ps1** — once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
- **`CREATE EXTENSION vector` fails** — the pgvector files were copied into the wrong Postgres version; check the `...\PostgreSQL\16\...` path.
- **Worker logs ReadTimeout from the LLM** — the LLM server isn't running or the model is still downloading; check `curl http://127.0.0.1:8080/health`.
- **Model runs slowly, GPU sits idle** — make sure you downloaded the `cuda` build of llama.cpp (not `cpu`/`vulkan`), and that the server's startup log shows layers going to the GPU (`offloaded 99/… layers to GPU`).
