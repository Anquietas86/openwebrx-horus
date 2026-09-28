#!/usr/bin/env python3
"""
Idempotent patcher for OpenWebRX+ Python source files, plus frontend cleanup.

v4.0.0 patches NO framework JavaScript. The plugin builds its telemetry window
with the official plugin JS API (Plugins.addWindow / addButton), added in
OpenWebRX+ 1.2.124, so the frontend needs no source patching at all.

Adds Horus decoder support to:
  - owrx/feature.py
  - owrx/modes.py
  - owrx/service/__init__.py
  - owrx/dsp.py

And RESTORES / cleans, for installs upgrading from v3.x:
  - htdocs/openwebrx.js   -> 'horus' removed from the hardcoded panel list
  - htdocs/plugins.js     -> v3.x cache-bust stripped
  - htdocs/index.html     -> v3.x panel div + standalone handler stripped
  - htdocs/css/custom.css -> created empty if missing (stops a per-load 404)

Safe to run multiple times.

Usage:
    python3 docker-patch.py /usr/lib/python3/dist-packages
"""

import os
import re
import sys

# Python/HTML marker
MARKER = "# openwebrx-horus"
MARKER_BEGIN = MARKER + " BEGIN"
MARKER_END = MARKER + " END"

# HTML comment marker
HTML_MARKER_BEGIN = "<!-- openwebrx-horus BEGIN -->"
HTML_MARKER_END = "<!-- openwebrx-horus END -->"

# JavaScript marker (MUST use // not # — # is invalid JS syntax)
JS_MARKER = "// openwebrx-horus"
JS_MARKER_BEGIN = JS_MARKER + " BEGIN"
JS_MARKER_END = JS_MARKER + " END"


def strip_existing_patches_py(content):
    """Remove # openwebrx-horus and <!-- openwebrx-horus --> marker blocks."""
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


def strip_existing_patches_js(content):
    """Remove // openwebrx-horus marker blocks from JavaScript files."""
    lines = content.split("\n")
    cleaned = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if JS_MARKER_BEGIN in stripped:
            skipping = True
            continue
        if JS_MARKER_END in stripped:
            skipping = False
            continue
        if not skipping:
            cleaned.append(line)
    return "\n".join(cleaned)


def strip_existing_patches_html(content):
    """Remove <!-- openwebrx-horus --> marker blocks from HTML files."""
    lines = content.split("\n")
    cleaned = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if HTML_MARKER_BEGIN in stripped:
            skipping = True
            continue
        if HTML_MARKER_END in stripped:
            skipping = False
            continue
        if not skipping:
            cleaned.append(line)
    return "\n".join(cleaned)


def patch_file(path, patch_func, strip_func=None):
    if not os.path.isfile(path):
        print(f"  SKIP {path} (not found)")
        return False

    with open(path, "r") as f:
        content = f.read()

    # Always strip existing patches first for a clean slate
    if strip_func:
        content = strip_func(content)
    else:
        content = strip_existing_patches_py(content)

    content = patch_func(content)

    # Atomic write: write to temp file, then replace
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        f.write(content)
    os.replace(tmp_path, path)

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
        '            underlying=["usb"],\n'
        "            bandpass=Bandpass(100, 4000),\n"
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

    lines = content.split("\n")

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
            indent + '    return HorusDemodulatorChain(mode_str="horus_binary")',
            indent + 'elif mod == "horus_rtty":',
            indent + "    from owrx.chain.horus import HorusDemodulatorChain",
            indent + '    return HorusDemodulatorChain(mode_str="horus_rtty")',
            indent + m + " END",
        ]
        for j, dl in enumerate(demod_lines):
            lines.insert(raise_idx + j, dl)

    return "\n".join(lines)


