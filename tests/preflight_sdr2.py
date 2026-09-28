#!/usr/bin/env python3
"""
SDR2 Horus pre-flight check — run ON SDR2.

Validates the whole Horus chain without waiting for a real balloon:
  A. OpenWebRX wrapper unit tests
  B. Decode a REAL Horus Binary signal from a WAV sample
  C. OpenWebRX demodulator wrapper construct/close
  D. The exact telemetry dict the plugin hook receives
  E. The map-location object the plugin plots
"""
import glob
import sys
import unittest
import wave

PYROOT = "/usr/lib/python3/dist-packages"
sys.path.insert(0, PYROOT)

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


print("=" * 68)
print("A. OpenWebRX Horus wrapper unit tests")
print("=" * 68)
loader = unittest.TestLoader()
suite = loader.discover("tests", pattern="test_decoder.py", top_level_dir=".")
res = unittest.TextTestRunner(verbosity=2).run(suite)
check("wrapper unit tests", res.wasSuccessful(),
      f"{res.testsRun} run, {len(res.failures)} failures, {len(res.errors)} errors")

print()
print("=" * 68)
print("B. Decode REAL Horus Binary signal (48 kHz WAV sample)")
print("=" * 68)
wavs = glob.glob("samples/horus_binary_ebno_4.5db.wav")
if not wavs:
    check("real-signal decode", False, "sample WAV not found")
else:
    from horusdemodlib.demod import HorusLib, Mode
    from horusdemodlib.decoder import decode_packet

    path = wavs[0]
    with wave.open(path, "rb") as wf:
        ch, sw, rate, n = wf.getnchannels(), wf.getsampwidth(), wf.getframerate(), wf.getnframes()
        print(f"  {path}: {ch}ch {sw*8}bit {rate}Hz {n/rate:.2f}s")
        demod = HorusLib(mode=Mode.BINARY, sample_rate=rate, stereo_iq=(ch == 2), verbose=False)
        frames, decoded, crc_fail = 0, [], 0
        chunk = rate // 10
        while True:
            raw = wf.readframes(chunk)
            if not raw:
                break
            f = demod.add_samples(raw)
            if f is not None:
                frames += 1
                if f.crc_pass:
                    decoded.append((decode_packet(f.data), f.snr))
                else:
                    crc_fail += 1
        demod.close()

    print(f"  frames={frames} crc_ok={len(decoded)} crc_fail={crc_fail}")
    for t, snr in decoded[:5]:
        print(f"    {t.get('callsign')} seq={t.get('sequence_number')} "
              f"lat={t.get('latitude')} lon={t.get('longitude')} "
              f"alt={t.get('altitude')}m snr={snr:.1f}dB")
    check("real Horus Binary signal decoded", len(decoded) > 0,
          f"{len(decoded)} valid packets from a real signal")

print()
print("=" * 68)
print("C. OpenWebRX demodulator wrapper (construct / feed / close)")
print("=" * 68)
try:
    from owrx.horus import HorusDemodulator, HORUS_MODES
    check("HORUS_MODES mapping", HORUS_MODES.get("horus_binary") is not None,
          str({k: str(v) for k, v in HORUS_MODES.items()}))
    for mode in ("horus_binary", "horus_rtty"):
        d = HorusDemodulator(mode_str=mode)
        ok = d._demod is not None
        d.close()
        check(f"wrapper {mode} construct+close", ok and d._demod is None)
except Exception as e:
    check("OpenWebRX demodulator wrapper", False, f"{type(e).__name__}: {e}")

print()
print("=" * 68)
print("D. Telemetry dict the plugin hook receives")
print("=" * 68)
try:
    from owrx.horus import format_horus_telemetry
    sample = {
        "callsign": "VK5ARG", "sequence_number": 127,
        "latitude": -35.1, "longitude": 138.6, "altitude": 28500,
        "snr": 11.4, "crc_ok": True,
        "temperature": 21.5, "humidity": 48.0, "pressure": 612.3,
        "battery": 3.87, "speed": 12.0, "sats": 9,
        "custom_field_names": [], "custom_fields": {},
    }
    out = format_horus_telemetry(sample)
    print(f"  {out}")
    need = ["mode", "callsign", "lat", "lon", "altitude", "snr", "timestamp"]
    check("plugin payload has required keys", all(k in out for k in need),
          f"missing={[k for k in need if k not in out]}")
    check("payload mode == 'Horus' (plugin routes on this)", out.get("mode") == "Horus",
          f"mode={out.get('mode')!r}")
except Exception as e:
    check("telemetry formatting", False, f"{type(e).__name__}: {e}")

print()
print("=" * 68)
print("E. Map-location object (plugin plots this on Leaflet)")
print("=" * 68)
try:
    from owrx.horus import HorusLocation
    loc = HorusLocation({"callsign": "VK5ARG", "latitude": -35.1, "longitude": 138.6,
                         "altitude": 28500, "sequence_number": 127})
    d = loc.__dict__()
    check("location has lat/lon/alt", all(k in d for k in ("latitude", "longitude", "altitude"))
          or all(k in d for k in ("lat", "lon", "altitude")), str(d)[:120])
except Exception as e:
    check("map location object", False, f"{type(e).__name__}: {e}")

print()
print("=" * 68)
print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
print("=" * 68)
sys.exit(1 if FAIL else 0)
