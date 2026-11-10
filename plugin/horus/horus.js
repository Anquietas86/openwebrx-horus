/**
 * OpenWebRX+ Horus Telemetry plugin — v4.1.0
 *
 * Built on the official plugin JS API (htdocs/lib/Plugins.js, OpenWebRX+
 * 1.2.124+). No framework source is patched.
 *
 * ── Two views of the same frames ─────────────────────────────────────────
 *
 * 1. The STOCK Packet panel (default). OpenWebRX+ has no API for registering
 *    a new message panel, but its PacketMessagePanel already renders
 *    UTC | callsign | map link | comment rows for APRS/AIS/SONDE. This
 *    plugin wraps three prototype methods so it also:
 *      - claims messages with mode "Horus" (supportsMessage),
 *      - is shown while a horus-* mode is selected (DemodulatorPanel
 *        .updatePanels, whose mode list is hardcoded upstream),
 *      - links Horus callsigns to SondeHub Amateur (Utils.linkifyByMode).
 *    The server fills in `source` and a one-line `comment` for it, and the
 *    position link opens the real /map at the balloon's marker.
 *
 * 2. The TELEM floating window (optional detail view): a full table with
 *    separate Seq / SNR / Sensors columns, opened with the TELEM button.
 *    It is fed from the same pushMessage hook. If the stock panel proves
 *    good enough, this window can be deleted without touching (1).
 *
 * ── Message routing ──────────────────────────────────────────────────────
 *
 * openwebrx.js offers each secondary_demod message to a hardcoded list of
 * panels and falls through to secondary_demod_push_data() if none claims it.
 * With the Packet panel claiming "Horus", that is the single path. The
 * secondary_demod_push_data hook is kept only as a fallback for a build
 * where PacketMessagePanel is missing, so frames still reach the window.
 *
 * ── Panels that used to be force-hidden ──────────────────────────────────
 *
 * v4.0.x permanently wrapped toggle_panel to hide the ISM and digimodes
 * panels for EVERY mode, breaking other decoders. The empty digimodes box is
 * now suppressed the supported way, server-side, with secondaryFft=False on
 * the Horus mode definitions.
 */

