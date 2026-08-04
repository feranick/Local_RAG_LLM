# Local RAG on a Linux workstation

**Version 2026.08.03.3**

Automated setup for running **retrieval-augmented generation (RAG) entirely on your own machine** — point a local model (served by Ollama) at a folder of papers/data and chat with it, with source citations, fully offline.

The scripts detect the hardware they're running on and adapt: model menus only offer models the machine can actually hold. Developed and used daily on an **NVIDIA DGX Spark** (GB10, 128 GB unified memory), and equally at home on a workstation with a single GPU — or none.

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
| `platform_probe.py` | Detects CPU, RAM, GPU/VRAM and Docker GPU support, and derives how much model this machine can hold. `--write` saves `hardware.conf`, which every other script reads — so all of them agree, and you can override any of it by hand. |
| `setup_local_rag.sh` | Installs Ollama, configures it for container access (creates a systemd service if none exists), interactively selects + pulls the chat/embedding/vision models, and launches Open WebUI + AnythingLLM. Idempotent — safe to re-run. |
| `llm_stack_healthcheck.sh` | Verifies every component: Ollama API, GPU, an embedding model, a live generation test (auto-detects whichever chat model is installed), both containers, container→Ollama connectivity, reboot-safe restart policies, and any optional add-ons (Tika, vision model). Model-agnostic — adapts to whatever you selected at setup. |
| `update_local_rag.sh` | Updates the stack when newer images exist — refreshes the containers (Open WebUI, AnythingLLM, and Tika/vision if present) while preserving data. `--check` reports without changing. |
| `uninstall_local_rag.sh` | Removes the stack. Safe by default (keeps data); `--purge-data` wipes everything including models. |
| `migrate_rag.py` | Move the whole stack to another machine: archives the docker volumes (collections **and** vectors), documents and sync files, then restores them and rewrites the absolute paths so nothing gets re-indexed or duplicated. See `MIGRATION_RUNBOOK.md`. |
| `pin_notes.py` | Mirrors a folder of short notes into a model preset's **system prompt**, so local practice applies in every chat without depending on retrieval. Replaces only its own marked block, so hand-written prompt text survives; `--dry-run` shows a diff, `--clear` removes it. |
| `manage_models.py` | Browse, add, test, list, remove or set the default **LLM** on a running stack. `--browse`/`--tags` read the available models live from the Ollama library; every add is followed by a real load test, since a model can download and still fail to run on this Ollama build. |
| `new_rag_instance.py` | Creates a **second, fully pre-configured Open WebUI instance** for an independent library with its own embedding model — container, admin account, API key, Knowledge collection and a ready sync config. Step-by-step: `NEW_INSTANCE_RUNBOOK.md`. |
| `sync/sync_folder.py` | Keeps a local folder in sync with your Open WebUI collection / AnythingLLM workspace — add/update, mirror deletions, re-sync, OCR text-less PDFs, and vision descriptions of figures & standalone images. **Documented separately in [`sync/README.md`](sync/README.md).** |

---

## Prerequisites

- Linux, x86_64 or ARM64 (developed on Ubuntu 24.04 / DGX OS)
- Docker; plus the **NVIDIA Container Toolkit** if you have an NVIDIA GPU and want containers to use it
- An NVIDIA GPU is recommended, not required — with none, inference runs on CPU (functional, slow)
- Internet access for the first run (to pull the installer, images, and models)

Both Docker images are multi-arch, so x86_64 and ARM64 both work.

**What can this machine run?** `platform_probe.py` answers that once and writes it where every script reads it:

```bash
python3 common/platform_probe.py            # summary: CPU, RAM, GPU, usable model memory
python3 common/platform_probe.py --write    # save it as hardware.conf
```

The budget it derives — VRAM for a discrete GPU, ~85% of RAM for unified memory, ~60% for CPU-only — is what `setup_local_rag.sh` and `manage_models.py` use to decide which models to offer. Hand-edits to `hardware.conf` win over detection, and `RAG_USABLE_MEM_GB` overrides both.

