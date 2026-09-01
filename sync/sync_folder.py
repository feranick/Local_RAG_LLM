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

Configuration lives in a CONFIG FILE (sync_folder.conf) — nothing in this file
needs editing, so upgrading the script never means re-customising it. Run
`--init-config` to create a starter file. Every setting may also be given as an
environment variable, which overrides the config file:

  RAG_BACKEND     anythingllm | openwebui        (default: openwebui)
  RAG_API_KEY     API key from the tool's UI     (or use the key file, below)
  RAG_KEY_FILE    path to a file holding the key (default: ~/.rag_sync_key)
  RAG_STATE_FILE  per-library sync state (default: ~/.rag_sync_state.json).
                  REQUIRED when you sync more than one folder/collection — give
                  each library its own file or they clobber each other.
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
  RAG_FIGURE_MODEL  vision model tag (default: llava). A vision-capable CHAT model
                  (qwen3.8:27b, gemma4:31b) usually captions better AND avoids
                  keeping a second model in memory — it is probably already loaded
                  for chat. The run prints the model's capabilities and stops early
                  if it cannot see. NOTE: some Ollama builds do not support the
                  'mllama' architecture, so llama3.2-vision may not load; llava does.
  RAG_FIGURE_TEMPERATURE  sampling temperature for captions (default 0.2)
  RAG_FIGURE_THINK  "1"/"true" to let a thinking model reason before captioning
                  (default off — it multiplies the time per image and the reasoning
                  is thrown away)
  RAG_FIGURE_KEEP_ALIVE  how long Ollama keeps the figure model loaded, e.g. "30m".
                  Worth setting when it is also your chat model.
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
  python3 sync_folder.py --init-config          # then edit sync_folder.conf
  python3 sync_folder.py                       # add/update only
  python3 sync_folder.py --prune               # full mirror (also deletes)
  python3 sync_folder.py --ocr-fallback        # auto-OCR PDFs that extract empty
  python3 sync_folder.py --describe-figures    # also index vision descriptions of plots
  python3 sync_folder.py --force               # re-sync everything (re-embed), no duplicates
  python3 sync_folder.py --status               # how far along? (safe during a run)
  python3 sync_folder.py --recaption            # redo existing figure/image captions
                                                # with the current FIGURE_MODEL only:
                                                # documents are NOT re-embedded
  python3 sync_folder.py --recaption --limit 5  # try five first, to time the change
