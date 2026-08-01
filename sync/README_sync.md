# Folder Sync for Local RAG — `sync_folder.py`

**Version 2026.08.01.9**

Keeps a local folder in sync with a RAG knowledge base — an AnythingLLM workspace or an Open WebUI collection. It hashes each file, uploads only new/changed ones, and (optionally) mirrors deletions, OCRs text-less PDFs, and describes figures/images with a vision model.

This is the companion to the main stack (setup, health check, update, uninstall). For installing Ollama + Open WebUI + AnythingLLM, the UI configuration, Tika extraction, updating, and general troubleshooting, see the **[main README](../README.md)**.

---

## What it does

- **Adds / updates:** new files are uploaded and embedded; changed files are re-uploaded cleanly (the old copy is removed first, so no duplicates).
- **Mirror (`--prune`):** files removed from the folder are also removed from the collection — the folder becomes the single source of truth.
- **Re-sync (`--force`):** re-upload/re-embed everything even if unchanged (e.g. after changing the embedding model or chunk settings).
- **OCR fallback (`--ocr-fallback`):** if the server extracts no text from a PDF, OCR it locally and retry.
- **Describe figures (`--describe-figures`):** render PDF pages and standalone images and index a vision-model description of each, so plots/figures become retrievable.

> **Don't mix methods on one collection.** The script tracks what *it* uploaded; documents you add by hand in the GUI are invisible to it. If you both drag files into the GUI and sync the same folder, you'll get duplicates, and `--prune` won't touch the GUI-added ones. Pick one method per collection.

---

## Prerequisites

```bash
pip install requests          # required
pip install pymupdf           # only for --describe-figures (renders PDFs/images)
```

### Which files are picked up

The watched folder is scanned **recursively**, so subfolders at any depth are
included. Two groups of file types:

| Group | Extensions | Notes |
|-------|-----------|-------|
| Documents (always) | `.pdf` `.txt` `.md` `.rst` `.html` `.htm` `.rtf` `.epub` | uploaded as-is; the server extracts the text |
| Microsoft Office | `.docx` `.doc` `.pptx` `.ppt` `.xlsx` `.xls` | Word, PowerPoint and Excel are all indexed |
| OpenDocument | `.odt` `.odp` `.ods` | |
| Tabular / data | `.csv` `.tsv` `.json` | |
| Images (only with `--describe-figures`) | `.png` `.jpg` `.jpeg` `.tif` `.tiff` `.webp` `.bmp` `.gif` | indexed as a vision-model description |

Anything else (archives, videos, `.bib`, …) is skipped. `library_stats.py` reports
how many were skipped with a breakdown by extension, and can name or export them:

```bash
python3 library_stats.py ~/papers --list-ignored              # print them
python3 library_stats.py ~/papers --save-ignored skipped.txt  # write the list to a file
```

Worth a look after the first sync of a new folder — it's how you notice that, say,
40 `.bib` files or a folder of `.h5` data was never indexed.

Two caveats on Office and other non-PDF formats: whether the text is actually
extracted is up to **Open WebUI's extraction engine**, not this script — the
Default engine handles the common cases, and **Tika** (see the main README) is more
tolerant, especially for legacy `.doc`/`.xls` and complex spreadsheets. If a file
extracts to nothing you'll see a clear `400 … content is empty` failure for it
rather than silence. And nesting is for your convenience only: the collection is
**flat** (documents are stored by filename), so two same-named files in different
subfolders become indistinguishable entries — give them distinct names.

**Create the collection first.** Open WebUI / AnythingLLM won't auto-create it — make the Knowledge collection (Open WebUI: **Workspace → Knowledge → + New Knowledge**) or workspace *before* syncing, and point `TARGET` at its id. If you sync to a non-existent target, files upload but land in no collection and `#` shows nothing in Open WebUI.

---

## Configuring the script

**Nothing is edited inside `sync_folder.py`.** All settings live in a config file,
so upgrading the script never means re-customising it.

Create one next to the script (or wherever you run from):

```bash
python3 sync_folder.py --init-config     # writes a commented sync_folder.conf
```

Then edit it — `TARGET`, `WATCH_DIR`, `STATE_FILE` are the ones that matter:

```ini
BACKEND    = openwebui
WATCH_DIR  = ~/papers
TARGET     = c411c9dc-289a-4e4c-bfa9-c5fab84d22c6
BASE_URL   = http://localhost:3000
KEY_FILE   = ~/.rag_sync_key
STATE_FILE = ~/.rag_sync_state.json

DESCRIBE_FIGURES = true
OCR_FALLBACK     = true
```

`~` and `$HOME` are expanded, inline `#` comments are allowed, and quoting a value
keeps it verbatim.

**Where the config file is looked for** (first hit wins):

