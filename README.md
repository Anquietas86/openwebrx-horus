# openwebrx-horus

OpenWebRX demodulator/decoder plugin for [Project Horus](https://github.com/projecthorus/horusdemodlib) high-altitude balloon telemetry.

Supports Horus Binary v1, v2, and v3 (ASN.1) over 4FSK, plus legacy RTTY.

## Features

- **4FSK + RTTY demodulation** via horusdemodlib's C modem (CFFI)
- **Auto-detection** of Horus Binary v1, v2, and v3 packet formats
- **Map plotting** with balloon markers and telemetry popups
- **SondeHub Amateur upload** — decoded telemetry is automatically uploaded to [SondeHub Amateur](https://amateur.sondehub.org/) using your OpenWebRX station callsign and position
- **Telemetry panel** — live table showing callsign, position, altitude, SNR, and sensor data (temperature, humidity, pressure, battery, custom v3 fields) alongside the waterfall
- **Metrics** — decode counts tracked per band

## Requirements

- [OpenWebRX+](https://github.com/luarvique/openwebrx) (luarvique fork)
- Python 3.9+
- `horusdemodlib` (`pip install horusdemodlib`)

## Installation

### Quick install (recommended)

```bash
git clone https://github.com/Anquietas86/openwebrx-horus.git
cd openwebrx-horus
chmod +x install.sh
sudo ./install.sh /opt/openwebrx
sudo systemctl restart openwebrx
```

The installer will:
- Install `horusdemodlib` via pip if not already present
- Copy all plugin files (Python modules + frontend) into your OpenWebRX installation
- Patch the 5 OpenWebRX files needed to register the decoder
- Back up every file it modifies (`.pre-horus` suffix)

The script is idempotent — safe to run more than once.

Replace `/opt/openwebrx` with your actual OpenWebRX path if different.

### Uninstall

```bash
sudo ./install.sh --uninstall /opt/openwebrx
sudo systemctl restart openwebrx
```

This removes all plugin files and cleanly strips the patched blocks from OpenWebRX's source files.

### Manual install

If you prefer to patch by hand, see the `patches/` directory for the exact changes needed to:
- `owrx/feature.py` — add horusdemodlib feature detection
- `owrx/modes.py` — add Horus mode definitions
- `owrx/service/__init__.py` — wire up the demodulator chain and parser
- `htdocs/index.html` — add panel div, CSS, and JS includes
- `htdocs/openwebrx.js` — register the panel in the message routing

### Post-install setup

1. Open the OpenWebRX **Features** page and confirm `horusdemodlib` shows as available
2. Set your **receiver callsign** and **GPS position** in Settings (used for SondeHub uploads)
3. Add a **Horus Binary** profile on your 70cm SDR source (e.g. 434.200 MHz)

## Architecture

```
RF → csdr (tuning/filtering) → NFM demod → 48kHz 16-bit PCM
    → HorusLib (C 4FSK modem via CFFI) → raw frames
    → decode_packet() → telemetry dict
    ├→ OpenWebRX map (balloon marker)
    ├→ Telemetry panel (live table)
    ├→ SondeHub Amateur (automatic upload)
    └→ ReportingEngine (OpenWebRX spots)
```

## SondeHub Amateur Integration

The uploader reads your station details from OpenWebRX's config:
- `receiver_callsign` — your amateur callsign (sent as the listener callsign)
- `receiver_gps` — your station lat/lon/alt (sent as listener position)
- `receiver_antenna` — your antenna description

Telemetry is batched and uploaded every 2 seconds. No API key needed — SondeHub Amateur is a free community service. Decoded balloons will appear on the [SondeHub Amateur Tracker](https://amateur.sondehub.org/).

## Telemetry Panel

The panel displays a live scrolling table with columns:

| UTC | Callsign | Seq | Position | Alt (m) | SNR | Sensors |
|-----|----------|-----|----------|---------|-----|---------|
| 12:34:56 | VK5ARG | 42 | 34.9285S 138.6007E | 30,000 m | 12.5 dB | 23.5°C \| 3.70V \| 8 sats |

- Callsigns link to the SondeHub Amateur tracker filtered to that payload
- Positions link to Google Maps
- Sensor data includes all standard fields plus v3 custom fields
- Auto-scrolls and prunes to 200 rows for performance
