#!/usr/bin/env python3
"""
Loopback test: encode a Horus packet -> 4FSK audio -> feed to C modem -> check CRC.
No numpy required — pure Python + horusdemodlib only.

Run inside the Docker container:
    python3 /opt/openwebrx-horus/tools/loopback_test.py
"""

import array
import math
import struct
import sys

from horusdemodlib.demod import HorusLib, Mode, Frame
from horusdemodlib.encoder import Encoder


results = []


def on_frame(frame: Frame):
    results.append(frame)
    status = "CRC OK" if frame.crc_pass else "CRC FAIL"
    print(f"  Frame received: {status}  SNR={frame.snr:.1f} dB")
    if frame.crc_pass and frame.data:
        print(f"  Data: {frame.data.hex()}")


def generate_4fsk_audio(symbols, sample_rate=48000, symbol_rate=100,
                        tone_spacing=270, center_freq=1500):
    samples_per_symbol = sample_rate // symbol_rate
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
        for j in range(samples_per_symbol):
            audio.append(math.sin(phase + two_pi * freq * j / sample_rate))
        phase += two_pi * freq * samples_per_symbol / sample_rate
        phase %= two_pi
    return audio


def build_horus_v1_payload(payload_id=1, sequence=0):
    payload = bytearray(22)
    payload[0] = payload_id & 0xFF
    struct.pack_into("<H", payload, 1, sequence & 0xFFFF)
    payload[3] = 12
    payload[4] = 34
    payload[5] = 56
    struct.pack_into("<f", payload, 6, -34.9285)
    struct.pack_into("<f", payload, 10, 138.6007)
    struct.pack_into("<H", payload, 14, 25000)
    payload[16] = 5
    payload[17] = 10
    payload[18] = 236
    payload[19] = 37
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


def test_loopback(sample_rate):
    print(f"\n{'='*60}")
    print(f"LOOPBACK TEST at {sample_rate} Hz")
    print(f"{'='*60}")

    payload = build_horus_v1_payload(payload_id=1, sequence=42)
    print(f"Payload ({len(payload)} bytes): {payload.hex()}")

    enc = Encoder()
    encoded, num_coded_bits = enc.horus_l2_encode_packet(payload)
    symbols = enc.bytes_to_4fsk_symbols(encoded)
    enc.close()
    print(f"Encoded: {len(encoded)} bytes, {num_coded_bits} coded bits, {len(symbols)} symbols")

    audio = generate_4fsk_audio(symbols, sample_rate=sample_rate)
    print(f"Audio: {len(audio)} samples at {sample_rate} Hz ({len(audio)/sample_rate:.2f}s)")

    # Add 0.5s silence before and after
    silence = [0.0] * (sample_rate // 2)
    audio = silence + audio + silence

    # Convert to int16
    peak = max(abs(s) for s in audio) if audio else 0.0
    if peak > 1e-6:
        scale = 30000.0 / peak
    else:
        scale = 32767.0
    pcm = array.array("h", (
        max(-32768, min(32767, int(s * scale))) for s in audio
    ))
    print(f"PCM: {len(pcm)} samples, peak={peak:.4f}, scale={scale:.1f}")

    results.clear()
    modem = HorusLib(
        mode=Mode.BINARY,
        sample_rate=sample_rate,
        stereo_iq=False,
        verbose=True,
        callback=on_frame,
    )
    print(f"Modem nin: {modem.nin}")

    chunk_size = modem.nin
    offset = 0
    while offset < len(pcm):
        end = min(offset + chunk_size, len(pcm))
        chunk = pcm[offset:end]
        modem.add_samples(chunk.tobytes())
        chunk_size = modem.nin
        offset = end

    modem.close()

    crc_ok = sum(1 for r in results if r.crc_pass)
    crc_fail = sum(1 for r in results if not r.crc_pass)
    print(f"\nResults: {crc_ok} CRC OK, {crc_fail} CRC FAIL out of {len(results)} frames")
    return crc_ok > 0


def main():
    print("Horus Loopback Test (no numpy)")
    print("encode packet -> 4FSK audio -> C modem -> check CRC")

    ok_48k = test_loopback(48000)

    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"  48000 Hz: {'PASS' if ok_48k else 'FAIL'}")
    print(f"{'='*60}")

    if not ok_48k:
        print("\nFAILURE: Loopback doesn't work even at 48 kHz!")
        print("The encoder/decoder pipeline itself is broken.")
        sys.exit(1)
    else:
        print("\nLoopback works — problem is in the RF/audio path.")


if __name__ == "__main__":
    main()
