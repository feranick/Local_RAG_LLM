#!/usr/bin/env python3
"""
determinism_check.py — ask the same question N times and measure how much the
answer moves.

Why this exists: "the same question gets a different answer" is a complaint about
the WHOLE pipeline, not about the decoder. Tools like detLLM measure engine-level
reproducibility by loading the model in-process (HuggingFace/vLLM) and controlling
seeds and batch composition — which cannot be done through Ollama's HTTP API, and
which would anyway measure the smallest of the terms that move a RAG answer. In
order of impact those are:

  1. sampling            — temperature 0.8 by default; fix with temperature 0
  2. retrieval variance  — under Native tool calling the model CHOOSES what to
                           search, and that choice is itself sampled
  3. prompt drift        — history, an index that changed between the two asks
  4. kernel / batching   — float non-associativity, concurrent requests; real,
                           but last, and not controllable from here

So this script measures what you can act on: it drives your actual Open WebUI
instance over its API, N times per question, and reports how often the answer text,
the cited sources and the numbers in the answer agree.

Typical use — did pinning temperature actually buy consistency?

    python3 determinism_check.py --model qwen3635b-breakerspace \\
        --question "Where are the polished cross-section samples stored?" \\
        --runs 5 --compare

    # a whole question set, current settings only
    python3 determinism_check.py --model qwen3635b-breakerspace \\
        --questions lab_questions.txt --runs 5

Question-set file: one question per line, `#` comments ignored, and an optional
expected-answer regex after `||`:

    Where are the polished cross-section samples stored?  || sample cabinet
    What is the EDS working distance on the Phenom?       || \\b10(\\.0)?\\s*mm

Artifacts land in --out (default ./determinism_<timestamp>/): runs.jsonl with every
raw response, report.json, report.txt. Keep them; they are what lets you compare a
configuration change a month later.

Caveats, stated plainly:
  * Each run is a NEW single-message chat. That removes history as a variable, so
    the numbers here are a floor: a long chat will be less repeatable, not more.
  * Sampling parameters are sent per request and override the preset. Send none
    (the default) to measure the preset exactly as your users experience it.
  * Whether a preset's attached knowledge is applied on the API path depends on the
    build. If sources come back empty but the UI cites documents, pass the
    collection explicitly with --collection <id>.
"""

__version__ = "2026.08.13.1"

import os
import re
import sys
import json
import time
import hashlib
import pathlib
import argparse
import datetime
import statistics
import collections

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

# The "pinned" pass in --compare. temperature 0 is greedy decoding, which makes
# seed irrelevant — it is sent anyway so the artifact records an unambiguous value.
# top_k 1 forecloses a tie being broken by sampling.
PINNED = {"temperature": 0, "top_k": 1, "seed": 42}

# Citation markers Open WebUI adds to answer text; they are not part of the answer.
CITE_RE = re.compile(r"\[\s*\d+\s*\]")
# Numbers with optional unit — the part of an answer where drift is dangerous.
NUM_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?:\s*(?:mm|cm|m|nm|um|µm|kv|kev|ma|na|s|min|h|%|x))?",
                    re.IGNORECASE)
WS_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------- API

def read_key(key_file):
    env = os.environ.get("RAG_API_KEY")
    if env:
        return env
    p = pathlib.Path(key_file).expanduser()
    if not p.is_file():
        die(f"no API key at {p} — put the sk-… key there (chmod 600) or set RAG_API_KEY")
    return p.read_text().strip()


def api(session, method, url, timeout=30, **kw):
    """JSON or None. Open WebUI answers unknown paths with 200 + the SPA HTML, so a
    status check alone would read a missing route as success."""
    try:
        r = session.request(method, url, timeout=timeout, **kw)
    except Exception as e:
        return {"__error__": f"{type(e).__name__}: {e}"}
    if not r.ok:
        return {"__error__": f"HTTP {r.status_code}: {(r.text or '')[:200]}"}
    if "application/json" not in r.headers.get("Content-Type", ""):
        return {"__error__": "not JSON (route missing on this build — got the SPA page)"}
    try:
        return r.json()
    except ValueError:
        return {"__error__": "malformed JSON in response"}


