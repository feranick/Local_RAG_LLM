# Full library rebuild — runbook

**Version 2026.07.28.1**

Every command, in order, with a verification checkpoint after each stage. Don't
move to the next stage until its check passes — that's the whole point of the
checkpoints, since a silent failure early on wastes hours downstream.

Adjust these paths to your setup:

```bash
SW=~/literature/flash-literature-mit-retrieved            # where the scripts live
OUT=$SW/access_all_papers           # proxify's output folder (named after the input CSV)
LIB=~/literature/flash              # the folder the RAG sync watches
CSV=$SW/access_all_papers.csv       # your metadata CSV
KEY=$(cat ~/.rag_sync_key)
COLL=c411c9dc-289a-4e4c-bfa9-c5fab84d22c6      # Open WebUI knowledge collection id
```

---

## Stage 0 — Diagnose the previous run (2 minutes, do this first)

If a browser pass ran before, this says exactly what it achieved:

```bash
python3 - <<'PY'
import csv, collections, pathlib, sys
p = pathlib.Path.home()/"Software/access_all_papers/browser_results.csv"
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
python3 proxify.py --version                      # expect 2026.07.27.3
grep -c curl_result_is_good_enough fetch_browser.py   # expect 2 (patched)
grep -c _wait_for_extraction sync_folder*.py          # expect 2 (patched sync)

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

```bash
curl -sS -X POST "http://localhost:3000/api/v1/knowledge/$COLL/reset" \
  -H "Authorization: Bearer $KEY"; echo
curl -sS -X DELETE "http://localhost:3000/api/v1/files/all" \
  -H "Authorization: Bearer $KEY"; echo
rm -f ~/.rag_sync_state.json

python3 $SW/sync_folder_nicola.py --describe-figures --ocr-fallback
```

Run it in `tmux`/`screen` — with figure descriptions this takes hours.

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
