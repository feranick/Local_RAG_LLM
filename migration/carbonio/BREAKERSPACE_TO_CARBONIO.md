# breakerspace → carbonio

**From** the DGX Spark (user `feranick`, instance on :3002, ~103 GB unified memory)
**to** `carbonio.mit.edu` (user `nicola`, Ubuntu 26.04, RTX 5060 Ti 16 GB, instance on :3000).

The Spark copy **stays running**. After this, the two are independent: documents,
notes, captures and chats added to one do not appear in the other.

## What changes, and why it matters

| | Spark | carbonio | Consequence |
|---|---|---|---|
| Model memory | ~103 GB unified | **~15 GB VRAM** | `qwen3.8:27b` (18 GB) no longer fits → new chat model |
| Chat + captions | `qwen3.8:27b-mtp-q4_K_M` | **`gemma4:12b-it-qat`** (7.2 GB, vision) | the preset's base model must be switched by hand |
| Ollama default context | 256k tier | **4k tier** (<24 GiB VRAM) | unpinned, RAG answers silently lose most retrieved context |
| Embedder | `bge-m3` | **`bge-m3`, same name** | a different name makes every stored vector useless |
| Port / home | :3002, `/home/feranick` | :3000, `/home/nicola` | the importer rewrites paths (`NEW_HOME`) and URLs (`URL_MAP`) |

What travels untouched inside the volume: accounts, the preset (knowledge attachments,
system prompt, pinned notes), both collections **with their vectors**, the
Breakerspace Calendar tool, and every existing figure caption.

Two 16 GB facts worth keeping in mind:
- The 5060 Ti moves memory faster than the Spark (~448 vs ~273 GB/s), so a model
  that fits the card will answer quickly.
- A 12B model is weaker than a 27B one at deciding to search. Stage 6 tests this
  before anyone depends on it.

---

## Stage 0 — Prepare carbonio

**Driver** — then reboot:

```bash
sudo ubuntu-drivers install
sudo reboot
nvidia-smi          # must show the RTX 5060 Ti with ~16 GB
```

**Docker + NVIDIA Container Toolkit.** 26.04 isn't on NVIDIA's official support list
yet, but its generic apt repository installs and works:

```bash
# Docker: follow docs.docker.com/engine/install/ubuntu, then
sudo usermod -aG docker $USER && newgrp docker
# NVIDIA Container Toolkit: follow docs.nvidia.com/datacenter/cloud-native/container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
docker run --rm --gpus all nvidia/cuda:12.6.0-base-ubuntu24.04 nvidia-smi   # GPU visible in a container
```

**The repo, the probe, Ollama.** `--skip-openwebui` matters: the installer would
otherwise start an *empty* Open WebUI on port 3000, which is where breakerspace goes.

```bash
git clone https://github.com/feranick/Local_RAG_LLM ~/Software/Local_RAG_LLM
cd ~/Software/Local_RAG_LLM
python3 common/platform_probe.py --write     # expect: discrete, ~15 GB usable
bash management/setup_local_rag.sh --skip-openwebui --skip-anythingllm --skip-models
```

**Pin the context server-wide**, so nothing ever falls back to the 4k tier:

```bash
sudo systemctl edit ollama
#   [Service]
#   Environment="OLLAMA_CONTEXT_LENGTH=16384"
sudo systemctl restart ollama
```

✅ *Check:* `curl -s localhost:11434/api/version` answers, and `docker ps` shows **no**
container on port 3000.

---

## Stage 1 — Export on the Spark

Copy `migrate_breakerspace.conf` to `~/` on the Spark. It moves only the breakerspace
volume, `~/breakerspace`, the two configs, the key and both state files — and it asks
the importer to pull only `bge-m3` and `gemma4:12b-it-qat`, not everything the Spark
has installed.

If you've moved the lab notes out to `~/lab_notes`, add that folder to `DOC_DIRS` first.

```bash
cd ~/Software/Local_RAG_LLM          # with the updated migrate_rag.py
python3 migration/migrate_rag.py --config ~/migrate_breakerspace.conf --export --dry-run
```

✅ *Check:* the dry run lists 1 volume, `~/breakerspace`, and **5** sync files.

The container has to stop briefly so its database is captured in a consistent state —
a few minutes of downtime:

```bash
sudo docker stop open-webui-breakerspace
python3 migration/migrate_rag.py --config ~/migrate_breakerspace.conf --export
sudo docker start open-webui-breakerspace
```

---

## Stage 2 — Transfer

```bash
rsync -avP ~/rag_migration/ nicola@carbonio.mit.edu:~/rag_migration/
```

✅ *Check on carbonio:* `du -sh ~/rag_migration` roughly matches the Spark, and
`manifest.json` is there.

---

## Stage 3 — Import on carbonio

```bash
cd ~/Software/Local_RAG_LLM
python3 migration/migrate_rag.py --config ~/rag_migration/migrate_breakerspace.conf --import --dry-run
```

