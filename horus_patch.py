#!/usr/bin/env python3
"""
openwebrx-horus installer: the ONE place that knows how to patch OpenWebRX+.

install.sh (bare metal), install-docker.sh and the Docker Compose override all
call this script, so the patch logic exists exactly once.

    python3 horus_patch.py install   [--pip] <owrx_path>
    python3 horus_patch.py uninstall <owrx_path>
    python3 horus_patch.py restore-frontend <owrx_path>

<owrx_path> is the directory holding owrx/ and htdocs/:
/usr/lib/python3/dist-packages for the Debian package and the Docker image,
or the checkout directory for a source install.

install:
  - copies owrx/horus.py, owrx/chain/horus.py and the frontend plugin
  - registers the plugin in htdocs/plugins/receiver/init.js
  - patches owrx/feature.py, owrx/modes.py, owrx/service/__init__.py and
    owrx/dsp.py (marker blocks; every run strips and re-inserts them)
  - with --pip, installs horusdemodlib if it is missing, and fails loudly
    if that does not work
  - cleans up what v3.x left in htdocs (openwebrx.js, plugins.js,
    index.html) and creates htdocs/css/custom.css if missing

It patches NO framework JavaScript. Files are only rewritten when their
content actually changes, and each is backed up once to <file>.pre-horus
before the first change. Safe to run on every container start.
"""

import os
import re
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.abspath(__file__))

# Mode name -> (display name, bandpass low, bandpass high). Keep in step with
# HORUS_MODES in owrx/horus.py (tests/test_installer.py checks it).
HORUS_MODES = [
    ("horus-binary", "Horus Binary", 100, 4000),
    ("horus-rtty", "Horus RTTY (7N2)", 300, 3000),
    ("horus-rtty-7n1", "Horus RTTY (7N1)", 300, 3000),
    ("horus-rtty-8n2", "Horus RTTY (8N2)", 300, 3000),
]
HORUS_MODE_NAMES = tuple(m[0] for m in HORUS_MODES)

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
    return write_if_changed(path, content)


def write_if_changed(path, content):
    """Atomically write content, but only if it differs from the file.

    Backs the original up to <path>.pre-horus before the first change.
    """
    with open(path, "r") as f:
        current = f.read()
    if content == current:
        print(f"  OK   {path} (unchanged)")
        return False
    backup = path + ".pre-horus"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        f.write(content)
    shutil.copymode(path, tmp_path)
    os.replace(tmp_path, path)
    print(f"  DONE {path}")
    return True


def patch_feature(content):
    m = MARKER

    feature_entry = (
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
    body = content.rstrip()
    # Keep the file's own trailing whitespace and add no blank lines outside
    # the markers, so uninstall gives back the original byte for byte.
    return body + "\n" + method + content[len(body):]


def patch_modes(content):
    """Add the Horus modes to Modes.mappings.

    ifRate=48000 matters for background decoding: OpenWebRX+ groups nearby
    service dials behind a Resampler whose rate comes from each mode's
    get_bandwidth() (bandpass, or ifRate if larger). Without it a Horus dial
    near other dials can land on a resampler slower than the modem's fixed
    48 kHz; the Selector then cannot upsample, silently delivers audio at the
    lower rate, and every frame fails CRC.
    """
    m = MARKER

    entries = []
    for mod, name, low, high in HORUS_MODES:
        entries.append(
            "        DigitalMode(\n"
            '            modulation="{mod}",\n'
            '            name="{name}",\n'
            '            underlying=["usb"],\n'
            "            bandpass=Bandpass({low}, {high}),\n"
            "            ifRate=48000,\n"
            '            requirements=["horusdemodlib"],\n'
            "            service=True,\n"
            "            squelch=False,\n"
            "            secondaryFft=True,\n"
            "        ),\n".format(mod=mod, name=name, low=low, high=high)
        )
    new_modes = "        {m} BEGIN\n".format(m=m) + "".join(entries) + "        {m} END".format(m=m)

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
            indent + "elif mod in {names!r}:".format(names=HORUS_MODE_NAMES),
            indent + "    from owrx.chain.horus import HorusDemodulatorChain",
            indent + "    return HorusDemodulatorChain(mode_str=mod, service=True)",
            indent + m + " END",
        ]
        for j, dl in enumerate(demod_lines):
            lines.insert(raise_idx + j, dl)

    return "\n".join(lines)


