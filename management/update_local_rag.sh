#!/usr/bin/env bash
#
# update_local_rag.sh
# --------------------------------------------------------------------------
# Updates the local RAG stack when newer versions are available. All data
# (Docker volumes, AnythingLLM storage, Ollama models) is preserved — only
# images/containers are refreshed.
#
# Updates by default (if a newer image exists):
#   - open-webui, anythingllm
#   - optional add-ons if present: tika, ollama-vision
#
# NOT updated by default:
#   - Ollama itself. It is a host package, not a container, and the right update
#     command depends on how it was installed: snap (the DGX Spark default) is
#     `sudo snap refresh ollama`; an install.sh binary is re-running that script.
#     There is NO ollama package in Ubuntu or DGX OS apt repositories. The script
#     detects which case you are in and prints the matching command.
#     (--include-ollama re-runs the generic installer, but only for a binary
#     install — on a snap that would give you two Ollamas, so it refuses.)
#
# Usage:
#   chmod +x update_local_rag.sh
#   ./update_local_rag.sh              # update containers that have newer images
#   ./update_local_rag.sh --check      # only report what's outdated (no changes)
#   ./update_local_rag.sh --yes        # skip the confirmation prompt
#   ./update_local_rag.sh --pull-models        # also re-pull installed models (latest tags)
#   ./update_local_rag.sh --include-ollama     # ALSO update Ollama (see warning above)
#   ./update_local_rag.sh --force-recreate     # recreate containers even if the image
#                                             # is unchanged — use after changing run
#                                             # flags (e.g. --ulimit, ports, env vars)
# --------------------------------------------------------------------------

set -uo pipefail

# ===================== CONFIG (match your setup) ==========================
RUN_USER="${SUDO_USER:-$USER}"
REAL_HOME="$(getent passwd "$RUN_USER" 2>/dev/null | cut -d: -f6)"
[ -z "$REAL_HOME" ] && REAL_HOME="$HOME"

OLLAMA_PORT=11434
OPENWEBUI_PORT=3000
ANYTHINGLLM_PORT=3001
VISION_PORT=11435
STORAGE_LOCATION="${STORAGE_LOCATION:-$REAL_HOME/anythingllm}"
# ==========================================================================

CHECK=0; ASSUME_YES=0; PULL_MODELS=0; INCLUDE_OLLAMA=0; FORCE_RECREATE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --check)          CHECK=1; shift ;;
    --yes|-y)         ASSUME_YES=1; shift ;;
    --pull-models)    PULL_MODELS=1; shift ;;
    --include-ollama) INCLUDE_OLLAMA=1; shift ;;
    --force-recreate) FORCE_RECREATE=1; shift ;;
    -h|--help)        grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

GREEN=$'\e[32m'; RED=$'\e[31m'; YELLOW=$'\e[33m'; BOLD=$'\e[1m'; RESET=$'\e[0m'
step() { echo; echo "${BOLD}==> $1${RESET}"; }
ok()   { echo "  ${GREEN}✔${RESET} $1"; }
warn() { echo "  ${YELLOW}!${RESET} $1"; }
info() { echo "  • $1"; }
die()  { echo "  ${RED}x $1${RESET}"; exit 1; }

# ---- docker: use sudo automatically if needed ----
DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
  if sudo docker info >/dev/null 2>&1; then DOCKER="sudo docker"; else
    die "Docker not available."
  fi
fi

# ---- image helpers ----
img_ref()      { $DOCKER inspect -f '{{.Config.Image}}' "$1" 2>/dev/null; }
running_imgid(){ $DOCKER inspect -f '{{.Image}}' "$1" 2>/dev/null; }
ref_imgid()    { $DOCKER image inspect -f '{{.Id}}' "$1" 2>/dev/null; }
extra_networks() {  # networks other than the default bridge, so we can reattach
  $DOCKER inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$1" 2>/dev/null \
    | tr ' ' '\n' | grep -v '^bridge$' | grep -v '^$'
}

GPU_FLAG=""
$DOCKER run --rm --gpus all nvidia/cuda:12.6.0-base-ubuntu24.04 true >/dev/null 2>&1 && GPU_FLAG="--gpus all"

