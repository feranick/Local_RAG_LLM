#!/usr/bin/env bash
#
# setup_local_rag.sh
# --------------------------------------------------------------------------
# Automated, idempotent setup of a local RAG stack on a Linux workstation or
# server (x86_64 or ARM64). Developed on an NVIDIA DGX Spark (GB10, DGX OS),
# but it detects the hardware it is actually running on and adapts: the model
# menus only offer models this machine can hold.
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

# ---- hardware detection -------------------------------------------------
# One source of truth for "what can this machine run": platform_probe.py, either
# a saved hardware.conf or a live probe. Everything below keys off
# RAG_USABLE_MEM_GB so the same script works on a 16 GB laptop and a 128 GB Spark.
# Capture an explicit override BEFORE initialising anything: these variables are
# pre-set for `set -u`, and blanking them would silently discard the caller's
# RAG_USABLE_MEM_GB (which the probe subprocess would then never see).
RAG_USABLE_OVERRIDE="${RAG_USABLE_MEM_GB:-}"
RAG_USABLE_MEM_GB=""; RAG_MEMORY_KIND=""; RAG_GPU_NAMES=""; RAG_GPU_COUNT="0"
RAG_RAM_GB=""; RAG_ARCH=""; RAG_DOCKER_GPU="no"; RAG_VRAM_GB=""
find_probe() {
  local here; here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  for p in "$here/platform_probe.py" "$here/../common/platform_probe.py" \
           "$here/../management/platform_probe.py"; do
    [ -f "$p" ] && { echo "$p"; return 0; }
  done
  command -v rag-probe >/dev/null 2>&1 && { echo "rag-probe"; return 0; }
  return 1
}
PROBE="$(find_probe || true)"
if [ -n "$PROBE" ] && command -v python3 >/dev/null 2>&1; then
  if [ "$PROBE" = "rag-probe" ]; then
    eval "$(rag-probe --shell 2>/dev/null || true)"
  else
    eval "$(python3 "$PROBE" --shell 2>/dev/null || true)"
  fi
fi
if [ -z "${RAG_USABLE_MEM_GB:-}" ]; then
  # Last resort: read RAM directly so the menus are still sensible.
  RAG_RAM_GB="$(awk '/MemTotal/ {printf "%.1f", $2/1048576}' /proc/meminfo 2>/dev/null || echo 0)"
  RAG_USABLE_MEM_GB="$(awk -v r="${RAG_RAM_GB:-0}" 'BEGIN{printf "%.1f", r*0.6}')"
  RAG_MEMORY_KIND="unknown (probe not found)"
fi
# An explicit override always wins over whatever was detected.
if [ -n "$RAG_USABLE_OVERRIDE" ]; then
  RAG_USABLE_MEM_GB="$RAG_USABLE_OVERRIDE"
  RAG_MEMORY_KIND="${RAG_MEMORY_KIND:-unknown} (RAG_USABLE_MEM_GB override)"
fi
# integer form for comparisons
USABLE_INT="$(awk -v v="${RAG_USABLE_MEM_GB:-0}" 'BEGIN{printf "%d", v}')"

# fits <approx_size_gb> — 1.2x for KV cache / context overhead
fits() { awk -v s="$1" -v u="${RAG_USABLE_MEM_GB:-0}" 'BEGIN{exit !(s*1.2 <= u)}'; }

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
# Report what we're running on, then offer only models that fit.
step "Hardware"
echo "  arch          : ${RAG_ARCH:-unknown}"
echo "  ram           : ${RAG_RAM_GB:-?} GB"
if [ "${RAG_GPU_COUNT:-0}" != "0" ]; then
  echo "  gpu           : ${RAG_GPU_NAMES} (${RAG_VRAM_GB:-?} GB VRAM)"
else
  warn "no NVIDIA GPU detected — generation will run on CPU and be slow"
fi
echo "  memory model  : ${RAG_MEMORY_KIND:-unknown}"
ok "usable for models: ${RAG_USABLE_MEM_GB} GB"
if [ "${RAG_GPU_COUNT:-0}" != "0" ] && [ "${RAG_DOCKER_GPU:-no}" = "no" ]; then
  warn "docker has no GPU runtime — install nvidia-container-toolkit for GPU in containers"
fi