def patch_dsp(content):
    """Patch dsp.py: fix validator regex + add Horus to _getSecondaryDemodulator."""
    m = MARKER

    # 1. Fix ModulationValidator regex to allow underscores for horus modulations
    old = r'"^[a-z0-9\-]+$"'
    new = r'"^[a-z0-9_\-]+$"'
    if old in content:
        content = content.replace(old, new, 1)

    # 2. Add Horus demodulators to _getSecondaryDemodulator()
    lines = content.split("\n")
    insert_idx = None
    indent = ""
    for i, line in enumerate(lines):
        if "def setSecondaryDemodulator(self, mod):" in line:
            insert_idx = i
            break

    if insert_idx is not None:
        for j in range(insert_idx - 1, -1, -1):
            stripped = lines[j].strip()
            if stripped.startswith("elif mod ==") or stripped.startswith("return "):
                indent = lines[j][: len(lines[j]) - len(lines[j].lstrip())]
                if stripped.startswith("return "):
                    indent = indent[:-4] if indent.endswith("    ") else indent
                break

        demod_block = [
            indent + m + " BEGIN",
            indent + 'elif mod == "horus_binary":',
            indent + "    from owrx.chain.horus import HorusDemodulatorChain",
            indent + '    return HorusDemodulatorChain(mode_str="horus_binary")',
            indent + 'elif mod == "horus_rtty":',
            indent + "    from owrx.chain.horus import HorusDemodulatorChain",
            indent + '    return HorusDemodulatorChain(mode_str="horus_rtty")',
            indent + m + " END",
            "",
        ]
        for j, dl in enumerate(demod_block):
            lines.insert(insert_idx + j, dl)

    return "\n".join(lines)


def clean_file(path, clean_func):
    """Rewrite a file only if clean_func actually changes it."""
    if not os.path.isfile(path):
        print(f"  SKIP {os.path.relpath(path)} (not found)")
        return False

    with open(path, "r") as f:
        content = f.read()

    cleaned = clean_func(content)
    if cleaned == content:
        print(f"  OK   {os.path.relpath(path)} (no legacy patch)")
        return False

    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        f.write(cleaned)
    os.replace(tmp_path, path)
    print(f"  CLEANED {os.path.relpath(path)}")
    return True


def clean_plugins_js(content):
    """Strip the v3.x ?v= cache-bust that older patchers added for horus.js.

    Obsolete in v4.0.0: the plugin is a normal API plugin, so no cache-busting
    of the loader is required.
    """
    return strip_existing_patches_js(content)


def clean_index_html(content):
    """Strip every v3.x index.html patch.

    v3.0.0 added a horus panel div and a standalone handler <script>, and
    cache-busted receiver.js / plugins.js. v4.0.0 uses Plugins.addWindow() and
    needs none of it, so both are removed and the script URLs revert to the
    upstream form.
    """
    content = strip_existing_patches_html(content)
    content = re.sub(r'src="compiled/receiver\.js(?:\?v=[^"]+)?"',
                     'src="compiled/receiver.js"', content)
    content = re.sub(r'src="static/plugins\.js(?:\?v=[^"]+)?"',
                     'src="static/plugins.js"', content)
    return content


def restore_openwebrx_js(content):
    """Restore htdocs/openwebrx.js to upstream — no 'horus' in the panel list.

    v3.x injected 'horus' into the hardcoded panel array. v4.0.0 does not want
    it: with 'horus' absent, openwebrx.js falls through to
    secondary_demod_push_data(), which is the plugin's single routing path. A
    leftover entry is actively harmful — the v4 plugin defines no
    $.fn.horusMessagePanel, so the framework would call an undefined function,
    the handler would throw, and every secondary_demod message would be lost.

    The panel-init expression is REBUILT from a canonical upstream block rather
    than having the marker lines deleted: deleting them removes the
    `var panels = ...` line itself and orphans the following `return`/`});`,
    producing exactly the broken-bundle syntax error this patch used to cause.
    Rebuilding also drops stale fragments left by older non-idempotent
    patchers, and clears any marker comments inside the case body.
    """
    lines = content.split("\n")

    case_idx = None
    for i, line in enumerate(lines):
        if "case 'secondary_demod':" in line or 'case "secondary_demod":' in line:
            case_idx = i
            break
    if case_idx is None:
        print("  WARN: secondary_demod case not found in openwebrx.js — skipping")
        return content

    break_idx = None
    for i in range(case_idx + 1, min(case_idx + 80, len(lines))):
        if lines[i].strip() == "break;":
            break_idx = i
            break
    if break_idx is None:
        print("  WARN: secondary_demod break not found — skipping")
        return content

    case_indent = lines[case_idx][: len(lines[case_idx]) - len(lines[case_idx].lstrip())]
    body_indent = case_indent + "    "

    # Preserve the real value line if present; otherwise regenerate it.
    value_line = None
    for i in range(case_idx + 1, min(case_idx + 6, break_idx)):
        if "var value = json" in lines[i]:
            value_line = lines[i]
            break
    if value_line is None:
        value_line = body_indent + "var value = json['value'];"

    # Keep everything from panels.push(...) to break; — that is the dispatch
    # tail, which is upstream code we must not touch.
    tail = []
    keep = False
    for i in range(case_idx + 1, break_idx + 1):
        if not keep and lines[i].strip().startswith("panels.push("):
            keep = True
        if keep:
            tail.append(lines[i])

    if not tail:
        print("  WARN: panels.push(...) tail not found — skipping openwebrx.js")
        return content

    canonical = [
        body_indent + "var panels = ['wsjt', 'packet', 'pocsag', 'page', 'sstv', 'fax', 'ism', 'hfdl', 'adsb', 'dsc', 'skimmer', 'meshtastic'].map(function(id) {",
        body_indent + "    return $('#openwebrx-panel-' + id + '-message')[id + 'MessagePanel']();",
        body_indent + "});",
    ]

    new_block = [lines[case_idx], value_line] + canonical + tail
    result = "\n".join(lines[:case_idx] + new_block + lines[break_idx + 1:])

    # Validate before handing the result back. A malformed panel-init here
    # breaks the entire compiled bundle, so refuse to write in that case.
    panels_lines = [l for l in result.split("\n") if "var panels = [" in l]
    if len(panels_lines) != 1:
        print(f"  ERROR: expected 1 panel-init line, found {len(panels_lines)} — file left unchanged")
        return content
    if "'horus'" in panels_lines[0] or ".map(" not in panels_lines[0]:
        print("  ERROR: panel-init line invalid — file left unchanged")
        return content

    print("  openwebrx.js: panel list restored to upstream (no 'horus')")
    return result


