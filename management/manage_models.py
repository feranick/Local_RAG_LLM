#!/usr/bin/env python3
"""
manage_models.py — add, test, list, remove and select the LLMs used by an
already-running local RAG stack.

Important distinction:
  * The CHAT model is used only when answering. Swapping it needs NO re-indexing,
    and because every Open WebUI instance shares one Ollama, a newly pulled model
    appears in all of them straight away.
  * The EMBEDDING model is baked into the stored vectors. Changing it invalidates
    the whole collection and forces a full re-sync — so this script refuses to
    treat that as a casual change (see --embedding-warning).

Every add is followed by a REAL load test: a model can download perfectly and
still fail to run on a given build (e.g. llama3.2-vision needs the 'mllama'
architecture, which several Ollama builds — including the DGX Spark's — lack).

What fits is decided by the detected hardware, not a hardcoded number: the memory
budget comes from platform_probe.py (RAG_USABLE_MEM_GB > hardware.conf > live
probe), so --tags and --suggest hide models this machine cannot hold.

Finding a name to install is the awkward part, so discovery is LIVE — it reads the
public Ollama library rather than a baked-in list that would go stale:
  --browse [TERM]   what models exist (optionally matching a search term)
  --tags NAME       the exact installable tags for one model, with sizes, and
                    whether each one fits this machine

Usage:
  python3 manage_models.py --list                     # what's installed, sizes, roles
  python3 manage_models.py --loaded                   # what's in memory right now
  python3 manage_models.py --browse                   # what's available (live)
  python3 manage_models.py --browse gemma             # ...matching a term
  python3 manage_models.py --tags gemma4              # exact tags + sizes (live)
  python3 manage_models.py --add llama3.3:70b         # pull + verify it loads
  python3 manage_models.py --test qwen3.6:27b         # verify only
  python3 manage_models.py --remove gemma4:12b        # free the disk space
  python3 manage_models.py --suggest                  # offline starting list that fits
  python3 manage_models.py --set-default llama3.3:70b --instance http://localhost:3000
"""

__version__ = "2026.08.03.2"

import os
import re
import sys
import json
import shutil
import pathlib
import argparse
import subprocess
from urllib.parse import quote

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

OLLAMA = os.environ.get("RAG_OLLAMA_URL", "http://localhost:11434")

# name patterns used to guess a model's role
EMBED_PAT = ("embed", "bge", "nomic", "arctic", "minilm")
VISION_PAT = ("llava", "moondream", "bakllava", "vision", "-vl", "vl:")

# ----------------------------- live catalogue ----------------------------
# Ollama publishes no catalogue API, so names/tags/sizes are read from the public
# library pages. That keeps this script from going stale as models change, but it
# does mean the parsing can break if the site is redesigned — every command below
# degrades to a clear message rather than a traceback.
CATALOG = "https://ollama.com"

# Tag suffixes that exist in the library but are NOT usable on this machine:
#   *-mlx*   Apple-silicon builds
#   *-cloud  run on Ollama's servers, not locally
UNUSABLE_TAG_MARKERS = ("-mlx", "mlx-", ":cloud", "-cloud")

# Tags that MAY be published for one platform only. The library tag page shows size,
# context and capabilities but not the platform requirement, so the only way to find
# out is to try: the registry answers HTTP 412 at the manifest stage, within seconds,
# before any weights are downloaded. Observed: qwen3.8:27b-nvfp4 -> "this model
# requires macOS" on Linux/ARM64, despite NVFP4 being an NVIDIA format.
PLATFORM_RISK_MARKERS = ("nvfp4", "mxfp8", "mxfp4", "-metal")


