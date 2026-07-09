# Local RAG on NVIDIA DGX Spark

Automated setup for running **retrieval-augmented generation (RAG) entirely on your DGX Spark** — point a local Gemma model (served by Ollama) at a folder of papers/data and chat with it, with source citations, fully offline.

```
Ollama  (serves the chat model + an embedding model)
   │
   ├──▶ Open WebUI    general chat UI + "Knowledge" collections   →  http://localhost:3000
   └──▶ AnythingLLM   document-chat / RAG workspaces               →  http://localhost:3001
```

Both UIs share the same Ollama backend on different ports, so you can run either or both.

---

## Files

| File | What it does |
|------|--------------|
| `setup_local_rag.sh` | Installs Ollama, configures it for container access, pulls models, and launches Open WebUI + AnythingLLM. Idempotent — safe to re-run. |
| `llm_stack_healthcheck.sh` | Verifies every component: Ollama API, GPU, a live generation test, both containers, container→Ollama connectivity, and reboot-safe restart policies. |
| `sync_folder.py` | Watches a local folder and uploads/embeds new or changed files into AnythingLLM or Open WebUI, so your knowledge base stays current. |

---

## Prerequisites

- NVIDIA DGX Spark (GB10, ARM64) running Ubuntu 24.04 / DGX OS
- Docker + NVIDIA Container Toolkit (ship with DGX OS)
- Internet access for the first run (to pull the installer, images, and models)

Both Docker images are multi-arch and run natively on ARM64.

---

## Quick start

```bash
chmod +x setup_local_rag.sh llm_stack_healthcheck.sh

# full stack with defaults (chat model gemma4:26b, embedding nomic-embed-text)
./setup_local_rag.sh

# verify
./llm_stack_healthcheck.sh
```

The setup script will, in order:

1. Install Ollama if it isn't already present.
2. Configure Ollama to listen on `0.0.0.0:11434` (a systemd override) so containers can reach it, then restart it.
3. Wait for the Ollama API to come up.
4. Pull the chat model and the embedding model (skipped if already present).
5. Launch **Open WebUI** on port 3000 (with `--gpus all` when a GPU test succeeds).
6. Create AnythingLLM's storage folder with the correct ownership and launch **AnythingLLM** on port 3001.

It's safe to re-run: containers are recreated cleanly, while pulled models, Ollama config, and data volumes are preserved.

### Options
The default model is `Gemma4:26b`.

```bash
./setup_local_rag.sh --chat-model llama3.3:70b   # use a different chat model
./setup_local_rag.sh --embed-model bge-m3         # multilingual / long-context embeddings
./setup_local_rag.sh --skip-openwebui             # AnythingLLM only
./setup_local_rag.sh --skip-anythingllm           # Open WebUI only
./setup_local_rag.sh --skip-models                # don't pull models

# Llama 3.3 70B as the chat model
./setup_local_rag.sh --chat-model llama3.3:70b

# Qwen 3 72B
./setup_local_rag.sh --chat-model qwen3:72b

# the larger dense Gemma
./setup_local_rag.sh --chat-model gemma4:31b
```

You can also edit the `CONFIG` block at the top of the script (ports, storage path, model names).

---

## One-time UI configuration

The scripts stand up the services; the last mile is done once in each web UI.

### AnythingLLM — `http://localhost:3001`

1. Onboarding wizard:
   - **LLM provider:** Ollama → Base URL `http://host.docker.internal:11434` → pick your chat model.
   - **Embedding provider:** Ollama → same URL → `nomic-embed-text`.
   - **Vector database:** LanceDB (bundled, local, default).
2. Create a workspace (e.g. *Papers*), upload the folder's files, click **Save and Embed**.
3. Ask questions — answers come back with per-source citations.

### Open WebUI — `http://localhost:3000`

1. Create the first (admin) account — it's local only.
2. **Admin Settings → Documents** → Embedding engine **Ollama**, model `nomic-embed-text`.
3. **Workspace → Knowledge → +** to create a collection, upload files. Reference it in chat with `#Papers`, or attach it to a custom Model so every chat retrieves from it automatically.

> **Why a separate embedding model?** The chat model (Gemma) writes answers; the embedding model turns your documents into vectors for retrieval. Without one, uploads silently fail. This is why the setup pulls `nomic-embed-text` alongside the chat model.

---

## Keeping the knowledge base updated

Neither tool watches a local directory out of the box. `sync_folder.py` closes that gap: it hashes each file, uploads only new/changed ones, and attaches them to your workspace/collection.