def list_presets(session, base):
    for path in ("/api/v1/models/", "/api/v1/models"):
        d = api(session, "GET", f"{base}{path}")
        items = d.get("data") if isinstance(d, dict) else d
        if isinstance(items, list):
            return items
    die(f"could not list models from {base} — check the URL and the API key")


def cmd_list(session, base):
    step(f"Models on {base}")
    for m in list_presets(session, base):
        if not isinstance(m, dict):
            continue
        kind = "preset" if m.get("info") else "base  "
        print(f"  {kind}  {str(m.get('id'))[:40]:42} {m.get('name')}")


def preflight_model(session, base, model):
    """Fail before running N generations if the model isn't on this instance.

    Presets are per-instance: one created on the second library's container is not
    visible on the first, and each instance has its own API key. That mistake
    otherwise surfaces as an opaque HTTP error inside the run loop."""
    models = list_presets(session, base)
    ids = [m.get("id") for m in models if isinstance(m, dict) and m.get("id")]
    if model in ids:
        return
    near = [i for i in ids if model.lower() in i.lower() or i.lower() in model.lower()]
    bad(f"model '{model}' is not on {base}")
    if near:
        info("close matches here: " + ", ".join(near))
    else:
        info("available here: " + (", ".join(ids[:12]) or "none"))
    info("Presets belong to ONE instance. If this preset was created on the second")
    info("library's container, point --instance at that port and use its key file:")
    info("  --instance http://localhost:3002 --key-file ~/.rag_sync_key_open-webui-<name>")
    sys.exit(1)


def environment(session, base):
    """Best-effort provenance for the artifact — the thing that makes a result from
    last month comparable with one from today."""
    env = {
        "when": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "tool_version": __version__,
        "base_url": base,
        "python": sys.version.split()[0],
    }
    cfg = api(session, "GET", f"{base}/api/config", timeout=10)
    if isinstance(cfg, dict) and "__error__" not in cfg:
        env["open_webui_version"] = cfg.get("version")
    for url in (os.environ.get("OLLAMA_HOST", "http://localhost:11434"),):
        v = api(session, "GET", f"{url.rstrip('/')}/api/version", timeout=5)
        if isinstance(v, dict) and "version" in v:
            env["ollama_version"] = v["version"]
        ps = api(session, "GET", f"{url.rstrip('/')}/api/ps", timeout=5)
        if isinstance(ps, dict) and isinstance(ps.get("models"), list):
            # context_length here is the answer to "what num_ctx am I actually running"
            env["loaded"] = [{"name": m.get("name"),
                              "context_length": m.get("context_length"),
                              "size_vram": m.get("size_vram")}
                             for m in ps["models"]]
    return env


# Documented endpoint first. The others are here only so a build that moved it can
# still be found — and note that POSTing to a path Open WebUI does NOT serve returns
# "405 Method Not Allowed", not 404, because the SPA catch-all is registered GET-only.
# So a 405 means "no such route on this build", and is not worth reporting as the
# reason a working route failed.
CHAT_PATHS = ["/api/chat/completions",
              "/api/v1/chat/completions",
              "/v1/chat/completions",
              "/openai/chat/completions"]


def chat_body(model, question, params, collection):
    body = {"model": model,
            "messages": [{"role": "user", "content": question}],
            "stream": False}
    body.update(params)
    if collection:
        body["files"] = [{"type": "collection", "id": collection}]
    return body


def ask(session, base, model, question, params, collection, timeout, path_cache):
    """One single-message chat. Returns (text, sources, raw, seconds, error).

    On failure the error names EVERY path tried and what each said — reporting only
    the last attempt hides the real cause behind the fallback's 405."""
    body = chat_body(model, question, params, collection)
    paths = [path_cache[0]] if path_cache[0] else CHAT_PATHS
    attempts = []
    for p in paths:
        t0 = time.time()
        d = api(session, "POST", f"{base}{p}", timeout=timeout, json=body)
        dt = time.time() - t0
        if isinstance(d, dict) and "__error__" not in d:
            path_cache[0] = p
            msg = ((d.get("choices") or [{}])[0].get("message")) or {}
            text = msg.get("content") or ""
            sources = d.get("sources") or msg.get("sources") or []
            return text, source_names(sources), d, dt, None
        attempts.append((p, d.get("__error__") if isinstance(d, dict) else "unknown error"))
    # put the informative failures first; 405 just means "route absent here"
    attempts.sort(key=lambda a: ("405" in a[1], "not JSON" in a[1]))
    return "", [], None, 0.0, "  ".join(f"{p} -> {e}" for p, e in attempts)


