#!/usr/bin/env python3
"""
migrate_rag.py — move a whole local RAG stack to another machine.

What actually has to travel:
  * the Open WebUI docker VOLUME(s) — accounts, settings, the Knowledge
    collections AND the stored vectors. Copy this and nothing needs re-indexing.
  * the AnythingLLM storage directory (if you use it)
  * the DOCUMENT folders each library syncs from
  * the sync side files: <lib>.conf, the API key file, the state file
  * the LIST of Ollama models — the models themselves are re-pulled, never copied:
    the DGX Spark runs a custom Blackwell build while a generic host runs stock
    Ollama, so the runtime is not portable even though the weights are.

Two commands, no network between the machines:

    python3 migrate_rag.py --export            # on the OLD machine
    <copy the output folder across by any means>
    python3 migrate_rag.py --import            # on the NEW machine

Everything specific to your setup lives in migrate_rag.conf
(`--init-config` writes a commented starter).

THE TRAP THIS TOOL EXISTS FOR: sync state is keyed on ABSOLUTE paths. If the
documents land under a different path on the new machine, every entry misses,
the next sync treats all files as new, and you get a duplicate of the entire
library on top of the restored collection. On import this script detects the
change, shows you the rewrite, and asks before applying it.

Usage:
  python3 migrate_rag.py --init-config
  python3 migrate_rag.py --export [--config FILE] [--dry-run]
  python3 migrate_rag.py --import [--config FILE] [--dry-run] [--yes]
  python3 migrate_rag.py --verify            # after import: what's in place?
"""

__version__ = "2026.08.03.2"

import os
import re
import sys
import json
import time
import shutil
import pathlib
import argparse
import subprocess

G, R, Y, B, X = "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[0m"
def ok(m):   print(f"  {G}✔{X} {m}")
def bad(m):  print(f"  {R}x{X} {m}")
def warn(m): print(f"  {Y}!{X} {m}")
def info(m): print(f"  • {m}")
def step(m): print(f"\n{B}==> {m}{X}")
def die(m):  sys.exit(f"  {R}x{X} {m}")

MANIFEST = "manifest.json"

CONFIG_TEMPLATE = """# migrate_rag.conf — what to move, and where it lands.
#
# Fill this in on the OLD machine, copy it along with the archives, then adjust
# the NEW_* entries on the new machine before importing.
# Lists are comma-separated. '#' starts a comment. '~' is expanded.

# --- what to export ------------------------------------------------------
# Docker volumes holding Open WebUI data (one per instance). Find them with:
#     docker ps --format '{{.Names}}'   and   docker volume ls
VOLUMES     = open-webui, open-webui-breakerspace

# AnythingLLM storage directory (leave empty if you don't run it)
ANYTHINGLLM_DIR =

# Document folders the libraries sync from (comma-separated)
DOC_DIRS    = ~/papers, ~/breakerspace

# Sync side files: config files, key files, state files.
# Globs are allowed; missing entries are skipped with a warning.
SYNC_FILES  = ~/Software/Local_RAG_LLM/sync/*.conf, ~/.rag_sync_key*, ~/.rag_sync_state*.json

# Where the export writes its archives (must have room for the volumes)
EXPORT_DIR  = ~/rag_migration

# --- import-side settings (edit these on the NEW machine) ----------------
# Leave NEW_HOME empty to keep the same absolute paths. Set it when the new
# machine has a different user/home, e.g. /home/nicola — the importer then
# rewrites the sync state keys and the .conf entries accordingly.
NEW_HOME    =

# Optional: move the documents somewhere else entirely on the new machine.
# Format: OLD_PATH=>NEW_PATH , one per comma-separated entry.
# PATH_MAP  = /home/feranick/breakerspace=>/data/breakerspace

# Re-pull these models on the new machine. Leave AUTO to use the list captured
# at export time (recommended — it records exactly what was installed).
MODELS      = AUTO
"""


# ----------------------------- config plumbing ---------------------------
def find_config(explicit=None):
    if explicit:
        p = pathlib.Path(explicit).expanduser()
        if not p.is_file():
            die(f"config file not found: {p}")
        return p
    for c in (pathlib.Path.cwd() / "migrate_rag.conf",
              pathlib.Path(__file__).resolve().parent / "migrate_rag.conf"):
        if c.is_file():
            return c
    return None


