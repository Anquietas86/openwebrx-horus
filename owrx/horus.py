"""
OpenWebRX decoder module for Project Horus high-altitude balloon telemetry.

Handles Horus Binary v1/v2/v3 (4FSK) and legacy RTTY via horusdemodlib.
Decoded positions are plotted on the OpenWebRX map, shown in the stock
Packet panel (and the optional TELEM window), sent to the reporting engine
(e.g. MQTT), and uploaded to SondeHub Amateur when SondeHub reporting is
enabled in the OpenWebRX settings.
"""

import array
import json
import logging
import threading
import time
from datetime import datetime, timezone

from horusdemodlib.demod import HorusLib, Mode, Frame
from horusdemodlib.decoder import decode_packet
from horusdemodlib.sondehubamateur import SondehubAmateurUploader

try:
    from owrx.config import Config
except ImportError:
    Config = None

try:
    from owrx.map import Map, LatLngLocation
except ImportError:
    Map = None
    # Provide a dummy base class so HorusLocation can be defined
    class LatLngLocation:
        def __init__(self, lat, lon):
            self.lat = lat
            self.lon = lon
        def __dict__(self):
            return {"lat": self.lat, "lon": self.lon}

try:
    from owrx.metrics import Metrics, CounterMetric
except ImportError:
    Metrics = None
    CounterMetric = None

try:
    from owrx.reporting import ReportingEngine
except ImportError:
    ReportingEngine = None

try:
    import numpy
except ImportError:
    numpy = None

try:
    from owrx.bands import Bandplan
except ImportError:
    try:
        from owrx.bandplan import Bandplan
    except ImportError:
        Bandplan = None

logger = logging.getLogger(__name__)

# Mode name -> horusdemodlib modem mode. Names must match [a-z0-9-] (OWRX+'s
# ModulationValidator). horus-rtty keeps the common 7N2 framing; the other
# RTTY framings are separate modes so each profile can pick its payload's.
HORUS_MODES = {
    "horus-binary": Mode.BINARY,
    "horus-rtty": Mode.RTTY_7N2,
    "horus-rtty-7n1": Mode.RTTY_7N1,
    "horus-rtty-8n2": Mode.RTTY_8N2,
}

# Display names, used by the installer when registering the modes.
HORUS_MODE_NAMES = {
    "horus-binary": "Horus Binary",
    "horus-rtty": "Horus RTTY (7N2)",
    "horus-rtty-7n1": "Horus RTTY (7N1)",
    "horus-rtty-8n2": "Horus RTTY (8N2)",
}

# Fixed PCM gain for the C modem. OpenWebRX's USB chain applies AGC, so audio
# arrives at roughly +/-1.0; this maps that to about half of s16 full scale
# and leaves headroom before clipping. A fixed gain (rather than normalising
# every block to its own peak) keeps the level steady across block edges.
AUDIO_GAIN = 16384.0

HORUS_SAMPLE_RATE = 48000

# Keep in step with VERSION in plugin/horus/horus.js and the release tag.
# Sent to SondeHub Amateur as software_version.
PLUGIN_VERSION = "5.1.0"


