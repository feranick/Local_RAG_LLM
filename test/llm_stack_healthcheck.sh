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
TEST_MODEL="gemma4:26b"        # model used for the generation test
OPENWEBUI_CONTAINER="open-webui"
ANYTHINGLLM_CONTAINER="anythingllm"
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
if [ -n "$MODELS" ]; then
  COUNT=$(echo "$MODELS" | grep -o '"name"' | wc -l | tr -d ' ')
  ok "Ollama reports $COUNT model(s) installed"
  echo "$MODELS" | grep -o '"name":"[^"]*"' | sed 's/"name":"/      - /; s/"$//'
else
  bad "Could not list Ollama models"
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
head "3. Generation test ($TEST_MODEL)"

if echo "$MODELS" | grep -q "\"$TEST_MODEL\""; then
  REQ="{\"model\":\"$TEST_MODEL\",\"prompt\":\"Reply with exactly one word: OK\",\"stream\":false}"
  START=$(date +%s)
  RESP=$(curl -fsS --max-time 120 "$OLLAMA_URL/api/generate" -d "$REQ" 2>/dev/null)
  END=$(date +%s)
  if echo "$RESP" | grep -q '"response"'; then
    TEXT=$(echo "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("response","").strip())' 2>/dev/null)
    [ -z "$TEXT" ] && TEXT="(empty)"
    ok "Model generated a response in $((END-START))s"
    echo "      model said: ${TEXT}"
    if [ "$((END-START))" -gt 20 ]; then
      warn "that was slow — check 'ollama ps' shows 100% GPU (not a CPU split / cold load)"
    fi
  else
    bad "Generation call failed (model loaded but no response)"
  fi
else
  warn "Model '$TEST_MODEL' not installed — skipping generation test"
  warn "install with:  ollama pull $TEST_MODEL   (or edit TEST_MODEL in this script)"
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
