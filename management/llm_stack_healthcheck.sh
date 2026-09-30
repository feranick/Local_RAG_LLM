#!/usr/bin/env bash
#
# llm_stack_healthcheck.sh
# Version: 2026.9.30.1
# Tests the local LLM stack (Ollama, Open WebUI, AnythingLLM) on any Linux host.
# Reports the detected hardware so the numbers match what setup_local_rag.sh used.
#
# Usage:
#   chmod +x llm_stack_healthcheck.sh
#   ./llm_stack_healthcheck.sh
#
# If your user needs sudo for docker, run:  sudo ./llm_stack_healthcheck.sh

set -u

# ---- config (edit if your ports/model differ) ----
OLLAMA_URL="http://localhost:11434"
OPENWEBUI_URL="http://localhost:3000"
ANYTHINGLLM_URL="http://localhost:3001"
# Model for the generation test. Unset (the default): the chat model already loaded
# in Ollama, else the first installed chat model — whatever this host actually uses.
# Set RAG_TEST_MODEL=<name> to test a specific one.
TEST_MODEL="${RAG_TEST_MODEL:-}"
OPENWEBUI_CONTAINER="open-webui"
ANYTHINGLLM_CONTAINER="anythingllm"
# optional add-ons (only reported if present)
TIKA_CONTAINER="tika"
VISION_CONTAINER="ollama-vision"
# ---------------------------------------------------

GREEN=$'\e[32m'; RED=$'\e[31m'; YELLOW=$'\e[33m'; BOLD=$'\e[1m'; RESET=$'\e[0m'
PASS=0; FAIL=0

# use sudo for docker automatically if the plain call is denied
DOCKER="docker"
if ! docker ps >/dev/null 2>&1; then
  if sudo -n docker ps >/dev/null 2>&1 || sudo docker ps >/dev/null 2>&1; then
    DOCKER="sudo docker"
  fi
fi

ok()   { echo "  ${GREEN}✔${RESET} $1"; PASS=$((PASS+1)); }
bad()  { echo "  ${RED}x${RESET} $1"; FAIL=$((FAIL+1)); }
warn() { echo "  ${YELLOW}!${RESET} $1"; }
# NOTE: do NOT name this `head`. A function by that name shadows /usr/bin/head for
# the whole script, so every `| head -1` silently calls the heading printer with the
# argument "-1" and the pipeline yields a heading instead of the first line. That bug
# made model lookups return "-1" and the generation test fail on a model that exists.
section() { echo; echo "${BOLD}$1${RESET}"; }

# -----------------------------------------------------------------
section "1. Ollama service"

if systemctl is-active --quiet ollama 2>/dev/null; then
  ok "systemd service 'ollama' is active"
else
  warn "systemd service not active (may be fine if you run Ollama another way)"
fi

if curl -fsS --max-time 5 "$OLLAMA_URL/api/version" >/dev/null 2>&1; then
  VER=$(curl -fsS --max-time 5 "$OLLAMA_URL/api/version" 2>/dev/null)
  ok "Ollama API reachable at $OLLAMA_URL  ($VER)"
else
  bad "Ollama API NOT reachable at $OLLAMA_URL"
fi

MODELS=$(curl -fsS --max-time 5 "$OLLAMA_URL/api/tags" 2>/dev/null)
MODEL_NAMES=$(echo "$MODELS" | grep -o '"name":"[^"]*"' | sed 's/"name":"//; s/"$//')
if [ -n "$MODEL_NAMES" ]; then
  COUNT=$(echo "$MODEL_NAMES" | grep -c . )
  ok "Ollama reports $COUNT model(s) installed"
  echo "$MODEL_NAMES" | sed 's/^/      - /'
else
  bad "Could not list Ollama models"
fi

# name patterns used to classify models (so the check works for any selection)
EMBED_PAT='embed|bge|nomic|arctic|minilm'
VISION_PAT='llava|moondream|bakllava|vision|vl|-vl'

