#!/usr/bin/env python3
"""
Idempotent patcher for OpenWebRX+ Python source files.

Adds Horus decoder support to feature.py, modes.py, and service/__init__.py.
Safe to run multiple times — skips files that are already patched.

Usage:
    python3 docker-patch.py /usr/lib/python3/dist-packages
"""

import os
import sys

MARKER = "# openwebrx-horus"


def patch_file(path, check_str, patch_func):
    if not os.path.isfile(path):
        print(f"  SKIP {path} (not found)")
        return False

    with open(path, "r") as f:
        content = f.read()

    if check_str in content:
        print(f"  OK   {os.path.relpath(path)} (already patched)")
        return False

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
        '            underlying="nfm",\n'
        "            bandpass=Bandpass(-4000, 4000),\n"
        "            ifRate=48000,\n"
        '            requirements=["horusdemodlib"],\n'
        "            service=True,\n"
        "        ),\n"
        "        DigitalMode(\n"
        '            modulation="horus_rtty",\n'
        '            name="Horus RTTY",\n'
        '            underlying="usb",\n'
        "            bandpass=Bandpass(300, 3000),\n"
        "            ifRate=48000,\n"
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

    import_block = (
        "{m} BEGIN\n"
        "from owrx.chain.horus import HorusDemodulatorChain\n"
        "from owrx.horus import HorusParser\n"
        "{m} END"
    ).format(m=m)

    lines = content.split("\n")
    last_import_idx = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")) and not stripped.startswith("#"):
            last_import_idx = i

    lines.insert(last_import_idx + 1, import_block)

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
            indent + '    return HorusDemodulatorChain(mode_str="horus_binary")',
            indent + 'elif mod == "horus_rtty":',
            indent + '    return HorusDemodulatorChain(mode_str="horus_rtty")',
            indent + m + " END",
        ]
        for j, dl in enumerate(demod_lines):
            lines.insert(raise_idx + j, dl)

    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 docker-patch.py <owrx_python_path>")
        print("  e.g. python3 docker-patch.py /usr/lib/python3/dist-packages")
        sys.exit(1)

    base = sys.argv[1]
    print(f"Patching OpenWebRX at {base}...")

    patch_file(
        os.path.join(base, "owrx", "feature.py"),
        "horusdemodlib",
        patch_feature,
    )

    patch_file(
        os.path.join(base, "owrx", "modes.py"),
        "horus_binary",
        patch_modes,
    )

    patch_file(
        os.path.join(base, "owrx", "service", "__init__.py"),
        "horus_binary",
        patch_service,
    )

    print("Done.")


if __name__ == "__main__":
    main()
