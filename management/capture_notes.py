#!/usr/bin/env python3
"""
capture_notes.py — turn things you learn in a chat into lab notes the RAG can find.

A fact that surfaces in conversation is knowledge the corpus does not have. This
writes it to a markdown file in your notes folder, where sync_folder indexes it on
the next run — so it becomes retrievable like any other document.

Two ways in, both of them YOUR words, never the model's:

  1. In chat, write a line starting with "Remember:"  (or "Remember that …")
     Then harvest them:
         python3 capture_notes.py --harvest --since 7
     Each marked line is extracted VERBATIM from your own message. The model is not
     asked to summarise anything, so nothing can be invented.

  2. From the terminal, when you already know what to save:
         python3 capture_notes.py --note "Polished cross-sections live in cabinet B" \\
             --topic "sample storage" --also-called "sample cabinet, cross sections"

Why not let the model write notes on its own: model output that becomes retrieval
context gets cited as a source in later answers, which makes an invention look
grounded and leaves no way to tell afterwards. Everything here is human-authored;
`--harvest` only moves text you already typed.

Why files and not direct uploads to the collection: sync_folder tracks files on
disk, so a document created through the API alone is untracked — and a later
--prune run deletes it as an orphan. The file is the source of truth.

Config: reads BASE_URL / KEY_FILE from the same .conf files the sync tool uses
(--config), or RAG_BASE_URL / RAG_KEY_FILE / RAG_NOTES_DIR from the environment.

After capturing, index them:
    python3 sync_folder.py --config lab_notes.conf
"""

__version__ = "2026.9.18.1"

import os
import re
import sys
import json
import time
import shutil
import difflib
import hashlib
import pathlib
import argparse
import datetime

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

G, R, Y, B, X = "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[0m"
def ok(m):   print(f"  {G}✔{X} {m}")
def bad(m):  print(f"  {R}x{X} {m}")
def warn(m): print(f"  {Y}!{X} {m}")
def info(m): print(f"  • {m}")
def step(m): print(f"\n{B}==> {m}{X}")
def die(m):  sys.exit(f"  {R}x{X} {m}")

# A marked line is the whole contract: everything after the marker is the note.
# Deliberately narrow — "remember" mid-sentence in ordinary conversation should not
# create a note, so the marker has to START the line.
MARKER_RE = re.compile(r"^\s*(?:#\s*)?remember(?:\s+that)?\s*[:,\-–]?\s+(.{8,})$", re.I)

NOTE_EXTS = (".md", ".txt")
# Below this, two notes are unrelated; above it, they are probably about the same
# thing and you should decide rather than end up with both.
SIMILAR = 0.55


# ------------------------------------------------------------------ config

def load_conf(path):
    out = {}
    if not path:
        return out
    p = pathlib.Path(path).expanduser()
    if not p.is_file():
        die(f"config file not found: {p}")
    for raw in p.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if not line or line[0] in "#;[" or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().upper()
        k = k[4:] if k.startswith("RAG_") else k
        v = re.split(r"\s+[#;]", v.strip(), maxsplit=1)[0].strip().strip("'\"")
        if v.startswith("~") or v.startswith("$") or "/" in v:
            v = os.path.expandvars(os.path.expanduser(v))
        out[k] = v
    return out


def cfg(conf, name, default=None):
    return os.environ.get("RAG_" + name) or conf.get(name) or default


# --------------------------------------------------------------------- API

def read_key(key_file):
    if os.environ.get("RAG_API_KEY"):
        return os.environ["RAG_API_KEY"]
    p = pathlib.Path(key_file).expanduser()
    if not p.is_file():
        die(f"no API key at {p} — put the sk-… key there (chmod 600) or set RAG_API_KEY")
    return p.read_text().strip()


def api(session, url, timeout=60):
    """JSON or None. Open WebUI serves the SPA for unknown paths, so a 200 alone
    proves nothing about the route existing."""
    try:
        r = session.get(url, timeout=timeout)
    except Exception as e:
        warn(f"{url}: {type(e).__name__}: {e}")
        return None
    if not r.ok:
        warn(f"{url} -> HTTP {r.status_code}: {(r.text or '')[:120]}")
        return None
    if "application/json" not in r.headers.get("Content-Type", ""):
        return None
    try:
        return r.json()
    except ValueError:
        return None


