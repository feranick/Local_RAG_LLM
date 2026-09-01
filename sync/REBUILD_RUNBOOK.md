# Full library rebuild — runbook

**Version 2026.08.13.1**

Every command, in order, with a verification checkpoint after each stage. Don't
move to the next stage until its check passes — that's the whole point of the
checkpoints, since a silent failure early on wastes hours downstream.

For creating a *second, independent* library instead, see `NEW_INSTANCE_RUNBOOK.md`.

Adjust these paths to your setup (they are yours already):

```bash
SW=~/literature/flash-literature-mit-retrieved            # where the scripts live
OUT=$SW/access_all_papers           # proxify's output folder (named after the input CSV)
LIB=~/literature/flash              # the folder the RAG sync watches
CSV=$SW/access_all_papers.csv       # your metadata CSV
KEY=$(cat ~/.rag_sync_key)
SYNC=$SW/sync_folder_nicola.py         # your copy of sync_folder.py
CONF=$SW/sync_folder.conf              # its configuration file (see README_sync.md)
BASE=http://localhost:3000             # the instance this library lives in
```

The API needs the collection's **id** (a UUID), never its display name — a name
gives `{"detail":"We could not find what you're looking for :/"}`. It's the same
value as `TARGET` in your config file, so read it from there rather than retyping it:

```bash
TARGET=$(sed -n 's/^ *TARGET *= *//p' $CONF | sed 's/[#;].*//' | tr -d '"'"'" | xargs)
echo "$TARGET"        # e.g. c411c9dc-289a-4e4c-bfa9-c5fab84d22c6
```

If that comes back empty, list what the instance has and copy the id:

```bash
curl -s "$BASE/api/v1/knowledge/list" -H "Authorization: Bearer $KEY" \
| python3 -c 'import sys,json
d=json.load(sys.stdin); d=d if isinstance(d,list) else d.get("data",[])
[print(k.get("id"),"|",k.get("name")) for k in d]'
```

> **Naming, so the commands below are unambiguous:** `TARGET` is always the **id**
> used by the API and by `sync_folder.conf`. A collection's human-readable *name*
> (what you click in the UI, e.g. `breakerspace`) is never accepted by these
> endpoints.

---

## Stage 0 — Diagnose the previous run (2 minutes, do this first)

If a browser pass ran before, this says exactly what it achieved:

```bash
python3 - <<PY
import csv, collections, pathlib, sys
p = pathlib.Path("$OUT/browser_results.csv")
if not p.exists():
    print("browser_results.csv NOT FOUND -> fetch_browser.py never completed a run"); sys.exit()
rows = list(csv.DictReader(p.open()))
print(f"{len(rows)} link(s) processed by fetch_browser:")
for k, v in collections.Counter(r["status"] for r in rows).most_common():
    print(f"   {v:6d}  {k}")
PY
```

- `playwright-missing` or all `failed` → the browser never actually ran (see Stage 1).
- File not found → the browser pass never completed.
- Mostly `pdf` / `abstract` → it did work, and the problem is elsewhere.

---

## Stage 1 — Prerequisites

```bash
cd $SW
python3 proxify.py --version                      # expect today's version
grep -c curl_result_is_good_enough fetch_browser.py   # expect 2 (patched)
grep -c _wait_for_extraction $SYNC                    # expect 2 (patched sync)

# Playwright must be importable AND have its Chromium build
python3 -c "from playwright.sync_api import sync_playwright; print('playwright OK')"
python3 -m playwright install chromium             # safe to re-run
python3 -m playwright install-deps chromium        # Linux system libs, if needed

export LIBPROXY_HOST=libproxy.mit.edu
```

**Re-export `cookies.txt` from your logged-in browser now.** An expired session
returns login pages that look identical to a paywall — the single most common
cause of a whole run coming back empty.

✅ *Check:* `playwright OK` printed, versions correct, cookies file is minutes old.

---

## Stage 2 — Extract (proxify)

```bash
cd $SW
rm -rf $OUT                     # start clean so counts are meaningful
python3 proxify_csv.py $CSV -r -g -j 40 -d -c cookies.txt
```

**Keep `-g`.** It rewrites landing URLs to direct-PDF URLs, which is what makes
most PDFs land at all. Its known downside — when a direct-PDF fetch is blocked,
the HTML that comes back is a viewer shell with no abstract, so the `.md` fallback
is metadata-only — is exactly what Stage 3 repairs via the DOI-landing fallback.
Dropping `-g` would trade a large number of PDFs for a few more abstracts: a bad
trade, since the browser pass recovers both.

