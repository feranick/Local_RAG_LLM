#!/usr/bin/env bash
#
# setup_local_rag.sh
# --------------------------------------------------------------------------
# Automated, idempotent setup of a local RAG stack on the NVIDIA DGX Spark
# (GB10 Grace Blackwell, ARM64, Ubuntu 24.04 / DGX OS).
#
#   Ollama  (serves the chat model + an embedding model)
#      |
#      +--> Open WebUI    (general chat UI + Knowledge collections)   :3000
#      +--> AnythingLLM   (document-chat / RAG workspaces)            :3001
#
# Safe to re-run: existing containers are recreated, existing Ollama config
# and pulled models are left in place. Data volumes are preserved.
#
# Usage:
#   chmod +x setup_local_rag.sh
#   ./setup_local_rag.sh                 # full stack
#   ./setup_local_rag.sh --skip-openwebui
#   ./setup_local_rag.sh --skip-anythingllm
#   ./setup_local_rag.sh --chat-model llama3.3:70b
#
# If your user needs sudo for docker, the script auto-detects and uses it.
# --------------------------------------------------------------------------

set -euo pipefail

# ===================== CONFIG (edit as needed) ============================
CHAT_MODEL="gemma4:26b"           # main chat/generation model
EMBED_MODEL="nomic-embed-text"    # embedding model — REQUIRED for RAG
OLLAMA_PORT=11434
OPENWEBUI_PORT=3000
ANYTHINGLLM_PORT=3001
STORAGE_LOCATION="${HOME}/anythingllm"   # AnythingLLM persistent data
INSTALL_OPENWEBUI=1
INSTALL_ANYTHINGLLM=1
PULL_MODELS=1
# ==========================================================================

# ---- args ----
while [ $# -gt 0 ]; do
  case "$1" in
    --chat-model)      CHAT_MODEL="$2"; shift 2 ;;
    --embed-model)     EMBED_MODEL="$2"; shift 2 ;;
    --skip-openwebui)  INSTALL_OPENWEBUI=0; shift ;;
    --skip-anythingllm) INSTALL_ANYTHINGLLM=0; shift ;;
    --skip-models)     PULL_MODELS=0; shift ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

GREEN=$'\e[32m'; RED=$'\e[31m'; YELLOW=$'\e[33m'; BOLD=$'\e[1m'; RESET=$'\e[0m'
step() { echo; echo "${BOLD}==> $1${RESET}"; }
ok()   { echo "  ${GREEN}✔${RESET} $1"; }
warn() { echo "  ${YELLOW}!${RESET} $1"; }
die()  { echo "  ${RED}x $1${RESET}"; exit 1; }

# ---- docker: use sudo automatically if needed ----
DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
  if sudo docker info >/dev/null 2>&1; then
    DOCKER="sudo docker"
    warn "using 'sudo docker' (your user isn't in the docker group)"
  else
    die "Docker not available. Install Docker / start the daemon and retry."
  fi
fi

# --------------------------------------------------------------------------
step "1/6  Ollama — install if missing"
if command -v ollama >/dev/null 2>&1; then
  ok "Ollama already installed ($(ollama --version 2>/dev/null | head -1))"
else
  warn "Ollama not found — installing via official script"
  curl -fsSL https://ollama.com/install.sh | sh
  ok "Ollama installed"
fi

# --------------------------------------------------------------------------
step "2/6  Ollama — bind to 0.0.0.0 so containers can reach it"
OVERRIDE_DIR="/etc/systemd/system/ollama.service.d"
OVERRIDE_FILE="${OVERRIDE_DIR}/override.conf"
if [ -f "$OVERRIDE_FILE" ] && grep -q "OLLAMA_HOST=0.0.0.0" "$OVERRIDE_FILE"; then
  ok "OLLAMA_HOST already set to 0.0.0.0:${OLLAMA_PORT}"
else
  sudo mkdir -p "$OVERRIDE_DIR"
  sudo tee "$OVERRIDE_FILE" >/dev/null <<EOF
[Service]
Environment="OLLAMA_HOST=0.0.0.0:${OLLAMA_PORT}"
EOF
  sudo systemctl daemon-reload
  sudo systemctl restart ollama
  ok "Configured OLLAMA_HOST=0.0.0.0:${OLLAMA_PORT} and restarted service"