### Optional: install as a package

Running the scripts straight from the checkout is fully supported and needs nothing
but `requests`. If you'd rather have them on `PATH`:

```bash
python3 -m pip install --upgrade build
python3 -m build --wheel
python3 -m pip install dist/*.whl
```

**Every command keeps its script's name** — no prefixes, nothing to translate:

| run from the checkout | installed command |
|---|---|
| `python3 common/platform_probe.py` | `platform_probe` |
| `python3 sync/sync_folder.py` | `sync_folder` |
| `python3 sync/library_stats.py` | `library_stats` |
| `python3 sync/diag_duplicate.py` | `diag_duplicate` |
| `python3 management/manage_models.py` | `manage_models` |
| `python3 management/new_rag_instance.py` | `new_rag_instance` |
| `python3 migration/migrate_rag.py` | `migrate_rag` |
| `python3 management/pin_notes.py` | `pin_notes` |

The four shell scripts install under their own names too (`setup_local_rag.sh`,
`update_local_rag.sh`, `uninstall_local_rag.sh`, `llm_stack_healthcheck.sh`), so
every command in this documentation works either way — drop the `python3 <dir>/`
prefix and the `.py`. `pyproject.toml` also carries a commented block of
`rag-`-prefixed aliases if you'd rather namespace them in a shared `/usr/local/bin`.

For a system-wide install without touching the distro's Python:

```bash
sudo python3 -m venv /opt/local-rag-llm
sudo /opt/local-rag-llm/bin/pip install dist/*.whl
sudo ln -sf /opt/local-rag-llm/bin/* /usr/local/bin/
```

Don't run the Python tools under `sudo`: they resolve `~/.rag_sync_key` and the state
files from `$HOME`, which becomes `/root`. The shell installers *do* expect sudo and
already target your real home.

---

## Quick start

```bash
chmod +x setup_local_rag.sh llm_stack_healthcheck.sh uninstall_local_rag.sh update_local_rag.sh

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
5. Launch **Open WebUI** on port 3000 (with `--gpus all` when an NVIDIA GPU *and* a Docker GPU runtime are both present — otherwise it says so and starts on CPU).
6. Create AnythingLLM's storage folder with the correct ownership and launch **AnythingLLM** on port 3001.

After pulling, the script **verifies each model actually loads** (a tiny generate/embeddings call), not just that it downloaded — so a model that's pulled but won't run on your Ollama build (like `llama3.2-vision`'s `mllama` architecture) is flagged immediately rather than failing later.

It's safe to re-run: containers are recreated cleanly, while pulled models, Ollama config, and data volumes are preserved. When run with `sudo`, it correctly targets your real home directory (not root's).

### Choosing models

When run in a terminal, the script **interactively prompts you to pick the models** — press Enter for the default or type a number / custom tag:

**Only models that fit the detected hardware are listed**, so the menu on a 16 GB GPU is shorter than on a 128 GB Spark. `c` still accepts any tag you like.

- **Chat model (LLM):** `gemma4:26b` (preferred default when it fits — MoE, fast), then `gemma4:12b`, then `gemma4:e2b` on small machines. `gemma4:31b`, `qwen3.6:27b`, `qwen3.6:35b` and `llama3.3:70b` appear when there's room. The default is the first *preferred* model that fits, not the largest — a 70B model fits 128 GB but is slower than a 26B MoE for the same work.
- **Embedding model:** `nomic-embed-text` (default), `mxbai-embed-large`, `bge-m3`, or custom.
- **Vision model (optional, for figure/image descriptions):** `llava` (default when it fits), `moondream` (smaller), `bakllava`, `none`, or custom.

(Note: `qwen3.6` is an Alibaba model — listed for completeness; it and other Chinese models are your call. Avoid `llama3.2-vision`: its `mllama` architecture fails to load on several Ollama builds, including the Spark's — `llava` works everywhere.)

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

## Changing or adding an LLM later

**Swapping the chat model needs no re-indexing.** The chat LLM is used only when
answering a question; only the *embedding* model is part of the stored vectors. And
because every Open WebUI instance talks to the same Ollama, a newly pulled model
shows up in all of them at once.

### Finding out what to install

The hard part isn't the swap, it's knowing the exact name. That list is read **live
from the Ollama library** each time, not baked into the script (a hardcoded list
would be wrong within months):

```bash
python3 manage_models.py --browse               # what's available right now
python3 manage_models.py --browse gemma         # ...matching a term
python3 manage_models.py --tags gemma4          # the exact installable tags + sizes
```

`--browse` shows each model's parameter sizes, capabilities (vision / thinking /
embedding), tag count and popularity, marks the ones you already have, and hides
**cloud-only** models — those run on Ollama's servers, not on your Spark.

`--tags NAME` is the one to run before `--add`. It prints every tag with its
download size and context window, flags anything larger than this machine
can hold (from the probe), and hides the `*-mlx*` tags (Apple-silicon builds) and `*cloud*`
tags, which cannot run here at all. Add `--all` to either command to see everything.

```
  tag                                size     ctx  notes
    gemma4:latest                  9.6 GB    128K  vision
  ✔ gemma4:26b                      18 GB    256K  vision
    gemma4:31b-it-q8_0              34 GB    256K  vision
    gemma4:26b-a4b-it-bf16          52 GB    256K  vision
