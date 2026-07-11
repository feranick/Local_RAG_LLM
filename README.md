# Local RAG on NVIDIA DGX Spark

**Version 2026.07.11.2**

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
| `setup_local_rag.sh` | Installs Ollama, configures it for container access (creates a systemd service if none exists), interactively selects + pulls the chat/embedding/vision models, and launches Open WebUI + AnythingLLM. Idempotent — safe to re-run. |
| `llm_stack_healthcheck.sh` | Verifies every component: Ollama API, GPU, an embedding model, a live generation test (auto-detects whichever chat model is installed), both containers, container→Ollama connectivity, reboot-safe restart policies, and any optional add-ons (Tika, vision model). Model-agnostic — adapts to whatever you selected at setup. |
| `sync_folder.py` | Syncs a local folder into an AnythingLLM workspace or Open WebUI collection: adds new/changed files, mirrors deletions (`--prune`), re-syncs on demand (`--force`), auto-OCRs text-less PDFs (`--ocr-fallback`), and describes figures in PDFs and standalone images via a vision model (`--describe-figures`). |
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

After pulling, the script **verifies each model actually loads** (a tiny generate/embeddings call), not just that it downloaded — so a model that's pulled but won't run on this Ollama build (like `llama3.2-vision`'s `mllama` architecture) is flagged immediately rather than failing later.

It's safe to re-run: containers are recreated cleanly, while pulled models, Ollama config, and data volumes are preserved. When run with `sudo`, it correctly targets your real home directory (not root's).

### Choosing models

When run in a terminal, the script **interactively prompts you to pick the models** — press Enter for the default or type a number / custom tag:

- **Chat model (LLM):** `gemma4:26b` (default, MoE — fast), `gemma4:31b`, `gemma4:12b`, `llama3.3:70b`, `qwen3.6:27b` (dense, higher quality), `qwen3.6:35b` (35B-A3B MoE, faster), or a custom tag.
- **Embedding model:** `nomic-embed-text` (default), `mxbai-embed-large`, `bge-m3`, or custom.
- **Vision model (optional, for `--describe-figures`):** `llava` (default — loads on the Spark), `moondream`, `bakllava`, `none`, or custom.