def patch_dsp(content):
    """Patch dsp.py: add Horus to _getSecondaryDemodulator().

    No ModulationValidator change is needed: the modes are named
    horus-binary / horus-rtty, which the stock [a-z0-9-] regex accepts.
    Older releases used underscores and tried to widen that regex, but the
    replace never matched upstream's source, so the browser rejected them.
    """
    m = MARKER

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
            indent + "elif mod in {names!r}:".format(names=HORUS_MODE_NAMES),
            indent + "    from owrx.chain.horus import HorusDemodulatorChain",
            indent + "    return HorusDemodulatorChain(mode_str=mod)",
            indent + m + " END",
        ]
        for j, dl in enumerate(demod_block):
            lines.insert(insert_idx + j, dl)

    return "\n".join(lines)


def clean_file(path, clean_func):
    """Rewrite a file only if clean_func actually changes it."""
    if not os.path.isfile(path):
        print(f"  SKIP {path} (not found)")
        return False

    with open(path, "r") as f:
        content = f.read()

    cleaned = clean_func(content)
    if cleaned == content:
        print(f"  OK   {path} (no legacy patch)")
        return False

    return write_if_changed(path, cleaned)


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


def needs_openwebrx_js_restore(content):
    if JS_MARKER in content:
        return True
    return any("var panels = [" in l and "'horus'" in l for l in content.split("\n"))


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
    # Only touch openwebrx.js when a v3.x patch is actually there. The
    # restore rebuilds the panel list from a canonical copy, which would
    # otherwise overwrite upstream's real list on every single run.
    js = os.path.join(base, "htdocs", "openwebrx.js")
    if os.path.isfile(js):
        with open(js) as f:
            js_content = f.read()
        if needs_openwebrx_js_restore(js_content):
            patch_file(js, restore_openwebrx_js, strip_func=lambda c: c)
        else:
            print(f"  OK   {js} (no legacy patch)")

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


# ---------------------------------------------------------------------------
# Files, init.js, pip


INIT_LINE = "Plugins.load('horus');"


def plugin_dir(base):
    return os.path.join(base, "htdocs", "plugins", "receiver")


def copy_if_changed(src, dst):
    if os.path.isfile(dst):
        with open(src, "rb") as a, open(dst, "rb") as b:
            if a.read() == b.read():
                return False
    shutil.copyfile(src, dst)
    print(f"  COPY {dst}")
    return True


def copy_files(base):
    chain_dir = os.path.join(base, "owrx", "chain")
    os.makedirs(chain_dir, exist_ok=True)
    init_py = os.path.join(chain_dir, "__init__.py")
    if not os.path.exists(init_py):
        open(init_py, "w").close()
    copy_if_changed(os.path.join(REPO, "owrx", "horus.py"), os.path.join(base, "owrx", "horus.py"))
    copy_if_changed(os.path.join(REPO, "owrx", "chain", "horus.py"), os.path.join(chain_dir, "horus.py"))

    dest = os.path.join(plugin_dir(base), "horus")
    os.makedirs(dest, exist_ok=True)
    for name in ("horus.js", "horus.css"):
        copy_if_changed(os.path.join(REPO, "plugin", "horus", name), os.path.join(dest, name))


def register_frontend(base):
    init_js = os.path.join(plugin_dir(base), "init.js")
    os.makedirs(os.path.dirname(init_js), exist_ok=True)
    content = ""
    if os.path.isfile(init_js):
        with open(init_js) as f:
            content = f.read()
    if "'horus'" in content or '"horus"' in content:
        print(f"  OK   {init_js} (already loads horus)")
        return
    if content and not content.endswith("\n"):
        content += "\n"
    with open(init_js, "w") as f:
        f.write(content + INIT_LINE + "\n")
    print(f"  DONE {init_js}")


def unregister_frontend(base):
    init_js = os.path.join(plugin_dir(base), "init.js")
    if not os.path.isfile(init_js):
        return
    with open(init_js) as f:
        lines = f.read().split("\n")
    kept = [l for l in lines if not re.search(r"Plugins\.load\(\s*['\"]horus['\"]\s*\)", l)]
    if kept != lines:
        with open(init_js, "w") as f:
            f.write("\n".join(kept))
        print(f"  DONE {init_js} (horus removed)")


def horusdemodlib_importable():
    return subprocess.run(
        [sys.executable, "-c", "import horusdemodlib.demod"],
        capture_output=True,
    ).returncode == 0


