# breakerspace → carbonio

**Version 2026.9.30.1**

**From** the DGX Spark (user `feranick`, instance on :3002, ~103 GB unified memory)
**to** `carbonio.mit.edu` (user `nicola`, Ubuntu 26.04, RTX 5060 Ti 16 GB, instance on :3000,
served to users as **https://carbonio.mit.edu:8443** — Stage 7).

The Spark copy **stays running**. After this, the two are independent: documents,
notes, captures and chats added to one do not appear in the other.

## What changes, and why it matters

| | Spark | carbonio | Consequence |
|---|---|---|---|
| Model memory | ~103 GB unified | **~15 GB VRAM** | `qwen3.8:27b` (18 GB) no longer fits → new chat model |
| Chat + captions | `qwen3.8:27b-mtp-q4_K_M` | **`gemma4:12b-it-qat`** (7.2 GB, vision) | the preset's base model must be switched by hand |
| Ollama default context | 256k tier | **4k tier** (<24 GiB VRAM) | unpinned, RAG answers silently lose most retrieved context |
| Embedder | `bge-m3` | **`bge-m3`, same name** | a different name makes every stored vector useless |
| Port / home | :3002, `/home/feranick` | :3000, `/home/nicola` | the importer rewrites paths (`NEW_HOME`) and URLs (`URL_MAP`) |

What travels untouched inside the volume: accounts, the preset (knowledge attachments,
system prompt, pinned notes), both collections **with their vectors**, the
Breakerspace Calendar tool, and every existing figure caption.

Two 16 GB facts worth keeping in mind:
- The 5060 Ti moves memory faster than the Spark (~448 vs ~273 GB/s), so a model
  that fits the card will answer quickly.
- A 12B model is weaker than a 27B one at deciding to search. Stage 6 tests this
  before anyone depends on it.

---

## Stage 0 — Prepare carbonio

**Driver** — then reboot:

```bash
sudo ubuntu-drivers install
sudo reboot
nvidia-smi          # must show the RTX 5060 Ti with ~16 GB
```

**Docker** — follow docs.docker.com/engine/install/ubuntu, then let your user run it:

```bash
docker --version                     # installed?
sudo usermod -aG docker $USER && newgrp docker
```

**NVIDIA Container Toolkit** — this is what provides `nvidia-ctk`. Its apt repository
is distribution-independent, so it works on 26.04 even though 26.04 isn't on NVIDIA's
support list yet:

```bash
sudo apt-get update && sudo apt-get install -y --no-install-recommends ca-certificates curl gnupg2

curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit

sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
docker run --rm --gpus all nvidia/cuda:12.6.0-base-ubuntu24.04 nvidia-smi   # GPU visible in a container
```

**The repo, the probe, Ollama.** `--skip-openwebui` matters: the installer would
otherwise start an *empty* Open WebUI on port 3000, which is where breakerspace goes.

```bash
git clone https://github.com/feranick/Local_RAG_LLM ~/Software/Local_RAG_LLM
cd ~/Software/Local_RAG_LLM
python3 common/platform_probe.py --write     # expect: discrete, ~15 GB usable
bash management/setup_local_rag.sh --skip-openwebui --skip-anythingllm --skip-models
```

**Ollama server settings.** Written straight to the override file: in
`systemctl edit`, a line left with its `#`, or typed below the "Edits below this
comment will be discarded" marker, is silently dropped. `OLLAMA_HOST` is already in
the main unit, so the override doesn't repeat it. Paste unindented (`EOF` must start
its line):

```bash
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/override.conf >/dev/null <<'EOF'
[Service]
Environment="OLLAMA_CONTEXT_LENGTH=16384"
Environment="OLLAMA_NUM_PARALLEL=2"
EOF
sudo systemctl daemon-reload && sudo systemctl restart ollama
systemctl show ollama -p Environment --no-pager | tr ' ' '\n' | grep OLLAMA
journalctl -u ollama | grep -o 'OLLAMA_NUM_PARALLEL:[0-9]*' | tail -1    # → 2
```

