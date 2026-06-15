#!/usr/bin/env python3
"""
Generate a Horus Binary 4FSK test signal and transmit via HackRF One.

Creates valid Horus Binary v2 packets, 4FSK modulates them, FM-modulates
into IQ samples, and either saves to file or pipes directly to hackrf_transfer.

Requirements (on the machine with the HackRF):
    pip install numpy

Usage:
    # Generate IQ file, then transmit separately:
    python3 hackrf_horus_test.py --save horus_test.iq
    hackrf_transfer -t horus_test.iq -f 434200000 -s 2000000 -x 10 -R

    # Transmit directly (generates + pipes to hackrf_transfer):
    python3 hackrf_horus_test.py --transmit -f 434200000 --txgain 10

    # Generate a longer test with more packets:
    python3 hackrf_horus_test.py --save horus_test.iq --packets 20

WARNING: Only transmit on frequencies you are licensed for.
         Use minimum power needed. A dummy load is safest for testing.
         In Australia, 434.200 MHz is commonly used for balloon telemetry.
"""

import argparse
import struct
import subprocess
import sys
import os

import numpy as np


# ── Horus Binary v2 packet format ─────────────────────────────────────
# Reference: https://github.com/projecthorus/horusdemodlib

HORUS_V2_PAYLOAD_LEN = 32  # bytes

def build_horus_v2_payload(
    payload_id=256,
    sequence=0,
    hours=12, minutes=34, seconds=56,
    latitude=-34.9285,
    longitude=138.6007,
    altitude=25000,
    speed=0,
    sats=10,
    temp=-20,
    battery_mv=3700,
    custom=None,
):
    """Build a 32-byte Horus Binary v2 payload."""
    payload = bytearray(32)

    # Payload ID (2 bytes, little-endian)
    struct.pack_into("<H", payload, 0, payload_id)

    # Sequence number (2 bytes)
    struct.pack_into("<H", payload, 2, sequence & 0xFFFF)

    # Time: hours (1), minutes (1), seconds (1)
    payload[4] = hours
    payload[5] = minutes
    payload[6] = seconds

    # Latitude (float32, little-endian)
    struct.pack_into("<f", payload, 7, latitude)

    # Longitude (float32, little-endian)
    struct.pack_into("<f", payload, 11, longitude)

    # Altitude (uint16, little-endian)
    struct.pack_into("<H", payload, 15, min(altitude, 65535))

    # Speed (uint8)
    payload[17] = min(speed, 255)

    # Sats (uint8)
    payload[18] = sats

    # Temp (int8, signed)
    struct.pack_into("<b", payload, 19, max(-128, min(127, temp)))

    # Battery (uint16 mV)
    struct.pack_into("<H", payload, 20, battery_mv)

    # Custom fields (bytes 22-29) — zeros if not provided
    if custom:
        for i, b in enumerate(custom[:8]):
            payload[22 + i] = b

    # CRC16-CCITT over bytes 0..29 → bytes 30-31
    crc = crc16_ccitt(payload[:30])
    struct.pack_into("<H", payload, 30, crc)

    return bytes(payload)


def crc16_ccitt(data, init=0xFFFF):
    """CRC-16 CCITT (0x1021 polynomial)."""
    crc = init
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


# ── Golay(23,12) FEC ──────────────────────────────────────────────────

GOLAY_GENERATOR = [
    0x8E3, 0x471, 0xA38, 0x51C, 0x28E, 0x947, 0xCA3, 0xE51,
    0xF28, 0x794, 0x3CA, 0x9E5,
]

def golay_encode_12(data_12bit):
    """Encode 12 data bits into 23-bit Golay codeword."""
    parity = 0
    for i in range(12):
        if data_12bit & (1 << (11 - i)):
            parity ^= GOLAY_GENERATOR[i]
    # 23-bit codeword: 12 data bits + 11 parity bits
    return (data_12bit << 11) | (parity & 0x7FF)


def golay_encode_bytes(payload):
    """Golay-encode a byte payload. Each 12-bit chunk → 23 bits."""
    # Convert bytes to bit array
    bits = []
    for b in payload:
        for i in range(7, -1, -1):
            bits.append((b >> i) & 1)

    # Pad to multiple of 12
    while len(bits) % 12 != 0:
        bits.append(0)

    # Encode each 12-bit block
    encoded_bits = []
    for i in range(0, len(bits), 12):
        data_12 = 0
        for j in range(12):
            data_12 = (data_12 << 1) | bits[i + j]
        codeword = golay_encode_12(data_12)
        for j in range(22, -1, -1):
            encoded_bits.append((codeword >> j) & 1)

    return encoded_bits


# ── Interleaver & Scrambler ───────────────────────────────────────────

def interleave(bits, ncols=23):
    """Block interleaver — write row-wise, read column-wise."""
    nrows = (len(bits) + ncols - 1) // ncols
    # Pad
    padded = bits + [0] * (nrows * ncols - len(bits))
    # Read column-wise
    out = []
    for col in range(ncols):
        for row in range(nrows):
            out.append(padded[row * ncols + col])
    return out