def cmd_probe(session, base, model):
    """Which chat endpoint does THIS build serve, and does the key work? Answers both
    in one pass, with the status of every candidate."""
    step(f"Probing chat endpoints on {base}")
    body = chat_body(model or "test", "Reply with the single word: ok", {}, None)
    found = None
    for p in CHAT_PATHS:
        d = api(session, "POST", f"{base}{p}", timeout=60, json=body)
        if isinstance(d, dict) and "__error__" not in d:
            txt = (((d.get("choices") or [{}])[0].get("message")) or {}).get("content", "")
            ok(f"{p:28} works — replied {txt[:40]!r}")
            found = found or p
        else:
            err = d.get("__error__", "?")
            note = ""
            if "405" in err:
                note = "  (route not served on this build — expected for the fallbacks)"
            elif "401" in err or "403" in err:
                note = "  (the API key is not valid for THIS instance)"
            elif "404" in err:
                note = "  (route exists elsewhere, or the model id is unknown here)"
            bad(f"{p:28} {err[:90]}{note}")
    print()
    if found:
        ok(f"use {found} — the tool picks this automatically")
    else:
        bad("no chat endpoint answered")
        info("check, in this order: the port (each library is its own instance),")
        info("the key file for THAT instance, and that the model id exists there")
    return found


# ---------------------------------------------------------------- analysis bits

def source_names(sources):
    """Open WebUI's `sources` shape varies by build and by retrieval mode, so pull
    any filename-ish string out of the structure rather than assuming one layout."""
    names = set()

    def walk(o, depth=0):
        if depth > 6:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("name", "title", "source", "file_id", "filename") and isinstance(v, str):
                    if v and not v.startswith("http") and len(v) < 200:
                        names.add(v)
                else:
                    walk(v, depth + 1)
        elif isinstance(o, list):
            for v in o:
                walk(v, depth + 1)

    walk(sources)
    return sorted(names)


def normalize(text):
    t = CITE_RE.sub(" ", text or "")
    t = t.replace("’", "'").replace("“", '"').replace("”", '"')
    t = WS_RE.sub(" ", t).strip().lower()
    return t


def fingerprint(norm):
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


def numbers(text):
    return sorted({WS_RE.sub("", n).lower() for n in NUM_RE.findall(text or "")})


def first_divergence(a, b):
    """Word index and surrounding context of the first difference between two
    answers — the same idea as detLLM's first_divergence.json, at answer level."""
    wa, wb = (a or "").split(), (b or "").split()
    n = min(len(wa), len(wb))
    i = 0
    while i < n and wa[i] == wb[i]:
        i += 1
    if i == n and len(wa) == len(wb):
        return None
    lo = max(0, i - 6)
    return {"word_index": i,
            "common_prefix_words": i,
            "a": " ".join(wa[lo:i + 6]),
            "b": " ".join(wb[lo:i + 6])}