def format_horus_telemetry(data: dict) -> dict:
    """Format decoded Horus telemetry for frontend display.

    Shared between HorusParser._build_output and
    HorusDemodulatorChain._format_for_frontend.
    """
    out = {
        "mode": "Horus",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    field_map = {
        "callsign": "callsign",
        "latitude": "lat",
        "longitude": "lon",
        "altitude": "altitude",
        "sequence_number": "sequence",
        "snr": "snr",
        "modulation": "modulation",
    }
    for src, dst in field_map.items():
        if src in data:
            out[dst] = data[src]

    sensor_keys = (
        "temperature", "humidity", "battery", "sats",
        "speed", "ascent_rate", "pressure", "battery_voltage",
    )
    for key in sensor_keys:
        if key in data:
            out[key] = data[key]

    if data.get("custom_field_names"):
        for name in data["custom_field_names"]:
            if name in data:
                out[name] = data[name]
        out["custom_field_names"] = data["custom_field_names"]

    # Fields read by the stock OpenWebRX+ Packet panel (PacketMessagePanel),
    # which the frontend plugin teaches to accept mode "Horus". The panel
    # shows UTC | callsign | map link | comment, so everything else is folded
    # into the comment. The map link opens /map?callsign=<source>, matching
    # the key HorusParser.updateMap() plots under.
    if "callsign" in data:
        out["source"] = data["callsign"]
    if _num(data.get("f_centre")) is not None:
        out["freq"] = int(data["f_centre"])
    out["comment"] = _build_comment(data)

    return out


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _build_comment(data: dict) -> str:
    """One-line telemetry summary for the stock Packet panel's comment column."""
    parts = []
    alt = _num(data.get("altitude"))
    if alt is not None:
        parts.append("Alt {:,.0f} m".format(alt))
    asc = _num(data.get("ascent_rate"))
    if asc is not None:
        parts.append("{:+.1f} m/s".format(asc))
    spd = _num(data.get("speed"))
    if spd is not None:
        parts.append("{:.0f} km/h".format(spd))
    snr = _num(data.get("snr"))
    if snr is not None:
        parts.append("SNR {:.1f} dB".format(snr))
    seq = data.get("sequence_number")
    if seq is not None:
        parts.append("#{}".format(seq))

    temp = _num(data.get("temperature"))
    if temp is not None:
        parts.append("{:.1f}°C".format(temp))
    hum = _num(data.get("humidity"))
    if hum is not None:
        parts.append("{:.0f}%RH".format(hum))
    pres = _num(data.get("pressure"))
    if pres is not None:
        parts.append("{:.1f} hPa".format(pres))
    batt = _num(data.get("battery_voltage"))
    if batt is None:
        batt = _num(data.get("battery"))
    if batt is not None:
        parts.append("{:.2f} V".format(batt))
    sats = _num(data.get("sats"))
    if sats is not None:
        parts.append("{:d} sats".format(int(sats)))

    for name in data.get("custom_field_names") or []:
        val = data.get(name)
        if val is None:
            continue
        if isinstance(val, float):
            val = "{:.2f}".format(val)
        parts.append("{}: {}".format(name, val))

    return " · ".join(parts)


class _RecentFrames:
    """Remembers which (callsign, sequence) frames were already handled.

    Every browser tuned to a Horus mode runs its own chain, and so does the
    background decoder, so one transmitted frame can be decoded several times
    by the same receiver. Uploads, spots and metrics should happen once per
    frame; the map and the browser panels still see every copy.
    """

    def __init__(self, ttl: float = 600.0, max_entries: int = 2000):
        self.ttl = ttl
        self.max_entries = max_entries
        self._seen = {}
        self._lock = threading.Lock()

    def first_time(self, telemetry: dict, now: float = None) -> bool:
        callsign = telemetry.get("callsign")
        seq = telemetry.get("sequence_number")
        if callsign is None or seq is None:
            return True
        now = time.monotonic() if now is None else now
        key = (str(callsign), seq)
        with self._lock:
            last = self._seen.get(key)
            if last is not None and now - last < self.ttl:
                return False
            self._seen[key] = now
            if len(self._seen) > self.max_entries:
                cutoff = now - self.ttl
                self._seen = {k: t for k, t in self._seen.items() if t >= cutoff}
            return True


_recent_frames = _RecentFrames()


def is_new_frame(telemetry: dict) -> bool:
    return _recent_frames.first_time(telemetry)


# OpenWebRX+'s own SondeHub settings (Settings > Reporting > "Sondehub
# settings"). Upstream uses them for radiosondes; the plugin honours the same
# switch, callsign and antenna for SondeHub Amateur so there is one place to
# turn SondeHub reporting on or off.
SONDEHUB_CONFIG_KEYS = (
    "sondehub_enabled", "sondehub_callsign", "sondehub_antenna",
    "receiver_gps", "receiver_asl",
)


def _cfg(pm, key, default=None):
    try:
        return pm[key] if key in pm else default
    except Exception:
        return default


def sondehub_station_settings(pm):
    """Uploader settings from the OpenWebRX config, or None when disabled.

    Mirrors upstream's SondehubReporter: reporting needs sondehub_enabled and a
    real sondehub_callsign (empty or N0CALL means off). Altitude comes from
    receiver_asl; receiver_gps only holds lat/lon.
    """
    if not _cfg(pm, "sondehub_enabled", False):
        return None
    callsign = _cfg(pm, "sondehub_callsign")
    callsign = str(callsign).strip() if callsign else ""
    if not callsign or callsign.upper() == "N0CALL":
        return None

    gps = _cfg(pm, "receiver_gps")
    lat = _cfg(gps, "lat", 0.0) if gps is not None else 0.0
    lon = _cfg(gps, "lon", 0.0) if gps is not None else 0.0
    asl = _cfg(pm, "receiver_asl", 0) or 0
    position = (float(lat), float(lon), float(asl)) if (lat or lon) else None

    antenna = _cfg(pm, "sondehub_antenna") or ""
    return {
        "callsign": callsign,
        "position": position,
        "antenna": str(antenna),
    }


class HorusSondehubUploader:
    """
    Singleton manager for the SondeHub Amateur uploader.

    Follows OpenWebRX+'s SondeHub settings (see sondehub_station_settings) and
    rebuilds the horusdemodlib uploader whenever they change, so turning
    reporting on/off or changing the callsign needs no restart.
    """

    _instance = None
    _lock = threading.Lock()

    @classmethod
    def getSharedInstance(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._uploader = None
        self._settings = None
        self._uploader_lock = threading.Lock()
        self._config_sub = None
        if Config is None:
            logger.warning("OpenWebRX Config not available; SondeHub upload disabled")
            return
        self.apply_config()
        try:
            self._config_sub = Config.get().filter(*SONDEHUB_CONFIG_KEYS).wire(self.apply_config)
        except Exception:
            logger.debug("Could not subscribe to SondeHub config changes", exc_info=True)

    def apply_config(self, *args):
        try:
            settings = sondehub_station_settings(Config.get())
        except Exception:
            logger.exception("Failed to read SondeHub settings")
            settings = None

        with self._uploader_lock:
            if settings == self._settings and (settings is None) == (self._uploader is None):
                return
            self._close_uploader()
            self._settings = settings
            if settings is None:
                logger.info(
                    "SondeHub Amateur upload is off (enable it and set a callsign "
                    "under Settings > Reporting > Sondehub settings)"
                )
                return
            try:
                self._uploader = SondehubAmateurUploader(
                    upload_rate=2,
                    user_callsign=settings["callsign"],
                    user_position=settings["position"],
                    user_radio="OpenWebRX",
                    user_antenna=settings["antenna"],
                    software_name="openwebrx-horus",
                    software_version=PLUGIN_VERSION,
                    inhibit=False,
                )
                logger.info("SondeHub Amateur uploader started: callsign=%s", settings["callsign"])
            except Exception:
                logger.exception("Failed to start SondeHub Amateur uploader")
                self._uploader = None

    @property
    def enabled(self) -> bool:
        return self._uploader is not None

    def upload(self, telemetry: dict):
        with self._uploader_lock:
            uploader = self._uploader
        if uploader is None:
            return
        try:
            # SondehubAmateurUploader.add() runs telem_to_sondehub() itself.
            uploader.add(telemetry)
        except Exception:
            logger.debug("SondeHub upload prep failed", exc_info=True)

    def _close_uploader(self):
        if self._uploader is not None:
            try:
                self._uploader.close()
            except Exception:
                pass
            self._uploader = None

    def close(self):
        if self._config_sub is not None:
            try:
                self._config_sub.cancel()
            except Exception:
                pass
            self._config_sub = None
        with self._uploader_lock:
            self._close_uploader()
            self._settings = None
        # Reset singleton so a new instance can be created on next use
        HorusSondehubUploader._instance = None


class HorusLocation(LatLngLocation):
    """Map marker for a Horus balloon payload."""

    def __init__(self, data: dict):
        super().__init__(data["latitude"], data["longitude"])
        self.data = data

    def __dict__(self):
        res = super().__dict__()
        if "altitude" in self.data:
            res["altitude"] = self.data["altitude"]
        if "callsign" in self.data:
            res["comment"] = self.data["callsign"]
        if "sequence_number" in self.data:
            res["sequence"] = self.data["sequence_number"]
        # Add telemetry details for map popup
        extras = []
        if "snr" in self.data:
            extras.append("SNR: %.1f dB" % self.data["snr"])
        if "temperature" in self.data:
            extras.append("Temp: %.1f°C" % self.data["temperature"])
        if "battery_voltage" in self.data:
            extras.append("Batt: %.2fV" % self.data["battery_voltage"])
        elif "battery" in self.data:
            extras.append("Batt: %.2fV" % self.data["battery"])
        if "speed" in self.data:
            extras.append("Speed: %.0f km/h" % self.data["speed"])
        if "ascent_rate" in self.data:
            extras.append("Ascent: %.1f m/s" % self.data["ascent_rate"])
        if "sats" in self.data:
            extras.append("Sats: %d" % self.data["sats"])
        elif "satellites" in self.data:
            extras.append("Sats: %d" % self.data["satellites"])
        if extras:
            res["comment"] = "%s | %s" % (self.data.get("callsign", "???"), " | ".join(extras))
        res["symbol"] = self._get_symbol()
        return res

    def _get_symbol(self):
        # APRS balloon symbol: /O
        return {"table": "/", "symbol": "O"}


class HorusDemodulator:
    """
    Wraps horusdemodlib's C modem as a streaming demodulator.

    Receives 16-bit signed PCM audio at 48 kHz, feeds it to the
    4FSK/RTTY modem, and emits decoded telemetry dicts via callback.
    """

    def __init__(self, mode_str: str = "horus-binary", callback=None, sample_rate: int = None):
        self.mode_str = mode_str
        self.callback = callback
        self._lock = threading.Lock()
        self._dial_freq = None

        rate = sample_rate or HORUS_SAMPLE_RATE
        self.lib_mode = HORUS_MODES.get(mode_str, Mode.BINARY)
        self._demod = HorusLib(
            mode=self.lib_mode,
            sample_rate=rate,
            stereo_iq=False,
            verbose=False,
            callback=self._on_frame,
        )

        logger.info("Horus demodulator initialized: mode=%s rate=%d Hz", mode_str, rate)

    def _on_frame(self, frame: Frame):
        """HorusLib callback — fires for every frame with data."""
        self._handle_frame(frame)

    def setDialFrequency(self, frequency: int):
        self._dial_freq = frequency

    def process(self, audio_bytes: bytes):
        """Feed 16-bit signed PCM audio. Decoded frames arrive via callback."""
        with self._lock:
            if self._demod is None:     # closed while a read was in flight
                return
            self._demod.add_samples(audio_bytes)

    def process_float(self, data) -> None:
        """Feed native-endian float32 audio, as OpenWebRX's audio buffers hold it.

        Converted to s16 PCM for the C modem with a fixed gain (AUDIO_GAIN)
        and hard clipping.
        """
        raw = bytes(data)
        if not raw:
            return
        if numpy is not None:
            floats = numpy.frombuffer(raw, dtype=numpy.float32)
            pcm = numpy.clip(floats * AUDIO_GAIN, -32767, 32767).astype(numpy.int16).tobytes()
        else:
            floats = array.array("f")
            floats.frombytes(raw)
            g = AUDIO_GAIN
            pcm = array.array(
                "h", (int(max(-32767.0, min(32767.0, x * g))) for x in floats)
            ).tobytes()
        self.process(pcm)

    def _tone_offset(self, frame: Frame):
        """Mean of the modem's tone frequency estimates, in Hz of audio.

        With USB, signal frequency = dial frequency + audio offset, which is
        what SondeHub expects as the payload frequency.
        """
        stats = getattr(frame, "extended_stats", None)
        if stats is None:
            return None
        tones = 4 if self.lib_mode == Mode.BINARY else 2
        try:
            est = [float(stats.f_est[i]) for i in range(tones)]
        except Exception:
            return None
        est = [f for f in est if f > 0]
        if len(est) != tones:
            return None
        return sum(est) / tones

    def _handle_frame(self, frame: Frame):
        if not frame.crc_pass:
            logger.debug(
                "Horus frame CRC fail (SNR: %.1f dB)", frame.snr
            )
            return None

        try:
            telemetry = decode_packet(frame.data)
        except Exception:
            logger.warning(
                "Horus CRC OK but decode_packet failed (raw: %s)",
                frame.data.hex() if isinstance(frame.data, bytes) else frame.data,
                exc_info=True,
            )
            return None

        telemetry["snr"] = frame.snr
        telemetry["crc_ok"] = True
        telemetry["mode"] = self.mode_str

        # horusdemodlib's telem_to_sondehub requires a 'time' field.
        # v1 packets don't include it; v2/v3 do. Fall back to UTC now.
        if "time" not in telemetry:
            telemetry["time"] = datetime.now(timezone.utc).strftime("%H:%M:%S")

        if self._dial_freq:
            offset = self._tone_offset(frame)
            telemetry["f_centre"] = int(round(self._dial_freq + (offset or 0)))

        logger.info(
            "Horus decode: %s seq=%s lat=%.5f lon=%.5f alt=%dm SNR=%.1fdB",
            telemetry.get("callsign", "???"),
            telemetry.get("sequence_number"),
            telemetry.get("latitude", 0),
            telemetry.get("longitude", 0),
            telemetry.get("altitude", 0),
            frame.snr,
        )

        if self.callback:
            self.callback(telemetry)

        return telemetry

    def close(self):
        with self._lock:
            if self._demod:
                self._demod.close()
                self._demod = None


class HorusParser:
    """
    Server-side handling of decoded Horus telemetry.

    HorusModule (owrx/chain/horus.py) hands every CRC-valid frame to
    handle(), which plots it on the map and, once per transmitted frame
    across all chains on this receiver, uploads it to SondeHub Amateur,
    counts it in the owrx.horus.decodes.<band> metric and sends it to the
    reporting engine (e.g. MQTT, under <topic>/Horus).
    """

    def __init__(self, service=False):
        # Deliberately not an owrx TextParser: that is a LineBasedModule (a
        # thread with csdr buffers). Decoding already happens in HorusModule,
        # so this is a plain helper it calls directly.
        self.service = service
        self.band = None
        self.metric = None
        self._update_metric()

    def setDialFrequency(self, frequency: int):
        self.band = None
        if Bandplan is not None:
            try:
                self.band = Bandplan.getSharedInstance().findBand(frequency)
            except Exception:
                self.band = None
        self._update_metric()

    def _update_metric(self):
        if Metrics is None or CounterMetric is None:
            return
        try:
            band_name = self.band.getName() if self.band else "unknown"
            metric_name = "horus.decodes.{band}".format(band=band_name)
            metrics = Metrics.getSharedInstance()
            self.metric = metrics.getMetric(metric_name)
            if self.metric is None:
                self.metric = CounterMetric()
                metrics.addMetric(metric_name, self.metric)
        except Exception:
            logger.debug("Horus metric setup failed", exc_info=True)
            self.metric = None

    def handle(self, telemetry: dict) -> dict:
        """Process one decoded frame; returns the dict for the browser."""
        try:
            self.updateMap(telemetry, self.band)
        except Exception:
            logger.debug("Map update failed", exc_info=True)

        out = format_horus_telemetry(telemetry)

        if is_new_frame(telemetry):
            try:
                HorusSondehubUploader.getSharedInstance().upload(telemetry)
            except Exception:
                logger.debug("SondeHub upload failed", exc_info=True)
            if self.metric is not None:
                self.metric.inc()
            if ReportingEngine is not None:
                try:
                    ReportingEngine.getSharedInstance().spot(out)
                except Exception:
                    logger.debug("Reporting spot failed", exc_info=True)
        return out

    def parse(self, msg: bytes):
        """JSON-bytes entry point, kept for anything feeding text frames."""
        try:
            data = json.loads(msg)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Horus parser: invalid JSON input")
            return None
        if not data.get("crc_ok", False):
            return None
        out = self.handle(data)
        return None if self.service else out

    def _build_output(self, data: dict) -> dict:
        return format_horus_telemetry(data)

    @staticmethod
    def updateMap(data: dict, band=None, timestamp=None):
        callsign = data.get("callsign")
        if not callsign:
            return

        lat = data.get("latitude", 0)
        lon = data.get("longitude", 0)
        if lat == 0 and lon == 0:
            return

        loc = HorusLocation(data)
        source = "horus"

        if Map is not None:
            Map.getSharedInstance().updateLocation(
                callsign, loc, source, timestamp=timestamp, band=band
            )