- **`OLLAMA_CONTEXT_LENGTH=16384`** — pins the context server-wide, so nothing falls
  back to the 4k tier Ollama picks for cards under 24 GB.
- **`OLLAMA_NUM_PARALLEL=2`** — how many requests one model serves **at the same
  time**. Open WebUI has no limit of its own on simultaneous chats; Ollama's slots
  are the limit. With 1 (the default here), two users asking at once are answered
  one after the other — the second sees a spinner until the first finishes —
  and background tasks (titles, search queries) queue behind chats too. Each slot
  reserves its own context memory, so 2 slots at 16k cost twice the context VRAM;
  with `qwen3.5:9b` (~6 GB loaded) that fits the 16 GB card. More slots mean less
  waiting, not faster answers: the GPU is shared, so each answer streams a bit
  slower. Test with two chats started together: both should begin answering,
  `ollama ps` should stay at **100% GPU** and `nvidia-smi` below ~15 GB. If either
  slips, go back to 1 or lower `num_ctx`.
- Optional, **`OLLAMA_MAX_QUEUE=8`** — requests beyond the slots wait in a queue of
  up to 512 by default; a small queue makes an overloaded server answer with an
  error quickly instead of a long wait.

✅ *Check:* `curl -s localhost:11434/api/version` answers, and `docker ps` shows **no**
container on port 3000.

---

## Stage 1 — Export on the Spark

Copy `migrate_breakerspace.conf` to `~/` on the Spark. It moves only the breakerspace
volume, the two configs, the key and both state files — the documents were already
copied to carbonio separately, so `DOC_DIRS` is empty. It also asks the importer to
pull only `bge-m3` and `gemma4:12b-it-qat`, not everything the Spark has installed.

**Where the documents landed decides whether this works.** The state file is rewritten
to expect them at `/home/nicola/breakerspace`. If you put them somewhere else, add one
line to the config (the more specific rule wins over `NEW_HOME`):

```ini
PATH_MAP = /home/feranick/breakerspace=>/actual/path/on/carbonio
```

```bash
cd ~/Software/Local_RAG_LLM          # with the updated migrate_rag.py
python3 migration/migrate_rag.py --config ~/migrate_breakerspace.conf --export --dry-run
```

✅ *Check:* the dry run lists 1 volume, **0** document trees, and **5** sync files.

The container has to stop briefly so its database is captured in a consistent state —
a few minutes of downtime:

```bash
sudo docker stop open-webui-breakerspace
python3 migration/migrate_rag.py --config ~/migrate_breakerspace.conf --export
sudo docker start open-webui-breakerspace
```

---

## Stage 2 — Transfer

```bash
rsync -avP ~/rag_migration/ nicola@carbonio.mit.edu:~/rag_migration/
```

**If the two machines can't reach each other**, package the folder and move it by hand
(USB drive, or via a third machine that reaches both). The volume archive inside is
already compressed, so a plain `tar` is enough; the checksum proves nothing was
damaged on the way:

```bash
# on the Spark
cd ~ && tar cf breakerspace_migration.tar rag_migration
sha256sum breakerspace_migration.tar > breakerspace_migration.tar.sha256

# on carbonio, with both files in ~
cd ~ && sha256sum -c breakerspace_migration.tar.sha256 && tar xf breakerspace_migration.tar
```

> **Treat the package as sensitive.** It holds the API key, every account's password
> hash, all chats, and the uploaded copy of every document (Open WebUI keeps them in
> the volume). Don't park it on a shared or cloud drive unencrypted — if it has to go
> that way, `gpg -c breakerspace_migration.tar` first — and delete the copies once
> the import checks out.

✅ *Check on carbonio:* `du -sh ~/rag_migration` roughly matches the Spark, and
`manifest.json` is there.

---

## Stage 3 — Import on carbonio

```bash
cd ~/Software/Local_RAG_LLM
python3 migration/migrate_rag.py --config ~/rag_migration/migrate_rag.conf --import --dry-run
```