"""

__version__ = "2026.08.13.1"

import os
import re
import sys
import json
import time
import atexit
import base64
import signal
import shutil
import hashlib
import pathlib
import tempfile
import collections
import subprocess

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

# Keep output line-buffered even when redirected to a file. Python block-buffers
# (8 KB) when stdout isn't a terminal, so under `nohup … > sync.log` the log would
# sit empty for a hundred lines at a time and look like a hung run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(line_buffering=True)
    except Exception:                      # pragma: no cover  (non-TextIO stream)
        pass


def _stop_cleanly(signum, frame):
    """Turn a stop signal into the normal interrupt path, so state is saved."""
    raise KeyboardInterrupt


# `kill <pid>` sends SIGTERM, whose default action kills the process outright —
# no exit handlers, so the state file would lose everything since the last
# checkpoint. Handle it like Ctrl-C instead.
signal.signal(signal.SIGTERM, _stop_cleanly)

# A shell sets SIGINT to *ignored* for background jobs, and the child inherits
# that: after `nohup … &`, `kill -INT` would be silently swallowed and the run
# would carry on. Restore normal interrupt behaviour when that has happened.
if signal.getsignal(signal.SIGINT) == signal.SIG_IGN:
    signal.signal(signal.SIGINT, _stop_cleanly)

# ============================ configuration ==============================
# NOTHING needs to be edited in this file. Settings come from a config file so
# that upgrading this script never means re-customising it.
#
# Resolution order for every setting (first hit wins):
#     1. environment variable   RAG_<NAME>        (handy for one-off overrides)
#     2. config file entry      <NAME> = value
#     3. the built-in default below
#
# The config file is looked for in this order:
#     --config <path>            (explicit)
#     $RAG_CONFIG                (environment)
#     ./sync_folder.conf         (the directory you run from)
#     <dir of this script>/sync_folder.conf
#     ~/.config/rag_sync/config
#
# Write a starter file with:   python3 sync_folder.py --init-config
# -------------------------------------------------------------------------

def _find_config():
    for i, arg in enumerate(sys.argv):
        if arg == "--config" and i + 1 < len(sys.argv):
            return pathlib.Path(sys.argv[i + 1]).expanduser()
        if arg.startswith("--config="):
            return pathlib.Path(arg.split("=", 1)[1]).expanduser()
    if os.environ.get("RAG_CONFIG"):
        return pathlib.Path(os.environ["RAG_CONFIG"]).expanduser()
    for cand in (pathlib.Path.cwd() / "sync_folder.conf",
                 pathlib.Path(__file__).resolve().parent / "sync_folder.conf",
                 pathlib.Path.home() / ".config" / "rag_sync" / "config"):
        if cand.is_file():
            return cand
    return None


def _load_config(path):
    """Parse a simple KEY = value file. '#'/';' comment lines and [sections] are
    ignored, quotes are stripped, and a leading RAG_ on a key is optional."""
    out = {}
    if not path or not path.is_file():
        return out
    for raw in path.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if not line or line[0] in "#;[" or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().upper()
        if k.startswith("RAG_"):
            k = k[4:]
        v = v.strip()
        if v[:1] in ('"', "'") and v.count(v[0]) >= 2:
            v = v[1:v.index(v[0], 1)]              # quoted: take it verbatim
        else:
            v = re.split(r"\s+[#;]", v, maxsplit=1)[0].strip()   # drop inline comment
        # allow ~ and $HOME in path-ish values
        if v.startswith("~") or v.startswith("$") or "/" in v:
            v = os.path.expandvars(os.path.expanduser(v))
        out[k] = v
    return out


CONFIG_PATH = _find_config()
CONFIG = _load_config(CONFIG_PATH)
_TRUE = ("1", "true", "yes", "on")


def cfg(name, default=None):
    """env RAG_<NAME>  >  config file <NAME>  >  default"""
    v = os.environ.get("RAG_" + name)
    if v is None or v == "":
        v = CONFIG.get(name)
    return default if v is None or v == "" else v


def cfg_flag(name, cli_on=None, cli_off=None, default=False):
    """A boolean. An explicit CLI flag always wins, then env, then config file."""
    if cli_on and cli_on in sys.argv:
        return True
    if cli_off and cli_off in sys.argv:
        return False
    v = cfg(name)
    return (str(v).lower() in _TRUE) if v is not None else default


def argv_int(flag, default=0):
    """Value of a numeric CLI flag, e.g. --limit 20."""
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            try:
                return int(sys.argv[i + 1])
            except ValueError:
                sys.exit(f"{flag} needs a number, got {sys.argv[i + 1]!r}")
        sys.exit(f"{flag} needs a number")
    return default


# Redo existing captions with the current FIGURE_MODEL, touching ONLY the caption
# docs. Separate from a normal run because --force would re-embed the whole library.
RECAPTION = "--recaption" in sys.argv
RECAPTION_LIMIT = argv_int("--limit", 0)

BACKEND   = str(cfg("BACKEND", "openwebui")).lower()
WATCH_DIR = pathlib.Path(cfg("WATCH_DIR", str(pathlib.Path.home() / "papers"))).expanduser()
TARGET    = cfg("TARGET", "")          # knowledge id (Open WebUI) / workspace slug

# API KEY — never store it in the config file if you can avoid it; point at a
# 0600 key file instead:  echo 'sk-xxxx' > ~/.rag_sync_key && chmod 600 ~/.rag_sync_key
KEY_FILE  = pathlib.Path(cfg("KEY_FILE", str(pathlib.Path.home() / ".rag_sync_key"))).expanduser()
API_KEY   = cfg("API_KEY", "")
if not API_KEY and KEY_FILE.is_file():
    API_KEY = KEY_FILE.read_text().strip()

# prune = also delete from the collection when a file disappears from the folder
PRUNE = cfg_flag("PRUNE", "--prune", "--no-prune")

# force = re-sync every file even if unchanged (old copy removed first)
FORCE = cfg_flag("FORCE", "--force", "--no-force")

# ocr fallback = OCR a PDF locally (ocrmypdf) and retry when the server got no text
OCR_FALLBACK = cfg_flag("OCR_FALLBACK", "--ocr-fallback", "--no-ocr-fallback")

# Convert pre-2007 .doc/.ppt/.xls to .docx/.pptx/.xlsx before uploading. ON by
# default: the server cannot read those formats at all, so without this every one
# of them fails. Cheap and local (LibreOffice, or antiword/catdoc for .doc).
CONVERT_LEGACY = cfg_flag("CONVERT_LEGACY", "--convert-legacy",
                          "--no-convert-legacy", default=True)

# describe figures = render pages/images and index a vision-model description
DESCRIBE_FIGURES = cfg_flag("DESCRIBE_FIGURES", "--describe-figures", "--no-describe-figures")
# A modern vision-capable CHAT model (qwen3.8:27b, gemma4:31b) is usually the better
# choice than a dedicated captioner like llava — better captions, and it is probably
# already resident for chat, so figure runs stop evicting it. Any model whose
# capabilities include "vision" works; the run reports what it found before starting.
FIGURE_MODEL = cfg("FIGURE_MODEL", "llava")
OLLAMA_URL   = cfg("OLLAMA_URL", "http://localhost:11434")
FIGURE_DPI   = int(cfg("FIGURE_DPI", 150))
# Thinking models spend tokens reasoning before answering. For a caption that is pure
# cost — often several times the latency, for output that gets discarded — so thinking
# is disabled unless you ask for it. Ignored by models without the capability.
FIGURE_THINK = cfg_flag("FIGURE_THINK", "--figure-think", "--no-figure-think", default=False)
# Low temperature for captions: the failure mode that matters here is a confidently
# invented axis label or number, and sampling is what produces those.
FIGURE_TEMPERATURE = float(cfg("FIGURE_TEMPERATURE", 0.2))
# Keep the model resident between images (e.g. "30m"). Worth setting when the figure
# model is also your chat model: the default 5-minute idle unload otherwise reloads
# ~18 GB whenever a slow page or a pause in the run exceeds it.
FIGURE_KEEP_ALIVE = cfg("FIGURE_KEEP_ALIVE", "")
# Context size for figure calls. Unlike `think` and `temperature` — which are per
# request and affect nothing else — num_ctx is a LOAD-time parameter: Ollama keys a
# loaded runner by it, so a chat preset pinned to 32768 and figure calls inheriting a
# different default make the same model load twice, or reload back and forth for the
# whole run. Set this to the SAME value your chat preset uses and one instance serves
# both. Empty = send nothing (fine if the preset doesn't pin num_ctx either).
FIGURE_NUM_CTX = cfg("FIGURE_NUM_CTX", "")

# preflight low-text warning (on by default; never blocks an upload)
PREFLIGHT = not cfg_flag("NO_PREFLIGHT", "--no-preflight", "--preflight")
MIN_TEXT_CHARS = int(cfg("MIN_TEXT_CHARS", 400))

# How long to let the server work on ONE attach/embed request. Embedding happens
# server-side on the shared GPU, so a big document — or any document while another
# library is syncing — can exceed the old 300 s. Raise this if you still see
# "the server is still embedding" messages.
ATTACH_TIMEOUT = int(cfg("ATTACH_TIMEOUT", 900))

# Print a "progress N/total, ~M min left" line every N files (0 = never).
PROGRESS_EVERY = int(cfg("PROGRESS_EVERY", 25))

DEFAULT_URLS = {
    "anythingllm": "http://localhost:3001",
    "openwebui":   "http://localhost:3000",
}
BASE_URL = cfg("BASE_URL", DEFAULT_URLS.get(BACKEND, ""))

# document types uploaded directly (text is extracted server-side)
EXTS = {
    # documents
    ".pdf", ".txt", ".md", ".rst", ".html", ".htm", ".rtf", ".epub",
    # Microsoft Office (modern + legacy)
    ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls",
    # OpenDocument
    ".odt", ".odp", ".ods",
    # tabular / data
    ".csv", ".tsv", ".json",
}

# standalone image types: indexed only with --describe-figures, by uploading a
# vision-model description of the image (the image itself has no extractable text).
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp", ".gif"}

# Per-library state. Give each library its OWN state file (RAG_STATE_FILE) when you
# sync more than one folder/collection, otherwise they overwrite each other's
# records and every run looks like a full re-sync.
STATE_FILE = pathlib.Path(cfg(
    "STATE_FILE", str(pathlib.Path.home() / ".rag_sync_state.json"))).expanduser()

# per-type tallies for the end-of-run summary (what actually made it in)
STATS = collections.Counter()
# -------------------------------------------------------------------------


def die(msg: str):
    sys.exit(f"[sync] ERROR: {msg}")


CONFIG_TEMPLATE = """# sync_folder.conf — configuration for sync_folder.py
#
# Keep this file next to the script (or in the directory you run from) so that
# updating sync_folder.py never overwrites your settings.
# Every entry may also be given as an environment variable: RAG_<NAME>.

# --- what to sync, and where to ------------------------------------------
BACKEND    = openwebui                 # openwebui | anythingllm
WATCH_DIR  = ~/papers                  # folder to sync
TARGET     =                           # knowledge collection id / workspace slug
BASE_URL   = http://localhost:3000     # the instance to sync into

# --- credentials ---------------------------------------------------------
KEY_FILE   = ~/.rag_sync_key           # file containing the sk-... key (chmod 600)
# API_KEY  =                           # (discouraged: prefer KEY_FILE)

# --- per-library state (MUST differ between libraries) -------------------
STATE_FILE = ~/.rag_sync_state.json

# --- behaviour (true/false; a CLI flag still overrides) ------------------
PRUNE            = false               # remove from collection when deleted locally
FORCE            = false               # re-sync everything even if unchanged
OCR_FALLBACK     = false               # OCR a PDF locally if the server got no text
CONVERT_LEGACY   = true                # convert .doc/.ppt/.xls before upload (needs
                                       # libreoffice, or antiword for .doc)
DESCRIBE_FIGURES = false               # index vision descriptions of figures/images
NO_PREFLIGHT     = false               # true = skip the low-text warning

