#!/usr/bin/env bash
#
# openwebrx-horus installer
#
# Installs the Horus balloon telemetry decoder plugin into an existing
# OpenWebRX+ installation. Patches are idempotent — safe to run twice.
#
# Usage:
#   ./install.sh [/path/to/openwebrx]
#
# Default OpenWebRX path: /opt/openwebrx
#
# To uninstall:
#   ./install.sh --uninstall [/path/to/openwebrx]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_OWRX="/opt/openwebrx"
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
OWRX=""

for arg in "$@"; do
    case "$arg" in
        --uninstall) UNINSTALL=true ;;
        *)           OWRX="$arg" ;;
    esac
done

OWRX="${OWRX:-$DEFAULT_OWRX}"

# ── Validation ──────────────────────────────────────────────────────

[[ -d "$OWRX/owrx" ]]   || error "OpenWebRX not found at $OWRX (no owrx/ directory)"
[[ -d "$OWRX/htdocs" ]]  || error "OpenWebRX not found at $OWRX (no htdocs/ directory)"
[[ -f "$OWRX/owrx/modes.py" ]]   || error "Missing $OWRX/owrx/modes.py"
[[ -f "$OWRX/owrx/feature.py" ]] || error "Missing $OWRX/owrx/feature.py"

info "OpenWebRX path: $OWRX"

# ── Backup helper ───────────────────────────────────────────────────

backup() {
    local f="$1"
    if [[ -f "$f" && ! -f "$f.pre-horus" ]]; then
        cp "$f" "$f.pre-horus"
        info "Backed up $f → $f.pre-horus"
    fi
}

# ── Uninstall ───────────────────────────────────────────────────────

if $UNINSTALL; then
    info "Uninstalling openwebrx-horus..."

    # Remove copied files
    rm -f "$OWRX/owrx/horus.py"
    rm -f "$OWRX/owrx/chain/horus.py"
    rm -rf "$OWRX/htdocs/plugins/receiver/horus"
    info "Removed plugin files"

    # Remove init.js entry
    INIT_JS="$OWRX/htdocs/plugins/receiver/init.js"
    if [[ -f "$INIT_JS" ]] && grep -q "'horus'" "$INIT_JS"; then
        sed -i "/'horus'/d" "$INIT_JS"
        info "Removed horus from init.js"
    fi

    # Remove patched blocks from Python source files
    for f in \
        "$OWRX/owrx/feature.py" \
        "$OWRX/owrx/modes.py" \
        "$OWRX/owrx/service/__init__.py" \
        "$OWRX/owrx/dsp.py" \
        "$OWRX/htdocs/openwebrx.js"
    do
        if [[ -f "$f" ]] && grep -q "$MARKER" "$f"; then
            sed -i "/$MARKER BEGIN/,/$MARKER END/d" "$f"
            info "Removed patches from $f"
        fi
    done

    info "Uninstall complete. Restart OpenWebRX to apply."
    exit 0
fi

# ── Install: check horusdemodlib ────────────────────────────────────

if python3 -c "import horusdemodlib" 2>/dev/null; then
    info "horusdemodlib found"
else
    warn "horusdemodlib not installed. Installing via pip (--user)..."
    pip3 install --user horusdemodlib || error "Failed to install horusdemodlib"
    info "horusdemodlib installed"
fi

# ── Install: copy plugin files ──────────────────────────────────────

mkdir -p "$OWRX/owrx/chain"
touch "$OWRX/owrx/chain/__init__.py"
cp "$SCRIPT_DIR/owrx/horus.py"       "$OWRX/owrx/horus.py"
cp "$SCRIPT_DIR/owrx/chain/horus.py" "$OWRX/owrx/chain/horus.py"
info "Copied Python modules"

mkdir -p "$OWRX/htdocs/lib" "$OWRX/htdocs/css"
cp "$SCRIPT_DIR/htdocs/lib/HorusMessagePanel.js" "$OWRX/htdocs/lib/"
cp "$SCRIPT_DIR/htdocs/css/horus.css"             "$OWRX/htdocs/css/"
info "Copied frontend files"

# ── Install: patch feature.py ───────────────────────────────────────

FEATURE_FILE="$OWRX/owrx/feature.py"

if grep -q "horusdemodlib" "$FEATURE_FILE"; then
    info "feature.py already patched, skipping"
else
    backup "$FEATURE_FILE"

    # Insert "horusdemodlib" into the features dict (after the last entry)
    python3 - "$FEATURE_FILE" <<'PYEOF'
