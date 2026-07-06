#!/usr/bin/env python3
"""
Idempotent patcher for OpenWebRX+ Python source files AND frontend HTML/JS.

Adds Horus decoder support to:
  - owrx/feature.py
  - owrx/modes.py
  - owrx/service/__init__.py
  - owrx/dsp.py
  - htdocs/openwebrx.js  (panel routing list)
  - htdocs/index.html    (panel div + standalone handler)

Safe to run multiple times — strips any existing patches first, then re-applies.

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


def patch_plugins_js(content):
    """Cache-bust the Horus plugin script so browsers fetch patched horus.js."""
    jm = JS_MARKER
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if 'var script_src = path + name + ".js";' in line:
            indent = line[: len(line) - len(line.lstrip())]
            block = [
                line,
                indent + jm + " BEGIN",
                indent + 'if (!remote && name === "horus") {',
                indent + '    script_src += "?v=20260628-visibility";',
                indent + '}',
                indent + jm + " END",
            ]
            lines = lines[:i] + block + lines[i+1:]
            break
    else:
        print("  WARN: plugin script_src line not found in plugins.js — skipping cache-bust patch")
    return "\n".join(lines)


def patch_openwebrx_js(content):
    """Repair secondary_demod routing and add Horus to the panel list.

    Important: the marker block must wrap the entire generated panel-init
    expression (var panels + return + closing });), but NOT the case/value
    lines and NOT the subsequent panels.push/dispatch logic. Older patcher
    versions wrapped only the var-panels line, leaving orphaned returns after
    strip/reapply; this function rebuilds the case body from a clean canonical
    panel-init block every time.
    """
    jm = JS_MARKER
    lines = content.split("\n")

    case_idx = None
    for i, line in enumerate(lines):
        if "case 'secondary_demod':" in line or 'case "secondary_demod":' in line:
            case_idx = i
            break
    if case_idx is None:
        print("  WARN: secondary_demod case not found in openwebrx.js — skipping JS patch")
        return content

    break_idx = None
    for i in range(case_idx + 1, min(case_idx + 80, len(lines))):
        if lines[i].strip() == "break;":
            break_idx = i
            break
    if break_idx is None:
        print("  WARN: secondary_demod break not found — skipping JS patch")
        return content

    case_indent = lines[case_idx][: len(lines[case_idx]) - len(lines[case_idx].lstrip())]
    body_indent = case_indent + "    "

    # Preserve the real value line if present; otherwise generate it.
    value_line = None
    for i in range(case_idx + 1, min(case_idx + 6, break_idx)):
        if "var value = json" in lines[i]:
            value_line = lines[i]
            break
    if value_line is None:
        value_line = body_indent + "var value = json['value'];"

    # Keep the dispatch tail from panels.push(...) onward. This drops all stale
    # var-panels/return/}); fragments produced by older non-idempotent patchers.
    tail_start = None
    for i in range(case_idx + 1, break_idx + 1):
        stripped = lines[i].strip()
        if stripped.startswith("panels.push("):
            tail_start = i
            break
    if tail_start is None:
        # Conservative fallback: keep all non-panel-init lines after value_line.
        tail = []
        for i in range(case_idx + 1, break_idx + 1):
            stripped = lines[i].strip()
            if stripped.startswith("var value = json"):
                continue
            if stripped.startswith("var panels ="):
                continue
            if stripped.startswith("return $('#openwebrx-panel-") and "MessagePanel" in stripped:
                continue
            if stripped == "});":
                continue
            if "openwebrx-horus" in stripped:
                continue
            tail.append(lines[i])
    else:
        tail = lines[tail_start:break_idx + 1]

    canonical = [
        body_indent + jm + " BEGIN",
        body_indent + "var panels = ['wsjt', 'packet', 'pocsag', 'page', 'sstv', 'fax', 'ism', 'hfdl', 'adsb', 'dsc', 'skimmer', 'horus'].map(function(id) {",
        body_indent + "    return $('#openwebrx-panel-' + id + '-message')[id + 'MessagePanel']();",
        body_indent + "});",
        body_indent + jm + " END",
    ]

    new_block = [lines[case_idx], value_line] + canonical + tail
    return "\n".join(lines[:case_idx] + new_block + lines[break_idx + 1:])


# ---------------------------------------------------------------------------
# Standalone handler script injected into index.html before </body>
# This is the reliable fallback for the jQuery widget routing timing issues.
# It bypasses the plugin system entirely and directly handles secondary_demod
# messages for mode == "Horus" using vanilla DOM APIs.
# ---------------------------------------------------------------------------
STANDALONE_HANDLER = r"""<script>
/* openwebrx-horus standalone panel handler v2.3.0
 * Nuclear option: bypass plugin system + jQuery widget routing entirely.
 * Works even when MessagePanel / $.fn.horusMessagePanel aren't ready yet.
 */