def analyze(question, expect, runs):
    good = [r for r in runs if not r["error"]]
    out = {"question": question, "expect": expect,
           "runs": len(runs), "ok": len(good),
           "errors": [r["error"] for r in runs if r["error"]]}
    if not good:
        out["category"] = "no-answer"
        out["consistency"] = 0.0
        return out

    fps = collections.Counter(r["fingerprint"] for r in good)
    modal_fp, modal_n = fps.most_common(1)[0]
    out["distinct_answers"] = len(fps)
    out["consistency"] = round(modal_n / len(good), 3)

    src_sets = collections.Counter(tuple(r["sources"]) for r in good)
    out["distinct_source_sets"] = len(src_sets)
    out["modal_sources"] = list(src_sets.most_common(1)[0][0])
    out["source_consistency"] = round(src_sets.most_common(1)[0][1] / len(good), 3)

    num_sets = collections.Counter(tuple(r["numbers"]) for r in good)
    out["distinct_number_sets"] = len(num_sets)
    out["numbers_agree"] = len(num_sets) == 1

    if expect:
        rx = re.compile(expect, re.IGNORECASE)
        hits = sum(1 for r in good if rx.search(r["text"] or ""))
        out["expect_hit_rate"] = round(hits / len(good), 3)

    out["seconds_median"] = round(statistics.median(r["seconds"] for r in good), 1)
    out["chars_median"] = int(statistics.median(len(r["text"]) for r in good))

    # category, in the order that matters to someone reading the report
    if expect and out.get("expect_hit_rate", 1.0) < 1.0:
        out["category"] = "answer-flips"      # worst: sometimes right, sometimes not
    elif not out["numbers_agree"]:
        out["category"] = "facts-differ"
    elif out["source_consistency"] < 1.0:
        out["category"] = "sources-differ"
    elif out["distinct_answers"] == 1:
        out["category"] = "identical"
    else:
        out["category"] = "wording-only"

    if out["distinct_answers"] > 1:
        two = [fp for fp, _ in fps.most_common(2)]
        a = next(r["normalized"] for r in good if r["fingerprint"] == two[0])
        b = next(r["normalized"] for r in good if r["fingerprint"] == two[1])
        out["first_divergence"] = first_divergence(a, b)
    return out


VERDICT = {
    "identical":    (G, "identical every run"),
    "wording-only": (G, "wording varies, facts and sources agree"),
    "sources-differ": (Y, "same facts, different documents cited"),
    "facts-differ": (R, "the numbers in the answer changed between runs"),
    "answer-flips": (R, "the expected answer appeared only some of the time"),
    "no-answer":    (R, "every run failed"),
}


# ------------------------------------------------------------------- execution

def run_set(session, base, model, questions, params, collection, runs, timeout,
            sleep, label, jsonl, path_cache):
    results = []
    for qi, (q, expect) in enumerate(questions, 1):
        step(f"[{label}] Q{qi}/{len(questions)}: {q[:70]}{'…' if len(q) > 70 else ''}")
        rows = []
        for i in range(runs):
            text, sources, raw, dt, err = ask(session, base, model, q, params,
                                              collection, timeout, path_cache)
            norm = normalize(text)
            row = {"run": i + 1, "text": text, "normalized": norm,
                   "fingerprint": fingerprint(norm) if norm else "",
                   "sources": sources, "numbers": numbers(text),
                   "seconds": round(dt, 2), "error": err}
            rows.append(row)
            with jsonl.open("a") as fh:
                fh.write(json.dumps({"pass": label, "question": q, **row,
                                     "params": params}, ensure_ascii=False) + "\n")
            if err:
                bad(f"run {i+1}: {err}")
                # Repeating a broken request N times produces a confident 0% and no
                # information. Stop at the first one and say how to diagnose it.
                if qi == 1 and i == 0:
                    print()
                    info(f"stopping instead of repeating this {runs * len(questions)} times")
                    info("find the working endpoint and check the key with:")
                    info(f"  determinism_check --instance {base} --model {model} --probe")
                    sys.exit(1)
            else:
                # "= run k" is more informative than "differs from run 1": three runs
                # that agree with each other but not with the first are not three
                # separate answers.
                prior = next((p["run"] for p in rows[:-1]
                              if p["fingerprint"] == row["fingerprint"]), None)
                mark = f"= run {prior}" if prior else ("NEW    " if i else "       ")
                info(f"run {i+1}: {row['fingerprint']} {mark}  "
                     f"{len(text)} chars, {len(sources)} source(s), {dt:.1f}s")
            if sleep and i < runs - 1:
                time.sleep(sleep)
        a = analyze(q, expect, rows)
        a["pass"] = label
        a["params"] = params
        col, msg = VERDICT.get(a["category"], ("", a["category"]))
        print(f"  {col}→ {a['category']}{X}: {msg} "
              f"({a.get('consistency', 0):.0%} identical)")
        results.append(a)
    return results