import sys, re

path = sys.argv[1]
with open(path, 'r') as f:
    content = f.read()

# Add to features dict — find the closing brace of the dict
# Insert before the last } in the features = { ... } block
marker = "# openwebrx-horus"

feature_entry = '''
        {marker} BEGIN
        "horusdemodlib": ["horusdemodlib"],
        {marker} END'''.format(marker=marker)

# Find "features = {" and its closing "}"
# Insert the new entry before the last requirement in the dict
# Strategy: find the last line before the closing } of features
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

# Add the has_ method at the end of the class
method = '''
    {marker} BEGIN
    def has_horusdemodlib(self):
        try:
            from horusdemodlib.demod import HorusLib, Mode
            test = HorusLib(mode=Mode.BINARY, sample_rate=48000)
            test.close()
            return True
        except Exception:
            return False
    {marker} END'''.format(marker=marker)

# Append before the last line if it's empty, or at the end
content = '\n'.join(lines)
content = content.rstrip() + '\n' + method + '\n'

with open(path, 'w') as f:
    f.write(content)
PYEOF
    info "Patched feature.py"
fi

# ── Install: patch modes.py ─────────────────────────────────────────

MODES_FILE="$OWRX/owrx/modes.py"

if grep -q "horus_binary" "$MODES_FILE"; then
    info "modes.py already patched, skipping"
else
    backup "$MODES_FILE"

    python3 - "$MODES_FILE" <<'PYEOF'
import sys

path = sys.argv[1]
marker = "# openwebrx-horus"

with open(path, 'r') as f:
    content = f.read()

# Find the last entry in Modes.mappings list and insert after it.
# Look for the last DigitalMode/AnalogMode/ServiceOnlyMode entry.
lines = content.split('\n')

# Find the closing ] of the mappings list
insert_idx = None
for i in range(len(lines) - 1, -1, -1):
    stripped = lines[i].strip()
    if stripped == ']':
        # Walk back to find the previous entry
        insert_idx = i
        break

if insert_idx is not None:
    new_modes = '''        {marker} BEGIN
        DigitalMode(
            modulation="horus_binary",
            name="Horus Binary",
            underlying=["usb"],
            bandpass=Bandpass(100, 4000),
            requirements=["horusdemodlib"],
            service=True,
            squelch=False,
        ),
        DigitalMode(
            modulation="horus_rtty",
            name="Horus RTTY",
            underlying=["usb"],
            bandpass=Bandpass(300, 3000),
            requirements=["horusdemodlib"],
            service=True,
        ),
        {marker} END'''.format(marker=marker)

    lines.insert(insert_idx, new_modes)

with open(path, 'w') as f:
    f.write('\n'.join(lines))
PYEOF
    info "Patched modes.py"
fi

# ── Install: patch service/__init__.py ──────────────────────────────

SERVICE_FILE="$OWRX/owrx/service/__init__.py"

if grep -q "horus_binary" "$SERVICE_FILE"; then
    info "service/__init__.py already patched, skipping"
else
    backup "$SERVICE_FILE"

    python3 - "$SERVICE_FILE" <<'PYEOF'
import sys

path = sys.argv[1]
marker = "# openwebrx-horus"

with open(path, 'r') as f:
    content = f.read()

# Use inline imports inside elif branches — matches the existing pattern
# in service/__init__.py and avoids top-level import of owrx.chain
lines = content.split('\n')
raise_idx = None
indent = ""
for i, line in enumerate(lines):
    if 'raise ValueError("unsupported service modulation' in line:
        raise_idx = i
        indent = line[:len(line) - len(line.lstrip())]
        break

if raise_idx is not None:
    demod_lines = [
        indent + marker + " BEGIN",
        indent + 'elif mod == "horus_binary":',
        indent + '    from owrx.chain.horus import HorusDemodulatorChain',
        indent + '    return HorusDemodulatorChain(mode_str="horus_binary")',
        indent + 'elif mod == "horus_rtty":',
        indent + '    from owrx.chain.horus import HorusDemodulatorChain',
        indent + '    return HorusDemodulatorChain(mode_str="horus_rtty")',
        indent + marker + " END",
    ]
    for j, dl in enumerate(demod_lines):
        lines.insert(raise_idx + j, dl)

with open(path, 'w') as f:
    f.write('\n'.join(lines))
PYEOF
    info "Patched service/__init__.py"
fi

# ── Install: frontend plugin ────────────────────────────────────────