```

Both commands need internet access. Offline, `--suggest` prints a small built-in
starting list — useful, but explicitly marked as the thing that ages.

### Installing and switching

```bash
python3 manage_models.py --add gemma4:31b       # pull + verify it actually loads
python3 manage_models.py --list                 # installed models, sizes, roles
python3 manage_models.py --loaded               # what's in memory right now
python3 manage_models.py --remove gemma4:12b    # reclaim disk space
```

Then pick it in the UI's model dropdown, or make it the default with
set it as the **Selected Model** (what "default model" is now called):

- **instance-wide:** **Settings → Admin → AI → Models** → the model's ellipsis **⋯** → **Set as Selected Model** (env: `DEFAULT_MODELS`)
- **just for you:** the model selector inside a chat → **Set as default**, which wins over the instance setting

`--set-default TAG` tries the API and, when the build doesn't expose it, prints the
alternatives instead of pretending. A new chat resolves in this order: URL parameter
→ models bound to the chat's folder → your own default → the instance's Selected
Models → **first available model**.

> **Setting it for a workspace preset (v0.11.0).** Presets aren't listed under
> **Admin → AI → Models** — that page lists *base* models — so there's no ⋯ menu to
> use, and `/api/v1/configs/…` isn't exposed. Two things that do work:
>
> - **Curate the picker.** Because resolution ends at *first available model*, hiding
>   the base models (the **eye** icon, one row per model) makes everyone land on your
>   preset. Leave them **enabled** — the toggle on the right — since hiding is not
>   disabling and a preset always needs access to its base model. Set the preset to
>   **Public** too, or other users can't select it. Bonus: nobody can accidentally
>   chat with `nomic-embed-text`.
> - **Per user:** open a chat, pick the model, then **Set as default** in the same
>   dropdown. A user's own default outranks the instance setting.
>
> `DEFAULT_MODELS` as a container env var looks tempting but is a **PersistentConfig**
> value: once the database holds a value the env var is ignored, and the documented
> override (`ENABLE_PERSISTENT_CONFIG=False`) makes *every* UI-configured setting —
> Top K, hybrid search, embedding model — revert on each restart. Not a good trade.

The verification step matters on this hardware: `--add` always follows the pull with
a real generate/embeddings call, because a model can download perfectly and still
fail to run — `llama3.2-vision` needs the `mllama` architecture, which the Spark's
Ollama build doesn't have. The script refuses to suggest that one and explains why.

**Changing the *embedding* model is a different matter** — it invalidates every
stored vector and forces a full re-sync, or better, a second instance:

```bash
python3 manage_models.py --embedding-warning    # what's involved, both options
```

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
   - **Raise Top K — this one matters more than it looks.** The default (`3`) means
     only three chunks are retrieved per question, which behaves like a much smaller
     library: a short, highly specific document has to out-rank two hits from your
     longest manuals, and usually loses unless the question happens to echo its
     wording. Set **Top K to 10–15**. This is a query-time setting, so **no
     re-indexing is needed** — unlike chunk size, it applies to the next question.
     The cost is a larger prompt per query (15 × ~1500 chars ≈ 6k tokens, negligible
     against a 256K context but not against memory bandwidth), so try 10 first.
     **Hybrid Search** (BM25 keyword matching + reranking) is *in principle* the
     other half of this — it's what lets a brief note with distinctive terms compete
     against long documents — but only enable it deliberately, per the next point.
   - **Hybrid Search can make retrieval *worse* if its reranker isn't working.**
     Observed here: with hybrid on, a short note stopped being retrieved by models
     that found it reliably with hybrid off — the reranker (and any **Relevance
     Threshold** above `0`) filters results *after* retrieval, so a misconfigured
     reranker silently removes good chunks. If enabling it hurts, either set
     **Reranking Model** explicitly and let it download on first use with
     **Relevance Threshold `0`**, or simply leave hybrid search off. Change Top K and
     hybrid search **one at a time**, or you can't tell which one did what.
3. **Workspace → Knowledge → +** to create a collection and upload files (see "Chatting with your papers" below for how to use it).
   - If you'll index large documents (books, theses, 100+ pages), also set
     **Embedding Batch Size** to `32` and **Embedding Concurrent Requests** to `4`
     under Admin Settings → Documents. The defaults (batch 1, unlimited
     concurrency) open a connection to Ollama per chunk and fail on big files with
     `Too many open files`. The setup script also launches the container with
     `--ulimit nofile=65536:65536` for the same reason.
4. Set Gemma as the default and hide the embedding model from the chat list:
   - **Default chat model:** set your chat model as the **Selected Model** — **Settings → Admin → AI → Models** → its ellipsis **⋯** → **Set as Selected Model** — so new chats start there rather than on the embedding model. (Per-user equivalent: **Set as default** in the model selector inside a chat.) This is *not* the "Task Model (Local/External)" under Admin Panel → Settings → Interface — that one runs background chores (titles, tags, follow-up suggestions, autocomplete). Leaving it on "Current Model" makes your big chat model write three-word titles; see *Follow-up suggestions, titles and tags* below for why pinning a tiny model there is worth it.
   - **Hide the embedding model:** **Admin Panel → Settings → Models** → toggle `nomic-embed-text` off. It stays available for embedding documents; this just removes it from the chat dropdown so it can't be picked (or auto-selected) as a chat model.

> **Why a separate embedding model?** The chat model (Gemma) writes answers; the embedding model turns your documents into vectors for retrieval. Without one, uploads silently fail. This is why the setup pulls `nomic-embed-text` alongside the chat model. It is not a chat model — never select it to chat with (see troubleshooting if a chat echoes your prompt back).

### Chatting with your papers (Open WebUI)

The collection must already exist **and contain documents** (check **Workspace → Knowledge**) before it can be used. Then, in a **New Chat**:

1. Select `gemma4:26b` in the model dropdown at the top.
2. Type `#` in the message box and **click your collection** from the popup that appears (don't just type the text `#Papers` — that's literal text and attaches nothing). You'll see the collection appear as a chip above the input.
3. Ask your question. Answers come back grounded in the papers, with sources.

