#!/usr/bin/env python3
"""
add_folder.py — add another synced folder (and its own Knowledge collection) to an
existing Open WebUI instance, in one command.

Starting from the config of a library that already works (e.g. ~/breakerspace.conf),
it:
  1. creates the folder if needed and reports what in it can be indexed
  2. creates the Knowledge collection on the server — or reuses one with that
     name — with the SAME sharing as the existing library's collection, so users
     who can read that one can read this one
  3. writes the new folder's .conf: same server, key and figure settings, but its
     own WATCH_DIR, TARGET and STATE_FILE (a shared state file would make both
     libraries look like full re-syncs)
  4. attaches the collection to the preset(s) that already use the existing
     library, after showing the change and asking (the preset is backed up first)
  5. prints — or with --sync runs — the first sync

Re-running is safe: an existing collection, config or attachment is reused, not
duplicated.

Usage:
  python3 add_folder.py --name breakerspace_lessons --base-config ~/breakerspace.conf --dry-run
  python3 add_folder.py --name breakerspace_lessons --base-config ~/breakerspace.conf
  python3 add_folder.py --name breakerspace_lessons --base-config ~/breakerspace.conf --sync

Defaults derived from --name (each can be overridden):
  folder  ~/<name>                       --folder PATH
  config  ~/<name>.conf                  --config-out PATH
  state   ~/.rag_sync_state_<name>.json  --state PATH
"""

__version__ = "2026.9.30.1"

import os
import re
import sys
import json
import time
import shutil
import pathlib
import argparse
import subprocess
import collections

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

G, R, Y, B, X = "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[0m"
def ok(m):   print(f"  {G}✔{X} {m}")
def warn(m): print(f"  {Y}!{X} {m}")
def info(m): print(f"  • {m}")
def step(m): print(f"\n{B}==> {m}{X}")
def die(m):  sys.exit(f"  {R}x{X} {m}")

HOME = pathlib.Path.home()

