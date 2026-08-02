# Moving the stack to another machine — runbook

**Version 2026.08.02.1**

Every command in order, with a checkpoint after each stage. The goal is a new
machine where the libraries are **already indexed** — no re-embedding, no
duplicates.

## What moves, and what doesn't

| Thing | How it travels | Why |
|-------|----------------|-----|
| Open WebUI **docker volume** | archived and restored | holds accounts, settings, collections **and the vectors** — this is what saves you the re-index |
| Document folders | archived and restored | the source of truth for future syncs |
| `<lib>.conf`, key file, state file | copied, then **path-rewritten** | the state file is what makes a re-sync a no-op |
| AnythingLLM storage | archived, ownership fixed to UID 1000 | it refuses to start otherwise |
| Ollama **models** | **re-pulled, never copied** | the Spark runs a custom Blackwell build; a generic host runs stock Ollama. Weights are portable, the runtime isn't |
| Containers | recreated with the same `docker run` | the volume carries the data, not the container |

> **The one trap.** Sync state is keyed on **absolute paths**. If the documents
> land at a different path, every entry misses, the next sync treats all files as
> new, and you get a **duplicate of the whole library** on top of the restored
> collection. The importer detects this and rewrites the paths — but only if you
> tell it the new home (`NEW_HOME` / `PATH_MAP`).

---

## Stage 0 — Inventory (old machine)

```bash
cd ~/Software/Local_RAG_LLM/management
sudo docker ps --format '{{.Names}}\t{{.Ports}}'
sudo docker volume ls
ollama list
ls ~/.rag_sync_key* ~/.rag_sync_state*.json
```

✅ *Check:* you can name every volume, every document folder, and every
`.conf`/key/state file. Anything you can't name here won't be migrated.

---

## Stage 1 — Configure the migration (old machine)

```bash
python3 migrate_rag.py --init-config
$EDITOR migrate_rag.conf
```

Fill in `VOLUMES`, `DOC_DIRS`, `SYNC_FILES`, `EXPORT_DIR`. Leave `NEW_HOME` empty
for now — it's an import-side setting.

```bash
python3 migrate_rag.py --export --dry-run
```

✅ *Check:* the dry run lists every volume, folder and file you expect, and
`EXPORT_DIR` sits on a disk with room for the volumes (a library of a few thousand
documents typically means a few GB of vector data).

---

## Stage 2 — Export (old machine)

**Stop the containers first.** Archiving a live SQLite database can capture a torn
state; the script warns and asks, but stopping is the correct answer.

```bash
sudo docker stop open-webui open-webui-breakerspace anythingllm
python3 migrate_rag.py --export
```

✅ *Check:* the DONE block reports the expected number of volumes, trees and files,
and `manifest.json` exists in the export folder:

```bash
python3 -m json.tool ~/rag_migration/manifest.json | head -20
```

Restart the old stack if you still need it (`sudo docker start …`) — the export is
a copy, nothing was moved.

---

## Stage 3 — Transfer

```bash
rsync -avP ~/rag_migration/ newhost:~/rag_migration/
```

✅ *Check:* on the new machine, `du -sh ~/rag_migration` matches the source, and
`manifest.json` is present.

---

## Stage 4 — Prerequisites on the new machine

```bash
docker --version && docker info >/dev/null && echo "docker OK"
curl -m 5 http://localhost:11434/api/version && echo " ollama OK"
```

If Ollama isn't installed yet, install it before importing so the models can be
pulled in the same pass. On a non-Spark host, use stock Ollama.

✅ *Check:* both commands succeed.

---

## Stage 5 — Import

Edit the copied config **before** running this:

```bash
cd ~/rag_migration
$EDITOR migrate_rag.conf      # set NEW_HOME if the username/home differs
                              # or PATH_MAP for a different documents location
python3 migrate_rag.py --import --config ~/rag_migration/migrate_rag.conf --dry-run
```

✅ *Check:* the dry run shows the path rewrites you expect. If it says
`no path rewriting` but your home directory *has* changed, stop and set
`NEW_HOME` — this is the duplication trap.

```bash
python3 migrate_rag.py --import --config ~/rag_migration/migrate_rag.conf
```

It asks before overwriting an existing volume and before each rewrite (`--yes`
skips the prompts). Backups are left as `.bak` next to every file it edits.

✅ *Check:* volumes restored, documents extracted, `N path(s) rewritten` for both
the `.conf` and the state file.

---

## Stage 6 — Start the containers

Use the same `docker run` as before. For a second instance, the easiest way to get
it exactly right is:

```bash
python3 new_rag_instance.py --collection Breakerspace --embed-model bge-m3 \
  --port 3002 --name open-webui-breakerspace --dry-run
```

Copy the printed command, and run it **as-is** — the named volume already exists
and carries all the data, so the container comes up with your collections intact.
Keep `--add-host=host.docker.internal:host-gateway` and
`--ulimit nofile=65536:65536`.

```bash
curl -m 5 http://localhost:3002/health && echo " instance OK"
```

✅ *Check:* you can log in with the **same account and password** as on the old
machine (the volume carries the user database), and the collection is listed under
Workspace → Knowledge with its document count.

---

## Stage 7 — Verify, in the right order

```bash
python3 migrate_rag.py --verify --config ~/rag_migration/migrate_rag.conf
```

Then the test that actually matters:

```bash
python3 sync_folder.py --config <lib>.conf --status
```

✅ *Check:* **`0 to go`**. A number close to the whole library means the path
rewrite didn't apply — fix it *before* running a sync, or you'll duplicate
everything:

```
[sync] [########################################] 2772/2772 processed (100%)
[sync] folder holds 2772 file(s): 2772 processed, 0 to go
```

Last, a real question in the UI: new chat → `#` → click the collection → confirm
the answer cites real documents. That exercises the restored vectors end to end.

---

## Stage 8 — Models

```bash
python3 manage_models.py --list
```

If the import didn't pull them, the manifest recorded exactly what was installed:

```bash
python3 -c "import json;print('\n'.join(json.load(open('$HOME/rag_migration/manifest.json'))['models']))"
```

✅ *Check:* the chat model and — critically — **the embedding model under the exact
same name** are both present. A missing or renamed embedder makes every stored
vector unusable, which looks like "retrieval suddenly returns nothing".

---

## Troubleshooting

**`--status` shows the whole library as "to go".** The path rewrite didn't happen.
Don't sync. Restore the `.bak` files if needed, set `NEW_HOME`/`PATH_MAP`, and
re-run the import — or rewrite the state file by hand (see `README_sync.md`).

**Login fails on the new machine.** The volume didn't restore. `docker volume ls`,
then check the import output for that volume.

**Collection exists but answers cite nothing.** The embedding model is missing or
named differently. `python3 manage_models.py --list` and compare with the manifest.

**AnythingLLM crash-loops with `unable to open database file`.** Ownership: the
importer runs `chown -R 1000:1000` on its storage directory, which needs `sudo`. If
it was skipped, run it manually.

**Different architecture (ARM Spark → x86, or the reverse).** The Open WebUI image
is multi-arch and the volume contents are portable. Ollama models are re-pulled, so
they're fine too. What does *not* transfer is any expectation about speed: a
smaller machine may not hold the same models in memory —
`python3 manage_models.py --tags <model>` shows what fits.
