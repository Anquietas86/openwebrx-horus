"""
Integration test: the Horus chain against OpenWebRX+'s real csdr Python code.

Needs an OpenWebRX+ source checkout (for csdr/ and owrx/), pointed at with
OWRX_SRC, e.g.:

    git clone --depth 1 https://github.com/luarvique/openwebrx /tmp/owrx
    OWRX_SRC=/tmp/owrx python -m pytest tests/test_chain.py

pycsdr (the C extension) is replaced by small stand-ins when it is not
installed, so this runs on any machine with horusdemodlib. On Python 3.12+
also install setuptools: upstream's owrx/feature.py still imports distutils. Skipped when
OWRX_SRC is not set.
"""

import array
import enum
import math
import os
import pickle
import queue
import shutil
import sys
import tempfile
import threading
import types
import unittest

OWRX_SRC = os.environ.get("OWRX_SRC")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _install_pycsdr_stubs():
    try:
        import pycsdr  # noqa: F401  (real one available)
        return
    except ImportError:
        pass

    class Format(enum.Enum):
        CHAR = 1
        SHORT = 2
        FLOAT = 3
        COMPLEX_FLOAT = 4
        COMPLEX_SHORT = 5
        COMPLEX_CHAR = 6

    class Reader:
        def __init__(self):
            self.q = queue.Queue()

        def read(self):
            return self.q.get()

        def stop(self):
            self.q.put(None)

    class Writer:
        pass

    class Buffer(Writer):
        def __init__(self, fmt=None):
            self.fmt = fmt
            self.readers = []

        def getReader(self):
            r = Reader()
            self.readers.append(r)
            return r

        def write(self, data):
            for r in self.readers:
                r.q.put(memoryview(bytes(data)))

        def getFormat(self):
            return self.fmt

    class Module:
        def __init__(self, *args, **kwargs):
            pass

    pycsdr = types.ModuleType("pycsdr")
    modules = types.ModuleType("pycsdr.modules")
    types_ = types.ModuleType("pycsdr.types")
    modules.Module, modules.Buffer, modules.Reader, modules.Writer = Module, Buffer, Reader, Writer
    types_.Format = Format
    pycsdr.modules, pycsdr.types = modules, types_
    sys.modules.update({"pycsdr": pycsdr, "pycsdr.modules": modules, "pycsdr.types": types_})


