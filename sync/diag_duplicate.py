#!/usr/bin/env python3
"""
diag_duplicate.py — find out WHY Open WebUI calls a file "duplicate content".

Uploads one file, reads back the text Open WebUI actually extracted from it,
attempts the knowledge/file/add, and — if rejected as a duplicate — searches
every stored file for the one whose extracted content matches. Cleans up after
itself (the test upload is deleted).

Usage:
    python3 diag_duplicate.py "/path/to/the/file/that/was/flagged.md"
    python3 diag_duplicate.py "/path/to/file.pdf" --keep     # don't delete the upload
"""

import os
import sys
import hashlib
import pathlib
import requests

BASE = os.environ.get("RAG_BASE_URL", "http://localhost:3000")
TARGET = os.environ.get("RAG_TARGET", "c411c9dc-289a-4e4c-bfa9-c5fab84d22c6")
_KEY_FILE = pathlib.Path(os.environ.get(
    "RAG_KEY_FILE", str(pathlib.Path.home() / ".rag_sync_key"))).expanduser()
KEY = os.environ.get("RAG_API_KEY") or _KEY_FILE.read_text().strip()
H = {"Authorization": f"Bearer {KEY}"}


def norm(s):
    return " ".join((s or "").split())


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    keep = "--keep" in sys.argv
    if not args:
        sys.exit("give me the path of a file that was flagged as duplicate")
    p = pathlib.Path(args[0]).expanduser()
    if not p.is_file():
        sys.exit(f"not found: {p}")

    s = requests.Session()
    s.headers.update(H)

    # ---- 1. snapshot what is stored now -------------------------------
    items = s.get(f"{BASE}/api/v1/files/", timeout=60).json()["items"]
    print(f"stored files before upload: {len(items)}")
    by_hash, by_raw = {}, {}
    for f in items:
        c = ((f.get("data") or {}).get("content") or "")
        by_hash.setdefault(hashlib.md5(norm(c).encode()).hexdigest(), []).append(f["filename"])
        by_raw.setdefault(f.get("hash") or "", []).append(f["filename"])

    # ---- 2. upload the suspect file -----------------------------------
    with open(p, "rb") as fh:
        r = s.post(f"{BASE}/api/v1/files/", files={"file": (p.name, fh)}, timeout=300)
    print(f"upload HTTP {r.status_code}")
    r.raise_for_status()
    up = r.json()
    fid = up.get("id")
    content = ((up.get("data") or {}).get("content") or "")
    raw_hash = up.get("hash") or ""
    print(f"file_id       : {fid}")
    print(f"file hash     : {raw_hash[:32]}…")
    print(f"extracted len : {len(content)} chars")
    print(f"extract head  : {norm(content)[:300]!r}")

    ch = hashlib.md5(norm(content).encode()).hexdigest()

    # ---- 3. who does it collide with? ---------------------------------
    print("\n--- collision analysis ---")
    same_text = by_hash.get(ch)
    print(f"stored file with IDENTICAL extracted text : {same_text}")
    same_bytes = by_raw.get(raw_hash)
    print(f"stored file with IDENTICAL file hash      : {same_bytes}")

    # ---- 4. try the knowledge add, show the exact error ----------------
    a = s.post(f"{BASE}/api/v1/knowledge/{TARGET}/file/add",
               json={"file_id": fid}, timeout=300)
    print(f"\nknowledge/file/add -> HTTP {a.status_code}: {a.text[:300]}")

    # ---- 5. cleanup -----------------------------------------------------
    if not keep:
        d = s.delete(f"{BASE}/api/v1/files/{fid}", timeout=60)
        print(f"cleanup delete -> HTTP {d.status_code}")
    else:
        print("(--keep: upload left in place)")


if __name__ == "__main__":
    main()