✅ *Check:* it shows `path rewrite: /home/feranick -> /home/nicola`,
`url rewrite: http://localhost:3002 -> http://localhost:3000`, and rewrites in both
`.conf` files and both state files. If it says *no path rewriting*, **stop** — that
is the duplicate-library trap.

```bash
python3 migration/migrate_rag.py --config ~/rag_migration/migrate_rag.conf --import
```

---

## Stage 4 — Start breakerspace on :3000

The same settings the Spark container runs with, published on 3000. The named volume
already holds all the data:

```bash
docker run -d --name open-webui-breakerspace --restart always \
  -p 3000:8080 --gpus all \
  --ulimit nofile=65536:65536 \
  --add-host=host.docker.internal:host-gateway \
  -e OLLAMA_BASE_URL=http://host.docker.internal:11434 \
  -e RAG_OLLAMA_BASE_URL=http://host.docker.internal:11434 \
  -e RAG_EMBEDDING_ENGINE=ollama -e RAG_EMBEDDING_MODEL=bge-m3 \
  -e RAG_EMBEDDING_BATCH_SIZE=32 -e CHUNK_SIZE=1500 -e CHUNK_OVERLAP=200 \
  -e WEBUI_NAME=breakerspace -e ENABLE_API_KEY=true \
  -e ENABLE_SIGNUP=true -e DEFAULT_USER_ROLE=pending \
  -v open-webui-breakerspace:/app/backend/data \
  ghcr.io/open-webui/open-webui:main

docker logs --tail 60 open-webui-breakerspace 2>&1 | grep -iE 'migrat|alembic|error'
```

The image pulled here may be newer than the Spark's 0.11.3; its first start upgrades
the database, and the log line above shows whether that went cleanly.

✅ *Check:* log in at `http://carbonio.mit.edu:3000` with your **existing** account.
Both collections are listed under Workspace → Knowledge with their document counts.

> carbonio has a campus hostname, and Open WebUI serves plain HTTP. With
> `ENABLE_SIGNUP=true` anyone who can reach port 3000 can register — as *pending*,
> so they need your approval. Set it to `false` if nobody new should join.
> Stage 7 puts the instance behind HTTPS and closes plain HTTP to the network.

---

## Stage 5 — What doesn't carry over by itself

**1. The preset's base model.** It still points at the qwen model, which isn't installed
here. Open the preset's **editor** (pencil icon, or `…/workspace/models/edit?id=<preset-id>`):

- **Base Model** → `gemma4:12b-it-qat`
- **Advanced Params** → `num_ctx` = **16384** (the same as the server setting, so
  Ollama loads the model once, not twice), `temperature` ≈ **0.3**
- Knowledge, Tools (the calendar), the system prompt and the pinned notes are already
  there — just confirm they are.

**2. Admin settings that name a model.** In Admin Panel → Settings:
- **Documents** → embedding model shows `bge-m3`.
- **Interface → Task Model** → a small model **without a thinking mode**, not the
  chat model: `ollama pull llama3.2:3b` (~2 GB), select it (local and external, if
  both are listed), and hide it from the selector. Background tasks (titles, tags,
  search queries) sent to a thinking chat model reason at length over each one;
  Ollama serves one request at a time, so a question queued behind them waited
  **over a minute** before retrieval even started. Measured on carbonio:
  chat model + `bge-m3` + `llama3.2:3b` fit comfortably in the 16 GB.
- **Interface → Retrieval query generation** → **off**. It's an extra model call
  before every retrieval; with it off, the question is searched as typed — as good
  for direct questions, and the answer starts within seconds. Also off unless
  wanted: web search query generation, tags, follow-ups, autocomplete. Keep title
  generation (a second or two on the small model).

> Diagnosing a slow start: `journalctl -u ollama -f | grep GIN` while asking. A
> healthy question shows `/api/embed` within about a second, then the answer's
> `/api/chat`. Minute-long `/api/chat` lines **before** the embed are queued
> background tasks.