def render(report):
    L = []
    A = L.append
    A(f"determinism_check {__version__}")
    A(f"instance {report['env']['base_url']}   model {report['model']}")
    if report["env"].get("open_webui_version"):
        A(f"Open WebUI {report['env']['open_webui_version']}"
          + (f"   Ollama {report['env']['ollama_version']}" if report["env"].get("ollama_version") else ""))
    for m in report["env"].get("loaded", []):
        A(f"loaded: {m['name']}  context {m.get('context_length')}")
    A(f"{report['runs']} run(s) per question, {len(report['questions'])} question(s)")
    A("")
    for p in report["passes"]:
        A(f"--- pass: {p['label']}   params: {json.dumps(p['params']) or '{}'}")
        A(f"{'consistency':>12}  {'sources':>8}  {'numbers':>8}  {'expected':>8}  category / question")
        for r in p["results"]:
            exp = "     n/a"
            if "expect_hit_rate" in r:
                exp = "{:>7.0%}".format(r["expect_hit_rate"])
            nums = "same" if r.get("numbers_agree") else "DIFFER"
            A(f"{r.get('consistency', 0):>11.0%}  "
              f"{r.get('source_consistency', 0):>7.0%}  "
              f"{nums:>8}  {exp}  "
              f"{r['category']:<14} {r['question'][:48]}")
            fd = r.get("first_divergence")
            if fd:
                A(f"{'':>12}  first divergence at word {fd['word_index']}:")
                A(f"{'':>14}A: …{fd['a']}…")
                A(f"{'':>14}B: …{fd['b']}…")
        A("")
    if len(report["passes"]) == 2:
        a, b = report["passes"]
        A("--- comparison")
        A(f"{'question':<50} {a['label']:>12} {b['label']:>12}")
        for ra, rb in zip(a["results"], b["results"]):
            A(f"{ra['question'][:50]:<50} {ra.get('consistency', 0):>11.0%} "
              f"{rb.get('consistency', 0):>11.0%}")
        A("")
        mean_a = statistics.mean(r.get("consistency", 0) for r in a["results"])
        mean_b = statistics.mean(r.get("consistency", 0) for r in b["results"])
        A(f"mean consistency: {a['label']} {mean_a:.0%}  →  {b['label']} {mean_b:.0%}")
        if mean_b <= mean_a + 0.01:
            A("Pinning the sampling parameters did NOT improve repeatability here.")
            A("That points at retrieval, not the decoder: under Native tool calling the")
            A("model chooses what to search. Try Function Calling = Legacy, a fixed Top K,")
            A("and hybrid search off, then re-run.")
        A("")
    A("Categories: identical | wording-only (facts agree) | sources-differ |")
    A("            facts-differ (numbers moved) | answer-flips (expected answer missing")
    A("            in some runs) | no-answer")
    return "\n".join(L)


def load_questions(path):
    out = []
    for line in pathlib.Path(path).expanduser().read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        q, sep, exp = line.partition("||")
        out.append((q.strip(), exp.strip() if sep else None))
    if not out:
        die(f"no questions in {path}")
    return out


