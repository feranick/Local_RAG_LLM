# Local RAG on NVIDIA DGX Spark

Automated setup for running **retrieval-augmented generation (RAG) entirely on your DGX Spark** — point a local Gemma model (served by Ollama) at a folder of papers/data and chat with it, with source citations, fully offline.

This repo reproduces, in a few scripts, the stack:

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
| `setup_local_rag.sh` | Installs Ollama, configures it for container access (creates a systemd service if none exists), pulls models, and launches Open WebUI + AnythingLLM. Idempotent — safe to re-run. |
| `llm_stack_healthcheck.sh` | Verifies every component: Ollama API, GPU, a live generation test, both containers, container→Ollama connectivity, and reboot-safe restart policies. |
| `sync_folder.py` | Watches a local folder and uploads/embeds new or changed files into AnythingLLM or Open WebUI, so your knowledge base stays current. |
| `uninstall_local_rag.sh` | Removes the stack. Safe by default (keeps data); `--purge-data` wipes everything including models. |

---

## Prerequisites

- NVIDIA DGX Spark (GB10, ARM64) running Ubuntu 24.04 / DGX OS
- Docker + NVIDIA Container Toolkit (ship with DGX OS)
- Internet access for the first run (to pull the installer, images, and models)

Both Docker images are multi-arch and run natively on ARM64.

---

## Quick start

```bash
chmod +x setup_local_rag.sh llm_stack_healthcheck.sh uninstall_local_rag.sh

# full stack with defaults (chat model gemma4:26b, embedding nomic-embed-text)
./setup_local_rag.sh

# verify
./llm_stack_healthcheck.sh
```

The setup script will, in order:

1. Install Ollama if it isn't already present.
2. Ensure Ollama listens on `0.0.0.0:11434` so containers can reach it. If a systemd service exists it adds a bind override; if none exists (e.g. a manual `ollama serve`) it creates a proper service that runs as your user and starts on boot.
3. Wait for the Ollama API to come up.
4. Pull the chat model and the embedding model (skipped if already present).
5. Launch **Open WebUI** on port 3000 (with `--gpus all` when a GPU test succeeds).
6. Create AnythingLLM's storage folder with the correct ownership and launch **AnythingLLM** on port 3001.

It's safe to re-run: containers are recreated cleanly, while pulled models, Ollama config, and data volumes are preserved. When run with `sudo`, it correctly targets your real home directory (not root's).

### Options

```bash
./setup_local_rag.sh --chat-model llama3.3:70b   # use a different chat model
./setup_local_rag.sh --embed-model bge-m3         # multilingual / long-context embeddings
./setup_local_rag.sh --skip-openwebui             # AnythingLLM only
./setup_local_rag.sh --skip-anythingllm           # Open WebUI only
./setup_local_rag.sh --skip-models                # don't pull models
```

You can also edit the `CONFIG` block at the top of the script (ports, storage path, model names).

---

## One-time UI configuration

The scripts stand up the services; the last mile is done once in each web UI.

> **Accessing a remote Spark (connecting by IP, not sitting at the machine).**
> There are two different kinds of address below, and only one changes when you're remote:
>
> - **Browser URLs** (`http://localhost:3000`, `http://localhost:3001`) — how *you* reach the web UIs. From another machine, replace `localhost` with the Spark's address: `http://<spark-ip>:3000` and `http://<spark-ip>:3001`. (Or keep using `localhost` over an SSH tunnel — see [Ports](#ports).)
> - **The Ollama base URL you type *inside* a UI** (`http://host.docker.internal:11434`) — this is a **container → host** address, i.e. the container reaching Ollama running on the Spark. It has nothing to do with how your browser connects, so **leave it exactly as `http://host.docker.internal:11434`** whether you're local or remote. Do *not* change it to the Spark's IP.
>
> So when the steps below say `localhost:3001`, read it as "the Spark's IP on port 3001" if you're remote — but every `host.docker.internal` stays verbatim.

### AnythingLLM — `http://localhost:3001`

1. Onboarding wizard:
   - **LLM provider:** Ollama → Base URL `http://host.docker.internal:11434` → pick your chat model.
   - **Embedding provider:** Ollama → same URL → `nomic-embed-text`.
   - **Vector database:** LanceDB (bundled, local, default).
2. Create a workspace (e.g. *Papers*), upload the folder's files, click **Save and Embed**.
3. Ask questions — answers come back with per-source citations.

### Open WebUI — `http://localhost:3000`

