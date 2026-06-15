#!/usr/bin/env python3
"""
Extended loopback test: simulates the full OpenWebRX chain.

1. Generate 4FSK at 48 kHz (like the HackRF transmitter)
2. Decimate to 12 kHz (simulating what the framework delivers)
3. Resample back to 48 kHz using the exact _resample_continuous code
4. Apply per-chunk normalization (matching owrx/chain/horus.py)
5. Feed to the C modem in realistic chunk sizes

If this test PASSES, the problem is in the RF/NFM path.
If this test FAILS, the problem is in the resampling/normalization chain.

Run inside the Docker container:
    python3 /opt/openwebrx-horus/tools/resample_loopback_test.py
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


def decimate(samples, src_rate, dst_rate):
    """Simple decimation with linear interpolation (anti-alias not needed
    since all content is below dst_rate/2 for our signal)."""
    ratio = src_rate / dst_rate
    out = []
    pos = 0.0
    n = len(samples)
    while pos < n - 1:
        idx = int(pos)
        frac = pos - idx
        val = samples[idx] * (1.0 - frac) + samples[idx + 1] * frac
        out.append(val)
        pos += ratio
    return out


def resample_continuous(samples, src_rate, dst_rate, state=None):
    """Exact copy of _resample_continuous from owrx/chain/horus.py"""
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


def test_direct(symbols):
    """Test 1: Direct 48 kHz loopback (baseline)."""
    print(f"\n{'='*60}")
    print("TEST 1: Direct 48 kHz loopback (baseline)")
    print(f"{'='*60}")

    audio = generate_4fsk_audio(symbols, sample_rate=48000)
    silence = [0.0] * 24000  # 0.5s
    audio = silence + audio + silence

    peak = max(abs(s) for s in audio) if audio else 0.0
    scale = 30000.0 / peak if peak > 1e-6 else 32767.0
    pcm = array.array("h", (
        max(-32768, min(32767, int(s * scale))) for s in audio
    ))

    results.clear()
    modem = HorusLib(mode=Mode.BINARY, sample_rate=48000, verbose=False, callback=on_frame)

    offset = 0
    while offset < len(pcm):
        end = min(offset + modem.nin, len(pcm))
        modem.add_samples(pcm[offset:end].tobytes())
        offset = end
    modem.close()

    ok = sum(1 for r in results if r.crc_pass)
    fail = sum(1 for r in results if not r.crc_pass)
    print(f"  Results: {ok} CRC OK, {fail} CRC FAIL")
    return ok > 0


def test_resample_bulk(symbols):
    """Test 2: 48 kHz → 12 kHz → 48 kHz, all-at-once resample + bulk normalize."""
    print(f"\n{'='*60}")
    print("TEST 2: Decimate 48k→12k, resample 12k→48k (bulk)")
    print(f"{'='*60}")

    audio_48k = generate_4fsk_audio(symbols, sample_rate=48000)
    silence_48k = [0.0] * 24000
    audio_48k = silence_48k + audio_48k + silence_48k

    # Decimate to 12 kHz
    audio_12k = decimate(audio_48k, 48000, 12000)
    print(f"  48 kHz: {len(audio_48k)} samples → 12 kHz: {len(audio_12k)} samples")

    # Resample back to 48 kHz (single chunk, no chunking effects)
    resampled, _ = resample_continuous(audio_12k, 12000, 48000)
    print(f"  Resampled back to 48 kHz: {len(resampled)} samples")

    # Normalize entire signal at once
    peak = max(abs(s) for s in resampled) if resampled else 0.0
    scale = 30000.0 / peak if peak > 1e-6 else 32767.0
    pcm = array.array("h", (
        max(-32768, min(32767, int(s * scale))) for s in resampled
    ))

    results.clear()
    modem = HorusLib(mode=Mode.BINARY, sample_rate=48000, verbose=False, callback=on_frame)

    offset = 0
    while offset < len(pcm):
        end = min(offset + modem.nin, len(pcm))
        modem.add_samples(pcm[offset:end].tobytes())
        offset = end
    modem.close()

    ok = sum(1 for r in results if r.crc_pass)
    fail = sum(1 for r in results if not r.crc_pass)
    print(f"  Results: {ok} CRC OK, {fail} CRC FAIL")
    return ok > 0


def test_resample_chunked(symbols):
    """Test 3: 48 kHz → 12 kHz → chunked resample + per-chunk normalize.
    This simulates the exact processing in owrx/chain/horus.py."""
    print(f"\n{'='*60}")
    print("TEST 3: Decimate 48k→12k, CHUNKED resample + per-chunk normalize")
    print(f"         (simulates owrx/chain/horus.py exactly)")
    print(f"{'='*60}")

    audio_48k = generate_4fsk_audio(symbols, sample_rate=48000)
    silence_48k = [0.0] * 24000
    audio_48k = silence_48k + audio_48k + silence_48k

    audio_12k = decimate(audio_48k, 48000, 12000)
    print(f"  48 kHz: {len(audio_48k)} samples → 12 kHz: {len(audio_12k)} samples")

    # Simulate framework chunk delivery (~757 float samples per read at 12 kHz)
    chunk_size = 757
    resample_state = None

    results.clear()
    modem = HorusLib(mode=Mode.BINARY, sample_rate=48000, verbose=False, callback=on_frame)

    offset = 0
    chunk_count = 0
    total_resampled = 0
    while offset < len(audio_12k):
        end = min(offset + chunk_size, len(audio_12k))
        chunk = audio_12k[offset:end]
        offset = end
        chunk_count += 1

        # Resample chunk (with state carry — matching horus.py)
        resampled, resample_state = resample_continuous(
            chunk, 12000, 48000, resample_state
        )
        total_resampled += len(resampled)

        # Per-chunk normalize to int16 (matching horus.py exactly)
        peak = max((abs(s) for s in resampled), default=0.0)
        if peak > 1e-6:
            scale = 30000.0 / peak
        else:
            scale = 32767.0

        pcm = array.array("h", (
            max(-32768, min(32767, int(s * scale)))
            for s in resampled
        ))
        modem.add_samples(pcm.tobytes())

    modem.close()
    print(f"  Processed {chunk_count} chunks, {total_resampled} total resampled samples")

    ok = sum(1 for r in results if r.crc_pass)
    fail = sum(1 for r in results if not r.crc_pass)
    print(f"  Results: {ok} CRC OK, {fail} CRC FAIL")
    return ok > 0


def test_resample_chunked_global_norm(symbols):
    """Test 4: Same as test 3, but with GLOBAL normalization instead of per-chunk.
    If this passes but test 3 fails, per-chunk normalization is the culprit."""
    print(f"\n{'='*60}")
    print("TEST 4: Decimate 48k→12k, CHUNKED resample + GLOBAL normalize")
    print(f"         (like test 3 but fixed amplitude)")
    print(f"{'='*60}")

    audio_48k = generate_4fsk_audio(symbols, sample_rate=48000)
    silence_48k = [0.0] * 24000
    audio_48k = silence_48k + audio_48k + silence_48k

    audio_12k = decimate(audio_48k, 48000, 12000)

    # Use a fixed scale factor instead of per-chunk normalization
    fixed_scale = 30000.0  # assumes peak ~1.0 for sine waves

    chunk_size = 757
    resample_state = None

    results.clear()
    modem = HorusLib(mode=Mode.BINARY, sample_rate=48000, verbose=False, callback=on_frame)

    offset = 0
    while offset < len(audio_12k):
        end = min(offset + chunk_size, len(audio_12k))
        chunk = audio_12k[offset:end]
        offset = end

        resampled, resample_state = resample_continuous(
            chunk, 12000, 48000, resample_state
        )

        pcm = array.array("h", (
            max(-32768, min(32767, int(s * fixed_scale)))
            for s in resampled
        ))
        modem.add_samples(pcm.tobytes())

    modem.close()

    ok = sum(1 for r in results if r.crc_pass)
    fail = sum(1 for r in results if not r.crc_pass)
    print(f"  Results: {ok} CRC OK, {fail} CRC FAIL")
    return ok > 0


def main():
    print("Extended Horus Loopback Test — Resample Chain Simulation")
    print("=" * 60)

    payload = build_horus_v1_payload(payload_id=1, sequence=42)
    enc = Encoder()
    encoded, _ = enc.horus_l2_encode_packet(payload)
    symbols = enc.bytes_to_4fsk_symbols(encoded)
    enc.close()
    print(f"Payload: {payload.hex()}")
    print(f"Symbols: {len(symbols)}")

    r1 = test_direct(symbols)
    r2 = test_resample_bulk(symbols)
    r3 = test_resample_chunked(symbols)
    r4 = test_resample_chunked_global_norm(symbols)

    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"  Test 1 (direct 48k):            {'PASS' if r1 else 'FAIL'}")
    print(f"  Test 2 (decimate+resample bulk): {'PASS' if r2 else 'FAIL'}")
    print(f"  Test 3 (chunked+per-chunk norm): {'PASS' if r3 else 'FAIL'}")
    print(f"  Test 4 (chunked+global norm):    {'PASS' if r4 else 'FAIL'}")
    print(f"{'='*60}")

    if r1 and r2 and r3 and r4:
        print("\nAll tests PASS — problem is in the RF/NFM path, not the resampler.")
    elif r1 and r2 and not r3 and r4:
        print("\nPer-chunk normalization is the culprit!")
    elif r1 and r2 and not r3 and not r4:
        print("\nChunked resampling is the culprit!")
    elif r1 and not r2:
        print("\nDecimate+resample is the culprit (even in bulk)!")
    elif not r1:
        print("\nDirect loopback fails — modem/encoder issue!")
    else:
        print("\nMixed results — investigate individual failures above.")

    sys.exit(0 if (r1 and r2 and r3 and r4) else 1)


if __name__ == "__main__":
    main()