def main():
    ap = argparse.ArgumentParser(
        description="Ask the same question N times and measure how much the answer moves.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--list", action="store_true", help="list models/presets and exit")
    ap.add_argument("--probe", action="store_true",
                    help="report which chat endpoint this build serves, and exit")
    ap.add_argument("--model", help="model or preset id (see --list)")
    ap.add_argument("--question", action="append", help="a question (repeatable)")
    ap.add_argument("--questions", help="file with one question per line, '|| regex' optional")
    ap.add_argument("--expect", help="regex the answer must contain (with --question)")
    ap.add_argument("--collection", help="knowledge collection id to attach explicitly")
    ap.add_argument("--runs", type=int, default=5, help="repetitions per question (default 5)")
    ap.add_argument("--compare", action="store_true",
                    help="run twice: as-configured, then with temperature 0 / top_k 1 / seed")
    ap.add_argument("--temperature", type=float)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--top-k", type=int, dest="top_k")
    ap.add_argument("--top-p", type=float, dest="top_p")
    ap.add_argument("--param", action="append", metavar="K=V",
                    help="any other parameter to send, repeatable (e.g. --param num_ctx=32768)")
    ap.add_argument("--instance", default=os.environ.get("RAG_BASE_URL", "http://localhost:3000"))
    ap.add_argument("--key-file", default=os.environ.get("RAG_KEY_FILE", "~/.rag_sync_key"))
    ap.add_argument("--timeout", type=int, default=300, help="per-request seconds (default 300)")
    ap.add_argument("--sleep", type=float, default=0.0, help="pause between runs")
    ap.add_argument("--warmup", action="store_true",
                    help="one throwaway request first, so model load time is excluded")
    ap.add_argument("--out", help="artifact folder (default ./determinism_<timestamp>)")
    ap.add_argument("--fail-under", type=float, metavar="FRAC",
                    help="exit non-zero if mean consistency is below this (e.g. 0.8)")
    a = ap.parse_args()

    base = a.instance.rstrip("/")
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {read_key(a.key_file)}",
                            "Content-Type": "application/json"})

    if a.list:
        cmd_list(session, base)
        return
    if a.probe:
        sys.exit(0 if cmd_probe(session, base, a.model) else 1)
    if not a.model:
        ap.print_help()
        print()
        info("start with:  python3 determinism_check.py --list")
        return

    questions = []
    if a.questions:
        questions += load_questions(a.questions)
    for q in a.question or []:
        questions.append((q, a.expect))
    if not questions:
        die("give --question TEXT or --questions FILE")

    params = {}
    if a.temperature is not None: params["temperature"] = a.temperature
    if a.seed is not None:        params["seed"] = a.seed
    if a.top_k is not None:       params["top_k"] = a.top_k
    if a.top_p is not None:       params["top_p"] = a.top_p
    for kv in a.param or []:
        k, _, v = kv.partition("=")
        try:
            params[k.strip()] = json.loads(v)
        except ValueError:
            params[k.strip()] = v

    out = pathlib.Path(a.out or f"determinism_{time.strftime('%Y%m%d_%H%M%S')}").expanduser()
    out.mkdir(parents=True, exist_ok=True)
    jsonl = out / "runs.jsonl"
    path_cache = [None]

    step(f"determinism_check {__version__}")
    info(f"instance   {base}")
    info(f"model      {a.model}")
    info(f"questions  {len(questions)}   runs each  {a.runs}"
         f"{'   x2 (compare)' if a.compare else ''}")
    info(f"params     {json.dumps(params) if params else 'none sent — measuring the preset as configured'}")
    info(f"artifacts  {out}")
    total = len(questions) * a.runs * (2 if a.compare else 1)
    warn(f"{total} generations to run; on a large model this takes a while")

    preflight_model(session, base, a.model)
    env = environment(session, base)
    if a.warmup:
        info("warmup request…")
        ask(session, base, a.model, "ping", params, a.collection, a.timeout, path_cache)

    passes = []
    label = "as-configured" if a.compare else "measured"
    passes.append({"label": label, "params": dict(params),
                   "results": run_set(session, base, a.model, questions, params,
                                      a.collection, a.runs, a.timeout, a.sleep,
                                      label, jsonl, path_cache)})
    if a.compare:
        pinned = dict(params); pinned.update(PINNED)
        passes.append({"label": "pinned", "params": pinned,
                       "results": run_set(session, base, a.model, questions, pinned,
                                          a.collection, a.runs, a.timeout, a.sleep,
                                          "pinned", jsonl, path_cache)})

    report = {"tool_version": __version__, "env": env, "model": a.model,
              "collection": a.collection, "runs": a.runs,
              "questions": [q for q, _ in questions], "passes": passes}
    (out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    text = render(report)
    (out / "report.txt").write_text(text + "\n")

    print()
    print(text)
    ok(f"artifacts written to {out}")

    mean = statistics.mean(r.get("consistency", 0)
                           for r in passes[-1]["results"]) if passes[-1]["results"] else 0.0
    if a.fail_under is not None and mean < a.fail_under:
        die(f"mean consistency {mean:.0%} is below --fail-under {a.fail_under:.0%}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\n  interrupted — partial results are in runs.jsonl")
