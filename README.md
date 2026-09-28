# openwebrx-horus

OpenWebRX demodulator/decoder plugin for [Project Horus](https://github.com/projecthorus/horusdemodlib) high-altitude balloon telemetry.

Supports Horus Binary v1, v2, and v3 (ASN.1) over 4FSK, plus legacy RTTY.

## Features

- **4FSK + RTTY demodulation** via horusdemodlib's C modem (CFFI)
- **Auto-detection** of Horus Binary v1, v2, and v3 packet formats
- **Map plotting** with balloon markers and telemetry popups
- **SondeHub Amateur upload** — decoded telemetry is automatically uploaded to [SondeHub Amateur](https://amateur.sondehub.org/) using your OpenWebRX station callsign and position
- **Telemetry panel** — a draggable, resizable floating window showing callsign, position, altitude, SNR, and sensor data (temperature, humidity, pressure, battery, custom v3 fields)
- **Metrics** — decode counts tracked per band

## Requirements

- [OpenWebRX+](https://github.com/luarvique/openwebrx) (luarvique fork) **1.2.124 or newer**
- Python 3.9+
- `horusdemodlib` (`pip install horusdemodlib`)

**Why 1.2.124+?** v4.0.0 builds its telemetry window on the official plugin JS API
(`Plugins.addButton` / `Plugins.addWindow` / `Plugins.toggleWindow`), which was added
in 1.2.124. On older releases the plugin loads but no window appears. Earlier releases
are still supported by the v3.0.0 tag.

## Installation

### Bare metal / systemd

```bash
git clone https://github.com/Anquietas86/openwebrx-horus.git
cd openwebrx-horus
chmod +x install.sh
sudo ./install.sh /usr/lib/python3/dist-packages   # Debian/Ubuntu package layout
sudo systemctl restart openwebrx
```

The installer will:
- Install `horusdemodlib` via pip if not already present
- Copy all plugin files (Python modules + frontend) into your OpenWebRX installation
- Patch the 4 OpenWebRX Python files needed to register the decoder
- Back up every file it modifies (`.pre-horus` suffix)

The script is idempotent — safe to run more than once. Use your actual OpenWebRX
path: `/opt/openwebrx` for a source checkout, or `/usr/lib/python3/dist-packages`
for a Debian package install.

On upgrade from v3.x the installer also **removes the old `htdocs/openwebrx.js`
patch** and restores the pristine upstream file — see *Framework patching* below.

To uninstall:

```bash
sudo ./install.sh --uninstall /usr/lib/python3/dist-packages
sudo systemctl restart openwebrx
```

### Docker

```bash
git clone https://github.com/Anquietas86/openwebrx-horus.git
cd openwebrx-horus
chmod +x install-docker.sh
sudo ./install-docker.sh openwebrx /opt/openwebrx/plugins
docker restart openwebrx
```

Replace `openwebrx` with your container name and `/opt/openwebrx/plugins` with your host plugins volume path.

The Docker installer handles these layers — all inside the container, so all of them
must be restored after a rebuild:

| What | Where | Persists across rebuild? |
|------|-------|--------------------------|
| Frontend plugin (JS/CSS) | `htdocs/plugins/receiver/horus/` | No — re-run installer |
| Plugin `init.js` registration | `htdocs/plugins/receiver/init.js` | No — re-run installer |
| Python decoder modules | `owrx/horus.py`, `owrx/chain/horus.py` | No — re-run installer |
| Python source patches (4 files) | `owrx/feature.py`, `modes.py`, `service/__init__.py`, `dsp.py` | No — re-run installer |
| `horusdemodlib` pip package | Container site-packages | No — re-run installer |

Like the systemd installer, it patches **no framework JavaScript**. On a container that
previously ran v3.x it additionally *restores* the frontend: `htdocs/openwebrx.js` goes
back to upstream (stale `'horus'` panel entry removed), the old v3.x patches are stripped
from `htdocs/plugins.js` and `htdocs/index.html`, and `htdocs/css/custom.css` is created
if missing to stop a 404 on every page load.

After a container rebuild or image update, re-run `install-docker.sh` to restore everything. For automatic re-install on every start, use the Docker Compose override below.

To uninstall:

```bash
sudo ./install-docker.sh --uninstall openwebrx /opt/openwebrx/plugins
docker restart openwebrx
```

Uninstall removes the decoder modules and **restores the framework frontend to
upstream**. It deliberately does not just delete patch markers: for `openwebrx.js` the
v3.x marker block wrapped the `var panels = ...` line, so deleting it orphaned the
trailing `panels.push()` dispatch and left a bundle that threw on every load.

### Docker Compose

If you run OpenWebRX via docker-compose, you can have the plugin auto-install on every container start — no need to re-run the installer after image updates.

1. Clone this repo alongside your `docker-compose.yml`:
   ```bash
   git clone https://github.com/Anquietas86/openwebrx-horus.git
   ```

2. Copy the example override:
   ```bash
   cp openwebrx-horus/docker-compose.override.example.yml docker-compose.override.yml
   ```

3. Edit `docker-compose.override.yml` to match your volume paths, then:
   ```bash
   docker-compose up -d
   ```

The override mounts the repo into the container (read-only) and runs the patch script on every startup before handing off to OpenWebRX. Everything — pip install, file copies, source patches — happens automatically.

### Manual install

If you prefer to patch by hand, see the `patches/` directory for the exact changes needed to:
- `owrx/feature.py` — add horusdemodlib feature detection
- `owrx/modes.py` — add Horus mode definitions
- `owrx/service/__init__.py` — wire up the demodulator chain and parser
- `owrx/dsp.py` — fix ModulationValidator regex for underscore mode names

The frontend needs no source patching: copy `plugin/horus/` to
`htdocs/plugins/receiver/horus/` and add `Plugins.load('horus');` to
`htdocs/plugins/receiver/init.js`.

### Post-install setup

1. Open the OpenWebRX **Features** page and confirm `horusdemodlib` shows as available
2. Set your **receiver callsign** and **GPS position** in Settings (used for SondeHub uploads)
3. Add a **Horus Binary** profile on your 70cm SDR source (e.g. 434.200 MHz)

## Architecture

```
RF → csdr (tuning/filtering) → NFM demod → 48kHz 16-bit PCM
    → HorusLib (C 4FSK modem via CFFI) → raw frames
    → decode_packet() → telemetry dict
    ├→ OpenWebRX map (balloon marker + flight path)
    ├→ Telemetry window (official plugin API floating window)
    ├→ SondeHub Amateur (automatic upload)
    └→ ReportingEngine (OpenWebRX spots)
```

### Frontend (v4.0.0)

The telemetry display is a floating window created with `Plugins.addWindow()`, part
of the official plugin JS API in OpenWebRX+ 1.2.124+. The API handles dragging,
resizing, closing and position/size persistence (localStorage) — none of which the
plugin has to implement. A `TELEM` button added with `Plugins.addButton()` shows and
hides the window.

Message routing is a **single path**. `openwebrx.js` dispatches a `secondary_demod`
message by offering it to each panel in a hardcoded list; when no panel claims it, it
falls through to `secondary_demod_push_data()`. Since `horus` is deliberately *not* in
that list, the plugin's hook on `secondary_demod_push_data` is the only consumer — so
frames arrive exactly once.

### Framework patching

**v4.0.0 patches no framework JavaScript.** This is the main structural change from
v3.x.

v3.0.0 (and earlier) injected `'horus'` into the hardcoded panel array inside
`htdocs/openwebrx.js`. Because OpenWebRX+ serves its scripts as a single concatenated
bundle (`/compiled/receiver.js`), a single syntax error in that patched file would kill
jQuery, `MessagePanel` and every panel — the page loaded but nothing worked. The patch
also had to be re-applied and re-verified after every OpenWebRX+ upgrade.

v4.0.0 needs no such patch. Both the systemd and Docker installers actively *remove* the
old one, restoring the pristine upstream file — and both refuse to write the file if the
rebuilt panel list fails validation, because a malformed line here breaks the whole bundle.

The 4 remaining Python patches are unavoidable — they are how *any* OpenWebRX plugin
registers a decoder (feature detection, mode definition, DSP chain wiring, service
dispatch).

## SondeHub Amateur Integration

The uploader reads your station details from OpenWebRX's config:
- `receiver_callsign` — your amateur callsign (sent as the listener callsign)
- `receiver_gps` — your station lat/lon/alt (sent as listener position)
- `receiver_antenna` — your antenna description

Telemetry is batched and uploaded every 2 seconds. No API key needed — SondeHub Amateur is a free community service. Decoded balloons will appear on the [SondeHub Amateur Tracker](https://amateur.sondehub.org/).

## Telemetry Window

A live scrolling table with columns:

| UTC | Callsign | Seq | Position | Alt (m) | SNR | Sensors |
|-----|----------|-----|----------|---------|-----|---------|
| 12:34:56 | VK5ARG | 42 | 34.9285S 138.6007E | 30,000 m | 12.5 dB | 23.5°C \| 3.70V \| 8 sats |

- Callsigns link to the SondeHub Amateur tracker filtered to that payload
- Positions link to Google Maps
- Sensor data includes all standard fields plus v3 custom fields
- Auto-scrolls and prunes to 200 rows for performance
- Drag by the title bar, resize from the corner, close with ✕ — position and size persist across reloads
- The window opens automatically on the first decoded frame, then respects your choice; use the `TELEM` button to reopen it
- A status line shows the running frame count and the last payload/callsign received
- `Clear` empties the table and removes the map path and marker
