"""
The installer (horus_patch.py) against a real OpenWebRX+ source tree.

Needs OWRX_SRC (see tests/test_chain.py). This is the test that catches an
upstream OpenWebRX+ change breaking the patches; CI runs it nightly against
the latest upstream.
"""

import filecmp
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

OWRX_SRC = os.environ.get("OWRX_SRC")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATCHER = os.path.join(REPO, "horus_patch.py")
PATCHED = ["owrx/feature.py", "owrx/modes.py", "owrx/service/__init__.py", "owrx/dsp.py"]


def run(*args):
    r = subprocess.run([sys.executable, PATCHER, *args], capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def snapshot(base):
    out = {}
    for root, _, files in os.walk(base):
        for f in files:
            p = os.path.join(root, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, base)] = fh.read()
    return out


@unittest.skipUnless(OWRX_SRC, "set OWRX_SRC to an OpenWebRX+ source checkout")
class TestInstaller(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="owrx-horus-install-")
        self.pristine = os.path.join(self.tmp, "pristine")
        self.tree = os.path.join(self.tmp, "tree")
        for d in (self.pristine, self.tree):
            for pkg in ("owrx", "htdocs"):
                shutil.copytree(os.path.join(OWRX_SRC, pkg), os.path.join(d, pkg))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def install(self):
        code, out = run("install", self.tree)
        self.assertEqual(code, 0, out)
        return out

    def test_install_applies_every_patch(self):
        self.install()
        for rel in PATCHED:
            with open(os.path.join(self.tree, rel)) as f:
                content = f.read()
            self.assertIn("# openwebrx-horus BEGIN", content, rel)
            compile(content, rel, "exec")
        for rel in ("owrx/horus.py", "owrx/chain/horus.py",
                    "htdocs/plugins/receiver/horus/horus.js",
                    "htdocs/plugins/receiver/horus/horus.css"):
            self.assertTrue(os.path.isfile(os.path.join(self.tree, rel)), rel)
        with open(os.path.join(self.tree, "htdocs/plugins/receiver/init.js")) as f:
            self.assertEqual(f.read().count("Plugins.load('horus');"), 1)

    def test_modes_register_and_match_backend(self):
        self.install()
        out = subprocess.run(
            [sys.executable, "-I", "-c",
             "import sys; sys.path.insert(0, sys.argv[1]);"
             "from owrx.modes import Modes;"
             "print(*['%s:%s' % (m.modulation, m.secondaryFft) for m in Modes.mappings"
             " if m.modulation.startswith('horus-')])", self.tree],
            check=True, capture_output=True, text=True,
        ).stdout.split()
        sys.path.insert(0, REPO)
        try:
            from owrx.horus import HORUS_MODES
        finally:
            sys.path.remove(REPO)
        self.assertEqual(sorted(m.split(":")[0] for m in out), sorted(HORUS_MODES))
        self.assertTrue(all(m.endswith(":True") for m in out), out)

    def test_second_run_changes_nothing(self):
        self.install()
        before = snapshot(self.tree)
        out = self.install()
        self.assertEqual(snapshot(self.tree), before)
        self.assertNotIn("DONE", out)
        self.assertNotIn("COPY", out)

    def test_uninstall_restores_upstream(self):
        self.install()
        code, out = run("uninstall", self.tree)
        self.assertEqual(code, 0, out)
        for rel in PATCHED + ["htdocs/openwebrx.js", "htdocs/index.html"]:
            self.assertTrue(
                filecmp.cmp(os.path.join(self.pristine, rel), os.path.join(self.tree, rel), shallow=False),
                rel + " differs from upstream after uninstall",
            )
        self.assertFalse(os.path.exists(os.path.join(self.tree, "owrx/horus.py")))
        self.assertFalse(os.path.exists(os.path.join(self.tree, "htdocs/plugins/receiver/horus")))

    def test_upgrade_from_old_mode_names(self):
        """Blocks from v5.0.0 and earlier (horus_binary / horus-binary ==) are replaced."""
        rel = "owrx/modes.py"
        path = os.path.join(self.tree, rel)
        with open(path) as f:
            content = f.read()
        idx = content.rindex("\n    ]\n") + 1   # end of Modes.mappings
        self.assertIn("    ]\n", content[idx:idx + 6])
        old = ('        # openwebrx-horus BEGIN\n'
               '        DigitalMode("horus_binary", "Horus Binary", underlying=["usb"]),\n'
               '        # openwebrx-horus END\n')
        with open(path, "w") as f:
            f.write(content[:idx] + old + content[idx:])
        self.install()
        with open(path) as f:
            patched = f.read()
        self.assertNotIn("horus_binary", patched)
        self.assertEqual(patched.count("# openwebrx-horus BEGIN"), 1)

    def test_legacy_docker_patch_entry_point(self):
        r = subprocess.run([sys.executable, os.path.join(REPO, "docker-patch.py"), self.tree],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(self.tree, "owrx/dsp.py")) as f:
            self.assertIn("horus-binary", f.read())

    def test_fails_loudly_when_upstream_layout_changes(self):
        path = os.path.join(self.tree, "owrx/service/__init__.py")
        with open(path) as f:
            content = f.read()
        with open(path, "w") as f:
            f.write(content.replace('raise ValueError("unsupported service modulation', 'raise ValueError("changed'))
        code, out = run("install", self.tree)
        self.assertNotEqual(code, 0)
        self.assertIn("did not apply", out)


if __name__ == "__main__":
    unittest.main()
