"""
OpenWebRX decoder module for Project Horus high-altitude balloon telemetry.

Handles Horus Binary v1/v2/v3 (4FSK) and legacy RTTY via horusdemodlib.
Decoded positions are plotted on the OpenWebRX map, reported to SondeHub
Amateur, and displayed in a dedicated telemetry panel.
"""

import json
import logging
import threading
from datetime import datetime, timezone

from horusdemodlib.demod import HorusLib, Mode, Frame
from horusdemodlib.decoder import decode_packet
from horusdemodlib.sondehubamateur import SondehubAmateurUploader
from horusdemodlib.utils import telem_to_sondehub

from owrx.config import Config

try:
    from owrx.toolbox import TextParser
except ImportError:
    from owrx.parser import TextParser

try:
    from owrx.map import Map, LatLngLocation
except ImportError:
    Map = None
    LatLngLocation = None

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
    from owrx.bands import Bandplan
except ImportError:
    try:
        from owrx.bandplan import Bandplan
    except ImportError:
        Bandplan = None

logger = logging.getLogger(__name__)

HORUS_MODES = {
    "horus_binary": Mode.BINARY,
    "horus_rtty": Mode.RTTY_7N2,
}

HORUS_SAMPLE_RATE = 48000


class HorusSondehubUploader:
    """
    Singleton manager for the SondeHub Amateur uploader.

    Reads station config from OpenWebRX settings (receiver_gps, receiver_callsign)
    and maintains the background upload thread from horusdemodlib.
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
        self._started = False
        self._init_uploader()

    def _init_uploader(self):
        try:
            pm = Config.get()
            callsign = pm["receiver_callsign"] if "receiver_callsign" in pm else "N0CALL"
            gps = pm["receiver_gps"] if "receiver_gps" in pm else {}
            lat = gps.get("lat", 0.0) if isinstance(gps, dict) else getattr(gps, "lat", 0.0)
            lon = gps.get("lon", 0.0) if isinstance(gps, dict) else getattr(gps, "lon", 0.0)
            alt = gps.get("alt", 0) if isinstance(gps, dict) else getattr(gps, "alt", 0)

            position = (lat, lon, alt) if lat != 0 or lon != 0 else None
            antenna = pm["receiver_antenna"] if "receiver_antenna" in pm else "Unknown"

            self._uploader = SondehubAmateurUploader(
                upload_rate=2,
                user_callsign=str(callsign),
                user_position=position,
                user_radio="OpenWebRX",
                user_antenna=str(antenna),
                software_name="openwebrx-horus",
                software_version="1.0.0",
                inhibit=False,
            )
            self._started = True
            logger.info(
                "SondeHub Amateur uploader started: callsign=%s", callsign
            )
        except Exception:
            logger.exception("Failed to start SondeHub Amateur uploader")
            self._uploader = None

    def upload(self, telemetry: dict):
        if not self._uploader:
            return

        try:
            sondehub_telem = telem_to_sondehub(telemetry)
            if sondehub_telem:
                self._uploader.add(sondehub_telem)
        except Exception:
            logger.debug("SondeHub upload prep failed", exc_info=True)

    def close(self):
        if self._uploader:
            try:
                self._uploader.close()
            except Exception:
                pass
            self._uploader = None
            self._started = False


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

    def __init__(self, mode_str: str = "horus_binary", callback=None, sample_rate: int = None):
        self.mode_str = mode_str
        self.callback = callback
        self._lock = threading.Lock()
        self._dial_freq = None
        self._sample_count = 0

        rate = sample_rate or HORUS_SAMPLE_RATE
        lib_mode = HORUS_MODES.get(mode_str, Mode.BINARY)
        self._demod = HorusLib(
            mode=lib_mode,
            sample_rate=rate,
            stereo_iq=False,
            verbose=True,
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
        self._sample_count += len(audio_bytes) // 2
        if self._sample_count <= len(audio_bytes) // 2:
            logger.info(
                "Horus modem receiving audio: %d bytes (%d int16 samples)",
                len(audio_bytes), len(audio_bytes) // 2,
            )

        with self._lock:
            self._demod.add_samples(audio_bytes)

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

        if self._dial_freq:
            telemetry["f_centre"] = self._dial_freq

        logger.info(
            "Horus decode: %s seq=%s lat=%.5f lon=%.5f alt=%dm SNR=%.1fdB",
            telemetry.get("callsign", "???"),
            telemetry.get("sequence_number"),
            telemetry.get("latitude", 0),
            telemetry.get("longitude", 0),
            telemetry.get("altitude", 0),
            frame.snr,
        )

        try:
            HorusSondehubUploader.getSharedInstance().upload(telemetry)
        except Exception:
            logger.debug("SondeHub upload failed", exc_info=True)

        if self.callback:
            self.callback(telemetry)

        return telemetry

    def close(self):
        with self._lock:
            if self._demod:
                self._demod.close()
                self._demod = None


class HorusParser(TextParser):
    """
    OpenWebRX parser for Horus telemetry.

    Receives decoded telemetry dicts (as JSON bytes from the chain),
    updates the map, reports to the reporting engine, and tracks metrics.
    """

    def __init__(self, service=False):
        self.service = service
        self.band = None
        self.metric = None

    def setDialFrequency(self, frequency: int):
        if Bandplan is not None:
            self.band = Bandplan.getSharedInstance().findBand(frequency)
        self._update_metric()

    def _update_metric(self):
        if Metrics is None or CounterMetric is None:
            return
        band_name = self.band.getName() if self.band else "unknown"
        metric_name = "owrx.horus.decodes.{band}".format(band=band_name)
        metrics = Metrics.getSharedInstance()
        self.metric = metrics.getMetric(metric_name)
        if self.metric is None:
            self.metric = CounterMetric()
            metrics.addMetric(metric_name, self.metric)

    def parse(self, msg: bytes):
        try:
            data = json.loads(msg)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Horus parser: invalid JSON input")
            return None

        if not data.get("crc_ok", False):
            return None

        if self.metric:
            self.metric.inc()

        self.updateMap(data, self.band)

        out = self._build_output(data)

        if ReportingEngine is not None:
            ReportingEngine.getSharedInstance().spot(out)

        if self.service:
            return None
        return out

    def _build_output(self, data: dict) -> dict:
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

        return out

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
