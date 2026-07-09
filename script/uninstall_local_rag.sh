#!/usr/bin/env bash
#
# uninstall_local_rag.sh
# --------------------------------------------------------------------------
# Removes the local RAG stack created by setup_local_rag.sh:
#   - Open WebUI container   (+ its data volume, only with --purge-data)
#   - AnythingLLM container  (+ its storage dir, only with --purge-data)
#   - Ollama service, systemd override, binary, and models
#
# SAFE BY DEFAULT:
#   * Removes containers and the Ollama software.
#   * KEEPS your data (Open WebUI volume, AnythingLLM storage, pulled models)
#     unless you pass --purge-data.
#   * Prompts for confirmation before doing anything (skip with --yes).
#
# Usage:
#   chmod +x uninstall_local_rag.sh
#   ./uninstall_local_rag.sh                 # remove software, keep data
#   ./uninstall_local_rag.sh --purge-data    # ALSO delete all data + models
#   ./uninstall_local_rag.sh --keep-ollama   # remove containers only
#   ./uninstall_local_rag.sh --yes           # no confirmation prompt
# --------------------------------------------------------------------------

set -uo pipefail

# ===================== CONFIG (match your setup) ==========================
STORAGE_LOCATION="${HOME}/anythingllm"       # AnythingLLM data dir
OPENWEBUI_VOLUME="open-webui"                # docker named volume
OPENWEBUI_CONTAINER="open-webui"
ANYTHINGLLM_CONTAINER="anythingllm"
# ==========================================================================

PURGE_DATA=0
KEEP_OLLAMA=0
ASSUME_YES=0

while [ $# -gt 0 ]; do
  case "$1" in
    --purge-data) PURGE_DATA=1; shift ;;
    --keep-ollama) KEEP_OLLAMA=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

GREEN=$'\e[32m'; RED=$'\e[31m'; YELLOW=$'\e[33m'; BOLD=$'\e[1m'; RESET=$'\e[0m'
step() { echo; echo "${BOLD}==> $1${RESET}"; }
ok()   { echo "  ${GREEN}✔${RESET} $1"; }
warn() { echo "  ${YELLOW}!${RESET} $1"; }

# ---- docker: use sudo automatically if needed ----
DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
  if sudo docker info >/dev/null 2>&1; then
    DOCKER="sudo docker"
  else
    warn "Docker not reachable — will skip container/volume removal"
    DOCKER=""
  fi
fi

# ---- summary of what will happen ----
echo "${BOLD}This will remove the local RAG stack:${RESET}"
echo "  - container: ${OPENWEBUI_CONTAINER}"
echo "  - container: ${ANYTHINGLLM_CONTAINER}"
if [ "$KEEP_OLLAMA" -eq 0 ]; then
  echo "  - Ollama service, systemd override, binary"
else
  echo "  - (Ollama kept: --keep-ollama)"
fi
echo
if [ "$PURGE_DATA" -eq 1 ]; then
  echo "  ${RED}${BOLD}--purge-data: the following WILL BE DELETED permanently:${RESET}"
  echo "  ${RED}  - Open WebUI volume '${OPENWEBUI_VOLUME}' (chats, accounts, knowledge)${RESET}"
  echo "  ${RED}  - AnythingLLM storage '${STORAGE_LOCATION}' (workspaces, embeddings, DB)${RESET}"
  [ "$KEEP_OLLAMA" -eq 0 ] && echo "  ${RED}  - all pulled Ollama models${RESET}"
else
  echo "  ${GREEN}Data is KEPT${RESET} (Open WebUI volume, AnythingLLM storage, models)."
  echo "  Re-run with --purge-data to delete it too."
fi
echo

if [ "$ASSUME_YES" -ne 1 ]; then
  printf "Proceed? [y/N] "
  read -r ans
  case "$ans" in
    y|Y|yes|YES) ;;
    *) echo "Aborted."; exit 0 ;;
  esac
fi