(function() {
    'use strict';

    var panelReady = false;
    var pending = [];
    var panelEl = null;
    var tbody = null;

    function esc(s) {
        var d = document.createElement('div');
        d.textContent = String(s);
        return d.innerHTML;
    }

    function initPanel() {
        // Use only the FIRST instance of the div (duplicate safety)
        var all = document.querySelectorAll('#openwebrx-panel-horus-message');
        // Remove duplicates
        for (var i = 1; i < all.length; i++) { all[i].parentNode.removeChild(all[i]); }

        panelEl = document.getElementById('openwebrx-panel-horus-message');
        if (!panelEl) { setTimeout(initPanel, 200); return; }
        if (panelReady) return;
        panelReady = true;

        // Clear any existing content (the plugin may have partially rendered)
        panelEl.innerHTML = '';
        var tbl = document.createElement('table');
        tbl.innerHTML = '<thead><tr>' +
            '<th class="time">UTC</th>' +
            '<th class="callsign">Callsign</th>' +
            '<th class="sequence">Seq</th>' +
            '<th class="position">Position</th>' +
            '<th class="altitude">Alt (m)</th>' +
            '<th class="snr">SNR</th>' +
            '<th class="sensors">Sensors</th>' +
            '</tr></thead><tbody></tbody>';
        panelEl.appendChild(tbl);
        tbody = tbl.querySelector('tbody');

        // Flush pending
        var msgs = pending.concat(window._horusPendingMessages || []);
        window._horusPendingMessages = [];
        pending = [];
        for (var i = 0; i < msgs.length; i++) { addRow(msgs[i]); }

        console.log('[horus-standalone] Panel ready, flushed ' + msgs.length + ' pending messages');
    }

    function showPanel() {
        if (!panelEl) return;
        if (panelEl.style.display === 'none' || !panelEl.style.display) {
            panelEl.style.display = 'block';
            panelEl.style.maxHeight = '300px';
            panelEl.style.overflowY = 'auto';
            panelEl.style.flexShrink = '0';
            panelEl.style.marginTop = '4px';
            panelEl.style.background = 'rgba(0,0,0,0.85)';
            // Hide digimodes placeholder
            var digi = document.getElementById('openwebrx-panel-digimodes');
            if (digi) digi.style.display = 'none';
        }
    }

    function addRow(msg) {
        if (!tbody) { pending.push(msg); initPanel(); return; }

        var time = '-';
        if (msg.timestamp) {
            try {
                var d = new Date(msg.timestamp);
                if (!isNaN(d.getTime())) {
                    time = ('0' + d.getUTCHours()).slice(-2) + ':' +
                           ('0' + d.getUTCMinutes()).slice(-2) + ':' +
                           ('0' + d.getUTCSeconds()).slice(-2);
                }
            } catch(e) {}
        }

        var cs = esc(msg.callsign || '???');
        var seq = msg.sequence !== undefined ? String(msg.sequence) : '-';

        var pos = '-';
        if (msg.lat !== undefined && msg.lon !== undefined) {
            var lat = Math.abs(msg.lat).toFixed(4) + (msg.lat >= 0 ? 'N' : 'S');
            var lon = Math.abs(msg.lon).toFixed(4) + (msg.lon >= 0 ? 'E' : 'W');
            pos = '<a href="https://www.google.com/maps/search/?api=1&query=' +
                  encodeURIComponent(msg.lat) + ',' + encodeURIComponent(msg.lon) +
                  '" target="_blank" rel="noopener">' + esc(lat + ' ' + lon) + '</a>';
        }

        var alt = msg.altitude !== undefined ? esc(msg.altitude.toLocaleString()) + ' m' : '-';
        var snr = msg.snr !== undefined ? esc(msg.snr.toFixed(1)) + ' dB' : '-';

        var sensors = [];
        if (msg.temperature !== undefined) sensors.push(esc(msg.temperature.toFixed(1)) + '°C');
        if (msg.battery_voltage !== undefined) sensors.push(esc(msg.battery_voltage.toFixed(2)) + 'V');
        else if (msg.battery !== undefined) sensors.push(esc(msg.battery.toFixed(2)) + 'V');
        if (msg.speed !== undefined) sensors.push(esc(msg.speed.toFixed(0)) + 'km/h');
        if (msg.ascent_rate !== undefined) sensors.push(esc(msg.ascent_rate.toFixed(1)) + 'm/s');
        if (msg.sats !== undefined) sensors.push(esc(String(msg.sats)) + ' sats');
        var sens = sensors.length > 0 ? sensors.join(' | ') : '-';

        var tr = document.createElement('tr');
        tr.innerHTML = '<td class="time">' + time + '</td>' +
            '<td class="callsign"><a href="https://amateur.sondehub.org/#!mt=Mapnik&mz=9&qm=6_hours&q=' +
            encodeURIComponent(msg.callsign || '') + '" target="_blank" rel="noopener">' + cs + '</a></td>' +
            '<td class="sequence">' + esc(seq) + '</td>' +
            '<td class="position">' + pos + '</td>' +
            '<td class="altitude">' + alt + '</td>' +
            '<td class="snr">' + snr + '</td>' +
            '<td class="sensors">' + sens + '</td>';
        tbody.appendChild(tr);

        showPanel();

        // Prune old rows (keep last 200)
        while (tbody.children.length > 200) { tbody.removeChild(tbody.firstChild); }
        panelEl.scrollTop = panelEl.scrollHeight;
    }

    function handleHorusMessage(msg) {
        if (!msg || msg.mode !== 'Horus') return false;
        if (panelReady) { addRow(msg); }
        else { pending.push(msg); initPanel(); }
        return true;
    }

    // Hook secondary_demod_push_data (the fallback path from openwebrx.js routing)
    // This fires when the hardcoded panel list routing fails or falls through.
    function hookFallback() {
        if (typeof window.secondary_demod_push_data === 'function') {
            var orig = window.secondary_demod_push_data;
            window.secondary_demod_push_data = function(value) {
                if (handleHorusMessage(value)) return;
                orig.apply(this, arguments);
            };
            console.log('[horus-standalone] Hooked secondary_demod_push_data');
        } else {
            setTimeout(hookFallback, 200);
        }
    }

    // Also override $.fn.horusMessagePanel once jQuery is ready.
    // This gives the hardcoded routing a widget that routes to us,
    // bypassing the plugin system's deferred init race.
    function hookJQuery() {
        if (typeof window.jQuery !== 'undefined' || typeof window.$ !== 'undefined') {
            var jq = window.jQuery || window.$;
            jq.fn.horusMessagePanel = function() {
                return {
                    supportsMessage: function(msg) {
                        return msg && msg.mode === 'Horus';
                    },
                    pushMessage: function(msg) {
                        handleHorusMessage(msg);
                    }
                };
            };
            console.log('[horus-standalone] Registered $.fn.horusMessagePanel');
        } else {
            setTimeout(hookJQuery, 200);
        }
    }

    // Start everything
    initPanel();
    hookFallback();
    hookJQuery();

    console.log('[horus-standalone] Handler installed v2.3.0');
})();
</script>"""


def patch_index_html(content):
    """Add Horus panel div + standalone handler to index.html.

    1. Cache-bust receiver/plugin scripts so browser gets patched assets
    2. Insert panel div after openwebrx-panel-ism-message
    3. Insert standalone handler script before </body>
    """
    # Normalize any previous cache-busts, then add current versions.
    content = re.sub(r'src="compiled/receiver\.js(?:\?v=[^"]+)?"',
                     'src="compiled/receiver.js?v=20260628-routing"', content)
    content = re.sub(r'src="static/plugins\.js(?:\?v=[^"]+)?"',
                     'src="static/plugins.js?v=20260628-loader"', content)

    lines = content.split("\n")

    # --- 1. Insert panel div after ism-message ---
    ism_idx = None
    for i, line in enumerate(lines):
        if 'id="openwebrx-panel-ism-message"' in line:
            ism_idx = i
            break

    if ism_idx is not None:
        panel_div = (
            '            <!-- openwebrx-horus BEGIN -->\n'
            '            <div class="openwebrx-panel openwebrx-message-panel"'
            ' id="openwebrx-panel-horus-message"'
            ' style="display: none; width: 619px;"'
            ' data-panel-name="horus-message"></div>\n'
            '            <!-- openwebrx-horus END -->'
        )
        lines.insert(ism_idx + 1, panel_div)
    else:
        print("  WARN: openwebrx-panel-ism-message not found — panel div not inserted")

    # --- 2. Insert standalone handler before </body> ---
    body_close_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == "</body>":
            body_close_idx = i
            break

    if body_close_idx is not None:
        handler_block = (
            "    <!-- openwebrx-horus BEGIN -->\n"
            + STANDALONE_HANDLER + "\n"
            + "    <!-- openwebrx-horus END -->"
        )
        lines.insert(body_close_idx, handler_block)
    else:
        print("  WARN: </body> not found — standalone handler not inserted")

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

    # openwebrx.js — uses JS comment markers (// not #)
    patch_file(
        os.path.join(base, "htdocs", "openwebrx.js"),
        patch_openwebrx_js,
        strip_func=strip_existing_patches_js,
    )

    # plugins.js — cache-bust Horus plugin script after frontend fixes
    patch_file(
        os.path.join(base, "htdocs", "plugins.js"),
        patch_plugins_js,
        strip_func=strip_existing_patches_js,
    )

    # index.html — uses HTML comment markers
    patch_file(
        os.path.join(base, "htdocs", "index.html"),
        patch_index_html,
        strip_func=strip_existing_patches_html,
    )

    print("[openwebrx-horus] Patching complete.")


if __name__ == "__main__":
    main()