def scramble(bits):
    """LFSR scrambler (same as horusdemodlib)."""
    lfsr = 0x1
    out = []
    for b in bits:
        feedback = ((lfsr >> 16) ^ (lfsr >> 13)) & 1
        lfsr = ((lfsr << 1) | feedback) & 0x1FFFF
        out.append(b ^ (lfsr & 1))
    return out


# ── 4FSK Modulation ──────────────────────────────────────────────────

# Horus Binary preamble and unique word
PREAMBLE = [0, 1] * 16  # alternating 01 pattern, 32 bits (16 symbols)
UNIQUE_WORD_V2 = [
    0, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0, 0, 0, 1, 1, 1,
    1, 1, 1, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0,
]


def bits_to_symbols(bits):
    """Convert bit pairs to 4FSK symbol indices (0-3)."""
    symbols = []
    for i in range(0, len(bits) - 1, 2):
        sym = (bits[i] << 1) | bits[i + 1]
        symbols.append(sym)
    return symbols


def generate_4fsk_audio(symbols, sample_rate=48000, symbol_rate=100,
                        tone_spacing=270, center_freq=1500):
    """Generate 4FSK audio from symbol indices."""
    samples_per_symbol = sample_rate // symbol_rate
    tone_freqs = [
        center_freq - 1.5 * tone_spacing,
        center_freq - 0.5 * tone_spacing,
        center_freq + 0.5 * tone_spacing,
        center_freq + 1.5 * tone_spacing,
    ]

    audio = np.array([], dtype=np.float32)
    phase = 0.0

    for sym in symbols:
        freq = tone_freqs[sym]
        t = np.arange(samples_per_symbol) / sample_rate
        segment = np.sin(2 * np.pi * freq * t + phase)
        phase += 2 * np.pi * freq * samples_per_symbol / sample_rate
        phase %= 2 * np.pi
        audio = np.concatenate([audio, segment.astype(np.float32)])

    return audio


def encode_packet(payload):
    """Full Horus Binary v2 encode: payload → 4FSK symbols.

    Uses horusdemodlib's official Encoder (C library) when available,
    falls back to the pure-Python implementation otherwise.
    """
    try:
        from horusdemodlib.encoder import Encoder
        enc = Encoder()
        symbols = enc.bytes_to_4fsk_symbols(payload)
        enc.close()
        return list(symbols)
    except ImportError:
        pass

    # Fallback: pure-Python encoding
    encoded_bits = golay_encode_bytes(payload)
    interleaved = interleave(encoded_bits)
    scrambled = scramble(interleaved)
    frame_bits = PREAMBLE + UNIQUE_WORD_V2 + scrambled
    symbols = bits_to_symbols(frame_bits)
    return symbols


# ── FM modulation to IQ ──────────────────────────────────────────────

def fm_modulate(audio, audio_rate=48000, iq_rate=2_000_000, deviation=3000):
    """FM-modulate baseband audio into IQ samples for HackRF."""
    # Resample audio to IQ rate
    duration = len(audio) / audio_rate
    n_iq = int(duration * iq_rate)
    t_audio = np.arange(len(audio)) / audio_rate
    t_iq = np.arange(n_iq) / iq_rate
    audio_resampled = np.interp(t_iq, t_audio, audio)

    # FM modulate: phase = integral of frequency deviation
    freq_dev = deviation * audio_resampled / np.max(np.abs(audio_resampled) + 1e-10)
    phase = 2 * np.pi * np.cumsum(freq_dev) / iq_rate

    # IQ samples
    i_samples = np.cos(phase)
    q_samples = np.sin(phase)

    # Convert to int8 for hackrf_transfer
    i_int8 = np.clip(i_samples * 127, -127, 127).astype(np.int8)
    q_int8 = np.clip(q_samples * 127, -127, 127).astype(np.int8)

    # Interleave I/Q
    iq = np.empty(2 * n_iq, dtype=np.int8)
    iq[0::2] = i_int8
    iq[1::2] = q_int8

    return iq


def generate_silence_iq(duration_sec, iq_rate=2_000_000):
    """Generate silent (carrier-only) IQ samples."""
    n = int(duration_sec * iq_rate)
    iq = np.zeros(2 * n, dtype=np.int8)
    iq[0::2] = 127  # constant I = max, Q = 0 → unmodulated carrier
    return iq


