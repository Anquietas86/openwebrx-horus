import math, array, struct, ctypes
from horusdemodlib.demod import HorusLib, Mode

def crc16(data, init=0xFFFF):
    crc = init
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc

GOLAY_GEN = [0x8E3,0x471,0xA38,0x51C,0x28E,0x947,0xCA3,0xE51,0xF28,0x794,0x3CA,0x9E5]

def golay_encode(d12):
    p = 0
    for i in range(12):
        if d12 & (1 << (11-i)):
            p ^= GOLAY_GEN[i]
    return (d12 << 11) | (p & 0x7FF)

def golay_encode_bytes(payload):
    bits = []
    for b in payload:
        for i in range(7, -1, -1):
            bits.append((b >> i) & 1)
    while len(bits) % 12:
        bits.append(0)
    out = []
    for i in range(0, len(bits), 12):
        d = 0
        for j in range(12):
            d = (d << 1) | bits[i + j]
        cw = golay_encode(d)
        for j in range(22, -1, -1):
            out.append((cw >> j) & 1)
    return out

def interleave(bits, nc=23):
    nr = (len(bits) + nc - 1) // nc
    p = bits + [0] * (nr * nc - len(bits))
    return [p[r * nc + c] for c in range(nc) for r in range(nr)]

def scramble(bits):
    lfsr, out = 1, []
    for b in bits:
        fb = ((lfsr >> 16) ^ (lfsr >> 13)) & 1
        lfsr = ((lfsr << 1) | fb) & 0x1FFFF
        out.append(b ^ (lfsr & 1))
    return out

def make_4fsk_audio(syms):
    SR = 48000
    SPS = SR // 100
    tones = [1500 - 1.5 * 270, 1500 - 0.5 * 270, 1500 + 0.5 * 270, 1500 + 1.5 * 270]
    samples = []
    phase = 0.0
    for s in syms:
        f = tones[s]
        for i in range(SPS):
            samples.append(math.sin(phase))
            phase += 2 * math.pi * f / SR
        phase %= 2 * math.pi
    return array.array('h', (max(-32768, min(32767, int(v * 32767))) for v in samples))

PREAMBLE = [0, 1] * 16

# Horus v1 unique word (from codec2 horus_l2.c)
UW_V1 = []
for byte_val in [0x24, 0x91, 0x24, 0xF1]:
    for i in range(7, -1, -1):
        UW_V1.append((byte_val >> i) & 1)

# Horus v2 unique word
UW_V2 = [0,0,0,1,0,0,1,0,0,1,0,0,0,1,1,1,1,1,1,0,0,1,0,0,1,0,0,0,0,1,0,0]

def encode_and_test(payload_bytes, uw, mode_val, label):
    enc = golay_encode_bytes(payload_bytes)
    enc = interleave(enc)
    enc = scramble(enc)
    frame_bits = PREAMBLE + uw + enc
    syms = [(frame_bits[i] << 1) | frame_bits[i + 1] for i in range(0, len(frame_bits) - 1, 2)]
    pcm = make_4fsk_audio(syms)
    print("\n=== %s ===" % label)
    print("Payload: %d bytes, Encoded: %d bits, Symbols: %d (%.2fs)" % (
        len(payload_bytes), len(enc), len(syms), len(syms) * 0.01))

    m = HorusLib(mode=mode_val, sample_rate=48000)
    sil = b'\x00\x00' * 48000
    m.add_samples(sil)
    f = m.add_samples(pcm.tobytes())
    r1 = "crc=%s snr=%.1f" % (f.crc_pass, f.snr) if f else "None"
    print("After packet: %s" % r1)
    f = m.add_samples(sil)
    r2 = "crc=%s snr=%.1f" % (f.crc_pass, f.snr) if f else "None"
    print("After flush:  %s" % r2)
    m.close()

# --- Build Horus v1 payload (22 bytes) ---
v1 = bytearray(22)
v1[0] = 1                                  # payload_id (uint8)
struct.pack_into("<H", v1, 1, 42)          # sequence
v1[3] = 12; v1[4] = 34; v1[5] = 56        # h, m, s
struct.pack_into("<f", v1, 6, -34.9285)    # lat
struct.pack_into("<f", v1, 10, 138.6007)   # lon
struct.pack_into("<H", v1, 14, 25000)      # alt
v1[16] = 5                                 # speed
v1[17] = 10                                # sats
v1[18] = 236                               # temp (uint8, -20 unsigned)
v1[19] = 37                                # battery (3.7V * 10)
struct.pack_into("<H", v1, 20, crc16(v1[:20]))

# --- Build Horus v2 payload (32 bytes) ---
v2 = bytearray(32)
struct.pack_into("<H", v2, 0, 256)         # payload_id (uint16)
struct.pack_into("<H", v2, 2, 42)          # sequence
v2[4] = 12; v2[5] = 34; v2[6] = 56        # h, m, s
struct.pack_into("<f", v2, 7, -34.9285)    # lat
struct.pack_into("<f", v2, 11, 138.6007)   # lon
struct.pack_into("<H", v2, 15, 25000)      # alt
v2[17] = 5; v2[18] = 10
struct.pack_into("<b", v2, 19, -20)
struct.pack_into("<H", v2, 20, 3700)
struct.pack_into("<H", v2, 30, crc16(v2[:30]))

# Test 1: v1 payload + v1 UW + Mode.BINARY
encode_and_test(bytes(v1), UW_V1, Mode.BINARY, "v1 payload + v1 UW + Mode.BINARY")

# Test 2: v2 payload + v2 UW + Mode.BINARY
encode_and_test(bytes(v2), UW_V2, Mode.BINARY, "v2 payload + v2 UW + Mode.BINARY")

# Test 3: v1 payload + v2 UW + Mode.BINARY
encode_and_test(bytes(v1), UW_V2, Mode.BINARY, "v1 payload + v2 UW + Mode.BINARY")

# Test 4: Try mode value 1 directly (v2 in C library)
print("\n=== Checking C library v2 support ===")
try:
    m = HorusLib(mode=1, sample_rate=48000)
    print("Mode 1 (v2-256bit) - AVAILABLE")
    m.close()
except Exception as e:
    print("Mode 1 failed: %s" % e)

# If mode 1 works via integer, try it
try:
    import enum
    FakeMode = type('FakeMode', (enum.Enum,), {'V2': 1})
    encode_and_test(bytes(v2), UW_V2, FakeMode.V2, "v2 payload + v2 UW + mode=1 (v2)")
except Exception as e:
    print("Mode 1 test failed: %s" % e)

print("\nDone")