# an embedding model of SOME kind is required for RAG (not a specific one)
if echo "$MODEL_NAMES" | grep -qiE "$EMBED_PAT"; then
  EMB=$(echo "$MODEL_NAMES" | grep -iE "$EMBED_PAT" | head -1)
  ok "embedding model present ($EMB) — required for RAG"
else
  bad "no embedding model found — RAG uploads will fail (e.g. ollama pull nomic-embed-text)"
fi

# -----------------------------------------------------------------
section "2. Hardware / GPU"

# Prefer the shared probe so this report and the setup script agree on the
# numbers; fall back to querying nvidia-smi directly if it isn't around.
PROBE=""
for p in "$(dirname "$0")/platform_probe.py" "$(dirname "$0")/../common/platform_probe.py"; do
  [ -f "$p" ] && { PROBE="$p"; break; }
done
if [ -n "$PROBE" ] && command -v python3 >/dev/null 2>&1; then
  eval "$(python3 "$PROBE" --shell 2>/dev/null || true)"
  echo "      arch: ${RAG_ARCH:-?}   ram: ${RAG_RAM_GB:-?} GB   memory model: ${RAG_MEMORY_KIND:-?}"
  if [ "${RAG_GPU_COUNT:-0}" != "0" ]; then
    # On unified-memory parts (GB10 and friends) there is no separate VRAM pool, so
    # nvidia-smi reports 0 — printing "0.0 GB VRAM" reads like a fault when it isn't.
    if [ "${RAG_MEMORY_KIND:-}" = "unified" ]; then
      ok "GPU visible: ${RAG_GPU_NAMES} (unified memory, no separate VRAM pool; driver ${RAG_DRIVER:-?})"
    else
      ok "GPU visible: ${RAG_GPU_NAMES} (${RAG_VRAM_GB} GB VRAM, driver ${RAG_DRIVER:-?})"
    fi
  else
    warn "no NVIDIA GPU detected — inference will run on CPU (slow but functional)"
  fi
  ok "usable for models: ${RAG_USABLE_MEM_GB} GB"
  [ "${RAG_GPU_COUNT:-0}" != "0" ] && [ "${RAG_DOCKER_GPU:-no}" = "no" ] && \
    warn "docker has no GPU runtime — containers run on CPU (install nvidia-container-toolkit)"
elif command -v nvidia-smi >/dev/null 2>&1; then
  GPU=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 | tr -d '\r')
  if [ -n "$GPU" ] && [ "$GPU" != "-1" ]; then
    ok "GPU visible: $GPU"
    MEM=$(nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader 2>/dev/null | head -1 | tr -d '\r')
    [ -n "$MEM" ] && echo "      memory (used/total): $MEM"
  else
    warn "nvidia-smi present but returned no usable name (a unified-memory quirk on"
    warn "some parts, e.g. GB10 — usually harmless)"
  fi
  warn "platform_probe.py not found — run it for the full picture"
else
  warn "no nvidia-smi and no platform_probe.py: assuming CPU-only inference"
fi

# -----------------------------------------------------------------
# Pick which model to test. No hardcoded preference: a fixed name (it used to be
# gemma4:26b) is wrong on every host that doesn't run that model, and the check then
# warns about a model nobody meant to install. Order:
#   1. RAG_TEST_MODEL, if set (exact name, then installed variant of it)
#   2. the chat model Ollama currently has loaded — what this host is really using
#   3. the first installed chat model
GEN_MODEL=""; MATCH_KIND="auto"
LOADED=$(curl -fsS --max-time 5 "$OLLAMA_URL/api/ps" 2>/dev/null \
         | grep -o '"name":"[^"]*"' | sed 's/"name":"//; s/"$//' \
         | grep -viE "$EMBED_PAT" | head -1)
