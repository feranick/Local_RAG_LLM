#!/usr/bin/env python3
"""
new_rag_instance.py — stand up a SECOND, fully configured Open WebUI instance for
an independent library with its own embedding model.

Why a separate instance: in Open WebUI the embedding model is a global setting, so
two collections in one instance must share it. A second container gets its own
data volume — own settings, own accounts, own collections, own embedding model —
while sharing your existing Ollama (and therefore the same chat models).

What this does, end to end:
  1. pulls the embedding model in Ollama (unless present)
  2. launches a new container with every relevant setting pre-applied as env vars,
     so there is nothing to hunt for in the UI afterwards
  3. waits for it to come up, creates the admin account
  4. enables + creates an API key
  5. creates the Knowledge collection and captures its id
  6. writes the key file AND a matching sync_folder config file, so syncing the
     new library is a single command

Requires: docker, requests (pip install requests), a running Ollama.

Usage:
  python3 new_rag_instance.py --collection Reports --embed-model bge-m3 \
      --watch-dir ~/reports --email me@example.com --password 'secret123'

  python3 new_rag_instance.py --dry-run          # show the docker command only
"""

__version__ = "2026.07.31.1"

import os
import sys
import time
import shutil
import getpass
import pathlib
import argparse
import subprocess

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


def run(cmd, **kw):
    """subprocess.run that tolerates a missing binary."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, **kw)
    except (FileNotFoundError, OSError):
        class _R:  # mimic CompletedProcess
            returncode, stdout, stderr = 127, "", "command not found"
        return _R()


def docker_cmd(required=True):
    if shutil.which("docker"):
        if run(["docker", "info"]).returncode == 0:
            return ["docker"]
        if run(["sudo", "docker", "info"]).returncode == 0:
            return ["sudo", "docker"]
    if required:
        die("Docker is not available (not installed, or needs sudo without a TTY).")
    return ["docker"]        # placeholder for --dry-run


def main():
    ap = argparse.ArgumentParser(description="Create a second, pre-configured Open WebUI instance.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--name", default="open-webui-2", help="container name (default: open-webui-2)")
    ap.add_argument("--port", type=int, default=3002, help="host port (default: 3002)")
    ap.add_argument("--embed-model", default="bge-m3",
                    help="embedding model for THIS library (default: bge-m3)")
    ap.add_argument("--collection", default="Library2", help="Knowledge collection name")
    ap.add_argument("--watch-dir", default=None,
                    help="folder this library will sync from (written into the config file)")
    ap.add_argument("--email", default=None, help="admin account email")
    ap.add_argument("--password", default=None, help="admin account password (prompted if omitted)")
    ap.add_argument("--admin-name", default="Admin", help="admin display name")
    ap.add_argument("--chunk-size", type=int, default=1500)
    ap.add_argument("--chunk-overlap", type=int, default=200)
    ap.add_argument("--batch-size", type=int, default=32,
                    help="embedding batch size (default 32; avoids 'too many open files')")
    ap.add_argument("--ollama-url", default="http://host.docker.internal:11434",
                    help="how the CONTAINER reaches Ollama (leave as-is)")
    ap.add_argument("--scripts-dir", default=".", help="where to write the generated .conf")
    ap.add_argument("--skip-pull", action="store_true", help="don't pull the embedding model")
    ap.add_argument("--dry-run", action="store_true", help="print the docker command and exit")
    a = ap.parse_args()

    DOCKER = docker_cmd(required=not a.dry_run)
    base = f"http://localhost:{a.port}"
    volume = a.name.replace("/", "_")
    key_file = pathlib.Path.home() / f".rag_sync_key_{volume}"
    state_file = pathlib.Path.home() / f".rag_sync_state_{volume}.json"

    # ---------- 1. embedding model ----------
    step("1/6  Embedding model")
    if a.skip_pull:
        warn("--skip-pull: not touching Ollama")
    elif shutil.which("ollama") is None:
        warn("ollama CLI not found — make sure the model exists on the server")
    else:
        have = run(["ollama", "list"]).stdout
        if any(l.split()[:1] == [a.embed_model] for l in have.splitlines()[1:] if l.split()):
            ok(f"{a.embed_model} already present")
        else:
            info(f"pulling {a.embed_model} …")
            if subprocess.run(["ollama", "pull", a.embed_model]).returncode == 0:
                ok(f"{a.embed_model} pulled")
            else:
                warn(f"could not pull {a.embed_model} — continuing anyway")

    # ---------- 2. launch the container ----------
    step(f"2/6  Container '{a.name}' on port {a.port}")
    gpu = []
    if run(DOCKER + ["run", "--rm", "--gpus", "all",
                     "nvidia/cuda:12.6.0-base-ubuntu24.04", "true"]).returncode == 0:
        gpu = ["--gpus", "all"]

    docker_run = DOCKER + [
        "run", "-d", "--name", a.name, "--restart", "always",
        "-p", f"{a.port}:8080", *gpu,
        "--ulimit", "nofile=65536:65536",
        "--add-host=host.docker.internal:host-gateway",
        "-v", f"{volume}:/app/backend/data",
        # --- everything below is pre-configured so you never hunt in the UI ---
        "-e", f"OLLAMA_BASE_URL={a.ollama_url}",
        "-e", f"RAG_OLLAMA_BASE_URL={a.ollama_url}",
        "-e", "RAG_EMBEDDING_ENGINE=ollama",
        "-e", f"RAG_EMBEDDING_MODEL={a.embed_model}",
        "-e", f"RAG_EMBEDDING_BATCH_SIZE={a.batch_size}",
        "-e", f"CHUNK_SIZE={a.chunk_size}",
        "-e", f"CHUNK_OVERLAP={a.chunk_overlap}",
        "-e", "ENABLE_API_KEY=true",
        "-e", "ENABLE_SIGNUP=true",          # needed to create the first account
        "-e", "DEFAULT_USER_ROLE=pending",   # later signups need approval
        "-e", f"WEBUI_NAME={a.collection}",
        "ghcr.io/open-webui/open-webui:main",
    ]

    if a.dry_run:
        print("\n" + " ".join(docker_run) + "\n")
        return

    existing = run(DOCKER + ["ps", "-aq", "-f", f"name=^/{a.name}$"]).stdout.strip()
    if existing:
        die(f"a container named '{a.name}' already exists — remove it or pass a different --name")
    r = run(docker_run)
    if r.returncode != 0:
        die(f"docker run failed: {r.stderr.strip()}")
    ok(f"started {a.name} (volume '{volume}')")
    info(f"embedding: ollama / {a.embed_model}   chunks: {a.chunk_size}/{a.chunk_overlap}"
         f"   batch: {a.batch_size}")

    # ---------- 3. wait for it ----------
    step("3/6  Waiting for the service")
    for i in range(120):
        try:
            if requests.get(f"{base}/health", timeout=3).ok:
                ok(f"up at {base}")
                break
        except Exception:
            pass
        time.sleep(2)
    else:
        die(f"{a.name} did not become healthy — check: {' '.join(DOCKER)} logs {a.name}")

    # ---------- 4. admin account ----------
    step("4/6  Admin account")
    email = a.email or input("  admin email: ").strip()
    password = a.password or getpass.getpass("  admin password: ")
    if not email or not password:
        die("email and password are required")
    s = requests.Session()
    try:
        r = s.post(f"{base}/api/v1/auths/signup", timeout=30,
                   json={"name": a.admin_name, "email": email, "password": password})
    except Exception as e:
        die(f"signup request failed: {e}")
    if not r.ok:
        die(f"signup failed (HTTP {r.status_code}): {r.text[:200]}\n"
            f"    Create the account by hand at {base}, then re-run with --skip-pull "
            f"to finish the rest.")
    tok = (r.json() or {}).get("token")
    if not tok:
        die("signup returned no token")
    s.headers.update({"Authorization": f"Bearer {tok}"})
    ok(f"admin created: {email}  (first account = admin)")

    # ---------- 5. API key ----------
    step("5/6  API key")
    api_key = ""
    for path in ("/api/v1/auths/api_key", "/api/v1/auths/api-key"):
        try:
            r = s.post(f"{base}{path}", timeout=30)
            if r.ok:
                api_key = (r.json() or {}).get("api_key", "")
                if api_key:
                    break
        except Exception:
            pass
    if api_key:
        key_file.write_text(api_key + "\n")
        key_file.chmod(0o600)
        ok(f"API key created and saved to {key_file}")
    else:
        warn("could not create the API key via the API.")
        warn(f"Do it once in the UI: {base} → Settings → Account → API Keys,")
        warn(f"then: echo 'sk-...' > {key_file} && chmod 600 {key_file}")

    # ---------- 6. knowledge collection ----------
    step("6/6  Knowledge collection")
    coll_id = ""
    payload = {"name": a.collection, "description": f"{a.collection} library",
                "data": {}, "access_control": None}
    for path in ("/api/v1/knowledge/create", "/api/v1/knowledge/"):
        try:
            r = s.post(f"{base}{path}", json=payload, timeout=30)
            if r.ok:
                coll_id = (r.json() or {}).get("id", "")
                if coll_id:
                    break
        except Exception:
            pass
    if coll_id:
        ok(f"collection '{a.collection}' created — id {coll_id}")
    else:
        warn("could not create the collection via the API.")
        warn(f"Create it in the UI ({base} → Workspace → Knowledge → +) and copy the")
        warn("id from its URL, then set TARGET in the generated .conf below.")
        coll_id = "PASTE_COLLECTION_ID_HERE"

    # ---------- config file for sync_folder.py ----------
    watch = a.watch_dir or str(pathlib.Path.home() / a.collection.lower())
    conf_dir = pathlib.Path(a.scripts_dir).expanduser()
    conf_dir.mkdir(parents=True, exist_ok=True)
    conf = conf_dir / f"{a.collection.lower()}.conf"
    conf.write_text(f"""# {a.collection} library — config for sync_folder.py
