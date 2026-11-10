"""
OpenWebRX DSP chain for Horus balloon telemetry.

Follows the same shape as upstream's own Python-thread decoders (e.g.
SstvDemodulator / SstvParser): a ServiceDemodulator chain declaring a fixed
48 kHz audio rate, wrapping a single ThreadModule that reads float audio and
writes pickled telemetry dicts.

Declaring FixedAudioRateChain is what makes both paths work:
  - Browser (owrx/dsp.py): the selector delivers 48 kHz audio straight to the
    module, so no resampling is needed here.
  - Background service (owrx/service): ServiceDemodulatorChain builds its
    Selector from getFixedAudioRate(); v4.0.x returned None there and crashed.
"""

import logging
import pickle

from csdr.chain.demodulator import ServiceDemodulator, DialFrequencyReceiver
from csdr.module import ThreadModule
from pycsdr.types import Format

from owrx.horus import HorusDemodulator, HorusParser, HORUS_SAMPLE_RATE, format_horus_telemetry

logger = logging.getLogger(__name__)


class HorusModule(ThreadModule):
    """Float audio in, pickled telemetry dicts out (Format.CHAR)."""

    def __init__(self, mode_str: str):
        self.mode_str = mode_str
        self.band = None
        self.demod = HorusDemodulator(
            mode_str=mode_str,
            callback=self._on_decode,
            sample_rate=HORUS_SAMPLE_RATE,
        )
        super().__init__()

    def getInputFormat(self) -> Format:
        return Format.FLOAT

    def getOutputFormat(self) -> Format:
        return Format.CHAR

    def setDialFrequency(self, frequency: int) -> None:
        self.demod.setDialFrequency(frequency)
        try:
            from owrx.bands import Bandplan
            self.band = Bandplan.getSharedInstance().findBand(frequency)
        except Exception:
            self.band = None

    def run(self):
        logger.debug("Horus %s decoder started", self.mode_str)
        while self.doRun:
            data = self.reader.read()
            if data is None:
                break
            try:
                self.demod.process_float(data)
            except Exception:
                logger.exception("Error in Horus demodulator")
        self.demod.close()
        logger.debug("Horus %s decoder stopped", self.mode_str)

    def _on_decode(self, telemetry: dict):
        """Called (from inside process_float) for every CRC-valid frame."""
        try:
            HorusParser.updateMap(telemetry, self.band)
        except Exception:
            logger.debug("Map update failed", exc_info=True)

        writer = self.writer
        if writer is None:
            return
        try:
            writer.write(pickle.dumps(format_horus_telemetry(telemetry)))
        except Exception:
            logger.exception("Failed to write decoded Horus telemetry")


class HorusDemodulatorChain(ServiceDemodulator, DialFrequencyReceiver):
    def __init__(self, mode_str: str = "horus-binary"):
        self.module = HorusModule(mode_str)
        super().__init__([self.module])

    def getFixedAudioRate(self) -> int:
        return HORUS_SAMPLE_RATE

    def setSampleRate(self, sampleRate: int) -> None:
        # The framework honours getFixedAudioRate(), so this is always 48 kHz.
        if sampleRate != HORUS_SAMPLE_RATE:
            logger.warning(
                "Horus chain got %d Hz audio, expected %d Hz; decoding will fail",
                sampleRate, HORUS_SAMPLE_RATE,
            )

    def setDialFrequency(self, frequency: int) -> None:
        self.module.setDialFrequency(frequency)

    def supportsSquelch(self) -> bool:
        return False

    def isSecondaryFftShown(self) -> bool:
        return False