(Note: `qwen3.6` is an Alibaba model — listed for completeness; it and other Chinese models are your call. For vision, avoid `llama3.2-vision` on the Spark — its `mllama` architecture won't load on this build; `llava` works.)

### Options / non-interactive

Pass any model explicitly (skips that prompt), or `--no-prompt` to skip all menus — useful for automation:

```bash
./setup_local_rag.sh --chat-model llama3.3:70b    # use a different chat model
./setup_local_rag.sh --embed-model bge-m3          # multilingual / long-context embeddings
./setup_local_rag.sh --vision-model llava          # also pull a vision model for figures/images
./setup_local_rag.sh --no-prompt                   # accept defaults, no menus (automation)
./setup_local_rag.sh --skip-openwebui              # AnythingLLM only
./setup_local_rag.sh --skip-anythingllm            # Open WebUI only
./setup_local_rag.sh --skip-models                 # don't pull models
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
3. **Workspace → Knowledge → +** to create a collection and upload files (see "Chatting with your papers" below for how to use it).
4. Set Gemma as the default and hide the embedding model from the chat list:
   - **Default chat model:** your **Settings → General → Default Model** → `gemma4:26b` (so new chats start on Gemma, not the embedding model). Note this is *not* the "Local/External Task Model" under Admin → Settings → Interface — that's only for background tasks like title/tag generation; leave it on "Current Model".
   - **Hide the embedding model:** **Admin Panel → Settings → Models** → toggle `nomic-embed-text` off. It stays available for embedding documents; this just removes it from the chat dropdown so it can't be picked (or auto-selected) as a chat model.

> **Why a separate embedding model?** The chat model (Gemma) writes answers; the embedding model turns your documents into vectors for retrieval. Without one, uploads silently fail. This is why the setup pulls `nomic-embed-text` alongside the chat model. It is not a chat model — never select it to chat with (see troubleshooting if a chat echoes your prompt back).

### Chatting with your papers (Open WebUI)

The collection must already exist **and contain documents** (check **Workspace → Knowledge**) before it can be used. Then, in a **New Chat**:

1. Select `gemma4:26b` in the model dropdown at the top.
2. Type `#` in the message box and **click your collection** from the popup that appears (don't just type the text `#Papers` — that's literal text and attaches nothing). You'll see the collection appear as a chip above the input.
3. Ask your question. Answers come back grounded in the papers, with sources.

To make retrieval always-on without typing `#` each time, go to **Workspace → Models → + New Model**, set the base model to `gemma4:26b`, attach your Knowledge collection, optionally add a system prompt ("Answer using the attached papers and cite sources."), and save. That preset then appears in the model dropdown and retrieves automatically.

---

## Keeping the knowledge base updated

Neither tool watches a local directory out of the box. `sync_folder.py` closes that gap: it hashes each file, uploads only new/changed ones, and attaches them to your workspace/collection. Changed files are re-uploaded cleanly (the old copy is removed first, so no duplicates).

By default it only *adds/updates* — deleting a file from the folder leaves it in the collection. Add `--prune` (or set `RAG_PRUNE=1`) to make it a true **mirror**: files removed from the folder are also removed from the collection, so the folder becomes the single source of truth.

> **Don't mix methods on one collection.** The script tracks what *it* uploaded; documents you add by hand in the GUI are invisible to it. If you both drag files into the GUI and sync the same folder, you'll get duplicates, and `--prune` won't touch the GUI-added ones. Pick one method per collection: either manage documents entirely in the GUI, or entirely via the folder + script.

**Create the collection first.** Open WebUI/AnythingLLM won't auto-create it — make the Knowledge collection (Open WebUI: **Workspace → Knowledge → + New Knowledge**) or workspace *before* syncing, and point `RAG_TARGET` at its id. If you sync to a non-existent target, files upload but land in no collection and `#` shows nothing.

### Configuring the script

There are four settings. Three live in the **CONFIG block at the top of `sync_folder.py`**; the API key is kept in a separate file. Any matching environment variable overrides the in-file value.

| Setting | In-file variable | Where the value comes from |
|---------|------------------|----------------------------|
| Backend | `BACKEND` | `"openwebui"` or `"anythingllm"` |
| Folder to sync | `WATCH_DIR` | a path — defaults to `~/papers` (see path note below) |
| Target collection | `TARGET` | the collection id / workspace slug (see "Finding the id" below) |
| API key | (not in the file) | `~/.rag_sync_key` (see "Finding the API key" below) |

**Editing paths (`WATCH_DIR`) — important:** this is Python, so do **not** use `$HOME` (Python won't expand it) and do **not** rename the variable to `RAG_WATCH_DIR` (that's only the env-var name it falls back to). The default already points at `~/papers`, so you usually don't touch it. To hardcode a different folder, change only the fallback path using a full path or `pathlib.Path.home()`:

```python
# default — leave as-is for ~/papers:
WATCH_DIR = pathlib.Path(os.environ.get("RAG_WATCH_DIR", str(pathlib.Path.home() / "papers")))

# or hardcode an absolute path:
WATCH_DIR = pathlib.Path(os.environ.get("RAG_WATCH_DIR", "/home/feranick/research/papers"))
```

**Finding the API key (`RAG_API_KEY`):**

- **Open WebUI:** first enable it — **Admin Panel → Settings → Authentication → Enable API Key** (Save). Then create it — your **avatar → Settings → Account**, scroll to the bottom, create a key (starts with `sk-`). In v0.10.x the enable toggle is under Authentication, *not* General.
- **AnythingLLM:** **Settings → Tools → Developer API**.

Store it once in the key file (keeps the secret out of the script and out of cron):

```bash
echo 'sk-your-key-here' > ~/.rag_sync_key && chmod 600 ~/.rag_sync_key
```

**Finding the target id (`RAG_TARGET`):**

- **Open WebUI:** the knowledge collection id is the last part of its URL — open the collection under **Workspace → Knowledge** and copy the `...` from `.../knowledge/<this-id>`.
- **AnythingLLM:** the workspace **slug** (the URL-safe name in the workspace's address).

The collection/workspace must already exist and contain (or be about to receive) documents — the script uploads *into* it, it does not create it.

### Running it

```bash
pip install requests                       # one-time dependency

python3 sync_folder.py                     # add/update only
python3 sync_folder.py --prune             # full mirror (also removes deleted files)
python3 sync_folder.py --ocr-fallback      # auto-OCR PDFs that extract to empty, then retry
python3 sync_folder.py --describe-figures  # also index vision descriptions of plots/figures
python3 sync_folder.py --force             # re-sync everything even if unchanged (see "Re-syncing")
```

### Re-syncing / resetting

The script skips files whose content hasn't changed. To deliberately re-upload and re-embed papers you've already synced — e.g. after changing the embedding model, chunk size, or extraction engine — use `--force`:

```bash
python3 sync_folder.py --force
```

`--force` treats every file as changed and, for anything it previously uploaded, **removes the old copy first (via its tracked remote id) before re-uploading** — so you get a clean refresh with no duplicates. It also regenerates figure docs if combined with `--describe-figures`.

If you'd rather start completely fresh, delete the state file so nothing is remembered:

```bash
rm ~/.rag_sync_state.json
python3 sync_folder.py
```

But note: deleting the state file forgets the remote ids, so the old copies are **not** removed and you'll get duplicates in the collection. For a clean slate this way, first empty (or delete and recreate) the Knowledge collection in the UI, then delete the state file and sync. In general, prefer `--force` — it's the tidy option.

`--ocr-fallback` (or `RAG_OCR_FALLBACK=1`): when the server reports "content empty" for a PDF, the script runs `ocrmypdf --force-ocr` on it locally and retries the upload once — since both the script and OCR run on the Spark, it can self-heal text-less PDFs with no manual step. Flags combine, e.g. `--prune --ocr-fallback`.

Install the OCR tools once (only needed if you use `--ocr-fallback` or OCR PDFs by hand):

```bash
sudo apt update
sudo apt install -y ocrmypdf jbig2enc
```

`ocrmypdf` pulls in Tesseract (the OCR engine) and Ghostscript automatically. `jbig2enc` is optional but recommended — it handles the "JBIG2" step you saw in the ocrmypdf output, compressing scanned/monochrome pages so the OCR'd PDFs don't balloon in size. For OCR in languages other than English, add the matching Tesseract pack, e.g. `sudo apt install tesseract-ocr-fra` for French.

> **Caveat:** OCR fallback only helps when a PDF genuinely lacks a text layer. It will *not* fix a misconfigured extraction engine (e.g. Tika selected but not running) — in that case even the OCR'd copy fails, and the script reports it and moves on. So if *every* file fails, fix the engine (see troubleshooting / Tika section), don't rely on OCR.

Environment variables override the in-file defaults for a one-off run:

```bash
RAG_BACKEND=anythingllm RAG_TARGET=papers python3 sync_folder.py
```

### Making figures/plots retrievable (`--describe-figures`)

Text-only RAG can't "see" plots — the data locked in figures is invisible to retrieval. `--describe-figures` bridges that: for each PDF it renders every page, has a **local vision model** (LLaVA, via Ollama) describe any figures/plots/charts (caption, axes, series, trends, legible values), and uploads those descriptions as a companion document so they're retrievable alongside the text, with citations.

One-time setup:

```bash
ollama pull llava                 # the vision model (~5 GB)
pip install pymupdf               # renders PDF pages to images
```

Then:

```bash
python3 sync_folder.py --describe-figures            # combines with --prune / --ocr-fallback / --force
RAG_FIGURE_MODEL=llava:13b python3 sync_folder.py --describe-figures   # larger LLaVA variant
```

This also indexes **standalone image files** in the folder — `.png`, `.jpg/.jpeg`, `.tif/.tiff`, `.webp`, `.bmp`, `.gif`. With `--describe-figures`, each image is described by the vision model and its description uploaded as a text document, so loose figures/screenshots/plots become retrievable too (they're skipped without the flag, since a raw image has no extractable text). Because a standalone image usually has no caption, the script also **passes the file name to the vision model as context** — so a descriptive name like `voltivity_resistivity_vs_temp.png` genuinely improves the description. If a **sidecar text file** with the same basename sits next to the image (`figure1.png` + `figure1.txt`, or `figure1.png.txt`; also `.md`/`.caption`/`.json`), its contents are fed in as extra context too — handy when you export figures with metadata. Tracking, `--force`, and `--prune` all apply to images the same as documents.

> **Vision model note (DGX Spark).** The default is **`llava`**, not Llama 3.2 Vision. The Spark's custom Blackwell-optimized Ollama build does **not** support the `mllama` architecture that Llama 3.2 Vision uses — it fails to load with `unknown model architecture: 'mllama'` (a 500 from Ollama). LLaVA uses a supported architecture and works. If you need a specific vision model that this build won't load, run a stock Ollama in a container just for it (`docker run -d --name ollama-vision --gpus all -p 11435:11434 ollama/ollama`) and point the script at it with `RAG_OLLAMA_URL=http://localhost:11435`.

The companion doc is tracked with its source file: re-syncing a changed PDF regenerates it, and `--prune` removes it when the source is deleted.

> **Important — numbers are approximate.** Vision models reliably capture *what* a figure shows and its trends, but they **hallucinate exact chart values**. Treat extracted numbers as approximate and verify against the source figure. For precise datapoints, use a plot-digitizer tool. This is a retrieval/understanding aid, not a data-extraction guarantee. It's also slower than a text-only sync (one model call per page), so it's opt-in.

For a heavier-duty "search papers *by* their visual content" system (rendering each page as an image and retrieving visually with ColPali/ColQwen + a vector DB like Qdrant), that's a separate, larger build outside this text-RAG pipeline — worth it only if figure retrieval becomes central to your workflow.

Run it on a schedule with cron — every 15 minutes (add `--prune` to keep the collection mirrored to the folder). With your defaults in the script and the key in `~/.rag_sync_key`, the cron line stays clean:

```bash
# crontab -e
*/15 * * * * /usr/bin/python3 /path/to/sync_folder.py --prune >> $HOME/rag_sync.log 2>&1
```

For near-instant updates instead of polling, swap the loop for Python `watchdog` running as a systemd service. AnythingLLM also has built-in **Scheduled Jobs** and a beta **Live Document Sync** you can use instead.

> API endpoint names shift between tool versions. If a call fails, check the tool's live API docs (Open WebUI: `http://localhost:3000/docs`) and adjust the endpoints in `sync_folder.py`.

---

## Better PDF extraction (optional: Tika)

The **Default** extraction engine runs in-process and needs no extra service — good enough for most PDFs. For a library of varied scientific papers, **Apache Tika** extracts text far more reliably, but it's a separate service you must run. Do **not** just select "Tika" in the settings without starting it — extraction will fail with "content empty" (the container can't resolve the `tika` hostname).

To run Tika and wire it in:

```bash
docker network create rag-net 2>/dev/null                       # shared network for name resolution
docker run -d --name tika --restart unless-stopped \
  --network rag-net apache/tika:latest
sudo docker network connect rag-net open-webui                  # put Open WebUI on the same network
```

Then in **Admin → Settings → Documents**: Content Extraction Engine → **Tika**, server URL `http://tika:9998`, Save. Re-add any documents that previously failed. If you ever remove Tika, switch the engine back to **Default** first, or ingestion will break.

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

The uninstaller also removes the optional add-ons if you set them up: the Tika container, the containerized vision Ollama (`ollama-vision`), the shared `rag-net` network, and (with `--purge-data`) its volume and the sync artifacts (`~/.rag_sync_state.json`, `~/.rag_sync_key`).

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

**The chat echoes my prompt back verbatim instead of answering.**
The chat is pointed at the **embedding model** (`nomic-embed-text`), which can't generate text. Switch the model at the top of the chat to `gemma4:26b`. Prevent it recurring by setting Gemma as the default model and hiding `nomic-embed-text` from the chat list (see the Open WebUI config steps above).

**Typing `#` shows no popup, or `#Papers` isn't grounding answers.**
The `#` menu lists **Knowledge collections that contain documents**. If it's empty, check **Workspace → Knowledge**: if there are 0 collections (or the collection has no files), that's the cause. Open WebUI does not auto-create a collection — create one under **Workspace → Knowledge → + New Knowledge**, then load documents into it (drag-and-drop if the files are on the machine running the browser, or run `sync_folder.py` with `RAG_TARGET` set to the new collection's id if the files live on the Spark in `~/papers`). Once the collection has documents, `#` will list it. Also make sure you *click* the collection in the popup rather than just typing the text.

**A document fails to add with `400: The content provided is empty` (or a chat says "No sources found").**
Open WebUI extracted no text from that file. The sync script now prints a hint when this happens; the usual causes, in order of likelihood:

1. **Extraction engine points at a service that isn't running.** If **Admin → Settings → Documents → Content Extraction Engine** is set to **Tika** or **Docling** but you never started that container, extraction fails (the server log shows `Failed to resolve 'tika'`) and every file comes back empty. Fix: set the engine back to **Default** (in-process, no service needed), or actually run the service — see "Better PDF extraction with Tika" below.
2. **The PDF has no text layer** (scanned/image-only). Test with `pdftotext file.pdf - | head`; if empty, OCR it first: `ocrmypdf --force-ocr in.pdf out.pdf`, then sync the OCR'd copy.
3. **The built-in Default parser chokes on a specific (text-bearing) PDF.** Rare, but happens with unusual font/encoding. Either OCR it as above, or use Tika (below), which is far more tolerant.

Filenames also matter: spaces or quotes in a name can break the upload — prefer `Underscore_Names.pdf`.

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