# How much model this machine can hold. Measured, not assumed: a DGX Spark has
# ~110 GB of unified memory, a 24 GB workstation card has 23. Resolution order is
# RAG_USABLE_MEM_GB > hardware.conf > a live probe > a conservative default, so a
# copy of this script on a machine with none of that still runs.
def _usable_mem_gb():
    try:
        import platform_probe                       # same directory / installed
        return platform_probe.usable_mem_gb()
    except ImportError:
        pass
    try:                                            # repo checkout: ../common
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
        import platform_probe
        return platform_probe.usable_mem_gb()
    except Exception:
        pass
    env = os.environ.get("RAG_USABLE_MEM_GB")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    warn("hardware not probed (platform_probe.py not found) — assuming 16 GB usable; "
         "set RAG_USABLE_MEM_GB to correct it")
    return 16.0


USABLE_MEM_GB = _usable_mem_gb()


def http_get(url, timeout=25):
    return requests.get(url, timeout=timeout,
                        headers={"User-Agent": "manage_models.py"}).text


def _size_gb(txt):
    m = re.search(r"(\d+(?:\.\d+)?)\s*(GB|MB|TB)", txt, re.I)
    if not m:
        return None
    v, u = float(m.group(1)), m.group(2).upper()
    return v / 1024 if u == "MB" else (v * 1024 if u == "TB" else v)


def catalog_tags(name):
    """[(tag, size_gb, context, note)] for a model, from its library page.

    Each row is a link to /library/<name>:<tag> followed by the digest, download
    size, context window and input types. Rows are bounded by the next *different*
    tag link so one row's size can't be read off the row below it (cloud tags in
    particular publish no size at all).
    """
    html = http_get(f"{CATALOG}/library/{name}/tags")
    hits = list(re.finditer(r"/library/" + re.escape(name) + r":([A-Za-z0-9._\-]+)", html))
    out, seen = [], set()
    for i, m in enumerate(hits):
        tag = m.group(1)
        if tag in seen:
            continue
        seen.add(tag)
        stop = len(html)
        for nxt in hits[i + 1:]:
            if nxt.group(1) != tag:
                stop = nxt.start()
                break
        window = html[m.end():min(stop, m.end() + 1500)]
        out.append((tag, _size_gb(window),
                    (re.search(r"(\d+K)\s*context", window) or [None, "?"])[1],
                    "vision" if re.search(r"Text,\s*Image", window) else ""))
    return out


CAP_WORDS = ("vision", "tools", "thinking", "audio", "embedding", "cloud")


def _text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _blurb(window, name):
    """The card's description: its first real paragraph."""
    for m in re.finditer(r"<p[^>]*>(.*?)</p>", window, re.S):
        t = _text(m.group(1))
        if len(t) > 25 and not re.match(r"^[\d.]+[KMB]?\s*(Pulls|Tags?)?$", t, re.I):
            return t
    return ""


def catalog_search(term=None):
    """Model cards from the library index: name, sizes, capabilities, blurb.

    Each card is one <a href="/library/NAME"> whose body holds the name, the
    description, capability chips, the parameter sizes, then
    "<n> Pulls  <n> Tags  Updated <when>". Cards are bounded by the next card's
    link so nothing bleeds across rows.
    """
    url = f"{CATALOG}/search?q={quote(term)}" if term else f"{CATALOG}/search"
    html = http_get(url)
    hits = [m for m in re.finditer(r'href="/library/([A-Za-z0-9._\-]+)"', html)
            if ":" not in m.group(1)]
    out, seen = [], set()
    for i, m in enumerate(hits):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        stop = hits[i + 1].start() if i + 1 < len(hits) else len(html)
        raw = html[m.end():min(stop, m.end() + 3000)]
        card = _text(raw)
        head = re.split(r"\d[\d.]*[KMB]?\s*Pulls", card)[0]
        caps = [c for c in CAP_WORDS if re.search(rf"\b{c}\b", head, re.I)]
        sizes = re.findall(r"\b(e?\d+(?:\.\d+)?b)\b", head, re.I)
        desc = _blurb(raw, name)
        if not desc:                      # fall back: cut where the chips start
            h = re.sub(r"^[^A-Za-z]+", "", head)
            if h.lower().startswith(name.lower()):
                h = h[len(name):].strip()
            stops = [p for p in (h.lower().find(t.lower()) for t in caps + sizes) if p > 0]
            desc = (h[:min(stops)] if stops else h).strip()
        out.append({
            "name": name,
            "desc": desc,
            "caps": caps,
            "sizes": sizes,
            "pulls": (re.search(r"([\d.]+[KMB]?)\s*Pulls", card) or [None, ""])[1],
            "ntags": (re.search(r"(\d+)\s*Tags?\b", card) or [None, ""])[1],
        })
    return out


