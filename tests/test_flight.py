"""Flight-day paths the other tests missed: real payload IDs and RTTY.

- Horus Binary v1/v2 carry a numeric payload ID. Without horusdemodlib's
  payload ID list loaded, every real payload decoded as UNKNOWN_PAYLOAD_ID
  and SondeHub refused the upload (the other tests use ID 1, HORUSBINARY,
  which is in the built-in list, so they never saw it).
- RTTY frames come out of HorusLib as ASCII sentences, which decode_packet()
  cannot take, so RTTY never decoded at all.
"""

import math
import os
import struct
import sys
import tempfile
import unittest
from unittest import mock

import horusdemodlib.payloads as horus_payloads
from horusdemodlib.utils import telem_to_sondehub

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from loopback_test import build_horus_v1_payload  # noqa: E402

TEST_ID = 213
TEST_CALL = "VK5TEST"


def _crc16(text: str) -> int:
    crc = 0xFFFF
    for c in text.encode("ascii"):
        crc ^= c << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def ukhas_sentence(seq: int) -> str:
    body = "{},{},12:00:00,-34.92850,138.60070,1234,5,23".format(TEST_CALL, seq)
    return "$$$$$" + body + "*%04X\n" % _crc16(body)


def rtty_audio(text, bits=7, stops=2, fs=48000, baud=100, f0=1200.0, shift=425.0):
    """Continuous-phase 2FSK, async framing (start bit, LSB first, stop bits)."""
    import numpy

    stream = [1] * 50
    for ch in text:
        v = ord(ch)
        stream += [0] + [(v >> i) & 1 for i in range(bits)] + [1] * stops
    stream += [1] * 50

    spb = fs // baud
    t = numpy.arange(spb)
    phase = 0.0
    out = []
    for bit in stream:
        f = f0 + shift if bit else f0
        out.append(numpy.sin(phase + 2 * math.pi * f * t / fs))
        phase = (phase + 2 * math.pi * f * spb / fs) % (2 * math.pi)
    return (0.9 * numpy.concatenate(out)).astype(numpy.float32)


class _SavedPayloadLists(unittest.TestCase):
    def setUp(self):
        self._saved = (horus_payloads.HORUS_PAYLOAD_LIST, horus_payloads.HORUS_CUSTOM_FIELDS)
        # Never touch the network from these tests.
        self._patches = [
            mock.patch("owrx.horus.download_latest_payload_id_list", return_value=None),
            mock.patch("owrx.horus.download_latest_custom_field_list", return_value=None),
        ]
        for p in self._patches:
            p.start()
        self.tmp = tempfile.TemporaryDirectory()
        self._dir = mock.patch("owrx.horus._PayloadLists._cache_dir", return_value=self.tmp.name)
        self._dir.start()
        # The shared loader: demodulators created here must not start one.
        self._shared = mock.patch("owrx.horus._payload_lists")
        self.shared_lists = self._shared.start()

    def tearDown(self):
        self._shared.stop()
        self._dir.stop()
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()
        horus_payloads.HORUS_PAYLOAD_LIST, horus_payloads.HORUS_CUSTOM_FIELDS = self._saved


class FakeFrame:
    def __init__(self, data):
        self.data = data
        self.crc_pass = True
        self.snr = 12.0
        self.extended_stats = None