1. Create the first (admin) account — click **Sign up**; the first account created becomes admin. It's local only, and works from any address (localhost or LAN IP) since it's stored server-side.
2. **Admin Settings → Documents**:
   - **Embedding Model Engine:** Ollama, with URL `http://host.docker.internal:11434`.
   - **Embedding Model:** this is a **free-text field, not a dropdown** — it will not auto-populate. Click into it and *type* the model name exactly as `ollama list` shows it: `nomic-embed-text` (use `nomic-embed-text:latest` if the short name is rejected). Then scroll down and click **Save** — the field doesn't apply until you save.
   - If you'd already uploaded documents, click **Reindex** afterward so they're re-embedded with this model.
   - (Optional) Chunk Size defaults to 1000 / overlap 100. For dense research papers, ~1500 / ~200 can improve retrieval; leave defaults otherwise.
3. **Workspace → Knowledge → +** to create a collection, upload files. Reference it in chat with `#Papers`, or attach it to a custom Model so every chat retrieves from it automatically.

> **Why a separate embedding model?** The chat model (Gemma) writes answers; the embedding model turns your documents into vectors for retrieval. Without one, uploads silently fail. This is why the setup pulls `nomic-embed-text` alongside the chat model.

---

## Keeping the knowledge base updated

Neither tool watches a local directory out of the box. `sync_folder.py` closes that gap: it hashes each file, uploads only new/changed ones, and attaches them to your workspace/collection. Changed files are re-uploaded cleanly (the old copy is removed first, so no duplicates).

By default it only *adds/updates* — deleting a file from the folder leaves it in the collection. Add `--prune` (or set `RAG_PRUNE=1`) to make it a true **mirror**: files removed from the folder are also removed from the collection, so the folder becomes the single source of truth.

> **Don't mix methods on one collection.** The script tracks what *it* uploaded; documents you add by hand in the GUI are invisible to it. If you both drag files into the GUI and sync the same folder, you'll get duplicates, and `--prune` won't touch the GUI-added ones. Pick one method per collection: either manage documents entirely in the GUI, or entirely via the folder + script.

```bash
pip install requests

# AnythingLLM: get an API key at Settings > Tools > Developer API
RAG_BACKEND=anythingllm \
RAG_API_KEY=xxxxxxxx \
RAG_WATCH_DIR=$HOME/papers \
RAG_TARGET=papers \
python3 sync_folder.py
```

For Open WebUI, set `RAG_BACKEND=openwebui` and set `RAG_TARGET` to the knowledge collection id. Getting the API key takes two steps:

1. **Enable the feature:** Admin Panel → Settings → **Authentication** → turn on **Enable API Key** (Save). In v0.10.x this toggle lives under Authentication, not General.
2. **Create the key:** your user **Settings → Account**, scroll to the bottom, create a key (starts with `sk-`), and paste that into the script as `RAG_API_KEY`.

The knowledge collection id is the last part of the collection's URL: `.../knowledge/<this-id>`.

Run it on a schedule with cron — every 15 minutes (add `--prune` to keep the collection mirrored to the folder):

```bash
# crontab -e
*/15 * * * * RAG_BACKEND=anythingllm RAG_API_KEY=xxxx RAG_WATCH_DIR=$HOME/papers RAG_TARGET=papers /usr/bin/python3 /path/to/sync_folder.py --prune >> $HOME/rag_sync.log 2>&1
```

For near-instant updates instead of polling, swap the loop for Python `watchdog` running as a systemd service. AnythingLLM also has built-in **Scheduled Jobs** and a beta **Live Document Sync** you can use instead.

> API endpoint names shift between tool versions. If a call fails, check the tool's live API docs (Open WebUI: `http://localhost:3000/docs`) and adjust the endpoints in `sync_folder.py`.

---

## Uninstall / clean reinstall

```bash
# remove the software but KEEP your data + models (default, safe)
./uninstall_local_rag.sh

# remove EVERYTHING including data and pulled models (for a from-scratch test)
./uninstall_local_rag.sh --purge-data

# remove only the containers, leave Ollama intact
./uninstall_local_rag.sh --keep-ollama
```

To test the setup script from a clean system:

- **True fresh test:** `./uninstall_local_rag.sh --purge-data` then `./setup_local_rag.sh`. Note this re-downloads the models (~17 GB for `gemma4:26b` + `nomic-embed-text`).
- **Fast iteration:** `./uninstall_local_rag.sh` (no purge) keeps the models on disk, so the re-run reuses them.

Docker and the NVIDIA Container Toolkit are never removed — they ship with DGX OS and other apps rely on them.

---

## Ports

| Service | URL | Port |
|---------|-----|------|
| Ollama API | `http://localhost:11434` | 11434 |
| Open WebUI | `http://localhost:3000` | 3000 |
| AnythingLLM | `http://localhost:3001` | 3001 |

To reach a UI from another machine you have two options:

- **Direct by IP:** browse to `http://<spark-ip>:3000` / `:3001`. The containers already publish on `0.0.0.0`, so this works as long as your network/firewall allows those ports. Since Open WebUI and AnythingLLM accounts are served over plain HTTP, only do this on a trusted network.
- **SSH tunnel (more secure):** `ssh -L 3000:localhost:3000 -L 3001:localhost:3001 user@<spark-ip>`, then browse to `http://localhost:3000` on your laptop as if it were local.