# ---------------------------------------------------------------------------
# v4.0.0: index.html is no longer patched
#
# v3.0.0 injected a horus panel div and a standalone handler <script> into
# index.html, because the jQuery-widget panel route needed its DOM element to
# exist before the framework's routing fired. v4.0.0 builds the telemetry
# window with Plugins.addWindow(), which creates everything it needs at
# runtime, so index.html is never modified -- only cleaned of the old patch.


def cleanup_frontend(base):
    """Restore every framework frontend asset to upstream.

    Shared by the normal patch run (which must strip any v3.x residue) and by
    --restore-frontend, which is what the Docker uninstaller calls.

    The uninstaller cannot simply delete the v3.x marker blocks: for
    openwebrx.js the block WRAPPED the `var panels = ...` line, so deleting it
    orphans the trailing panels.push()/dispatch and leaves a file that throws
    on every load. restore_openwebrx_js() rebuilds the case body instead.
    """
    patch_file(
        os.path.join(base, "htdocs", "openwebrx.js"),
        restore_openwebrx_js,
        strip_func=lambda c: c,
    )

    clean_file(os.path.join(base, "htdocs", "plugins.js"), clean_plugins_js)
    clean_file(os.path.join(base, "htdocs", "index.html"), clean_index_html)

    # OpenWebRX+ unconditionally requests /static/css/custom.css. NOTE the URL
    # prefix: /static/ maps to htdocs/, NOT htdocs/static/ (plugins are served
    # from /static/plugins/receiver/... out of htdocs/plugins/receiver/...).
    # Without this file the browser logs a 404 on every page load.
    custom_css = os.path.join(base, "htdocs", "css", "custom.css")
    if not os.path.isfile(custom_css):
        os.makedirs(os.path.dirname(custom_css), exist_ok=True)
        open(custom_css, "w").close()
        print("  CREATED htdocs/css/custom.css (stops a per-load 404)")


def main():
    args = sys.argv[1:]

    # Uninstall support: restore the frontend only, leaving Python sources to
    # the caller (which re-installs the stock package files anyway).
    if args and args[0] == "--restore-frontend":
        if len(args) < 2:
            print("Usage: python3 docker-patch.py --restore-frontend <owrx_python_path>")
            sys.exit(1)
        print(f"[openwebrx-horus] Restoring OpenWebRX frontend at {args[1]}...")
        cleanup_frontend(args[1])
        print("[openwebrx-horus] Frontend restore complete.")
        return

    if not args:
        print("Usage: python3 docker-patch.py <owrx_python_path>")
        print("       python3 docker-patch.py --restore-frontend <owrx_python_path>")
        print("  e.g. python3 docker-patch.py /usr/lib/python3/dist-packages")
        sys.exit(1)

    base = args[0]
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

    # Frontend — v4.0.0 patches NO framework JavaScript; it only restores
    # whatever v3.x left behind (and creates custom.css if missing).
    cleanup_frontend(base)

    print("[openwebrx-horus] Patching complete.")


if __name__ == "__main__":
    main()