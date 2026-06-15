"""
OpenWebRX DSP chain module for Horus balloon telemetry.

Provides the demodulator chain that slots into OpenWebRX's service framework.
Audio from csdr (NFM demod, 48 kHz, 16-bit signed) is piped into horusdemodlib's
C modem, and decoded telemetry is emitted as pickled dicts for the websocket
handler (matching the AudioChopperDemodulator pattern).
"""

import array
import logging
import pickle
import struct
import threading
import time
from datetime import datetime, timezone

from owrx.horus import HorusDemodulator, HorusParser, HORUS_SAMPLE_RATE

try:
    from csdr.chain import Format
except ImportError:
    Format = None

try:
    from csdr.chain.demodulator import SecondaryDemodulator, FixedAudioRateChain
except ImportError:
    SecondaryDemodulator = None
    FixedAudioRateChain = None

logger = logging.getLogger(__name__)


class HorusDemodulatorChain:
    """
    Demodulator chain that bridges OpenWebRX's audio pipeline to horusdemodlib.

    Receives raw audio bytes from the selector/NFM stage, feeds them to the
    Horus modem, and writes decoded telemetry as pickled dicts to the output
    buffer. The OpenWebRX framework unpickles these and sends them as
    secondary_demod websocket messages to the client.
    """

    def __init__(self, mode_str: str = "horus_binary"):
        self.mode_str = mode_str
        self._writer = None
        self._reader = None
        self._running = False
        self._thread = None
        self._lock = threading.Lock()
        self._band = None
        self._sample_rate = None
        self._demod = None

    def setDialFrequency(self, frequency: int):
        self._demod.setDialFrequency(frequency)
        try:
            from owrx.bands import Bandplan
            self._band = Bandplan.getSharedInstance().findBand(frequency)
        except Exception:
            pass

    def _on_decode(self, telemetry: dict):
        """Called by HorusDemodulator when a valid frame is decoded."""
        if not self._writer or not telemetry:
            return
        try:
            HorusParser.updateMap(telemetry, self._band)
        except Exception:
            logger.debug("Map update failed", exc_info=True)

        try:
            out = self._format_for_frontend(telemetry)
            data = pickle.dumps(out)
            logger.info(
                "Horus writing decoded frame to output: %s (%d bytes pickled)",
                telemetry.get("callsign", "???"), len(data),
            )
            self._writer.write(data)
        except Exception:
            logger.exception("Failed to write decoded Horus telemetry")

    @staticmethod
    def _format_for_frontend(data: dict) -> dict:
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
        for key in ("temperature", "humidity", "battery", "sats",
                    "speed", "ascent_rate", "pressure", "battery_voltage"):
            if key in data:
                out[key] = data[key]
        if data.get("custom_field_names"):
            for name in data["custom_field_names"]:
                if name in data:
                    out[name] = data[name]
            out["custom_field_names"] = data["custom_field_names"]
        return out

    def setReader(self, reader):
        self._reader = reader

    def setWriter(self, writer):
        self._writer = writer
        if self._reader is not None and not self._running:
            self.start()

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="horus-demod"
        )
        self._thread.start()

    def _run(self):
        logger.info("Horus demod chain started: mode=%s sample_rate=%s",
                     self.mode_str, self._sample_rate)
        bytes_total = 0
        samples_total = 0
        read_count = 0
        t_start = time.monotonic()

        while self._running:
            try:
                data = self._reader.read()
                if not data:
                    logger.warning("Horus reader returned empty data, stopping")
                    break
                bytes_total += len(data)
                read_count += 1
                if isinstance(data, memoryview):
                    data = bytes(data)
                n_floats = len(data) // 4
                floats = struct.unpack_from("<%df" % n_floats, data)
                samples_total += n_floats

                if read_count == 1:
                    logger.info(
                        "Horus first audio read: %d bytes (%d float32 samples, type=%s, "
                        "bytes_per_sample=%.1f)",
                        len(data), n_floats, type(data).__name__,
                        len(data) / max(n_floats, 1),
                    )
                elif read_count % 500 == 0:
                    elapsed = time.monotonic() - t_start
                    measured_rate = samples_total / elapsed if elapsed > 0 else 0
                    logger.info(
                        "Horus audio stats: %d reads, %.1f KB, "
                        "configured_rate=%s measured_rate=%.0f Hz",
                        read_count, bytes_total / 1024,
                        self._sample_rate, measured_rate,
                    )

                if read_count <= 3 or read_count % 500 == 0:
                    abs_vals = [abs(s) for s in floats]
                    peak = max(abs_vals) if abs_vals else 0
                    rms = (sum(s * s for s in floats) / max(len(floats), 1)) ** 0.5
                    logger.info(
                        "Horus audio levels: peak=%.6f rms=%.6f n=%d",
                        peak, rms, len(floats),
                    )

                if not self._demod:
                    continue
                pcm = array.array("h", (
                    max(-32768, min(32767, int(s * 32767)))
                    for s in floats
                ))
                self._demod.process(pcm.tobytes())
            except Exception:
                if self._running:
                    logger.exception("Error in Horus demod chain")
                break

        logger.info(
            "Horus demod chain stopped: %d reads, %.1f KB processed",
            read_count, bytes_total / 1024,
        )

    def stop(self):
        self._running = False
        with self._lock:
            if self._demod:
                self._demod.close()
        if self._thread:
            self._thread.join(timeout=2.0)

    def setSampleRate(self, rate):
        self._sample_rate = rate
        logger.info("Horus chain setSampleRate: %d Hz", rate)
        with self._lock:
            if self._demod:
                self._demod.close()
            self._demod = HorusDemodulator(
                mode_str=self.mode_str,
                callback=self._on_decode,
                sample_rate=rate,
            )

    def getFixedAudioRate(self):
        return None

    def getInputFormat(self):
        if Format is not None:
            return Format.FLOAT
        return None

    def isSecondaryDemodulator(self):
        return True

    def isSecondaryFftShown(self):
        return False

    def supportsSquelch(self):
        return False


if SecondaryDemodulator is not None:
    SecondaryDemodulator.register(HorusDemodulatorChain)
