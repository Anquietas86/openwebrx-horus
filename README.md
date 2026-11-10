# openwebrx-horus

[![CI](https://github.com/Anquietas86/openwebrx-horus/actions/workflows/ci.yml/badge.svg)](https://github.com/Anquietas86/openwebrx-horus/actions/workflows/ci.yml)

OpenWebRX demodulator/decoder plugin for [Project Horus](https://github.com/projecthorus/horusdemodlib) high-altitude balloon telemetry.

Supports Horus Binary v1, v2, and v3 (ASN.1) over 4FSK, plus legacy RTTY
(7N2, 7N1 and 8N2 framing).

## Features

- **4FSK + RTTY demodulation** via horusdemodlib's C modem (CFFI)
- **Auto-detection** of Horus Binary v1, v2, and v3 packet formats
- **SondeHub Amateur upload** — decoded telemetry is uploaded to [SondeHub Amateur](https://amateur.sondehub.org/), controlled by OpenWebRX+'s own SondeHub settings (off until you enable it)
- **Tuning waterfall** — the digimodes panel shows the Horus tones, so you can see the signal sits in the passband before anything decodes
- **Map, metrics, MQTT** — positions go on the OpenWebRX map, decodes are counted per band (`horus.decodes.<band>`), and frames are sent to the reporting engine, so the MQTT reporter publishes them under `<topic>/Horus`
- **Each frame handled once** — when several browsers and the background decoder all decode the same frame, it is uploaded, spotted and counted only once
- **Stock Packet panel** — decoded frames appear in OpenWebRX+'s own Packet panel (UTC, callsign, map link, one-line telemetry summary), like APRS or radiosondes
- **TELEM window** — an optional draggable, resizable floating window with the full table: callsign, sequence, position, altitude, SNR, and sensor data (temperature, humidity, pressure, battery, custom v3 fields)

## Requirements

- [OpenWebRX+](https://github.com/luarvique/openwebrx) (luarvique fork) **1.2.124 or newer**
- Python 3.9+
- `horusdemodlib` (`pip install horusdemodlib`)

**Why 1.2.124+?** v4.0.0 builds its telemetry window on the official plugin JS API
(`Plugins.addButton` / `Plugins.addWindow` / `Plugins.toggleWindow`), which was added
in 1.2.124. On older releases the plugin loads but no window appears. Earlier releases
are still supported by the v3.0.0 tag.

## Installation

All three install methods run the same installer, `horus_patch.py`. It copies the
plugin files, patches `owrx/feature.py`, `modes.py`, `service/__init__.py` and
`dsp.py` between `# openwebrx-horus BEGIN/END` markers, and registers the frontend
plugin. It:

- only rewrites a file whose content actually changes, so re-running it is a no-op
- backs every file up to `<file>.pre-horus` before the first change
- checks every patch landed and still compiles, and **fails loudly** if one didn't
  (e.g. an OpenWebRX+ update moved something), instead of silently doing nothing
- patches **no framework JavaScript**; it only cleans up what v3.x installs left behind

### Bare metal / systemd

```bash
git clone https://github.com/Anquietas86/openwebrx-horus.git
cd openwebrx-horus
sudo ./install.sh /usr/lib/python3/dist-packages   # Debian/Ubuntu package layout
sudo systemctl restart openwebrx
```

Use your actual OpenWebRX path: `/usr/lib/python3/dist-packages` for a Debian
package install, or the checkout directory (default `/opt/openwebrx`) for a source
install.

`horusdemodlib` is pip-installed **system-wide** if it is missing (with
`--break-system-packages` on Debian 12+), so the `openwebrx` service user can import
it. If pip fails, the installer stops and shows the error.

To uninstall:

```bash
sudo ./install.sh --uninstall /usr/lib/python3/dist-packages
sudo systemctl restart openwebrx
```

### Docker

```bash
git clone https://github.com/Anquietas86/openwebrx-horus.git
cd openwebrx-horus
./install-docker.sh openwebrx          # your container name
docker restart openwebrx
```

Everything is installed **inside the container**, so it is lost when the container
is recreated (image update, `docker compose up --force-recreate`). Re-run the script
afterwards, or use the Compose override below to reinstall on every start.

(v5.0.0 and earlier also took a host plugins path as a second argument. It was never
used for installing and is now ignored.)

To uninstall: `./install-docker.sh --uninstall openwebrx`, then restart the container.

### Docker Compose

Have the plugin install itself on every container start, so image updates need
nothing from you:

1. Clone this repo alongside your `docker-compose.yml`:
   ```bash
   git clone https://github.com/Anquietas86/openwebrx-horus.git
   ```

2. Copy the example override:
   ```bash
   cp openwebrx-horus/docker-compose.override.example.yml docker-compose.override.yml
   ```

3. Change the service name in `docker-compose.override.yml` to match yours, then:
   ```bash
   docker compose up -d
   ```

The override mounts the repo read-only and runs `horus_patch.py install --pip` before
handing off to OpenWebRX. If the install fails, OpenWebRX still starts (without
Horus) and the error is in `docker compose logs`. To update the plugin:
`git -C openwebrx-horus pull && docker compose restart`.

Overrides copied from v5.0.0 or earlier still work (`docker-patch.py` forwards to
`horus_patch.py`), but the new one is shorter and reports failures.

### Manual install

There are no hand-written patch files any more (the old `patches/` directory had
drifted out of date). To see exactly what changes, run the installer on a scratch
copy and diff it:

```bash
cp -r /usr/lib/python3/dist-packages/{owrx,htdocs} /tmp/owrx-copy/
python3 horus_patch.py install /tmp/owrx-copy
diff -ru /usr/lib/python3/dist-packages/owrx /tmp/owrx-copy/owrx
```

### Upgrading to v5.1.0

- **SondeHub uploads now follow OpenWebRX+'s SondeHub settings and are off until you
  turn them on.** Go to **Settings > Reporting > Sondehub settings**, tick *Enable
  Sondehub telemetry reporting* and set the *Uploader callsign* (and antenna). The
  same switch also controls OpenWebRX+'s own radiosonde uploads. v5.0.0 and earlier
  always uploaded, using `receiver_callsign`, falling back to `N0CALL`.
- Your listener altitude now comes from the receiver altitude setting
  (`receiver_asl`). It was always sent as 0 before.
- New modes **Horus RTTY (7N1)** (`horus-rtty-7n1`) and **Horus RTTY (8N2)**
  (`horus-rtty-8n2`). `horus-rtty` stays 7N2.

### Upgrading to v5.0.0 (from v4.0.2 or earlier)

The modes were renamed from `horus_binary` / `horus_rtty` to **`horus-binary` /
`horus-rtty`**. OpenWebRX+ only accepts `[a-z0-9-]` in mode names, so the old
underscore names were rejected when picked in the browser (the old installers'
attempt to widen that check never actually applied). The installer removes the
old patch blocks automatically. **Re-select the Horus mode in any SDR profile,
bookmark or background-decoding config** that used the old names.

### Post-install setup

1. Open the OpenWebRX **Features** page and confirm `horusdemodlib` shows as available
2. Set your receiver **GPS position** and **altitude** in Settings
3. For SondeHub Amateur uploads: **Settings > Reporting > Sondehub settings**, enable
   it and set your callsign
4. Add a **Horus Binary** profile on your 70cm SDR source (e.g. 434.200 MHz)

## Architecture

```
RF → csdr (tuning/filtering) → USB demod + AGC → 48 kHz float audio
    → HorusModule (ThreadModule) → s16 PCM
    → HorusLib (C 4FSK modem via CFFI) → raw frames
    → decode_packet() → telemetry dict → HorusParser.handle()
    ├→ OpenWebRX map (server side, on the /map window)
    ├→ Stock Packet panel + optional TELEM window (browser only)
    └→ once per frame, across all chains on the receiver:
         ├→ SondeHub Amateur (if enabled in Settings > Reporting)
         ├→ horus.decodes.<band> metric
         └→ ReportingEngine.spot() (e.g. MQTT, topic <topic>/Horus)
```

### Decoder chain

`owrx/chain/horus.py` follows the same pattern as upstream's own Python-thread
decoders (SSTV, Fax): a `ServiceDemodulator` chain that declares a fixed 48 kHz audio
rate via `getFixedAudioRate()`, wrapping one `ThreadModule` that reads float audio and
writes pickled telemetry dicts. The fixed rate is what lets OpenWebRX+ hand the modem
48 kHz audio directly, in the browser and as a background service. v4.0.x returned
`None` there, which crashed background decoding and forced a hand-rolled Python
resampler on the browser path.

### Frontend

OpenWebRX+ has no plugin API for registering a new *message panel* (the stock ones
are hardcoded in `index.html`, `openwebrx.js` and `DemodulatorPanel.updatePanels()`).
But the stock **Packet panel** already renders `UTC | callsign | map link | comment`
rows, so the plugin reuses it by wrapping three prototype methods at runtime (no
source patching):

- `PacketMessagePanel.prototype.supportsMessage` also claims messages with mode `Horus`
- `DemodulatorPanel.prototype.updatePanels` also shows the Packet panel while a
  `horus-*` mode is selected
- `Utils.linkifyByMode` links Horus callsigns to SondeHub Amateur

The server fills in `source` and a one-line `comment` for each frame. The position
pin opens the real OpenWebRX map at the balloon's marker.

The **TELEM window** is a second, optional view built with `Plugins.addWindow()`
(OpenWebRX+ 1.2.124+). It is fed from the same Packet panel hook, so frames still
arrive exactly once. If the stock panel turns out to be enough, the window can be
removed without touching the panel integration.

The "digimodes" panel shows the secondary FFT for Horus modes (`secondaryFft=True`
and `isSecondaryFftShown()`): a waterfall of the passband, where the four 4FSK tones
(or the two RTTY tones) should sit. (v4.0.x hid this panel by wrapping `toggle_panel`
globally, which also hid the ISM and digimodes panels for every other mode; v5.0.0
switched it off. It now shows something useful instead of an empty box.)

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

The remaining Python patches are unavoidable — they are how *any* OpenWebRX plugin
registers a decoder (feature detection, mode definition, DSP chain wiring, service
dispatch).

## Tests

```bash
pip install horusdemodlib pytest setuptools
python -m pytest tests/                      # decoder, formatting, SondeHub settings

# Also run the chain and the installer against OpenWebRX+'s real code:
git clone --depth 1 https://github.com/luarvique/openwebrx /tmp/owrx
OWRX_SRC=/tmp/owrx python -m pytest tests/
```

- `tests/test_chain.py` feeds a loopback-encoded Horus packet through the real chain
  classes (pycsdr's C buffers are stubbed when it isn't installed) and checks the
  contract `owrx/dsp.py` and `owrx/service` rely on.
- `tests/test_installer.py` installs into a copy of upstream, checks every patch
  landed and compiles, that a second run changes nothing, and that uninstall gives
  back upstream's files byte for byte.

GitHub Actions runs all of it on every push and **nightly against the latest
OpenWebRX+ master**, so an upstream change that breaks the patches shows up as a red
build rather than as a silent failure on launch day.

## SondeHub Amateur Integration

Uploads use OpenWebRX+'s own SondeHub settings (**Settings > Reporting > Sondehub
settings**), the same ones its radiosonde decoders use:

- `sondehub_enabled`: uploads happen only when this is ticked
- `sondehub_callsign`: your uploader callsign (empty or `N0CALL` means off)
- `sondehub_antenna`: your antenna description
- `receiver_gps` + `receiver_asl`: your listener position and altitude

Changes apply immediately, with no restart. Telemetry is batched and uploaded every
2 seconds, with the payload frequency worked out as dial frequency plus the modem's
measured tone centre. Each frame is uploaded once, even when several browsers and the
background decoder all decode it. No API key needed. Decoded balloons appear on the
[SondeHub Amateur Tracker](https://amateur.sondehub.org/).

## TELEM Window

Open it with the `TELEM` button. A live scrolling table with columns:

| UTC | Callsign | Seq | Position | Alt (m) | SNR | Sensors |
|-----|----------|-----|----------|---------|-----|---------|
| 12:34:56 | VK5ARG | 42 | 34.9285S 138.6007E | 30,000 m | 12.5 dB | 23.5°C \| 3.70V \| 8 sats |

- Callsigns link to the SondeHub Amateur tracker filtered to that payload
- Positions link to Google Maps
- Sensor data includes all standard fields plus v3 custom fields
- Auto-scrolls and prunes to 200 rows for performance
- Drag by the title bar, resize from the corner, close with ✕ — position and size persist across reloads
- A status line shows the running frame count and the last payload/callsign received
- `Clear` empties the table and resets the frame counter

> **Where's the map?** This plugin does not plot on the map itself, and never
> did on OpenWebRX+ 1.2.125. The MAP button opens `/map` in a **separate
> browsing context** (`<a href="map" target="openwebrx-map">`), so neither
> Leaflet nor the map instance exists on the page the plugin runs in — the old
> plotting code was dead and failed silently, so it was removed in v4.0.2.
>
> Balloon positions still appear there: the **server-side** parser calls
> `Map.getSharedInstance().updateLocation(...)` on every fix, so the balloon is
> plotted on the `/map` window (and on
> [SondeHub Amateur](https://amateur.sondehub.org/)) with no help from the
> frontend.

## License

[AGPL-3.0](LICENSE), the same as OpenWebRX+, which this plugin patches and runs inside.