1. `--config <path>` on the command line
2. `$RAG_CONFIG`
3. `./sync_folder.conf` — the directory you run from
4. `sync_folder.conf` next to the script
5. `~/.config/rag_sync/config`

**Precedence for each individual setting:** an explicit **CLI flag** beats an
**environment variable** (`RAG_<NAME>`), which beats the **config file**, which
beats the built-in default. So the config file holds your normal setup, the env
var is for a one-off, and a flag like `--no-describe-figures` overrides both.

Every run prints what's actually in effect, which makes a wrong target or a shared
state file obvious before anything is uploaded:

```
[sync] config: /home/you/sync/sync_folder.conf
[sync] openwebui at http://localhost:3000 | target=c411c9dc-… | dir=/home/you/papers | state=.rag_sync_state.json
```

Running several libraries is then just several config files:

```bash
python3 sync_folder.py --config ~/sync/papers.conf  --describe-figures
python3 sync_folder.py --config ~/sync/reports.conf --describe-figures
```

Give each one a **different `STATE_FILE`** — otherwise they overwrite each other's
records and every run looks like a full re-sync.

### The two side files: who creates them, and when

| File | Config entry | Created by | When |
|------|--------------|-----------|------|
| API key (default `~/.rag_sync_key`) | `KEY_FILE` | **you**, by hand — the script only reads it | before the first sync; the run aborts immediately if it's missing |
| Sync state (default `~/.rag_sync_state.json`) | `STATE_FILE` | **the script**, automatically | checkpointed every 10 files *or* 2 minutes, on exit, and at the end of every run; parent directories are created if needed |
| Heartbeat (`<STATE_FILE>.progress`) | — follows `STATE_FILE` | **the script**, automatically | written continuously while running, deleted on a clean finish; read by `--status` |

So the only one you make yourself is the key file:

```bash
echo 'sk-your-key' > ~/.rag_sync_key && chmod 600 ~/.rag_sync_key
```

Name them per library in the config file (or via `RAG_KEY_FILE` / `RAG_STATE_FILE`);
if you leave them unset, the defaults above are used:

```ini
KEY_FILE   = ~/.rag_sync_papers_key
STATE_FILE = ~/.rag_sync_papers_state.json
```

