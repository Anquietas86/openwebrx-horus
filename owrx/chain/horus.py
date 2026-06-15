"""
OpenWebRX DSP chain module for Horus balloon telemetry.

Provides the demodulator chain that slots into OpenWebRX's service framework.
Audio from csdr (NFM demod, 48 kHz, 16-bit signed) is piped into horusdemodlib's
C modem, and decoded telemetry is emitted as JSON for the HorusParser.
"""

import array
import json
import logging
import struct
import threading
from datetime import datetime, timezone

from owrx.horus import HorusDemodulator, HORUS_SAMPLE_RATE

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

    Used as a secondary demodulator in the service framework. Receives raw
    audio bytes from the selector/NFM stage, feeds them to the Horus modem,
    and writes decoded JSON to the output writer.
    """

    def __init__(self, mode_str: str = "horus_binary"):
        self.mode_str = mode_str
        self._writer = None
        self._reader = None
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

        self._demod = HorusDemodulator(
            mode_str=mode_str,
            callback=self._on_decode,
        )

    def setDialFrequency(self, frequency: int):
        self._demod.setDialFrequency(frequency)

    def _on_decode(self, telemetry: dict):
        """Called by HorusDemodulator when a valid frame is decoded."""
        if self._writer and telemetry:
            try:
                out = self._format_for_frontend(telemetry)
                msg = json.dumps(out).encode("utf-8") + b"\n"
                logger.info("Horus writing decoded frame to output: %s", out.get("callsign", "???"))
                self._writer.write(msg)
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
        logger.info("Horus demod chain started: mode=%s", self.mode_str)
        bytes_total = 0
        read_count = 0

        while self._running:
            try:
                data = self._reader.read()
                if not data:
                    logger.warning("Horus reader returned empty data, stopping")
                    break
                bytes_total += len(data)
                read_count += 1
                if read_count == 1:
                    logger.info(
                        "Horus first audio read: %d bytes (type=%s)",
                        len(data), type(data).__name__,
                    )
                elif read_count % 500 == 0:
                    logger.info(
                        "Horus audio stats: %d reads, %.1f KB total",
                        read_count, bytes_total / 1024,
                    )
                if isinstance(data, memoryview):
                    data = bytes(data)
                n_floats = len(data) // 4
                floats = struct.unpack_from("<%df" % n_floats, data)

                if read_count <= 5 or read_count % 500 == 0:
                    abs_vals = [abs(s) for s in floats]
                    peak = max(abs_vals) if abs_vals else 0
                    rms = (sum(s * s for s in floats) / max(len(floats), 1)) ** 0.5
                    logger.info(
                        "Horus audio levels: peak=%.6f rms=%.6f n=%d",
                        peak, rms, len(floats),
                    )

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
        logger.info("Horus chain setSampleRate: %d Hz", rate)

    def getFixedAudioRate(self):
        return HORUS_SAMPLE_RATE

    def getInputFormat(self):
        if Format is not None:
            return Format.SHORT
        return None

    def isSecondaryDemodulator(self):
        return True

    def isSecondaryFftShown(self):
        return False

    def supportsSquelch(self):
        return False


if FixedAudioRateChain is not None:
    FixedAudioRateChain.register(HorusDemodulatorChain)
if SecondaryDemodulator is not None:
    SecondaryDemodulator.register(HorusDemodulatorChain)
