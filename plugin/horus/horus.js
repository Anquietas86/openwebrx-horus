/**
 * OpenWebRX+ Horus Telemetry Panel v3.0.0
 *
 * DEFINITIVE APPROACH: Creates an independent panel inserted into
 * openwebrx-panels-container-left (or document.body as fallback).
 * Completely bypasses OpenWebRX+ framework panel lifecycle:
 *   - No MessagePanel inheritance (no toggle_panel CSS 3D fight)
 *   - No MutationObserver watchdog
 *   - No display = 'block' fight loop
 *   - Immune to initPanels(), DemodulatorPanel.updatePanels()
 *
 * Message routing uses THREE paths for reliability:
 *   1. $.fn.horusMessagePanel stub (hardcoded routing in openwebrx.js)
 *   2. secondary_demod_push_data hook (fallback path)
 *   3. Direct WebSocket listener (catches timing edge cases)
 *
 * Map path uses Leaflet L.polyline + L.marker on rx.map (OpenWebRX+ global).
 *
 * ISM + digimodes panels blocked via toggle_panel intercept.
 * Digimodes panel (4FSK waterfall) is empty on Horus Binary — blocked to save space.
 */
(function() {
    'use strict';

    var panelEl = null, tbody = null, pending = [], maxRows = 200;
    var pathPoints = [], mapPath = null, mapMarker = null;
    var lastSeq = {};  // dedup by callsign+seq

    function esc(s) {
        var d = document.createElement('div');
        d.textContent = String(s);
        return d.innerHTML;
    }

    // ── Block ISM + digimodes panels ────────────────────────────────

    (function blockPanels() {
        if (typeof toggle_panel !== 'function') { setTimeout(blockPanels, 100); return; }
        if (window._horusPanelsBlocked) return;
        window._horusPanelsBlocked = true;
        var orig = window.toggle_panel;
        window.toggle_panel = function(what, on) {
            if (on && (what === 'openwebrx-panel-ism-message' || what === 'openwebrx-panel-digimodes')) {
                return;
            }
            return orig.apply(this, arguments);
        };
        orig('openwebrx-panel-ism-message', false);
        orig('openwebrx-panel-digimodes', false);
        console.log('[horus] ISM + digimodes panels blocked');
    })();

    // ── Panel creation ──────────────────────────────────────────────

    function init() {
        var existing = document.getElementById('horus-telemetry-panel');
        if (existing) existing.parentNode.removeChild(existing);

        panelEl = document.createElement('div');
        panelEl.id = 'horus-telemetry-panel';

        var container = document.getElementById('openwebrx-panels-container-left');
        if (container) {
            panelEl.style.cssText =
                'width:619px;max-height:250px;overflow-y:auto;' +
                'background:rgba(20,20,35,0.95);border:1px solid #444;border-radius:3px;' +
                'font-family:monospace;font-size:12px;color:#e0e0e0;display:block;' +
                'margin-bottom:4px;box-shadow:0 2px 8px rgba(0,0,0,0.6);flex-shrink:0;';
            var firstPanel = container.querySelector('.openwebrx-panel');
            if (firstPanel) container.insertBefore(panelEl, firstPanel);
            else container.appendChild(panelEl);
        } else {
            // Fallback: fixed-position on document.body
            panelEl.style.cssText =
                'position:fixed;bottom:10px;left:10px;right:10px;max-height:300px;' +
                'overflow-y:auto;background:rgba(0,0,0,0.92);border:1px solid #444;' +
                'border-radius:4px;z-index:99999;font-family:monospace;font-size:12px;' +
                'color:#e0e0e0;display:block;padding:0;box-shadow:0 0 20px rgba(0,0,0,0.8);';
            document.body.appendChild(panelEl);
        }

        panelEl.innerHTML =
            '<div style="background:#1a1a2e;color:#88ccff;padding:3px 8px;font-weight:bold;' +
            'display:flex;justify-content:space-between;align-items:center;' +
            'border-bottom:1px solid #333;position:sticky;top:0;z-index:1;font-size:12px;">' +
            '<span>\u25b2 Horus Telemetry</span>' +
            '<button id="horus-clear" style="background:#333;color:#aaa;border:none;' +
            'padding:1px 6px;cursor:pointer;border-radius:2px;font-size:11px;">Clear</button></div>' +
            '<table style="width:100%;border-collapse:collapse;"><thead>' +
            '<tr style="background:#141423;position:sticky;top:22px;z-index:1;">' +
            '<th style="color:#888;padding:2px 4px;text-align:left;width:50px;font-weight:normal;">UTC</th>' +
            '<th style="color:#ffcc66;padding:2px 4px;text-align:left;width:65px;font-weight:normal;">Call</th>' +
            '<th style="color:#aaa;padding:2px 4px;text-align:center;width:35px;font-weight:normal;">Seq</th>' +
            '<th style="color:#88ccff;padding:2px 4px;text-align:left;width:120px;font-weight:normal;">Position</th>' +
            '<th style="color:#88ff88;padding:2px 4px;text-align:left;width:60px;font-weight:normal;">Alt</th>' +
            '<th style="color:#ff8888;padding:2px 4px;text-align:left;width:50px;font-weight:normal;">SNR</th>' +
            '<th style="color:#aaa;padding:2px 4px;text-align:left;font-weight:normal;">Sensors</th>' +
            '</tr></thead><tbody></tbody></table>';

        tbody = panelEl.querySelector('tbody');

        panelEl.querySelector('#horus-clear').onclick = function() {
            tbody.innerHTML = '';
            pathPoints = [];
            if (mapPath && typeof rx !== 'undefined' && rx.map) {
                rx.map.removeLayer(mapPath);
                mapPath = null;
            }
            if (mapMarker && typeof rx !== 'undefined' && rx.map) {
                rx.map.removeLayer(mapMarker);
                mapMarker = null;
            }
            lastSeq = {};
        };

        // Flush queued messages
        var msgs = pending.concat(window._horusPendingMessages || []);
        window._horusPendingMessages = [];
        pending = [];
        for (var i = 0; i < msgs.length; i++) addRow(msgs[i]);
        console.log('[horus] Panel ready, flushed ' + msgs.length + ' queued messages');
    }

    // ── Map integration ─────────────────────────────────────────────

    function updateMap(lat, lon, alt, callsign) {
        pathPoints.push([lat, lon]);
        if (pathPoints.length > 500) pathPoints.shift();

        if (typeof rx === 'undefined' || !rx || !rx.map) return;

        if (mapPath) {
            mapPath.setLatLngs(pathPoints);
        } else {
            mapPath = L.polyline(pathPoints, {
                color: '#ff6600', weight: 2, opacity: 0.8
            }).addTo(rx.map);
        }

        var icon = L.divIcon({
            className: '',
            html: '<div style="background:#ff6600;border:2px solid #fff;border-radius:50%;' +
                  'width:10px;height:10px;margin:-5px 0 0 -5px;"></div>' +
                  '<div style="background:rgba(0,0,0,0.8);color:#ff6600;padding:1px 3px;' +
                  'font-size:10px;white-space:nowrap;border-radius:2px;margin-top:2px;">' +
                  esc(callsign || 'HORUS') + ' ' + Math.round((alt || 0) / 1000) + 'km</div>',
            iconAnchor: [0, 0]
        });

        if (mapMarker) {
            mapMarker.setLatLng([lat, lon]);
            mapMarker.setIcon(icon);
        } else {
            mapMarker = L.marker([lat, lon], { icon: icon }).addTo(rx.map);
        }
    }

    // ── Row rendering ───────────────────────────────────────────────

    function addRow(msg) {
        if (!tbody) { pending.push(msg); init(); return; }

        // Dedup by callsign+seq (WebSocket listener may deliver duplicates)
        var dedupKey = (msg.callsign || '') + ':' + (msg.sequence != null ? msg.sequence : '');
        if (dedupKey !== ':' && lastSeq[dedupKey]) return;
        if (dedupKey !== ':') lastSeq[dedupKey] = true;

        if (msg.lat != null && msg.lon != null) {
            updateMap(msg.lat, msg.lon, msg.altitude || 0, msg.callsign || 'HORUS');
        }

        // UTC timestamp
        var t = '-';
        if (msg.timestamp) {
            try {
                var d = new Date(msg.timestamp);
                if (!isNaN(d.getTime())) {
                    t = ('0' + d.getUTCHours()).slice(-2) + ':' +
                        ('0' + d.getUTCMinutes()).slice(-2) + ':' +
                        ('0' + d.getUTCSeconds()).slice(-2);
                }
            } catch(e) {}
        }

        var cs = esc(msg.callsign || '???');
        var sq = msg.sequence != null ? String(msg.sequence) : '-';

        // Position with Google Maps link
        var pos = '-';
        if (msg.lat != null && msg.lon != null) {
            var la = Math.abs(msg.lat).toFixed(4) + (msg.lat >= 0 ? 'N' : 'S');
            var lo = Math.abs(msg.lon).toFixed(4) + (msg.lon >= 0 ? 'E' : 'W');
            pos = '<a href="https://www.google.com/maps/search/?api=1&query=' +
                  encodeURIComponent(msg.lat) + ',' + encodeURIComponent(msg.lon) +
                  '" target="_blank" rel="noopener" style="color:#88ccff;text-decoration:none;">' +
                  esc(la + ' ' + lo) + '</a>';
        }

        var al = msg.altitude != null ? esc(msg.altitude.toLocaleString()) + ' m' : '-';
        var sn = msg.snr != null ? esc(msg.snr.toFixed(1)) + ' dB' : '-';

        // Sensor fields
        var se = [];
        if (msg.temperature != null) se.push(esc(msg.temperature.toFixed(1)) + '\u00b0C');
        if (msg.humidity != null) se.push(esc(msg.humidity.toFixed(0)) + '%RH');
        if (msg.pressure != null) se.push(esc(msg.pressure.toFixed(1)) + 'hPa');
        if (msg.battery_voltage != null) se.push(esc(msg.battery_voltage.toFixed(2)) + 'V');
        else if (msg.battery != null) se.push(esc(msg.battery.toFixed(2)) + 'V');
        if (msg.speed != null) se.push(esc(msg.speed.toFixed(0)) + 'km/h');
        if (msg.ascent_rate != null) se.push(esc(msg.ascent_rate.toFixed(1)) + 'm/s');
        if (msg.sats != null) se.push(esc(String(msg.sats)) + ' sats');

        // Custom v3 fields
        var customNames = msg.custom_field_names || [];
        for (var i = 0; i < customNames.length; i++) {
            var name = customNames[i];
            if (msg[name] != null) {
                var val = msg[name];
                if (typeof val === 'number' && val % 1 !== 0) val = val.toFixed(2);
                se.push(esc(name + ':' + val));
            }
        }

        var ss = se.length ? se.join(' | ') : '-';

        var tr = document.createElement('tr');
        tr.style.cssText = 'border-bottom:1px solid #1a1a1a;';
        tr.innerHTML =
            '<td style="color:#888;padding:1px 4px;white-space:nowrap;">' + t + '</td>' +
            '<td style="color:#ffcc66;padding:1px 4px;white-space:nowrap;">' +
            '<a href="https://amateur.sondehub.org/#!mt=Mapnik&mz=9&qm=6_hours&q=' +
            encodeURIComponent(msg.callsign || '') + '" target="_blank" rel="noopener" ' +
            'style="color:#ffcc66;text-decoration:none;">' + cs + '</a></td>' +
            '<td style="color:#aaa;padding:1px 4px;text-align:center;white-space:nowrap;">' + esc(sq) + '</td>' +
            '<td style="padding:1px 4px;white-space:nowrap;">' + pos + '</td>' +
            '<td style="color:#88ff88;padding:1px 4px;white-space:nowrap;">' + al + '</td>' +
            '<td style="color:#ff8888;padding:1px 4px;white-space:nowrap;">' + sn + '</td>' +
            '<td style="color:#aaa;padding:1px 4px;white-space:nowrap;">' + ss + '</td>';

        tbody.appendChild(tr);
        while (tbody.children.length > maxRows) tbody.removeChild(tbody.firstChild);
        panelEl.scrollTop = panelEl.scrollHeight;
    }

    // ── Message handler ─────────────────────────────────────────────

    function handle(msg) {
        if (!msg || msg.mode !== 'Horus') return false;
        if (panelEl) addRow(msg);
        else { pending.push(msg); init(); }
        return true;
    }

    // ── Routing path 1: $.fn.horusMessagePanel (hardcoded routing) ──

    (function hookJQ() {
        if (typeof $ !== 'undefined') {
            $.fn.horusMessagePanel = function() {
                return {
                    supportsMessage: function(m) { return m && m.mode === 'Horus'; },
                    pushMessage: handle
                };
            };
        } else {
            setTimeout(hookJQ, 200);
        }
    })();

    // ── Routing path 2: secondary_demod_push_data hook ─────────────

    (function hookPush() {
        if (typeof secondary_demod_push_data === 'function') {
            var orig = secondary_demod_push_data;
            secondary_demod_push_data = function(v) {
                if (handle(v)) return;
                orig.apply(this, arguments);
            };
        } else {
            setTimeout(hookPush, 200);
        }
    })();

    // ── Routing path 3: Direct WebSocket listener ───────────────────

    (function hookWS() {
        function attach(s) {
            s.addEventListener('message', function(e) {
                if (typeof e.data !== 'string') return;
                try {
                    var j = JSON.parse(e.data);
                    if (j.type === 'secondary_demod' && j.value && j.value.mode === 'Horus') {
                        handle(j.value);
                    }
                } catch(ex) {}
            });
            console.log('[horus] WebSocket listener attached');
        }
        if (typeof ws !== 'undefined' && ws && ws.readyState === WebSocket.OPEN) {
            attach(ws);
        } else {
            var iv = setInterval(function() {
                if (typeof ws !== 'undefined' && ws && ws.readyState === WebSocket.OPEN) {
                    clearInterval(iv);
                    attach(ws);
                }
            }, 1000);
            setTimeout(function() { clearInterval(iv); }, 30000);
        }
    })();

    // ── Bootstrap ───────────────────────────────────────────────────

    init();
    console.log('[horus] v3.0.0 ready — floating panel, 3-path routing');

})();

// ── Plugin system no-op (satisfies Plugins.load('horus')) ──────────

Plugins.horus = {
    _version: '3.0.0',
    init: function() {
        console.log('[horus] Plugin v3.0.0 — panel managed by standalone IIFE');
        return true;
    }
};
