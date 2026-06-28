"""
OpenWebRX DSP chain module for Horus balloon telemetry.

Provides the demodulator chain that slots into OpenWebRX's service framework.
Audio from csdr (NFM demod, 48 kHz, 16-bit signed) is piped into horusdemodlib's
C modem, and decoded telemetry is emitted as pickled dicts for the websocket
handler (matching the AudioChopperDemodulator pattern).
"""

import array
import logging
import math
import pickle
import struct
import threading
import time
from datetime import datetime, timezone

from owrx.horus import HorusDemodulator, HorusParser, HORUS_SAMPLE_RATE, format_horus_telemetry

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
        self._invert_spectrum = True

    def setDialFrequency(self, frequency: int):
        self._demod.setDialFrequency(frequency)
        try:
            from owrx.bands import Bandplan
            self._band = Bandplan.getSharedInstance().findBand(frequency)
        except Exception:
            pass

    def _on_decode(self, telemetry: dict):
        """Called by HorusDemodulator when a valid frame is decoded."""
        if not telemetry:
            return
        if not self._writer:
            logger.warning("Horus decode arrived but no writer connected — frame dropped: %s",
                           telemetry.get("callsign", "???"))
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
        return format_horus_telemetry(data)

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

    @staticmethod
    def _resample_continuous(samples, src_rate, dst_rate, state=None):
        """Resample with phase and last-sample carry across chunks.

        state is (phase, prev_last_sample) or None for the first call.
        Returns (output_samples, new_state).
        """
        if len(samples) < 2:
            last = samples[-1] if samples else 0.0
            return list(samples), (0.0, last)
        if src_rate == dst_rate:
            return list(samples), (0.0, samples[-1])

        step = src_rate / dst_rate
        if state is not None:
            pos, prev_last = state
        else:
            pos, prev_last = 0.0, samples[0]

        out = []
        n = len(samples)
        while pos < n - 1:
            idx = math.floor(pos)
            frac = pos - idx
            if idx >= 0:
                val = samples[idx] * (1.0 - frac) + samples[idx + 1] * frac
            elif idx == -1:
                val = prev_last * (1.0 - frac) + samples[0] * frac
            else:
                pos += step
                continue
            out.append(val)
            pos += step
        new_phase = pos - n
        return out, (new_phase, samples[-1])

    def _run(self):
        configured_rate = self._sample_rate or HORUS_SAMPLE_RATE
        src_rate = configured_rate
        ratio = HORUS_SAMPLE_RATE / max(src_rate, 1)
        logger.info(
            "Horus demod chain started: mode=%s src_rate=%d modem_rate=%d "
            "resample_ratio=%.3f invert=%s",
            self.mode_str, src_rate, HORUS_SAMPLE_RATE, ratio,
            self._invert_spectrum,
        )

        bytes_total = 0
        read_count = 0
        resample_state = None

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
                floats = list(struct.unpack_from("<%df" % n_floats, data))

                if read_count == 1:
                    logger.info(
                        "Horus first audio read: %d bytes (%d float32 samples)",
                        len(data), n_floats,
                    )

                if not self._demod:
                    continue

                if self._invert_spectrum:
                    floats = [-s for s in floats]

                resampled, resample_state = self._resample_continuous(
                    floats, src_rate, HORUS_SAMPLE_RATE, resample_state
                )

                peak = max((abs(s) for s in resampled), default=0.0)
                scale = 30000.0 / peak if peak > 1e-6 else 32767.0
                pcm = array.array("h", (
                    max(-32768, min(32767, int(s * scale)))
                    for s in resampled
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
        logger.info(
            "Horus chain setSampleRate: framework=%d Hz, modem=%d Hz (resample %.3fx)",
            rate, HORUS_SAMPLE_RATE, HORUS_SAMPLE_RATE / max(rate, 1),
        )
        with self._lock:
            if self._demod:
                self._demod.close()
            self._demod = HorusDemodulator(
                mode_str=self.mode_str,
                callback=self._on_decode,
                sample_rate=HORUS_SAMPLE_RATE,
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
