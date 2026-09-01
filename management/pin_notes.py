#!/usr/bin/env python3
"""
pin_notes.py — put a folder of short notes into a model preset's SYSTEM PROMPT,
so they are present in every chat without depending on retrieval.

Why this exists: retrieval is a ranking contest. A four-file "how we actually do
it here" collection loses that contest against a 3000-file library unless the
question happens to echo its wording, and with native function calling an
attached knowledge base isn't even injected — the model has to choose to search
it, which smaller models often don't. For guidance that must ALWAYS apply, the
system prompt is the only place that cannot fail.

What it does:
  * reads every note in a folder (.md/.txt/.rst by default)
  * wraps them in a marked block with a precedence line
  * replaces only that block in the preset's system prompt, leaving anything you
    wrote by hand alone
  * shows a diff and asks before saving (--yes to skip, --dry-run to just look)

Usage:
  python3 pin_notes.py --list                       # what presets exist
  python3 pin_notes.py --model qwen3635b-breakerspace --notes ~/lab_notes --dry-run
  python3 pin_notes.py --model qwen3635b-breakerspace --notes ~/lab_notes
  python3 pin_notes.py --model qwen3635b-breakerspace --clear   # remove the block

Config comes from the same places as the sync tool, so you can point it at a
second instance:
  --instance URL        (default http://localhost:3000, or $RAG_BASE_URL)
  --key-file PATH       (default ~/.rag_sync_key, or $RAG_KEY_FILE)

The notes stay in files — this only mirrors them into the preset. Re-run after
editing a note. If the folder is also synced as a Knowledge collection, that's
fine and complementary: the collection makes them searchable, this makes them
unconditional.
"""

__version__ = "2026.08.13.1"

import os
import sys
import json
import difflib
import pathlib
import argparse

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

# The block is fenced by markers so re-runs replace it instead of stacking, and
# anything you write outside the markers is never touched.
BEGIN = "<!-- pinned-notes:begin (managed by pin_notes.py — edits here are overwritten) -->"
END = "<!-- pinned-notes:end -->"

NOTE_EXTS = (".md", ".txt", ".rst")

PREAMBLE = ("The following local notes describe practice at this facility. They take "
            "precedence over vendor documentation and general knowledge wherever they "
            "conflict. Cite the note by name when you rely on it.")

# A system prompt is sent with every message, so a big folder is a real cost.
WARN_CHARS = 8000


def api(session, method, url, **kw):
    """JSON or None. Open WebUI answers unknown paths with 200 + the SPA HTML,
    so a status check alone would treat a missing route as success."""
    try:
        r = session.request(method, url, timeout=30, **kw)
    except Exception as e:
        warn(f"{method} {url}: {e}")
        return None
    if not r.ok:
        warn(f"{method} {url} -> HTTP {r.status_code}: {(r.text or '')[:120]}")
        return None
    if "application/json" not in r.headers.get("Content-Type", ""):
        return None
    try:
        return r.json()
    except ValueError:
        return None


def read_key(key_file):
    p = pathlib.Path(key_file).expanduser()
    env = os.environ.get("RAG_API_KEY")
    if env:
        return env
    if not p.is_file():
        die(f"no API key at {p} — put the sk-… key there (chmod 600) or set RAG_API_KEY")
    return p.read_text().strip()


def get_models(session, base):
    for path in ("/api/v1/models/", "/api/v1/models"):
        d = api(session, "GET", f"{base}{path}")
        items = d.get("data") if isinstance(d, dict) else d
        if isinstance(items, list):
            return items
    die(f"could not list models from {base} — check the URL and the API key")


def find_model(session, base, wanted):
    """Match on id first, then exact name, then case-insensitive name."""
    models = get_models(session, base)
    presets = [m for m in models if isinstance(m, dict) and m.get("info")]
    for m in presets:
        if m.get("id") == wanted:
            return m
    for m in presets:
        if m.get("name") == wanted:
            return m
    for m in presets:
        if (m.get("name") or "").lower() == wanted.lower():
            return m
    names = ", ".join(f"{m.get('id')} ({m.get('name')})" for m in presets) or "none found"
    die(f"no preset matching '{wanted}'. Presets on this instance: {names}")


def cmd_list(session, base):
    step(f"Model presets on {base}")
    models = get_models(session, base)
    presets = [m for m in models if isinstance(m, dict) and m.get("info")]
    if not presets:
        warn("no workspace presets — create one under Workspace → Models")
        info("(base models can't hold a system prompt; only presets can)")
        return
    for m in presets:
        sp = ((m.get("info") or {}).get("params") or {}).get("system", "") or ""
        mark = f"{G}notes pinned{X}" if BEGIN in sp else "            "
        print(f"  {mark}  {m.get('id'):32} {m.get('name')}")
        print(f"                base={((m.get('info') or {}).get('base_model_id'))}"
              f"   system prompt: {len(sp)} chars")


def collect_notes(folder, exts):
    d = pathlib.Path(folder).expanduser()
    if not d.is_dir():
        die(f"not a folder: {d}")
    files = sorted(p for p in d.rglob("*")
                   if p.is_file() and p.suffix.lower() in exts)
    notes = []
    for p in files:
        try:
            text = p.read_text(errors="ignore").strip()
        except OSError as e:
            warn(f"{p.name}: {e}")
            continue
        if not text:
            warn(f"{p.name}: empty, skipped")
            continue
        notes.append((p, text))
    return notes


