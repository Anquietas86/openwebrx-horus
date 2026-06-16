"""
Standalone tests for the Horus decoder integration.

Run without OpenWebRX — validates that horusdemodlib works correctly
and our wrapper produces the expected output format.
"""

import json
import struct
import unittest
from unittest.mock import MagicMock, patch


class TestHorusDemodulatorInit(unittest.TestCase):
    """Test that HorusDemodulator initializes correctly with horusdemodlib."""

    def test_import_horusdemodlib(self):
        from horusdemodlib.demod import HorusLib, Mode, Frame
        from horusdemodlib.decoder import decode_packet
        self.assertIsNotNone(HorusLib)
        self.assertIsNotNone(decode_packet)

    def test_mode_mapping(self):
        from owrx.horus import HORUS_MODES
        from horusdemodlib.demod import Mode
        self.assertEqual(HORUS_MODES["horus_binary"], Mode.BINARY)
        self.assertEqual(HORUS_MODES["horus_rtty"], Mode.RTTY_7N2)

    def test_demodulator_create_close(self):
        from owrx.horus import HorusDemodulator
        demod = HorusDemodulator(mode_str="horus_binary")
        self.assertIsNotNone(demod._demod)
        demod.close()
        self.assertIsNone(demod._demod)


class TestHorusDecoder(unittest.TestCase):
    """Test packet decoding against known-good test vectors."""

    def test_decode_v2_packet(self):
        """Decode a real Horus v2 packet using the loopback test payload."""
        from horusdemodlib.decoder import decode_packet

        # Real v2 payload from loopback_test.py: payload_id=1, seq=42,
        # time=12:34:56, lat=-34.9285, lon=138.6007, alt=25000m,
        # speed=5, sats=10, temp=236 (raw), battery=37 (raw)
        test_hex = "012a000c2238c9b60bc2c7990a43a861050aec25a402"
        test_bytes = bytes.fromhex(test_hex)
        result = decode_packet(test_bytes)
        self.assertIsInstance(result, dict)
        # payload_id=1 resolves to "HORUSBINARY" in horusdemodlib's lookup
        self.assertEqual(result.get("callsign"), "HORUSBINARY")
        self.assertEqual(result.get("sequence_number"), 42)
        self.assertAlmostEqual(result.get("latitude", 0), -34.9285, places=3)
        self.assertAlmostEqual(result.get("longitude", 0), 138.6007, places=3)
        self.assertEqual(result.get("altitude"), 25000)
        self.assertTrue(result.get("crc_ok"))

    def test_output_format(self):
        """Verify our parser produces the expected output structure."""
        mock_data = {
            "callsign": "VK5ARG",
            "sequence_number": 42,
            "latitude": -34.9285,
            "longitude": 138.6007,
            "altitude": 30000,
            "snr": 12.5,
            "crc_ok": True,
            "modulation": "horus_binary_v2",
            "temperature": 23.5,
            "battery": 3.7,
        }

        from owrx.horus import format_horus_telemetry

        out = format_horus_telemetry(mock_data)

        self.assertEqual(out["mode"], "Horus")
        self.assertEqual(out["callsign"], "VK5ARG")
        self.assertEqual(out["lat"], -34.9285)
        self.assertEqual(out["lon"], 138.6007)
        self.assertEqual(out["altitude"], 30000)
        self.assertEqual(out["snr"], 12.5)
        self.assertEqual(out["temperature"], 23.5)
        self.assertEqual(out["battery"], 3.7)
        self.assertIn("timestamp", out)

    def test_output_format_v3_custom_fields(self):
        """Verify v3 custom fields are passed through."""
        mock_data = {
            "callsign": "VK5QI",
            "sequence_number": 1,
            "latitude": -34.0,
            "longitude": 138.0,
            "altitude": 10000,
            "crc_ok": True,
            "custom_field_names": ["ext_temp", "solar_v"],
            "ext_temp": -42.5,
            "solar_v": 4.12,
        }

        from owrx.horus import format_horus_telemetry

        out = format_horus_telemetry(mock_data)

        self.assertEqual(out["ext_temp"], -42.5)
        self.assertEqual(out["solar_v"], 4.12)
        self.assertIn("ext_temp", out["custom_field_names"])
        self.assertIn("solar_v", out["custom_field_names"])


class TestSondehubUploadFormat(unittest.TestCase):
    """Test that telemetry dicts are compatible with telem_to_sondehub."""

    def test_telem_to_sondehub_import(self):
        from horusdemodlib.utils import telem_to_sondehub
        self.assertIsNotNone(telem_to_sondehub)

    def test_sondehub_uploader_import(self):
        from horusdemodlib.sondehubamateur import SondehubAmateurUploader
        self.assertIsNotNone(SondehubAmateurUploader)


class TestHorusLocation(unittest.TestCase):
    """Test map location object."""

    def test_location_dict(self):
        from owrx.horus import HorusLocation

        data = {
            "callsign": "VK5QI",
            "latitude": -34.0,
            "longitude": 138.0,
            "altitude": 25000,
            "sequence_number": 100,
        }
        loc = HorusLocation(data)
        d = loc.__dict__()
        self.assertEqual(d["altitude"], 25000)
        self.assertEqual(d["comment"], "VK5QI")
        self.assertEqual(d["symbol"]["table"], "/")
        self.assertEqual(d["symbol"]["symbol"], "O")


if __name__ == "__main__":
    unittest.main()