if [ -n "$TEST_MODEL" ]; then
  # Installed tags usually carry a variant/quant suffix (gemma4:26b-a4b-it-qat), so an
  # exact match on a plain name fails when the model IS installed. Try exact, then prefix.
  TEST_ESC=$(printf '%s' "$TEST_MODEL" | sed 's/[.[\*^$+?(){}|]/\\&/g')
  if echo "$MODEL_NAMES" | grep -qx "$TEST_MODEL"; then
    GEN_MODEL="$TEST_MODEL"; MATCH_KIND="exact"
  else
    GEN_MODEL=$(echo "$MODEL_NAMES" | grep -E "^${TEST_ESC}([-:]|$)" | head -1)
    [ -n "$GEN_MODEL" ] && MATCH_KIND="variant"
  fi
  [ -z "$GEN_MODEL" ] && MATCH_KIND="missing"
fi
if [ -z "$GEN_MODEL" ] && [ -n "$LOADED" ]; then
  GEN_MODEL="$LOADED"; [ "$MATCH_KIND" = "missing" ] || MATCH_KIND="loaded"
fi
if [ -z "$GEN_MODEL" ]; then
  # any model that isn't an embedder (vision-capable chat models are chat models too)
  GEN_MODEL=$(echo "$MODEL_NAMES" | grep -viE "$EMBED_PAT" | head -1)
fi

section "3. Generation test (${GEN_MODEL:-none available})"