def build_block(notes, root):
    root = pathlib.Path(root).expanduser()
    parts = [BEGIN, PREAMBLE, ""]
    for p, text in notes:
        try:
            label = p.relative_to(root)
        except ValueError:
            label = p.name
        parts.append(f"### Note: {label}")
        parts.append(text)
        parts.append("")
    parts.append(END)
    return "\n".join(parts)


def splice(system_prompt, block):
    """Replace an existing managed block, else append. Hand-written text survives."""
    sp = system_prompt or ""
    if BEGIN in sp and END in sp:
        head = sp.split(BEGIN)[0]
        tail = sp.split(END, 1)[1]
        return (head.rstrip() + "\n\n" + block + tail).strip() + "\n"
    return (sp.rstrip() + ("\n\n" if sp.strip() else "") + block).strip() + "\n"


def unsplice(system_prompt):
    sp = system_prompt or ""
    if BEGIN not in sp or END not in sp:
        return sp, False
    head = sp.split(BEGIN)[0]
    tail = sp.split(END, 1)[1]
    return (head.rstrip() + "\n" + tail.lstrip()).strip() + "\n", True


def show_diff(old, new):
    diff = list(difflib.unified_diff(
        (old or "").splitlines(), (new or "").splitlines(),
        fromfile="system prompt (before)", tofile="system prompt (after)",
        lineterm="", n=1))
    if not diff:
        info("no change")
        return False
    for line in diff[:60]:
        c = G if line.startswith("+") else (R if line.startswith("-") else "")
        print(f"    {c}{line}{X}" if c else f"    {line}")
    if len(diff) > 60:
        info(f"… {len(diff) - 60} more diff line(s)")
    return True


def save(session, base, model, new_prompt, dry, assume_yes):
    info_obj = dict(model.get("info") or {})
    params = dict(info_obj.get("params") or {})
    params["system"] = new_prompt
    info_obj["params"] = params
    body = {
        "id": model.get("id"),
        "name": model.get("name"),
        "base_model_id": info_obj.get("base_model_id"),
        "meta": info_obj.get("meta") or {},
        "params": params,
    }
    if dry:
        info("[dry-run] not saved")
        return
    if not assume_yes and input("\n  apply to the preset? [y/N] ").strip().lower() \
            not in ("y", "yes"):
        warn("not saved")
        return
    mid = model.get("id")
    for path in (f"/api/v1/models/model/update?id={mid}",
                 f"/api/v1/models/update?id={mid}",
                 f"/api/v1/models/{mid}/update"):
        if api(session, "POST", f"{base}{path}", json=body) is not None:
            # verify by reading it back — a 200 alone is not proof
            fresh = find_model(session, base, mid)
            got = ((fresh.get("info") or {}).get("params") or {}).get("system", "") or ""
            if got.strip() == new_prompt.strip():
                ok(f"saved to preset '{mid}' via {path} (verified)")
                info("open a NEW chat to pick it up; existing chats keep the old prompt")
                return
            warn(f"{path} accepted the update but the prompt did not change")
    bad("could not update the preset through the API on this build")
    info("paste the block into Workspace → Models → <preset> → System Prompt by hand;")
    info("--dry-run prints exactly what to paste")


def main():
    ap = argparse.ArgumentParser(
        description="Pin a folder of notes into a model preset's system prompt.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--list", action="store_true", help="list presets and their prompt sizes")
    ap.add_argument("--model", help="preset id or display name")
    ap.add_argument("--notes", help="folder of notes to pin")
    ap.add_argument("--clear", action="store_true", help="remove the managed block")
    ap.add_argument("--instance", default=os.environ.get("RAG_BASE_URL", "http://localhost:3000"))
    ap.add_argument("--key-file", default=os.environ.get("RAG_KEY_FILE", "~/.rag_sync_key"))
    ap.add_argument("--ext", default=",".join(NOTE_EXTS),
                    help=f"comma-separated extensions (default: {','.join(NOTE_EXTS)})")
    ap.add_argument("--dry-run", action="store_true", help="show the change, don't save")
    ap.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    a = ap.parse_args()

    base = a.instance.rstrip("/")
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {read_key(a.key_file)}"})

    if a.list:
        cmd_list(session, base)
        return
    if not a.model:
        ap.print_help()
        print()
        info("start with:  python3 pin_notes.py --list")
        return

    model = find_model(session, base, a.model)
    current = ((model.get("info") or {}).get("params") or {}).get("system", "") or ""
    step(f"Preset '{model.get('id')}' on {base}")

    if a.clear:
        new, had = unsplice(current)
        if not had:
            info("no managed block present — nothing to clear")
            return
        show_diff(current, new)
        save(session, base, model, new, a.dry_run, a.yes)
        return

    if not a.notes:
        die("--notes FOLDER is required (or use --clear)")
    exts = tuple(e if e.startswith(".") else "." + e
                 for e in (s.strip().lower() for s in a.ext.split(",")) if e)
    notes = collect_notes(a.notes, exts)
    if not notes:
        die(f"no notes found in {a.notes} with extensions {', '.join(exts)}")

    for p, text in notes:
        info(f"{p.name}  ({len(text)} chars)")
    block = build_block(notes, a.notes)
    new = splice(current, block)

    total = len(new)
    print()
    info(f"system prompt: {len(current)} → {total} chars "
         f"(~{total // 4} tokens, sent with EVERY message)")
    if total > WARN_CHARS:
        warn(f"that is large for a system prompt (>{WARN_CHARS} chars). Keep only the "
             "always-relevant notes here and leave the rest to the Knowledge collection.")
    print()
    if show_diff(current, new):
        save(session, base, model, new, a.dry_run, a.yes)


if __name__ == "__main__":
    main()
