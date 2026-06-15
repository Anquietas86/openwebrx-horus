"""
OpenWebRX DSP chain module for Horus balloon telemetry.

Provides the demodulator chain that slots into OpenWebRX's service framework.
Audio from csdr (NFM demod, 48 kHz, 16-bit signed) is piped into horusdemodlib's
C modem, and decoded telemetry is emitted as JSON for the HorusParser.
"""

import json
import logging
import threading

from owrx.horus import HorusDemodulator, HORUS_SAMPLE_RATE

try:
    from csdr.chain import Format
except ImportError:
    Format = None

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
                msg = json.dumps(telemetry).encode("utf-8") + b"\n"
                self._writer.write(msg)
            except Exception:
                logger.exception("Failed to write decoded Horus telemetry")

    def setReader(self, reader):
        self._reader = reader

    def setWriter(self, writer):
        self._writer = writer

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="horus-demod"
        )
        self._thread.start()

    def _run(self):
        # Read audio in chunks matching the modem's expected input size.
        # 16-bit mono at 48 kHz = 96000 bytes/sec. Read ~100ms chunks.
        chunk_size = HORUS_SAMPLE_RATE * 2 // 10  # 9600 bytes = 100ms
        logger.info("Horus demod chain started: mode=%s", self.mode_str)

        while self._running:
            try:
                data = self._reader.read(chunk_size)
                if not data:
                    break
                self._demod.process(data)
            except Exception:
                if self._running:
                    logger.exception("Error in Horus demod chain")
                break

        logger.info("Horus demod chain stopped")

    def stop(self):
        self._running = False
        with self._lock:
            if self._demod:
                self._demod.close()
        if self._thread:
            self._thread.join(timeout=2.0)

    def setSampleRate(self, rate):
        pass

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
