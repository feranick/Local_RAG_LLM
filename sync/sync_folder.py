#!/usr/bin/env python3
"""
sync_folder.py — keep a RAG knowledge base in sync with a local folder.

Mirrors WATCH_DIR into an AnythingLLM workspace or an Open WebUI knowledge
collection:
  * new files          -> uploaded + embedded
  * changed files      -> old copy removed, new copy uploaded (no duplicates)
  * deleted files      -> removed from the collection  (only with --prune)

A per-file record (content hash + the remote id returned at upload) is kept in
a small state file so repeat runs only touch what changed. Designed to be run
repeatedly (cron/systemd).

Configure by editing the CONFIG block below (so a bare `python3 sync_folder.py`
works), or via environment variables which override those defaults:

  RAG_BACKEND     anythingllm | openwebui        (default: openwebui)
  RAG_API_KEY     API key from the tool's UI     (or use the key file, below)
  RAG_KEY_FILE    path to a file holding the key (default: ~/.rag_sync_key)
  RAG_WATCH_DIR   folder to sync                  (default: ~/papers)
  RAG_BASE_URL    override the default base URL
  RAG_TARGET      AnythingLLM workspace slug  OR  Open WebUI knowledge id
                  (the collection must already exist — the script won't create it)
  RAG_PRUNE       "1"/"true" to delete from the collection when a file is
                  removed from the folder (makes the folder the source of truth)
  RAG_FORCE       "1"/"true" to re-sync every file even if unchanged (removes the
                  old copy first, then re-uploads). Same as passing --force.
  RAG_OCR_FALLBACK "1"/"true" to auto-OCR a PDF (ocrmypdf) and retry when the
                  server extracts no text. Same as passing --ocr-fallback.
  RAG_NO_PREFLIGHT "1"/"true" to disable the pre-upload low-text warning
                  (JS-shell HTML / scanned PDF heads-up). Same as --no-preflight.
  RAG_MIN_TEXT_CHARS  min extractable chars before an HTML is flagged (default 400)
  RAG_DESCRIBE_FIGURES "1"/"true" to render each PDF, have a local vision model
                  describe its figures/plots, and upload those descriptions as a
                  companion doc (so plots become retrievable). Also indexes any
                  standalone image files (png/jpg/tiff/…) the same way.
                  = --describe-figures. Needs PyMuPDF (pip install pymupdf) + a
                  vision model in Ollama.
  RAG_FIGURE_MODEL  vision model tag (default: llava). NOTE: the DGX Spark's
                  custom Ollama build does NOT support the 'mllama' architecture,
                  so llama3.2-vision will not load there; llava does.
  RAG_OLLAMA_URL    Ollama base URL for figure calls (default: http://localhost:11434)
  RAG_FIGURE_DPI    page render DPI for the vision model (default: 150)

Get an API key:
  AnythingLLM -> Settings > Tools > Developer API
  Open WebUI  -> Admin Panel > Settings > Authentication > Enable API Key,
                 then Settings > Account > create key (starts with sk-)

Endpoint names shift between versions; check each tool's /docs if a call fails
(Open WebUI: http://localhost:3000/docs).

Usage:
  pip install requests
  echo 'sk-xxxx' > ~/.rag_sync_key && chmod 600 ~/.rag_sync_key   # one-time
  # then, with TARGET filled in below:
  python3 sync_folder.py                       # add/update only
  python3 sync_folder.py --prune               # full mirror (also deletes)
  python3 sync_folder.py --ocr-fallback        # auto-OCR PDFs that extract empty
  python3 sync_folder.py --describe-figures    # also index vision descriptions of plots
  python3 sync_folder.py --force               # re-sync everything (re-embed), no duplicates
"""

import os
import re
import sys
import json
import time
import base64
import shutil
import hashlib
import pathlib
import tempfile
import subprocess

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

# ----------------------------- config -----------------------------------
# Hard-code your settings here so you can just run:  python3 sync_folder.py
# Any environment variable, if set, overrides the value here.
#
#   BACKEND  : "openwebui" or "anythingllm"
#   WATCH_DIR: folder to sync
#   TARGET   : Open WebUI knowledge id  /  AnythingLLM workspace slug
#              (the collection must already exist — the script won't create it)
BACKEND   = os.environ.get("RAG_BACKEND", "openwebui").lower()
WATCH_DIR = pathlib.Path(os.environ.get("RAG_WATCH_DIR", str(pathlib.Path.home() / "papers")))
TARGET    = os.environ.get("RAG_TARGET", "")   # <-- paste your knowledge id / workspace slug here

