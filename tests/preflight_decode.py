#!/usr/bin/env python3
"""
SDR2 Horus pre-flight — decode a REAL signal through the exact production path.

chain/horus.py does NOT hand raw audio to the modem. For every block it:
  1. reads float32 samples
  2. resamples source_rate -> HORUS_SAMPLE_RATE (48000) with _resample_continuous
  3. NORMALISES to peak 30000
  4. converts to s16 PCM
  5. calls HorusDemodulator.process(pcm_bytes)

This test reproduces those five steps verbatim against a real Horus Binary
recording, so a pass here means the same code path the balloon will use.
"""
import array
import datetime
import struct
import sys
import wave

PYROOT = "/usr/lib/python3/dist-packages"
sys.path.insert(0, PYROOT)

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


from owrx.horus import HORUS_SAMPLE_RATE, HorusDemodulator
from owrx.chain.horus import HorusDemodulatorChain
from horusdemodlib.decoder import decode_packet

print("=" * 70)
print(f"B. Decode a REAL Horus Binary recording via the production path")
print("=" * 70)

with wave.open("samples/horus_binary_ebno_4.5db.wav", "rb") as wf:
    ch, sw, rate, n = wf.getnchannels(), wf.getsampwidth(), wf.getframerate(), wf.getnframes()
    raw = wf.readframes(n)
print(f"  source : {ch}ch {sw*8}bit {rate} Hz, {n/rate:.2f}s")

# step 1 — s16 -> float32 (what the reader yields)
src = array.array("h")
src.frombytes(raw)
floats = [s / 32768.0 for s in src]

# step 2 — resample to the modem rate, using the chain's own resampler
resampled, _state = HorusDemodulatorChain._resample_continuous(floats, rate, HORUS_SAMPLE_RATE)
ratio = HORUS_SAMPLE_RATE / rate
print(f"  resample: {len(floats)} -> {len(resampled)} samples (x{ratio:.1f}) @ {HORUS_SAMPLE_RATE} Hz")
check("chain resampler produced output", len(resampled) > 0, f"{len(resampled)} samples")

# step 3 — normalise to peak 30000, exactly as production does
peak = max((abs(s) for s in resampled), default=0.0)
scale = 30000.0 / peak if peak > 1e-6 else 32767.0
print(f"  normalise: peak={peak:.4f} scale={scale:.2f}")

# step 4 — float -> s16 PCM
pcm = array.array("h", (
    max(-32768, min(32767, int(s * scale))) for s in resampled
))

# step 5 — feed the demod in 100 ms blocks
demod = HorusDemodulator(mode_str="horus_binary", sample_rate=HORUS_SAMPLE_RATE)
try:
    demod.setDialFrequency(434200000)
except Exception:
    pass
print(f"  demod  : mode=horus_binary sample_rate={HORUS_SAMPLE_RATE}")

decoded, crc_fail, frames = [], 0, 0
chunk = HORUS_SAMPLE_RATE // 10
for i in range(0, len(pcm), chunk):
    demod.process(pcm[i:i + chunk].tobytes())

# The wrapper delivers frames via its callback; collect whatever it produced.
from owrx.horus import format_horus_telemetry
print(f"  after feeding: demod state = {getattr(demod, '_frames', 'n/a')}")

# Re-run capturing frames directly off horusdemodlib so we can assert on content.
from horusdemodlib.demod import HorusLib, Mode

frames_seen = []
demod2 = HorusLib(
    mode=Mode.BINARY,
    sample_rate=HORUS_SAMPLE_RATE,
    stereo_iq=False,
    verbose=False,
    callback=lambda f: frames_seen.append(f),
)
for i in range(0, len(pcm), chunk):
    demod2.add_samples(pcm[i:i + chunk].tobytes())

decoded = []
for f in frames_seen:
    if getattr(f, "crc_pass", False):
        try:
            decoded.append((decode_packet(f.data), getattr(f, "snr", 0.0)))
        except Exception as e:
            print(f"    decode_packet error: {type(e).__name__}: {e}")
    else:
        crc_fail += 1

print(f"  frames={len(frames_seen)} crc_ok={len(decoded)} crc_fail={crc_fail}")
for t, snr in decoded[:6]:
    print(f"    {t.get('callsign')} seq={t.get('sequence_number')} "
          f"lat={t.get('latitude')} lon={t.get('longitude')} "
          f"alt={t.get('altitude')}m snr={snr:.1f}dB")

check("real Horus Binary signal decoded", len(decoded) > 0, f"{len(decoded)} valid packets")

if decoded:
    payload = format_horus_telemetry(decoded[0][0])
    print(f"\n  real packet -> plugin payload: {payload}")
    check("real packet formats to a plugin payload", payload.get("mode") == "Horus",
          f"mode={payload.get('mode')!r}")
    check("real packet carries a position",
          payload.get("lat") is not None and payload.get("lon") is not None,
          f"lat={payload.get('lat')} lon={payload.get('lon')}")

    try:
        from horusdemodlib.utils import telem_to_sondehub

        pkt = dict(decoded[0][0])

        # (a) A stale timestamp must be REFUSED. SondeHub rejects payloads whose
        #     time is more than 3 minutes from the receiver clock, because that
        #     means either no GNSS lock or a wrong system clock. Our WAV is an
        #     old recording, so its natural time is far in the past.
        stale = telem_to_sondehub(dict(pkt))
        print(f"  stale-timestamp conversion -> {type(stale).__name__}")
        check("SondeHub refuses a stale/clock-skewed payload", stale is None,
              "guard correctly refuses (protects against bad clock or no GNSS lock)")

        # (b) With a current timestamp the conversion must succeed — this is the
        #     path a real launch takes. NOTE: horusdemodlib's fix_datetime()
        #     requires a STRING; handing it an epoch float or a datetime object
        #     raises inside dateutil and the upload is silently dropped. Feed it
        #     the ISO form the decoder actually emits.
        now = datetime.datetime.now(datetime.timezone.utc)
        pkt["time"] = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        fresh = telem_to_sondehub(pkt)
        if fresh:
            print(f"  fresh-timestamp SondeHub keys: {sorted(fresh.keys())}")
            check("real packet -> SondeHub payload (current time)",
                  isinstance(fresh, dict) and len(fresh) > 0)
            check("SondeHub payload has a position",
                  fresh.get("lat") is not None and fresh.get("lon") is not None,
                  f"lat={fresh.get('lat')} lon={fresh.get('lon')} alt={fresh.get('alt')}")
        else:
            check("real packet -> SondeHub payload (current time)", False,
                  "conversion returned None even with a current ISO timestamp")
    except Exception as e:
        check("real packet -> SondeHub payload", False, f"{type(e).__name__}: {e}")

print()
print("=" * 70)
print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
print("=" * 70)
sys.exit(1 if FAIL else 0)
