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

Configure with environment variables (or edit the defaults below):

  RAG_BACKEND     anythingllm | openwebui        (default: anythingllm)
  RAG_API_KEY     API key from the tool's UI     (required)
  RAG_WATCH_DIR   folder to sync                  (default: ~/papers)
  RAG_BASE_URL    override the default base URL
  RAG_TARGET      AnythingLLM workspace slug  OR  Open WebUI knowledge id
  RAG_PRUNE       "1"/"true" to delete from the collection when a file is
                  removed from the folder (makes the folder the source of truth)

Get an API key:
  AnythingLLM -> Settings > Tools > Developer API
  Open WebUI  -> Admin Panel > Settings > Authentication > Enable API Key,
                 then Settings > Account > create key (starts with sk-)

Endpoint names shift between versions; check each tool's /docs if a call fails
(Open WebUI: http://localhost:3000/docs).

Usage:
  pip install requests
  RAG_API_KEY=xxxx RAG_TARGET=papers python3 sync_folder.py            # add/update only
  RAG_API_KEY=xxxx RAG_TARGET=papers python3 sync_folder.py --prune    # full mirror
"""

import os
import sys
import json
import hashlib
import pathlib

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

# ----------------------------- config -----------------------------------
BACKEND   = os.environ.get("RAG_BACKEND", "anythingllm").lower()
API_KEY   = os.environ.get("RAG_API_KEY", "")
WATCH_DIR = pathlib.Path(os.environ.get("RAG_WATCH_DIR", str(pathlib.Path.home() / "papers")))
TARGET    = os.environ.get("RAG_TARGET", "")   # workspace slug (AnythingLLM) / knowledge id (Open WebUI)

# prune = delete from the collection when the file disappears from the folder.
# enabled by --prune on the command line or RAG_PRUNE=1 in the environment.
PRUNE = ("--prune" in sys.argv) or (os.environ.get("RAG_PRUNE", "").lower() in ("1", "true", "yes"))

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
        session.post(f"{BASE_URL}/api/v1/knowledge/{TARGET}/file/add",
                     json={"file_id": file_id}, timeout=300).raise_for_status()
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
        try:
            if entry and entry.get("remote_id"):
                # changed file: remove the old copy first to avoid duplicates
                print(f"[sync] updating {p.name} …")
                remove_fn(session, entry["remote_id"])
                updated += 1
            else:
                print(f"[sync] adding {p.name} …")
                added += 1
            remote_id = add_fn(session, p)
            files[key] = {"hash": digest, "remote_id": remote_id}
        except requests.HTTPError as e:
            print(f"[sync]   failed ({e}) — check API key / endpoint / target")

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
                print(f"[sync]   remove failed ({e})")
    else:
        stale = [k for k in files if k not in on_disk]
        if stale:
            print(f"[sync] {len(stale)} file(s) gone from the folder but kept in the "
                  f"collection. Run with --prune to remove them.")

    save_state(state)
    print(f"[sync] done — {added} added, {updated} updated, {removed} removed, "
          f"{len(files)} tracked total"
          f"{' (prune ON)' if PRUNE else ''}.")


if __name__ == "__main__":
    main()
