/**
 * OpenWebRX+ Horus Telemetry plugin — v4.0.2
 *
 * Rewritten against the OFFICIAL plugin JS API shipped in OpenWebRX+ 1.2.124+
 * (htdocs/lib/Plugins.js): Plugins.addButton / addWindow / toggleWindow.
 *
 * ── Why this is less than half the size of v3.0.0 ────────────────────────
 *
 * v3.0.0 was built for OpenWebRX+ ~1.2.110, before the plugin API existed.
 * It had to fight the framework's hardcoded panel-array route in
 * openwebrx.js, which required:
 *   - three redundant message-routing paths (jQuery widget stub +
 *     secondary_demod_push_data hook + a direct WebSocket listener)
 *   - duplicate-suppression, because those three paths delivered the same
 *     frame up to three times
 *   - a hand-built DOM panel with a hardcoded 619px width and per-cell
 *     inline styles
 *   - an invasive PATCH TO FRAMEWORK SOURCE (openwebrx.js) to inject
 *     'horus' into the hardcoded panel list — the reason a single syntax
 *     error anywhere used to kill the entire compiled bundle
 *
 * Plugins.addWindow() creates a floating window OUTSIDE the framework's
 * panel container, so initPanels(), toggle_panel() and the CSS 3D transform
 * system never touch it. All of the above falls away, and we gain drag,
 * resize, close and position/size persistence (localStorage) for free.
 *
 * ── Message routing is now a SINGLE path ─────────────────────────────────
 *
 * openwebrx.js routes a 'secondary_demod' message by asking each panel in a
 * hardcoded list whether it claims the message; if none do, it calls
 * secondary_demod_push_data(value). With 'horus' absent from that list
 * (this version no longer patches it in), our hook is the only consumer —
 * so one path, no duplicate delivery, no dedup requirement.
 *
 *   case 'secondary_demod':
 *       var value = json['value'];
 *       var panels = [ ...built-in ids... ].map(...);
 *       panels.push($('#openwebrx-panel-js8-message').js8());
 *       if (!panels.some(p => p.supportsMessage(value) && (p.pushMessage(value), true)))
 *           secondary_demod_push_data(value);   // <-- our single path
 *
 * The plugin loader contract: Plugins.load('horus') (from plugins/receiver/
 * init.js) injects horus.js + horus.css, then awaits Plugins.horus.init().
 *
 * ── Map plotting was REMOVED in v4.0.2 ───────────────────────────────────
 *
 * v4.0.0/v4.0.1 carried code to plot the balloon on the OpenWebRX map by
 * reaching for a global `rx.map` or `map`. That never worked on OpenWebRX+
 * 1.2.125, and it failed SILENTLY: the MAP button opens /map in a SEPARATE
 * browsing context (<a href="map" target="openwebrx-map">), so neither
 * Leaflet nor the map instance exists on the page this plugin runs in.
 *
 * Balloon positions still reach the real map. The server-side parser calls
 * Map.getSharedInstance().updateLocation(...) on every fix, so the balloon
 * plots on the /map window with no help from this plugin. The dead code here
 * is therefore deleted rather than left implied-working.
 */

(function () {
    'use strict';

    var WINDOW_ID  = 'horus';
    var TITLE      = 'Horus Telemetry';
    var VERSION    = '4.0.2';
    var MAX_ROWS   = 200;

    // Framework panels suppressed while Horus telemetry is in use.
    // OpenWebRX+ shows the ISM panel spuriously on some non-ISM modes, and the
    // digimodes panel is redundant beside the telemetry window.
    // Remove the 'digimodes' entry to let the 4FSK waterfall panel show.
    var SUPPRESS = [
        'openwebrx-panel-ism-message',
        'openwebrx-panel-digimodes'
    ];

    var win = null, scrollEl = null, tbody = null, statusEl = null;
    var pending = [];
    var seen = {};          // light duplicate guard: callsign:seq
    var frames = 0;
    var autoShown = false;  // auto-open the window once per page load

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
            '<td class="callsign"><a href="https://amateur.sondehub.org/#!mt=Mapnik&mz=9&qm=6_hours&q='
              + encodeURIComponent(msg.callsign || '')
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

        // Open the window automatically on the first frame of a page load,
        // then respect the user's choice (they may have closed it on purpose).
        if (!autoShown) {
            autoShown = true;
            Plugins.toggleWindow(WINDOW_ID, true);
        }
    }

    // ── message routing — the SINGLE path ────────────────────────────────

    function handle(msg) {
        if (!msg || msg.mode !== 'Horus') return false;   // not ours
        addRow(msg);
        return true;                                      // claimed
    }

    function hookRouting() {
        if (typeof secondary_demod_push_data !== 'function') return false;
        if (window._horusRoutingHooked) return true;

        var orig = secondary_demod_push_data;
        window.secondary_demod_push_data = function (value) {
            if (handle(value)) return;
            return orig.apply(this, arguments);
        };
        window._horusRoutingHooked = true;
        console.log('[horus] routing hooked via secondary_demod_push_data');
        return true;
    }

    // ── suppress framework panels that are noise on a Horus profile ──────

    function suppressPanels() {
        if (window._horusPanelsSuppressed) return true;
        if (typeof toggle_panel !== 'function') return false;

        var orig = window.toggle_panel;
        window.toggle_panel = function (what, on) {
            if (on && SUPPRESS.indexOf(what) !== -1) return;
            return orig.apply(this, arguments);
        };
        window._horusPanelsSuppressed = true;

        for (var i = 0; i < SUPPRESS.length; i++) {
            try { orig(SUPPRESS[i], false); } catch (e) {}
        }
        console.log('[horus] suppressed: ' + SUPPRESS.join(', '));
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
            poll(suppressPanels, 'toggle_panel');
            poll(build,          'plugin window');
            poll(hookRouting,    'secondary_demod_push_data');
            poll(function () {
                return !!Plugins.addButton(WINDOW_ID, 'TELEM', onButton, '#1f4e79');
            }, 'plugin button stack');

            console.log('[horus] v' + VERSION + ' initialising (official plugin API)');
            return true;
        }
    };
})();