# --- figure descriptions -------------------------------------------------
# A vision-capable CHAT model gives better captions than llava and is likely already
# loaded, so figure runs stop evicting it: FIGURE_MODEL = qwen3.8:27b
FIGURE_MODEL   = llava
OLLAMA_URL     = http://localhost:11434
FIGURE_DPI     = 150
FIGURE_TEMPERATURE = 0.2     # low: invented axis labels are the failure that matters
FIGURE_THINK   = false       # thinking costs latency and is discarded for a caption
                             # (per-request — your chats keep thinking as configured)
FIGURE_KEEP_ALIVE =          # e.g. 30m — keeps a big shared model resident
FIGURE_NUM_CTX =             # set to the SAME num_ctx as your chat preset, or the
                             # same model gets loaded twice (num_ctx is load-time)
MIN_TEXT_CHARS = 400

# --- server patience & progress ------------------------------------------
ATTACH_TIMEOUT = 900                   # seconds to let ONE embed request run
PROGRESS_EVERY = 25                    # progress/ETA line every N files (0 = off)
"""


def init_config():
    """Write a starter config file in the current directory."""
    target = pathlib.Path.cwd() / "sync_folder.conf"
    if target.exists():
        print(f"[sync] {target} already exists — not overwriting.")
        return
    target.write_text(CONFIG_TEMPLATE)
    print(f"[sync] wrote {target}\n"
          f"[sync] edit TARGET / WATCH_DIR / STATE_FILE, then run: "
          f"python3 {pathlib.Path(__file__).name}")


if "--init-config" in sys.argv:
    init_config()
    sys.exit(0)


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
    # create the parent directory if the configured STATE_FILE lives somewhere
    # that doesn't exist yet — otherwise a long run would fail at the very end
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    STATE_FILE.write_text(json.dumps(state, indent=2))


# --- crash/interrupt safety ----------------------------------------------
# A sync can run for hours. State used to be written only at the very end, so
# any crash (or Ctrl-C) threw away the whole session and the next run re-uploaded
# everything. Now progress is checkpointed periodically and on exit.
_LIVE_STATE = {}


def _save_on_exit():
    st = _LIVE_STATE.get("state")
    if st:
        try:
            save_state(st)
            print(f"[sync] progress saved to {STATE_FILE}")
        except Exception:
            pass


atexit.register(_save_on_exit)


def maybe_checkpoint(state, every=10, seconds=120):
    """Persist progress every `every` files OR every `seconds`, whichever first.

    The time limit matters: one 300-page PDF being figure-described can take an
    hour, and a purely count-based checkpoint would leave the state file looking
    untouched the whole time.
    """
    maybe_checkpoint.n = getattr(maybe_checkpoint, "n", 0) + 1
    last = getattr(maybe_checkpoint, "t", 0)
    if maybe_checkpoint.n % every == 0 or (time.time() - last) > seconds:
        maybe_checkpoint.t = time.time()
        try:
            save_state(state)
        except Exception:
            pass


# --- heartbeat: what is the run doing RIGHT NOW ---------------------------
# A small file updated continuously (per file, and per page during figure
# description) so `--status` can distinguish "working hard on a big document"
# from "died", which the state file's timestamp alone cannot do.
HEARTBEAT_FILE = STATE_FILE.with_suffix(".progress")
_PROGRESS = {}


def beat(stage=""):
    try:
        d = dict(_PROGRESS)
        d.update(pid=os.getpid(), updated=time.time(), stage=stage)
        HEARTBEAT_FILE.write_text(json.dumps(d))
    except OSError:
        pass


def read_beat():
    try:
        return json.loads(HEARTBEAT_FILE.read_text())
    except Exception:
        return {}


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:      # exists, owned by someone else
        return True
    except (ValueError, TypeError, OSError):
        return False


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


def _file_in_collection(session, file_id):
    """True/False if the collection can be read, else None (unknown).

    Used after a request timeout: the server may well have finished embedding
    after our client gave up waiting, and re-attaching a file that already
    landed would either duplicate it or look like a hard failure.
    """
    if not (TARGET and file_id):
        return None
    try:
        r = session.get(f"{BASE_URL}/api/v1/knowledge/{TARGET}", timeout=60)
        if not r.ok:
            return None
        d = r.json() or {}
        ids = {f.get("id") for f in (d.get("files") or []) if isinstance(f, dict)}
        ids |= set((d.get("data") or {}).get("file_ids") or [])
        return file_id in ids
    except Exception:
        return None


def add_openwebui(session, path):
    beat(f"uploading {path.name}")
    with open(path, "rb") as f:
        r = session.post(f"{BASE_URL}/api/v1/files/",
                         files={"file": (path.name, f)}, timeout=300)
    r.raise_for_status()
    file_id = r.json().get("id")
    if TARGET and file_id:
        # Don't attach until the server has actually extracted the text. Big
        # documents take minutes, so the patience is scaled by document weight
        # (pages / text volume), not just bytes — see extraction_budget().
        wait_s = extraction_budget(path)
        beat(f"server extracting text from {path.name}")
        n_chars, waited = _wait_for_extraction(session, file_id, wait_s)
        beat(f"server embedding {path.name}")
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
        budget = max(ATTACH_TIMEOUT, wait_s)   # server-side embed can take minutes
        timeouts = 0
        for i in range(attempts):
            # embedding a very large document can take many minutes server-side,
            # so allow a generous budget for the request itself
            try:
                resp = session.post(f"{BASE_URL}/api/v1/knowledge/{TARGET}/file/add",
                                    json={"file_id": file_id}, timeout=budget)
            except (requests.Timeout, requests.ConnectionError) as e:
                # AMBIGUOUS: the embed may still be running server-side and may
                # yet succeed. Don't fail, and don't blindly re-post — wait, then
                # ask the collection whether the file landed.
                timeouts += 1
                print(f"      (no reply after {budget}s — the server is still embedding; "
                      f"{type(e).__name__})")
                time.sleep(30)
                if _file_in_collection(session, file_id):
                    print("      (it completed server-side despite the timeout)")
                    return file_id
                if timeouts >= 3:
                    raise
                budget = min(budget * 2, 3600)
                print(f"      (retrying with a {budget}s budget)")
                continue
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


_VLM_CAPS = None          # None = not asked yet
_VLM_THINK_FIELD_OK = True


def vlm_caps(session):
    """Capabilities of the figure model, from Ollama, cached for the run.

    Recent Ollama reports these as e.g. ["completion", "vision", "thinking", "tools"].
    Worth asking once: it turns "every image failed" into a single clear message
    before the first page is rendered, and it says whether thinking must be switched
    off — which is the difference between a 4-second caption and a 30-second one."""
    global _VLM_CAPS
    if _VLM_CAPS is None:
        try:
            r = session.post(f"{OLLAMA_URL}/api/show",
                             json={"model": FIGURE_MODEL}, timeout=30)
            caps = (r.json() or {}).get("capabilities") if r.ok else None
            _VLM_CAPS = set(caps or [])
        except Exception:
            _VLM_CAPS = set()
    return _VLM_CAPS


def report_figure_model(session):
    """Print what the figure model is and whether it can actually see. Returns False
    only when Ollama is explicit that the model has no vision capability."""
    caps = vlm_caps(session)
    if not caps:
        print(f"[sync] figure model: {FIGURE_MODEL} (capabilities unknown — older "
              f"Ollama, or the model is not installed)")
        return True
    bits = [c for c in ("vision", "thinking", "tools") if c in caps]
    print(f"[sync] figure model: {FIGURE_MODEL}  [{', '.join(bits) or 'text only'}]")
    if "vision" not in caps:
        print(f"[sync] {FIGURE_MODEL} has NO vision capability — it cannot describe "
              f"images. Pick a vision model (e.g. qwen3.8:27b, gemma4:31b, llava) "
              f"with FIGURE_MODEL.")
        return False
    if "thinking" in caps and not FIGURE_THINK:
        print("[sync] thinking disabled for THESE requests only (per-request flag; your "
              "chats are unaffected). FIGURE_THINK=true to allow it here too.")
    if FIGURE_KEEP_ALIVE:
        print(f"[sync] keep_alive={FIGURE_KEEP_ALIVE} — model stays resident between images")
    if FIGURE_NUM_CTX:
        print(f"[sync] num_ctx={FIGURE_NUM_CTX} for figure calls — match your chat "
              f"preset's value or the model loads twice")
    else:
        print("[sync] num_ctx not set for figure calls: if your chat preset pins one, "
              "set FIGURE_NUM_CTX to the same value to avoid reload thrash")
    return True


def _vlm_describe(session, png_b64, prompt):
    """Ask the local vision model (Ollama) to describe one image."""
    global _VLM_THINK_FIELD_OK
    opts = {"temperature": FIGURE_TEMPERATURE}
    if FIGURE_NUM_CTX:
        opts["num_ctx"] = int(FIGURE_NUM_CTX)
    payload = {"model": FIGURE_MODEL, "prompt": prompt,
               "images": [png_b64], "stream": False, "options": opts}
    if FIGURE_KEEP_ALIVE:
        payload["keep_alive"] = FIGURE_KEEP_ALIVE
    thinking = "thinking" in vlm_caps(session)
    if thinking and not FIGURE_THINK and _VLM_THINK_FIELD_OK:
        payload["think"] = False

    r = session.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=600)
    # Older Ollama builds reject the `think` field outright. Drop it once and retry,
    # rather than failing every image for the rest of the run.
    if r.status_code == 400 and "think" in payload:
        _VLM_THINK_FIELD_OK = False
        payload.pop("think")
        r = session.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=600)
    r.raise_for_status()
    d = r.json()
    text = (d.get("response") or "").strip()
    if not text:
        # A thinking model that reasoned and returned nothing else lands here; the
        # reasoning is in its own field and is not a caption.
        text = (d.get("thinking") or "").strip()
    return text


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


def pdf_page_count(path):
    """Page count, or 0 if unknown."""
    try:
        import fitz
        d = fitz.open(str(path))
        n = d.page_count
        d.close()
        return n
    except Exception:
        return 0


def extraction_budget(path):
    """Seconds to allow for server-side extraction + embedding of one file.

    Scaled by how much WORK the document represents — pages and text volume, not
    just bytes on disk. A 2.5 MB / 40-page paper with 75k characters still needs
    minutes of chunking and embedding, while a big-but-sparse scan needs less.
    """
    try:
        mb = path.stat().st_size / 1e6
    except OSError:
        mb = 1.0
    pages = chars = 0
    if path.suffix.lower() == ".pdf":
        pages = pdf_page_count(path)
        chars = max(0, pdf_text_chars(path))
    est = max(mb * 30, pages * 5, chars / 40)
    return int(min(3600, max(180, est)))


def pdf_text_chars(path, max_pages=12):
    """Extractable characters in a PDF, sampled EVENLY across the whole file.

    Sampling only the first pages misreads long documents whose opening pages are
    scanned covers/title art but whose body is real text — they'd be flagged as
    'scanned' and pointlessly sent to OCR. Returns -1 if PyMuPDF isn't available.
    """
    try:
        import fitz
    except ImportError:
        return -1
    try:
        d = fitz.open(str(path))
        n = d.page_count
        idxs = range(n) if n <= max_pages else [
            int(i * (n - 1) / (max_pages - 1)) for i in range(max_pages)]
        total = 0
        for i in idxs:
            try:
                total += len(d[i].get_text("text").strip())
            except Exception:
                pass
        d.close()
        return total
    except Exception:
        return -1


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
            chars = pdf_text_chars(path)
            if chars < 0:
                return ""                      # PyMuPDF missing — can't tell
            if chars < 100:
                return (f"almost no extractable text (~{chars} chars sampled across "
                        "the file) — likely scanned / no text layer; try --ocr-fallback")
        elif ext in (".txt", ".md", ".csv", ".rtf"):
            if path.stat().st_size < 40:
                return "file is nearly empty"
    except Exception:
        return ""
    return ""


TEXTLIKE_EXTS = {".txt", ".md", ".rst", ".csv", ".tsv", ".json", ".html", ".htm"}

# Pre-2007 binary Office formats. Open WebUI's Default extractor reads the modern
# zip-based .docx/.pptx/.xlsx but NOT these, so they always come back as
# "400: The content provided is empty" even though the file is full of text.
# Converting locally to the modern equivalent fixes them.
LEGACY_OFFICE = {".doc": "docx", ".ppt": "pptx", ".xls": "xlsx"}


def _which(*names):
    for n in names:
        found = shutil.which(n)
        if found:
            return found
    return None


def converter_available():
    return bool(_which("soffice", "libreoffice") or _which("antiword", "catdoc"))


def make_converted_copy(src: pathlib.Path):
    """Convert a legacy .doc/.ppt/.xls into a readable copy in a temp dir.

    Prefers LibreOffice (keeps structure, handles all three formats); falls back to
    antiword/catdoc for .doc, which yield plain text. Returns (path, tool_name) or
    (None, ""). The caller removes path.parent.
    """
    target = LEGACY_OFFICE.get(src.suffix.lower())
    if not target:
        return None, ""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="rag_conv_"))
    soffice = _which("soffice", "libreoffice")
    if soffice:
        try:
            subprocess.run(
                [soffice, "--headless",
                 # own profile dir: avoids clashing with a desktop LibreOffice
                 f"-env:UserInstallation=file://{tmp}/profile",
                 "--convert-to", target, "--outdir", str(tmp), str(src)],
                check=True, timeout=300,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            out = tmp / f"{src.stem}.{target}"
            if out.is_file() and out.stat().st_size > 0:
                return out, pathlib.Path(soffice).name
        except Exception:
            pass
    if src.suffix.lower() == ".doc":
        tool = _which("antiword", "catdoc")
        if tool:
            try:
                r = subprocess.run([tool, str(src)], check=True, timeout=180,
                                   capture_output=True)
                txt = r.stdout.decode("utf-8", "ignore")
                if txt.strip():
                    out = tmp / f"{src.stem}.txt"
                    out.write_text(txt)
                    return out, pathlib.Path(tool).name
            except Exception:
                pass
    shutil.rmtree(tmp, ignore_errors=True)
    return None, ""


def has_nothing_to_index(path, min_chars=5, min_html_chars=20):
    """True when a file demonstrably holds no text at all.

    Uploading such a file is pointless: the server extracts nothing and answers
    400 "content provided is empty", which used to be logged as a FAILURE and made
    the whole run exit non-zero. An empty file isn't a failure — there is simply
    nothing to index — so it's skipped locally and reported separately.

    HTML gets its own test on the *rendered* text rather than the raw bytes: a
    frameset, a JS-shell page or a help-system stub is thousands of bytes of markup
    around zero readable words, so a byte-length check would wave it through.

    PDFs are never judged here — an image-only PDF has no text *yet*, which is what
    --ocr-fallback exists for.
    """
    try:
        if path.stat().st_size == 0:
            return True
        ext = path.suffix.lower()
        if ext in (".html", ".htm"):
            raw = path.read_text(errors="ignore")
            body = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", raw)
            return len(_WS_RE.sub(" ", _TAG_RE.sub(" ", body)).strip()) < min_html_chars
        if ext in TEXTLIKE_EXTS:
            return len(path.read_text(errors="ignore").strip()) < min_chars
    except OSError:
        pass
    return False


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

    n_pages = doc.page_count
    print(f"[sync]   describing figures in {pdf_path.name} ({n_pages} pages, "
          f"model={FIGURE_MODEL})…")
    sections = []
    t_fig = time.time()
    for i in range(n_pages):
        beat(f"figures page {i + 1}/{n_pages} of {pdf_path.name}")
        # A long PDF can occupy the vision model for an hour; without this the run
        # looks frozen. Report periodically once it's clear this is a long one.
        if n_pages >= 20 and i and i % 10 == 0:
            el = time.time() - t_fig
            print(f"[sync]     … page {i}/{n_pages} "
                  f"({el/60:.0f} min, ~{(n_pages - i) * el / i / 60:.0f} min left "
                  f"on this file)")
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
        # Which model wrote this caption. Recorded so --recaption can find the stale
        # ones and skip the done ones, which is what makes a multi-hour re-caption of
        # a big library resumable.
        files[key]["figures_model"] = FIGURE_MODEL
        STATS["figure_docs"] += 1
        STATS["figure_pages"] += n
        print(f"[sync]   + figure descriptions added ({n} page(s) with figures)")
    except requests.HTTPError as e:
        print(f"[sync]   figure-description upload failed ({e})")
    finally:
        shutil.rmtree(md_path.parent, ignore_errors=True)


def caption_inventory(state):
    """[(path, entry, kind, model_that_wrote_it)] for everything with a caption."""
    out = []
    for key, e in (state.get("files") or {}).items():
        p = pathlib.Path(key)
        ext = p.suffix.lower()
        if ext == ".pdf" and e.get("figures_id"):
            out.append((p, e, "pdf", e.get("figures_model")))
        elif ext in IMAGE_EXTS and e.get("remote_id"):
            out.append((p, e, "image", e.get("desc_model")))
    return out


def cmd_recaption(session, state, add_fn, remove_fn, limit=0):
    """Redo existing figure/image descriptions with the CURRENT figure model.

    Why this exists as its own mode: --force would re-upload and re-embed every
    document in the library to change captions that live in separate companion docs.
    This touches only the caption docs — the papers themselves are never re-embedded,
    so switching captioner costs vision time and nothing else.

    Resumable by design: each caption records the model that wrote it, so a run that
    stops after 300 files picks up at 301. --force redoes even up-to-date ones.
    """
    files = state["files"]
    targets = caption_inventory(state)
    if not targets:
        print("[sync] no existing figure/image descriptions recorded in the state file")
        print("[sync] (nothing to redo — a normal --describe-figures run creates them)")
        return
    by_model = collections.Counter(m or "unknown (pre-dates model tracking)"
                                  for _, _, _, m in targets)
    print(f"[sync] {len(targets)} caption doc(s) recorded:")
    for m, n in by_model.most_common():
        mark = "  <- current" if m == FIGURE_MODEL else ""
        print(f"[sync]     {n:5}  {m}{mark}")

    todo = targets if FORCE else [t for t in targets if t[3] != FIGURE_MODEL]
    if not todo:
        print(f"[sync] all captions already written by {FIGURE_MODEL} "
              f"— use --force to redo them anyway")
        return
    if limit:
        todo = todo[:limit]
        print(f"[sync] --limit {limit}: doing the first {len(todo)} only "
              f"(good for timing a sample before committing the library)")

    print(f"[sync] re-captioning {len(todo)} document(s) with {FIGURE_MODEL}")
    print("[sync] the source documents are NOT re-uploaded or re-embedded")
    done = failed = missing = 0
    t0 = time.time()
    for i, (p, entry, kind, old_model) in enumerate(todo, 1):
        _PROGRESS.update(idx=i, total=len(todo), file=p.name,
                         file_started=time.time(), started=t0,
                         config=str(CONFIG_PATH or ""))
        beat("recaption")
        if not p.is_file():
            print(f"[sync] [{i}/{len(todo)}] {p.name}: source file is gone — skipped "
                  f"(a --prune run removes its docs)")
            missing += 1
            continue
        print(f"[sync] [{i}/{len(todo)}] {p.name} "
              f"(was: {old_model or 'unknown'}) …")
        try:
            # Build the NEW caption first. If the vision model fails, the old caption
            # is still in the collection — better a stale description than none.
            if kind == "pdf":
                md, n = build_figures_doc(session, p)
            else:
                md, n = build_image_doc(session, p), 1
            if not md:
                if kind == "pdf":
                    print("[sync]   no figures found this time — removing the old "
                          "description doc")
                    try:
                        remove_fn(session, entry["figures_id"])
                    except Exception as e:
                        print(f"[sync]   (could not remove the old doc: {e})")
                    entry["figures_id"] = None
                    entry["figures_model"] = FIGURE_MODEL
                else:
                    print("[sync]   could not describe the image — old doc kept")
                    failed += 1
                continue
            try:
                old_id = entry.get("figures_id") if kind == "pdf" else entry.get("remote_id")
                if old_id:
                    try:
                        remove_fn(session, old_id)
                    except Exception as e:
                        print(f"[sync]   old doc not removed ({e}) — continuing; "
                              f"check for a duplicate in the UI")
                new_id = add_fn(session, md)
                if kind == "pdf":
                    entry["figures_id"] = new_id
                    entry["figures_model"] = FIGURE_MODEL
                    print(f"[sync]   + new descriptions ({n} page(s) with figures)")
                else:
                    entry["remote_id"] = new_id
                    entry["desc_model"] = FIGURE_MODEL
                    print("[sync]   + new image description")
                done += 1
            finally:
                shutil.rmtree(md.parent, ignore_errors=True)
        except requests.HTTPError as e:
            print(f"[sync]   FAILED: {e}")
            failed += 1
        except Exception as e:
            print(f"[sync]   FAILED: {type(e).__name__}: {e}")
            failed += 1
        maybe_checkpoint(state, every=5, seconds=120)
        if i % 5 == 0 and i < len(todo):
            el = time.time() - t0
            print(f"[sync] --- {i}/{len(todo)} ({i/len(todo)*100:.0f}%) — "
                  f"{el/60:.0f} min elapsed, ~{(len(todo)-i)*(el/i)/60:.0f} min left "
                  f"at {el/i:.0f}s/doc ---")
    save_state(state)
    print(f"\n[sync] re-captioned {done}, failed {failed}, source missing {missing}")
    print(f"[sync] took {(time.time()-t0)/60:.0f} min")
    if failed:
        print("[sync] failures keep their previous description — re-run to retry them")


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


def cmd_status():
    """How far along is this library? Safe to run WHILE a sync is in progress.

    Compares the state file against the folder. Because state is checkpointed
    every 10 files, the count during a run is accurate to within ~10 files.
    """
    print(f"[sync] status for {WATCH_DIR}")
    print(f"[sync] state file: {STATE_FILE}")
    if not STATE_FILE.is_file():
        print("[sync] no state file yet — nothing has been synced with this config.")
        return
    st = load_state()
    tracked = st.get("files", {})
    if not WATCH_DIR.is_dir():
        die(f"watch dir does not exist: {WATCH_DIR}")
    # The denominator is what THIS configuration can index, so it matches what a
    # run would attempt. Anything excluded is named rather than silently dropped.
    wanted = EXTS | (IMAGE_EXTS if DESCRIBE_FIGURES else set())
    on_disk_all = [p for p in WATCH_DIR.rglob("*")
                   if p.is_file() and p.suffix.lower() in (EXTS | IMAGE_EXTS)]
    cand = [p for p in on_disk_all if p.suffix.lower() in wanted]
    ignored = len(on_disk_all) - len(cand)
    keys = {str(p) for p in cand}
    done = len(keys & set(tracked))
    total = len(cand)
    ghosts = len(set(tracked) - keys)
    age = time.time() - STATE_FILE.stat().st_mtime
    pct = (done / total * 100) if total else 0
    bar = "#" * int(pct / 2.5) + "." * (40 - int(pct / 2.5))
    print(f"[sync] [{bar}] {done}/{total} processed ({pct:.0f}%)")
    print(f"[sync] folder holds {len(on_disk_all)} file(s): "
          f"{done} processed, {total - done} to go"
          + (f", {ignored} image(s) ignored (needs --describe-figures)" if ignored else ""))
    if ghosts:
        print(f"[sync] {ghosts} tracked entr(ies) no longer on disk "
              f"(--prune removes them from the collection)")

    # Caption inventory by model: the answer to "how far through the re-caption am I"
    # and "which of these were written by the old captioner".
    caps = caption_inventory(state)
    if caps:
        counts = collections.Counter(m or "unknown (pre-dates model tracking)"
                                    for _, _, _, m in caps)
        print(f"[sync] {len(caps)} caption doc(s), by the model that wrote them:")
        for m, n in counts.most_common():
            mark = "  <- current FIGURE_MODEL" if m == FIGURE_MODEL else ""
            print(f"[sync]     {n:5}  {m}{mark}")
        stale = sum(n for m, n in counts.items() if m != FIGURE_MODEL)
        if stale:
            print(f"[sync] {stale} could be redone with:  --recaption "
                  f"(only the caption docs; nothing is re-embedded)")

    # Liveness comes from the heartbeat + the PID, NOT from the state file's age:
    # a single big PDF under --describe-figures can legitimately take an hour, and
    # judging by the state timestamp alone would call a healthy run dead.
    b = read_beat()
    if b and pid_alive(b.get("pid")):
        quiet = time.time() - b.get("updated", 0)
        on_file = time.time() - b.get("file_started", b.get("updated", time.time()))
        print(f"[sync] RUNNING (pid {b['pid']}) — [{b.get('idx','?')}/{b.get('total','?')}] "
              f"{b.get('file','?')}")
        print(f"[sync]   currently: {b.get('stage','?')}"
              f"  ({on_file/60:.1f} min on this file)")
        if quiet > 900:
            print(f"[sync]   ! no heartbeat for {quiet/60:.0f} min — this one may be "
                  "genuinely stuck; check `ollama ps` and the run's own output.")
    elif b:
        print(f"[sync] NOT running — process {b.get('pid')} is gone; it stopped while on "
              f"[{b.get('idx','?')}/{b.get('total','?')}] {b.get('file','?')} "
              f"({b.get('stage','?')}).")
        print("[sync]   just re-run the same command; it resumes from the state file.")
    elif age < 600:
        # No heartbeat, yet the state file is moving: something IS syncing, but it
        # was started from a version without heartbeats (they arrived in
        # 2026.08.01.6). Don't claim it's idle.
        print(f"[sync] a run appears ACTIVE (state written {age/60:.1f} min ago) but it "
              "publishes no heartbeat,")
        print(f"[sync]   so it was started from an older copy of this script "
              f"(heartbeats were added in 2026.08.01.6; this is {__version__}).")
        print("[sync]   Confirm with:  pgrep -af sync_folder")
        print("[sync]   Restart it with the current script to get live per-file status.")
    else:
        print(f"[sync] no run in progress (state last written {age/60:.1f} min ago).")
    print("[sync] the collection's own count is in the UI: Workspace → Knowledge.")


def main():
    # show which configuration is actually in effect — makes a wrong TARGET or a
    # shared STATE_FILE obvious before anything is uploaded
    if "--version" in sys.argv:
        print(f"sync_folder.py {__version__}")
        return
    if "--status" in sys.argv:
        cmd_status()
        return
    print(f"[sync] v{__version__} | config: "
          f"{CONFIG_PATH if CONFIG_PATH else '(none found — using defaults/env)'}")
    print(f"[sync] {BACKEND} at {BASE_URL} | target={TARGET or '(unset)'} | "
          f"dir={WATCH_DIR} | state={STATE_FILE.name}")
    if not API_KEY:
        die("no API key — set KEY_FILE in the config file (or RAG_API_KEY)")
    if BACKEND not in ADAPTERS:
        die(f"RAG_BACKEND must be 'anythingllm' or 'openwebui', got '{BACKEND}'")
    if not WATCH_DIR.is_dir():
        die(f"watch dir does not exist: {WATCH_DIR}")
    if not TARGET:
        print("[sync] WARNING: RAG_TARGET not set — files upload but won't be "
              "attached to a workspace/collection (and can't be pruned).")

    # Refuse to run twice against the same library. Two concurrent syncs share one
    # STATE_FILE (last writer wins, so progress is lost), race each other into
    # "Duplicate content detected", and double the load on one GPU.
    other = read_beat()
    if other and pid_alive(other.get("pid")) and other.get("pid") != os.getpid():
        if "--allow-parallel" in sys.argv:
            print(f"[sync] WARNING: pid {other['pid']} is already syncing this library; "
                  "continuing anyway because --allow-parallel was given.")
        else:
            die(f"another sync is already running for this library (pid {other['pid']}, "
                f"on [{other.get('idx','?')}/{other.get('total','?')}] "
                f"{other.get('file','?')}).\n"
                f"       Check it with:  python3 {pathlib.Path(__file__).name} "
                f"--config <conf> --status\n"
                f"       Stop it with:   kill {other['pid']}\n"
                f"       Two runs would overwrite each other's state file "
                f"({STATE_FILE.name}). Use --allow-parallel only for different libraries.")

    add_fn, remove_fn = ADAPTERS[BACKEND]
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {API_KEY}"})

    # Check the figure model BEFORE any work: a wrong FIGURE_MODEL otherwise shows up
    # as an error on every image, hundreds of files into a run.
    if (DESCRIBE_FIGURES or RECAPTION) and not report_figure_model(session):
        die("figure descriptions requested but the model cannot see images — "
            "fix FIGURE_MODEL, or drop --describe-figures/--recaption")

    state = load_state()
    # if the backend/target changed, stored remote ids no longer apply
    if state.get("backend") not in (None, BACKEND) or state.get("target") not in (None, TARGET):
        print("[sync] backend/target changed since last run — starting a fresh state "
              "(old remote ids can't be reused).")
        state = {"files": {}}
    state["backend"], state["target"] = BACKEND, TARGET

    if RECAPTION:
        cmd_recaption(session, state, add_fn, remove_fn, limit=RECAPTION_LIMIT)
        return
    files = state.setdefault("files", {})
    _LIVE_STATE["state"] = state    # so progress survives a crash or Ctrl-C

    added = updated = removed = dup = 0
    failed = []   # names of files that couldn't be added

    # --- add / update files currently on disk ---
    # Work out the whole to-do list FIRST, so every line can be numbered and the
    # run can report a percentage and an ETA. Hashing up front costs no extra work
    # overall (each file was hashed inside the loop before), it just front-loads it.
    print(f"[sync] scanning {WATCH_DIR} …")
    # every file the folder offers (images included, so PRUNE doesn't drop image
    # docs added by an earlier --describe-figures run)
    all_cand = [p for p in sorted(WATCH_DIR.rglob("*"))
                if p.is_file() and p.suffix.lower() in (EXTS | IMAGE_EXTS)]
    on_disk = {str(p) for p in all_cand}
    work, n_img_skipped, n_unchanged = [], 0, 0
    for p in all_cand:
        if p.suffix.lower() in IMAGE_EXTS and not DESCRIBE_FIGURES:
            n_img_skipped += 1   # not indexable without a vision pass
            continue
        digest = file_hash(p)
        entry = files.get(str(p))
        if entry and entry.get("hash") == digest and not FORCE:
            n_unchanged += 1     # unchanged (use --force to re-sync anyway)
            continue
        work.append((p, digest, entry))
    n_work = len(work)
    print(f"[sync] {len(all_cand)} file(s) on disk: {n_work} to process, "
          f"{n_unchanged} unchanged"
          + (f", {n_img_skipped} image(s) ignored (needs --describe-figures)"
             if n_img_skipped else ""))

    # Warn ONCE, up front, rather than failing on each legacy file in turn.
    n_legacy = sum(1 for w in work if w[0].suffix.lower() in LEGACY_OFFICE)
    if n_legacy:
        if not CONVERT_LEGACY:
            print(f"[sync] ! {n_legacy} legacy Office file(s) (.doc/.ppt/.xls) will be "
                  "uploaded as-is (CONVERT_LEGACY off) and will almost certainly fail.")
        elif not converter_available():
            print(f"[sync] ! {n_legacy} legacy Office file(s) (.doc/.ppt/.xls) need a local "
                  "converter, and none is installed. The server cannot read these formats,")
            print("[sync]   so they WILL fail. Install one now and re-run:")
            print("[sync]     sudo apt install libreoffice-writer     # best: handles doc/ppt/xls")
            print("[sync]     sudo apt install antiword               # lighter, .doc only")
        else:
            print(f"[sync] {n_legacy} legacy Office file(s) will be converted locally "
                  "before upload.")
    if OCR_FALLBACK and shutil.which("ocrmypdf") is None:
        print("[sync] ! OCR_FALLBACK is enabled but 'ocrmypdf' is not installed, so "
              "text-less PDFs cannot be recovered:")
        print("[sync]     sudo apt install -y ocrmypdf jbig2enc")
    t_start = time.time()

    for idx, (p, digest, entry) in enumerate(work, 1):
        ext = p.suffix.lower()
        key = str(p)
        is_update = bool(entry and entry.get("remote_id"))
        pos = f"[{idx}/{n_work}]"
        _PROGRESS.update(idx=idx, total=n_work, file=p.name,
                         file_started=time.time(), started=t_start,
                         config=str(CONFIG_PATH or ""))
        beat("starting")
        # ETA from the files already finished, so the numbers refer to real work
        done_n = idx - 1
        if PROGRESS_EVERY and done_n and done_n % PROGRESS_EVERY == 0:
            el = time.time() - t_start
            left = (n_work - done_n) * (el / done_n)
            print(f"[sync] --- {done_n}/{n_work} done ({done_n/n_work*100:.0f}%) — "
                  f"{el/60:.0f} min elapsed, ~{left/60:.0f} min left "
                  f"at {el/done_n:.1f}s/file ---")

        # --- standalone image files: index a vision description of the image ---
        if ext in IMAGE_EXTS:
            if not DESCRIBE_FIGURES:
                print(f"[sync] {pos} skipping image {p.name} "
                      "(run with --describe-figures to index images)")
                continue
            print(f"[sync] {pos} {'updating' if is_update else 'adding'} image {p.name} …")
            try:
                if is_update:
                    remove_fn(session, entry["remote_id"])
                md = build_image_doc(session, p)
                if not md:
                    failed.append(p.name)
                    continue
                try:
                    remote_id = add_fn(session, md)
                    # for a standalone image the uploaded doc IS the description, so
                    # the model that wrote it is recorded on the entry itself
                    files[key] = {"hash": digest, "remote_id": remote_id,
                                  "desc_model": FIGURE_MODEL}
                finally:
                    shutil.rmtree(md.parent, ignore_errors=True)
                updated += is_update
                added += not is_update
                STATS["images"] += 1
                maybe_checkpoint(state)
                print("[sync]   + image description added")
            except (requests.Timeout, requests.ConnectionError) as e:
                # the server never answered (busy GPU / slow embed). Report and
                # move on — one stalled file must not abort a multi-hour run.
                print(f"[sync]   FAILED: {p.name}")
                print(f"[sync]     the server took too long or dropped the connection "
                      f"({type(e).__name__}); it may still finish server-side.")
                print(f"[sync]     re-run later, or raise ATTACH_TIMEOUT "
                      f"(currently {ATTACH_TIMEOUT}s) in the config.")
                failed.append(p.name)
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
        # An empty text file can't be indexed by anyone; skip it here rather than
        # letting the server reject it as a "failure" on every single run.
        if has_nothing_to_index(p):
            why = ("only markup, no readable text (frameset / JS shell / help stub)"
                   if ext in (".html", ".htm")
                   else f"the file contains no text ({p.stat().st_size} bytes)")
            print(f"[sync] {pos} skipping {p.name} — {why}")
            if is_update:
                try:
                    remove_fn(session, entry["remote_id"])
                except Exception:
                    pass
            files[key] = {"hash": digest, "remote_id": None}
            STATS["empty"] += 1
            maybe_checkpoint(state)
            continue

        print(f"[sync] {pos} {'updating' if is_update else 'adding'} {p.name} …")
        pf = preflight_text_warning(p)
        if pf:
            print(f"[sync]   ! {pf}")

        # Legacy binary Office files are converted BEFORE upload: the server can
        # never read them, so uploading first just wastes a round trip and logs a
        # spurious failure. The original file is what gets hashed/tracked.
        upload_path, conv_dir = p, None
        if CONVERT_LEGACY and ext in LEGACY_OFFICE:
            conv, tool = make_converted_copy(p)
            if conv:
                upload_path, conv_dir = conv, conv.parent
                print(f"[sync]   converted {ext} → {conv.suffix} with {tool} "
                      "(the server cannot read legacy Office files)")
            else:
                print(f"[sync]   ! cannot convert {ext} — install libreoffice "
                      "(or antiword), or enable Tika; uploading as-is will likely fail")
        try:
            if is_update:
                # changed file: remove the old copy (and its figure doc) first
                remove_fn(session, entry["remote_id"])
                if entry.get("figures_id"):
                    remove_fn(session, entry["figures_id"])
            remote_id = add_fn(session, upload_path)
            files[key] = {"hash": digest, "remote_id": remote_id}
            updated += is_update
            added += not is_update
            STATS["pdfs" if ext == ".pdf" else "text_records"] += 1
            if ext == ".md":
                STATS["md_records"] += 1
            if conv_dir:
                STATS["converted"] += 1
            attach_figures(session, p, key, files, add_fn)
            maybe_checkpoint(state)
        except (requests.Timeout, requests.ConnectionError) as e:
            # a very large document can exceed the request budget; report it
            # clearly instead of letting the exception kill the whole run
            print(f"[sync]   FAILED: {p.name}")
            print(f"[sync]     the server took too long / dropped the connection ({type(e).__name__}).")
            print(f"[sync]     size: {p.stat().st_size/1e6:.0f} MB"
                  + (f", {pdf_page_count(p)} pages" if ext == '.pdf' else ""))
            print(f"[sync]     the embed may still finish server-side; re-run later. If this "
                  f"recurs, raise ATTACH_TIMEOUT (now {ATTACH_TIMEOUT}s) in the config,")
            print("[sync]     sync this file on its own, or split it into parts.")
            failed.append(p.name)
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
            # Skip it when the PDF demonstrably already HAS text — OCR can't help,
            # and force-OCRing a 200-page file wastes a lot of time.
            if (empty_content and OCR_FALLBACK and p.suffix.lower() == ".pdf"
                    and pdf_text_chars(p) < 500):
                ocr_path = make_ocr_copy(p)
                if ocr_path:
                    try:
                        remote_id = add_fn(session, ocr_path)
                        files[key] = {"hash": digest, "remote_id": remote_id}
                        added += not is_update
                        updated += is_update
                        STATS["pdfs"] += 1
                        STATS["ocr_recovered"] += 1
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
            if empty_content and pdf_text_chars(p) >= 500:
                print("[sync]     hint: this file DOES contain extractable text, so the "
                      "server-side extraction is at fault — usually a large document "
                      "timing out. Retry it alone, or check the extraction engine.")
            elif empty_content and ext in LEGACY_OFFICE:
                print(f"[sync]     hint: '{ext}' is the pre-2007 binary Office format, which")
                print("[sync]       the Default extractor cannot read at all (the file is fine).")
                print("[sync]       Fix it in one of three ways:")
                print("[sync]         - install a converter, then re-run this script:")
                print("[sync]             sudo apt install libreoffice-writer   # or: antiword")
                print(f"[sync]         - resave the file as .{LEGACY_OFFICE[ext]}")
                print("[sync]         - enable Tika, which parses legacy Office natively")
            elif empty_content and ext == ".pdf":
                print("[sync]     hint: the server extracted no text from this PDF.")
                if not OCR_FALLBACK:
                    print("[sync]       - no text layer? enable OCR_FALLBACK in the config "
                          "(or --ocr-fallback)")
                elif shutil.which("ocrmypdf") is None:
                    print("[sync]       - OCR_FALLBACK is on but 'ocrmypdf' is NOT installed:")
                    print("[sync]           sudo apt install -y ocrmypdf jbig2enc")
                else:
                    print("[sync]       - OCR ran and the retry still failed, so the "
                          "extraction engine itself")
                    print("[sync]         is likely at fault (e.g. Tika selected but not "
                          "running): Admin > Settings > Documents.")
            elif empty_content:
                # OCR is meaningless for non-PDFs — don't suggest it.
                print(f"[sync]     hint: the server extracted no text from this "
                      f"'{ext}' file. OCR does not apply to this format. Common causes:")
                print("[sync]       - the file really has no readable text (a frameset, a")
                print("[sync]         JS-shell page, a help stub, a template) -> nothing to index")
                print("[sync]       - extraction engine set to Tika/Docling but that service")
                print("[sync]         isn't running -> revert to Default (or start Tika), in")
                print("[sync]         Admin > Settings > Documents.")
            elif e.response is not None and e.response.status_code in (401, 403):
                print("[sync]     hint: auth rejected -> check the API key (use the sk- key,")
                print(f"[sync]       not the JWT token) in {KEY_FILE}.")
            failed.append(p.name)
        finally:
            if conv_dir:
                shutil.rmtree(conv_dir, ignore_errors=True)

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
    _LIVE_STATE.pop("state", None)   # saved cleanly; no exit-handler save needed
    try:
        HEARTBEAT_FILE.unlink()      # nothing is running any more
    except OSError:
        pass
    # "Processed" is the honest denominator: every file this run attempted. It is
    # reported alongside the categories that were deliberately NOT attempted
    # (unchanged, images without --describe-figures) so the folder count adds up.
    n_empty = STATS.get("empty", 0)
    processed = added + updated + dup + len(failed)
    print(f"[sync] done — processed {processed}/{n_work} to-do file(s): "
          f"{added} added, {updated} updated, {dup} already-present, "
          f"{len(failed)} failed, {removed} removed"
          + (f"; {n_empty} skipped as empty" if n_empty else "")
          + f"{' (prune ON)' if PRUNE else ''}.")
    print(f"[sync] folder holds {len(all_cand)} file(s): {n_work} to process, "
          f"{n_unchanged} unchanged"
          + (f", {n_img_skipped} image(s) ignored" if n_img_skipped else "")
          + f"; {len(files)} tracked in state.")

    # ---- what actually made it in, by type -----------------------------
    md = STATS.get("md_records", 0)
    other_text = STATS.get("text_records", 0) - md
    print("[sync] processed this run:")
    print(f"         full PDFs                 : {STATS.get('pdfs', 0)}"
          + (f"  ({STATS['ocr_recovered']} recovered via OCR)"
             if STATS.get("ocr_recovered") else ""))
    print(f"         abstract/metadata .md     : {md}")
    if other_text:
        print(f"         other text documents      : {other_text}")
    print(f"         standalone images         : {STATS.get('images', 0)}"
          + (f"   ({n_img_skipped} ignored — needs --describe-figures)"
             if n_img_skipped else ""))
    if STATS.get("converted"):
        print(f"         legacy .doc/.ppt/.xls converted : {STATS['converted']}")
    if n_empty:
        print(f"         empty files (nothing to index, not an error): {n_empty}")
    if DESCRIBE_FIGURES:
        print(f"         figure-description docs   : {STATS.get('figure_docs', 0)}"
              f"  (from {STATS.get('figure_pages', 0)} page(s) containing figures)")
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
    try:
        main()
    except KeyboardInterrupt:
        # Ctrl-C or `kill -INT`: the exit handler has already written the state
        # file, so say so plainly instead of dumping a traceback.
        print("\n[sync] interrupted — progress saved; re-run the same command to "
              "resume where it stopped.")
        sys.exit(130)
