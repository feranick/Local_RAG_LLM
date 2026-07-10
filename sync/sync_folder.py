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
  RAG_OCR_FALLBACK "1"/"true" to auto-OCR a PDF (ocrmypdf) and retry when the
                  server extracts no text. Same as passing --ocr-fallback.

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
"""

import os
import sys
import json
import time
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

# ocr fallback = if a PDF fails because the server extracted no text, OCR it
# locally (ocrmypdf) and retry once. enabled by --ocr-fallback or RAG_OCR_FALLBACK=1.
# Requires the `ocrmypdf` command to be installed (sudo apt install ocrmypdf).
OCR_FALLBACK = ("--ocr-fallback" in sys.argv) or (os.environ.get("RAG_OCR_FALLBACK", "").lower() in ("1", "true", "yes"))

DEFAULT_URLS = {
    "anythingllm": "http://localhost:3001",
    "openwebui":   "http://localhost:3000",
}
BASE_URL = os.environ.get("RAG_BASE_URL", DEFAULT_URLS.get(BACKEND, ""))

# file types worth embedding
EXTS = {".pdf", ".txt", ".md", ".docx", ".doc", ".epub", ".csv", ".html", ".rtf", ".pptx"}

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


def add_openwebui(session, path):
    with open(path, "rb") as f:
        r = session.post(f"{BASE_URL}/api/v1/files/",
                         files={"file": (path.name, f)}, timeout=300)
    r.raise_for_status()
    file_id = r.json().get("id")
    if TARGET and file_id:
        # Text extraction can lag behind the upload for large files, so a
        # "content is empty" 400 may just mean processing isn't finished.
        # Retry the attach for a while before giving up.
        attempts = 15          # ~45s total
        for i in range(attempts):
            resp = session.post(f"{BASE_URL}/api/v1/knowledge/{TARGET}/file/add",
                                json={"file_id": file_id}, timeout=300)
            if resp.status_code == 200:
                return file_id
            if resp.status_code == 400 and "empty" in resp.text.lower() and i < attempts - 1:
                if i == 0:
                    print("      (waiting for the server to finish extracting text…)")
                time.sleep(3)
                continue
            # Permanent failure: delete the just-uploaded file so it doesn't
            # linger on the server as an orphan, then surface the error.
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

    added = updated = removed = 0
    failed = []   # (name, reason) for files that couldn't be added

    # --- add / update files currently on disk ---
    on_disk = set()
    for p in sorted(WATCH_DIR.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in EXTS:
            continue
        key = str(p)
        on_disk.add(key)
        digest = file_hash(p)
        entry = files.get(key)
        if entry and entry.get("hash") == digest:
            continue  # unchanged
        is_update = bool(entry and entry.get("remote_id"))
        print(f"[sync] {'updating' if is_update else 'adding'} {p.name} …")
        try:
            if is_update:
                # changed file: remove the old copy first to avoid duplicates
                remove_fn(session, entry["remote_id"])
            remote_id = add_fn(session, p)
            files[key] = {"hash": digest, "remote_id": remote_id}
            updated += is_update
            added += not is_update
        except requests.HTTPError as e:
            detail = ""
            try:
                detail = (e.response.text or "")[:400]
            except Exception:
                pass
            low = detail.lower()
            empty_content = ("empty" in low and "content" in low)

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
          f"{len(files)} tracked total"
          f"{' (prune ON)' if PRUNE else ''}.")

    if failed:
        print(f"[sync] {len(failed)} file(s) FAILED and were not added:")
        for n in failed:
            print(f"         - {n}")
        print("[sync] these aren't tracked, so they'll retry on the next run once fixed.")
        sys.exit(1)   # non-zero so cron / scripts can detect a problem


if __name__ == "__main__":
    main()