def ensure_horusdemodlib():
    """pip-install horusdemodlib for THIS interpreter, system-wide.

    Not --user: under sudo that lands in root's home, where the openwebrx
    service user never looks. Debian 12+ marks the system Python as
    externally managed (PEP 668), so retry with --break-system-packages,
    which is what the OpenWebRX+ image and package installs need.
    """
    if horusdemodlib_importable():
        print("  OK   horusdemodlib already installed")
        return True
    base_cmd = [sys.executable, "-m", "pip", "install", "horusdemodlib"]
    for cmd in (base_cmd, base_cmd + ["--break-system-packages"]):
        print("  RUN  " + " ".join(cmd))
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0 and horusdemodlib_importable():
            print("  DONE horusdemodlib installed")
            return True
        out = (r.stdout + r.stderr).strip()
        if "No module named pip" in out:
            print("  ERROR pip is not available. Install it first (e.g. apt-get install python3-pip).")
            return False
        if "externally-managed" not in out:
            print("  ERROR pip failed:\n" + "\n".join("        " + l for l in out.splitlines()[-15:]))
            return False
    print("  ERROR horusdemodlib still not importable after pip install")
    return False


# ---------------------------------------------------------------------------
# Commands


PY_PATCHES = (
    (("owrx", "feature.py"), patch_feature),
    (("owrx", "modes.py"), patch_modes),
    (("owrx", "service", "__init__.py"), patch_service),
    (("owrx", "dsp.py"), patch_dsp),
)


def check_base(base):
    for rel in (("owrx", "modes.py"), ("owrx", "feature.py"), ("htdocs",)):
        if not os.path.exists(os.path.join(base, *rel)):
            print(f"ERROR: OpenWebRX not found at {base} (missing {os.path.join(*rel)})")
            sys.exit(2)


def verify_patch(base):
    """Fail loudly if a patch did not land, instead of a silent no-op."""
    ok = True
    for rel, _ in PY_PATCHES:
        path = os.path.join(base, *rel)
        if not os.path.isfile(path):
            continue
        with open(path) as f:
            content = f.read()
        if MARKER_BEGIN not in content:
            print(f"  ERROR patch did not apply to {path}: upstream layout changed?")
            ok = False
            continue
        try:
            compile(content, path, "exec")
        except SyntaxError as e:
            print(f"  ERROR {path} no longer compiles after patching: {e}")
            ok = False
    return ok


def cmd_install(base, pip=False):
    check_base(base)
    print(f"[openwebrx-horus] Installing into {base}")
    if pip and not ensure_horusdemodlib():
        return 1
    copy_files(base)
    register_frontend(base)
    for rel, func in PY_PATCHES:
        patch_file(os.path.join(base, *rel), func)
    cleanup_frontend(base)
    if not verify_patch(base):
        print("[openwebrx-horus] Install FAILED, see errors above.")
        return 1
    if not pip and not horusdemodlib_importable():
        print("  WARN horusdemodlib is not importable by this Python; run with --pip or install it.")
    print("[openwebrx-horus] Install complete. Restart OpenWebRX to load it.")
    return 0


def cmd_uninstall(base):
    check_base(base)
    print(f"[openwebrx-horus] Uninstalling from {base}")
    for rel in (("owrx", "horus.py"), ("owrx", "chain", "horus.py")):
        path = os.path.join(base, *rel)
        if os.path.exists(path):
            os.remove(path)
            print(f"  REMOVED {path}")
    dest = os.path.join(plugin_dir(base), "horus")
    if os.path.isdir(dest):
        shutil.rmtree(dest)
        print(f"  REMOVED {dest}/")
    unregister_frontend(base)
    for rel, _ in PY_PATCHES:
        path = os.path.join(base, *rel)
        if os.path.isfile(path):
            with open(path) as f:
                content = f.read()
            write_if_changed(path, strip_existing_patches_py(content))
    cleanup_frontend(base)
    print("[openwebrx-horus] Uninstall complete. Restart OpenWebRX.")
    return 0


USAGE = """Usage:
  python3 horus_patch.py install [--pip] <owrx_path>
  python3 horus_patch.py uninstall <owrx_path>
  python3 horus_patch.py restore-frontend <owrx_path>
  e.g. python3 horus_patch.py install --pip /usr/lib/python3/dist-packages"""


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    pip = "--pip" in args
    args = [a for a in args if a != "--pip"]

    # Legacy forms: "<path>" (old docker-patch.py) and "--restore-frontend <path>".
    if len(args) == 1 and not args[0].startswith("-") and args[0] not in ("install", "uninstall", "restore-frontend"):
        args = ["install", args[0]]
    if args and args[0] == "--restore-frontend":
        args[0] = "restore-frontend"

    if len(args) != 2 or args[0] not in ("install", "uninstall", "restore-frontend"):
        print(USAGE)
        return 1
    cmd, base = args
    if cmd == "install":
        return cmd_install(base, pip=pip)
    if cmd == "uninstall":
        return cmd_uninstall(base)
    print(f"[openwebrx-horus] Restoring OpenWebRX frontend at {base}...")
    cleanup_frontend(base)
    return 0


if __name__ == "__main__":
    sys.exit(main())