def load_config(path):
    cfg = {}
    if not path:
        return cfg
    for raw in path.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if not line or line[0] in "#;[" or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split("#")[0].strip().strip('"').strip("'")
        cfg[k.strip().upper()] = os.path.expandvars(v)
    return cfg


def cfg_list(cfg, key):
    return [s.strip() for s in cfg.get(key, "").split(",") if s.strip()]


def expand(p):
    return pathlib.Path(p).expanduser()


# ----------------------------- docker helpers ----------------------------
def run(cmd, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, **kw)
    except (FileNotFoundError, OSError):
        class _R:
            returncode, stdout, stderr = 127, "", "command not found"
        return _R()


def docker_cmd(required=True):
    if shutil.which("docker"):
        if run(["docker", "info"]).returncode == 0:
            return ["docker"]
        if run(["sudo", "docker", "info"]).returncode == 0:
            return ["sudo", "docker"]
    if required:
        die("docker is not available (not installed, or needs sudo without a TTY)")
    return ["docker"]


def volume_exists(DOCKER, name):
    return run(DOCKER + ["volume", "inspect", name]).returncode == 0


def containers_using(DOCKER, volume):
    r = run(DOCKER + ["ps", "-q", "--filter", f"volume={volume}"])
    return [c for c in r.stdout.split() if c]


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.1f} {u}"
        n /= 1024


# ----------------------------- export ------------------------------------
def export_volume(DOCKER, name, out_dir, dry):
    """tar a docker volume into out_dir/<name>.tgz using a throwaway container."""
    tgz = out_dir / f"volume__{name}.tgz"
    cmd = DOCKER + ["run", "--rm",
                    "-v", f"{name}:/data:ro",
                    "-v", f"{out_dir}:/backup",
                    "alpine", "tar", "czf", f"/backup/{tgz.name}", "-C", "/data", "."]
    if dry:
        info("would run: " + " ".join(cmd))
        return None
    r = run(cmd)
    if r.returncode != 0:
        bad(f"failed to archive volume {name}: {r.stderr.strip()[:200]}")
        return None
    ok(f"volume {name} → {tgz.name} ({human(tgz.stat().st_size)})")
    return tgz.name


def export_tree(src, out_dir, label, dry):
    """tar a directory tree (documents, AnythingLLM storage)."""
    src = expand(src)
    if not src.is_dir():
        warn(f"{label}: not a directory, skipped ({src})")
        return None
    tgz = out_dir / f"{label}__{src.name}.tgz"
    if dry:
        info(f"would archive {src} → {tgz.name}")
        return None
    r = run(["tar", "czf", str(tgz), "-C", str(src.parent), src.name])
    if r.returncode != 0:
        bad(f"failed to archive {src}: {r.stderr.strip()[:200]}")
        return None
    ok(f"{src} → {tgz.name} ({human(tgz.stat().st_size)})")
    return {"archive": tgz.name, "source": str(src)}


def export_files(patterns, out_dir, dry):
    """Copy the small side files, preserving their absolute paths in the manifest."""
    files_dir = out_dir / "files"
    if not dry:
        files_dir.mkdir(exist_ok=True)
    recorded = []
    for pat in patterns:
        pat = os.path.expanduser(pat)
        base = pathlib.Path(pat)
        matches = (sorted(base.parent.glob(base.name))
                   if any(ch in base.name for ch in "*?[") else
                   ([base] if base.exists() else []))
        if not matches:
            warn(f"no match for {pat}")
            continue
        for m in matches:
            if not m.is_file():
                continue
            flat = str(m).replace("/", "__")
            if dry:
                info(f"would copy {m}")
            else:
                shutil.copy2(m, files_dir / flat)
                ok(f"{m}")
            recorded.append({"source": str(m), "stored": flat,
                             "mode": oct(m.stat().st_mode & 0o777)})
    return recorded


