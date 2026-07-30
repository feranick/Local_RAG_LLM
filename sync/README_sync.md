# Folder Sync for Local RAG — `sync_folder.py`

**Version 2026.07.28.5**

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

**Create the collection first.** Open WebUI / AnythingLLM won't auto-create it — make the Knowledge collection (Open WebUI: **Workspace → Knowledge → + New Knowledge**) or workspace *before* syncing, and point `TARGET` at its id. If you sync to a non-existent target, files upload but land in no collection and `#` shows nothing in Open WebUI.

---

## Configuring the script

Four settings. Three live in the **CONFIG block at the top of `sync_folder.py`**; the API key is kept in a separate file. Any matching environment variable overrides the in-file value.

| Setting | In-file variable | Where the value comes from |
|---------|------------------|----------------------------|
| Backend | `BACKEND` | `"openwebui"` or `"anythingllm"` |
| Folder to sync | `WATCH_DIR` | a path — defaults to `~/papers` (see path note) |
| Target collection | `TARGET` | the collection id / workspace slug (see "Finding the id") |
| API key | (not in the file) | `~/.rag_sync_key` (see "Finding the API key") |

**Editing paths (`WATCH_DIR`) — important:** this is Python, so do **not** use `$HOME` (Python won't expand it) and do **not** rename the variable to `RAG_WATCH_DIR` (that's only the env-var name it falls back to). The default already points at `~/papers`. To hardcode a different folder, change only the fallback path:

```python
# default — leave as-is for ~/papers:
WATCH_DIR = pathlib.Path(os.environ.get("RAG_WATCH_DIR", str(pathlib.Path.home() / "papers")))

# or hardcode an absolute path:
WATCH_DIR = pathlib.Path(os.environ.get("RAG_WATCH_DIR", "/home/feranick/research/papers"))
```

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
rm -f ~/.rag_sync_state.json

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
rm ~/.rag_sync_state.json
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

# 3. clear local state and re-sync fresh
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
| — | `RAG_MIN_TEXT_CHARS` | HTML low-text threshold in chars (default 400) |
| — | `RAG_BACKEND` | `openwebui` / `anythingllm` |
| — | `RAG_TARGET` | collection id / workspace slug |
| — | `RAG_API_KEY` / `RAG_KEY_FILE` | key value / path to key file (default `~/.rag_sync_key`) |
| — | `RAG_WATCH_DIR` | folder to sync |
| — | `RAG_BASE_URL` | override the tool's base URL |
| — | `RAG_FIGURE_MODEL` | vision model tag (default `llava`) |
| — | `RAG_OLLAMA_URL` | Ollama URL for figure calls (default `http://localhost:11434`) |
| — | `RAG_FIGURE_DPI` | page render DPI for the vision model (default `150`) |

---

## Sync troubleshooting

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

**`400: The content provided is empty` (or a chat says "No sources found").**
Open WebUI extracted no text from that file. Likely causes: (1) the extraction engine is set to Tika/Docling but that service isn't running — set it back to **Default** or start the service (see the main README's Tika section); (2) the PDF has no text layer — test with `pdftotext file.pdf - | head`, and use `--ocr-fallback` (or OCR manually) if empty; (3) the Default parser choked on a specific PDF — OCR it or use Tika. Filenames with spaces/quotes can also break the upload — prefer `Underscore_Names.pdf`.

**Can't find API Keys in Open WebUI.**
The section doesn't appear in **Settings → Account** until enabled: **Admin Panel → Settings → Authentication → Enable API Key**. Then create the `sk-...` key under Account. Not needed if you only upload via the GUI.

**Typing `#` shows no popup / `#Papers` isn't grounding answers.**
The `#` menu lists Knowledge collections that **contain documents**. If empty, the collection is empty or doesn't exist — create it and sync into it (`TARGET` = its id). Once it has documents, `#` lists it; be sure to *click* the collection in the popup, not just type the text.
