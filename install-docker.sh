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
docker cp "$SCRIPT_DIR/owrx/chain/horus.py" "$CONTAINER:$OWRX_PY/owrx/chain/horus.py"
info "Copied owrx/horus.py and owrx/chain/horus.py"

# ── Install Step 4: Patch Python source (inside container) ─────────

info "Patching OpenWebRX source inside container..."

# Copy the patch script into the container and run it
docker exec -i "$CONTAINER" python3 - "$OWRX_PY" "$MARKER" << 'PYEOF'
import sys, os, re

owrx_py = sys.argv[1]
marker = sys.argv[2]

def patch_file(path, check_str, patch_func):
    with open(path, 'r') as f:
        content = f.read()
    if check_str in content:
        print(f"  {os.path.basename(path)} already patched, skipping")
        return
    content = patch_func(content)
    with open(path, 'w') as f:
        f.write(content)
    print(f"  Patched {os.path.basename(path)}")

# ── feature.py ──────────────────────────────────────────────────

def patch_feature(content):
    m = "# openwebrx-horus"

    # Add to features dict
    feature_entry = '\n        {m} BEGIN\n        "horusdemodlib": ["horusdemodlib"],\n        {m} END'.format(m=m)

    lines = content.split('\n')
    in_features = False
    last_entry_idx = None
    brace_depth = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if 'features' in line and '{' in line and '=' in line and not in_features:
            in_features = True
            brace_depth = line.count('{') - line.count('}')
            continue
        if in_features:
            brace_depth += line.count('{') - line.count('}')
            if stripped.startswith('"') and ':' in stripped:
                last_entry_idx = i
            if brace_depth <= 0:
                break

    if last_entry_idx is not None:
        lines.insert(last_entry_idx + 1, feature_entry)

    method = '\n    {m} BEGIN\n    def has_horusdemodlib(self):\n        try:\n            from horusdemodlib.demod import HorusLib, Mode\n            test = HorusLib(mode=Mode.BINARY, sample_rate=48000)\n            test.close()\n            return True\n        except Exception:\n            return False\n    {m} END'.format(m=m)

    content = '\n'.join(lines)
    content = content.rstrip() + '\n' + method + '\n'
    return content

patch_file(
    os.path.join(owrx_py, 'owrx', 'feature.py'),
    'horusdemodlib',
    patch_feature
)

# ── modes.py ────────────────────────────────────────────────────

def patch_modes(content):
    m = "# openwebrx-horus"

    new_modes = '        {m} BEGIN\n        DigitalMode(\n            modulation="horus_binary",\n            name="Horus Binary",\n            underlying="nfm",\n            bandpass=Bandpass(-4000, 4000),\n            ifRate=48000,\n            requirements=["horusdemodlib"],\n            service=True,\n        ),\n        DigitalMode(\n            modulation="horus_rtty",\n            name="Horus RTTY",\n            underlying="usb",\n            bandpass=Bandpass(300, 3000),\n            ifRate=48000,\n            requirements=["horusdemodlib"],\n            service=True,\n        ),\n        {m} END'.format(m=m)

    lines = content.split('\n')
    insert_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == ']':
            insert_idx = i
            break

    if insert_idx is not None:
        lines.insert(insert_idx, new_modes)

    return '\n'.join(lines)

patch_file(
    os.path.join(owrx_py, 'owrx', 'modes.py'),
    'horus_binary',
    patch_modes
)

# ── service/__init__.py ─────────────────────────────────────────

def patch_service(content):
    m = "# openwebrx-horus"

    import_block = '{m} BEGIN\nfrom owrx.chain.horus import HorusDemodulatorChain\nfrom owrx.horus import HorusParser\n{m} END'.format(m=m)

    lines = content.split('\n')
    last_import_idx = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(('import ', 'from ')) and not stripped.startswith('#'):
            last_import_idx = i

    lines.insert(last_import_idx + 1, import_block)
    content = '\n'.join(lines)

    demod_block = '        {m} BEGIN\n        elif mod == "horus_binary":\n            return HorusDemodulatorChain(mode_str="horus_binary")\n        elif mod == "horus_rtty":\n            return HorusDemodulatorChain(mode_str="horus_rtty")\n        {m} END\n'.format(m=m)

    content = content.replace(
        '        raise ValueError("unsupported service modulation',
        demod_block + '        raise ValueError("unsupported service modulation'
    )

    return content

patch_file(
    os.path.join(owrx_py, 'owrx', 'service', '__init__.py'),
    'horus_binary',
    patch_service
)

print("  All patches applied")
PYEOF

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