# ---- recreate commands (must mirror setup_local_rag.sh) ----
recreate_open-webui() {
  $DOCKER rm -f open-webui >/dev/null
  # nofile: embedding a very large document opens thousands of connections to
  # Ollama; the 1024 default causes "Too many open files"
  $DOCKER run -d --name open-webui --restart always \
    -p "${OPENWEBUI_PORT}:8080" ${GPU_FLAG} \
    --ulimit nofile=65536:65536 \
    --add-host=host.docker.internal:host-gateway \
    -e OLLAMA_BASE_URL="http://host.docker.internal:${OLLAMA_PORT}" \
    -v open-webui:/app/backend/data \
    ghcr.io/open-webui/open-webui:main >/dev/null
}
recreate_anythingllm() {
  $DOCKER rm -f anythingllm >/dev/null
  sudo chown -R 1000:1000 "$STORAGE_LOCATION" 2>/dev/null || true
  $DOCKER run -d --name anythingllm --restart unless-stopped \
    -p "${ANYTHINGLLM_PORT}:3001" --cap-add SYS_ADMIN \
    --add-host=host.docker.internal:host-gateway \
    -v "${STORAGE_LOCATION}:/app/server/storage" \
    -v "${STORAGE_LOCATION}/.env:/app/server/.env" \
    -e STORAGE_DIR="/app/server/storage" \
    mintplexlabs/anythingllm:latest >/dev/null
}
recreate_tika() {
  $DOCKER rm -f tika >/dev/null
  $DOCKER run -d --name tika --restart unless-stopped \
    --network rag-net apache/tika:latest >/dev/null
}
recreate_ollama-vision() {
  $DOCKER rm -f ollama-vision >/dev/null
  $DOCKER run -d --name ollama-vision --restart unless-stopped \
    ${GPU_FLAG} -p "${VISION_PORT}:11434" \
    -v ollama_vision:/root/.ollama ollama/ollama >/dev/null
}

# ---- update one container ----
update_container() {
  local c="$1"
  local ref; ref="$(img_ref "$c")"
  if [ -z "$ref" ]; then
    info "$c not installed — skipping"
    return
  fi
  info "checking $c ($ref)…"
  if ! $DOCKER pull "$ref" >/dev/null 2>&1; then
    warn "$c: could not pull $ref (network?) — skipping"
    return
  fi
  if [ "$(running_imgid "$c")" = "$(ref_imgid "$ref")" ] && [ "$FORCE_RECREATE" -eq 0 ]; then
    ok "$c is up to date"
    return
  fi
  if [ "$FORCE_RECREATE" -eq 1 ] && [ "$(running_imgid "$c")" = "$(ref_imgid "$ref")" ]; then
    info "$c image unchanged, but --force-recreate was given (applies new run flags)"
  fi
  UPDATES_AVAILABLE=$((UPDATES_AVAILABLE+1))
  if [ "$CHECK" -eq 1 ]; then
    warn "$c: a newer image is available (run without --check to apply)"
    return
  fi
  # remember extra networks (e.g. rag-net) to reattach after recreate
  local nets; nets="$(extra_networks "$c")"
  "recreate_${c}"
  for n in $nets; do
    $DOCKER network connect "$n" "$c" >/dev/null 2>&1 && info "  reattached $c to network '$n'" || true
  done
  # confirm it came back up
  if [ "$($DOCKER inspect -f '{{.State.Status}}' "$c" 2>/dev/null)" = "running" ]; then
    ok "$c updated and running"
  else
    warn "$c updated but not running — check: $DOCKER logs $c"
  fi
}

UPDATES_AVAILABLE=0

# ---- summary / confirm ----
echo "${BOLD}Update local RAG stack${RESET}"
echo "  containers: open-webui, anythingllm, tika (if present), ollama-vision (if present)"
[ "$FORCE_RECREATE" -eq 1 ] && echo "  + --force-recreate: recreate even if the image is unchanged"
[ "$PULL_MODELS" -eq 1 ]    && echo "  + re-pull installed Ollama models to latest tags"
[ "$INCLUDE_OLLAMA" -eq 1 ] && echo "  ${RED}+ update Ollama via the generic installer (see warning)${RESET}"
[ "$CHECK" -eq 1 ]          && echo "  (--check: report only, no changes)"
echo "  data (volumes, storage, models) is preserved."
echo
if [ "$CHECK" -eq 0 ] && [ "$ASSUME_YES" -ne 1 ]; then
  printf "Proceed? [y/N] "; read -r a; case "$a" in y|Y|yes|YES) ;; *) echo "Aborted."; exit 0 ;; esac