```bash
pip install requests

# AnythingLLM: get an API key at Settings > Tools > Developer API
RAG_BACKEND=anythingllm \
RAG_API_KEY=xxxxxxxx \
RAG_WATCH_DIR=$HOME/papers \
RAG_TARGET=papers \
python3 sync_folder.py
```

For Open WebUI, set `RAG_BACKEND=openwebui`, use an API key from **Settings → Account → API Keys**, and set `RAG_TARGET` to the knowledge collection id.

Run it on a schedule with cron — every 15 minutes:

```bash
# crontab -e
*/15 * * * * RAG_BACKEND=anythingllm RAG_API_KEY=xxxx RAG_WATCH_DIR=$HOME/papers RAG_TARGET=papers /usr/bin/python3 /path/to/sync_folder.py >> $HOME/rag_sync.log 2>&1
```

For near-instant updates instead of polling, swap the loop for Python `watchdog` running as a systemd service. AnythingLLM also has built-in **Scheduled Jobs** and a beta **Live Document Sync** you can use instead.

> API endpoint names shift between tool versions. If a call fails, check the tool's live API docs (Open WebUI: `http://localhost:3000/docs`) and adjust the endpoints in `sync_folder.py`.

---

## Ports

| Service | URL | Port |
|---------|-----|------|
| Ollama API | `http://localhost:11434` | 11434 |
| Open WebUI | `http://localhost:3000` | 3000 |
| AnythingLLM | `http://localhost:3001` | 3001 |

To reach a UI from your laptop, SSH-tunnel: `ssh -L 3001:localhost:3001 user@spark`.

---

## Troubleshooting

These are the exact issues encountered while bringing this stack up, and their fixes (all already handled by the scripts).

**A model works from the Ollama CLI but doesn't appear in a web UI.**
The container can't reach Ollama. By default Ollama binds to `127.0.0.1`; inside a container `host.docker.internal` resolves to the host gateway (e.g. `172.17.0.1`), which Ollama then refuses. Fix: bind Ollama to `0.0.0.0` (the setup script's systemd override) and launch containers with `--add-host=host.docker.internal:host-gateway`. Test from inside a container:
```bash
sudo docker exec -it open-webui curl http://host.docker.internal:11434/api/tags
```

**AnythingLLM crash-loops with `unable to open database file: ../storage/anythingllm.db`.**
Its storage folder isn't writable by the container's user. AnythingLLM runs as UID 1000, so the mounted folder must be owned by it:
```bash
sudo chown -R 1000:1000 $HOME/anythingllm
sudo docker restart anythingllm
```
The setup script does this automatically before launching the container.

**AnythingLLM can't reach Ollama even though Open WebUI can.**
The AnythingLLM container was launched without `--add-host=host.docker.internal:host-gateway`. Recreate it with the flag (the setup script includes it).

**Ollama runs at half speed / high CPU.**
Ollama silently splits a model across CPU and GPU when it thinks GPU memory is short. Check with `ollama ps` — if it shows a CPU/GPU split, choose a smaller model or a heavier quant. On the Spark's 128 GB unified memory (~110 GB usable), keep models comfortably under the ceiling to leave room for the KV cache.

**Do I need to restart containers after a reboot?**
No. Ollama is a systemd service (auto-starts), and both containers use a restart policy (`always` / `unless-stopped`), so Docker relaunches them on boot. Confirm with:
```bash
sudo docker inspect -f '{{.Name}} -> {{.HostConfig.RestartPolicy.Name}}' open-webui anythingllm
```

---

## Choosing a tool

| | AnythingLLM | Open WebUI |
|---|---|---|
| Focus | Document chat / RAG workspaces | General chat UI + Knowledge collections |
| Vector DB | Bundled (LanceDB) | Bundled |
| Citations | Strong, per-source | Yes |
| Built-in scheduling | Yes (Scheduled Jobs) | No (use cron) |
| Best first pick for | A papers library | Everyday multi-model chat that also does RAG |

Start with **AnythingLLM** for a pure "know my papers" use case; run **Open WebUI** alongside for general multi-model chat. They share the same Ollama/Gemma backend.

---

## A note on the model

The original guide assumed there was no "Gemma 4" and suggested Gemma 3 27B. As of 2026, **Gemma 4 does exist** (released April 2026, Apache 2.0 license), with sizes including `gemma4:12b`, `gemma4:26b` (a mixture-of-experts model, ~4B active params per token — a great speed/quality fit for the Spark's bandwidth), and `gemma4:31b`. This stack defaults to `gemma4:26b`. Whatever tag `ollama list` shows is what you select as the chat model — change it with `--chat-model` or in the UI at any time.