if [ -n "$GEN_MODEL" ]; then
  case "$MATCH_KIND" in
    variant) echo "      RAG_TEST_MODEL '$TEST_MODEL' resolved to installed variant '$GEN_MODEL'" ;;
    missing) warn "RAG_TEST_MODEL '$TEST_MODEL' is not installed — testing '$GEN_MODEL' instead" ;;
    loaded)  echo "      (the chat model currently loaded in Ollama)" ;;
  esac
  REQ="{\"model\":\"$GEN_MODEL\",\"prompt\":\"Reply with exactly one word: OK\",\"stream\":false}"
  START=$(date +%s)
  RESP=$(curl -fsS --max-time 180 "$OLLAMA_URL/api/generate" -d "$REQ" 2>/dev/null)
  END=$(date +%s)
  if echo "$RESP" | grep -q '"response"'; then
    TEXT=$(echo "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("response","").strip())' 2>/dev/null)
    [ -z "$TEXT" ] && TEXT="(empty)"
    COLD=$((END-START))
    ok "$GEN_MODEL generated a response in ${COLD}s"
    echo "      model said: ${TEXT}"
    # The first request usually includes loading the weights, so timing it says
    # nothing about inference speed. Time a SECOND request, with the model resident —
    # and judge it by Ollama's own counters, not wall-clock seconds: a model that
    # thinks before answering spends many tokens on a one-word reply at full speed,
    # which a plain stopwatch mistakes for a slow GPU.
    if [ "$COLD" -gt 10 ]; then
      START2=$(date +%s)
      RESP2=$(curl -fsS --max-time 180 "$OLLAMA_URL/api/generate" -d "$REQ" 2>/dev/null)
      END2=$(date +%s); WARM=$((END2-START2))
      read -r NTOK TPS LOADS < <(echo "$RESP2" | python3 -c '
import sys, json
try:
    r = json.load(sys.stdin)
    n = r.get("eval_count", 0); d = r.get("eval_duration", 0) / 1e9
    ld = r.get("load_duration", 0) / 1e9
    print(n, f"{n/d:.1f}" if d else "0", f"{ld:.1f}")
except Exception:
    print(0, 0, 0)' 2>/dev/null)
      echo "      warm request: ${WARM}s, ${NTOK:-?} tokens at ${TPS:-?} tok/s (model load ${LOADS:-?}s)"
      if python3 -c "import sys; sys.exit(0 if float('${TPS:-0}') >= 15 else 1)" 2>/dev/null; then
        ok "generation speed ${TPS} tok/s"
        [ "${NTOK:-0}" -gt 20 ] && \
          echo "      (${NTOK} tokens for a one-word answer: the model is thinking first — normal for a chat request)"
        python3 -c "import sys; sys.exit(0 if float('${LOADS:-0}') > 2 else 1)" 2>/dev/null && \
          warn "the model was RELOADED for the second request — something else requested it with different settings (num_ctx?)"
      else
        warn "only ${TPS:-?} tok/s with the model loaded — part of it is probably in system RAM:"
      fi
      if command -v ollama >/dev/null 2>&1; then
        ollama ps 2>/dev/null | sed 's/^/        /'
        echo "      PROCESSOR should read 100% GPU; CONTEXT is what each request reserves"
      fi
    fi
  else
    bad "$GEN_MODEL failed to generate — it may not load on this build (check: journalctl -u ollama)"
  fi
else
  warn "no chat model installed to test — pull one (see: manage_models --recommend)"
fi

# -----------------------------------------------------------------
# EVERY Open WebUI instance, not just the first. A second library runs in its own
# container on its own port (new_rag_instance.py), and checking one hardcoded name
# means a dead — or silently un-updated — second instance passes the healthcheck.
# Discovery matches the container NAME as well as the image, because an image whose
# tag has moved shows up as a bare id.
owui_containers() {
  $DOCKER ps -a --format '{{.Names}} {{.Image}}' 2>/dev/null \
    | awk '$1 ~ /open-webui|openwebui/ || $2 ~ /open-webui/ {print $1}' | sort -u
}
first_host_port() {
  $DOCKER inspect -f '{{range $p, $conf := .NetworkSettings.Ports}}{{range $conf}}{{.HostPort}} {{end}}{{end}}' \
    "$1" 2>/dev/null | awk '{print $1}'
}

INSTANCES="$(owui_containers)"
[ -z "$INSTANCES" ] && INSTANCES="$OPENWEBUI_CONTAINER"
N_INST=$(echo "$INSTANCES" | grep -c .)

section "4. Open WebUI  (${N_INST} instance(s))"

for C in $INSTANCES; do
  STATE=$($DOCKER inspect -f '{{.State.Status}}' "$C" 2>/dev/null)
  PORT=$(first_host_port "$C")
  URL="http://localhost:${PORT:-8080}"
  echo "    --- $C  ${PORT:+(port $PORT)}"
  if [ "$STATE" = "running" ]; then
    ok "container '$C' is running"
  else
    bad "container '$C' state: ${STATE:-not found}"
    continue
  fi
  if curl -fsS --max-time 5 "$URL" >/dev/null 2>&1; then
    VER=$(curl -fsS --max-time 5 "$URL/api/config" 2>/dev/null \
          | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("version") or "")
except Exception: pass' 2>/dev/null)
    ok "responding at $URL${VER:+  (version $VER)}"
  else
    bad "NOT responding at $URL"
  fi
  if $DOCKER exec "$C" curl -fsS --max-time 5 \
        http://host.docker.internal:11434/api/tags >/dev/null 2>&1; then
    ok "'$C' CAN reach Ollama (host.docker.internal)"
  else
    bad "'$C' CANNOT reach Ollama — check OLLAMA_HOST=0.0.0.0:11434"
  fi
done

# Different versions across instances usually means one was missed by an update —
# they share an Ollama and are expected to move together.
VERSIONS=$(for C in $INSTANCES; do
             P=$(first_host_port "$C"); [ -n "$P" ] || continue
             curl -fsS --max-time 5 "http://localhost:$P/api/config" 2>/dev/null \
               | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("version") or "")
except Exception: pass' 2>/dev/null
           done | sort -u | grep -c .)
[ "${VERSIONS:-0}" -gt 1 ] && \
  warn "instances are running DIFFERENT versions — run: ./update_local_rag.sh"

# -----------------------------------------------------------------
section "5. AnythingLLM  ($ANYTHINGLLM_URL)"

# AnythingLLM is optional (setup_local_rag.sh --skip-anythingllm). A host that never
# installed it isn't failing — only a container that exists and isn't healthy is.
STATE=$($DOCKER inspect -f '{{.State.Status}}' "$ANYTHINGLLM_CONTAINER" 2>/dev/null)
HAVE_ALLM=0
if [ -z "$STATE" ]; then
  echo "      not installed on this host — skipped"
else
  HAVE_ALLM=1
  [ "$STATE" = "running" ] && ok "container '$ANYTHINGLLM_CONTAINER' is running" \
                           || bad "container '$ANYTHINGLLM_CONTAINER' state: $STATE"
  if curl -fsS --max-time 5 "$ANYTHINGLLM_URL" >/dev/null 2>&1; then
    ok "AnythingLLM responding at $ANYTHINGLLM_URL"
  else
    bad "AnythingLLM NOT responding at $ANYTHINGLLM_URL"
  fi
fi
if [ "$HAVE_ALLM" = 1 ] && [ -n "$($DOCKER ps -q -f name=^/${ANYTHINGLLM_CONTAINER}$ 2>/dev/null)" ]; then
  if $DOCKER exec "$ANYTHINGLLM_CONTAINER" curl -fsS --max-time 5 \
        http://host.docker.internal:11434/api/tags >/dev/null 2>&1; then
    ok "AnythingLLM container CAN reach Ollama (host.docker.internal)"
  else
    warn "AnythingLLM container cannot reach Ollama via host.docker.internal"
    warn "  (fine if you use a different LLM provider; needed for local Ollama)"
  fi
fi

# -----------------------------------------------------------------
section "6. Restart policies (survives reboot?)"
# every discovered instance, not just the first — a second library that doesn't come
# back after a reboot is exactly the failure this section exists to catch
RESTART_SET="$INSTANCES"
[ "$HAVE_ALLM" = 1 ] && RESTART_SET="$RESTART_SET $ANYTHINGLLM_CONTAINER"
for c in $RESTART_SET; do
  POL=$($DOCKER inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$c" 2>/dev/null)
  case "$POL" in
    always|unless-stopped) ok "$c restart policy: $POL" ;;
    "" ) bad "$c not found" ;;
    * ) warn "$c restart policy: $POL  (won't auto-start on reboot)" ;;
  esac