class TestPayloadIdList(_SavedPayloadLists):
    def test_real_payload_id_decodes_and_uploads(self):
        from owrx.horus import HorusDemodulator, _PayloadLists

        horus_payloads.HORUS_PAYLOAD_LIST = {0: "4FSKTEST", 1: "HORUSBINARY", 256: "4FSKTEST-V2"}
        packet = build_horus_v1_payload(payload_id=TEST_ID, sequence=5)

        d = HorusDemodulator("horus-binary")
        try:
            before = d._handle_frame(FakeFrame(packet))
            self.assertEqual(before["callsign"], "UNKNOWN_PAYLOAD_ID")
            # an unknown ID asks for a new list
            self.shared_lists.refresh.assert_called_once()

            new_list = dict(horus_payloads.HORUS_PAYLOAD_LIST)
            new_list[TEST_ID] = TEST_CALL
            lists = _PayloadLists()
            with mock.patch("owrx.horus.download_latest_payload_id_list", return_value=new_list):
                lists.load()

            after = d._handle_frame(FakeFrame(packet))
        finally:
            d.close()

        self.assertEqual(after["callsign"], TEST_CALL)
        after["time"] = "12:00:00"
        sh = telem_to_sondehub(after, check_time=False)
        self.assertIsNotNone(sh, "SondeHub reformat refused the frame")
        self.assertEqual(sh["payload_callsign"], TEST_CALL)

    def test_offline_start_uses_bundled_lists(self):
        from owrx.horus import _PayloadLists

        horus_payloads.HORUS_PAYLOAD_LIST = {0: "4FSKTEST"}
        _PayloadLists().load()
        # The copy shipped in the horusdemodlib package has hundreds of IDs.
        self.assertGreater(len(horus_payloads.HORUS_PAYLOAD_LIST), 10)

    def test_offline_start_prefers_cached_download(self):
        from owrx.horus import _PayloadLists

        cache = os.path.join(self.tmp.name, _PayloadLists.PAYLOAD_CACHE)
        with open(cache, "w") as f:
            f.write("# HORUS BINARY PAYLOAD ID LIST\n0, 4FSKTEST\n1, HORUSBINARY\n"
                    "256, 4FSKTEST-V2\n{}, {}\n".format(TEST_ID, TEST_CALL))
        horus_payloads.HORUS_PAYLOAD_LIST = {0: "4FSKTEST"}
        _PayloadLists().load()
        self.assertEqual(horus_payloads.HORUS_PAYLOAD_LIST.get(TEST_ID), TEST_CALL)

    def test_refresh_is_rate_limited(self):
        from owrx.horus import _PayloadLists

        lists = _PayloadLists()
        with mock.patch.object(lists, "load"):
            self.assertTrue(lists.refresh())
            lists._loading = False
            self.assertFalse(lists.refresh())          # inside the retry window
            lists._next_allowed = 0
            self.assertTrue(lists.refresh())


class TestRtty(_SavedPayloadLists):
    def decode(self, mode, bits, stops):
        import numpy
        from owrx.horus import HorusDemodulator

        got = []
        d = HorusDemodulator(mode, callback=got.append)
        try:
            audio = numpy.concatenate(
                [numpy.zeros(24000, numpy.float32)]
                + [rtty_audio(ukhas_sentence(i), bits=bits, stops=stops) for i in range(3)]
                + [numpy.zeros(24000, numpy.float32)]
            )
            for i in range(0, len(audio), 2048):
                d.process_float(audio[i:i + 2048].tobytes())
        finally:
            d.close()
        return got

    def check(self, got):
        self.assertTrue(got, "no RTTY sentence decoded")
        t = got[-1]
        self.assertEqual(t["callsign"], TEST_CALL)
        self.assertAlmostEqual(t["latitude"], -34.9285, places=4)
        self.assertEqual(t["altitude"], 1234)
        self.assertIsNotNone(telem_to_sondehub(dict(t), check_time=False))

    def test_rtty_7n2(self):
        self.check(self.decode("horus-rtty", 7, 2))

    def test_rtty_8n2(self):
        self.check(self.decode("horus-rtty-8n2", 8, 2))


class TestSatellites(unittest.TestCase):
    def test_satellites_reach_the_panels(self):
        from owrx.horus import format_horus_telemetry

        out = format_horus_telemetry({"callsign": "X", "satellites": 9})
        self.assertEqual(out["sats"], 9)
        self.assertIn("9 sats", out["comment"])


if __name__ == "__main__":
    unittest.main()
