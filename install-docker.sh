#!/usr/bin/env bash
#
# openwebrx-horus Docker installer
#
# Installs the Horus balloon telemetry decoder plugin into a running
# OpenWebRX+ Docker container.
#
# Run this ON THE HOST, not inside the container.
#
# Usage:
#   ./install-docker.sh [container_name] [host_plugins_path]
#
# Defaults:
#   container_name:   openwebrx
#   host_plugins_path: /opt/openwebrx/plugins
#
# What it does:
#   1. Copies the frontend plugin to the host plugins volume
#      (persists across container rebuilds)
#   2. Installs horusdemodlib + Python modules inside the container
#      (must be re-run after container rebuild)
#   3. Patches the Python source inside the container
#
# To uninstall:
#   ./install-docker.sh --uninstall [container_name] [host_plugins_path]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MARKER="# openwebrx-horus"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[x]${NC} $*"; exit 1; }

# ── Argument parsing ────────────────────────────────────────────────

UNINSTALL=false
CONTAINER=""
PLUGINS_PATH=""

for arg in "$@"; do
    case "$arg" in
        --uninstall) UNINSTALL=true ;;
        *)
            if [[ -z "$CONTAINER" ]]; then
                CONTAINER="$arg"
            else
                PLUGINS_PATH="$arg"
            fi
            ;;
    esac
done

CONTAINER="${CONTAINER:-openwebrx}"
PLUGINS_PATH="${PLUGINS_PATH:-/opt/openwebrx/plugins}"

# Python source path inside the container (Debian package layout)
OWRX_PY="/usr/lib/python3/dist-packages"

# ── Validation ──────────────────────────────────────────────────────

docker inspect "$CONTAINER" > /dev/null 2>&1 || error "Container '$CONTAINER' not found. Is it running?"
[[ -d "$PLUGINS_PATH" ]] || error "Plugins path '$PLUGINS_PATH' not found on host"

info "Container: $CONTAINER"
info "Host plugins path: $PLUGINS_PATH"
info "Container Python path: $OWRX_PY"

# ── Verify paths inside container ───────────────────────────────────

docker exec "$CONTAINER" test -d "$OWRX_PY/owrx" || error "owrx/ not found at $OWRX_PY inside container"

# ── Uninstall ───────────────────────────────────────────────────────

if $UNINSTALL; then
    info "Uninstalling openwebrx-horus..."

    # Remove host-side plugin
    rm -rf "$PLUGINS_PATH/horus"
    info "Removed plugin from $PLUGINS_PATH/horus"

    # Remove init.js entry
    if [[ -f "$PLUGINS_PATH/init.js" ]] && grep -q "horus" "$PLUGINS_PATH/init.js"; then
        sed -i "/horus/d" "$PLUGINS_PATH/init.js"
        info "Removed horus from init.js"
    fi

    # Remove container-side files and patches
    docker exec "$CONTAINER" bash -c "
        rm -f $OWRX_PY/owrx/horus.py
        rm -f $OWRX_PY/owrx/chain/horus.py

        for f in $OWRX_PY/owrx/feature.py $OWRX_PY/owrx/modes.py $OWRX_PY/owrx/service/__init__.py; do
            if grep -q '$MARKER' \"\$f\" 2>/dev/null; then
                sed -i '/$MARKER BEGIN/,/$MARKER END/d' \"\$f\"
            fi
        done
    "
    info "Removed container-side files and patches"

    warn "Restart the container to apply: docker restart $CONTAINER"
    exit 0
fi

# ── Install Step 1: Frontend plugin (host-side, persists) ───────────

info "Installing frontend plugin..."

mkdir -p "$PLUGINS_PATH/horus"
cp "$SCRIPT_DIR/plugin/horus/horus.js"  "$PLUGINS_PATH/horus/"
cp "$SCRIPT_DIR/plugin/horus/horus.css" "$PLUGINS_PATH/horus/"
info "Copied plugin to $PLUGINS_PATH/horus/"

# Create or update init.js to load the horus plugin
if [[ ! -f "$PLUGINS_PATH/init.js" ]]; then
    cat > "$PLUGINS_PATH/init.js" << 'EOF'
// OpenWebRX+ receiver plugins
Plugins.load('horus');
EOF
    info "Created init.js with horus plugin"
elif ! grep -q "'horus'" "$PLUGINS_PATH/init.js"; then
    echo "Plugins.load('horus');" >> "$PLUGINS_PATH/init.js"
    info "Added horus to existing init.js"
else
    info "init.js already loads horus, skipping"
fi

# ── Install Step 2: horusdemodlib (inside container) ────────────────

info "Installing horusdemodlib inside container..."

docker exec "$CONTAINER" bash -c "
    python3 -c 'import horusdemodlib' 2>/dev/null && echo 'ALREADY_INSTALLED' || {
        pip3 install horusdemodlib 2>&1 | tail -1
    }
"
info "horusdemodlib ready"

# ── Install Step 3: Python modules (inside container) ──────────────

info "Copying Python modules into container..."

docker cp "$SCRIPT_DIR/owrx/horus.py"       "$CONTAINER:$OWRX_PY/owrx/horus.py"
docker exec "$CONTAINER" mkdir -p "$OWRX_PY/owrx/chain"
docker exec "$CONTAINER" touch "$OWRX_PY/owrx/chain/__init__.py"
docker cp "$SCRIPT_DIR/owrx/chain/horus.py" "$CONTAINER:$OWRX_PY/owrx/chain/horus.py"
info "Copied owrx/horus.py and owrx/chain/horus.py"

# ── Install Step 4: Patch Python source (inside container) ─────────

info "Patching OpenWebRX source inside container..."

docker cp "$SCRIPT_DIR/docker-patch.py" "$CONTAINER:/tmp/docker-patch.py"
docker exec "$CONTAINER" python3 /tmp/docker-patch.py "$OWRX_PY"
docker exec "$CONTAINER" rm -f /tmp/docker-patch.py

info "Patches applied"

# ── Done ────────────────────────────────────────────────────────────

echo ""
info "Installation complete!"
echo ""
echo "  What persists across container rebuilds:"
echo "    ✓ Frontend plugin ($PLUGINS_PATH/horus/)"
echo "    ✓ Plugin init.js entry"
echo ""
echo "  What must be re-applied after container rebuild:"
echo "    ✗ Python modules (owrx/horus.py, owrx/chain/horus.py)"
echo "    ✗ Python patches (feature.py, modes.py, service/__init__.py)"
echo "    ✗ horusdemodlib pip package"
echo "    → Re-run this script after rebuilding the container"
echo ""
echo "  Next steps:"
echo "    1. Restart the container:  docker restart $CONTAINER"
echo "    2. Check Features page for 'horusdemodlib'"
echo "    3. Add a Horus Binary profile (e.g. 434.200 MHz)"
echo "    4. Set receiver callsign + GPS in Settings for SondeHub upload"
echo ""
echo "  To uninstall:  $0 --uninstall $CONTAINER $PLUGINS_PATH"
echo ""