✅ *Check:*

```bash
ls $OUT/downloads | wc -l          # PDFs so far
ls $OUT/abstract_failed | wc -l    # .md records so far
wc -l < $OUT/failed.csv            # links for the browser to retry
```

---

## Stage 3 — Browser pass (the stage that decides library quality)

Test five links first and **read the output**:

```bash
python3 fetch_browser.py $OUT/failed.csv -c cookies.txt --headful --limit 5 -v
```

✅ *Check — at least one of these must be true:*

```bash
# a real abstract now present in a fresh .md?
grep -l '## Abstract' $OUT/abstract_failed/*.md | head
# or new PDFs appeared?
ls -lt $OUT/downloads | head
```

If all five came back as metadata-only stubs, **stop** — fix that before running
1200 of them. Then the full passes:

```bash
python3 fetch_browser.py $OUT/failed.csv        -c cookies.txt
python3 fetch_browser.py $OUT/needs_browser.csv -c cookies.txt
```

Note: `fetch_browser.py $OUT` (the folder form) only reads `needs_browser.csv` —
`failed.csv` must be named explicitly. Skipping it is why a library ends up
mostly metadata-only.

✅ *Check:*

```bash
ls $OUT/downloads | wc -l                                  # should be well up
grep -l '## Abstract' $OUT/abstract_failed/*.md | wc -l     # real abstracts
python3 -c "import csv,collections;print(collections.Counter(r['status'] for r in csv.DictReader(open('$OUT/browser_results.csv'))))"
```

---

## Stage 4 — Consolidate

```bash
rm -rf $LIB && mkdir -p $LIB
cp $OUT/downloads/*       $LIB/ 2>/dev/null
cp $OUT/abstract_failed/* $LIB/ 2>/dev/null

# drop metadata-only stubs for papers whose PDF you now have
cd $LIB
for f in *.md; do b="${f%.md}"; [ -f "$b.pdf" ] && \
  grep -qi "no abstract could be extracted" "$f" && rm "$f"; done
```

---

## Stage 5 — Quality gate

```bash
python3 $SW/library_stats.py $LIB
```

✅ *Check:* "have the full PDF" should be far above 15%, and "metadata only" far
below 84%. **If it isn't, do not index — the problem is upstream in Stage 3.**
Use `--list` to see which papers are still content-free.

---

## Stage 6 — Index

Check the sync configuration first — settings now live in a config file, not inside
the script, so upgrading `sync_folder.py` never means re-editing it:

```bash
cat $CONF     # TARGET, WATCH_DIR, KEY_FILE, STATE_FILE — all correct?
```

Empty the collection, then clear the state so the sync treats every file as new.
`$TARGET` here is the **id** from the top of this runbook, not the collection's name:

```bash
curl -sS -X POST "$BASE/api/v1/knowledge/$TARGET/reset" \
  -H "Authorization: Bearer $KEY"; echo
curl -sS -X DELETE "$BASE/api/v1/files/all" \
  -H "Authorization: Bearer $KEY"; echo
rm -f ~/.rag_sync_state.json        # use the STATE_FILE named in $CONF

python3 $SYNC --config $CONF --describe-figures --ocr-fallback
```

`reset` returns the collection's JSON on success. A `404 We could not find what
you're looking for :/` means `$TARGET` holds a name instead of the id, or the key
belongs to a different instance than `$BASE`.

`DELETE /files/all` removes **every** uploaded file in that instance, across all
collections — correct for a single-library rebuild, wrong if the instance hosts
another library. Skip it in that case; `reset` alone is enough, at the cost of
leaving orphaned file objects that the duplicate check still compares against.

Run it in `tmux`/`screen` — with figure descriptions this takes hours. Progress is
numbered with an ETA, and from another terminal:

```bash
python3 $SYNC --config $CONF --status
```

✅ *Check:* the closing `[sync] done —` line shows nearly everything **added**,
`already-present` near zero, and the per-type breakdown matches Stage 5.

---

## Stage 7 — Verify in the UI

- **Workspace → Knowledge → Papers** — file count in the right ballpark.
- New chat → select the chat model → type `#`, click the collection → ask a
  question you know the answer to, and confirm it cites real papers.

---

## If the browser pass yields nothing

In order of likelihood: expired `cookies.txt`; Playwright installed but Chromium
missing (`python3 -m playwright install chromium`); a CAPTCHA that needs one
manual `--headful` pass; or the publisher genuinely isn't subscribed — the browser
defeats bot-walls, not paywalls. Run with `-v` to see per-link timing and exit
codes.