def capture_models():
    """Record installed Ollama models so the new machine can re-pull them."""
    r = run(["ollama", "list"])
    if r.returncode != 0:
        warn("could not run `ollama list` — model list not captured")
        return []
    names = []
    for line in r.stdout.splitlines()[1:]:
        parts = line.split()
        if parts:
            names.append(parts[0])
    return names


def cmd_export(cfg, dry):
    vols = cfg_list(cfg, "VOLUMES")
    # Docker is only needed for volumes — documents and side files don't require it
    DOCKER = docker_cmd(required=bool(vols) and not dry)
    out_dir = expand(cfg.get("EXPORT_DIR", "~/rag_migration"))
    step(f"Export → {out_dir}")
    if not dry:
        out_dir.mkdir(parents=True, exist_ok=True)

    manifest = {"version": __version__, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source_host": os.uname().nodename, "source_home": str(pathlib.Path.home()),
                "volumes": {}, "trees": [], "files": [], "models": [],
                "containers": {}}

    step("1/5  Docker volumes")
    if not vols:
        warn("no VOLUMES configured — the collections and their vectors will NOT move")
    for v in vols:
        if not dry and not volume_exists(DOCKER, v):
            bad(f"volume '{v}' does not exist — skipping")
            continue
        running = containers_using(DOCKER, v) if not dry else []
        if running:
            warn(f"volume {v} is in use by a running container.")
            warn("  Archiving a live SQLite database can capture a torn state.")
            warn(f"  Stop it first:  {' '.join(DOCKER)} stop <container>")
            if input("  archive anyway? [y/N] ").strip().lower() not in ("y", "yes"):
                continue
        name = export_volume(DOCKER, v, out_dir, dry)
        if name:
            manifest["volumes"][v] = name
            insp = run(DOCKER + ["inspect", v, "--format", "{{json .Config}}"])
            if insp.returncode == 0 and insp.stdout.strip():
                manifest["containers"][v] = insp.stdout.strip()[:4000]

    step("2/5  Document folders")
    for d in cfg_list(cfg, "DOC_DIRS"):
        t = export_tree(d, out_dir, "docs", dry)
        if t:
            manifest["trees"].append({**t, "kind": "docs"})

    step("3/5  AnythingLLM storage")
    a = cfg.get("ANYTHINGLLM_DIR", "").strip()
    if a:
        t = export_tree(a, out_dir, "anythingllm", dry)
        if t:
            manifest["trees"].append({**t, "kind": "anythingllm"})
    else:
        info("not configured — skipped")

    step("4/5  Sync config / key / state files")
    manifest["files"] = export_files(cfg_list(cfg, "SYNC_FILES"), out_dir, dry)

    step("5/5  Installed Ollama models (recorded, not copied)")
    manifest["models"] = capture_models()
    for m in manifest["models"]:
        info(m)

    if dry:
        print("\n[dry-run] nothing was written.\n")
        return

    (out_dir / MANIFEST).write_text(json.dumps(manifest, indent=2))
    cfgsrc = find_config(None)
    if cfgsrc:
        shutil.copy2(cfgsrc, out_dir / "migrate_rag.conf")

    total = sum(f.stat().st_size for f in out_dir.rglob("*") if f.is_file())
    print(f"\n{B}================ EXPORT DONE ================{X}")
    print(f"  folder : {out_dir}   ({human(total)})")
    print(f"  volumes: {len(manifest['volumes'])}   trees: {len(manifest['trees'])}   "
          f"files: {len(manifest['files'])}   models recorded: {len(manifest['models'])}")
    print(f"\n  Copy the whole folder to the new machine, e.g.:")
    print(f"      rsync -avP {out_dir}/ newhost:{out_dir}/")
    print(f"\n  Then on the new machine:")
    print(f"      python3 migrate_rag.py --import --config {out_dir}/migrate_rag.conf\n")