done

# -----------------------------------------------------------------
section "7. Optional add-ons (informational)"
# these are only present if you enabled Tika extraction or a containerized
# vision model; report status without affecting pass/fail.
found_addon=0
for c in "$TIKA_CONTAINER" "$VISION_CONTAINER"; do
  ST=$($DOCKER inspect -f '{{.State.Status}}' "$c" 2>/dev/null)
  if [ -n "$ST" ]; then
    found_addon=1
    [ "$ST" = "running" ] && ok "add-on '$c' is running" \
                          || warn "add-on '$c' present but $ST"
  fi
done
# a vision model (for --describe-figures) present on the host Ollama? Ask Ollama for
# each model's capabilities rather than guessing from the name: gemma4 and qwen3.8 see
# images without "vision" or "vl" anywhere in their names.
VMODS=""
for m in $(echo "$MODEL_NAMES" | grep -viE "$EMBED_PAT"); do
  if curl -fsS --max-time 10 "$OLLAMA_URL/api/show" -d "{\"model\":\"$m\"}" 2>/dev/null \
       | python3 -c 'import sys,json; sys.exit(0 if "vision" in (json.load(sys.stdin).get("capabilities") or []) else 1)' 2>/dev/null \
     || echo "$m" | grep -qiE "$VISION_PAT"; then
    VMODS="${VMODS:+$VMODS, }$m"
  fi
done
if [ -n "$VMODS" ]; then
  ok "vision-capable model(s): $VMODS — usable as FIGURE_MODEL"
  found_addon=1
fi
[ "$found_addon" -eq 0 ] && warn "no optional add-ons detected (Tika / vision) — fine if unused"

# -----------------------------------------------------------------
echo
echo "${BOLD}================ SUMMARY ================${RESET}"
echo "  ${GREEN}passed: $PASS${RESET}    ${RED}failed: $FAIL${RESET}"
if [ "$FAIL" -eq 0 ]; then
  echo "  ${GREEN}${BOLD}All good — the stack is healthy.${RESET}"
  exit 0
else
  echo "  ${RED}${BOLD}Some checks failed — see the x lines above.${RESET}"
  exit 1
fi
