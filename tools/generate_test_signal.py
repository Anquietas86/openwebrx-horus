#!/usr/bin/env python3
"""
Generate a known-good 12 kHz float32 test signal for the Horus demod chain.

Produces /tmp/horus_test_12k.raw — a file of float32 samples at 12 kHz
containing repeated Horus Binary v1 4FSK frames with silence gaps between
them, exactly as the framework would deliver from a real USB-mode reception.

To use:
    1. Run this inside the container to generate the test file:
       python3 /opt/openwebrx-horus/tools/generate_test_signal.py

    2. Set TEST_INJECT = True in owrx/chain/horus.py and restart.
       The chain reads from this file instead of live audio.

    3. Check Docker logs for successful decodes and verify the frontend panel.
"""

import math
import struct
import sys

from horusdemodlib.encoder import Encoder


OUTPUT_PATH = "/opt/openwebrx-horus/test_signal_12k.raw"
SAMPLE_RATE = 12000
TONE_SPACING = 270
CENTER_FREQ = 1500
SYMBOL_RATE = 100
NUM_PACKETS = 10
SILENCE_BETWEEN = 2.0  # seconds of silence between packets


def build_horus_v1_payload(payload_id=1, sequence=0):
    payload = bytearray(22)
    payload[0] = payload_id & 0xFF
    struct.pack_into("<H", payload, 1, sequence & 0xFFFF)
    payload[3] = 12  # hours
    payload[4] = 34  # minutes
    payload[5] = 56  # seconds
    struct.pack_into("<f", payload, 6, -34.9285)   # latitude
    struct.pack_into("<f", payload, 10, 138.6007)   # longitude
    struct.pack_into("<H", payload, 14, 25000)      # altitude
    payload[16] = 5   # speed
    payload[17] = 10  # sats
    payload[18] = 236  # temp (raw)
    payload[19] = 37   # battery (raw)
    crc = 0xFFFF
    for byte in payload[:20]:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    struct.pack_into("<H", payload, 20, crc)
    return bytes(payload)


def generate_4fsk_audio(symbols, sample_rate, symbol_rate=SYMBOL_RATE,
                        tone_spacing=TONE_SPACING, center_freq=CENTER_FREQ):
    samples_per_symbol = sample_rate / symbol_rate
    tone_freqs = [
        center_freq - 1.5 * tone_spacing,
        center_freq - 0.5 * tone_spacing,
        center_freq + 0.5 * tone_spacing,
        center_freq + 1.5 * tone_spacing,
    ]
    audio = []
    phase = 0.0
    two_pi = 2.0 * math.pi
    for sym in symbols:
        freq = tone_freqs[sym]
        n_samp = int(round(samples_per_symbol))
        for j in range(n_samp):
            audio.append(0.8 * math.sin(phase))
            phase += two_pi * freq / sample_rate
        phase %= two_pi
    return audio


def main():
    print(f"Generating test signal: {NUM_PACKETS} Horus v1 packets at {SAMPLE_RATE} Hz")
    print(f"Tones: {CENTER_FREQ - 1.5*TONE_SPACING:.0f}, "
          f"{CENTER_FREQ - 0.5*TONE_SPACING:.0f}, "
          f"{CENTER_FREQ + 0.5*TONE_SPACING:.0f}, "
          f"{CENTER_FREQ + 1.5*TONE_SPACING:.0f} Hz")

    enc = Encoder()

    all_samples = []
    silence_samples = int(SILENCE_BETWEEN * SAMPLE_RATE)

    # Lead-in silence
    all_samples.extend([0.0] * silence_samples)

    for i in range(NUM_PACKETS):
        payload = build_horus_v1_payload(payload_id=1, sequence=i)
        encoded, _ = enc.horus_l2_encode_packet(payload)
        symbols = enc.bytes_to_4fsk_symbols(encoded)

        audio = generate_4fsk_audio(symbols, SAMPLE_RATE)
        all_samples.extend(audio)
        all_samples.extend([0.0] * silence_samples)

        print(f"  Packet {i}: {len(symbols)} symbols, {len(audio)} samples, "
              f"seq={i}, CRC in payload: {payload[20]:02X}{payload[21]:02X}")

    enc.close()

    duration = len(all_samples) / SAMPLE_RATE
    print(f"\nTotal: {len(all_samples)} samples ({duration:.1f}s)")

    with open(OUTPUT_PATH, "wb") as f:
        f.write(struct.pack("<%df" % len(all_samples), *all_samples))

    print(f"Written to {OUTPUT_PATH} ({len(all_samples) * 4} bytes)")
    print(f"\nTo use: set TEST_INJECT = True in owrx/chain/horus.py and restart container")


if __name__ == "__main__":
    main()
