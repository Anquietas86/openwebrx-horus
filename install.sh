#!/usr/bin/env bash
#
# openwebrx-horus installer (bare metal / systemd)
#
# Installs the Horus balloon telemetry decoder plugin into an existing
# OpenWebRX+ installation. All the real work is done by horus_patch.py, which
# the Docker installer and the Compose override use too. Safe to run twice.
#
# Usage:
#   sudo ./install.sh [/path/to/openwebrx]
#   sudo ./install.sh --uninstall [/path/to/openwebrx]
#
# The path is the directory that holds owrx/ and htdocs/:
#   /usr/lib/python3/dist-packages   Debian/Ubuntu package install
#   /opt/openwebrx                   source checkout (the default)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OWRX="/opt/openwebrx"
UNINSTALL=false

for arg in "$@"; do
    case "$arg" in
        --uninstall) UNINSTALL=true ;;
        -h|--help)   sed -n '2,15p' "$0"; exit 0 ;;
        *)           OWRX="$arg" ;;
    esac
done

# Use the same interpreter OpenWebRX runs under, so horusdemodlib is
# installed where the service can import it.
PYTHON="${PYTHON:-python3}"

if $UNINSTALL; then
    exec "$PYTHON" "$SCRIPT_DIR/horus_patch.py" uninstall "$OWRX"
fi

"$PYTHON" "$SCRIPT_DIR/horus_patch.py" install --pip "$OWRX"

cat <<MSG

  Next steps:
    1. Restart OpenWebRX:  sudo systemctl restart openwebrx
    2. Check the Features page: 'horusdemodlib' should show as available
    3. Add a Horus Binary profile on your 70cm SDR (e.g. 434.200 MHz)
    4. For SondeHub Amateur uploads: Settings > Reporting > Sondehub settings
       (enable it and set your callsign), plus your receiver GPS and altitude

  To uninstall:  sudo $0 --uninstall $OWRX
MSG
