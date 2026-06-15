#!/usr/bin/env python3
"""
Idempotent patcher for OpenWebRX+ Python source files.

Adds Horus decoder support to feature.py, modes.py, and service/__init__.py.
Safe to run multiple times — strips any existing patches first, then re-applies.

Usage:
    python3 docker-patch.py /usr/lib/python3/dist-packages
"""

import os
import re
import sys

MARKER = "# openwebrx-horus"
MARKER_BEGIN = MARKER + " BEGIN"
MARKER_END = MARKER + " END"
HTML_MARKER_BEGIN = "<!-- openwebrx-horus BEGIN -->"
HTML_MARKER_END = "<!-- openwebrx-horus END -->"


def strip_existing_patches(content):
    """Remove any existing openwebrx-horus marker blocks from content."""
    lines = content.split("\n")
    cleaned = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if MARKER_BEGIN in stripped or HTML_MARKER_BEGIN in stripped:
            skipping = True
            continue
        if MARKER_END in stripped or HTML_MARKER_END in stripped:
            skipping = False
            continue
        if not skipping:
            cleaned.append(line)
    return "\n".join(cleaned)


def patch_file(path, patch_func):
    if not os.path.isfile(path):
        print(f"  SKIP {path} (not found)")
        return False

    with open(path, "r") as f:
        content = f.read()

    # Always strip existing patches first for a clean slate
    content = strip_existing_patches(content)

    content = patch_func(content)

    with open(path, "w") as f:
        f.write(content)

    print(f"  DONE {os.path.relpath(path)}")
    return True


def patch_feature(content):
    m = MARKER

    feature_entry = (
        "\n"
        "        {m} BEGIN\n"
        '        "horusdemodlib": ["horusdemodlib"],\n'
        "        {m} END"
    ).format(m=m)

    lines = content.split("\n")
    in_features = False
    last_entry_idx = None
    brace_depth = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if "features" in line and "{" in line and "=" in line and not in_features:
            in_features = True
            brace_depth = line.count("{") - line.count("}")
            continue
        if in_features:
            brace_depth += line.count("{") - line.count("}")
            if stripped.startswith('"') and ":" in stripped:
                last_entry_idx = i
            if brace_depth <= 0:
                break

    if last_entry_idx is not None:
        lines.insert(last_entry_idx + 1, feature_entry)

    method = (
        "\n"
        "    {m} BEGIN\n"
        "    def has_horusdemodlib(self):\n"
        "        try:\n"
        "            from horusdemodlib.demod import HorusLib, Mode\n"
        "            test = HorusLib(mode=Mode.BINARY, sample_rate=48000)\n"
        "            test.close()\n"
        "            return True\n"
        "        except Exception:\n"
        "            return False\n"
        "    {m} END"
    ).format(m=m)

    content = "\n".join(lines)
    content = content.rstrip() + "\n" + method + "\n"
    return content


def patch_modes(content):
    m = MARKER

    new_modes = (
        "        {m} BEGIN\n"
        "        DigitalMode(\n"
        '            modulation="horus_binary",\n'
        '            name="Horus Binary",\n'
        '            underlying=["nfm"],\n'
        "            bandpass=Bandpass(-4000, 4000),\n"
        '            requirements=["horusdemodlib"],\n'
        "            service=True,\n"
        "            squelch=False,\n"
        "        ),\n"
        "        DigitalMode(\n"
        '            modulation="horus_rtty",\n'
        '            name="Horus RTTY",\n'
        '            underlying=["usb"],\n'
        "            bandpass=Bandpass(300, 3000),\n"
        '            requirements=["horusdemodlib"],\n'
        "            service=True,\n"
        "        ),\n"
        "        {m} END"
    ).format(m=m)

    lines = content.split("\n")
    insert_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == "]":
            insert_idx = i
            break

    if insert_idx is not None:
        lines.insert(insert_idx, new_modes)

    return "\n".join(lines)