Either way, the in-UI Ollama base URL stays `http://host.docker.internal:11434` — see the note under [One-time UI configuration](#one-time-ui-configuration).

---

## Troubleshooting

These are the exact issues encountered while bringing this stack up, and their fixes (all now handled by the scripts).

**A model works from the Ollama CLI but doesn't appear in a web UI, or a container "cannot reach Ollama".**
The container can't reach Ollama. By default Ollama binds to `127.0.0.1`; inside a container `host.docker.internal` resolves to the host gateway (e.g. `172.17.0.1`), which Ollama then refuses. Fix: bind Ollama to `0.0.0.0` and launch containers with `--add-host=host.docker.internal:host-gateway`. Check what address Ollama is bound to:
```bash
sudo ss -tlnp | grep 11434     # want 0.0.0.0:11434, not 127.0.0.1:11434
```
Test from inside a container:
```bash
sudo docker exec -it open-webui curl http://host.docker.internal:11434/api/tags
```

**"Unit ollama.service could not be found" / systemd service not active but API works.**
Ollama is running as a manual `ollama serve` (bound to localhost), with no systemd service. The setup script now detects this and creates a proper service that runs as your user and binds `0.0.0.0`. To fix by hand: `sudo pkill -f "ollama serve"`, create `/etc/systemd/system/ollama.service` with `User=<you>` and `Environment="OLLAMA_HOST=0.0.0.0:11434"`, then `sudo systemctl enable --now ollama`. Don't start `ollama serve` manually afterward — the service handles it; the CLI still works.

**AnythingLLM crash-loops with `unable to open database file: ../storage/anythingllm.db`.**
Its storage folder isn't writable by the container's user. AnythingLLM runs as UID 1000, so the mounted folder must be owned by it:
```bash
sudo chown -R 1000:1000 $HOME/anythingllm
sudo docker restart anythingllm
```
The setup script does this automatically before launching the container.

**AnythingLLM can't reach Ollama even though Open WebUI can.**
The AnythingLLM container was launched without `--add-host=host.docker.internal:host-gateway`. Recreate it with the flag (the setup script includes it).

**Open WebUI shows an "authorization failure" over the LAN IP.**
No admin account exists yet, or you're on the login screen without one. Click **Sign up** — the first account becomes admin and works from any address.

**Can't find API Keys in Open WebUI (needed for the sync script).**
The API Keys section doesn't appear in **Settings → Account** until the feature is enabled by an admin. Go to **Admin Panel → Settings → Authentication** and turn on **Enable API Key** (in v0.10.x it's under Authentication, not General). Then **Settings → Account** shows the section — scroll down, create a key (`sk-...`), and paste it into the sync script as `RAG_API_KEY`. Not needed at all if you upload documents through the UI instead of using the sync script.

**The Embedding Model field in Open WebUI won't populate / stays empty even after refresh.**
It isn't a dropdown — it's a free-text input. Type the model name in by hand (`nomic-embed-text`, or `nomic-embed-text:latest`) exactly as `ollama list` shows it, then click **Save**. This is expected behavior, not a connection problem — you can confirm the connection is fine with:
```bash
sudo docker exec open-webui curl -s http://host.docker.internal:11434/api/tags | grep -o '"name":"[^"]*"'
```

**Ollama runs at half speed / high CPU, or a generation is very slow.**
Ollama silently splits a model across CPU and GPU when it thinks GPU memory is short. Check with `ollama ps` — if it shows a CPU/GPU split, choose a smaller model or a heavier quant. On the Spark's 128 GB unified memory (~110 GB usable), keep models comfortably under the ceiling to leave room for the KV cache. A slow first response is often just the model loading from disk.

**Unloading a model from memory.**
`ollama ps` shows what's loaded; `ollama stop <model>` unloads it immediately. Ollama also auto-unloads after ~5 min idle (tune with the `keep_alive` setting or `OLLAMA_KEEP_ALIVE`).

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

## Model notes

Sizes that fit the Spark's 128 GB unified memory (~110 GB usable), roughly: FP16 up to ~55B params, INT8 up to ~110B, and 4-bit/NVFP4 up to ~200B. The real bottleneck is memory bandwidth (~273 GB/s), so bigger models fit but generate more slowly — 4-bit quants are the sweet spot.

This stack defaults to `gemma4:26b` (a mixture-of-experts model, ~4B active params per token — fast on the Spark's bandwidth). Other good picks: `gemma4:31b`, `llama3.3:70b`, `qwen3:72b`, `deepseek-r1:70b`. Whatever tag `ollama list` shows is what you select as the chat model — change it with `--chat-model` or in the UI at any time.