fi

# ---- containers ----
step "Containers"
for c in open-webui anythingllm tika ollama-vision; do
  update_container "$c"
done

# ---- models (opt-in) ----
if [ "$PULL_MODELS" -eq 1 ] && [ "$CHECK" -eq 0 ]; then
  step "Re-pulling Ollama models"
  if command -v ollama >/dev/null 2>&1; then
    for m in $(ollama list 2>/dev/null | awk 'NR>1{print $1}'); do
      info "pulling $m…"; ollama pull "$m" >/dev/null 2>&1 && ok "$m updated" || warn "$m pull failed"
    done
  else
    warn "ollama CLI not found — skipping model pulls"
  fi
fi

# ---- Ollama (opt-in) ----
#
# This script's remit is the containers. Ollama is left alone by default because it
# is a host package, and — importantly — because HOW it should be updated depends on
# how it was installed. There is no Ollama package in the Ubuntu or DGX OS apt
# repositories: DGX OS updates the driver, firmware and CUDA stack, not Ollama.
#
#   snap    preinstalled on DGX Spark images (/snap/bin/ollama, configured with
#           `snap set ollama host=…`, no plain ollama.service). Update: snap refresh.
#           Running the generic installer on top of a snap leaves TWO installations
#           and a PATH that decides which one you get — that is the thing to avoid.
#   binary  installed by ollama.com/install.sh into /usr/local/bin. Update: re-run it.
#   deb     packaged by some distributions. Update: the package manager.
ollama_install_method() {
  local bin real
  bin=$(command -v ollama 2>/dev/null) || { echo absent; return; }
  real=$(readlink -f "$bin" 2>/dev/null || echo "$bin")
  case "$real" in /snap/*) echo snap; return ;; esac
  if command -v snap >/dev/null 2>&1 && snap list ollama >/dev/null 2>&1; then
    echo snap; return
  fi
  if command -v dpkg >/dev/null 2>&1 && dpkg -S "$real" >/dev/null 2>&1; then
    echo deb; return
  fi
  echo binary
}

step "Ollama"
OLLAMA_METHOD=$(ollama_install_method)
info "current: $(ollama --version 2>/dev/null | head -1 || echo unknown)   installed as: $OLLAMA_METHOD"

case "$OLLAMA_METHOD" in
  snap)
    info "update with:  sudo snap refresh ollama        (snaps also auto-refresh)"
    info "check channel: snap info ollama | grep -A3 channels"
    if [ "$INCLUDE_OLLAMA" -eq 1 ]; then
      warn "--include-ollama runs the GENERIC installer, which would install a SECOND"
      warn "Ollama alongside the snap. Use 'sudo snap refresh ollama' instead."
      info "not touching it"
    fi
    ;;
  deb)
    info "update with your package manager (e.g. sudo apt install --only-upgrade ollama)"
    [ "$INCLUDE_OLLAMA" -eq 1 ] && warn "--include-ollama would shadow the packaged copy; using apt is cleaner"
    ;;
  binary)
    if [ "$INCLUDE_OLLAMA" -eq 1 ] && [ "$CHECK" -eq 0 ]; then
      # Worth knowing on GB10: early Spark images shipped an Ollama ahead of upstream
      # GB10 support. Upstream has since caught up, so a recent version is not
      # "downgraded" by re-running the installer — but a GPU check afterwards is cheap.
      printf "  Re-run the ollama.com installer to update in place? [y/N] "; read -r a
      case "$a" in
        y|Y|yes|YES)
          # install.sh REWRITES /etc/systemd/system/ollama.service. Anything set
          # directly in that unit — notably OLLAMA_HOST=0.0.0.0, which is what lets the
          # containers reach Ollama — is lost, and the symptom is Open WebUI showing no
          # models after an update that "worked". Drop-ins under ollama.service.d/
          # survive; the unit itself does not. So: record the environment, back up the
          # unit, and compare afterwards.
          # `ollama --version` reports the SERVER's version when one is reachable, so
          # it keeps showing the old number if the service wasn't really restarted.
          # The API is the authoritative source; record it to compare afterwards.
          SRV_BEFORE=$(curl -fsS -m 5 "${OLLAMA_URL:-http://localhost:11434}/api/version" 2>/dev/null \
                       | sed 's/.*"version" *: *"\([^"]*\)".*/\1/')
          ENV_BEFORE=$(systemctl show ollama -p Environment --value 2>/dev/null || true)
          UNIT=/etc/systemd/system/ollama.service
          if [ -f "$UNIT" ]; then
            sudo cp -a "$UNIT" "$UNIT.bak.$(date +%Y%m%d%H%M%S)" 2>/dev/null \
              && info "backed up $UNIT"
          fi
          [ -n "$ENV_BEFORE" ] && info "environment before: $ENV_BEFORE"

          curl -fsSL https://ollama.com/install.sh | sh
          sudo systemctl daemon-reload 2>/dev/null || true
          sudo systemctl restart ollama 2>/dev/null || warn "systemctl restart ollama failed"
          sleep 3

          # Did the RUNNING server actually change? A new file on disk proves nothing.
          SRV_AFTER=$(curl -fsS -m 5 "${OLLAMA_URL:-http://localhost:11434}/api/version" 2>/dev/null \
                      | sed 's/.*"version" *: *"\([^"]*\)".*/\1/')
          BIN_PATH=$(command -v ollama 2>/dev/null || echo /usr/local/bin/ollama)
          if [ -n "$SRV_AFTER" ] && [ "$SRV_AFTER" != "$SRV_BEFORE" ]; then
            ok "server is now ${SRV_AFTER} (was ${SRV_BEFORE:-unknown})"
          elif [ -n "$SRV_AFTER" ]; then
            warn "server STILL reports ${SRV_AFTER} — the new binary is not the one running"
            info "the binary on disk: $("$BIN_PATH" --version 2>/dev/null | head -1)"
            info "what the service runs:"
            systemctl cat ollama 2>/dev/null | grep -E '^ExecStart' | sed 's/^/    /'
            info "other copies on PATH:"
            command -v -a ollama 2>/dev/null | sed 's/^/    /' || true
            info "if ExecStart names a different path than $BIN_PATH, that stale binary"
            info "is what is serving; point the unit at the new one, or remove the old copy"
          else
            warn "no answer from the Ollama API — is the service running? systemctl status ollama"
          fi

          ENV_AFTER=$(systemctl show ollama -p Environment --value 2>/dev/null || true)
          if [ "$ENV_BEFORE" != "$ENV_AFTER" ]; then
            warn "the service environment CHANGED:"
            warn "  before: ${ENV_BEFORE:-<none>}"
            warn "  after:  ${ENV_AFTER:-<none>}"
            case "$ENV_BEFORE" in
              *OLLAMA_HOST*) case "$ENV_AFTER" in
                *OLLAMA_HOST*) : ;;
                *) warn "OLLAMA_HOST is gone — containers will not reach Ollama. Restore with:"
                   warn "  sudo systemctl edit ollama   ->  [Service]"
                   warn "  Environment=\"OLLAMA_HOST=0.0.0.0:11434\""
                   warn "  then: sudo systemctl daemon-reload && sudo systemctl restart ollama" ;;
              esac ;;
            esac
          else
            ok "service environment preserved"
          fi
          info "verify next: ./llm_stack_healthcheck.sh"
          info "and 'ollama ps' — PROCESSOR should stay 100% GPU, and note the CONTEXT"
          info "column: default context sizing has changed between Ollama versions"
          ;;
        *) info "left unchanged" ;;
      esac
    else
      info "update with:  curl -fsSL https://ollama.com/install.sh | sh   (or --include-ollama)"
    fi
    ;;
  absent)
    warn "ollama CLI not found on PATH — nothing to report"
    ;;
esac

# ---- done ----
echo
echo "${BOLD}================ DONE ================${RESET}"
if [ "$CHECK" -eq 1 ]; then
  echo "  ${UPDATES_AVAILABLE} container image(s) have updates available."
  [ "$UPDATES_AVAILABLE" -gt 0 ] && echo "  Re-run without --check to apply."
else
  echo "  Update complete. Verify with:  ./llm_stack_healthcheck.sh"
fi