# API KEY — kept OUT of this file for safety. Resolved in this order:
#   1) RAG_API_KEY environment variable
#   2) a key file (default ~/.rag_sync_key) containing just the key on one line
# Create the key file once:   echo 'sk-xxxx' > ~/.rag_sync_key && chmod 600 ~/.rag_sync_key
KEY_FILE  = pathlib.Path(os.environ.get("RAG_KEY_FILE", str(pathlib.Path.home() / ".rag_sync_key")))
API_KEY   = os.environ.get("RAG_API_KEY", "")
if not API_KEY and KEY_FILE.is_file():
    API_KEY = KEY_FILE.read_text().strip()

# prune = delete from the collection when the file disappears from the folder.
# enabled by --prune on the command line or RAG_PRUNE=1 in the environment.
PRUNE = ("--prune" in sys.argv) or (os.environ.get("RAG_PRUNE", "").lower() in ("1", "true", "yes"))

# force = re-sync every file even if unchanged (re-upload + re-embed). Old copies
# are removed first via their tracked remote id, so no duplicates. Use after
# changing the embedding model, chunk settings, or extraction engine.
FORCE = ("--force" in sys.argv) or (os.environ.get("RAG_FORCE", "").lower() in ("1", "true", "yes"))

# ocr fallback = if a PDF fails because the server extracted no text, OCR it
# locally (ocrmypdf) and retry once. enabled by --ocr-fallback or RAG_OCR_FALLBACK=1.
# Requires the `ocrmypdf` command to be installed (sudo apt install ocrmypdf).
OCR_FALLBACK = ("--ocr-fallback" in sys.argv) or (os.environ.get("RAG_OCR_FALLBACK", "").lower() in ("1", "true", "yes"))

# describe figures = for each PDF, render its pages and have a local vision model
# (via Ollama) describe any figures/plots, then upload those descriptions as a
# companion document so figure content becomes retrievable. Text-based RAG can't
# "see" plots; this bridges that gap. Enabled by --describe-figures / RAG_DESCRIBE_FIGURES=1.
# Requires PyMuPDF (pip install pymupdf) and a vision model pulled in Ollama.
DESCRIBE_FIGURES = ("--describe-figures" in sys.argv) or (os.environ.get("RAG_DESCRIBE_FIGURES", "").lower() in ("1", "true", "yes"))
FIGURE_MODEL = os.environ.get("RAG_FIGURE_MODEL", "llava")   # vision model (Western; loads on the Spark's Ollama build)
OLLAMA_URL   = os.environ.get("RAG_OLLAMA_URL", "http://localhost:11434")
FIGURE_DPI   = int(os.environ.get("RAG_FIGURE_DPI", "150"))

# preflight = before uploading, warn if a file has little extractable text (a
# JavaScript-shell HTML, a scanned/no-text-layer PDF, or a near-empty doc). It
# only warns — it never blocks the upload. Disable with --no-preflight.
PREFLIGHT = ("--no-preflight" not in sys.argv) and (os.environ.get("RAG_NO_PREFLIGHT", "").lower() not in ("1", "true", "yes"))
MIN_TEXT_CHARS = int(os.environ.get("RAG_MIN_TEXT_CHARS", "400"))

DEFAULT_URLS = {
    "anythingllm": "http://localhost:3001",
    "openwebui":   "http://localhost:3000",
}
BASE_URL = os.environ.get("RAG_BASE_URL", DEFAULT_URLS.get(BACKEND, ""))

# document types uploaded directly (text is extracted server-side)
EXTS = {".pdf", ".txt", ".md", ".docx", ".doc", ".epub", ".csv", ".html", ".rtf", ".pptx"}

# standalone image types: indexed only with --describe-figures, by uploading a
# vision-model description of the image (the image itself has no extractable text).
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp", ".gif"}

STATE_FILE = pathlib.Path.home() / ".rag_sync_state.json"
# -------------------------------------------------------------------------


def die(msg: str):
    sys.exit(f"[sync] ERROR: {msg}")