To make retrieval always-on without typing `#` each time, go to **Workspace → Models → + New Model**, set the base model to `gemma4:26b`, attach your Knowledge collection, optionally add a system prompt ("Answer using the attached papers and cite sources."), and save. That preset then appears in the model dropdown.

> **Attaching a collection is not enough on its own.** Since v0.10 native function
> calling is the default, and an attached knowledge base is **not auto-injected** —
> the model is expected to fetch it with tool calls. Set **Advanced Params →
> Function Calling → `Legacy`** on the preset to restore automatic injection.
> (`Default` in that column means *unset → inherit the global*, which is Native.)
> See below.

### Two ways answers get grounded — and why one is model-dependent

Open WebUI can reach your documents by two quite different routes, and knowing which one is in play explains most "the answer is in there but the model says it isn't" moments:

| | How it works | Depends on the chat model? |
|---|---|---|
| **Direct injection** — `#` + collection, or a preset with **Function Calling = `Legacy`** | the server retrieves the top-k chunks and puts them in the prompt | **No.** Every model sees the same text |
| **Agentic knowledge tools** — Native mode, the default since v0.10 — the model calls `search_knowledge_files`, `query_knowledge_files`, `grep_knowledge_files`, … itself | the model decides what to search for, reads the results, and decides whether to search again | **Yes, heavily** |