✅ *Check:* it shows `path rewrite: /home/feranick -> /home/nicola`,
`url rewrite: http://localhost:3002 -> http://localhost:3000`, and rewrites in both
`.conf` files and both state files. If it says *no path rewriting*, **stop** — that
is the duplicate-library trap.

```bash
python3 migration/migrate_rag.py --config ~/rag_migration/migrate_breakerspace.conf --import
```

---

## Stage 4 — Start breakerspace on :3000

The same settings the Spark container runs with, published on 3000. The named volume
already holds all the data:

```bash
docker run -d --name open-webui-breakerspace --restart always \
  -p 3000:8080 --gpus all \
  --ulimit nofile=65536:65536 \
  --add-host=host.docker.internal:host-gateway \
  -e OLLAMA_BASE_URL=http://host.docker.internal:11434 \
  -e RAG_OLLAMA_BASE_URL=http://host.docker.internal:11434 \
  -e RAG_EMBEDDING_ENGINE=ollama -e RAG_EMBEDDING_MODEL=bge-m3 \
  -e RAG_EMBEDDING_BATCH_SIZE=32 -e CHUNK_SIZE=1500 -e CHUNK_OVERLAP=200 \
  -e WEBUI_NAME=breakerspace -e ENABLE_API_KEY=true \
  -e ENABLE_SIGNUP=true -e DEFAULT_USER_ROLE=pending \
  -v open-webui-breakerspace:/app/backend/data \
  ghcr.io/open-webui/open-webui:main

docker logs --tail 60 open-webui-breakerspace 2>&1 | grep -iE 'migrat|alembic|error'
```

The image pulled here may be newer than the Spark's 0.11.3; its first start upgrades
the database, and the log line above shows whether that went cleanly.

✅ *Check:* log in at `http://carbonio.mit.edu:3000` with your **existing** account.
Both collections are listed under Workspace → Knowledge with their document counts.

> carbonio has a campus hostname, and Open WebUI serves plain HTTP. With
> `ENABLE_SIGNUP=true` anyone who can reach port 3000 can register — as *pending*,
> so they need your approval. Set it to `false` if nobody new should join.

---

## Stage 5 — What doesn't carry over by itself

**1. The preset's base model.** It still points at the qwen model, which isn't installed
here. Open the preset's **editor** (pencil icon, or `…/workspace/models/edit?id=<preset-id>`):

- **Base Model** → `gemma4:12b-it-qat`
- **Advanced Params** → `num_ctx` = **16384** (the same as the server setting, so
  Ollama loads the model once, not twice), `temperature` ≈ **0.3**
- Knowledge, Tools (the calendar), the system prompt and the pinned notes are already
  there — just confirm they are.

**2. Admin settings that name a model.** In Admin Panel → Settings:
- **Documents** → embedding model shows `bge-m3`.
- **Interface → Task Model** → leave it on the current chat model. A second model
  would compete for the same 16 GB.

**3. The figure captioner** in `~/breakerspace.conf`, for documents added from now on:

```ini
FIGURE_MODEL   = gemma4:12b-it-qat
FIGURE_NUM_CTX = 16384
```

Existing captions (written by qwen3.8 on the Spark) are kept; `--status` will show a
mixed inventory by model, and nothing needs redoing.

---

## Stage 6 — Verify, in this order

```bash
bash management/llm_stack_healthcheck.sh
python3 sync/sync_folder.py --config ~/breakerspace.conf --status
python3 sync/sync_folder.py --config ~/lab_notes_breakerspace.conf --status
```

✅ *Check:* **`0 to go`** on both. A number close to the whole library means the path
rewrite didn't take — do not sync until it's fixed.

Then in a **new** chat on the preset:

1. A question you know the answer to from the documents, **without** `#`. It should
   cite sources. This is the test the 12B model might fail: if it answers from memory,
   set the preset's **Function Calling = Legacy**, which retrieves on every question
   regardless of the model's judgment.
2. *"What trainings are on this week?"* — the status line "Checking the Breakerspace
   calendar…" should appear before the answer.
3. While a model is loaded:

   ```bash
   ollama ps     # PROCESSOR 100% GPU, CONTEXT 16384
   ```

   Anything less than 100% GPU means the context pushed part of the model into system
   RAM; lower `num_ctx` (and `OLLAMA_CONTEXT_LENGTH`) to 12288.

For a number rather than an impression — and a baseline to compare against later:

```bash
determinism_check --instance http://localhost:3000 \
  --key-file ~/.rag_sync_key_open-webui-breakerspace \
  --model <preset-id> --collection 4cc8efc4-2fe5-4071-abc4-d584390bb7b4 \
  --question "Where are the polished cross-section samples stored?" \
  --expect "sample cabinet" --runs 3
```

---

## Running two copies

Both machines now hold a breakerspace with the same collection ids, on different
hosts. Nothing links them:

- Syncing new documents on one doesn't update the other. Run the sync on each, or
  decide which machine is canonical and only sync there.
- `Remember:` captures belong to the instance you typed them in. Harvest each
  instance with its own config, and both write into their own machine's notes folder.
- The Spark's configs are unchanged (still :3002, `/home/feranick`). The importer only
  rewrote the copies on carbonio.
