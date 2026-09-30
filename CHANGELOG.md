# Changelog

All notable changes to this project. Versions are date-based
(`YYYY.M.D.N`), and every script, document and the wheel share one version string.

## [2026.9.30.1]

Shaped by the first real migration: moving a live library from the DGX Spark to a
16 GB workstation under a different user, on a different port, over networks that
can't reach each other.

### Added

- **Live calendar tool** (`breakerspace_calendar_tool.py`, an Open WebUI Tool): the
  model answers date questions ("what trainings are on this week?") from the live
  LibCal feed instead of from retrieval, which can't know what's current. Stdlib only,
  cached for 15 minutes, settings exposed as Valves.
- **Community models in `manage_models.py`**: `user/model` uploads can be browsed and
  pulled, marked as unverified, with a warning before pulling quantizations (1-bit /
  ternary) that stock Ollama can't run.
- **Migration across users and ports** (`migrate_rag.py`): `URL_MAP` rewrites
  `BASE_URL`/`OLLAMA_URL` alongside paths; `NOTES_DIR` is rewritten too; the most
  specific `PATH_MAP` rule wins; the export packages the config it actually used, so
  the importer runs with the same settings.
- **Sync mass-prune guard**: a sync that would remove half or more of a collection's
  tracked documents refuses unless given `--confirm-mass-prune`. An empty or
  misplaced `WATCH_DIR` with `PRUNE = true` used to mean an emptied collection.
- **Healthcheck on any host**: tests the chat model that's actually loaded rather
  than a hardcoded name; judges speed in tokens/s from Ollama's own counters and
  shows `ollama ps`; detects vision models by capability, not name; treats a host
  without AnythingLLM as skipped rather than failed.
- `BREAKERSPACE_TO_CARBONIO.md`: a worked, staged example of a migration to a
  smaller discrete-GPU machine.

### Changed

- Exports are atomic and interruption-safe: a named worker writes to `.partial` and
  renames on completion; an interrupted run is detected (with the command to clear
  it) and reported as `EXPORT INCOMPLETE` with a non-zero exit, not a quiet "DONE".
- Config values can continue over several lines after a trailing comma.
- `--pull` reports "nothing is answering at <url>" when the instance isn't running,
  instead of a list of connection errors.
- Shell scripts now carry the project version in their header, like everything else.

## [2026.9.18.1] — since [v2026.08.03.1]

Six weeks in which the project stopped being "scripts for one DGX Spark" and became
a stack that detects its own hardware, adopts libraries it didn't create, and can
tell you whether a configuration change actually helped.

### Added

**Four new tools**

| Tool | What it's for |
|---|---|
| `common/platform_probe.py` | Detects CPU, RAM, GPU/VRAM, and Docker GPU support, and derives how much model this machine can hold. Writes `hardware.conf`, which every other script reads, so all of them agree on one number — and hand edits win over detection. |
| `management/pin_notes.py` | Mirrors a folder of short notes into a model preset's **system prompt**, for guidance that must apply in every answer whether or not retrieval finds it. Replaces only its own marked block, so hand-written prompt text survives. |
| `management/determinism_check.py` | Asks the same question N times through the API and reports how much the answer moves — text, cited sources, and the numbers in it. `--compare` runs the set twice, as configured and with sampling pinned, so "did that setting help?" becomes a measurement. |
| `management/capture_notes.py` | Turns things you learn in a chat into lab notes the RAG can retrieve: write `Remember: …` in a conversation and harvest it later, or capture from the terminal. Only human-written text is ever captured. |

**Hardware-aware behaviour**

- Model menus, `--tags` and `--suggest` now hide models this machine cannot hold,
  using the probe's budget (VRAM for a discrete GPU, ~85 % of RAM for unified
  memory, ~60 % for CPU-only) rather than a number hardcoded for one machine.
- `setup_local_rag.sh` picks its default chat model by what fits, and says so when
  nothing does.
- `llm_stack_healthcheck.sh` reports the detected hardware, so its numbers and the
  installer's always match.

**Live model catalogue (`manage_models.py`)**

- `--browse [TERM]` and `--tags NAME` read the public Ollama library at run time
  instead of a built-in list that goes stale.
- Tags are annotated with download size, context window, vision capability, whether
  they fit this machine, and **which tags are the same build under another name**
  (shared digest).
- Tags that cannot run locally are identified rather than merely failing: Apple MLX
  builds — including ones named after a numeric format, like `-nvfp4` and `-mxfp8` —
  and cloud-only models, which run on someone else's servers.
- A family name that isn't a library name (`deepseek`) now returns the models it
  could have meant instead of an empty list.