Agentic retrieval is more flexible, but it only works as well as the model's tool use. A real example from this stack — same collection, same embeddings, same question ("recommended sample height below the holder surface for EDS"):

- **qwen3.6:35b** → `search_knowledge_files` → `query_knowledge_files` → 3 sources → quoted the manual correctly (4–7 mm, 5 mm as the trade-off).
- **gemma4:26b** → queried the knowledge base *inventory*, got one irrelevant chunk from an unrelated CSV, and concluded the information wasn't available — while listing, by name, the very manual that contains it.

The passage was plainly present in the indexed text, and adding `#` + the collection made gemma4 answer correctly straight away. So a failure like this is **not** evidence of a bad index, a bad chunk size or a bad embedding model — all of that is shared between the two runs.

### Making a smaller model reliable: switch it to Legacy tool calling

Attaching the collection to a preset does **not** by itself take the model's tool use
out of the loop — that was the whole point of the move to native function calling.
To get server-side injection back, set **Function Calling** to **`Legacy`** at
whichever scope suits you:

| Scope | Where |
|-------|-------|
| One model | **Workspace → Models → <preset> → Advanced Params → Show → Function Calling → `Legacy`** |
| One chat | **Chat Controls → Advanced Params → Function Calling → `Legacy`** |
| Every model | **Settings → Admin → AI → Models → Model Defaults** (top of the list) → `Legacy` |
| Env var | `DEFAULT_MODEL_PARAMS: '{"function_calling": "legacy"}'` (no standalone variable exists) |

Two naming traps here, both easy to trip over:

- The mode **formerly called "Default" is now "Legacy"** (renamed in v0.10, when Native
  became the default).
- In the Advanced Params table, a greyed **`Default`** means *this parameter is unset —
  inherit the global*, which resolves to **Native**. Seeing `Default` next to Function
  Calling does **not** mean legacy behaviour is active. You must explicitly pick
  `Legacy`.

Verified here: `gemma4:26b` answers correctly with `#`, and fails with a preset that
has the collection attached but Function Calling left unset.

> Upstream considers Legacy unsupported and kept only for backward compatibility —
> their recommended fix is a stronger tool-calling model rather than falling back.
> For this stack that means: use Legacy to make a small model dependable today, and
> prefer a stronger model (e.g. `qwen3.6:35b`) where you want agentic retrieval.

Two alternatives, both narrower:

- **Full Context** — click the attached collection chip to toggle it. Injects whole
  documents with no chunking or search. Excellent for a few short reference docs,
  unusable for a library of thousands.
- **System prompt** instructing the model to call `query_knowledge_files` first. Helps
  a bit, but you're still relying on the tool use that failed.

### A configuration that works for a shared assistant

Reached after a lot of trial and error on v0.11.0, and worth copying wholesale if
you want a lab assistant that answers reliably without anyone typing `#`:

| Setting | Value | Why |
|---|---|---|
| Knowledge attached to the preset | the big library **and** a small notes collection | so neither has to be added per chat |
| **Function Calling** | **`Legacy`** | attached collections are then retrieved server-side and injected. Under `Native` they're exposed as *tools* and only reach the model if it chooses to search them — which is why an attached collection can look completely ignored |
| Top K | `10–15` | the default `3` is too tight once a library is large |
| Hybrid Search | **off** (unless the reranker is verified) | enabling it with a missing/misconfigured reranker made retrieval *worse* here |
| System prompt | a precedence line naming the notes collection | e.g. "LabNotes describes local practice and takes precedence over vendor documentation" |
| Prompt suggestions | a few real questions | replaces the generic "Tell me a fun fact" cards |

`Legacy` is deprecated upstream, and their advice is a stronger tool-calling model
instead. That's fair for chat quality, but it doesn't address this case: the issue
isn't the model's intelligence, it's that **`Native` never injects attached
knowledge at all**. Until that changes, `Legacy` is the setting that makes an
attached collection behave the way the UI implies it should.

Two navigation traps in the Workspace UI, both of which cost time here:

- **Clicking a preset's title opens a chat with it**, not the editor. The editor is
  the row's **pencil** (hidden on narrow windows) or, reliably, the direct URL:
  `http://<host>:<port>/workspace/models/edit?id=<preset-id>`
- The **Knowledge** attachment row lives *inside that editor*, below Advanced Params
  and Prompt Suggestions — not on the Workspace → **Knowledge** tab, which is only
  the library of collections.

### Which models cope with agentic retrieval