def load_state() -> dict:
    """State schema: {"backend","target","files": {path: {hash, remote_id}}}."""
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {"files": {}}
        # migrate legacy flat {path: hash} format
        if "files" not in data:
            data = {"files": {p: {"hash": h, "remote_id": None}
                              for p, h in data.items()}}
        return data
    return {"files": {}}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def file_hash(p: pathlib.Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------- backend adapters ----------------------------
# each add_* returns a remote_id string used later for removal.

def add_anythingllm(session, path):
    with open(path, "rb") as f:
        r = session.post(f"{BASE_URL}/api/v1/document/upload",
                         files={"file": (path.name, f)}, timeout=300)
    r.raise_for_status()
    docs = r.json().get("documents", [])
    location = docs[0]["location"] if docs else None
    if TARGET and location:
        session.post(f"{BASE_URL}/api/v1/workspace/{TARGET}/update-embeddings",
                     json={"adds": [location]}, timeout=300).raise_for_status()
    return location


def remove_anythingllm(session, remote_id):
    if not remote_id:
        return
    # drop from the workspace's embeddings
    if TARGET:
        session.post(f"{BASE_URL}/api/v1/workspace/{TARGET}/update-embeddings",
                     json={"deletes": [remote_id]}, timeout=300).raise_for_status()
    # and remove the stored document from the system
    session.post(f"{BASE_URL}/api/v1/system/remove-documents",
                 json={"names": [remote_id]}, timeout=300)


def _wait_for_extraction(session, file_id, timeout_s=180):
    """Block until Open WebUI has finished extracting text from an uploaded file.

    The upload endpoint returns 200 *before* extraction completes, so the file's
    content is briefly empty. Attaching it in that window makes the server hash
    EMPTY content — which then collides with any other still-empty file and is
    reported as "Duplicate content detected" even though the documents differ.
    Waiting for the content (or a terminal status) removes that race.
    Returns (chars_extracted, did_wait); chars is -1 on timeout.
    """
    waited = False
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = session.get(f"{BASE_URL}/api/v1/files/{file_id}", timeout=60)
            if r.status_code == 200:
                d = (r.json().get("data") or {})
                content = d.get("content") or ""
                status = str(d.get("status") or "").lower()
                if content.strip():
                    return len(content), waited
                if status in ("completed", "failed", "error"):
                    return len(content), waited
        except Exception:
            pass
        waited = True
        time.sleep(2)
    return -1, waited


def add_openwebui(session, path):
    with open(path, "rb") as f:
        r = session.post(f"{BASE_URL}/api/v1/files/",
                         files={"file": (path.name, f)}, timeout=300)
    r.raise_for_status()
    file_id = r.json().get("id")
    if TARGET and file_id:
        # Don't attach until the server has actually extracted the text.
        n_chars, waited = _wait_for_extraction(session, file_id)
        if waited:
            if n_chars > 0:
                print(f"      (waited for text extraction: {n_chars} chars)")
            elif n_chars == 0:
                print("      (server extracted no text from this file)")
            else:
                print("      (timed out waiting for text extraction — attaching anyway)")
        # Text extraction can lag behind the upload for large files, so a
        # "content is empty" 400 may just mean processing isn't finished.
        # Retry the attach for a while before giving up.
        attempts = 15          # ~45s total
        for i in range(attempts):
            resp = session.post(f"{BASE_URL}/api/v1/knowledge/{TARGET}/file/add",
                                json={"file_id": file_id}, timeout=300)
            if resp.status_code == 200:
                return file_id
            low = resp.text.lower()
            if resp.status_code == 400 and "empty" in low and i < attempts - 1:
                if i == 0:
                    print("      (waiting for the server to finish extracting text…)")
                time.sleep(3)
                continue
            # A "duplicate" seen on a RETRY means our own earlier attempt already
            # attached the file — treat it as success, and never delete it.
            if resp.status_code == 400 and "duplicate" in low and i > 0:
                return file_id
            # Permanent failure: delete the just-uploaded file so it doesn't
            # linger on the server as an orphan, then surface the error.
            # (Not for duplicates: the twin may be a file we legitimately added.)
            if "duplicate" not in low:
                try:
                    session.delete(f"{BASE_URL}/api/v1/files/{file_id}", timeout=60)
                except Exception:
                    pass
            resp.raise_for_status()   # a different error, or retries exhausted
    return file_id


def remove_openwebui(session, remote_id):
    if not remote_id:
        return
    # detach from the knowledge collection, then delete the file object
    if TARGET:
        session.post(f"{BASE_URL}/api/v1/knowledge/{TARGET}/file/remove",
                     json={"file_id": remote_id}, timeout=300)
    session.delete(f"{BASE_URL}/api/v1/files/{remote_id}", timeout=300)


ADAPTERS = {
    "anythingllm": (add_anythingllm, remove_anythingllm),
    "openwebui":   (add_openwebui,   remove_openwebui),
}


def make_ocr_copy(src: pathlib.Path):
    """OCR a PDF locally into a temp copy (same filename). Returns the Path, or
    None if OCR isn't possible. Caller removes the temp dir when done."""
    if src.suffix.lower() != ".pdf":
        return None
    if shutil.which("ocrmypdf") is None:
        print("[sync]     OCR fallback: 'ocrmypdf' not installed "
              "(sudo apt install ocrmypdf) — skipping.")
        return None
    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="rag_ocr_"))
    out = tmpdir / src.name
    print(f"[sync]     OCR fallback: running ocrmypdf on {src.name} …")
    try:
        subprocess.run(["ocrmypdf", "--force-ocr", "--quiet", str(src), str(out)],
                       check=True, timeout=1800)
        return out
    except Exception as e:
        print(f"[sync]     OCR fallback failed: {e}")
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None


