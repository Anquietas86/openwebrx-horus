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

    # Remove patched blocks from Python source files using line-by-line parsing.
    # Avoids naive sed range deletion which can remove adjacent code (pitfall #17).
    for f in \
        "$OWRX/owrx/feature.py" \
        "$OWRX/owrx/modes.py" \
        "$OWRX/owrx/service/__init__.py" \
        "$OWRX/owrx/dsp.py" \
        "$OWRX/htdocs/openwebrx.js"
    do
        if [[ -f "$f" ]] && grep -q "$MARKER" "$f"; then
            python3 - "$f" "$MARKER" <<'PYEOF'
import sys
path, marker = sys.argv[1], sys.argv[2]
with open(path, 'r') as fh:
    lines = fh.readlines()
cleaned = []
skipping = False
for line in lines:
    stripped = line.strip()
    if (marker + " BEGIN") in stripped or ("<!-- " + marker.lstrip("# ") + " BEGIN -->") in stripped:
        skipping = True
        continue
    if (marker + " END") in stripped or ("<!-- " + marker.lstrip("# ") + " END -->") in stripped:
        skipping = False
        continue
    if not skipping:
        cleaned.append(line)
with open(path, 'w') as fh:
    fh.writelines(cleaned)
PYEOF
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

# Frontend files are copied to the plugin directory below (lines 310-314).
# No separate htdocs/lib/ or htdocs/css/ copies needed — the plugin system
# serves them from htdocs/plugins/receiver/horus/.

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

# OpenWebRX+ unconditionally requests /static/css/custom.css for user style
# overrides. Note the URL prefix: /static/ maps to htdocs/, NOT htdocs/static/
# (the plugin JS is served from /static/plugins/... out of htdocs/plugins/...).
# So the file belongs at htdocs/css/custom.css. If it is absent the browser
# logs a 404 on every page load, which masks real errors.
CUSTOM_CSS="$OWRX/htdocs/css/custom.css"
if [ ! -f "$CUSTOM_CSS" ]; then
    mkdir -p "$(dirname "$CUSTOM_CSS")"
    : > "$CUSTOM_CSS"
    info "Created empty $CUSTOM_CSS (prevents a 404 on every page load)"
fi

# Create or update init.js to load the horus plugin
INIT_JS="$OWRX/htdocs/plugins/receiver/init.js"
if [ ! -f "$INIT_JS" ]; then
    echo "Plugins.load('horus');" > "$INIT_JS"
elif ! grep -q "'horus'" "$INIT_JS"; then
    echo "Plugins.load('horus');" >> "$INIT_JS"
fi
info "init.js updated"

# ── Install: REMOVE the legacy openwebrx.js panel-list patch ────────
#
# v3.0.0 and earlier injected 'horus' into the hardcoded panel array in
# htdocs/openwebrx.js. v4.0.0 uses the official plugin JS API
# (Plugins.addWindow) and neither needs nor wants that patch:
#
#   - With 'horus' absent from the panel list, openwebrx.js falls through
#     to secondary_demod_push_data() for any secondary_demod message no
#     built-in panel claims. That is the plugin's SINGLE routing path.
#   - Patching framework source is what made one stray syntax error capable
#     of killing the entire compiled receiver.js bundle (no jQuery, no
#     MessagePanel, no audio, no waterfall).
#
# The un-patch is surgical and version-independent: inside the marker block
# only the ", 'horus'" addition is removed and the two marker comment lines
# are dropped, so the upstream line is restored exactly. Deleting the whole
# marker block instead would orphan the following `return (...)` and `});`
# lines and break the file — see the marker-stripping pitfall in the skill.

JS_FILE="$OWRX/htdocs/openwebrx.js"

if [ -f "$JS_FILE" ]; then
    if grep -q "openwebrx-horus" "$JS_FILE"; then
        backup "$JS_FILE"

        python3 - "$JS_FILE" <<'PYEOF'
import sys

path  = sys.argv[1]
BEGIN = "// openwebrx-horus BEGIN"
END   = "// openwebrx-horus END"

with open(path, 'r') as f:
    lines = f.read().split('\n')

out, in_block, changed = [], False, 0

for line in lines:
    stripped = line.strip()

    if stripped == BEGIN:
        in_block = True
        changed += 1
        continue
    if stripped == END:
        in_block = False
        changed += 1
        continue

    if in_block and "var panels = [" in line and "'horus'" in line:
        line = line.replace(", 'horus'", "", 1)   # restore the upstream line
        changed += 1

    out.append(line)