def as_list(payload):
    """Chat lists come bare or wrapped depending on the build."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("data", "items", "results", "chats"):
            if isinstance(payload.get(k), list):
                return payload[k]
        for v in payload.values():
            if isinstance(v, list) and (not v or isinstance(v[0], dict)):
                return v
    return None


def list_chats(session, base):
    for path in ("/api/v1/chats/", "/api/v1/chats/list", "/api/v1/chats"):
        items = as_list(api(session, f"{base}{path}"))
        if items is not None:
            return items
    die(f"could not list chats on {base} — check the URL and the API key")


def chat_messages(obj):
    """[(role, text, message_id)] from whatever shape this build stores.

    Open WebUI has kept messages as a list under chat.messages and as a dict under
    chat.history.messages at different times; both appear in the wild."""
    out = []
    chat = obj.get("chat") if isinstance(obj, dict) else None
    chat = chat if isinstance(chat, dict) else (obj if isinstance(obj, dict) else {})
    msgs = chat.get("messages")
    if not isinstance(msgs, list):
        hist = chat.get("history")
        if isinstance(hist, dict) and isinstance(hist.get("messages"), dict):
            msgs = list(hist["messages"].values())
    for m in msgs or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, list):          # multimodal: keep the text parts
            content = " ".join(p.get("text", "") for p in content
                               if isinstance(p, dict))
        if isinstance(content, str) and content.strip():
            out.append((m.get("role", "?"), content, str(m.get("id", ""))))
    return out


# ------------------------------------------------------------------- notes

def slugify(text, maxlen=48):
    s = re.sub(r"[^A-Za-z0-9]+", "-", text.lower()).strip("-")
    return (s[:maxlen].rstrip("-") or "note")


def existing_notes(folder):
    notes = []
    for p in sorted(pathlib.Path(folder).expanduser().rglob("*")):
        if p.is_file() and p.suffix.lower() in NOTE_EXTS:
            try:
                notes.append((p, p.read_text(errors="ignore")))
            except OSError:
                pass
    return notes


def normalise(t):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (t or "").lower())).strip()


def similar_notes(claim, notes, threshold=SIMILAR):
    """Existing notes that look like they are about the same thing.

    Two signals, because either alone misfires: overall text similarity (catches a
    rephrasing) and shared distinctive words (catches a short note about the same
    object written differently)."""
    c = normalise(claim)
    c_terms = {w for w in c.split() if len(w) > 4}
    hits = []
    for p, text in notes:
        body = normalise(text)
        ratio = difflib.SequenceMatcher(None, c, body[:2000]).ratio()
        shared = c_terms & {w for w in body.split() if len(w) > 4}
        score = max(ratio, (len(shared) / max(3, len(c_terms))) if c_terms else 0)
        if score >= threshold:
            hits.append((score, p, text, sorted(shared)[:8]))
    return sorted(hits, reverse=True, key=lambda h: h[0])


def render_note(claim, topic, aliases, source):
    title = topic or claim[:60].rstrip(" .,;")
    lines = [f"# {title}", ""]
    if aliases:
        # The lesson from retrieval: a note is only found if it carries the words
        # people search with. An alias line is the cheapest way to add them.
        lines += [f"Also called: {aliases}", ""]
    lines += [claim.strip(), ""]
    lines += ["<!-- captured by capture_notes.py",
              f"     when: {datetime.datetime.now().astimezone().isoformat(timespec='seconds')}"]
    for k, v in (source or {}).items():
        lines.append(f"     {k}: {v}")
    lines += ["     verbatim from a human message; not model-generated -->", ""]
    return "\n".join(lines)


def write_note(folder, filename, text, dry=False):
    folder = pathlib.Path(folder).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    if dry:
        info(f"[dry-run] would write {path}")
        print("\n".join("      " + l for l in text.splitlines()))
        return path
    path.write_text(text)
    ok(f"wrote {path}")
    return path


def append_to_note(path, claim, source, dry=False):
    """Add a dated addition to an existing note rather than creating a rival one."""
    stamp = datetime.date.today().isoformat()
    add = (f"\n## Added {stamp}\n\n{claim.strip()}\n\n"
           f"<!-- captured by capture_notes.py; {json.dumps(source or {})} -->\n")
    if dry:
        info(f"[dry-run] would append to {path}:")
        print("\n".join("      " + l for l in add.splitlines()))
        return
    with path.open("a") as fh:
        fh.write(add)
    ok(f"appended to {path}")


# --------------------------------------------------------------- capture UX

def resolve_conflict(claim, hits, folder, topic, aliases, source, dry, assume_yes):
    """Show the old note and the new claim, and let the human decide.

    Silently adding a second note is the worst option: retrieval then returns two
    documents that disagree and the model picks one, with no signal that anything
    is wrong."""
    score, path, text, shared = hits[0]
    print()
    warn(f"this looks related to an existing note ({score:.0%} similar): {path.name}")
    if shared:
        info(f"shared terms: {', '.join(shared)}")
    print(f"\n  {B}existing{X} ({path.name}):")
    for line in text.strip().splitlines()[:14]:
        print(f"      {line}")
    print(f"\n  {B}new{X}:")
    print(f"      {claim.strip()}")
    if assume_yes:
        info("--yes: keeping both is not automatic for a conflict — skipping this one")
        return None
    print()
    print("  [a] append to the existing note (dated addition)")
    print("  [r] replace the existing note's body with the new claim")
    print("  [n] write a NEW separate note anyway")
    print("  [s] skip")
    choice = (input("  choice? [a/r/n/s] ").strip().lower() or "s")[:1]
    if choice == "a":
        append_to_note(path, claim, source, dry)
        return path
    if choice == "r":
        if not dry:
            bak = path.with_suffix(path.suffix + ".bak")
            shutil.copy2(path, bak)
            info(f"previous version kept as {bak.name}")
        write_note(folder, path.name, render_note(claim, topic, aliases, source), dry)
        return path
    if choice == "n":
        name = f"{slugify(topic or claim)}-{datetime.date.today().isoformat()}.md"
        return write_note(folder, name, render_note(claim, topic, aliases, source), dry)
    info("skipped")
    return None


def capture(claim, folder, topic=None, aliases=None, source=None,
            dry=False, assume_yes=False):
    notes = existing_notes(folder)
    hits = similar_notes(claim, notes)
    if hits:
        return resolve_conflict(claim, hits, folder, topic, aliases, source,
                                dry, assume_yes)
    name = f"{slugify(topic or claim)}.md"
    if (pathlib.Path(folder).expanduser() / name).exists():
        name = f"{slugify(topic or claim)}-{datetime.date.today().isoformat()}.md"
    return write_note(folder, name, render_note(claim, topic, aliases, source), dry)


# ------------------------------------------------------------------ harvest

def state_path(folder):
    return pathlib.Path(folder).expanduser() / ".capture_state.json"


def load_seen(folder):
    p = state_path(folder)
    if p.is_file():
        try:
            return set(json.loads(p.read_text()).get("seen", []))
        except Exception:
            pass
    return set()


def save_seen(folder, seen):
    try:
        state_path(folder).write_text(json.dumps({"seen": sorted(seen)}, indent=1))
    except OSError as e:
        warn(f"could not save capture state: {e}")


def cmd_harvest(session, base, folder, since_days, dry, assume_yes, limit):
    step(f"Harvesting 'Remember:' lines from {base}")
    chats = list_chats(session, base)
    cutoff = time.time() - since_days * 86400
    seen = load_seen(folder)
    fresh = []
    for c in chats:
        if not isinstance(c, dict):
            continue
        ts = c.get("updated_at") or c.get("created_at") or 0
        ts = ts / 1000 if ts > 1e11 else ts        # ms vs s
        if ts and ts < cutoff:
            continue
        fresh.append(c)
    info(f"{len(fresh)} chat(s) updated in the last {since_days} day(s)")

    found = written = 0
    for c in fresh:
        cid = c.get("id")
        full = api(session, f"{base}/api/v1/chats/{cid}")
        if not full:
            continue
        title = (c.get("title") or "").strip() or "(untitled)"
        for role, text, mid in chat_messages(full):
            if role != "user":          # ONLY your own words are ever captured
                continue
            for line in text.splitlines():
                m = MARKER_RE.match(line)
                if not m:
                    continue
                claim = m.group(1).strip()
                key = hashlib.sha1(f"{cid}:{mid}:{claim}".encode()).hexdigest()[:16]
                if key in seen:
                    continue
                found += 1
                print()
                step(f"from chat '{title}'")
                print(f"  {claim}")
                src = {"chat": title, "url": f"{base}/c/{cid}"}
                if assume_yes:
                    action = "w"
                else:
                    print("\n  [w] write it   [e] edit first   [s] skip   [q] quit")
                    action = (input("  choice? [w/e/s/q] ").strip().lower() or "s")[:1]
                if action == "q":
                    save_seen(folder, seen)
                    return found, written
                if action == "s":
                    seen.add(key)
                    continue
                if action == "e":
                    edited = input("  text: ").strip()
                    if edited:
                        claim = edited
                topic = "" if assume_yes else input("  topic (blank = from the text): ").strip()
                alias = "" if assume_yes else input("  also called (comma separated, optional): ").strip()
                if capture(claim, folder, topic or None, alias or None, src,
                           dry, assume_yes):
                    written += 1
                seen.add(key)
                if limit and written >= limit:
                    save_seen(folder, seen)
                    return found, written
    save_seen(folder, seen)
    return found, written


def main():
    ap = argparse.ArgumentParser(
        description="Capture facts from chats into lab notes the RAG can retrieve.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--note", help="capture this text directly (no chat involved)")
    ap.add_argument("--topic", help="short title / filename basis")
    ap.add_argument("--also-called", help="comma-separated aliases people would search for")
    ap.add_argument("--harvest", action="store_true",
                    help="scan recent chats for lines starting with 'Remember:'")
    ap.add_argument("--since", type=int, default=7, help="days of chat history (default 7)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N captures")
    ap.add_argument("--notes", help="notes folder (default ~/lab_notes or RAG_NOTES_DIR)")
    ap.add_argument("--config", help="a sync .conf to read BASE_URL / KEY_FILE from")
    ap.add_argument("--instance", help="Open WebUI URL (overrides the config)")
    ap.add_argument("--key-file", help="API key file (overrides the config)")
    ap.add_argument("--dry-run", action="store_true", help="show, don't write")
    ap.add_argument("--yes", action="store_true", help="don't ask (conflicts are skipped)")
    a = ap.parse_args()

    conf = load_conf(a.config)
    # NOTES_DIR exists for the case where captures go somewhere other than the synced
    # folder (a captured/ subfolder, say). When it isn't set, fall back to the sync
    # tool's WATCH_DIR: for the usual setup the two are the same folder, and keeping
    # one value in one place is the difference between a note that gets indexed and a
    # note that sits in a directory nothing watches.
    folder = (a.notes or cfg(conf, "NOTES_DIR")
              or cfg(conf, "WATCH_DIR")
              or str(pathlib.Path.home() / "lab_notes"))
    where = ("--notes" if a.notes else
             "NOTES_DIR" if cfg(conf, "NOTES_DIR") else
             "WATCH_DIR" if cfg(conf, "WATCH_DIR") else "the default")
    base = (a.instance or cfg(conf, "BASE_URL", "http://localhost:3000")).rstrip("/")
    key_file = a.key_file or cfg(conf, "KEY_FILE", str(pathlib.Path.home() / ".rag_sync_key"))

    if not a.note and not a.harvest:
        ap.print_help()
        print()
        info("examples:")
        info('  capture_notes.py --note "Polished sections are in cabinet B" \\')
        info('        --topic "sample storage" --also-called "sample cabinet"')
        info("  capture_notes.py --harvest --since 7 --config ~/lab_notes.conf")
        return

    print(f"[capture] notes folder: {folder}   (from {where})")
    if a.harvest:
        print(f"[capture] harvesting chats from: {base}")
    if a.note:
        step("Capturing a note")
        print(f"  {a.note}")
        capture(a.note, folder, a.topic, a.also_called, {"source": "command line"},
                a.dry_run, a.yes)
    if a.harvest:
        session = requests.Session()
        session.headers.update({"Authorization": f"Bearer {read_key(key_file)}"})
        found, written = cmd_harvest(session, base, folder, a.since,
                                     a.dry_run, a.yes, a.limit)
        print()
        if not found:
            info("no new 'Remember:' lines found")
            info("write one in a chat as:  Remember: <the thing worth keeping>")
        else:
            ok(f"{found} marked line(s) seen, {written} note(s) written")
    print()
    info("nothing is retrievable until the notes are indexed:")
    info("  python3 sync_folder.py --config <lab notes conf>")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\n  interrupted")