def patch_service(content):
    m = MARKER

    # Use inline imports inside the elif branches — matches the existing
    # pattern in this file (e.g. `from csdr.chain.satellite import ...`
    # inside elif blocks) and avoids needing owrx.chain to be importable
    # at module load time.

    lines = content.split("\n")

    # Find the raise ValueError line and detect its indentation
    raise_idx = None
    indent = ""
    for i, line in enumerate(lines):
        if 'raise ValueError("unsupported service modulation' in line:
            raise_idx = i
            indent = line[: len(line) - len(line.lstrip())]
            break

    if raise_idx is not None:
        demod_lines = [
            indent + m + " BEGIN",
            indent + 'elif mod == "horus_binary":',
            indent + "    from owrx.chain.horus import HorusDemodulatorChain",
            indent + "    return HorusDemodulatorChain(mode_str=\"horus_binary\")",
            indent + 'elif mod == "horus_rtty":',
            indent + "    from owrx.chain.horus import HorusDemodulatorChain",
            indent + "    return HorusDemodulatorChain(mode_str=\"horus_rtty\")",
            indent + m + " END",
        ]
        for j, dl in enumerate(demod_lines):
            lines.insert(raise_idx + j, dl)

    return "\n".join(lines)


def patch_dsp(content):
    """Patch dsp.py: fix validator regex + add Horus to _getSecondaryDemodulator."""
    m = MARKER

    # 1. Extend ModulationValidator regex to allow underscores
    old = r'"^[a-z0-9\\-]+$"'
    new = r'"^[a-z0-9_\\-]+$"'
    if old in content:
        content = content.replace(old, new, 1)

    # 2. Add Horus demodulators to _getSecondaryDemodulator()
    #    Insert before the end of the elif chain (before setSecondaryDemodulator method)
    lines = content.split("\n")
    insert_idx = None
    indent = ""
    for i, line in enumerate(lines):
        if "def setSecondaryDemodulator(self, mod):" in line:
            insert_idx = i
            break

    if insert_idx is not None:
        # Walk back to find the indentation of the elif blocks
        for j in range(insert_idx - 1, -1, -1):
            stripped = lines[j].strip()
            if stripped.startswith("elif mod ==") or stripped.startswith("return "):
                indent = lines[j][: len(lines[j]) - len(lines[j].lstrip())]
                if stripped.startswith("return "):
                    # Use the elif indent (one level less)
                    indent = indent[:-4] if indent.endswith("    ") else indent
                break

        demod_block = [
            indent + m + " BEGIN",
            indent + 'elif mod == "horus_binary":',
            indent + "    from owrx.chain.horus import HorusDemodulatorChain",
            indent + "    return HorusDemodulatorChain(mode_str=\"horus_binary\")",
            indent + 'elif mod == "horus_rtty":',
            indent + "    from owrx.chain.horus import HorusDemodulatorChain",
            indent + "    return HorusDemodulatorChain(mode_str=\"horus_rtty\")",
            indent + m + " END",
            "",
        ]
        for j, dl in enumerate(demod_block):
            lines.insert(insert_idx + j, dl)

    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 docker-patch.py <owrx_python_path>")
        print("  e.g. python3 docker-patch.py /usr/lib/python3/dist-packages")
        sys.exit(1)

    base = sys.argv[1]
    print(f"[openwebrx-horus] Patching OpenWebRX at {base}...")

    patch_file(
        os.path.join(base, "owrx", "feature.py"),
        patch_feature,
    )

    patch_file(
        os.path.join(base, "owrx", "modes.py"),
        patch_modes,
    )

    patch_file(
        os.path.join(base, "owrx", "service", "__init__.py"),
        patch_service,
    )

    patch_file(
        os.path.join(base, "owrx", "dsp.py"),
        patch_dsp,
    )

    print("[openwebrx-horus] Patching complete.")


if __name__ == "__main__":
    main()