# ----------------------------- import ------------------------------------
def build_path_map(cfg, manifest):
    """{old_prefix: new_prefix} from NEW_HOME and/or explicit PATH_MAP entries."""
    m = {}
    old_home = manifest.get("source_home", "")
    new_home = cfg.get("NEW_HOME", "").strip()
    if new_home and old_home and expand(new_home) != pathlib.Path(old_home):
        m[old_home.rstrip("/")] = str(expand(new_home)).rstrip("/")
    for entry in cfg_list(cfg, "PATH_MAP"):
        if "=>" in entry:
            o, n = entry.split("=>", 1)
            m[o.strip().rstrip("/")] = str(expand(n.strip())).rstrip("/")
    return m


def remap(path_str, path_map):
    for old, new in path_map.items():
        if path_str == old or path_str.startswith(old + "/"):
            return new + path_str[len(old):]
    return path_str


def rewrite_state_file(p, path_map, dry, assume_yes, label=None):
    """Rewrite the absolute-path keys inside a sync state file."""
    label = label or p.name
    try:
        data = json.loads(p.read_text())
    except Exception as e:
        warn(f"{label}: not readable as JSON ({e}) — left untouched")
        return
    files = data.get("files")
    if not isinstance(files, dict):
        return
    changed = {k: remap(k, path_map) for k in files}
    n = sum(1 for k, v in changed.items() if k != v)
    if not n:
        info(f"{label}: {len(files)} entries, no path change needed")
        return
    print(f"  {label}: {n} of {len(files)} entries need rewriting, e.g.")
    for k, v in list((k, v) for k, v in changed.items() if k != v)[:2]:
        print(f"      {k}\n   -> {v}")
    if dry:
        info("[dry-run] not written")
        return
    if not assume_yes and input("  apply? [Y/n] ").strip().lower() in ("n", "no"):
        warn("skipped — the next sync will treat those files as new (duplicates!)")
        return
    data["files"] = {changed[k]: v for k, v in files.items()}
    shutil.copy2(p, p.with_suffix(p.suffix + ".bak"))
    p.write_text(json.dumps(data, indent=2))
    ok(f"{label}: {n} path(s) rewritten (backup: {p.name}.bak)")


CONF_PATH_KEYS = ("WATCH_DIR", "KEY_FILE", "STATE_FILE")


def rewrite_conf_file(p, path_map, dry, assume_yes, label=None):
    """Rewrite path-valued entries inside a sync_folder .conf file."""
    label = label or p.name
    try:
        lines = p.read_text().splitlines()
    except OSError as e:
        warn(f"{label}: {e}")
        return
    out, changes = [], []
    for line in lines:
        m = re.match(r"^(\s*([A-Za-z_]+)\s*=\s*)(.*)$", line)
        if m and m.group(2).upper() in CONF_PATH_KEYS:
            head, val = m.group(1), m.group(3)
            comment = ""
            if "#" in val:
                val, comment = val.split("#", 1)
                comment = "#" + comment
            new = remap(val.strip(), path_map)
            if new != val.strip():
                changes.append((line.strip(), f"{head}{new} {comment}".rstrip()))
                out.append(f"{head}{new} {comment}".rstrip())
                continue
        out.append(line)
    if not changes:
        info(f"{label}: no path change needed")
        return
    print(f"  {label}:")
    for oldl, newl in changes:
        print(f"      - {oldl}\n      + {newl.strip()}")
    if dry:
        info("[dry-run] not written")
        return
    if not assume_yes and input("  apply? [Y/n] ").strip().lower() in ("n", "no"):
        warn("skipped")
        return
    shutil.copy2(p, p.with_suffix(p.suffix + ".bak"))
    p.write_text("\n".join(out) + "\n")
    ok(f"{label}: {len(changes)} line(s) rewritten (backup: {p.name}.bak)")


