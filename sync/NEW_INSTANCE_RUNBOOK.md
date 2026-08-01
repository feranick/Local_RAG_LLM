# New library runbook — a second RAG instance

**Version 2026.08.01.1**

Every command in order, with a checkpoint after each stage. Use this when you want
an **independent library** — its own documents, its own collection, and (the usual
reason) its **own embedding model**.

Why a separate instance: in Open WebUI the embedding model is a **global** setting,
so two collections in one instance must share it, and changing it invalidates
everything already indexed. A second container gets its own data volume — own
settings, accounts, collections, embedding model — while sharing your existing
Ollama, so the same chat models remain available.

> **If you don't need a different embedding**, skip all of this: create another
> Knowledge collection in your existing instance and give it its own config file
> with a distinct `TARGET`, `WATCH_DIR` and `STATE_FILE`. See *Running a second
> library → A* in `README_sync.md`.

Set these once for the session:

```bash
SW=~/Software/Local_RAG_LLM/script     # where the scripts live
NAME=reports                           # library name (lower-case; used for files)
COLL_NAME=Reports                      # collection NAME shown in the UI
PORT=3002                              # host port for the new instance
EMBED=bge-m3                           # embedding model for THIS library
DOCS=~/reports                         # folder holding this library's documents
```

> **Name vs id.** `COLL_NAME` is the label you see in the UI. Every API call and the
> `TARGET` entry in the config file need the collection's **id** (a UUID), which
> Stage 1 creates and writes into `<NAME>.conf` for you. Passing a name to an API
> endpoint returns `{"detail":"We could not find what you're looking for :/"}`.

---

## Stage 0 — Prerequisites

```bash
cd $SW
python3 -c "import requests; print('requests OK')"
docker ps >/dev/null && echo "docker OK"
curl -m 5 http://localhost:11434/api/version && echo " ollama OK"
ss -tlnp 2>/dev/null | grep -q ":$PORT " && echo "!! PORT $PORT IS IN USE" || echo "port $PORT free"
```

✅ *Check:* requests + docker + Ollama all OK, and the port is free. Ports 3000
(Open WebUI) and 3001 (AnythingLLM) are already taken by the first stack.

---

## Stage 1 — Create the instance

One command does everything: pulls the embedding model, launches the container with
all settings pre-applied, creates the admin account, the API key, the Knowledge
collection, and a matching config file.

```bash
mkdir -p $DOCS
python3 new_rag_instance.py \
  --collection $COLL_NAME --embed-model $EMBED --port $PORT \
  --name open-webui-$NAME --watch-dir $DOCS \
  --email you@example.com
```

It prompts for the admin password (or pass `--password`). Add `--dry-run` first if
you want to inspect the `docker run` command without executing it.

**Pre-configured for you** — nothing to set in the UI: embedding engine (Ollama) +
model, chunk size/overlap, embedding batch size 32 (avoids the "too many open
files" failure on large documents), API keys enabled, new signups set to `pending`,
the file-descriptor ulimit, and the `host.docker.internal` route to Ollama.

✅ *Check:* the DONE block prints a collection id (not `PASTE_COLLECTION_ID_HERE`),
a key file, and a config file. Then:

```bash
curl -m 5 http://localhost:$PORT/health && echo " instance OK"
cat $NAME.conf                      # TARGET / BASE_URL / KEY_FILE / STATE_FILE filled in
```

If the collection id came back empty, create it in the UI and paste the id into
`$NAME.conf`.

**If the API key step warns** (Open WebUI ≥0.11 often ships the personal-API-key
feature disabled, so *Settings → Account* shows no "API Keys" section at all), the
script tries to enable it via the admin config, and failing that writes the **login
token** to the key file so you can sync immediately. That token expires, so swap in
a real key when convenient — either:

```bash
# UI route
#   Admin → Settings → Authentication → Enable API Key (Save)
#   Settings → Account → API Keys → create  (starts with sk-)
echo 'sk-...' > ~/.rag_sync_key_open-webui-$NAME

# or mint it from the shell
BASE=http://localhost:$PORT
TOK=$(curl -s -X POST $BASE/api/v1/auths/signin -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"YOURPASS"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("token",""))')
curl -s -X POST $BASE/api/v1/auths/api_key -H "Authorization: Bearer $TOK"; echo
```

---

## Stage 2 — Add the documents

```bash
cp /path/to/your/pdfs/*  $DOCS/
python3 library_stats.py $DOCS
```

✅ *Check:* the counts look right, and "have the full PDF" is high. If most entries
are metadata-only stubs, fix that before indexing — see `REBUILD_RUNBOOK.md`
(the proxify + browser-pass workflow).

---

## Stage 3 — Sync

```bash
python3 sync_folder.py --config $NAME.conf
```

The first two lines echo the configuration in effect — **read them before letting a
long run proceed**:

```
[sync] config: /home/you/.../reports.conf
[sync] openwebui at http://localhost:3002 | target=<id> | dir=/home/you/reports | state=.rag_sync_state_open-webui-reports.json
```

Confirm the port, target and state file are the *new* ones, not your papers
library. Then let it run (use `tmux`/`screen` — with `--describe-figures` this takes
hours for a big folder).

✅ *Check:* the closing `[sync] done —` line shows files **added**, with
`already-present` near zero, plus the per-type breakdown.

---

## Stage 4 — Use it

Open `http://<host>:$PORT`, log in with the account from Stage 1, then:

1. **Settings → General → Default Model** → your chat model (both instances share
   Ollama, so the same models are listed).
2. Optionally hide the embedding model from the chat list:
   **Admin → Settings → Models** → toggle `$EMBED` off.
3. New chat → type `#`, click the collection → ask a question and confirm it cites
   real documents.

---

## Housekeeping

**What was created**

| Thing | Name |
|-------|------|
| container | `open-webui-<NAME>` |
| docker volume | `open-webui-<NAME>` |
| API key file | `~/.rag_sync_key_open-webui-<NAME>` |
| sync state | `~/.rag_sync_state_open-webui-<NAME>.json` |
| sync config | `<NAME>.conf` |

**Routine re-sync** (only new/changed files are touched):

```bash
python3 sync_folder.py --config $NAME.conf
python3 sync_folder.py --config $NAME.conf --prune    # mirror deletions too
```

**Update it** — `update_local_rag.sh` does *not* know about this container:

```bash
sudo docker pull ghcr.io/open-webui/open-webui:main
sudo docker rm -f open-webui-$NAME
# re-run the Stage 1 command; the volume (and all data) is preserved
```

**Remove it** — `uninstall_local_rag.sh` doesn't know about it either:

```bash
sudo docker rm -f open-webui-$NAME
sudo docker volume rm open-webui-$NAME          # deletes this library's data
rm -f ~/.rag_sync_key_open-webui-$NAME ~/.rag_sync_state_open-webui-$NAME.json $NAME.conf
```

---

## Notes and gotchas

- **One GPU, shared.** Both instances use the same Ollama, so heavy work in one
  queues behind the other. If the UI stalls during a big sync, see the "OI splash"
  entry in `README_sync.md`.
- **Keys are per-instance.** The new API key only works against this port; that's
  why each instance gets its own `KEY_FILE`.
- **Distinct `STATE_FILE` is mandatory.** Sharing one across libraries makes every
  run look like a full re-sync.
- **Comparing embeddings?** Point both instances at the *same* `WATCH_DIR` with
  different `EMBED` values and different state files — then ask both the same
  question. That's the only honest way to judge retrieval quality.
