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
#   ./setup_local_rag.sh --vision-model llava     # also pull a vision model
#   ./setup_local_rag.sh --no-prompt              # skip interactive model menus
#
# When run in a terminal it interactively prompts you to choose the chat,
# embedding, and (optional) vision models, with sensible defaults. Pass the
# --chat-model / --embed-model / --vision-model flags or --no-prompt to skip
# the menus (e.g. for automation).
#
# If your user needs sudo for docker, the script auto-detects and uses it.
# --------------------------------------------------------------------------

set -euo pipefail

# ===================== CONFIG (edit as needed) ============================
CHAT_MODEL="gemma4:26b"           # main chat/generation model
EMBED_MODEL="nomic-embed-text"    # embedding model — REQUIRED for RAG
VISION_MODEL=""                   # optional vision model for figure/image descriptions (pulled if set)
OLLAMA_PORT=11434
OPENWEBUI_PORT=3000
ANYTHINGLLM_PORT=3001
STORAGE_LOCATION=""              # AnythingLLM data dir (blank = <real-user-home>/anythingllm, auto-detected)
INSTALL_OPENWEBUI=1
INSTALL_ANYTHINGLLM=1
PULL_MODELS=1
INTERACTIVE=1                     # prompt to choose models in a terminal (disable with --no-prompt)
# ==========================================================================

# ---- args ----
CHAT_SET=0; EMBED_SET=0           # track whether a model was set explicitly on the CLI
while [ $# -gt 0 ]; do
  case "$1" in
    --chat-model)      CHAT_MODEL="$2"; CHAT_SET=1; shift 2 ;;
    --embed-model)     EMBED_MODEL="$2"; EMBED_SET=1; shift 2 ;;
    --vision-model)    VISION_MODEL="$2"; shift 2 ;;
    --no-prompt)       INTERACTIVE=0; shift ;;
    --skip-openwebui)  INSTALL_OPENWEBUI=0; shift ;;
    --skip-anythingllm) INSTALL_ANYTHINGLLM=0; shift ;;
    --skip-models)     PULL_MODELS=0; shift ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# resolve the real (non-root) user even when the script is run via sudo,
# so models and data land in the user's home, not root's.
RUN_USER="${SUDO_USER:-$USER}"
RUN_GROUP="$(id -gn "$RUN_USER" 2>/dev/null || echo "$RUN_USER")"
REAL_HOME="$(getent passwd "$RUN_USER" 2>/dev/null | cut -d: -f6)"
[ -z "$REAL_HOME" ] && REAL_HOME="$HOME"
: "${STORAGE_LOCATION:=$REAL_HOME/anythingllm}"

GREEN=$'\e[32m'; RED=$'\e[31m'; YELLOW=$'\e[33m'; BOLD=$'\e[1m'; RESET=$'\e[0m'
step() { echo; echo "${BOLD}==> $1${RESET}"; }
ok()   { echo "  ${GREEN}✔${RESET} $1"; }
warn() { echo "  ${YELLOW}!${RESET} $1"; }
die()  { echo "  ${RED}x $1${RESET}"; exit 1; }

# pick_model VARNAME "label" "default" opt1 opt2 ...  — interactive chooser
pick_model() {
  local __var="$1" label="$2" def="$3"
  shift 3
  local opts=("$@")
  local i=1 o ans custom result="$def"
  echo
  echo "${BOLD}Select ${label}${RESET}"
  for o in "${opts[@]}"; do echo "  $i) $o"; i=$((i+1)); done
  echo "  c) enter a custom tag"
  printf "Choice [Enter = keep default: %s]: " "$def"
  read -r ans || ans=""
  if [ -z "$ans" ]; then
    result="$def"
  elif [ "$ans" = c ] || [ "$ans" = C ]; then
    printf "  model tag: "; read -r custom || custom=""
    [ -n "$custom" ] && result="$custom"
  elif printf '%s' "$ans" | grep -qE '^[0-9]+$' && [ "$ans" -ge 1 ] 2>/dev/null && [ "$ans" -le "${#opts[@]}" ] 2>/dev/null; then
    result="${opts[$((ans-1))]}"
  else
    echo "  (unrecognized '$ans' — keeping default: $def)"
  fi
  printf -v "$__var" '%s' "$result"
}

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
# Interactive model selection (only in a terminal, unless --no-prompt / flags).
if [ "$INTERACTIVE" -eq 1 ] && [ -t 0 ]; then
  step "Model selection (press Enter to accept the default)"
  [ "$CHAT_SET" -eq 0 ] && pick_model CHAT_MODEL "chat model (LLM)" "$CHAT_MODEL" \
      gemma4:26b gemma4:31b gemma4:12b llama3.3:70b qwen3.6:27b qwen3.6:35b
  [ "$EMBED_SET" -eq 0 ] && pick_model EMBED_MODEL "embedding model (required for RAG)" "$EMBED_MODEL" \
      nomic-embed-text mxbai-embed-large bge-m3
  pick_model VISION_MODEL "vision model for figure/image descriptions (optional)" "${VISION_MODEL:-llava}" \
      llava moondream bakllava none
  [ "$VISION_MODEL" = "none" ] && VISION_MODEL=""
  ok "selected: chat=${CHAT_MODEL}  embed=${EMBED_MODEL}  vision=${VISION_MODEL:-<none>}"
