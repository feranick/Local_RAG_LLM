# Folder Sync for Local RAG — `sync_folder.py`

**Version 2026.07.11.6**

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
```

Flags combine, e.g. `--prune --ocr-fallback --describe-figures`.

Environment variables override the in-file defaults for one-off runs:

```bash
RAG_BACKEND=anythingllm RAG_TARGET=papers python3 sync_folder.py
```

### On a schedule (cron)

Every 15 minutes (add `--prune` to keep the collection mirrored). With defaults set in the script and the key in `~/.rag_sync_key`, the line stays clean:

```bash
# crontab -e
*/15 * * * * /usr/bin/python3 /path/to/sync/sync_folder.py --prune >> $HOME/rag_sync.log 2>&1
```

For near-instant updates instead of polling, swap the loop for Python `watchdog` as a systemd service. AnythingLLM also has built-in **Scheduled Jobs** and a beta **Live Document Sync**.

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

**`400: Duplicate content detected` for files I'm sure are unique.**
Usually not a real duplicate — Open WebUI dedupes by document *content*, so the file is already stored. It happens when the state file and the collection drift apart (you deleted `~/.rag_sync_state.json`, or changed `TARGET`). The script treats this as "already present," records the file so it won't retry, and reports a count at the end. If a file is flagged duplicate even though it's **not** in the collection, the cause is orphan stored file objects from earlier syncs — resetting the collection alone won't clear them; you must also `DELETE /api/v1/files/all` (see "Fully wiping an Open WebUI collection"), then `rm ~/.rag_sync_state.json` and re-sync.

**`400: The content provided is empty` (or a chat says "No sources found").**
Open WebUI extracted no text from that file. Likely causes: (1) the extraction engine is set to Tika/Docling but that service isn't running — set it back to **Default** or start the service (see the main README's Tika section); (2) the PDF has no text layer — test with `pdftotext file.pdf - | head`, and use `--ocr-fallback` (or OCR manually) if empty; (3) the Default parser choked on a specific PDF — OCR it or use Tika. Filenames with spaces/quotes can also break the upload — prefer `Underscore_Names.pdf`.

**Can't find API Keys in Open WebUI.**
The section doesn't appear in **Settings → Account** until enabled: **Admin Panel → Settings → Authentication → Enable API Key**. Then create the `sk-...` key under Account. Not needed if you only upload via the GUI.

**Typing `#` shows no popup / `#Papers` isn't grounding answers.**
The `#` menu lists Knowledge collections that **contain documents**. If empty, the collection is empty or doesn't exist — create it and sync into it (`TARGET` = its id). Once it has documents, `#` lists it; be sure to *click* the collection in the popup, not just type the text.
