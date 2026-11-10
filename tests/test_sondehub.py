"""SondeHub settings, duplicate-frame gate and audio gain (no OpenWebRX needed)."""

import array
import unittest


class FakeLayer(dict):
    """Behaves like an OpenWebRX PropertyLayer for `in` and `[]`."""


class TestSondehubSettings(unittest.TestCase):
    def settings(self, **kw):
        from owrx.horus import sondehub_station_settings
        return sondehub_station_settings(FakeLayer(**kw))

    def test_off_by_default(self):
        self.assertIsNone(self.settings(sondehub_callsign="VK5ABC"))

    def test_off_when_disabled(self):
        self.assertIsNone(self.settings(sondehub_enabled=False, sondehub_callsign="VK5ABC"))

    def test_off_without_real_callsign(self):
        for cs in (None, "", "  ", "N0CALL", "n0call"):
            self.assertIsNone(self.settings(sondehub_enabled=True, sondehub_callsign=cs), cs)

    def test_uses_receiver_asl_for_altitude(self):
        s = self.settings(
            sondehub_enabled=True,
            sondehub_callsign="VK5ABC",
            sondehub_antenna="Yagi",
            receiver_gps=FakeLayer(lat=-34.9, lon=138.6),
            receiver_asl=120,
        )
        self.assertEqual(s["callsign"], "VK5ABC")
        self.assertEqual(s["antenna"], "Yagi")
        self.assertEqual(s["position"], (-34.9, 138.6, 120.0))

    def test_no_position_when_gps_unset(self):
        s = self.settings(sondehub_enabled=True, sondehub_callsign="VK5ABC",
                          receiver_gps=FakeLayer(lat=0.0, lon=0.0))
        self.assertIsNone(s["position"])


class TestRecentFrames(unittest.TestCase):
    def test_first_time_only(self):
        from owrx.horus import _RecentFrames
        r = _RecentFrames(ttl=600)
        t = {"callsign": "HORUS-V2", "sequence_number": 7}
        self.assertTrue(r.first_time(t, now=0))
        self.assertFalse(r.first_time(dict(t), now=5))
        self.assertTrue(r.first_time({"callsign": "HORUS-V2", "sequence_number": 8}, now=6))
        self.assertTrue(r.first_time({"callsign": "OTHER", "sequence_number": 7}, now=6))

    def test_expires(self):
        from owrx.horus import _RecentFrames
        r = _RecentFrames(ttl=600)
        t = {"callsign": "HORUS-V2", "sequence_number": 7}
        self.assertTrue(r.first_time(t, now=0))
        self.assertTrue(r.first_time(t, now=601))

    def test_frames_without_ids_always_pass(self):
        from owrx.horus import _RecentFrames
        r = _RecentFrames()
        self.assertTrue(r.first_time({"callsign": "X"}, now=0))
        self.assertTrue(r.first_time({"callsign": "X"}, now=0))

    def test_bounded(self):
        from owrx.horus import _RecentFrames
        r = _RecentFrames(ttl=10, max_entries=100)
        for i in range(1000):
            r.first_time({"callsign": "X", "sequence_number": i}, now=i)
        self.assertLessEqual(len(r._seen), 101)


class TestParserDedupe(unittest.TestCase):
    def test_upload_and_spot_once_per_frame(self):
        from unittest import mock
        import owrx.horus as h

        uploads, spots = [], []
        fake_uploader = mock.Mock(upload=uploads.append)
        fake_engine = mock.Mock(spot=spots.append)
        with mock.patch.object(h, "_recent_frames", h._RecentFrames()), \
             mock.patch.object(h.HorusSondehubUploader, "getSharedInstance", return_value=fake_uploader), \
             mock.patch.object(h, "ReportingEngine", mock.Mock(getSharedInstance=lambda: fake_engine)), \
             mock.patch.object(h.HorusParser, "updateMap"):
            a, b = h.HorusParser(), h.HorusParser(service=True)
            t = {"callsign": "HORUS-V2", "sequence_number": 1, "latitude": -34.0, "longitude": 138.0}
            out1 = a.handle(dict(t))
            out2 = b.handle(dict(t))
        self.assertEqual(len(uploads), 1)
        self.assertEqual(len(spots), 1)
        self.assertEqual(spots[0]["mode"], "Horus")
        self.assertEqual(out1["callsign"], out2["callsign"])


class TestAudioGain(unittest.TestCase):
    def _pcm(self, floats):
        from owrx.horus import HorusDemodulator
        d = HorusDemodulator.__new__(HorusDemodulator)
        got = []
        d.process = got.append
        d.process_float(array.array("f", floats).tobytes())
        out = array.array("h")
        out.frombytes(got[0])
        return list(out)

    def test_fixed_gain_and_clipping(self):
        from owrx.horus import AUDIO_GAIN
        pcm = self._pcm([0.0, 0.5, -0.5, 5.0, -5.0])
        self.assertEqual(pcm[0], 0)
        self.assertEqual(pcm[1], int(0.5 * AUDIO_GAIN))
        self.assertEqual(pcm[2], -int(0.5 * AUDIO_GAIN))
        self.assertEqual(pcm[3], 32767)
        self.assertEqual(pcm[4], -32767)

    def test_quiet_block_not_normalised(self):
        """A quiet block stays quiet (v5.0.0 scaled every block to full scale)."""
        pcm = self._pcm([0.001, -0.001])
        self.assertLess(max(abs(x) for x in pcm), 100)


if __name__ == "__main__":
    unittest.main()