**Optional: keep models resident.** Ollama unloads idle models after 5 minutes, so
the first chat after a pause loads them again. On carbonio that costs only a few
seconds (a ~6 GB model from local disk), and measured, the difference was minimal —
so it's **not set**: unloading frees the VRAM when nobody is chatting, which leaves
room for sync runs with figure captioning. Worth adding if the chat model grows, or
loads from slow storage — one more line in the override above:

```
Environment="OLLAMA_KEEP_ALIVE=-1"     # or e.g. 1h; `ollama ps` → UNTIL: Forever
```

then `sudo systemctl daemon-reload && sudo systemctl restart ollama`.

**3. The figure captioner** in `~/breakerspace.conf`, for documents added from now on:

```ini
FIGURE_MODEL   = gemma4:12b-it-qat
FIGURE_NUM_CTX = 16384
```

Existing captions (written by qwen3.8 on the Spark) are kept; `--status` will show a
mixed inventory by model, and nothing needs redoing.

---

## Stage 6 — Verify, in this order

```bash
cd ~/Software/Local_RAG_LLM          # the paths below are relative to the repo
bash management/llm_stack_healthcheck.sh
python3 sync/sync_folder.py --config ~/breakerspace.conf --status
python3 sync/sync_folder.py --config ~/lab_notes_breakerspace.conf --status
```

✅ *Check:* **`0 to go`** on both. This is also the real test of your manual copy: the
state file records each document's content hash, so any file that didn't arrive
intact, or was changed since, shows up here. A number close to the whole library
means the path rewrite didn't take — do not sync until it's fixed. A handful means a
handful of files differ, and a normal sync re-uploads just those.

Then in a **new** chat on the preset:

1. A question you know the answer to from the documents, **without** `#`. It should
   cite sources. This is the test the 12B model might fail: if it answers from memory,
   set the preset's **Function Calling = Legacy**, which retrieves on every question
   regardless of the model's judgment.
2. *"What trainings are on this week?"* — the status line "Checking the Breakerspace
   calendar…" should appear before the answer.
3. While a model is loaded:

   ```bash
   ollama ps     # PROCESSOR 100% GPU, CONTEXT 16384
   ```

   Anything less than 100% GPU means the context pushed part of the model into system
   RAM; lower `num_ctx` (and `OLLAMA_CONTEXT_LENGTH`) to 12288.

For a number rather than an impression — and a baseline to compare against later:

```bash
determinism_check --instance http://localhost:3000 \
  --key-file ~/.rag_sync_key_open-webui-breakerspace \
  --model <preset-id> --collection 4cc8efc4-2fe5-4071-abc4-d584390bb7b4 \
  --question "Where are the polished cross-section samples stored?" \
  --expect "sample cabinet" --runs 3
```

---

## Stage 7 — HTTPS on :8443

Open WebUI doesn't serve TLS itself. Apache — already running on carbonio with a
Let's Encrypt certificate — terminates HTTPS and forwards to the container, which is
then reachable from carbonio only.

**Why port 8443.** Apache's site root on 443 belongs to `data_collector`, and Open
WebUI can't live under a sub-path, so it gets its own port with the same certificate.
Port 80 is held by Tor, deliberately, so nothing here uses it.

**1. Apache site first** — so the switch below costs only seconds of downtime:

```bash
sudo a2enmod proxy proxy_http proxy_wstunnel headers ssl
```

Paste the next block **unindented**: the heredoc only ends at an `EOF` at the very
start of a line.

```bash
sudo tee /etc/apache2/sites-available/breakerspace.conf >/dev/null <<'EOF'
Listen 8443
<VirtualHost *:8443>
    ServerName carbonio.mit.edu
    SSLEngine on
    SSLCertificateFile    /etc/letsencrypt/live/carbonio.mit.edu/fullchain.pem
    SSLCertificateKeyFile /etc/letsencrypt/live/carbonio.mit.edu/privkey.pem

    ProxyPreserveHost On
    ProxyTimeout 600
    RequestHeader set X-Forwarded-Proto "https"
    ProxyPass        / http://127.0.0.1:3000/ upgrade=websocket flushpackets=on
    ProxyPassReverse / http://127.0.0.1:3000/
</VirtualHost>
EOF
sudo a2ensite breakerspace
sudo ufw status | grep -q active && sudo ufw allow 8443/tcp
sudo apache2ctl configtest && sudo systemctl reload apache2
```

