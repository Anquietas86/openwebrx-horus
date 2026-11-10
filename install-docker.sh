#!/usr/bin/env bash
#
# openwebrx-horus Docker installer
#
# Installs the Horus decoder plugin into a running OpenWebRX+ container.
# Run this ON THE HOST. It copies horus_patch.py and the plugin files into the
# container and runs the same installer the bare-metal script uses.
#
# Usage:
#   ./install-docker.sh [container_name]
#   ./install-docker.sh --uninstall [container_name]
#
# Default container name: openwebrx
#
# Everything lands inside the container, so it is lost when the container is
# recreated (image update, docker compose up --force-recreate). Re-run this
# script afterwards, or use docker-compose.override.example.yml to reinstall
# automatically on every start.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OWRX_PY="/usr/lib/python3/dist-packages"
STAGE="/tmp/openwebrx-horus"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[x]${NC} $*"; exit 1; }

UNINSTALL=false
CONTAINER=""
for arg in "$@"; do
    case "$arg" in
        --uninstall) UNINSTALL=true ;;
        -h|--help)   sed -n '2,18p' "$0"; exit 0 ;;
        *)
            if [[ -z "$CONTAINER" ]]; then
                CONTAINER="$arg"
            else
                # v5.0.0 and earlier took a host plugins path here. It was
                # never actually used for installing, so it is ignored now.
                warn "Ignoring extra argument '$arg' (the host plugins path is no longer used)"
            fi
            ;;
    esac
done
CONTAINER="${CONTAINER:-openwebrx}"

docker inspect "$CONTAINER" > /dev/null 2>&1 || error "Container '$CONTAINER' not found. Is it running?"
docker exec "$CONTAINER" test -d "$OWRX_PY/owrx" || error "owrx/ not found at $OWRX_PY inside $CONTAINER"

info "Copying installer into $CONTAINER..."
tar -C "$SCRIPT_DIR" -cf - horus_patch.py owrx plugin \
    | docker exec -i "$CONTAINER" sh -c "rm -rf '$STAGE' && mkdir -p '$STAGE' && tar -C '$STAGE' -xf -"

if $UNINSTALL; then
    STATUS=0
    docker exec "$CONTAINER" python3 "$STAGE/horus_patch.py" uninstall "$OWRX_PY" || STATUS=$?
else
    STATUS=0
    docker exec "$CONTAINER" python3 "$STAGE/horus_patch.py" install --pip "$OWRX_PY" || STATUS=$?
fi
docker exec "$CONTAINER" rm -rf "$STAGE" || true

[[ $STATUS -eq 0 ]] || error "Installer failed inside the container (exit $STATUS); see the output above."

info "Done. Restart the container to apply:  docker restart $CONTAINER"
if ! $UNINSTALL; then
    echo "    SondeHub Amateur uploads: Settings > Reporting > Sondehub settings (enable + callsign)"
fi