PAGE_PROMPT = (
    "You are examining one page of a scientific paper. If it contains any "
    "figures, plots, charts, graphs, diagrams, or images, describe each in "
    "detail: caption/title, what is plotted, the axis labels and units, the "
    "series or curves shown, notable trends, and any clearly legible numeric "
    "values or ranges. Do NOT invent precise numbers you cannot actually read "
    "from the image. If the page has no figures (only body text, references, "
    "tables of text, etc.), reply with exactly: NO_FIGURES"
)

IMAGE_PROMPT = (
    "Describe this image in detail. If it is a figure, plot, chart, graph, or "
    "diagram, include the caption/title, the axis labels and units, the series "
    "or curves shown, notable trends, and any clearly legible numeric values or "
    "ranges. If it is a photo or other image, describe its content. Do NOT invent "
    "precise numbers you cannot actually read from the image."
)


def _vlm_describe(session, png_b64, prompt):
    """Ask the local vision model (Ollama) to describe one image."""
    r = session.post(f"{OLLAMA_URL}/api/generate",
                     json={"model": FIGURE_MODEL, "prompt": prompt,
                           "images": [png_b64], "stream": False},
                     timeout=600)
    r.raise_for_status()
    return r.json().get("response", "").strip()


# sidecar text files that may sit next to an image and hold its caption/metadata
SIDECAR_EXTS = (".txt", ".md", ".caption", ".json")


def find_sidecar_text(img_path):
    """Return (text, name) from a sidecar file sharing the image's basename, e.g.
    figure1.png + figure1.txt  OR  figure1.png.txt. Empty strings if none."""
    candidates = []
    for e in SIDECAR_EXTS:
        candidates.append(img_path.with_suffix(e))                  # figure1.txt
        candidates.append(img_path.parent / (img_path.name + e))    # figure1.png.txt
    for c in candidates:
        if c.is_file():
            try:
                txt = c.read_text(errors="ignore").strip()
                if txt:
                    return txt[:4000], c.name
            except Exception:
                pass
    return "", ""


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def preflight_text_warning(path):
    """Heuristic heads-up (never blocks): return a warning string if the file
    looks like it has little extractable text — a JavaScript-shell HTML, a
    scanned/no-text-layer PDF, or a near-empty doc. Else ''."""
    if not PREFLIGHT:
        return ""
    ext = path.suffix.lower()
    try:
        if ext in (".html", ".htm"):
            raw = path.read_text(errors="ignore")
            text = _WS_RE.sub(" ", _TAG_RE.sub(" ", raw)).strip()
            shell = ("enable javascript" in raw.lower() or "please enable" in raw.lower())
            if len(text) < MIN_TEXT_CHARS or (shell and len(text) < 3000):
                extra = " + JavaScript-shell markers" if shell else ""
                return (f"low extractable text (~{len(text)} chars{extra}) — may be a "
                        "saved shell page, not the article; prefer the PDF or enable Tika")
        elif ext == ".pdf":
            try:
                import fitz
            except ImportError:
                return ""
            d = fitz.open(str(path))
            chars = sum(len(d[i].get_text("text").strip()) for i in range(min(3, d.page_count)))
            d.close()
            if chars < 100:
                return (f"almost no text in the first pages (~{chars} chars) — likely "
                        "scanned / no text layer; try --ocr-fallback")
        elif ext in (".txt", ".md", ".csv", ".rtf"):
            if path.stat().st_size < 40:
                return "file is nearly empty"
    except Exception:
        return ""
    return ""