`upgrade=websocket` carries live chat updates; `flushpackets=on` streams answers token
by token instead of all at once; `ProxyTimeout 600` covers long answers.

**2. Recreate the container local-only.** Same as Stage 4, with three changes — the
`127.0.0.1:` binding, `WEBUI_URL`, and secure cookies. The volume carries the data:

```bash
docker stop open-webui-breakerspace && docker rm open-webui-breakerspace
docker run -d --name open-webui-breakerspace --restart always \
  -p 127.0.0.1:3000:8080 --gpus all --ulimit nofile=65536:65536 \
  --add-host=host.docker.internal:host-gateway \
  -e OLLAMA_BASE_URL=http://host.docker.internal:11434 \
  -e RAG_OLLAMA_BASE_URL=http://host.docker.internal:11434 \
  -e RAG_EMBEDDING_ENGINE=ollama -e RAG_EMBEDDING_MODEL=bge-m3 \
  -e RAG_EMBEDDING_BATCH_SIZE=32 -e CHUNK_SIZE=1500 -e CHUNK_OVERLAP=200 \
  -e WEBUI_NAME=breakerspace -e ENABLE_API_KEY=true \
  -e ENABLE_SIGNUP=true -e DEFAULT_USER_ROLE=pending \
  -e WEBUI_URL=https://carbonio.mit.edu:8443 \
  -e WEBUI_SESSION_COOKIE_SECURE=true -e WEBUI_AUTH_COOKIE_SECURE=true \
  -v open-webui-breakerspace:/app/backend/data \
  ghcr.io/open-webui/open-webui:main
```

The `127.0.0.1:` binding is what actually closes plain HTTP: Docker's published ports
bypass `ufw`, so a firewall rule alone would leave :3000 open to the campus. The local
tools (sync, capture, healthcheck) keep using `http://localhost:3000` unchanged.

**3. WebUI URL in the admin panel.** Admin → System → General → **WebUI URL** →
`https://carbonio.mit.edu:8443` → Save. A value saved in the database — even an empty
one — overrides the `-e WEBUI_URL`, so set it here once. It's used for links Open
WebUI generates itself (shared chats, notifications).

✅ *Check:*

```bash
curl -sI https://carbonio.mit.edu:8443 | head -1        # HTTP/1.1 200
```

From another machine, `curl -m 5 http://carbonio.mit.edu:3000` must **fail** to
connect. Then log in at **https://carbonio.mit.edu:8443** and confirm an answer
streams in word by word — all at once means the proxy is buffering.

### Certificate renewal

Let's Encrypt validates over port 80, which Tor holds, so automatic renewal can't
succeed — and a failed attempt can **stop Apache** (certbot's temporary config fails
to restart it), taking `data_collector` and breakerspace down together. So the
automatic timer is off and renewal is manual:

```bash
systemctl list-timers | grep -i certbot
sudo systemctl disable --now certbot.timer      # or snap.certbot.renew.timer
```

Before each expiry (`sudo certbot certificates` shows the date):

```bash
sudo systemctl stop tor
sudo certbot renew
sudo systemctl start tor
sudo systemctl reload apache2                   # both 443 and 8443 pick up the new cert
systemctl is-active apache2                     # must say: active
```

---

## Running two copies

Both machines now hold a breakerspace with the same collection ids, on different
hosts. Nothing links them:

- Syncing new documents on one doesn't update the other. Run the sync on each, or
  decide which machine is canonical and only sync there.
- `Remember:` captures belong to the instance you typed them in. Harvest each
  instance with its own config, and both write into their own machine's notes folder.
- The Spark's configs are unchanged (still :3002, `/home/feranick`). The importer only
  rewrote the copies on carbonio.