# --------------------------------------------------------------------------
step "Removing containers"
if [ -n "$DOCKER" ]; then
  for c in "$OPENWEBUI_CONTAINER" "$ANYTHINGLLM_CONTAINER"; do
    if [ -n "$($DOCKER ps -aq -f "name=^/${c}$")" ]; then
      $DOCKER rm -f "$c" >/dev/null && ok "removed container '$c'"
    else
      warn "container '$c' not found (already gone)"
    fi
  done
else
  warn "skipped (docker unavailable)"
fi

# --------------------------------------------------------------------------
if [ "$PURGE_DATA" -eq 1 ] && [ -n "$DOCKER" ]; then
  step "Deleting data (--purge-data)"
  if $DOCKER volume inspect "$OPENWEBUI_VOLUME" >/dev/null 2>&1; then
    $DOCKER volume rm "$OPENWEBUI_VOLUME" >/dev/null && ok "removed volume '$OPENWEBUI_VOLUME'"
  else
    warn "volume '$OPENWEBUI_VOLUME' not found"
  fi
  if [ -d "$STORAGE_LOCATION" ]; then
    sudo rm -rf "$STORAGE_LOCATION" && ok "deleted '$STORAGE_LOCATION'"
  else
    warn "'$STORAGE_LOCATION' not found"
  fi
fi

# --------------------------------------------------------------------------
if [ "$KEEP_OLLAMA" -eq 0 ]; then
  step "Removing Ollama"

  # optionally delete models first (before removing the binary/dirs)
  if [ "$PURGE_DATA" -eq 1 ] && command -v ollama >/dev/null 2>&1; then
    if systemctl is-active --quiet ollama 2>/dev/null; then
      for m in $(ollama list 2>/dev/null | awk 'NR>1{print $1}'); do
        ollama rm "$m" >/dev/null 2>&1 && ok "removed model $m"
      done
    fi
  fi

  # stop and disable the service
  if systemctl list-unit-files 2>/dev/null | grep -q '^ollama.service'; then
    sudo systemctl stop ollama 2>/dev/null || true
    sudo systemctl disable ollama 2>/dev/null || true
    ok "stopped and disabled ollama service"
  fi

  # remove systemd unit + our override
  sudo rm -f /etc/systemd/system/ollama.service
  sudo rm -rf /etc/systemd/system/ollama.service.d
  sudo systemctl daemon-reload 2>/dev/null || true
  ok "removed systemd unit and override"

  # remove binary (official installer drops it in /usr/local/bin or /usr/bin)
  for bin in /usr/local/bin/ollama /usr/bin/ollama; do
    [ -e "$bin" ] && sudo rm -f "$bin" && ok "removed $bin"
  done

  # remove the ollama service user's home / model store if purging
  if [ "$PURGE_DATA" -eq 1 ]; then
    sudo rm -rf /usr/share/ollama/.ollama 2>/dev/null && ok "removed /usr/share/ollama model store" || true
    rm -rf "$HOME/.ollama" 2>/dev/null && ok "removed ~/.ollama" || true
  else
    warn "kept model store (pulled models). Use --purge-data to delete."
  fi

  # remove the service user created by the installer (best effort)
  if id ollama >/dev/null 2>&1; then
    sudo userdel ollama 2>/dev/null && ok "removed 'ollama' service user" || warn "could not remove 'ollama' user (in use?)"
  fi
else
  step "Ollama kept (--keep-ollama)"
  warn "left Ollama service, binary, and models untouched"
fi

# --------------------------------------------------------------------------
echo
echo "${BOLD}================ UNINSTALL COMPLETE ================${RESET}"
if [ "$PURGE_DATA" -eq 1 ]; then
  echo "  Software and all data removed."
else
  echo "  Software removed. Data preserved:"
  [ -d "$STORAGE_LOCATION" ] && echo "    - AnythingLLM storage: $STORAGE_LOCATION"
  [ -n "$DOCKER" ] && $DOCKER volume inspect "$OPENWEBUI_VOLUME" >/dev/null 2>&1 && \
    echo "    - Open WebUI volume:   $OPENWEBUI_VOLUME"
  echo "  (delete later with: ./uninstall_local_rag.sh --purge-data)"
fi
echo
echo "  Note: Docker itself and the NVIDIA Container Toolkit were NOT touched"
echo "  (they ship with DGX OS and other apps may rely on them)."