def cmd_import(cfg, dry, assume_yes):
    src = expand(cfg.get("EXPORT_DIR", "~/rag_migration"))
    step(f"Import ← {src}")
    mf = src / MANIFEST
    if not mf.is_file():
        die(f"no {MANIFEST} in {src} — point EXPORT_DIR at the folder you copied over")
    manifest = json.loads(mf.read_text())
    # only needed if the export actually contains volumes
    DOCKER = docker_cmd(required=bool(manifest.get("volumes")) and not dry)
    info(f"exported {manifest.get('created')} from {manifest.get('source_host')} "
         f"(home {manifest.get('source_home')})")

    path_map = build_path_map(cfg, manifest)
    if path_map:
        for o, n in path_map.items():
            info(f"path rewrite: {o}  ->  {n}")
    else:
        info("no path rewriting (same absolute paths as the source machine)")

    step("1/5  Docker volumes")
    for vol, arch in manifest.get("volumes", {}).items():
        tgz = src / arch
        if not tgz.is_file():
            bad(f"{arch} missing from {src}")
            continue
        if dry:
            info(f"would restore {arch} → volume {vol}")
            continue
        if volume_exists(DOCKER, vol):
            warn(f"volume '{vol}' already exists on this machine.")
            if not assume_yes and input("  overwrite its contents? [y/N] ").strip().lower() \
                    not in ("y", "yes"):
                info("skipped")
                continue
            if containers_using(DOCKER, vol):
                bad(f"a running container is using {vol} — stop it first; skipped")
                continue
        else:
            run(DOCKER + ["volume", "create", vol])
        r = run(DOCKER + ["run", "--rm", "-v", f"{vol}:/data", "-v", f"{src}:/backup",
                          "alpine", "sh", "-c",
                          f"rm -rf /data/* /data/..?* 2>/dev/null; "
                          f"tar xzf /backup/{arch} -C /data"])
        if r.returncode != 0:
            bad(f"restore of {vol} failed: {r.stderr.strip()[:200]}")
        else:
            ok(f"volume {vol} restored from {arch}")

    step("2/5  Document folders and AnythingLLM storage")
    for t in manifest.get("trees", []):
        tgz = src / t["archive"]
        dest_src = remap(t["source"], path_map)
        dest_parent = pathlib.Path(dest_src).parent
        if not tgz.is_file():
            bad(f"{t['archive']} missing")
            continue
        if dry:
            info(f"would extract {t['archive']} → {dest_parent}")
            continue
        dest_parent.mkdir(parents=True, exist_ok=True)
        r = run(["tar", "xzf", str(tgz), "-C", str(dest_parent)])
        if r.returncode != 0:
            bad(f"extract failed for {t['archive']}: {r.stderr.strip()[:200]}")
            continue
        ok(f"{t['archive']} → {dest_src}")
        if t.get("kind") == "anythingllm":
            info("AnythingLLM runs as UID 1000 — fixing ownership")
            run(["sudo", "chown", "-R", "1000:1000", dest_src])

    step("3/5  Sync config / key / state files")
    restored = []
    for f in manifest.get("files", []):
        stored = src / "files" / f["stored"]
        dest = pathlib.Path(remap(f["source"], path_map))
        if not stored.is_file():
            bad(f"{f['stored']} missing from the archive")
            continue
        if dry:
            info(f"would restore {dest}")
            # preview the rewrite against the archived copy, since the
            # destination doesn't exist yet in a dry run
            restored.append((dest, stored))
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.copy2(dest, dest.with_suffix(dest.suffix + ".before-migration"))
        shutil.copy2(stored, dest)
        try:
            dest.chmod(int(f.get("mode", "0o600"), 8))
        except (ValueError, OSError):
            pass
        ok(f"{dest}")
        restored.append((dest, dest))

    step("4/5  Rewriting absolute paths")
    if not path_map:
        info("nothing to rewrite")
    else:
        for dest, readable in restored:
            if dest.name.endswith(".json") and "rag_sync_state" in dest.name:
                rewrite_state_file(readable, path_map, dry, assume_yes, label=dest.name)
            elif dest.suffix == ".conf":
                rewrite_conf_file(readable, path_map, dry, assume_yes, label=dest.name)

    step("5/5  Ollama models to re-pull")
    models = cfg_list(cfg, "MODELS")
    if not models or models == ["AUTO"]:
        models = manifest.get("models", [])
    if not models:
        warn("no model list available")
    else:
        info("the weights are NOT copied — the runtime differs between machines")
        for m in models:
            print(f"      ollama pull {m}")
        if not dry and not assume_yes and shutil.which("ollama") and \
                input("\n  pull them now? [y/N] ").strip().lower() in ("y", "yes"):
            for m in models:
                subprocess.run(["ollama", "pull", m])

    print(f"\n{B}================ IMPORT DONE ================{X}")
    print("  Next:")
    print("   1. start the containers (same `docker run` as before — see")
    print("      new_rag_instance.py, and keep --add-host=host.docker.internal:host-gateway)")
    print("   2. python3 migrate_rag.py --verify")
    print("   3. python3 sync_folder.py --config <lib>.conf --status")
    print("      -> 'to go' should be ~0. A number close to the whole library means")
    print("         the path rewrite did not apply; fix it BEFORE syncing or you will")
    print("         duplicate everything.\n")


