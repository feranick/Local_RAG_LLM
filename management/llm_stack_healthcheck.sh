#!/usr/bin/env bash
#
# llm_stack_healthcheck.sh
# Tests the local LLM stack on the DGX Spark: Ollama, Open WebUI, AnythingLLM.
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
TEST_MODEL="${RAG_TEST_MODEL:-gemma4:26b}"   # preferred model for the generation test
                                             # (auto-falls back to any installed chat model)
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
head() { echo; echo "${BOLD}$1${RESET}"; }

# -----------------------------------------------------------------
head "1. Ollama service"

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
head "2. GPU"

if command -v nvidia-smi >/dev/null 2>&1; then
  GPU=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 | tr -d '\r')
  if [ -n "$GPU" ] && [ "$GPU" != "-1" ]; then
    ok "GPU visible: $GPU"
    # memory line is informational; GB10 unified memory may report [N/A]
    MEM=$(nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader 2>/dev/null | head -1 | tr -d '\r')
    [ -n "$MEM" ] && echo "      memory (used/total): $MEM"
  else
    warn "nvidia-smi present but returned no usable name (unified-memory quirk on GB10 — usually harmless)"
  fi
else
  warn "nvidia-smi not found on PATH"
fi

# -----------------------------------------------------------------
# Pick which model to test: the preferred one if installed, else auto-fall back
# to any installed chat model (excluding embedding/vision models).
GEN_MODEL=""
if echo "$MODEL_NAMES" | grep -qx "$TEST_MODEL"; then
  GEN_MODEL="$TEST_MODEL"
else
  GEN_MODEL=$(echo "$MODEL_NAMES" | grep -viE "$EMBED_PAT|$VISION_PAT" | head -1)
fi

head "3. Generation test (${GEN_MODEL:-none available})"

if [ -n "$GEN_MODEL" ]; then
  [ "$GEN_MODEL" != "$TEST_MODEL" ] && warn "preferred '$TEST_MODEL' not installed — testing '$GEN_MODEL' instead"
  REQ="{\"model\":\"$GEN_MODEL\",\"prompt\":\"Reply with exactly one word: OK\",\"stream\":false}"
  START=$(date +%s)
  RESP=$(curl -fsS --max-time 180 "$OLLAMA_URL/api/generate" -d "$REQ" 2>/dev/null)
  END=$(date +%s)
  if echo "$RESP" | grep -q '"response"'; then
    TEXT=$(echo "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("response","").strip())' 2>/dev/null)
    [ -z "$TEXT" ] && TEXT="(empty)"
    ok "$GEN_MODEL generated a response in $((END-START))s"
    echo "      model said: ${TEXT}"
    if [ "$((END-START))" -gt 20 ]; then
      warn "that was slow — check 'ollama ps' shows 100% GPU (not a CPU split / cold load)"
    fi
  else
    bad "$GEN_MODEL failed to generate — it may not load on this build (check: journalctl -u ollama)"
  fi
else
  warn "no chat model installed to test — pull one (e.g. ollama pull gemma4:26b)"
fi

# -----------------------------------------------------------------
head "4. Open WebUI  ($OPENWEBUI_URL)"

STATE=$($DOCKER inspect -f '{{.State.Status}}' "$OPENWEBUI_CONTAINER" 2>/dev/null)
[ "$STATE" = "running" ] && ok "container '$OPENWEBUI_CONTAINER' is running" \
                         || bad "container '$OPENWEBUI_CONTAINER' state: ${STATE:-not found}"
if curl -fsS --max-time 5 "$OPENWEBUI_URL" >/dev/null 2>&1; then
  ok "Open WebUI responding at $OPENWEBUI_URL"
else
  bad "Open WebUI NOT responding at $OPENWEBUI_URL"
fi
# can the Open WebUI container reach Ollama?
if [ -n "$($DOCKER ps -q -f name=^/${OPENWEBUI_CONTAINER}$ 2>/dev/null)" ]; then
  if $DOCKER exec "$OPENWEBUI_CONTAINER" curl -fsS --max-time 5 \
        http://host.docker.internal:11434/api/tags >/dev/null 2>&1; then
    ok "Open WebUI container CAN reach Ollama (host.docker.internal)"
  else
    bad "Open WebUI container CANNOT reach Ollama — check OLLAMA_HOST=0.0.0.0:11434"
  fi
fi

# -----------------------------------------------------------------
head "5. AnythingLLM  ($ANYTHINGLLM_URL)"

STATE=$($DOCKER inspect -f '{{.State.Status}}' "$ANYTHINGLLM_CONTAINER" 2>/dev/null)
[ "$STATE" = "running" ] && ok "container '$ANYTHINGLLM_CONTAINER' is running" \
                         || bad "container '$ANYTHINGLLM_CONTAINER' state: ${STATE:-not found}"

if curl -fsS --max-time 5 "$ANYTHINGLLM_URL" >/dev/null 2>&1; then
  ok "AnythingLLM responding at $ANYTHINGLLM_URL"
else
  bad "AnythingLLM NOT responding at $ANYTHINGLLM_URL"
fi
if [ -n "$($DOCKER ps -q -f name=^/${ANYTHINGLLM_CONTAINER}$ 2>/dev/null)" ]; then
  if $DOCKER exec "$ANYTHINGLLM_CONTAINER" curl -fsS --max-time 5 \
        http://host.docker.internal:11434/api/tags >/dev/null 2>&1; then
    ok "AnythingLLM container CAN reach Ollama (host.docker.internal)"
  else
    warn "AnythingLLM container cannot reach Ollama via host.docker.internal"
    warn "  (fine if you use a different LLM provider; needed for local Ollama)"
  fi
fi

# -----------------------------------------------------------------
head "6. Restart policies (survives reboot?)"
for c in "$OPENWEBUI_CONTAINER" "$ANYTHINGLLM_CONTAINER"; do
  POL=$($DOCKER inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$c" 2>/dev/null)
  case "$POL" in
    always|unless-stopped) ok "$c restart policy: $POL" ;;
    "" ) bad "$c not found" ;;
    * ) warn "$c restart policy: $POL  (won't auto-start on reboot)" ;;
  esac
done

# -----------------------------------------------------------------
head "7. Optional add-ons (informational)"
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
# a vision model (for --describe-figures) present on the host Ollama?
if echo "$MODEL_NAMES" | grep -qiE "$VISION_PAT"; then
  VMOD=$(echo "$MODEL_NAMES" | grep -iE "$VISION_PAT" | head -1)
  ok "a vision model is available ($VMOD) — for --describe-figures"
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