if changed:
    body = '\n'.join(out)

    # The un-patch is only correct if the upstream panels line came back
    # exactly, with its .map() call intact and no 'horus' left in it.
    panels_line = [l for l in body.split('\n') if 'var panels = [' in l]
    if len(panels_line) != 1:
        print("ERROR: expected exactly one 'var panels = [' line, found %d" % len(panels_line))
        sys.exit(1)
    if "'horus'" in panels_line[0]:
        print("ERROR: 'horus' still present in the panels line; file NOT written")
        sys.exit(1)
    if '.map(' not in panels_line[0]:
        print("ERROR: panels line lost its .map() call; file NOT written")
        sys.exit(1)

    # The case block must still open and close cleanly.
    idx = body.find("case 'secondary_demod'")
    if idx < 0:
        print("ERROR: secondary_demod case not found; file NOT written")
        sys.exit(1)
    blk = body[idx:idx + 2000]
    if '});' not in blk or 'break;' not in blk:
        print("ERROR: secondary_demod case looks malformed; file NOT written")
        sys.exit(1)

    with open(path, 'w') as f:
        f.write(body)
    print("openwebrx.js: removed legacy patch (%d line(s) changed)" % changed)
else:
    print("openwebrx.js: no legacy patch present")
PYEOF

        if [ $? -ne 0 ]; then
            warn "un-patch failed; restoring pristine copy"
            [ -f "$JS_FILE.pre-horus" ] && cp -f "$JS_FILE.pre-horus" "$JS_FILE"
        fi

        # Definitive check where a JS engine is available.
        if command -v node >/dev/null 2>&1; then
            if node --check "$JS_FILE" >/dev/null 2>&1; then
                info "openwebrx.js syntax OK"
            else
                warn "openwebrx.js failed node syntax check — restoring pristine copy"
                [ -f "$JS_FILE.pre-horus" ] && cp -f "$JS_FILE.pre-horus" "$JS_FILE"
            fi
        fi

        if grep -q "'horus'" "$JS_FILE"; then
            warn "openwebrx.js still references 'horus' — inspect $JS_FILE manually"
        else
            info "openwebrx.js restored — v4.0.0 does not patch framework source"
        fi
    else
        # 'horus' in the panel array without marker comments would mean an
        # older/other patcher touched the file. The v4.0.0 plugin defines no
        # $.fn.horusMessagePanel, so a stale entry here WOULD break decoding.
        if grep -q "'horus'" "$JS_FILE"; then
            warn "openwebrx.js contains 'horus' but no marker comments"
            if [ -f "$JS_FILE.pre-horus" ]; then
                cp -f "$JS_FILE.pre-horus" "$JS_FILE"
                info "restored pristine openwebrx.js from $JS_FILE.pre-horus"
            else
                error "cannot clean $JS_FILE — no .pre-horus backup available"
            fi
        else
            info "openwebrx.js is unpatched (correct for v4.0.0)"
        fi
    fi
fi

# ── Install: patch dsp.py ──────────────────────────────────────────

DSP_FILE="$OWRX/owrx/dsp.py"

# The ModulationValidator regex fix is idempotent and MUST run regardless of
# whether the elif branches are already present. A prior partial patch can
# leave the horus_binary elif branches in place while the regex fix is missing
# (the old guard `grep -q 'horus_binary'` skipped the whole block in that case,
# leaving `horus_binary` rejected by the validator → decoder never fires).
# Apply the regex fix unconditionally first.
backup "$DSP_FILE"

python3 - "$DSP_FILE" <<'PYEOF'
import sys

path = sys.argv[1]

with open(path, 'r') as f:
    content = f.read()

# Fix ModulationValidator regex to allow underscores for horus modulations.
# Idempotent: only replaces if the old (underscore-less) pattern is present.
old = '"^[a-z0-9\\-]+$"'
new = '"^[a-z0-9_\\-]+$"'
if old in content:
    content = content.replace(old, new, 1)
    print("regex fix applied")
else:
    print("regex fix already present (or pattern not found)")

with open(path, 'w') as f:
    f.write(content)
PYEOF

# Now add the Horus elif branches to _getSecondaryDemodulator() if not present.
if grep -q 'horus_binary' "$DSP_FILE"; then
    info "dsp.py elif branches already present, skipping"
else
    python3 - "$DSP_FILE" <<'PYEOF'
import sys

path = sys.argv[1]
marker = "# openwebrx-horus"

with open(path, 'r') as f:
    content = f.read()

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
    info "Patched dsp.py (elif branches)"
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
