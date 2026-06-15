#!/usr/bin/env python3
"""
Analyze raw audio dumps from the Horus demod chain.

Reads /tmp/horus_audio_12k.raw and /tmp/horus_audio_48k.raw,
computes spectral content, and tries to decode the 48k audio
directly with the C modem.

Run inside the Docker container:
    python3 /opt/openwebrx-horus/tools/analyze_audio_dump.py

The dumped audio is float32, produced by owrx/chain/horus.py when
DUMP_AUDIO=True. Start the HackRF transmitter, wait ~15 seconds,
then run this script to analyze the captured audio.
"""

import array
import math
import os
import struct
import sys

from horusdemodlib.demod import HorusLib, Mode, Frame


results = []

def on_frame(frame: Frame):
    results.append(frame)
    status = "CRC OK" if frame.crc_pass else "CRC FAIL"
    print(f"  Frame: {status}  SNR={frame.snr:.1f} dB")
    if frame.crc_pass and frame.data:
        print(f"  Data: {frame.data.hex()}")


def read_float_raw(path):
    """Read a raw float32 file into a list of floats."""
    if not os.path.isfile(path):
        print(f"  File not found: {path}")
        return None
    size = os.path.getsize(path)
    n_samples = size // 4
    with open(path, "rb") as f:
        data = f.read(n_samples * 4)
    return list(struct.unpack("<%df" % n_samples, data))


def compute_spectrum(samples, sample_rate, fft_size=4096):
    """Compute power spectrum using DFT (no numpy needed).
    Returns list of (frequency, power_dB) tuples for positive freqs."""
    n = min(len(samples), fft_size)
    # Apply Hanning window
    windowed = [samples[i] * (0.5 - 0.5 * math.cos(2 * math.pi * i / n))
                for i in range(n)]

    # DFT for positive frequencies only
    spectrum = []
    for k in range(n // 2):
        freq = k * sample_rate / n
        re = sum(windowed[i] * math.cos(-2 * math.pi * k * i / n) for i in range(n))
        im = sum(windowed[i] * math.sin(-2 * math.pi * k * i / n) for i in range(n))
        power = re * re + im * im
        db = 10 * math.log10(power + 1e-20)
        spectrum.append((freq, db))
    return spectrum


def find_peaks(spectrum, min_db=-20, min_freq=500, max_freq=3000):
    """Find spectral peaks above min_db between min_freq and max_freq."""
    filtered = [(f, db) for f, db in spectrum if min_freq <= f <= max_freq]
    if not filtered:
        return []

    max_db = max(db for _, db in filtered)
    threshold = max_db - 20  # peaks within 20 dB of max

    peaks = []
    for i in range(1, len(filtered) - 1):
        f, db = filtered[i]
        if db > threshold and db > filtered[i-1][1] and db > filtered[i+1][1]:
            peaks.append((f, db))
    return peaks


def analyze_signal(samples, sample_rate, label):
    """Analyze audio signal characteristics."""
    print(f"\n--- {label} ---")
    print(f"  Samples: {len(samples)}, Duration: {len(samples)/sample_rate:.2f}s")

    peak = max(abs(s) for s in samples)
    rms = math.sqrt(sum(s*s for s in samples) / len(samples))
    print(f"  Peak: {peak:.6f}, RMS: {rms:.6f}")

    # DC offset
    dc = sum(samples) / len(samples)
    print(f"  DC offset: {dc:.6f}")

    # Analyze a chunk from the middle (where signal should be present)
    mid = len(samples) // 2
    chunk_size = min(8192, len(samples) // 4)
    chunk_start = mid - chunk_size // 2
    chunk = samples[chunk_start:chunk_start + chunk_size]

    chunk_peak = max(abs(s) for s in chunk)
    chunk_rms = math.sqrt(sum(s*s for s in chunk) / len(chunk))
    chunk_dc = sum(chunk) / len(chunk)
    print(f"  Mid-chunk peak: {chunk_peak:.6f}, RMS: {chunk_rms:.6f}, DC: {chunk_dc:.6f}")

    # Spectrum of middle chunk (using smaller FFT for speed)
    fft_size = min(2048, len(chunk))
    print(f"  Computing spectrum ({fft_size}-point DFT)...")
    spectrum = compute_spectrum(chunk, sample_rate, fft_size)

    # Expected Horus tones: 1095, 1365, 1635, 1905 Hz (270 Hz spacing, 1500 Hz center)
    print(f"\n  Expected 4FSK tones: 1095, 1365, 1635, 1905 Hz")
    print(f"  Power at expected tone frequencies:")
    for target in [1095, 1365, 1635, 1905]:
        # Find closest bin
        closest = min(spectrum, key=lambda x: abs(x[0] - target))
        print(f"    {target} Hz: {closest[1]:.1f} dB (nearest bin: {closest[0]:.1f} Hz)")

    # Also check for energy at unexpected frequencies
    print(f"\n  Spectral peaks (500-3000 Hz):")
    peaks = find_peaks(spectrum, min_freq=500, max_freq=3000)
    for f, db in sorted(peaks, key=lambda x: -x[1])[:10]:
        print(f"    {f:.1f} Hz: {db:.1f} dB")

    # Check low frequency (DC offset / carrier leak)
    print(f"\n  Low-frequency content (0-500 Hz):")
    low_peaks = find_peaks(spectrum, min_freq=0, max_freq=500)
    for f, db in sorted(low_peaks, key=lambda x: -x[1])[:5]:
        print(f"    {f:.1f} Hz: {db:.1f} dB")

    return spectrum


def try_decode_48k(samples):
    """Feed the 48 kHz audio directly to the modem."""
    print(f"\n--- Attempting decode of 48 kHz dump ---")

    # Normalize to int16 (global normalization)
    peak = max(abs(s) for s in samples) if samples else 0.0
    if peak < 1e-6:
        print("  Audio is silent!")
        return

    scale = 30000.0 / peak
    print(f"  Peak: {peak:.6f}, Scale: {scale:.1f}")

    pcm = array.array("h", (
        max(-32768, min(32767, int(s * scale))) for s in samples
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
    print(f"  Results: {ok} CRC OK, {fail} CRC FAIL out of {len(results)} frames")

    if ok == 0 and fail > 0:
        print("  CRC failures with global normalization — signal is distorted")
        print("  (not a normalization issue since this uses global norm)")


def main():
    print("Horus Audio Dump Analyzer")
    print("=" * 60)

    audio_12k = read_float_raw("/tmp/horus_audio_12k.raw")
    audio_48k = read_float_raw("/tmp/horus_audio_48k.raw")

    if audio_12k is None and audio_48k is None:
        print("\nNo audio dumps found! Make sure:")
        print("  1. DUMP_AUDIO=True in owrx/chain/horus.py")
        print("  2. Container was restarted after the change")
        print("  3. HackRF transmitter was running")
        print("  4. Wait ~15 seconds for the dump to complete")
        sys.exit(1)

    if audio_12k:
        analyze_signal(audio_12k, 12000, "12 kHz audio (from framework/NFM demod)")

    if audio_48k:
        analyze_signal(audio_48k, 48000, "48 kHz audio (after resampling)")
        try_decode_48k(audio_48k)

    print(f"\n{'='*60}")
    print("Done. Check the spectral peaks above:")
    print("  - If tones are at 1095/1365/1635/1905 Hz → signal OK, issue is elsewhere")
    print("  - If tones are shifted → frequency offset in RF path")
    print("  - If no clear tones → NFM demod is not recovering the signal")
    print("  - If decode succeeds with global norm → per-chunk norm is the issue")


if __name__ == "__main__":
    main()