def cmd_browse(term, show_all):
    step(f"Ollama library — {'search: ' + term if term else 'popular models'} (live)")
    try:
        found = catalog_search(term)
    except Exception as e:
        bad(f"could not reach {CATALOG}: {e}")
        info("offline? --suggest prints a small built-in starting list instead")
        return
    if not found:
        warn(f"nothing matched — try a broader term, or open {CATALOG}/search")
        return
    have = {m.get("name", "").split(":")[0] for m in installed(soft=True)}
    shown = cloud_only = 0
    print(f"  {'model':22} {'sizes':22} {'pulls':>7}  notes")
    for m in found:
        # a model whose only tag is a cloud tag runs on Ollama's servers, not here
        if "cloud" in m["caps"] and not m["sizes"]:
            cloud_only += 1
            if not show_all:
                continue
        if not show_all and shown >= 30:
            continue
        shown += 1
        mark = f"{G}✔{X}" if m["name"] in have else " "
        notes = [c for c in m["caps"] if c != "tools"]
        if m["ntags"]:
            notes.append(f"{m['ntags']} tags")
        print(f"  {mark} {m['name']:20} {' '.join(m['sizes'])[:21]:22} "
              f"{m['pulls']:>7}  {', '.join(notes)}")
        if m["desc"]:
            print(f"      {m['desc'][:88]}")
    print(f"\n  {len(found)} model(s) listed"
          + (f", {cloud_only} cloud-only hidden (can't run locally)" if cloud_only and not show_all else "")
          + ("" if show_all else "; --all shows everything"))
    print(f"  'sizes' are parameter counts, not memory — this machine holds "
          f"~{USABLE_MEM_GB:.0f} GB of loaded weights.")
    print("  next:  python3 manage_models.py --tags <model>    # exact tags + download sizes")


def cmd_tags(name, show_all):
    step(f"Available tags for '{name}'")
    try:
        tags = catalog_tags(name)
    except Exception as e:
        bad(f"could not read the tag list: {e}")
        info(f"check the name — see: {CATALOG}/library/{name}/tags")
        return
    if not tags:
        warn("no tags parsed — the model name may be wrong, or the site changed layout")
        info(f"open {CATALOG}/library/{name}/tags to check")
        return
    have = {m.get("name", "") for m in installed(soft=True)}
    hidden = 0
    print(f"  {'tag':30} {'size':>8}  {'ctx':>6}  notes")
    for tag, gb, ctx, note in tags:
        full = f"{name}:{tag}"
        unusable = any(k in f":{tag}" for k in UNUSABLE_TAG_MARKERS)
        if unusable and not show_all:
            hidden += 1
            continue
        bits = []
        if note:
            bits.append(note)
        if unusable:
            bits.append("NOT usable locally (Apple-MLX or cloud tag)")
        elif gb and gb > USABLE_MEM_GB:
            bits.append(f"too big for {USABLE_MEM_GB:.0f} GB")
        elif gb and gb > USABLE_MEM_GB / 2:
            bits.append("large — will be slow")
        mark = f"{G}✔{X}" if full in have else " "
        size = "?" if not gb else (f"{gb:.1f} GB" if gb < 10 else f"{gb:.0f} GB")
        print(f"  {mark} {full:28} {size:>8}  {ctx:>6}  {', '.join(bits)}")
    if hidden:
        info(f"{hidden} Apple-MLX/cloud tag(s) hidden — use --all to see them")
    print(f"\n  install one with:  python3 manage_models.py --add {name}:<tag>")