(function () {
    'use strict';

    var WINDOW_ID  = 'horus';
    var TITLE      = 'Horus Telemetry';
    var VERSION    = '4.1.0';
    var MAX_ROWS   = 200;

    var win = null, scrollEl = null, tbody = null, statusEl = null;
    var pending = [];
    var seen = {};          // light duplicate guard: callsign:seq
    var frames = 0;

    // ── small helpers ────────────────────────────────────────────────────

    function esc(v) {
        var d = document.createElement('div');
        d.textContent = String(v == null ? '' : v);
        return d.innerHTML;
    }

    // Numeric accessor — never call .toFixed()/.toLocaleString() on a
    // non-number. v3.0.0 assumed numbers and would throw on a string field.
    function num(v) {
        return (typeof v === 'number' && isFinite(v)) ? v : null;
    }

    function pad(n) { return (n < 10 ? '0' : '') + n; }

    // Poll fn() until it returns truthy, then stop. fn() returns true once the
    // thing it was waiting for exists and has been handled.
    function poll(fn, label) {
        var attempts = 50, n = 0;
        (function tick() {
            var done = false;
            try { done = fn(); } catch (e) { console.error('[horus] ' + label + ':', e); }
            if (done) return;
            if (++n >= attempts) {
                console.error('[horus] gave up waiting for ' + label);
                return;
            }
            setTimeout(tick, 200);
        })();
    }

    // ── window construction ──────────────────────────────────────────────

    var CONTENT =
        '<div class="horus-toolbar">'
      +   '<span class="horus-status">Waiting for telemetry\u2026</span>'
      +   '<button type="button" class="openwebrx-button horus-clear">Clear</button>'
      + '</div>'
      + '<div class="horus-scroll">'
      +   '<table class="horus-table">'
      +     '<thead><tr>'
      +       '<th class="time">UTC</th>'
      +       '<th class="callsign">Call</th>'
      +       '<th class="sequence">Seq</th>'
      +       '<th class="position">Position</th>'
      +       '<th class="altitude">Alt</th>'
      +       '<th class="snr">SNR</th>'
      +       '<th class="sensors">Sensors</th>'
      +     '</tr></thead>'
      +     '<tbody></tbody>'
      +   '</table>'
      + '</div>';

    function build() {
        if (win) return true;

        if (typeof Plugins === 'undefined' || typeof Plugins.addWindow !== 'function')
            return false;
        if (typeof Utils === 'undefined' || !Utils.htmlEscape)
            return false;

        win = Plugins.addWindow(WINDOW_ID, TITLE, CONTENT);
        if (!win) return false;        // #webrx-page-container not in the DOM yet

        scrollEl = win.querySelector('.horus-scroll');
        tbody    = win.querySelector('.horus-table tbody');
        statusEl = win.querySelector('.horus-status');

        var clearBtn = win.querySelector('.horus-clear');
        if (clearBtn) clearBtn.addEventListener('click', clearAll);

        console.log('[horus] telemetry window created');

        // Flush anything that arrived before the window existed.
        if (pending.length) {
            var queued = pending;
            pending = [];
            for (var i = 0; i < queued.length; i++) renderRow(queued[i]);
            console.log('[horus] flushed ' + queued.length + ' queued frame(s)');
        }
        return true;
    }

    function setStatus(t) { if (statusEl) statusEl.textContent = t; }

    function clearAll() {
        if (tbody) tbody.innerHTML = '';
        seen = {};
        frames = 0;
        setStatus('Waiting for telemetry\u2026');
    }

    // ── row rendering ────────────────────────────────────────────────────

    function renderRow(msg) {
        if (!tbody) return;

        var t = '-';
        if (msg.timestamp) {
            var d = new Date(msg.timestamp);
            if (!isNaN(d.getTime()))
                t = pad(d.getUTCHours()) + ':'
                  + pad(d.getUTCMinutes()) + ':'
                  + pad(d.getUTCSeconds());
        }

        var cs = esc(msg.callsign || '???');
        var sq = msg.sequence != null ? String(msg.sequence) : '-';

        var pos = '-';
        var lat = num(msg.lat), lon = num(msg.lon);
        if (lat != null && lon != null) {
            var la = Math.abs(lat).toFixed(4) + (lat >= 0 ? 'N' : 'S');
            var lo = Math.abs(lon).toFixed(4) + (lon >= 0 ? 'E' : 'W');
            pos = '<a href="https://www.google.com/maps/search/?api=1&query='
                + encodeURIComponent(lat) + ',' + encodeURIComponent(lon)
                + '" target="_blank" rel="noopener">' + esc(la + ' ' + lo) + '</a>';
        }

        var alt = num(msg.altitude), snr = num(msg.snr);
        var al  = alt != null ? esc(alt.toLocaleString()) + ' m' : '-';
        var sn  = snr != null ? esc(snr.toFixed(1)) + ' dB' : '-';

        var temp = num(msg.temperature), hum = num(msg.humidity);
        var pres = num(msg.pressure);
        var bv   = num(msg.battery_voltage);
        if (bv == null) bv = num(msg.battery);
        var spd  = num(msg.speed), asc = num(msg.ascent_rate), sats = num(msg.sats);

        var se = [];
        if (temp != null) se.push(esc(temp.toFixed(1)) + '\u00b0C');
        if (hum  != null) se.push(esc(hum.toFixed(0)) + '%RH');
        if (pres != null) se.push(esc(pres.toFixed(1)) + 'hPa');
        if (bv   != null) se.push(esc(bv.toFixed(2)) + 'V');
        if (spd  != null) se.push(esc(spd.toFixed(0)) + 'km/h');
        if (asc  != null) se.push(esc(asc.toFixed(1)) + 'm/s');
        if (sats != null) se.push(esc(String(sats)) + ' sats');

        // Horus v3 custom fields
        var customNames = msg.custom_field_names || [];
        for (var i = 0; i < customNames.length; i++) {
            var name = customNames[i];
            if (msg[name] == null) continue;
            var val = msg[name];
            if (typeof val === 'number' && val % 1 !== 0) val = val.toFixed(2);
            se.push(esc(name + ':' + val));
        }

        var tr = document.createElement('tr');
        tr.innerHTML =
            '<td class="time">' + t + '</td>' +
            '<td class="callsign"><a href="' + sondehubUrl(msg.callsign)
              + '" target="_blank" rel="noopener">' + cs + '</a></td>' +
            '<td class="sequence">' + esc(sq) + '</td>' +
            '<td class="position">' + pos + '</td>' +
            '<td class="altitude">' + al + '</td>' +
            '<td class="snr">' + sn + '</td>' +
            '<td class="sensors">' + (se.length ? se.join(' | ') : '-') + '</td>';

        tbody.appendChild(tr);
        while (tbody.children.length > MAX_ROWS) tbody.removeChild(tbody.firstChild);
        if (scrollEl) scrollEl.scrollTop = scrollEl.scrollHeight;

        frames++;
        setStatus(frames + ' frame' + (frames === 1 ? '' : 's')
                  + ' \u00b7 last ' + (msg.callsign || '???') + ' at ' + t + ' UTC');
    }

    function addRow(msg) {
        if (!win) {
            pending.push(msg);
            build();
            return;
        }

        // Duplicate guard. With a single routing path this should never fire,
        // but it is cheap insurance against a server-side re-send.
        var key = (msg.callsign || '') + ':'
                + (msg.sequence != null ? msg.sequence : '');
        if (key !== ':' && seen[key]) return;
        if (key !== ':') seen[key] = true;

        renderRow(msg);
    }

    // ── stock Packet panel integration ───────────────────────────────────

    function isHorus(msg) {
        return !!msg && msg.mode === 'Horus';
    }

    function isHorusModulation(mod) {
        return typeof mod === 'string' && mod.indexOf('horus-') === 0;
    }

    function hookPacketPanel() {
        if (window._horusPacketHooked) return true;
        if (typeof PacketMessagePanel === 'undefined') return false;

        var proto = PacketMessagePanel.prototype;
        var origSupports = proto.supportsMessage;
        var origPush = proto.pushMessage;

        proto.supportsMessage = function (msg) {
            return isHorus(msg) || origSupports.apply(this, arguments);
        };
        proto.pushMessage = function (msg) {
            if (isHorus(msg)) addRow(msg);         // feed the TELEM window too
            return origPush.apply(this, arguments);
        };

        window._horusPacketHooked = true;
        console.log('[horus] stock Packet panel accepts Horus frames');
        return true;
    }

    function hookPanelToggle() {
        if (window._horusToggleHooked) return true;
        if (typeof DemodulatorPanel === 'undefined' || typeof toggle_panel !== 'function')
            return false;

        var orig = DemodulatorPanel.prototype.updatePanels;
        DemodulatorPanel.prototype.updatePanels = function () {
            var ret = orig.apply(this, arguments);
            // Upstream hides the Packet panel for any mode not in its
            // hardcoded list; show it again while a Horus mode is selected.
            try {
                if (isHorusModulation(this.getDemodulator().get_secondary_demod()))
                    toggle_panel('openwebrx-panel-packet-message', true);
            } catch (e) { /* no demodulator yet */ }
            return ret;
        };
        window._horusToggleHooked = true;

        // A Horus mode may already be active (e.g. restored from the URL).
        try { UI.getDemodulatorPanel().updatePanels(); } catch (e) {}
        return true;
    }

    function hookLinkify() {
        if (window._horusLinkHooked) return true;
        if (typeof Utils === 'undefined' || typeof Utils.linkifyByMode !== 'function')
            return false;

        var orig = Utils.linkifyByMode;
        Utils.linkifyByMode = function (mode, name) {
            if (mode !== 'Horus') return orig.apply(this, arguments);
            return '<a href="' + sondehubUrl(name) + '" target="_blank" rel="noopener">'
                 + esc(name) + '</a>';
        };
        window._horusLinkHooked = true;
        return true;
    }

    function sondehubUrl(callsign) {
        return 'https://amateur.sondehub.org/#!mt=Mapnik&mz=9&qm=6_hours&q='
             + encodeURIComponent(callsign || '');
    }

    // ── fallback routing (only if no panel claimed the frame) ────────────

    function hookRouting() {
        if (typeof secondary_demod_push_data !== 'function') return false;
        if (window._horusRoutingHooked) return true;

        var orig = secondary_demod_push_data;
        window.secondary_demod_push_data = function (value) {
            if (isHorus(value)) { addRow(value); return; }
            return orig.apply(this, arguments);
        };
        window._horusRoutingHooked = true;
        return true;
    }

    // ── button ───────────────────────────────────────────────────────────

    function onButton() {
        if (!build()) return;                        // still waiting on containers
        Plugins.toggleWindow(WINDOW_ID);
    }

    // ── plugin entry point (called by the plugin loader) ─────────────────

    Plugins.horus = {
        _version: VERSION,

        init: function () {
            poll(hookPacketPanel, 'PacketMessagePanel');
            poll(hookPanelToggle, 'DemodulatorPanel');
            poll(hookLinkify,     'Utils.linkifyByMode');
            poll(hookRouting,     'secondary_demod_push_data');
            poll(build,           'plugin window');
            poll(function () {
                return !!Plugins.addButton(WINDOW_ID, 'TELEM', onButton, '#1f4e79');
            }, 'plugin button stack');

            console.log('[horus] v' + VERSION + ' initialising (official plugin API)');
            return true;
        }
    };
})();