fi

# wait for the API to come up
step "3/6  Ollama — wait for API"
for i in $(seq 1 30); do
  if curl -fsS --max-time 3 "http://localhost:${OLLAMA_PORT}/api/version" >/dev/null 2>&1; then
    ok "Ollama API is up at http://localhost:${OLLAMA_PORT}"
    break
  fi
  sleep 1
  [ "$i" -eq 30 ] && die "Ollama API did not come up after 30s"
done

# --------------------------------------------------------------------------
step "4/6  Pull models"
if [ "$PULL_MODELS" -eq 1 ]; then
  for m in "$CHAT_MODEL" "$EMBED_MODEL"; do
    if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$m"; then
      ok "$m already present"
    else
      warn "pulling $m (this can take a while)…"
      ollama pull "$m"
      ok "$m pulled"
    fi
  done
else
  warn "skipping model pull (--skip-models)"
fi

# helper: (re)create a container cleanly
recreate() {
  local name="$1"; shift
  if [ -n "$($DOCKER ps -aq -f "name=^/${name}$")" ]; then
    warn "removing existing '${name}' container (data volumes are preserved)"
    $DOCKER rm -f "$name" >/dev/null
  fi
  $DOCKER run -d "$@" >/dev/null
}

# --------------------------------------------------------------------------
step "5/6  Open WebUI  (port ${OPENWEBUI_PORT})"
if [ "$INSTALL_OPENWEBUI" -eq 1 ]; then
  GPU_FLAG=""
  $DOCKER run --rm --gpus all nvidia/cuda:12.6.0-base-ubuntu24.04 true >/dev/null 2>&1 && GPU_FLAG="--gpus all"
  recreate open-webui \
    --name open-webui \
    --restart always \
    -p "${OPENWEBUI_PORT}:8080" \
    ${GPU_FLAG} \
    --add-host=host.docker.internal:host-gateway \
    -e OLLAMA_BASE_URL="http://host.docker.internal:${OLLAMA_PORT}" \
    -v open-webui:/app/backend/data \
    ghcr.io/open-webui/open-webui:main
  ok "Open WebUI started → http://localhost:${OPENWEBUI_PORT}"
else
  warn "skipping Open WebUI (--skip-openwebui)"
fi

# --------------------------------------------------------------------------
step "6/6  AnythingLLM  (port ${ANYTHINGLLM_PORT})"
if [ "$INSTALL_ANYTHINGLLM" -eq 1 ]; then
  mkdir -p "$STORAGE_LOCATION"
  touch "${STORAGE_LOCATION}/.env"
  # AnythingLLM runs as UID 1000 inside the container; it must own the storage
  sudo chown -R 1000:1000 "$STORAGE_LOCATION"
  recreate anythingllm \
    --name anythingllm \
    --restart unless-stopped \
    -p "${ANYTHINGLLM_PORT}:3001" \
    --cap-add SYS_ADMIN \
    --add-host=host.docker.internal:host-gateway \
    -v "${STORAGE_LOCATION}:/app/server/storage" \
    -v "${STORAGE_LOCATION}/.env:/app/server/.env" \
    -e STORAGE_DIR="/app/server/storage" \
    mintplexlabs/anythingllm:latest
  ok "AnythingLLM started → http://localhost:${ANYTHINGLLM_PORT}"
else
  warn "skipping AnythingLLM (--skip-anythingllm)"
fi

# --------------------------------------------------------------------------
cat <<EOF

${BOLD}================ DONE ================${RESET}
Stack is up. Next steps (one-time, in each web UI):

  Open WebUI   http://localhost:${OPENWEBUI_PORT}
    - create the first (admin) account
    - Admin Settings > Documents > Embedding: Ollama / ${EMBED_MODEL}
    - Workspace > Knowledge > + to build a document collection

  AnythingLLM  http://localhost:${ANYTHINGLLM_PORT}
    - onboarding: LLM = Ollama, URL http://host.docker.internal:${OLLAMA_PORT}, model ${CHAT_MODEL}
    - Embedding = Ollama / ${EMBED_MODEL}
    - Vector DB = LanceDB (default)
    - create a workspace, upload docs, Save & Embed

Verify everything with:  ./llm_stack_healthcheck.sh
Auto-sync a folder with:  see sync_folder.py + README.md
EOF