# Small fallback list for when the machine is offline. NOT authoritative — use
# --browse / --tags for the current catalogue. Sizes are approximate download
# sizes in GB, used only to hide models this machine cannot hold.
SUGGESTED = [
    ("gemma4:e2b-it-qat", "chat", 4.3, "smallest useful chat model (QAT)"),
    ("gemma4:12b-it-qat", "chat", 7.2, "12B, quantisation-aware — modest GPUs"),
    ("gemma4:12b",   "chat",  7.6,  "small and quick"),
    ("gemma4:26b",   "chat",  18.0, "MoE, ~4B active/token — fast"),
    ("gemma4:31b",   "chat",  20.0, "largest dense Gemma 4"),
    ("qwen3.6:27b",  "chat",  17.0, "dense, high quality (Alibaba model)"),
    ("qwen3.6:35b",  "chat",  21.0, "35B-A3B MoE, faster (Alibaba model)"),
    ("llama3.3:70b", "chat",  43.0, "strong all-rounder, slower"),
    ("deepseek-r1:70b", "chat", 43.0, "reasoning specialist"),
    ("nomic-embed-text", "embedding", 0.3, "default embedder, 768-dim"),
    ("bge-m3",       "embedding", 1.2, "multilingual / long context"),
    ("mxbai-embed-large", "embedding", 0.7, "alternative embedder"),
    ("llava",        "vision", 4.7,  "figure descriptions — widely compatible"),
    ("moondream",    "vision", 1.7,  "small vision model"),
]

# A model needs room for weights plus KV cache/context; 1.2x is a rough floor.
FIT_FACTOR = 1.2


def fits(size_gb):
    return size_gb * FIT_FACTOR <= USABLE_MEM_GB
KNOWN_BAD = {
    "llama3.2-vision": "needs the 'mllama' architecture, unsupported by the "
                       "some Ollama builds (fails with 'unknown model architecture')",
}


def role_of(name):
    low = name.lower()
    if any(p in low for p in EMBED_PAT):
        return "embedding"
    if any(p in low for p in VISION_PAT):
        return "vision"
    return "chat"


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.1f} {u}"
        n /= 1024


def api(path, **kw):
    return requests.get(f"{OLLAMA}{path}", timeout=10, **kw)


def installed(soft=False):
    """Installed models. With soft=True, an unreachable Ollama yields [] and a
    warning instead of exiting — so informational commands still work offline."""
    try:
        return api("/api/tags").json().get("models", [])
    except Exception as e:
        if soft:
            warn(f"cannot reach Ollama at {OLLAMA} — "
                 "listing without the 'installed' marks")
            return []
        sys.exit(f"cannot reach Ollama at {OLLAMA}: {e}")


def cmd_list():
    step(f"Models installed in Ollama ({OLLAMA})")
    models = installed()
    if not models:
        warn("none installed")
        return
    total = 0
    for m in sorted(models, key=lambda x: x.get("name", "")):
        name, size = m.get("name", "?"), m.get("size", 0)
        total += size
        print(f"  {name:34} {human(size):>9}   {role_of(name)}")
    print(f"\n  {len(models)} model(s), {human(total)} on disk")
    print(f"  (this machine can hold ~{USABLE_MEM_GB:.0f} GB of *loaded* models; disk "
          f"usage may safely exceed that\n"
          f"   — only what's resident matters. See: python3 platform_probe.py)")