# ----------------------------- verify ------------------------------------
def cmd_verify(cfg):
    DOCKER = docker_cmd(required=False)
    step("Verifying the migrated stack")
    # The config lists the OLD machine's paths, so apply the same rewrite the
    # import used — otherwise this checks locations that only existed there.
    src = expand(cfg.get("EXPORT_DIR", "~/rag_migration"))
    manifest = {}
    if (src / MANIFEST).is_file():
        try:
            manifest = json.loads((src / MANIFEST).read_text())
        except Exception:
            pass
    path_map = build_path_map(cfg, manifest)
    if path_map:
        for o, n in path_map.items():
            info(f"checking with rewrite {o} -> {n}")

    for v in cfg_list(cfg, "VOLUMES"):
        (ok if volume_exists(DOCKER, v) else bad)(f"volume {v}")
    for d in cfg_list(cfg, "DOC_DIRS"):
        p = pathlib.Path(remap(str(expand(d)), path_map))
        if p.is_dir():
            n = sum(1 for _ in p.rglob("*") if _.is_file())
            ok(f"{p}  ({n} files)")
        else:
            bad(f"{p} missing")
    for pat in cfg_list(cfg, "SYNC_FILES"):
        base = pathlib.Path(remap(os.path.expanduser(pat), path_map))
        got = (sorted(base.parent.glob(base.name))
               if any(c in base.name for c in "*?[") else
               ([base] if base.exists() else []))
        (ok if got else bad)(f"{base}  ({len(got)} file(s))")
    r = run(["ollama", "list"])
    if r.returncode == 0:
        n = max(0, len(r.stdout.strip().splitlines()) - 1)
        ok(f"ollama reachable, {n} model(s) installed")
    else:
        bad("ollama not reachable")
    print("\n  Then the real test — a sync must see the library as already done:")
    print("      python3 sync_folder.py --config <lib>.conf --status\n")


def main():
    ap = argparse.ArgumentParser(
        description="Migrate a local RAG stack (volumes, documents, sync state) "
                    "to another machine.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--init-config", action="store_true",
                    help="write a starter migrate_rag.conf")
    ap.add_argument("--export", action="store_true", help="archive everything (old machine)")
    ap.add_argument("--import", dest="do_import", action="store_true",
                    help="restore everything (new machine)")
    ap.add_argument("--verify", action="store_true", help="check what's in place after an import")
    ap.add_argument("--config", help="path to migrate_rag.conf")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen")
    ap.add_argument("--yes", action="store_true", help="assume yes for all confirmations")
    a = ap.parse_args()

    if a.init_config:
        dest = pathlib.Path(a.config).expanduser() if a.config else \
            pathlib.Path.cwd() / "migrate_rag.conf"
        if dest.exists():
            die(f"{dest} already exists — delete it first")
        dest.write_text(CONFIG_TEMPLATE)
        ok(f"wrote {dest}")
        info("edit it, then run:  python3 migrate_rag.py --export")
        return

    if not any([a.export, a.do_import, a.verify]):
        ap.print_help()
        return

    path = find_config(a.config)
    if not path:
        die("no migrate_rag.conf found — create one with --init-config")
    print(f"[migrate] v{__version__} | config: {path}")
    cfg = load_config(path)

    if a.export:
        cmd_export(cfg, a.dry_run)
    if a.do_import:
        cmd_import(cfg, a.dry_run, a.yes)
    if a.verify:
        cmd_verify(cfg)


if __name__ == "__main__":
    main()