# What sync_folder.py indexes (kept in step with its EXTS / IMAGE_EXTS).
DOC_EXTS = {".pdf", ".txt", ".md", ".rst", ".html", ".htm", ".rtf", ".epub",
            ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls",
            ".odt", ".odp", ".ods", ".csv", ".tsv", ".json"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp", ".gif"}


# ----------------------------------------------------------------- config file
def load_config(path):
    """Same KEY = value format as sync_folder.py (comments, quotes, inline #)."""
    out = {}
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
            v = v[1:v.index(v[0], 1)]
        else:
            v = re.split(r"\s+[#;]", v, maxsplit=1)[0].strip()
        if v.startswith("~") or v.startswith("$") or "/" in v:
            v = os.path.expandvars(os.path.expanduser(v))
        out[k] = v
    return out


def derive_config(base_text, values):
    """The base config with WATCH_DIR / TARGET / STATE_FILE replaced. Every other
    line — server, key, figure settings, comments — is kept exactly as written."""
    lines, seen = [], set()
    for raw in base_text.splitlines():
        m = re.match(r"\s*(?:RAG_)?([A-Za-z_]+)\s*=", raw)
        key = m.group(1).upper() if m else None
        if key in values:
            if key not in seen:
                lines.append(f"{key:<11}= {values[key]}")
                seen.add(key)
            continue                      # drop duplicates of a replaced key
        lines.append(raw)
    missing = [k for k in values if k not in seen]
    if missing:
        lines.append("")
        lines += [f"{k:<11}= {values[k]}" for k in missing]
    return "\n".join(lines).rstrip() + "\n"


def tilde(p):
    p = str(p)
    h = str(HOME)
    return "~" + p[len(h):] if p == h or p.startswith(h + "/") else p


# ------------------------------------------------------------------------ API
def api(session, method, url, quiet=False, **kw):
    """JSON or None. Open WebUI answers unknown GET paths with 200 + the SPA page,
    so only a JSON body counts as an answer."""
    try:
        r = session.request(method, url, timeout=60, **kw)
    except requests.exceptions.ConnectionError:
        die(f"nothing is answering at {url.split('/api/')[0]} — is the container running?")
    except Exception as e:
        if not quiet:
            warn(f"{method} {url}: {e}")
        return None
    if not r.ok:
        if not quiet:
            warn(f"{method} {url} -> HTTP {r.status_code}: {(r.text or '')[:160]}")
        return None
    if "application/json" not in r.headers.get("Content-Type", ""):
        return None
    try:
        return r.json()
    except ValueError:
        return None


def as_list(d):
    """Lists arrive bare, or as {data:[...]}, or as {items:[...]} depending on build."""
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for k in ("data", "items", "knowledge"):
            if isinstance(d.get(k), list):
                return d[k]
    return None


def list_collections(session, base):
    for path in ("/api/v1/knowledge/", "/api/v1/knowledge/list", "/api/v1/knowledge"):
        items = as_list(api(session, "GET", f"{base}{path}", quiet=True))
        if items is not None:
            return [c for c in items if isinstance(c, dict)]
    die(f"could not list Knowledge collections at {base} — check BASE_URL and the API key")


def get_collection(session, base, kid):
    d = api(session, "GET", f"{base}/api/v1/knowledge/{kid}", quiet=True)
    return d if isinstance(d, dict) and d.get("id") else None


def create_collection(session, base, name, description, access_control):
    body = {"name": name, "description": description, "access_control": access_control}
    d = api(session, "POST", f"{base}/api/v1/knowledge/create", json=body)
    if not (isinstance(d, dict) and d.get("id")):
        die("the server did not create the collection — create it in the UI "
            "(Workspace → Knowledge → +) and re-run: an existing name is reused")
    return d


def set_access(session, base, coll, access_control):
    """Some builds ignore access_control on create; apply it again, then verify."""
    fresh = get_collection(session, base, coll["id"]) or coll
    if fresh.get("access_control") == access_control:
        return True
    body = {"name": fresh.get("name"), "description": fresh.get("description") or "",
            "access_control": access_control}
    for path in (f"/api/v1/knowledge/{coll['id']}/update",
                 f"/api/v1/knowledge/{coll['id']}"):
        if api(session, "POST", f"{base}{path}", json=body, quiet=True) is not None:
            again = get_collection(session, base, coll["id"]) or {}
            if again.get("access_control") == access_control:
                return True
    return False


def describe_access(ac):
    if ac is None:
        return "public (every user can read)"
    if not ac:
        return "private (owner and admins only)"
    r = (ac.get("read") or {})
    who = []
    if r.get("group_ids"):
        who.append(f"{len(r['group_ids'])} group(s)")
    if r.get("user_ids"):
        who.append(f"{len(r['user_ids'])} user(s)")
    return "shared with " + (", ".join(who) or "nobody") + " (read)"


def get_presets(session, base):
    for path in ("/api/v1/models/", "/api/v1/models"):
        items = as_list(api(session, "GET", f"{base}{path}", quiet=True))
        if items is not None:
            return [m for m in items if isinstance(m, dict) and m.get("info")]
    return []


def preset_knowledge(m):
    return list(((m.get("info") or {}).get("meta") or {}).get("knowledge") or [])


def knowledge_entry(coll, template):
    """A preset's knowledge list holds collection objects. Build the new entry in
    the same shape as one already there, so this build's UI reads it correctly."""
    if not template:
        return {"id": coll["id"], "name": coll.get("name"),
                "description": coll.get("description") or "", "type": "collection"}
    entry = {}
    for k, v in template.items():
        if k in coll:
            entry[k] = coll[k]
        elif k == "type":
            entry[k] = v                      # e.g. "collection"
        elif k in ("files", "data"):
            entry[k] = [] if isinstance(v, list) else ({} if isinstance(v, dict) else v)
        else:
            entry[k] = None
    entry["id"], entry["name"] = coll["id"], coll.get("name")
    return entry


def attach(session, base, preset, coll, base_target, assume_yes, dry):
    pid = preset.get("id")
    know = preset_knowledge(preset)
    names = [k.get("name") or k.get("id") for k in know]
    if any(k.get("id") == coll["id"] for k in know):
        ok(f"'{preset.get('name')}' already searches {coll.get('name')}")
        return
    print(f"    {preset.get('name')}  ({pid})")
    print(f"      knowledge now:   {', '.join(names) or '(none)'}")
    print(f"      knowledge after: {', '.join(names + [coll.get('name')])}")
    if dry:
        info("[dry-run] preset not changed")
        return
    if not assume_yes and input("    attach? [y/N] ").strip().lower() not in ("y", "yes"):
        warn("not attached — do it in Workspace → Models → edit → Knowledge")
        return

    # Back up the preset first: this rewrites its whole definition.
    backup = HOME / f".add_folder_preset_{re.sub(r'[^A-Za-z0-9_.-]', '_', pid)}_{time.strftime('%Y%m%d-%H%M%S')}.json"
    backup.write_text(json.dumps(preset, indent=2))
    os.chmod(backup, 0o600)

    info_obj = dict(preset.get("info") or {})
    meta = dict(info_obj.get("meta") or {})
    # template: the base library's own entry (a collection, not a single file)
    tmpl = next((k for k in know if k.get("id") == base_target), None) or \
        next((k for k in know if k.get("type") == "collection"), None)
    meta["knowledge"] = know + [knowledge_entry(coll, tmpl)]
    body = {"id": pid, "name": preset.get("name"),
            "base_model_id": info_obj.get("base_model_id"),
            "meta": meta, "params": info_obj.get("params") or {}}
    for k in ("access_control", "is_active"):
        if k in info_obj:
            body[k] = info_obj[k]
    for path in (f"/api/v1/models/model/update?id={pid}",
                 f"/api/v1/models/update?id={pid}",
                 f"/api/v1/models/{pid}/update"):
        if api(session, "POST", f"{base}{path}", json=body, quiet=True) is None:
            continue
        fresh = next((m for m in get_presets(session, base) if m.get("id") == pid), {})
        if any(k.get("id") == coll["id"] for k in preset_knowledge(fresh)):
            ok(f"attached to '{preset.get('name')}' (verified; backup: {tilde(backup)})")
            return
    warn("could not attach through the API on this build — nothing was changed.")
    info("attach by hand: Workspace → Models → edit → Knowledge → select "
         f"'{coll.get('name')}' → Save & Update")


# --------------------------------------------------------------------- folder
def inventory(folder, describe_figures):
    counts, skipped, noext = collections.Counter(), collections.Counter(), []
    for p in folder.rglob("*"):
        if not p.is_file() or p.name.startswith(".") or p.name.startswith("~$"):
            continue
        ext = p.suffix.lower()
        if ext in DOC_EXTS or (describe_figures and ext in IMAGE_EXTS):
            counts[ext] += 1
        elif not ext:
            noext.append(p)
        else:
            skipped[ext] += 1
    return counts, skipped, noext


def overlaps(a, b):
    a, b = a.resolve(), b.resolve()
    return a == b or a in b.parents or b in a.parents


def find_sync_tool():
    here = pathlib.Path(__file__).resolve().parent
    for cand in (here / "sync_folder.py", here.parent / "sync" / "sync_folder.py"):
        if cand.is_file():
            return [sys.executable, str(cand)]
    exe = shutil.which("sync_folder")
    return [exe] if exe else None


# ----------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(
        description="Add another synced folder + Knowledge collection to an instance.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--name", required=True,
                    help="collection name in the UI, e.g. breakerspace_lessons")
    ap.add_argument("--base-config", required=True,
                    help="config of a library that already works on this instance")
    ap.add_argument("--folder", help="folder to sync (default ~/<name>)")
    ap.add_argument("--config-out", help="config to write (default ~/<name>.conf)")
    ap.add_argument("--state", help="state file (default ~/.rag_sync_state_<name>.json)")
    ap.add_argument("--description", default="", help="collection description")
    ap.add_argument("--preset", action="append",
                    help="preset id or name to attach to (repeatable; default: the "
                         "presets that already use the base library)")
    ap.add_argument("--no-attach", action="store_true", help="don't touch any preset")
    ap.add_argument("--sync", action="store_true", help="run the first sync at the end")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen")
    ap.add_argument("--yes", action="store_true", help="don't ask before attaching")
    a = ap.parse_args()

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", a.name):
        die("--name: letters, digits, _ . - only (it also names the files)")
    dry = a.dry_run
    if dry:
        print(f"{Y}[dry-run] nothing is created, written or attached{X}")

    # --- base library ---------------------------------------------------------
    step("Base library")
    base_conf = pathlib.Path(a.base_config).expanduser()
    if not base_conf.is_file():
        die(f"no such config: {base_conf}")
    cfg = load_config(base_conf)
    if str(cfg.get("BACKEND", "openwebui")).lower() != "openwebui":
        die("only Open WebUI libraries are supported")
    base_url = (cfg.get("BASE_URL") or "http://localhost:3000").rstrip("/")
    key_file = pathlib.Path(cfg.get("KEY_FILE") or HOME / ".rag_sync_key").expanduser()
    key = cfg.get("API_KEY") or (key_file.read_text().strip() if key_file.is_file() else "")
    if not key:
        die(f"no API key: {key_file} missing (the base config's KEY_FILE)")
    base_target = cfg.get("TARGET", "")
    base_watch = pathlib.Path(cfg.get("WATCH_DIR") or HOME / "papers").expanduser()
    info(f"config {tilde(base_conf)}  ->  {base_url}")
    info(f"folder {tilde(base_watch)}   collection {base_target or '(none)'}")

    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {key}"

    # --- the new folder -------------------------------------------------------
    folder = pathlib.Path(a.folder or HOME / a.name).expanduser()
    conf_out = pathlib.Path(a.config_out or HOME / f"{a.name}.conf").expanduser()
    state = pathlib.Path(a.state or HOME / f".rag_sync_state_{a.name}.json").expanduser()

    step(f"Folder {tilde(folder)}")
    if overlaps(folder, base_watch):
        die(f"{tilde(folder)} overlaps the base library's folder {tilde(base_watch)}: "
            "its files would be indexed twice. Use a folder outside it.")
    base_state = cfg.get("STATE_FILE") or str(HOME / ".rag_sync_state.json")
    if state.resolve() == pathlib.Path(base_state).expanduser().resolve():
        die("--state must differ from the base library's STATE_FILE")
    if folder.is_dir():
        describe = str(cfg.get("DESCRIBE_FIGURES", "")).lower() in ("1", "true", "yes", "on")
        counts, skipped, noext = inventory(folder, describe)
        total = sum(counts.values())
        ok(f"{total} indexable file(s)" +
           (": " + ", ".join(f"{n} {e}" for e, n in counts.most_common()) if total else ""))
        if skipped:
            warn("not indexed (unsupported type): " +
                 ", ".join(f"{n} {e}" for e, n in skipped.most_common()))
        for p in noext[:5]:
            warn(f"no extension, so not indexed: {p.relative_to(folder)} "
                 "(rename it, e.g. add .txt)")
        if len(noext) > 5:
            warn(f"… and {len(noext) - 5} more without an extension")
        if not total:
            info("empty for now — add documents, then run the sync")
    elif dry:
        info("[dry-run] would create it (empty)")
    else:
        folder.mkdir(parents=True)
        ok("created (empty) — put the documents here")

    # --- the collection -------------------------------------------------------
    step(f"Collection '{a.name}' on {base_url}")
    access = None
    base_coll = get_collection(s, base_url, base_target) if base_target else None
    if base_coll is not None:
        access = base_coll.get("access_control")
        info(f"sharing copied from '{base_coll.get('name')}': {describe_access(access)}")
    else:
        access = {}
        warn("couldn't read the base collection — the new one will be private; "
             "share it under Workspace → Knowledge")

    same_name = [c for c in list_collections(s, base_url) if c.get("name") == a.name]
    if len(same_name) > 1:
        die(f"{len(same_name)} collections are named '{a.name}' — rename or delete the "
            "extras in the UI first, so the config points at the right one")
    if same_name:
        coll = same_name[0]
        ok(f"already exists, reusing it: {coll['id']}")
    elif dry:
        coll = {"id": "<new-collection-id>", "name": a.name}
        info("[dry-run] would create it")
    else:
        coll = create_collection(s, base_url, a.name, a.description, access)
        ok(f"created: {coll['id']}")
    if not dry and not same_name:
        if set_access(s, base_url, coll, access):
            ok(f"sharing: {describe_access(access)}")
        else:
            warn("couldn't apply the sharing — set it in Workspace → Knowledge → "
                 f"{a.name} → Access, to match '{(base_coll or {}).get('name', 'the base')}'")

    # --- the config -----------------------------------------------------------
    step(f"Config {tilde(conf_out)}")
    values = {"WATCH_DIR": tilde(folder), "TARGET": coll["id"], "STATE_FILE": tilde(state)}
    if conf_out.is_file():
        existing = load_config(conf_out)
        if existing.get("TARGET") == coll["id"]:
            ok("already exists and points at this collection — left as is")
        else:
            die(f"{tilde(conf_out)} exists but targets {existing.get('TARGET') or 'nothing'}"
                " — move it aside or choose --config-out")
    else:
        text = derive_config(base_conf.read_text(errors="ignore"), values)
        text = (f"# {conf_out.name} — written by add_folder.py from {base_conf.name}\n"
                f"# Same server, key and figure settings; its own folder, collection "
                f"and state.\n\n") + text
        if dry:
            for k, v in values.items():
                info(f"[dry-run] {k:<10} = {v}")
        else:
            conf_out.write_text(text)
            ok("written: " + ", ".join(f"{k} = {v}" for k, v in values.items()))
            info(f"everything else copied from {base_conf.name} "
                 "(BASE_URL, KEY_FILE, PRUNE, figure settings)")

    # --- presets --------------------------------------------------------------
    if not a.no_attach:
        step("Presets")
        presets = get_presets(s, base_url)
        if a.preset:
            chosen = []
            for want in a.preset:
                m = next((p for p in presets if p.get("id") == want), None) or \
                    next((p for p in presets if (p.get("name") or "").lower() == want.lower()), None)
                if not m:
                    die(f"no preset '{want}'. Presets: " +
                        ", ".join(f"{p.get('id')} ({p.get('name')})" for p in presets))
                chosen.append(m)
        else:
            chosen = [p for p in presets
                      if any(k.get("id") == base_target for k in preset_knowledge(p))]
            if not chosen:
                info("no preset uses the base library — pass --preset <id>, or attach "
                     "it in Workspace → Models")
        for p in chosen:
            attach(s, base_url, p, coll, base_target, a.yes, dry)
        if chosen:
            info("the preset now retrieves from one more collection; ask a question "
                 "you know is in the new folder to check it's found")

    # --- first sync -----------------------------------------------------------
    step("First sync")
    tool = find_sync_tool()
    cmd = (tool or ["sync_folder"]) + ["--config", str(conf_out)]
    shown = " ".join("python3" if c == sys.executable else tilde(c) for c in cmd)
    if a.sync and not dry:
        if not tool:
            die("sync_folder.py not found next to this script or on PATH; run: " + shown)
        print(f"  $ {shown}\n")
        sys.exit(subprocess.call(cmd))
    info(f"run:  {shown}")
    info("figure captioning uses the GPU — on a shared card, run it when nobody's chatting")
    info(f"status any time:  {shown} --status")


if __name__ == "__main__":
    main()