- A failed pull explains itself: platform-gated tag, unknown tag, no disk space, or
  a network problem, each with the next command to run.

**Whole-stack migration**

- `migration/migrate_rag.py` plus `MIGRATION_RUNBOOK.md` move an installation to
  another machine: docker volumes (collections **and** their vectors), document
  folders, and the sync side-files — rewriting the absolute paths so the library
  isn't re-indexed or duplicated on arrival.

**Sync: new modes** (`sync/sync_folder.py`)

- `--discover` — rebuild a lost configuration by asking the server what collections
  exist and matching them against the state files on disk; prints a config to paste.
- `--pull` — adopt a collection that was filled through the web UI: downloads its
  files and records them as already synced, so the folder becomes the source of
  truth without re-uploading anything.
- `--wipe` — empty **one** collection (detach *and* delete its file objects) and
  clear its state, for a clean re-index that leaves other collections untouched.
- `--recaption` — redo existing figure/image descriptions with a new vision model,
  touching only the caption documents: nothing is re-embedded, and the run is
  resumable because each caption records the model that wrote it.
- `--status` now also reports the caption inventory by model, so you can see how much
  of a library still carries the old captioner's work.

**Sync: captioning with a modern vision model**

- Any vision-capable chat model can be the captioner (`FIGURE_MODEL`), which reads
  axis labels and multi-panel figures far better than a dedicated small captioner —
  and, being the model already loaded for chat, stops figure runs from evicting it.
- New controls for that case: `FIGURE_THINK` (off by default — thinking is pure
  latency for a caption), `FIGURE_TEMPERATURE`, `FIGURE_KEEP_ALIVE`, and
  `FIGURE_NUM_CTX` (match the chat preset or the same model loads twice).
- A capability preflight reports what the figure model can do and refuses to start
  when it cannot see images, rather than failing on every page hundreds of files in.

**Stack maintenance**

- `update_local_rag.sh` now finds **every** Open WebUI instance, including second
  libraries with names it can't guess, and recreates any container it has no recipe
  for by reconstructing its `docker run` from the live configuration. It reports the
  version each instance actually serves, and detects how Ollama was installed
  (snap / installer binary / distribution package) to print the right update command.
- `llm_stack_healthcheck.sh` checks all instances — state, port, served version,
  Ollama reachability, restart policy — and warns when two instances are running
  different versions.

**Packaging**

- `pyproject.toml` builds a wheel with **ten console commands, each named exactly
  after its script** (`sync_folder`, `manage_models`, `platform_probe`, …), plus the
  four shell scripts installed under their own names. A commented block offers
  `rag-`-prefixed aliases for a shared `/usr/local/bin`.

### Changed

- **The project is no longer Spark-specific.** Documentation and defaults target any
  Linux + NVIDIA workstation; the Spark is now the reference machine rather than the
  requirement.
- **Configuration lives in files, not in scripts.** Each library gets its own
  `.conf`, so upgrading a script never means re-editing it; a missing `--config` file
  is now a hard error instead of a silent fall back to defaults.
- Sampling parameters, context size and captioner settings are documented as what
  they are — per-request for `temperature`/`think`, load-time for `num_ctx` — with
  the measured behaviour of this Open WebUI build rather than assumptions.
- Shell scripts are committed executable, so an installed wheel ships them runnable.
- `.gitignore` now excludes `*.conf`, `hardware.conf`, `.rag_sync_*` and capture
  state: those carry collection ids, absolute paths and the location of an API key.

### Documentation

- **Retrieval, explained from what actually happened here**: why an attached
  collection may never be consulted under Native tool calling, what `Legacy` changes,
  which models cope with agentic retrieval — and the finding that mattered most, that
  notes are only found when they contain the words people search with.
- **Generation parameters**: what context window and temperature are *really* set to
  (Ollama's tiered defaults, not the model's maximum), how to check with `ollama ps`,
  and where each value must be set to take effect.
- **Adopting a collection filled through the UI**, the one arrangement the sync tool
  could not previously maintain.
- **Capturing knowledge from conversations**, including why model-written notes are
  deliberately excluded: model output that re-enters the corpus comes back cited as a
  source, and nothing afterwards can distinguish it from a fact.
- Runbooks updated to prefer `--wipe` over the blunt `DELETE /files/all`, which
  destroys every collection on an instance rather than the one you meant.

[2026.9.30.1]: https://github.com/feranick/Local_RAG_LLM/compare/v2026.9.18.1...v2026.9.30.1
[2026.9.18.1]: https://github.com/feranick/Local_RAG_LLM/compare/v2026.08.03.1...v2026.9.18.1
[v2026.08.03.1]: https://github.com/feranick/Local_RAG_LLM/releases/tag/v2026.08.03.1