def cmd_loaded():
    step("Loaded in memory right now")
    if shutil.which("ollama"):
        subprocess.run(["ollama", "ps"])
    else:
        try:
            for m in api("/api/ps").json().get("models", []):
                print(f"  {m.get('name')}  {human(m.get('size', 0))}")
        except Exception as e:
            warn(f"could not query: {e}")


def test_model(name, quiet=False):
    """Real load test. Returns True if the model actually runs."""
    role = role_of(name)
    try:
        if role == "embedding":
            r = requests.post(f"{OLLAMA}/api/embeddings", timeout=180,
                              json={"model": name, "prompt": "test"})
            good = r.ok and "embedding" in r.text
        else:
            r = requests.post(f"{OLLAMA}/api/generate", timeout=300,
                              json={"model": name, "prompt": "Reply with: OK",
                                    "stream": False})
            good = r.ok and "response" in r.text
    except Exception as e:
        if not quiet:
            bad(f"{name}: request failed ({e})")
        return False
    if good:
        if not quiet:
            ok(f"{name} loads and responds ({role})")
        return True
    if not quiet:
        bad(f"{name} did NOT run (HTTP {r.status_code})")
        detail = (r.text or "")[:200]
        if detail:
            print(f"      server said: {detail}")
        if "architecture" in detail.lower():
            print("      -> this Ollama build lacks that model architecture; pick another,")
            print("         or run a stock Ollama in a container for it.")
        else:
            print("      -> check: journalctl -u ollama -n 50 --no-pager")
    return False