# ── Main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Generate Horus Binary 4FSK test signal for HackRF One",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Save IQ file, transmit manually:
  python3 %(prog)s --save horus_test.iq
  hackrf_transfer -t horus_test.iq -f 434200000 -s 2000000 -x 10 -R

  # Transmit directly:
  python3 %(prog)s --transmit -f 434200000 --txgain 10

  # Custom payload:
  python3 %(prog)s --save test.iq --callsign 256 --lat -34.9 --lon 138.6 --alt 30000
        """,
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--save", metavar="FILE", help="Save IQ to file")
    mode.add_argument("--transmit", action="store_true",
                      help="Pipe directly to hackrf_transfer")

    parser.add_argument("-f", "--freq", type=int, default=434_200_000,
                        help="TX frequency in Hz (default: 434200000)")
    parser.add_argument("--txgain", type=int, default=10,
                        help="HackRF TX gain 0-47 dB (default: 10, keep LOW)")
    parser.add_argument("--packets", type=int, default=10,
                        help="Number of packets to generate (default: 10)")
    parser.add_argument("--gap", type=float, default=2.0,
                        help="Seconds between packets (default: 2.0)")
    parser.add_argument("--iq-rate", type=int, default=2_000_000,
                        help="IQ sample rate (default: 2000000)")

    # Payload fields
    parser.add_argument("--payload-id", type=int, default=256,
                        help="Horus payload ID (default: 256 = 4FSKTEST)")
    parser.add_argument("--lat", type=float, default=-34.9285,
                        help="Latitude (default: -34.9285)")
    parser.add_argument("--lon", type=float, default=138.6007,
                        help="Longitude (default: 138.6007)")
    parser.add_argument("--alt", type=int, default=25000,
                        help="Altitude in metres (default: 25000)")

    args = parser.parse_args()

    print(f"Generating {args.packets} Horus Binary v2 packets...")
    print(f"  Payload ID: {args.payload_id}")
    print(f"  Position:   {args.lat:.4f}, {args.lon:.4f}")
    print(f"  Altitude:   {args.alt} m")
    print(f"  Frequency:  {args.freq / 1e6:.3f} MHz")
    print(f"  TX Gain:    {args.txgain} dB")
    print()

    all_iq = bytearray()

    # Leading silence (1 second)
    all_iq.extend(generate_silence_iq(1.0, args.iq_rate).tobytes())

    # Use official encoder if available
    _official_encoder = None
    try:
        from horusdemodlib.encoder import Encoder
        _official_encoder = Encoder()
        print("  Using horusdemodlib official encoder")
    except ImportError:
        print("  WARNING: horusdemodlib not installed, using fallback encoder")
        print("  Install with: pip3 install horusdemodlib")

    for seq in range(args.packets):
        print(f"  Packet {seq + 1}/{args.packets} (seq={seq})")

        alt = args.alt + seq * 500

        if _official_encoder:
            payload = _official_encoder.create_horus_v2_packet(
                payload_id=args.payload_id,
                sequence_number=seq,
                hours=12, minutes=34, seconds=56 + seq,
                latitude=args.lat + seq * 0.001,
                longitude=args.lon + seq * 0.001,
                altitude=min(alt, 65535),
                speed=5,
                satellites=10,
                temperature=-20 - seq,
                battery_voltage=3.7,
            )
            symbols = _official_encoder.bytes_to_4fsk_symbols(payload)
        else:
            payload = build_horus_v2_payload(
                payload_id=args.payload_id,
                sequence=seq,
                hours=12, minutes=34, seconds=56 + seq,
                latitude=args.lat + seq * 0.001,
                longitude=args.lon + seq * 0.001,
                altitude=min(alt, 65535),
                speed=5,
                sats=10,
                temp=-20 - seq,
                battery_mv=3700,
            )
            symbols = encode_packet(payload)

        audio = generate_4fsk_audio(symbols)
        iq = fm_modulate(audio, iq_rate=args.iq_rate)
        all_iq.extend(iq.tobytes())

        # Gap between packets
        if seq < args.packets - 1:
            all_iq.extend(generate_silence_iq(args.gap, args.iq_rate).tobytes())

    # Trailing silence
    all_iq.extend(generate_silence_iq(1.0, args.iq_rate).tobytes())

    total_bytes = len(all_iq)
    duration = total_bytes / (2 * args.iq_rate)
    print(f"\n  Total: {total_bytes / 1e6:.1f} MB, {duration:.1f} seconds")

    if args.save:
        with open(args.save, "wb") as f:
            f.write(all_iq)
        print(f"  Saved to: {args.save}")
        print(f"\n  To transmit:")
        print(f"    hackrf_transfer -t {args.save} -f {args.freq} -s {args.iq_rate} -x {args.txgain} -R")

    elif args.transmit:
        print("\n  Transmitting via hackrf_transfer...")
        cmd = [
            "hackrf_transfer",
            "-t", "-",           # read from stdin
            "-f", str(args.freq),
            "-s", str(args.iq_rate),
            "-x", str(args.txgain),
        ]
        print(f"  Command: {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
            proc.stdin.write(all_iq)
            proc.stdin.close()
            proc.wait()
        except FileNotFoundError:
            print("\n  ERROR: hackrf_transfer not found.")
            print("  Install it: apt install hackrf  (or brew install hackrf)")
            sys.exit(1)
        except KeyboardInterrupt:
            print("\n  Stopped.")

    if _official_encoder:
        _official_encoder.close()


if __name__ == "__main__":
    main()