def _horus_audio(sample_rate=48000):
    """One Horus Binary v1 packet as 4FSK float audio, padded with silence."""
    sys.path.insert(0, os.path.join(REPO, "tools"))
    from loopback_test import build_horus_v1_payload, generate_4fsk_audio
    from horusdemodlib.encoder import Encoder

    enc = Encoder()
    encoded, _ = enc.horus_l2_encode_packet(build_horus_v1_payload(payload_id=1, sequence=42))
    symbols = enc.bytes_to_4fsk_symbols(encoded)
    enc.close()
    silence = [0.0] * (sample_rate // 2)
    audio = silence + generate_4fsk_audio(symbols, sample_rate=sample_rate) + silence
    return array.array("f", (0.5 * s for s in audio))


@unittest.skipUnless(OWRX_SRC, "set OWRX_SRC to an OpenWebRX+ source checkout")
class TestHorusChain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_pycsdr_stubs()
        # Overlay this repo's modules onto a copy of upstream's csdr/ and
        # owrx/, exactly as the installers do, and import from that tree.
        cls.tmp = tempfile.mkdtemp(prefix="owrx-horus-test-")
        for pkg in ("csdr", "owrx"):
            shutil.copytree(os.path.join(OWRX_SRC, pkg), os.path.join(cls.tmp, pkg))
        os.makedirs(os.path.join(cls.tmp, "owrx", "chain"), exist_ok=True)
        shutil.copy(os.path.join(REPO, "owrx", "horus.py"), os.path.join(cls.tmp, "owrx"))
        shutil.copy(os.path.join(REPO, "owrx", "chain", "__init__.py"), os.path.join(cls.tmp, "owrx", "chain"))
        shutil.copy(os.path.join(REPO, "owrx", "chain", "horus.py"), os.path.join(cls.tmp, "owrx", "chain"))
        for name in [m for m in sys.modules if m == "owrx" or m.startswith(("owrx.", "csdr"))]:
            del sys.modules[name]
        sys.path.insert(0, cls.tmp)
        from owrx.chain.horus import HorusDemodulatorChain
        cls.Chain = HorusDemodulatorChain

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(cls.tmp)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_framework_contract(self):
        """What owrx/dsp.py and owrx/service need from a secondary demodulator."""
        from csdr.chain.demodulator import (
            ServiceDemodulator, FixedAudioRateChain, DialFrequencyReceiver,
        )
        from pycsdr.types import Format

        chain = self.Chain("horus-binary")
        self.assertIsInstance(chain, ServiceDemodulator)
        self.assertIsInstance(chain, FixedAudioRateChain)
        self.assertIsInstance(chain, DialFrequencyReceiver)
        self.assertEqual(chain.getFixedAudioRate(), 48000)
        self.assertIs(chain.getInputFormat(), Format.FLOAT)
        self.assertIs(chain.getOutputFormat(), Format.CHAR)
        self.assertFalse(chain.isSecondaryFftShown())
        chain.stop()

    def test_decodes_audio_through_the_chain(self):
        from pycsdr.modules import Buffer
        from pycsdr.types import Format

        decoded = []
        got_one = threading.Event()

        class Sink:
            def write(self, data):
                decoded.append(pickle.loads(bytes(data)))
                got_one.set()

        chain = self.Chain("horus-binary")
        chain.setSampleRate(48000)
        chain.setDialFrequency(434200000)

        audio_in = Buffer(Format.FLOAT)
        chain.setReader(audio_in.getReader())
        chain.setWriter(Sink())

        audio = _horus_audio()
        block = 1024           # arbitrary block size, like the real buffers
        for i in range(0, len(audio), block):
            audio_in.write(audio[i:i + block].tobytes())

        self.assertTrue(got_one.wait(10), "no frame decoded")
        chain.stop()

        msg = decoded[0]
        self.assertEqual(msg["mode"], "Horus")
        self.assertEqual(msg["sequence"], 42)
        self.assertAlmostEqual(msg["lat"], -34.9285, places=3)
        self.assertAlmostEqual(msg["lon"], 138.6007, places=3)
        self.assertIn("comment", msg)
        self.assertEqual(msg["source"], msg["callsign"])

    def test_service_resampler_never_below_modem_rate(self):
        """Background decoding groups nearby dials behind a Resampler whose
        rate is derived from each mode's get_bandwidth(). It must never fall
        below the chain's fixed 48 kHz, or the Selector can't upsample and the
        modem gets audio at the wrong rate (every frame fails CRC)."""
        import subprocess
        tree = tempfile.mkdtemp(prefix="owrx-horus-modes-")
        try:
            shutil.copytree(os.path.join(OWRX_SRC, "owrx"), os.path.join(tree, "owrx"))
            subprocess.run(
                [sys.executable, os.path.join(REPO, "docker-patch.py"), tree],
                check=True, capture_output=True,
            )
            out = subprocess.run(
                [sys.executable, "-I", "-c",
                 "import sys; sys.path.insert(0, sys.argv[1]);"
                 "from owrx.modes import Modes;"
                 "print(*[m.get_bandwidth() for m in Modes.mappings"
                 " if m.modulation.startswith('horus-')])", tree],
                check=True, capture_output=True, text=True,
            ).stdout.split()
        finally:
            shutil.rmtree(tree, ignore_errors=True)

        self.assertEqual(len(out), 2, "both Horus modes registered")
        for bw in map(float, out):
            # Worst case: a lone dial in a group (upstream floors at 25 kHz).
            self.assertGreaterEqual(max(bw * 1.15, 25000), 48000)

    def test_stop_unblocks_reader(self):
        from pycsdr.modules import Buffer
        from pycsdr.types import Format

        chain = self.Chain("horus-binary")
        buf = Buffer(Format.FLOAT)
        chain.setReader(buf.getReader())
        chain.setWriter(Buffer(Format.CHAR))
        chain.stop()
        chain.module.join(2)
        self.assertFalse(chain.module.is_alive())


if __name__ == "__main__":
    unittest.main()