# Generated by new_rag_instance.py {__version__}
# Use with:  python3 sync_folder.py --config {conf.name}

BACKEND    = openwebui
BASE_URL   = {base}
TARGET     = {coll_id}
WATCH_DIR  = {watch}
KEY_FILE   = {key_file}
STATE_FILE = {state_file}

# behaviour (a CLI flag still overrides these)
DESCRIBE_FIGURES = true
OCR_FALLBACK     = true
PRUNE            = false

FIGURE_MODEL = llava
OLLAMA_URL   = http://localhost:11434
""")

    print(f"\n{B}================ DONE ================{X}")
    print(f"  UI            : {base}   (login: {email})")
    print(f"  embedding     : ollama / {a.embed_model}  — already configured")
    print(f"  collection    : {a.collection}  (id {coll_id})")
    print(f"  API key file  : {key_file}")
    print(f"  sync state    : {state_file}")
    print(f"  sync config   : {conf}")
    print(f"\n  Sync this library with:\n"
          f"      python3 sync_folder.py --config {conf}")
    print(f"\n  Notes:")
    print(f"   - put your documents in {watch}")
    print(f"   - this instance shares your Ollama, so the same chat models are available")
    print(f"   - it is NOT removed by uninstall_local_rag.sh; to delete it later:")
    print(f"       {' '.join(DOCKER)} rm -f {a.name} && {' '.join(DOCKER)} volume rm {volume}")
    print()


if __name__ == "__main__":
    main()