elif [ "$INTERACTIVE" -eq 0 ]; then
  warn "non-interactive (--no-prompt): chat=${CHAT_MODEL} embed=${EMBED_MODEL} vision=${VISION_MODEL:-<none>}"
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
step "2/6  Ollama — configure service to listen on 0.0.0.0"
# Containers reach the host Ollama via host.docker.internal, which resolves to
# the docker bridge gateway — so Ollama MUST bind 0.0.0.0, not 127.0.0.1.
OLLAMA_BIN="$(command -v ollama || echo /usr/local/bin/ollama)"

if systemctl list-unit-files 2>/dev/null | grep -q '^ollama\.service'; then
  # A systemd unit exists — add a drop-in override for the bind address.
  OVERRIDE_DIR="/etc/systemd/system/ollama.service.d"
  if [ -f "${OVERRIDE_DIR}/override.conf" ] && grep -q "OLLAMA_HOST=0.0.0.0" "${OVERRIDE_DIR}/override.conf"; then
    ok "existing ollama.service already bound to 0.0.0.0:${OLLAMA_PORT}"
  else
    sudo mkdir -p "$OVERRIDE_DIR"
    sudo tee "${OVERRIDE_DIR}/override.conf" >/dev/null <<EOF
[Service]
Environment="OLLAMA_HOST=0.0.0.0:${OLLAMA_PORT}"
EOF
    ok "added 0.0.0.0 bind override to existing ollama.service"
  fi
else
  # No systemd unit (a manual 'ollama serve', or an install that didn't create
  # one). Create a proper service that runs as the real user so it can see that
  # user's already-pulled models in ~/.ollama, and binds to 0.0.0.0.
  warn "no ollama.service found — creating one (runs as '${RUN_USER}', binds 0.0.0.0)"
  sudo pkill -f "ollama serve" 2>/dev/null || true   # free the port from any manual instance
  sleep 1
  sudo tee /etc/systemd/system/ollama.service >/dev/null <<EOF
[Unit]
Description=Ollama Service
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=${OLLAMA_BIN} serve
User=${RUN_USER}
Group=${RUN_GROUP}
Restart=always
RestartSec=3
Environment="OLLAMA_HOST=0.0.0.0:${OLLAMA_PORT}"
Environment="PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

[Install]
WantedBy=multi-user.target
EOF
  ok "created /etc/systemd/system/ollama.service"
fi

sudo systemctl daemon-reload
sudo systemctl enable ollama >/dev/null 2>&1 || true
sudo systemctl restart ollama
ok "ollama.service enabled (starts on boot) and (re)started"

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
  MODELS_TO_PULL=("$CHAT_MODEL" "$EMBED_MODEL")
  [ -n "$VISION_MODEL" ] && MODELS_TO_PULL+=("$VISION_MODEL")
  for m in "${MODELS_TO_PULL[@]}"; do
    if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$m"; then
      ok "$m already present"
    else
      warn "pulling $m (this can take a while)…"
      ollama pull "$m"
      ok "$m pulled"
    fi
  done

  # verify each model actually LOADS — pulling is not the same as running.
  # this catches "pulled but unsupported architecture" (e.g. mllama on this build).
  step "4b/6  Verify models load"
  for m in "${MODELS_TO_PULL[@]}"; do
    if [ "$m" = "$EMBED_MODEL" ]; then
      if curl -fsS --max-time 120 "http://localhost:${OLLAMA_PORT}/api/embeddings" \
           -d "{\"model\":\"$m\",\"prompt\":\"test\"}" 2>/dev/null | grep -q '"embedding"'; then
        ok "$m loads (embeddings OK)"
      else
        warn "$m did NOT return an embedding — it may not load on this build (check: journalctl -u ollama)"
      fi
    else
      if curl -fsS --max-time 180 "http://localhost:${OLLAMA_PORT}/api/generate" \
           -d "{\"model\":\"$m\",\"prompt\":\"hi\",\"stream\":false}" 2>/dev/null | grep -q '"response"'; then
        ok "$m loads (generation OK)"
      else
        warn "$m did NOT load — likely unsupported on this Ollama build; check 'journalctl -u ollama' (e.g. 'unknown model architecture')"
      fi
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

Models: chat=${CHAT_MODEL}  embed=${EMBED_MODEL}  vision=${VISION_MODEL:-<none>}
$( [ -n "$VISION_MODEL" ] && echo "  (use the vision model for plots/images:  RAG_FIGURE_MODEL=${VISION_MODEL} python3 sync_folder.py --describe-figures)" )

Verify everything with:  ./llm_stack_healthcheck.sh
Auto-sync a folder with:  see sync_folder.py + README.md
EOF