Measured on this stack, same collection, same question ("recommended sample height
below the holder surface for EDS", answer present in the Phenom XL manual):

| Model | Native / agentic | Notes |
|-------|------------------|-------|
| `qwen3.6:35b` | ✅ correct | chained `search_knowledge_files` → `query_knowledge_files`, cited 3 sources |
| `gemma4:31b` | ✅ correct | dense; no workaround needed |
| `gemma4:26b` | ❌ gave up | MoE, ~4B active parameters/token; searched the file inventory, took one irrelevant hit, declared the information missing |

The pattern is **active parameters per token, not total size**: the 26B MoE is the
odd one out, while the smaller-on-paper dense 31B is fine. If a model fails this way,
either move up to one that doesn't, or set `Function Calling = Legacy` for it.

Practical guidance:

- Use **`Function Calling = Legacy`** for anything you rely on with a model that
  fails the test above — and prefer switching model where you can, since Legacy is
  unsupported upstream.
- Test a new chat model with one question you know the answer to *before* trusting it
  for real work. The failure mode is a confident "not in the documents", not an error.

### Local notes that must always apply: a second collection in Full Context

Retrieval is a ranking contest, and a short "how we actually do it here" note usually
loses it against long vendor manuals — it only wins when your question happens to
echo its wording. If a document's whole purpose is to state local practice, take it
out of the contest:

1. **Workspace → Knowledge → + Create** a second collection, e.g. `Lab Notes`. Give it
   a description that says what it's for — models searching across bases see it.
2. Put the notes in it: drag them into the UI, or sync a small folder with its own
   config. Same instance means the same `KEY_FILE`, but the **`STATE_FILE` must be
   distinct** from your main library's:

   ```ini
   # lab_notes.conf
   BASE_URL   = http://localhost:3002
   TARGET     = <the new collection id>
   WATCH_DIR  = ~/lab_notes
   KEY_FILE   = ~/.rag_sync_key_open-webui-breakerspace
   STATE_FILE = ~/.rag_sync_state_lab_notes.json
   ```

3. **Workspace → Models → <your preset> → Knowledge** → attach `Lab Notes` *alongside*
   the main collection.
4. **Click the attached `Lab Notes` chip** to switch it from **Focused Retrieval** to
   **Full Context**. Leave the big library on Focused Retrieval.
5. Add a precedence line to the preset's system prompt:
   *"Lab Notes describes local practice at this facility and takes precedence over
   vendor documentation where they conflict."*

Full Context injects the content verbatim in **every** message, regardless of tool
calling or Top K — so it can't be out-ranked, and it works identically on every chat
model. The trade-off is that it always occupies context, so keep this collection to a
handful of short notes; anything longer belongs in the retrieved library.

### Follow-up suggestions, titles and tags: the task model

The clickable follow-up questions under each answer are **not** produced by the chat
model as part of its reply. Open WebUI generates them with a separate **task model**,
which also writes chat titles, tags and retrieval queries. Auto-generation is on by
default (**Settings → Interface → Chat → Follow-Up Auto-Generation**).

If follow-ups appear with one chat model and not another, that's the task model
defaulting to *whatever you're chatting with*: the model is asked for a structured
payload as a side job, and if its output doesn't parse you simply get no chips — no
error. Observed here: `qwen3.6:35b` produces them, `gemma4:31b` does not.

Pin a small model for the job instead:

**Admin Panel → Settings → Interface → Task Model (Local)** → e.g. `gemma3:1b`,
`llama3.2:3b` (there's a separate *Task Model (External)* field for cloud models;
locally hosted chat models use the Local one).

Upstream recommends a genuinely tiny, *non-reasoning* model here — titles, tags and
follow-ups are trivial jobs. Two benefits beyond consistent follow-ups: background
work stops occupying your big model, and on the Spark that avoids evicting ~20 GB of
weights to write a three-word chat title.

If the menu labels differ in your build, the env vars are stable:
`TASK_MODEL` (local), `TASK_MODEL_EXTERNAL`.

Each chore can also be switched off individually on the same page:

| Chore | Toggle | Env var |
|---|---|---|
| Follow-up suggestions | Follow-up Generation | `ENABLE_FOLLOW_UP_GENERATION=False` |
| Chat titles | Title Generation | `ENABLE_TITLE_GENERATION=False` |
| Tags | Tags Generation | `ENABLE_TAGS_GENERATION=False` |
| Prompt autocomplete | Autocomplete Generation | `ENABLE_AUTOCOMPLETE_GENERATION=False` |

Autocomplete is the one to disable first if the UI feels sluggish during a sync — it
fires on every keystroke, and every keystroke then queues behind whatever the GPU is
already doing.

Worth turning on while exploring a library: **Keep Follow-Up Prompts in Chat**
(Settings → Interface), which preserves the suggestions on older messages instead of
only the latest one.
- A heterogeneous collection makes agentic retrieval harder for *every* model: if a spreadsheet of tensile data is the top hit for a microscopy question, split the library (see *Running a second library* in `sync/README.md`).
- Before blaming retrieval, re-ask the same question with `#` + the collection. If that works, the index is fine and the difference was tool use.
- The agentic tool set is worth knowing even so: `query_knowledge_files` is semantic, `grep_knowledge_files` does exact string/regex matching, and `view_file` reads a line range. Capable models chain them; a system prompt naming which to prefer per collection helps.

---

## Keeping the knowledge base updated (folder sync)

`sync_folder.py` keeps a local folder in sync with your Open WebUI collection / AnythingLLM workspace — adding new/changed files, mirroring deletions, OCRing text-less PDFs, and describing figures and standalone images with a vision model. It lives in its own `sync/` folder with a dedicated guide:

**→ See [`sync/README.md`](sync/README.md)** for full setup, configuration, all flags, reset/wipe procedures, and sync-specific troubleshooting.

Quick start (after creating the collection in the UI and setting `TARGET` + the API key file):

```bash
pip install requests
echo 'sk-your-key' > ~/.rag_sync_key && chmod 600 ~/.rag_sync_key
python3 sync/sync_folder.py --describe-figures
```

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

## Updating

When Open WebUI, AnythingLLM, or the add-ons ship new versions, refresh them without losing data:

```bash
./update_local_rag.sh --check   # report which containers have newer images (no changes)
./update_local_rag.sh           # pull newer images and recreate those containers
./update_local_rag.sh --pull-models   # also re-pull installed Ollama models to latest tags
./update_local_rag.sh --force-recreate  # recreate even if the image is unchanged
```

`--force-recreate` is what you need after changing a container's **run flags**
rather than its image — e.g. adding `--ulimit nofile=65536:65536` or an env var.
Without it, a container whose image is already current is reported "up to date"
and left running with its old flags.

It only recreates a container when its image actually changed, preserves the data volumes / AnythingLLM storage, and reattaches containers to the `rag-net` network (Tika) if they were on it. Verify afterward with `./llm_stack_healthcheck.sh`.

> **After an Open WebUI image update, clear the browser cache once.** The updated container serves new JS chunk names, so a cached app shell from the old version 404s and the UI gets stuck on the "OI" splash in a reload loop. Fix: DevTools → Application → Storage → **Clear site data** (a plain hard-refresh often won't clear the service worker), then reload. Nothing is wrong server-side — see the sync README's troubleshooting for how to tell this apart from Ollama being busy.

**Ollama is left alone by default.** On the DGX Spark it's a custom Blackwell-optimized build; the generic `ollama.com` installer would replace it with the stock ARM build and lose the GB10/FP4 optimizations — so update Ollama through DGX OS / NVIDIA channels instead. `--include-ollama` will run the generic installer anyway, but only after a warning and confirmation (not recommended on the Spark).

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

The uninstaller also removes the optional add-ons if you set them up: the Tika container, the containerized vision Ollama (`ollama-vision`), the shared `rag-net` network, and (with `--purge-data`) its volume and the sync artifacts — all of them, including per-library variants (`~/.rag_sync_state*.json`, `~/.rag_sync_key*`).

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

These are the exact issues encountered while bringing this stack up, and their fixes (all now handled by the scripts). **Sync-specific issues** (duplicate content, empty-extraction, API keys, `#` grounding) are covered in [`sync/README.md`](sync/README.md).

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

**The Embedding Model field in Open WebUI won't populate / stays empty even after refresh.**
It isn't a dropdown — it's a free-text input. Type the model name in by hand (`nomic-embed-text`, or `nomic-embed-text:latest`) exactly as `ollama list` shows it, then click **Save**. This is expected behavior, not a connection problem — you can confirm the connection is fine with:
```bash
sudo docker exec open-webui curl -s http://host.docker.internal:11434/api/tags | grep -o '"name":"[^"]*"'
```

**The chat echoes my prompt back verbatim instead of answering.**
The chat is pointed at the **embedding model** (`nomic-embed-text`), which can't generate text. Switch the model at the top of the chat to `gemma4:26b`. Prevent it recurring by setting your chat model as the **Selected Model** and hiding `nomic-embed-text` from the chat list (see the Open WebUI config steps above).

**Ollama runs at half speed / high CPU, or a generation is very slow.**
Ollama silently splits a model across CPU and GPU when it thinks GPU memory is short. Check with `ollama ps` — if it shows a CPU/GPU split, choose a smaller model or a heavier quant. Keep models comfortably under the detected ceiling (`python3 common/platform_probe.py`) to leave room for the KV cache. A slow first response is often just the model loading from disk.

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

Rough sizing per byte-per-parameter: FP16 needs ~2 GB per billion parameters, INT8 ~1 GB, 4-bit ~0.5 GB. So a 24 GB GPU holds a ~12B model at FP16 or a ~45B at 4-bit; the Spark's ~110 GB usable holds ~55B at FP16 or ~200B at 4-bit. Memory bandwidth is usually the real limit (~273 GB/s on the Spark), so a model that merely *fits* can still be slow — 4-bit quants are generally the sweet spot.

This stack defaults to `gemma4:26b` (a mixture-of-experts model, ~4B active params per token — fast on the Spark's bandwidth). Other good picks: `gemma4:31b`, `llama3.3:70b`, `qwen3.6:27b`, `qwen3.6:35b`. Whatever tag `ollama list` shows is what you select as the chat model — change it with `--chat-model` or in the UI at any time.