def build_figures_doc(session, pdf_path):
    """Render each page of a PDF and have the vision model describe any figures.
    Writes the descriptions to a temp .md file. Returns (md_path, n_pages_with_figures)
    or (None, 0) if nothing found / unavailable. Caller removes md_path.parent."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        print("[sync]   --describe-figures needs PyMuPDF (pip install pymupdf) — skipping.")
        return None, 0
    try:
        doc = fitz.open(str(pdf_path))
    except Exception as e:
        print(f"[sync]   could not open {pdf_path.name} for figure rendering: {e}")
        return None, 0

    print(f"[sync]   describing figures in {pdf_path.name} ({doc.page_count} pages, "
          f"model={FIGURE_MODEL})…")
    sections = []
    for i in range(doc.page_count):
        try:
            pix = doc[i].get_pixmap(dpi=FIGURE_DPI)
            b64 = base64.b64encode(pix.tobytes("png")).decode()
            desc = _vlm_describe(session, b64, PAGE_PROMPT)
        except requests.HTTPError as e:
            print(f"[sync]   vision model call failed ({e}). Is '{FIGURE_MODEL}' pulled? "
                  f"-> ollama pull {FIGURE_MODEL}")
            doc.close()
            return None, 0
        except Exception as e:
            print(f"[sync]   figure render/describe error on page {i + 1}: {e}")
            continue
        if desc and "NO_FIGURES" not in desc.upper():
            sections.append(f"## {pdf_path.name} — page {i + 1}\n\n{desc}\n")
    doc.close()

    if not sections:
        return None, 0
    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="rag_fig_"))
    out = tmpdir / f"{pdf_path.stem}_figures.md"
    # include a source-unique id so two companion docs can never collide on
    # Open WebUI's content-hash dedup (which would flag a real figure as duplicate)
    header = (f"# Figure descriptions for {pdf_path.name}\n\n"
              f"Source file: `{pdf_path.name}` (content-id `{file_hash(pdf_path)}`)\n\n"
              "Auto-generated visual descriptions of figures/plots (via a vision "
              "model). Numeric values are approximate — verify against the source "
              "figure before relying on them.\n\n")
    out.write_text(header + "\n".join(sections))
    return out, len(sections)


def attach_figures(session, src_pdf, key, files, add_fn):
    """If enabled, build figure descriptions for a PDF and upload them as a
    companion document, recording its remote id under files[key]['figures_id']."""
    if not DESCRIBE_FIGURES or src_pdf.suffix.lower() != ".pdf":
        return
    md_path, n = build_figures_doc(session, src_pdf)
    if not md_path:
        return
    try:
        fig_id = add_fn(session, md_path)
        files[key]["figures_id"] = fig_id
        print(f"[sync]   + figure descriptions added ({n} page(s) with figures)")
    except requests.HTTPError as e:
        print(f"[sync]   figure-description upload failed ({e})")
    finally:
        shutil.rmtree(md_path.parent, ignore_errors=True)


def build_image_doc(session, img_path):
    """Describe a standalone image file with the vision model and write the
    description to a temp .md. Returns the md Path, or None. Caller removes
    md_path.parent."""
    try:
        import fitz  # PyMuPDF also opens image files
    except ImportError:
        print("[sync]   indexing images needs PyMuPDF (pip install pymupdf) — skipping.")
        return None
    try:
        doc = fitz.open(str(img_path))
        b64 = base64.b64encode(doc[0].get_pixmap().tobytes("png")).decode()
        doc.close()
        # standalone images often lack a caption; the file name is frequently the
        # only context, so feed it to the model as a hint.
        prompt = (f"{IMAGE_PROMPT}\n\nContext: this image's file name is "
                  f"\"{img_path.name}\", which often hints at its subject or the "
                  f"quantities shown. Use it as a clue where relevant, but do not "
                  f"contradict what you actually see in the image.")
        # if a sidecar text/caption file sits next to the image, add it as context
        sidecar, sc_name = find_sidecar_text(img_path)
        if sidecar:
            print(f"[sync]   using sidecar context from {sc_name}")
            prompt += (f"\n\nAdditional context from an accompanying file "
                       f"(\"{sc_name}\"):\n{sidecar}\n\nUse it to inform your "
                       f"description where relevant.")
        desc = _vlm_describe(session, b64, prompt)
    except requests.HTTPError as e:
        print(f"[sync]   vision model call failed ({e}). Is '{FIGURE_MODEL}' pulled? "
              f"-> ollama pull {FIGURE_MODEL}")
        return None
    except Exception as e:
        print(f"[sync]   could not process image {img_path.name}: {e}")
        return None
    if not desc:
        return None
    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="rag_img_"))
    out = tmpdir / f"{img_path.stem}_image.md"
    # include a source-unique id so two image-description docs can never collide
    # on Open WebUI's content-hash dedup (which would flag a real figure as duplicate)
    header = (f"# Description of image {img_path.name}\n\n"
              f"Source file: `{img_path.name}` (content-id `{file_hash(img_path)}`)\n\n"
              "Auto-generated description of a standalone image (via a vision "
              "model). Numeric values are approximate — verify against the source "
              "image before relying on them.\n\n")
    out.write_text(header + desc + "\n")
    return out


def main():
    if not API_KEY:
        die("RAG_API_KEY is not set")
    if BACKEND not in ADAPTERS:
        die(f"RAG_BACKEND must be 'anythingllm' or 'openwebui', got '{BACKEND}'")
    if not WATCH_DIR.is_dir():
        die(f"watch dir does not exist: {WATCH_DIR}")
    if not TARGET:
        print("[sync] WARNING: RAG_TARGET not set — files upload but won't be "
              "attached to a workspace/collection (and can't be pruned).")

    add_fn, remove_fn = ADAPTERS[BACKEND]
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {API_KEY}"})

    state = load_state()
    # if the backend/target changed, stored remote ids no longer apply
    if state.get("backend") not in (None, BACKEND) or state.get("target") not in (None, TARGET):
        print("[sync] backend/target changed since last run — starting a fresh state "
              "(old remote ids can't be reused).")
        state = {"files": {}}
    state["backend"], state["target"] = BACKEND, TARGET
    files = state.setdefault("files", {})

    added = updated = removed = dup = 0
    failed = []   # names of files that couldn't be added

    # --- add / update files currently on disk ---
    on_disk = set()
    for p in sorted(WATCH_DIR.rglob("*")):
        ext = p.suffix.lower()
        if not p.is_file() or ext not in (EXTS | IMAGE_EXTS):
            continue
        key = str(p)
        on_disk.add(key)
        digest = file_hash(p)
        entry = files.get(key)
        if entry and entry.get("hash") == digest and not FORCE:
            continue  # unchanged (use --force to re-sync anyway)
        is_update = bool(entry and entry.get("remote_id"))

        # --- standalone image files: index a vision description of the image ---
        if ext in IMAGE_EXTS:
            if not DESCRIBE_FIGURES:
                print(f"[sync] skipping image {p.name} "
                      "(run with --describe-figures to index images)")
                continue
            print(f"[sync] {'updating' if is_update else 'adding'} image {p.name} …")
            try:
                if is_update:
                    remove_fn(session, entry["remote_id"])
                md = build_image_doc(session, p)
                if not md:
                    failed.append(p.name)
                    continue
                try:
                    remote_id = add_fn(session, md)
                    files[key] = {"hash": digest, "remote_id": remote_id}
                finally:
                    shutil.rmtree(md.parent, ignore_errors=True)
                updated += is_update
                added += not is_update
                print("[sync]   + image description added")
            except requests.HTTPError as e:
                detail = ""
                try:
                    detail = (e.response.text or "")[:400]
                except Exception:
                    pass
                if "duplicate" in detail.lower():
                    files[key] = {"hash": digest,
                                  "remote_id": (entry.get("remote_id") if entry else None)}
                    print("[sync]   image description already in collection — recorded, will skip next run")
                    dup += 1
                    continue
                print(f"[sync]   FAILED: {p.name}")
                if detail:
                    print(f"[sync]     server said: {detail}")
                failed.append(p.name)
            continue

        # --- document files (pdf / text) ---
        print(f"[sync] {'updating' if is_update else 'adding'} {p.name} …")
        pf = preflight_text_warning(p)
        if pf:
            print(f"[sync]   ! {pf}")
        try:
            if is_update:
                # changed file: remove the old copy (and its figure doc) first
                remove_fn(session, entry["remote_id"])
                if entry.get("figures_id"):
                    remove_fn(session, entry["figures_id"])
            remote_id = add_fn(session, p)
            files[key] = {"hash": digest, "remote_id": remote_id}
            updated += is_update
            added += not is_update
            attach_figures(session, p, key, files, add_fn)
        except requests.HTTPError as e:
            detail = ""
            try:
                detail = (e.response.text or "")[:400]
            except Exception:
                pass
            low = detail.lower()
            empty_content = ("empty" in low and "content" in low)

            # Already in the collection: Open WebUI dedupes by content. Not an
            # error — record it so we stop retrying. (No in-KB id is returned, so
            # prune/force can't manage it until a clean re-sync of the collection.)
            if "duplicate" in low:
                files[key] = {"hash": digest,
                              "remote_id": (entry.get("remote_id") if entry else None)}
                print(f"[sync]   already in collection (duplicate content) — recorded, will skip next run")
                dup += 1
                continue

            # Auto-recovery: OCR the PDF locally and retry once (opt-in).
            if empty_content and OCR_FALLBACK and p.suffix.lower() == ".pdf":
                ocr_path = make_ocr_copy(p)
                if ocr_path:
                    try:
                        remote_id = add_fn(session, ocr_path)
                        files[key] = {"hash": digest, "remote_id": remote_id}
                        added += not is_update
                        updated += is_update
                        print(f"[sync]   recovered via OCR: {p.name}")
                        shutil.rmtree(ocr_path.parent, ignore_errors=True)
                        attach_figures(session, p, key, files, add_fn)
                        continue
                    except requests.HTTPError as e2:
                        shutil.rmtree(ocr_path.parent, ignore_errors=True)
                        print(f"[sync]   OCR retry still failed ({e2}) — the extraction "
                              "engine itself may be broken (e.g. Tika not running).")

            print(f"[sync]   FAILED: {p.name}")
            if detail:
                print(f"[sync]     server said: {detail}")
            if empty_content:
                print("[sync]     hint: the server extracted no text. Common causes:")
                print("[sync]       - extraction engine set to Tika/Docling but that service")
                print("[sync]         isn't running -> revert to Default (or start Tika), in")
                print("[sync]         Admin > Settings > Documents.")
                print("[sync]       - the PDF has no text layer -> re-run with --ocr-fallback")
                print("[sync]         (auto-OCRs and retries), or OCR manually with ocrmypdf.")
            elif e.response is not None and e.response.status_code in (401, 403):
                print("[sync]     hint: auth rejected -> check the API key (use the sk- key,")
                print("[sync]       not the JWT token) in ~/.rag_sync_key.")
            failed.append(p.name)

    # --- prune files deleted from the folder ---
    if PRUNE:
        for key in [k for k in files if k not in on_disk]:
            name = pathlib.Path(key).name
            try:
                print(f"[sync] removing {name} (deleted from folder) …")
                remove_fn(session, files[key].get("remote_id"))
                if files[key].get("figures_id"):
                    remove_fn(session, files[key]["figures_id"])
                del files[key]
                removed += 1
            except requests.HTTPError as e:
                detail = ""
                try:
                    detail = (e.response.text or "")[:400]
                except Exception:
                    pass
                print(f"[sync]   remove failed ({e}) {('- ' + detail) if detail else ''}")
    else:
        stale = [k for k in files if k not in on_disk]
        if stale:
            print(f"[sync] {len(stale)} file(s) gone from the folder but kept in the "
                  f"collection. Run with --prune to remove them.")

    save_state(state)
    print(f"[sync] done — {added} added, {updated} updated, {removed} removed, "
          f"{dup} already-present, {len(files)} tracked total"
          f"{' (prune ON)' if PRUNE else ''}.")
    if dup:
        print(f"[sync] {dup} file(s) were already in the collection (recorded, won't retry). "
              f"If that's unexpected, the collection and state drifted — empty the collection "
              f"and re-sync for a clean rebuild.")

    if failed:
        print(f"[sync] {len(failed)} file(s) FAILED and were not added:")
        for n in failed:
            print(f"         - {n}")
        print("[sync] these aren't tracked, so they'll retry on the next run once fixed.")
        sys.exit(1)   # non-zero so cron / scripts can detect a problem


if __name__ == "__main__":
    main()
