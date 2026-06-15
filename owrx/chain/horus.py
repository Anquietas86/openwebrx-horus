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

    DUMP_AUDIO = True
    DUMP_SECONDS = 15
    DUMP_PATH_12K = "/tmp/horus_audio_12k.raw"
    DUMP_PATH_48K = "/tmp/horus_audio_48k.raw"

    TEST_INJECT = True

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
        self._invert_spectrum = False
        self._dump_12k = None
        self._dump_48k = None
        self._dump_samples = 0

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
            "resample_ratio=%.3f invert=%s test_inject=%s",
            self.mode_str, src_rate, HORUS_SAMPLE_RATE, ratio,
            self._invert_spectrum, self.TEST_INJECT,
        )

        if self.TEST_INJECT:
            self._run_test_inject(src_rate)
            return

        bytes_total = 0
        samples_total = 0
        read_count = 0
        t_start = time.monotonic()
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
                samples_total += n_floats

                if read_count == 1:
                    logger.info(
                        "Horus first audio read: %d bytes (%d float32 samples)",
                        len(data), n_floats,
                    )
                elif read_count % 500 == 0:
                    elapsed = time.monotonic() - t_start
                    measured = samples_total / elapsed if elapsed > 0 else 0
                    logger.info(
                        "Horus audio stats: %d reads, %.1f KB, "
                        "configured_rate=%d measured_throughput=%.0f Hz "
                        "resample_ratio=%.3f",
                        read_count, bytes_total / 1024,
                        src_rate, measured, ratio,
                    )

                if not self._demod:
                    continue

                if self._invert_spectrum:
                    floats = [-s for s in floats]

                # Diagnostic audio dump
                if self.DUMP_AUDIO and self._dump_samples < self.DUMP_SECONDS * src_rate:
                    if self._dump_12k is None:
                        self._dump_12k = open(self.DUMP_PATH_12K, "wb")
                        self._dump_48k = open(self.DUMP_PATH_48K, "wb")
                        logger.info("Horus audio dump started: %s, %s",
                                    self.DUMP_PATH_12K, self.DUMP_PATH_48K)
                    self._dump_12k.write(struct.pack("<%df" % len(floats), *floats))
                    self._dump_samples += len(floats)
                elif self._dump_12k is not None:
                    self._dump_12k.close()
                    self._dump_12k = None
                    logger.info("Horus 12k audio dump complete: %d samples (%.1fs)",
                                self._dump_samples, self._dump_samples / src_rate)

                resampled, resample_state = self._resample_continuous(
                    floats, src_rate, HORUS_SAMPLE_RATE, resample_state
                )

                # Dump resampled audio
                if self.DUMP_AUDIO and self._dump_48k is not None and not self._dump_48k.closed:
                    self._dump_48k.write(struct.pack("<%df" % len(resampled), *resampled))

                peak = max((abs(s) for s in resampled), default=0.0)
                if peak > 1e-6:
                    scale = 30000.0 / peak
                else:
                    scale = 32767.0
                if read_count <= 3:
                    logger.info(
                        "Horus int16 scaling: peak=%.6f scale=%.1f "
                        "in=%d out=%d samples",
                        peak, scale, len(floats), len(resampled),
                    )
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

    @staticmethod
    def _generate_test_audio(src_rate, num_packets=10):
        """Generate known-good 4FSK audio in memory at src_rate Hz."""
        from horusdemodlib.encoder import Encoder

        center_freq = 1500
        tone_spacing = 270
        symbol_rate = 100
        tone_freqs = [
            center_freq - 1.5 * tone_spacing,
            center_freq - 0.5 * tone_spacing,
            center_freq + 0.5 * tone_spacing,
            center_freq + 1.5 * tone_spacing,
        ]

        def build_payload(seq):
            payload = bytearray(22)
            payload[0] = 1
            struct.pack_into("<H", payload, 1, seq & 0xFFFF)
            payload[3], payload[4], payload[5] = 12, 34, 56
            struct.pack_into("<f", payload, 6, -34.9285)
            struct.pack_into("<f", payload, 10, 138.6007)
            struct.pack_into("<H", payload, 14, 25000)
            payload[16], payload[17], payload[18], payload[19] = 5, 10, 236, 37
            crc = 0xFFFF
            for byte in payload[:20]:
                crc ^= byte << 8
                for _ in range(8):
                    crc = ((crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1) & 0xFFFF
            struct.pack_into("<H", payload, 20, crc)
            return bytes(payload)

        enc = Encoder()
        samples = [0.0] * int(2.0 * src_rate)
        two_pi = 2.0 * math.pi

        for seq in range(num_packets):
            payload = build_payload(seq)
            encoded, _ = enc.horus_l2_encode_packet(payload)
            symbols = enc.bytes_to_4fsk_symbols(encoded)

            phase = 0.0
            sps = src_rate / symbol_rate
            for sym in symbols:
                freq = tone_freqs[sym]
                n = int(round(sps))
                for j in range(n):
                    samples.append(0.8 * math.sin(phase))
                    phase += two_pi * freq / src_rate
                phase %= two_pi
            samples.extend([0.0] * int(2.0 * src_rate))

        enc.close()
        logger.info("Test inject: generated %d packets, %d samples (%.1fs) at %d Hz",
                     num_packets, len(samples), len(samples) / src_rate, src_rate)
        return samples

    def _run_test_inject(self, src_rate):
        """Generate test audio in memory and feed through the full chain."""
        all_floats = self._generate_test_audio(src_rate)

        chunk_size = int(src_rate * 0.063)
        resample_state = None
        offset = 0
        chunk_count = 0

        while offset < len(all_floats) and self._running:
            end = min(offset + chunk_size, len(all_floats))
            floats = all_floats[offset:end]
            offset = end
            chunk_count += 1

            if not self._demod:
                continue

            resampled, resample_state = self._resample_continuous(
                floats, src_rate, HORUS_SAMPLE_RATE, resample_state
            )

            peak = max((abs(s) for s in resampled), default=0.0)
            if peak > 1e-6:
                scale = 30000.0 / peak
            else:
                scale = 32767.0

            pcm = array.array("h", (
                max(-32768, min(32767, int(s * scale)))
                for s in resampled
            ))
            self._demod.process(pcm.tobytes())

            time.sleep(chunk_size / src_rate)

        logger.info("Test inject complete: %d chunks fed to modem", chunk_count)

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
