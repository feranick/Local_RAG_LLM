#!/usr/bin/env python3
"""
sync_folder.py — keep a RAG knowledge base in sync with a local folder.

Detects new/changed files in WATCH_DIR and uploads + embeds them into either
AnythingLLM or Open WebUI. Tracks a content hash per file so unchanged files
are skipped on subsequent runs. Designed to be run repeatedly (cron/systemd).

Configure with environment variables (or edit the defaults below):

  RAG_BACKEND     anythingllm | openwebui        (default: anythingllm)
  RAG_API_KEY     API key from the tool's UI     (required)
  RAG_WATCH_DIR   folder to sync                  (default: ~/papers)
  RAG_BASE_URL    override the default base URL
  RAG_TARGET      AnythingLLM workspace slug  OR  Open WebUI knowledge id

Get an API key:
  AnythingLLM -> Settings > Tools > Developer API
  Open WebUI  -> Settings > Account > API Keys

Endpoint names shift between versions; check each tool's /docs if a call fails
(Open WebUI: http://localhost:3000/docs).

Usage:
  pip install requests
  RAG_API_KEY=xxxx RAG_TARGET=papers python3 sync_folder.py
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
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def file_hash(p: pathlib.Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def upload_anythingllm(session: requests.Session, path: pathlib.Path):
    """Upload a document, then embed it into the target workspace."""
    with open(path, "rb") as f:
        r = session.post(f"{BASE_URL}/api/v1/document/upload",
                         files={"file": (path.name, f)}, timeout=300)
    r.raise_for_status()
    # pull the stored document location from the response
    docs = r.json().get("documents", [])
    location = docs[0]["location"] if docs else None
    if TARGET and location:
        session.post(f"{BASE_URL}/api/v1/workspace/{TARGET}/update-embeddings",
                     json={"adds": [location]}, timeout=300).raise_for_status()


def upload_openwebui(session: requests.Session, path: pathlib.Path):
    """Upload a file, then attach it to the target knowledge collection."""
    with open(path, "rb") as f:
        r = session.post(f"{BASE_URL}/api/v1/files/",
                         files={"file": (path.name, f)}, timeout=300)
    r.raise_for_status()
    file_id = r.json().get("id")
    if TARGET and file_id:
        session.post(f"{BASE_URL}/api/v1/knowledge/{TARGET}/file/add",
                     json={"file_id": file_id}, timeout=300).raise_for_status()


def main():
    if not API_KEY:
        die("RAG_API_KEY is not set")
    if BACKEND not in ("anythingllm", "openwebui"):
        die(f"RAG_BACKEND must be 'anythingllm' or 'openwebui', got '{BACKEND}'")
    if not WATCH_DIR.is_dir():
        die(f"watch dir does not exist: {WATCH_DIR}")
    if not TARGET:
        print("[sync] WARNING: RAG_TARGET not set — files upload but won't be "
              "attached to a workspace/collection.")

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {API_KEY}"})
    upload = upload_anythingllm if BACKEND == "anythingllm" else upload_openwebui

    state = load_state()
    changed = 0
    for p in sorted(WATCH_DIR.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in EXTS:
            continue
        digest = file_hash(p)
        if state.get(str(p)) == digest:
            continue  # unchanged
        try:
            print(f"[sync] uploading {p.name} …")
            upload(session, p)
            state[str(p)] = digest
            changed += 1
        except requests.HTTPError as e:
            print(f"[sync]   failed ({e}) — check API key / endpoint / target")
    save_state(state)
    print(f"[sync] done — {changed} file(s) added/updated, "
          f"{len(state)} tracked total.")


if __name__ == "__main__":
    main()