Deleting the state file is harmless — it just makes the next run treat every file
as new (see [Re-syncing / resetting](#re-syncing--resetting)). `new_rag_instance.py`
creates both files for a new instance automatically, named after it.

**Finding the API key:**

- **Open WebUI:** enable it first — **Admin Panel → Settings → Authentication → Enable API Key** (Save). Then create it — your **avatar → Settings → Account**, scroll to the bottom, create a key (starts with `sk-`). In v0.10.x the enable toggle is under Authentication, *not* General.
- **AnythingLLM:** **Settings → Tools → Developer API**.

Store it once in the key file (keeps the secret out of the script and cron):

```bash
echo 'sk-your-key-here' > ~/.rag_sync_key && chmod 600 ~/.rag_sync_key
```

**Finding the target id (`TARGET`):**

- **Open WebUI:** the knowledge collection id is the last part of its URL — open the collection under **Workspace → Knowledge** and copy the id from `.../knowledge/<this-id>`.
- **AnythingLLM:** the workspace **slug** (the URL-safe name in the workspace's address).

---

## Running it

```bash
python3 sync_folder.py                     # add/update only
python3 sync_folder.py --prune             # full mirror (also removes deleted files)
python3 sync_folder.py --force             # re-sync everything even if unchanged
python3 sync_folder.py --ocr-fallback      # auto-OCR PDFs that extract to empty, then retry
python3 sync_folder.py --describe-figures  # also index vision descriptions of plots/figures/images
python3 sync_folder.py --no-preflight      # skip the pre-upload low-text warning
```

Flags combine, e.g. `--prune --ocr-fallback --describe-figures`.

Environment variables override the in-file defaults for one-off runs:

```bash
RAG_BACKEND=anythingllm RAG_TARGET=papers python3 sync_folder.py
```

### Following progress

The run works out the full to-do list before uploading anything, so every line is
numbered and an ETA appears every 25 files (`PROGRESS_EVERY`, 0 to switch off):

```
[sync] 1482 file(s) on disk: 430 to process, 1051 unchanged, 1 image(s) ignored (needs --describe-figures)
[sync] [12/430] adding Chen_FlashSintering_2019.pdf …
[sync] --- 25/430 done (6%) — 14 min elapsed, ~228 min left at 33.6s/file ---
```

The denominator is *work to do*, not the whole folder. Nothing is hidden, though —
the closing lines reconcile the two, so a count that looks short is explained rather
than mysterious:

```
[sync] done — processed 430/430 to-do file(s): 425 added, 0 updated, 3 already-present, 2 failed, 0 removed.
[sync] folder holds 1490 file(s): 430 to process, 1051 unchanged, 9 image(s) ignored; 1481 tracked in state.
```

"Processed" means *attempted this run* — added + updated + already-present + failed.
The categories deliberately **not** attempted are named on the second line:
unchanged files, and images when `--describe-figures` is off.

**From another terminal**, `--status` reads the state file and reports the same
thing without touching the server. It's safe to run while a sync is going, and
accurate to within ~10 files (the checkpoint interval):

```bash
python3 sync_folder.py --config papers.conf --status
```

```
[sync] [###############.........................] 561/1481 processed (38%)
[sync] folder holds 1490 file(s): 561 processed, 920 to go, 9 image(s) ignored (needs --describe-figures)
[sync] RUNNING (pid 48213) — [562/1481] Chen_Thesis_2021.pdf
[sync]   currently: figures page 143/312 of Chen_Thesis_2021.pdf  (24.7 min on this file)
```

Liveness is decided by a heartbeat file plus the process id — **not** by how recently
the state file changed. That distinction matters: one 300-page PDF under
`--describe-figures` can hold the run for an hour, which looks identical to a hang if
you only watch timestamps. If the process is gone you get the position it died at:

```
[sync] NOT running — process 48213 is gone; it stopped while on [562/1481] Chen_Thesis_2021.pdf (server embedding).
[sync]   just re-run the same command; it resumes from the state file.
```

The heartbeat lives next to the state file (`<state>.progress`) and is deleted on a
clean finish. It also acts as a **lock**: starting a second sync against the same
library is refused, because both runs would write the same state file (last writer
wins, so progress is lost) and race each other into false "Duplicate content
detected" errors.

```
[sync] ERROR: another sync is already running for this library (pid 48213, on [562/1481] Chen_Thesis.pdf).
       Stop it with:   kill 48213
```

A heartbeat left behind by a crashed run doesn't block anything — the PID is checked,
not just the file. Syncing two *different* libraries at once is fine (separate state
files); `--allow-parallel` overrides the check if you're certain.

> **After upgrading mid-run:** a sync started from a pre-2026.08.01.6 copy writes no
> heartbeat, so `--status` can't see it. Rather than calling it idle, it says the run
> looks active but publishes no heartbeat, and suggests `pgrep -af sync_folder`.
> Restart the sync with the current script to get live per-file status.

Long figure runs also report themselves inline every 10 pages:

```
[sync]   describing figures in Chen_Thesis_2021.pdf (312 pages, model=llava)…
[sync]     … page 140/312 (24 min, ~30 min left on this file)
```

For a long run, start it under `tmux`/`screen` so closing the SSH session doesn't
kill it, or log it and tail the log:

```bash
python3 sync_folder.py --config papers.conf --describe-figures 2>&1 | tee ~/sync.log
grep -c '^\[sync\] \[' ~/sync.log     # files attempted so far
```

`nohup` works equally well; output is line-buffered on purpose, so the log fills
continuously rather than in 8 KB bursts:

```bash
nohup python3 sync_folder.py --config papers.conf > ~/sync.log 2>&1 &
tail -f ~/sync.log
```

### Stopping a run

```bash
kill <pid>            # plain SIGTERM is enough
```

Both `kill` and Ctrl-C are handled: the state file is written, the run reports
`interrupted — progress saved`, and re-running the same command resumes. Only
`kill -9` loses progress back to the last checkpoint (≤10 files or 2 minutes).

Two subtleties that make this work, in case you're wondering why the script bothers:
SIGTERM's default action would kill Python outright without running exit handlers,
and a shell sets SIGINT to *ignored* for background jobs — so after `nohup … &`,
`kill -INT` would otherwise be silently swallowed and the run would carry on.

## Running a second library

**Key constraint:** in Open WebUI the **embedding model is a global setting**
(Admin → Documents), not per-collection. Two collections in the same instance
therefore share one embedding model, and changing it invalidates everything
already indexed. Which route you take depends on whether you need a *different*
embedding.

### A. Second library, SAME embedding — one instance

Order: create the collection in the UI, *then* write the config.

```bash
# 1. Workspace → Knowledge → + New Knowledge, then copy the id from its URL
# 2. copy the config and edit TARGET, WATCH_DIR, STATE_FILE (KEY_FILE can stay —
#    it's the same instance, so the same API key works)
cp sync_folder.conf reports.conf

# 3. put the documents in the folder you set as WATCH_DIR, then sync
python3 sync_folder.py --config reports.conf --describe-figures
```

A distinct `STATE_FILE` per library is not optional — without it both libraries
write the same state file, so each run treats the other library's files as new.
The same applies to `KEY_FILE` when the libraries live in **different instances**:
API keys are per-instance, so each needs its own key file. Neither path is
hardcoded anywhere — both are ordinary config entries, and the defaults
(`~/.rag_sync_key`, `~/.rag_sync_state.json`) only apply when you don't set them.

### B. Second library with a DIFFERENT embedding — second instance

Order matters here too, but the other way round: **create the instance first**, and
it writes the config file for you — there are no names to set by hand.

```bash
# 1. create the instance. This also pulls the embedding model, creates the admin
#    account, the API key file, the Knowledge collection, and <collection>.conf
python3 new_rag_instance.py \
  --collection Reports --embed-model bge-m3 --port 3002 \
  --watch-dir ~/reports --email me@example.com

# 2. put your documents in ~/reports

# 3. sync — the generated config already has the collection id, BASE_URL,
#    KEY_FILE and STATE_FILE filled in
python3 sync_folder.py --config reports.conf
```

You only open `reports.conf` if you want to *change* something (turn off
`DESCRIBE_FIGURES`, point `WATCH_DIR` elsewhere, …).

Step-by-step with verification checkpoints: **`NEW_INSTANCE_RUNBOOK.md`**.

Either way, check the first two lines of sync output before letting a long run
proceed — they echo the config path, target, folder and state file, which is the
cheapest way to catch a copy-paste mistake before anything is uploaded.

Pre-set for you in the new instance: embedding engine (Ollama) and model, chunk
size/overlap, embedding batch size 32, API keys enabled, new signups set to
`pending`, the FD ulimit, and the `host.docker.internal` route to your existing
Ollama. Useful flags: `--dry-run` (print the docker command only), `--name`,
`--chunk-size`, `--batch-size`, `--skip-pull`.

Doing it by hand instead is fine too — run the container with its own volume and
`-e RAG_EMBEDDING_MODEL=…`, then create the account/key/collection in the UI and
pass `RAG_BASE_URL`, `RAG_TARGET`, `RAG_KEY_FILE`, `RAG_STATE_FILE` to
`sync_folder.py` yourself.

Both instances share the same Ollama (so the same chat models are available) and
the same GPU — expect them to queue behind each other under load. Remember the
second container when uninstalling; `uninstall_local_rag.sh` doesn't know about it.

Tip: keep one `.conf` per library (`papers.conf`, `reports.conf`) and select it with
`--config`. Never edit settings inside `sync_folder.py` — there is nothing there to
edit, and that's what makes upgrading the script a straight file copy.

### End-of-run summary

Each run finishes with the totals plus a breakdown of what actually made it in,
so you can see at a glance whether the library is mostly full text or mostly
abstract stubs:

```
[sync] done — 1009 added, 0 updated, 0 removed, 2 already-present, 1009 tracked total.
[sync] processed this run:
         full PDFs                 : 215  (1 recovered via OCR)
         abstract/metadata .md     : 794
         standalone images         : 12
         figure-description docs   : 180  (from 940 page(s) containing figures)
```

For a view of the whole library (not just this run), including which papers are
metadata-only and which PDFs have no extractable text, use `library_stats.py`.

### Pre-flight low-text warning

Before each upload the script does a quick local check and prints a `!` warning if a file looks like it has little extractable text — a JavaScript-shell HTML (a saved journal page that's just the loader, not the article), a scanned/no-text-layer PDF, or a near-empty file. It **only warns; it never blocks the upload.** This helps you spot papers that will index poorly (and often show up later as phantom "duplicates" because many shell pages extract to the same boilerplate). Turn it off with `--no-preflight`, or tune the HTML threshold with `RAG_MIN_TEXT_CHARS` (default 400). The PDF part of the check needs PyMuPDF; it's skipped silently if not installed.

### On a schedule (cron)

Every 15 minutes (add `--prune` to keep the collection mirrored). With defaults set in the script and the key in `~/.rag_sync_key`, the line stays clean:

```bash
# crontab -e
*/15 * * * * /usr/bin/python3 /path/to/sync/sync_folder.py --prune >> $HOME/rag_sync.log 2>&1
```

For near-instant updates instead of polling, swap the loop for Python `watchdog` as a systemd service. AnythingLLM also has built-in **Scheduled Jobs** and a beta **Live Document Sync**.

---

## Rebuilding the whole library from scratch

Steps 1–5 (fetch the papers, recover what curl couldn't, consolidate, and check
coverage) live in the **proxify toolkit README** under *Complete workflow*. Do
those first — in particular the browser pass and the `library_stats.py` coverage
check, since a library that's mostly metadata-only stubs caps what the RAG can
ever answer. Then:

### 6. Wipe the collection and sync

```bash
KEY=$(cat ~/.rag_sync_key)
curl -sS -X POST "http://localhost:3000/api/v1/knowledge/<COLLECTION_ID>/reset" \
  -H "Authorization: Bearer $KEY"; echo
curl -sS -X DELETE "http://localhost:3000/api/v1/files/all" \
  -H "Authorization: Bearer $KEY"; echo
rm -f ~/.rag_sync_state.json      # the STATE_FILE of the library you're rebuilding

python3 sync_folder.py --describe-figures --ocr-fallback
```

Both API calls matter — see
[Fully wiping an Open WebUI collection](#fully-wiping-an-open-webui-collection-true-clean-slate)
for why resetting the collection alone isn't enough. With `--describe-figures`
this takes hours for a large library (one vision-model call per page), so run it
under `tmux`/`screen`.

### 7. Verify

- The final `[sync] done —` line should show nearly everything **added**, with
  `already-present` down to just genuinely identical files (e.g. the same paper
  saved twice under different years).
- **Workspace → Knowledge → Papers** should show a file count in the right
  ballpark (documents + figure-description companions).
- Ask a real question in a new chat: select your chat model, type `#` and click
  the collection, and confirm the answer cites actual papers.

---

## Re-syncing / resetting

The script skips files whose content hasn't changed. To deliberately re-upload/re-embed papers you've already synced, use `--force`:

```bash
python3 sync_folder.py --force
```

`--force` treats every file as changed and, for anything it previously uploaded, **removes the old copy first (via its tracked remote id) before re-uploading** — a clean refresh with no duplicates. It also regenerates figure docs if combined with `--describe-figures`.

If you'd rather start fresh, delete the state file so nothing is remembered:

```bash
rm ~/.rag_sync_state.json      # or whatever STATE_FILE your config sets
python3 sync_folder.py
```

But deleting state forgets the remote ids, so old copies are **not** removed — you'd get duplicates. For a full clean slate, empty the collection *and* delete the state file.

### Fully wiping an Open WebUI collection (true clean slate)

There's no "delete all" button in the UI, and — importantly — **resetting the collection is not enough on its own.** Open WebUI keeps every uploaded file as a stored *file object* and dedupes new uploads against those. Orphan file objects from earlier syncs survive a collection reset and then cause phantom `duplicate content` errors on re-sync — even for papers no longer in the collection. A real clean slate takes two API calls: reset the collection **and** delete the stored file objects.

```bash
KEY=$(cat ~/.rag_sync_key)

# 1. reset the collection (keeps the same id, so TARGET stays valid)
curl -sS -X POST "http://localhost:3000/api/v1/knowledge/<COLLECTION_ID>/reset" \
  -H "Authorization: Bearer $KEY"; echo

# 2. delete ALL stored file objects (the step people miss — it's what the
#    duplicate check matches against). Safe here because the collection is now empty.
curl -sS -X DELETE "http://localhost:3000/api/v1/files/all" \
  -H "Authorization: Bearer $KEY"; echo

# 3. clear THIS library's state and re-sync fresh
#    (use the STATE_FILE from the config file you sync with, not necessarily this default)
rm -f ~/.rag_sync_state.json
python3 sync_folder.py --describe-figures
```

Sanity-check counts (both should read `0` after step 2): files in the collection —
`curl -s http://localhost:3000/api/v1/knowledge/<COLLECTION_ID> -H "Authorization: Bearer $KEY" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(len((d.get("data") or {}).get("file_ids") or []))'`
and total stored files —
`curl -s http://localhost:3000/api/v1/files/ -H "Authorization: Bearer $KEY" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["items"]))'`.

> `DELETE /api/v1/files/all` removes **every** file you've uploaded to Open WebUI (across all collections). That's what you want for a single-collection setup; if you keep multiple collections, delete files individually. For routine refreshes that don't need a full wipe, prefer `--force`.

---

## OCR fallback (`--ocr-fallback`)

When the server reports "content empty" for a PDF, the script runs `ocrmypdf --force-ocr` on it locally and retries the upload once — since both the script and OCR run on the Spark, it self-heals text-less PDFs with no manual step.

Install the OCR tools once:

```bash
sudo apt update
sudo apt install -y ocrmypdf jbig2enc
```

`ocrmypdf` pulls in Tesseract and Ghostscript automatically. `jbig2enc` is optional but recommended — it compresses scanned/monochrome pages so OCR'd PDFs don't balloon in size. For non-English OCR add the matching pack, e.g. `sudo apt install tesseract-ocr-fra`.

> **Caveat:** OCR fallback only helps when a PDF genuinely lacks a text layer. It will *not* fix a misconfigured extraction engine (e.g. Tika selected but not running) — in that case even the OCR'd copy fails, and the script reports it and moves on. If *every* file fails, fix the engine (see the main README's Tika section), don't rely on OCR.

---

## Making figures/plots retrievable (`--describe-figures`)

Text-only RAG can't "see" plots — data locked in figures is invisible to retrieval. `--describe-figures` bridges that: for each PDF it renders every page, has a **local vision model** (LLaVA, via Ollama) describe any figures/plots/charts (caption, axes, series, trends, legible values), and uploads those descriptions as a companion document, retrievable alongside the text.

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

### Standalone images

It also indexes **standalone image files** — `.png`, `.jpg/.jpeg`, `.tif/.tiff`, `.webp`, `.bmp`, `.gif`. Each is described by the vision model and its description uploaded as a text document, so loose figures/screenshots/plots become retrievable too (skipped without `--describe-figures`, since a raw image has no extractable text). Because a standalone image usually has no caption, the script also **passes the file name to the vision model as context** — so a descriptive name like `voltivity_resistivity_vs_temp.png` genuinely improves the description. If a **sidecar text file** with the same basename sits next to the image (`figure1.png` + `figure1.txt`, or `figure1.png.txt`; also `.md`/`.caption`/`.json`), its contents are fed in as extra context too. Tracking, `--force`, and `--prune` apply to images the same as documents.

Each figure/image description doc is stamped with a **source-unique content id** (the md5 of the source file), so two different figures — even from the same paper (e.g. a `.html` plus a `.gif` and a `.jpg`) — can never collide on Open WebUI's content-hash dedup and be wrongly rejected as "duplicate."

> **Vision model note (DGX Spark).** The default is **`llava`**, not Llama 3.2 Vision. The Spark's custom Blackwell-optimized Ollama build does **not** support the `mllama` architecture that Llama 3.2 Vision uses — it fails to load with `unknown model architecture: 'mllama'` (a 500 from Ollama). LLaVA uses a supported architecture and works. If you need a specific vision model this build won't load, run a stock Ollama in a container just for it (`docker run -d --name ollama-vision --gpus all -p 11435:11434 ollama/ollama`) and point the script at it with `RAG_OLLAMA_URL=http://localhost:11435`.

> **Important — numbers are approximate.** Vision models reliably capture *what* a figure shows and its trends, but they **hallucinate exact chart values**. Treat extracted numbers as approximate and verify against the source figure. For precise datapoints use a plot-digitizer tool. This is a retrieval/understanding aid, not a data-extraction guarantee, and it's slower than a text-only sync (one model call per page), so it's opt-in.

For a heavier-duty "search papers *by* their visual content" system (page-as-image retrieval with ColPali/ColQwen + a vector DB like Qdrant), that's a separate, larger build outside this text-RAG pipeline.

---

## All flags & environment variables

| Flag | Env var | Effect |
|------|---------|--------|
| `--prune` | `RAG_PRUNE=1` | remove from the collection when a file is deleted from the folder |
| `--force` | `RAG_FORCE=1` | re-sync every file even if unchanged (removes old copy first) |
| `--ocr-fallback` | `RAG_OCR_FALLBACK=1` | OCR a PDF locally and retry when the server extracts no text |
| `--describe-figures` | `RAG_DESCRIBE_FIGURES=1` | describe PDF figures + standalone images with a vision model |
| `--no-preflight` | `RAG_NO_PREFLIGHT=1` | disable the pre-upload low-text warning |
| `--no-convert-legacy` | `RAG_CONVERT_LEGACY=0` | don't convert `.doc`/`.ppt`/`.xls` before upload (on by default; needs libreoffice or antiword) |
| `--status` | — | print how far along this library is, then exit (safe during a run) |
| `--allow-parallel` | — | permit a second concurrent run against the same library (normally refused) |
| — | `RAG_PROGRESS_EVERY` | progress/ETA line every N files (default `25`, `0` = off) |
| — | `RAG_MIN_TEXT_CHARS` | HTML low-text threshold in chars (default 400) |
| — | `RAG_BACKEND` | `openwebui` / `anythingllm` |
| — | `RAG_TARGET` | collection id / workspace slug |
| — | `RAG_API_KEY` / `RAG_KEY_FILE` | key value / path to key file (default `~/.rag_sync_key`) |
| — | `RAG_STATE_FILE` | per-library sync state (default `~/.rag_sync_state.json`) — **required when syncing more than one library** |
| — | `RAG_WATCH_DIR` | folder to sync |
| — | `RAG_BASE_URL` | override the tool's base URL |
| — | `RAG_FIGURE_MODEL` | vision model tag (default `llava`) |
| — | `RAG_OLLAMA_URL` | Ollama URL for figure calls (default `http://localhost:11434`) |
| — | `RAG_FIGURE_DPI` | page render DPI for the vision model (default `150`) |
| — | `RAG_ATTACH_TIMEOUT` | seconds to let one server-side embed request run (default `900`) |

---

## Sync troubleshooting

**`ReadTimeout` on `/api/v1/knowledge/…/file/add` — the run stops with a traceback.**
Embedding happens server-side on the shared GPU, so one document can take longer
than the request budget — especially while a second library is syncing or a chat
model is loaded. Two things changed to handle it:

- A timeout is now treated as **ambiguous rather than fatal**. The server may finish
  after the client gives up, so the script waits, asks the collection whether the
  file actually landed, and only re-posts if it didn't (up to 3 tries, doubling the
  budget). A file that landed is recorded, so it won't be uploaded twice.
- The budget itself is configurable and defaults to 900 s:

  ```ini
  ATTACH_TIMEOUT = 1800        # in your .conf, for a busy machine
  ```

If a single file still can't get through, it's reported and the run continues; the
file isn't recorded, so the next run retries it. Reduce contention by not syncing
two libraries at once, and by lowering **Admin → Settings → Documents → Embedding
Batch Size** (32) and **Concurrent Requests** (4).

**A crash or Ctrl-C used to lose the whole session's progress.** State is now
checkpointed every 10 files and again on exit, so a re-run resumes instead of
re-uploading everything. You'll see `[sync] progress saved to …` if a run ends
abnormally.

**Open WebUI is stuck on the "OI" splash screen / reload loop.**
First diagnose — the two causes have completely different fixes:

```bash
sudo docker ps                                   # healthy or unhealthy?
sudo docker logs --tail 30 open-webui            # look for repeated 404s
ollama ps                                        # what's loaded / busy
curl -m 5 http://localhost:11434/api/tags >/dev/null && echo "ollama OK" || echo "ollama busy"
curl -m 5 http://localhost:3000/health && echo " backend OK"
```

*Cause 1 — stale cached frontend (most common; nothing to do with the sync).*
The logs show a fast-repeating 404 for an immutable JS chunk, e.g.:

```
"GET /_app/immutable/chunks/DI6U-d8h.js HTTP/1.1" 404
"GET /_app/version.json HTTP/1.1" 200
```

Your browser is serving a cached app shell from the **previous** Open WebUI image
that references chunks the updated container no longer has; the app can't boot and
SvelteKit's version polling reloads it in a loop. Ollama and the backend are both
healthy in this case. Fix it in the **browser**, not on the server:

1. Open the site in an incognito window — if it works there, it's confirmed cache.
2. **DevTools (F12) → Application → Storage → Clear site data** (clears the
   service worker too; a plain Ctrl+Shift+R often isn't enough), or delete the
   site's data via Chrome → Settings → Privacy → Site settings.
3. Reload.

Do one hard refresh / clear-site-data after any update that bumps the Open WebUI
image — that's the whole prevention.

*Cause 2 — Ollama saturated by a long `--describe-figures` sync.* Here `ollama
busy` fails while the backend is OK, because the UI's model-list call is queued
behind thousands of vision-model requests. `sudo docker restart open-webui` clears
the symptom; to prevent it, let Ollama serve concurrently (both models fit in the
Spark's 128 GB):

```bash
sudo systemctl edit ollama
# add under [Service]:
#   Environment="OLLAMA_MAX_LOADED_MODELS=2"
#   Environment="OLLAMA_NUM_PARALLEL=2"
sudo systemctl daemon-reload && sudo systemctl restart ollama
```

Also useful: enable **Cache Base Model List** (Admin Settings → Connections) so
the model list is fetched once at startup instead of on every page load; or run
the vision work against a separate containerized Ollama
(`RAG_OLLAMA_URL=http://localhost:11435`); or just run long figure syncs overnight.


**`400: Duplicate content detected` for files I'm sure are unique — the common cause.**
Open WebUI's upload endpoint returns `200` **before** it has extracted the document's text, so for a moment the file's stored content is empty. If the script attaches it in that window, the server hashes *empty* content, matches it against any other still-unprocessed file, and reports "Duplicate content detected" — even though the two documents are completely different. This affects PDFs and markdown alike, recurs on every run, and is unaffected by wiping the collection. **Fixed in 2026.07.27.3**: the script now polls the uploaded file until the server reports extracted text (or a terminal status) before attaching, and never deletes a file on a duplicate response. If you see this on an older version, upgrade the script.

**`400: Duplicate content detected` — other causes.**
Usually not a real duplicate — Open WebUI dedupes by document *content*, so the file is already stored. It happens when the state file and the collection drift apart (you deleted `~/.rag_sync_state.json`, or changed `TARGET`). The script treats this as "already present," records the file so it won't retry, and reports a count at the end. If a file is flagged duplicate even though it's **not** in the collection, the cause is orphan stored file objects from earlier syncs — resetting the collection alone won't clear them; you must also `DELETE /api/v1/files/all` (see "Fully wiping an Open WebUI collection"), then `rm ~/.rag_sync_state.json` and re-sync.

**A few very large PDFs fail (books, theses, proceedings — hundreds of pages).**
Not a language or content problem. Extracting *and embedding* a 100–430 page
document with 75k–900k characters takes Open WebUI many minutes; if the script
attaches before that finishes it lands back in the empty-content race, and if the
request itself outlives its timeout the connection drops. Handling (2026.07.28.4):

- The wait is scaled by **document weight** — pages and sampled text volume, not
  just file size (a 2.5 MB / 40-page paper with 75k chars gets ~8 min, a 432-page
  volume ~36 min, capped at 60). Earlier versions scaled by bytes alone, which
  under-served small-but-dense files.
- The attach request gets the same budget, and read-timeouts / dropped
  connections are caught and reported per file instead of aborting the run.
- PDF text is sampled *evenly across* the file, so scanned cover pages no longer
  make a text-rich thesis look scanned.
- The OCR fallback is skipped for PDFs that already contain text — force-OCRing a
  200-page file is slow and cannot help.

If one still fails, sync it alone (put just that file in a folder and run against
it) so it isn't competing with the rest, or split the PDF into parts.

**`400: Cannot connect to host host.docker.internal:11434 [Too many open files]`**
The document extracted fine, but embedding it exhausted Open WebUI's
file-descriptor limit: a 500k–900k-character document becomes thousands of chunks,
and with **Async Embedding Processing** on and **Embedding Concurrent Requests =
0** (unlimited) it opens a connection to Ollama for each one. Two fixes — do both:

1. **Throttle the embedding** (Admin Settings → **Documents**), no restart needed:
   - *Embedding Batch Size*: `1` → **`32`** — far fewer requests for the same work
   - *Embedding Concurrent Requests*: `0` → **`4`**
2. **Raise the container's FD limit** so it can't recur. `setup_local_rag.sh` and
   `update_local_rag.sh` now launch Open WebUI with
   `--ulimit nofile=65536:65536`; to apply it to an existing container, recreate it
   with `./update_local_rag.sh` (data is preserved).

Only very large documents trigger this, which is why a library of ordinary papers
syncs cleanly and a few books/proceedings volumes fail.

**Every `.doc` / `.ppt` / `.xls` fails with `content provided is empty`.**
Those are the **pre-2007 binary** Office formats. Open WebUI's Default extractor
reads the modern zip-based `.docx`/`.pptx`/`.xlsx` but cannot read the old binary
ones at all — the files are perfectly fine, the parser simply can't open them. Note
this is unrelated to genuinely empty files, despite the identical error message.

The script now converts them locally before uploading, which needs one tool:

```bash
sudo apt install libreoffice-writer     # handles .doc, .ppt and .xls
# or, lighter and .doc-only:
sudo apt install antiword
```

With that installed you'll see, per file:

```
[sync] [61/2548] adding fl54x00.doc …
[sync]   converted .doc → .docx with soffice (the server cannot read legacy Office files)
```

and a count in the summary (`legacy .doc/.ppt/.xls converted : 214`). If no converter
is installed, the run says so **once at the start** rather than failing 200 times:

```
[sync] ! 214 legacy Office file(s) (.doc/.ppt/.xls) need a local converter, and none is installed.
```

The original file is what gets hashed and tracked, so nothing re-converts on the next
run; only the uploaded copy is modern (temporary, deleted immediately). Set
`CONVERT_LEGACY = false` to disable. Enabling Tika instead also works — it parses
legacy Office natively — but it's a whole extra service for one file format.

**`skipping <name> — the file contains no text`.** Not an error. A genuinely empty
text file (0 bytes, or just a newline — stray `readonly.txt`, `.gitkeep`-style
placeholders and the like) can't be indexed by anything, so it's skipped locally
instead of being uploaded, rejected by the server, and logged as a failure on every
run. Such files are counted separately (`N skipped as empty`) and don't affect the
exit code. They're recorded in the state file, so if one later gains content its
hash changes and it gets indexed normally. PDFs are never skipped this way — an
image-only PDF has no text *yet*, which is what `--ocr-fallback` is for.

**`400: The content provided is empty` (or a chat says "No sources found").**
Open WebUI extracted no text from a file that *does* have content locally. Likely causes: (1) the extraction engine is set to Tika/Docling but that service isn't running — set it back to **Default** or start the service (see the main README's Tika section); (2) the PDF has no text layer — test with `pdftotext file.pdf - | head`, and use `--ocr-fallback` (or OCR manually) if empty; (3) the Default parser choked on a specific PDF — OCR it or use Tika. Filenames with spaces/quotes can also break the upload — prefer `Underscore_Names.pdf`.

**Can't find API Keys in Open WebUI.**
The section doesn't appear in **Settings → Account** until enabled: **Admin Panel → Settings → Authentication → Enable API Key**. Then create the `sk-...` key under Account. Not needed if you only upload via the GUI.

**Typing `#` shows no popup / `#Papers` isn't grounding answers.**
The `#` menu lists Knowledge collections that **contain documents**. If empty, the collection is empty or doesn't exist — create it and sync into it (`TARGET` = its id). Once it has documents, `#` lists it; be sure to *click* the collection in the popup, not just type the text.