# Candidate models with approximate download sizes (GB). Only those that fit the
# detected budget are offered; the rest are hidden rather than set up to fail.
# Sizes drift — 'manage_models.py --tags NAME' reads the live catalogue.
# NOTE: these helpers must always return 0. Under `set -e`, a function whose last
# command fails takes the whole script down — a model simply not fitting would
# otherwise abort setup with no message at all.
CHAT_CANDIDATES=""
add_chat() { if fits "$2"; then CHAT_CANDIDATES="$CHAT_CANDIDATES $1"; fi; return 0; }
add_chat gemma4:e2b-it-qat 4.3
add_chat gemma4:12b-it-qat 7.2
add_chat gemma4:12b   7.6
add_chat gemma4:26b   18
add_chat gemma4:31b   20
add_chat qwen3.6:27b  17
add_chat qwen3.6:35b  21
add_chat llama3.3:70b 43
# smallest first if nothing else fits, so a modest machine still gets a suggestion
[ -z "$CHAT_CANDIDATES" ] && CHAT_CANDIDATES="gemma4:e2b-it-qat gemma4:12b-it-qat"

VISION_CANDIDATES=""
add_vision() { if fits "$2"; then VISION_CANDIDATES="$VISION_CANDIDATES $1"; fi; return 0; }
add_vision llava     4.7
add_vision moondream 1.7
add_vision bakllava  4.7
VISION_CANDIDATES="$VISION_CANDIDATES none"

# Default chat model: the first PREFERRED model that fits — not the largest.
# Bigger is not better here; a 70B model fits a 128 GB machine but is far slower
# than a 26B MoE for the same work, so preference order wins over capacity.
if [ "$CHAT_SET" -eq 0 ]; then
  CHAT_PICK=""
  for cand in "gemma4:26b 18" "gemma4:12b 7.6" "gemma4:12b-it-qat 7.2" "gemma4:e2b-it-qat 4.3"; do
    # shellcheck disable=SC2086
    set -- $cand
    if fits "$2"; then CHAT_PICK="$1"; break; fi
  done
  if [ -n "$CHAT_PICK" ]; then
    CHAT_MODEL="$CHAT_PICK"
  else
    # Nothing fits comfortably. Don't silently keep a default that can't run —
    # take the smallest known model and say plainly what to expect.
    CHAT_MODEL="gemma4:e2b-it-qat"
    warn "no listed chat model fits ~${RAG_USABLE_MEM_GB} GB; defaulting to the"
    warn "smallest (${CHAT_MODEL}). It will spill into system RAM and be slow."
    warn "Consider a smaller quantisation: manage_models.py --tags gemma4"
  fi
fi
# Vision default: the first that fits, or none at all on a small machine.
DEF_VISION="none"
for m in $VISION_CANDIDATES; do [ "$m" != "none" ] && { DEF_VISION="$m"; break; }; done

# Interactive model selection (only in a terminal, unless --no-prompt / flags).
if [ "$INTERACTIVE" -eq 1 ] && [ -t 0 ]; then
  step "Model selection (press Enter to accept the default)"
  echo "  only models that fit ~${RAG_USABLE_MEM_GB} GB are listed;"
  echo "  'c' lets you enter any tag, and larger models still load by spilling to RAM (slowly)."
  # shellcheck disable=SC2086
  [ "$CHAT_SET" -eq 0 ] && pick_model CHAT_MODEL "chat model (LLM)" "$CHAT_MODEL" \
      $CHAT_CANDIDATES
  [ "$EMBED_SET" -eq 0 ] && pick_model EMBED_MODEL "embedding model (required for RAG)" "$EMBED_MODEL" \
      nomic-embed-text mxbai-embed-large bge-m3
  # shellcheck disable=SC2086
  pick_model VISION_MODEL "vision model for figure/image descriptions (optional)" "${VISION_MODEL:-$DEF_VISION}" \
      $VISION_CANDIDATES
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
  # GPU passthrough for the container. Skip the probe entirely when there's no
  # NVIDIA GPU or no GPU runtime — otherwise every CPU-only machine pulls a
  # ~200 MB CUDA image just to be told "no".
  GPU_FLAG=""
  if [ "${RAG_GPU_COUNT:-0}" != "0" ] && [ "${RAG_DOCKER_GPU:-no}" = "yes" ]; then
    $DOCKER run --rm --gpus all nvidia/cuda:12.6.0-base-ubuntu24.04 true >/dev/null 2>&1 \
      && GPU_FLAG="--gpus all" \
      || warn "GPU runtime present but '--gpus all' failed; starting without GPU"
  elif [ "${RAG_GPU_COUNT:-0}" != "0" ]; then
    warn "NVIDIA GPU present but docker has no GPU runtime — container will use CPU"
  fi
  recreate open-webui \
    --name open-webui \
    --restart always \
    -p "${OPENWEBUI_PORT}:8080" \
    ${GPU_FLAG} \
    --ulimit nofile=65536:65536 \
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