def pull_with_capture(name):
    """Run `ollama pull`, stream its output through unchanged (the progress bar uses
    \\r, so this reads in chunks rather than lines), and keep the tail for diagnosis.

    Capturing matters: the reason a pull failed is in that output, and 'pull failed'
    on its own sends you looking for a typo when the real answer may be that the tag
    is published for another platform, or that the disk is full."""
    buf = []
    try:
        p = subprocess.Popen(["ollama", "pull", name],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as e:
        return 1, f"could not run ollama: {e}"
    while True:
        chunk = p.stdout.read(256)
        if not chunk:
            break
        text = chunk.decode("utf-8", "replace")
        sys.stdout.write(text)
        sys.stdout.flush()
        buf.append(text)
        if len(buf) > 40:                 # keep only the tail
            del buf[:-40]
    p.wait()
    return p.returncode, "".join(buf)


def explain_pull_failure(name, tail):
    """Turn the registry's answer into the next thing to do."""
    low = (tail or "").lower()
    base = name.split(":")[0]

    if "requires macos" in low or ("412" in low and "requires" in low):
        warn("that TAG is published for another platform — nothing to do with the")
        warn("name being wrong, and no weights were downloaded (it fails at the manifest)")
        info("the library tag page shows size/context/capabilities but NOT the platform")
        info("requirement, so a gated tag looks identical to a usable one there")
        info("try a portable quantization instead, same footprint:")
        for alt in (f"{base}:27b-q4_K_M", f"{base}:27b-mtp-q4_K_M"):
            info(f"    python3 manage_models.py --add {alt}")
        info(f"or list what exists:  python3 manage_models.py --tags {base}")
        return
    if "no space left" in low or "not enough space" in low:
        bad("out of disk space — check with:  df -h ~/.ollama /usr/share/ollama")
        return
    if "manifest unknown" in low or "file does not exist" in low or "404" in low:
        info("that name/tag does not exist in the library; list the real ones:")
        info(f"    python3 manage_models.py --tags {base}")
        return
    if any(k in low for k in ("timeout", "connection", "eof", "tls", "temporary failure")):
        warn("that looks like a network problem, not a bad name — retrying may work")
        info("check: curl -I https://registry.ollama.ai/")
        return
    info("if the name or tag was wrong, list the real ones:")
    info(f"    python3 manage_models.py --tags {base}")


def cmd_add(name):
    step(f"Adding {name}")
    if name.split(":")[0] in KNOWN_BAD or name in KNOWN_BAD:
        why = KNOWN_BAD.get(name) or KNOWN_BAD[name.split(":")[0]]
        warn(f"{name} is known NOT to work here: {why}")
        if input("  continue anyway? [y/N] ").strip().lower() not in ("y", "yes"):
            return
    if any(k in name for k in UNUSABLE_TAG_MARKERS):
        warn(f"'{name}' looks like an Apple-MLX or cloud tag — those don't run on "
             "this machine")
        info(f"see the usable tags with:  python3 manage_models.py --tags {name.split(':')[0]}")
        if input("  continue anyway? [y/N] ").strip().lower() not in ("y", "yes"):
            return
    if any(k in name.lower() for k in PLATFORM_RISK_MARKERS):
        info(f"note: '{name}' is a vendor quantization format tag; some of these are")
        info("published for one platform only. If so the pull stops in a few seconds")
        info("at the manifest stage — no wasted download.")
    if shutil.which("ollama") is None:
        sys.exit("the 'ollama' CLI is required to pull models")
    names = [m.get("name") for m in installed(soft=True)]
    if name in names:
        info(f"{name} already installed")
    else:
        info("pulling — this can take a while…")
        rc, tail = pull_with_capture(name)
        if rc != 0:
            bad(f"pull failed for {name}")
            explain_pull_failure(name, tail)
            return
        ok("pulled")
    step("Verifying it actually loads")
    if test_model(name):
        role = role_of(name)
        print()
        if role == "chat":
            ok("ready to use — select it in the UI's model dropdown")
            info("no re-indexing needed: the chat model isn't part of the stored vectors")
            info("to start new chats on it: Settings → Admin → AI → Models → ⋯ → "
                 "Set as Selected Model")
        elif role == "vision":
            ok("ready for figure descriptions")
            info(f"use it with:  RAG_FIGURE_MODEL={name} python3 sync_folder.py --describe-figures")
            info("or set FIGURE_MODEL in your sync config file")
        else:
            warn("this is an EMBEDDING model — switching embedders invalidates an "
                 "existing collection")
            info("see --embedding-warning before changing it on a live library")


def cmd_remove(name):
    step(f"Removing {name}")
    if shutil.which("ollama") is None:
        sys.exit("the 'ollama' CLI is required")
    if role_of(name) == "embedding":
        warn("that looks like an EMBEDDING model. If a collection was built with it,")
        warn("removing it breaks retrieval for that collection until you re-sync.")
        if input("  really remove? [y/N] ").strip().lower() not in ("y", "yes"):
            return
    if subprocess.run(["ollama", "rm", name]).returncode == 0:
        ok(f"{name} removed")
    else:
        bad("removal failed (check the exact tag with --list)")


def cmd_suggest(show_all=False):
    step(f"Starting points that fit this machine (~{USABLE_MEM_GB:.0f} GB usable)")
    warn("this short list is built into the script and WILL age — "
         "use --browse / --tags for the live catalogue")
    have = {m.get("name") for m in installed(soft=True)}
    hidden = 0
    for name, role, size, note in SUGGESTED:
        if not fits(size) and not show_all:
            hidden += 1
            continue
        mark = f"{G}installed{X}" if name in have or f"{name}:latest" in have else "         "
        flag = "" if fits(size) else "   [too big for this machine]"
        print(f"  {mark}  {name:22} {role:10} ~{size:>4.1f} GB  {note}{flag}")
    if hidden:
        info(f"{hidden} model(s) hidden as too large for ~{USABLE_MEM_GB:.0f} GB "
             f"— use --all to see them")
    if not any(fits(sz) for _, role, sz, _ in SUGGESTED if role == "chat"):
        warn(f"no chat model in this list fits ~{USABLE_MEM_GB:.0f} GB. Smaller "
             f"quantisations exist — try:  python3 manage_models.py --tags gemma4")
    print()
    for name, why in KNOWN_BAD.items():
        bad(f"avoid {name}: {why}")
    print("\n  add one with:  python3 manage_models.py --add <tag>")


def _api_json(session, method, url, **kw):
    """Return parsed JSON, or None if this isn't really an API endpoint.

    Open WebUI serves a single-page app at '/', and its catch-all returns the
    index page with **HTTP 200 and text/html** for unknown paths. A plain
    `response.ok` check therefore reports success for routes that don't exist —
    which is exactly how an earlier version of this function claimed to have set
    the default model while doing nothing at all. Insist on JSON.
    """
    try:
        r = session.request(method, url, timeout=20, **kw)
    except Exception:
        return None
    if not r.ok:
        return None
    if "application/json" not in r.headers.get("Content-Type", ""):
        return None
    try:
        return r.json()
    except ValueError:
        return None


def _default_model_help(instance, name):
    """What to do when the API can't set this — the routes move between versions."""
    info("Options, most reliable first:")
    print()
    print("  1. Curate the picker so the preset is the only thing to land on.")
    print("     A new chat resolves: URL param > folder models > the user's own")
    print("     default > the instance's Selected Models > FIRST AVAILABLE MODEL.")
    print("     So hide the base models and everyone starts on your preset:")
    print(f"       {instance} → Admin → Settings → AI → Models → the eye icon")
    print("     Keep them ENABLED (the toggle on the right) — hide is not disable,")
    print("     and a preset always needs access to its base model. Also set the")
    print("     preset itself to Public, or other users can't select it.")
    print()
    print("  2. Per user, no admin rights needed: open a chat, pick the model,")
    print("     then 'Set as default' in the same dropdown. A user's own default")
    print("     outranks the instance setting.")
    print()
    print("  3. DEFAULT_MODELS as a container env var — but note it is a")
    print("     PersistentConfig value: once the database holds a value, the env")
    print("     var is ignored. ENABLE_PERSISTENT_CONFIG=False forces env to win,")
    print("     at the cost of every UI-configured setting (Top K, hybrid search,")
    print("     embedding model…) reverting on each restart. Rarely worth it.")
    print()
    if ":" not in name:
        info(f"'{name}' has no ':' so it looks like a workspace preset id. Those are "
             "not listed under Admin → AI → Models at all (that page lists base "
             "models), which is why there is no ⋯ menu to use for them.")


def cmd_set_default(name, instance, key_file):
    """Try to set the instance's Selected Model ("default") via the API.

    Deliberately conservative: it verifies by reading the setting back, and says
    plainly when the version doesn't expose it rather than pretending.
    """
    step(f"Setting {name} as the Selected (default) model on {instance}")
    if role_of(name) != "chat" and ":" in name:
        warn(f"{name} looks like a {role_of(name)} model — probably not what you "
             "want as the chat default")
    key = ""
    kf = pathlib.Path(key_file).expanduser()
    if kf.is_file():
        key = kf.read_text().strip()
    if not key:
        warn(f"no API key found at {kf} — cannot call the API")
        _default_model_help(instance, name)
        return

    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {key}"})

    # Find a config endpoint that actually exists on THIS build, by reading it
    # first. Writing blind is how you end up believing a 200-with-HTML.
    candidates = ("/api/v1/configs/models",
                  "/api/v1/configs/default/models",
                  "/api/v1/configs/default")
    found = None
    for path in candidates:
        cur = _api_json(s, "GET", f"{instance}{path}")
        if isinstance(cur, dict):
            found = (path, cur)
            info(f"config endpoint: {path}")
            break
    if not found:
        warn("this build exposes no models-config endpoint (all candidates returned "
             "the web app, not JSON)")
        _default_model_help(instance, name)
        return

    path, cur = found
    # Merge into whatever shape the server already uses, so nothing else is lost.
    for field, value in (("DEFAULT_MODELS", name), ("models", [name])):
        if field not in cur:
            continue
        body = dict(cur)
        body[field] = value
        if _api_json(s, "POST", f"{instance}{path}", json=body) is None:
            continue
        back = _api_json(s, "GET", f"{instance}{path}") or {}
        if name in str(back.get(field, "")):
            ok(f"Selected model set to {name} (verified)")
            info("users who already chose their own default keep it — that wins")
            return
        warn(f"the POST was accepted but reading it back did not show {name}")
        break
    else:
        warn(f"{path} exists but carries no recognisable default-model field "
             f"(keys: {', '.join(sorted(cur)[:8])})")

    _default_model_help(instance, name)


def cmd_embedding_warning():
    step("Changing the EMBEDDING model on an existing library")
    print("""
  The embedding model is part of the stored vectors, so it cannot be swapped in
  place — old and new vectors are not comparable. Two safe options:

  1. Re-index the existing collection (destructive, but keeps one instance):
       a. pull the new embedder:      python3 manage_models.py --add bge-m3
       b. set it in Admin → Settings → Documents → Embedding Model, and Save
       c. wipe + re-sync the collection (see README_sync.md → "Fully wiping…")
          — every document must be embedded again, which takes as long as the
            original sync did.

  2. Build a SECOND instance instead (non-destructive, allows comparison):
       python3 new_rag_instance.py --collection Lib2 --embed-model bge-m3 --port 3002
     Both share Ollama and the same chat models; only the embeddings differ, so you
     can ask both the same question and judge which retrieves better.

  The CHAT model has none of these constraints — pull it and pick it in the UI.
""")


def main():
    ap = argparse.ArgumentParser(
        description="Manage the LLMs used by a running local RAG stack.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--list", action="store_true", help="list installed models")
    ap.add_argument("--loaded", action="store_true", help="show what's loaded in memory")
    ap.add_argument("--suggest", action="store_true",
                    help="small built-in starting list (offline fallback)")
    ap.add_argument("--browse", metavar="TERM", nargs="?", const="",
                    help="list models available to install, live from ollama.com "
                         "(optionally filtered by TERM)")
    ap.add_argument("--tags", metavar="NAME",
                    help="live list of installable tags for one model, with sizes")
    ap.add_argument("--all", action="store_true",
                    help="with --browse/--tags: don't truncate or hide unusable tags")
    ap.add_argument("--add", metavar="TAG", help="pull a model and verify it loads")
    ap.add_argument("--test", metavar="TAG", help="verify a model actually loads")
    ap.add_argument("--remove", metavar="TAG", help="delete a model from Ollama")
    ap.add_argument("--set-default", metavar="TAG", help="make it the instance default")
    ap.add_argument("--instance", default="http://localhost:3000",
                    help="Open WebUI instance for --set-default")
    ap.add_argument("--key-file", default="~/.rag_sync_key",
                    help="API key file for --set-default")
    ap.add_argument("--embedding-warning", action="store_true",
                    help="explain what changing the embedding model entails")
    a = ap.parse_args()

    if not any([a.list, a.loaded, a.suggest, a.add, a.test, a.remove,
                a.set_default, a.embedding_warning, a.tags, a.browse is not None]):
        ap.print_help()
        return
    if a.list:
        cmd_list()
    if a.loaded:
        cmd_loaded()
    if a.browse is not None:
        cmd_browse(a.browse or None, a.all)
    if a.tags:
        cmd_tags(a.tags.split(":")[0], a.all)
    if a.suggest:
        cmd_suggest(a.all)
    if a.add:
        cmd_add(a.add)
    if a.test:
        step(f"Testing {a.test}")
        test_model(a.test)
    if a.remove:
        cmd_remove(a.remove)
    if a.set_default:
        cmd_set_default(a.set_default, a.instance.rstrip("/"), a.key_file)
    if a.embedding_warning:
        cmd_embedding_warning()
    print()


if __name__ == "__main__":
    main()