info "Installing frontend plugin..."

# Copy plugin files to OpenWebRX's plugin directory
PLUGIN_DIR="$OWRX/htdocs/plugins/receiver/horus"
mkdir -p "$PLUGIN_DIR"
cp "$SCRIPT_DIR/plugin/horus/horus.js"  "$PLUGIN_DIR/"
cp "$SCRIPT_DIR/plugin/horus/horus.css" "$PLUGIN_DIR/"
info "Copied plugin to $PLUGIN_DIR"

# Create or update init.js to load the horus plugin
INIT_JS="$OWRX/htdocs/plugins/receiver/init.js"
if [ ! -f "$INIT_JS" ]; then
    echo "Plugins.load('horus');" > "$INIT_JS"
elif ! grep -q "'horus'" "$INIT_JS"; then
    echo "Plugins.load('horus');" >> "$INIT_JS"
fi
info "init.js updated"

# ── Install: patch openwebrx.js (add horus to panel list) ────────────

JS_FILE="$OWRX/htdocs/openwebrx.js"

if grep -q "'horus'" "$JS_FILE"; then
    info "openwebrx.js already patched, skipping"
else
    backup "$JS_FILE"

    python3 - "$JS_FILE" <<'PYEOF'
import sys

path = sys.argv[1]
marker = "// openwebrx-horus"

with open(path, 'r') as f:
    content = f.read()

# Add 'horus' to the secondary_demod panel ID list.
# The framework iterates this list to route messages to panels.
# Pattern: ['wsjt', 'packet', ..., 'meshtastic'] → add 'horus'
# We find the array containing 'meshtastic' and insert 'horus' before the closing ]
lines = content.split('\n')
for i, line in enumerate(lines):
    stripped = line.strip()
    if "'meshtastic'" in stripped and stripped.endswith("].map("):
        # Insert 'horus' before the closing bracket
        new_line = line.replace("'meshtastic']", "'meshtastic', 'horus']")
        lines[i] = marker + " BEGIN"
        lines.insert(i + 1, new_line)
        lines.insert(i + 2, marker + " END")
        break

with open(path, 'w') as f:
    f.write('\n'.join(lines))
PYEOF
    info "Patched openwebrx.js"
fi

# ── Install: patch dsp.py ──────────────────────────────────────────

if grep -q 'horus_binary' "$DSP_FILE"; then
    info "dsp.py already patched, skipping"
else
    backup "$DSP_FILE"

    python3 - "$DSP_FILE" <<'PYEOF'
import sys

path = sys.argv[1]
marker = "# openwebrx-horus"

with open(path, 'r') as f:
    content = f.read()

# 1. Fix ModulationValidator regex to allow underscores for horus modulations
content = content.replace('"^[a-z0-9\\-]+$"', '"^[a-z0-9_\\-]+$"', 1)

# 2. Add Horus to _getSecondaryDemodulator() — before setSecondaryDemodulator method
lines = content.split('\n')
insert_idx = None
indent = "        "
for i, line in enumerate(lines):
    if 'def setSecondaryDemodulator(self, mod):' in line:
        insert_idx = i
        break

if insert_idx is not None:
    block = [
        indent + marker + " BEGIN",
        indent + 'elif mod == "horus_binary":',
        indent + '    from owrx.chain.horus import HorusDemodulatorChain',
        indent + '    return HorusDemodulatorChain(mode_str="horus_binary")',
        indent + 'elif mod == "horus_rtty":',
        indent + '    from owrx.chain.horus import HorusDemodulatorChain',
        indent + '    return HorusDemodulatorChain(mode_str="horus_rtty")',
        indent + marker + " END",
        "",
    ]
    for j, dl in enumerate(block):
        lines.insert(insert_idx + j, dl)

with open(path, 'w') as f:
    f.write('\n'.join(lines))
PYEOF
    info "Patched dsp.py"
fi

# ── Done ────────────────────────────────────────────────────────────

echo ""
info "Installation complete!"
echo ""
echo "  Next steps:"
echo "    1. Restart OpenWebRX:  systemctl restart openwebrx"
echo "    2. Check the Features page to confirm 'horusdemodlib' shows as available"
echo "    3. Add a Horus Binary profile on your 70cm SDR (e.g. 434.200 MHz)"
echo "    4. Set your receiver callsign and GPS in Settings for SondeHub upload"
echo ""
echo "  To uninstall:  $0 --uninstall $OWRX"
echo ""
