/**
 * OpenWebRX+ Plugin: Horus Balloon Telemetry
 *
 * Integrates with OpenWebRX's MessagePanel framework for proper
 * panel lifecycle (sizing, scrolling, clear button, visibility).
 *
 * Two integration paths (belt and suspenders):
 * 1. PRIMARY: The installer adds 'horus' to the hardcoded panel ID list
 *    in openwebrx.js, so the framework routes secondary_demod messages
 *    directly to HorusMessagePanel (same as WSJT, Packet, Meshtastic, etc.)
 * 2. FALLBACK: Hooks secondary_demod_push_data as a safety net for
 *    installations where the openwebrx.js patch wasn't applied.
 *
 * Works for both bare-metal and Docker installs.
 */

Plugins.horus = {
    _version: "2.1.0",
    _panel: null,
    _visible: false,
    _pendingMessages: [],
    _mapPathPoints: [], // Store all decoded positions for map path
    _mapPath: null,
    _mapMarker: null,
    _initAttempts: 0,
    _maxInitAttempts: 20,

    // Early jQuery widget stub — registered before openwebrx.js runs
    // so the hardcoded panel routing doesn't fail on undefined widget.
    _earlyWidgetStub: (function() {
        if (typeof $ !== "undefined" && !$.fn.horusMessagePanel) {
            $.fn.horusMessagePanel = function() {
                // Deferred: if the real panel isn't ready yet, return a
                // dummy that silently accepts messages until init completes.
                var panel = Plugins.horus._panel;
                if (!panel) {
                    // Queue messages until real panel is ready
                    return {
                        supportsMessage: function(msg) {
                            return msg && msg.mode === "Horus";
                        },
                        pushMessage: function(msg) {
                            Plugins.horus._pendingMessages.push(msg);
                        }
                    };
                }
                return panel;
            };
        }
    })(),

    init: function() {
        // Intercept toggle_panel to permanently block ISM and digimodes panels
        // from showing when we're in Horus Binary mode.
        (function blockPanels() {
            if (typeof toggle_panel !== 'function') { setTimeout(blockPanels, 100); return; }
            if (window._horusPanelsBlocked) return;
            window._horusPanelsBlocked = true;
            var origToggle = window.toggle_panel;
            window.toggle_panel = function(what, on) {
                if (on && (what === 'openwebrx-panel-ism-message' || what === 'openwebrx-panel-digimodes')) {
                    console.log('[horus] Blocked panel open: ' + what);
                    return;
                }
                return origToggle.apply(this, arguments);
            };
            // Close both immediately if already open
            origToggle('openwebrx-panel-ism-message', false);
            origToggle('openwebrx-panel-digimodes', false);
            console.log('[horus] ISM + digimodes panels blocked');
        })();

        // Defer initialization until MessagePanel is available.
        // The plugin system loads plugins before openwebrx.js,
        // so MessagePanel may not be defined yet.
        this._deferredInit();
        return true;
    },

    _deferredInit: function() {
        if (typeof MessagePanel === "undefined") {
            if (this._initAttempts < this._maxInitAttempts) {
                this._initAttempts++;
                console.log("[horus] MessagePanel not ready — retrying in 200ms (attempt " +
                    this._initAttempts + "/" + this._maxInitAttempts + ")");
                setTimeout(() => this._deferredInit(), 200);
            } else {
                console.error("[horus] MessagePanel not available after " +
                    this._maxInitAttempts + " attempts — giving up");
            }
            return;
        }

        this._definePanelClass();
        this._createPanelDiv();
        this._hookRouting();

        console.log("[horus] Plugin initialized v" + this._version);
    },

    // ── Panel class definition ──────────────────────────────────────

    _definePanelClass: function() {
        if (typeof window.HorusMessagePanel !== "undefined") return;

        function HorusMessagePanel(el) {
            MessagePanel.call(this, el);
            // initClearButton() is already called by MessagePanel constructor
        }

        HorusMessagePanel.prototype = Object.create(MessagePanel.prototype);
        HorusMessagePanel.prototype.constructor = HorusMessagePanel;

        HorusMessagePanel.prototype.supportsMessage = function(message) {
            return message.mode && message.mode === "Horus";
        };

        HorusMessagePanel.prototype.render = function() {
            // Corrected HTML string concatenation to avoid syntax errors
            $(this.el).append($('<table>' +
                '<thead><tr>' +
                    '<th class="time">UTC</th>' +
                    '<th class="callsign">Callsign</th>' +
                    '<th class="sequence">Seq</th>' +
                    '<th class="position">Position</th>' +
                    '<th class="altitude">Alt (m)</th>' +
                    '<th class="snr">SNR</th>' +
                    '<th class="sensors">Sensors</th>' +
                '</tr></thead><tbody></tbody>' +
            '</table>'));
        };

        HorusMessagePanel.prototype.pushMessage = function(msg) {
            // Hardcoded OpenWebRX+ secondary_demod routing calls pushMessage()
            // directly and does NOT call Plugins.horus._showPanel(). Force the
            // panel visible here so decoded rows are actually seen in the GUI.
            Plugins.horus._showPanel();

            var $b = $(this.el).find("tbody");

            var timeStr = this.formatTime(msg.timestamp);
            var callsign = Utils.htmlEscape(msg.callsign || "???");
            var seq = msg.sequence !== undefined ? Utils.htmlEscape(String(msg.sequence)) : "-";
            var position = this.formatPosition(msg.lat, msg.lon);
            var altitude = this.formatAltitude(msg.altitude);
            var snr = msg.snr !== undefined ? Utils.htmlEscape(msg.snr.toFixed(1) + " dB") : "-";
            var sensors = this.formatSensors(msg);

            // Update map if position data is available
            if (msg.lat !== undefined && msg.lon !== undefined) {
                this.updateMap(msg.lat, msg.lon, msg.altitude, msg.callsign, msg.sequence);
            }

            var $row = $('<tr>' +
                '<td class="time">' + timeStr + '</td>' +
                '<td class="callsign">' + this.linkCallsign(callsign) + '</td>' +
                '<td class="sequence">' + seq + '</td>' +
                '<td class="position">' + position + '</td>' +
                '<td class="altitude">' + altitude + '</td>' +
                '<td class="snr">' + snr + '</td>' +
                '<td class="sensors">' + sensors + '</td>' +
            '</tr>');

            $b.append($row);
            this.scrollToBottom();
            this.clearMessages(200);
        };

        HorusMessagePanel.prototype.formatTime = function(timestamp) {
            if (!timestamp) return "-";
            try {
                var d = new Date(timestamp);
                if (isNaN(d.getTime())) return "-";
                return ("0" + d.getUTCHours()).slice(-2) + ":" +
                       ("0" + d.getUTCMinutes()).slice(-2) + ":" +
                       ("0" + d.getUTCSeconds()).slice(-2);
            } catch(e) {
                return "-";
            }
        };

        HorusMessagePanel.prototype.formatPosition = function(lat, lon) {
            if (lat === undefined || lon === undefined) return "-";
            var latStr = Math.abs(lat).toFixed(4) + (lat >= 0 ? "N" : "S");
            var lonStr = Math.abs(lon).toFixed(4) + (lon >= 0 ? "E" : "W");
            var link = '<a href="https://www.google.com/maps/search/?api=1&query=' +
                encodeURIComponent(lat) + ',' + encodeURIComponent(lon) +
                '" target="_blank" rel="noopener">' +
                Utils.htmlEscape(latStr + ' ' + lonStr) + '</a>';
            return link;
        };

        HorusMessagePanel.prototype.formatAltitude = function(alt) {
            if (alt === undefined || alt === null) return "-";
            return Utils.htmlEscape(alt.toLocaleString() + " m");
        };

        HorusMessagePanel.prototype.linkCallsign = function(callsign) {
            return '<a href="https://amateur.sondehub.org/#!mt=Mapnik&mz=9&qm=6_hours' +
                '&q=' + encodeURIComponent(callsign) +
                '" target="_blank" rel="noopener">' + callsign + '</a>';
        };

        HorusMessagePanel.prototype.formatSensors = function(msg) {
            var parts = [];
            if (msg.temperature !== undefined)
                parts.push(Utils.htmlEscape(msg.temperature.toFixed(1) + "°C"));
            if (msg.humidity !== undefined)
                parts.push(Utils.htmlEscape(msg.humidity.toFixed(0) + "%RH"));
            if (msg.pressure !== undefined)
                parts.push(Utils.htmlEscape(msg.pressure.toFixed(1) + "hPa"));
            if (msg.battery !== undefined)
                parts.push(Utils.htmlEscape(msg.battery.toFixed(2) + "V"));
            else if (msg.battery_voltage !== undefined)
                parts.push(Utils.htmlEscape(msg.battery_voltage.toFixed(2) + "V"));
            if (msg.sats !== undefined)
                parts.push(Utils.htmlEscape(String(msg.sats) + " sats"));
            if (msg.speed !== undefined)
                parts.push(Utils.htmlEscape(msg.speed.toFixed(0) + "km/h"));
            if (msg.ascent_rate !== undefined)
                parts.push(Utils.htmlEscape(msg.ascent_rate.toFixed(1) + "m/s"));

            var customNames = msg.custom_field_names || [];
            for (var i = 0; i < customNames.length; i++) {
                var name = customNames[i];
                if (msg[name] !== undefined) {
                    var val = msg[name];
                    if (typeof val === "number" && val % 1 !== 0) val = val.toFixed(2);
                    parts.push(Utils.htmlEscape(name + ":" + val));
                }
            }
            return parts.length > 0 ? parts.join(" | ") : "-";
        };

        HorusMessagePanel.prototype.updateMap = function(lat, lon, alt, callsign, seq) {
            // Add to path
            Plugins.horus._mapPathPoints.push([lat, lon]);
            if (Plugins.horus._mapPathPoints.length > 500) Plugins.horus._mapPathPoints.shift(); // Keep path recent

            // Initialize map if needed
            // Ensure L (Leaflet) and rx (OpenWebRX map instance) are available
            if (typeof L !== 'undefined' && typeof rx !== 'undefined' && rx.map) {
                if (!Plugins.horus._mapPath) {
                    Plugins.horus._mapPath = L.polyline(Plugins.horus._mapPathPoints, {color: '#8888ff', weight: 3, opacity: 0.7}).addTo(rx.map);
                    console.log('[horus] Initialized map path');
                } else {
                    Plugins.horus._mapPath.setLatLngs(Plugins.horus._mapPathPoints);
                }

                // Add/update marker
                var markerPos = [lat, lon];
                var iconHtml = '<div class="horus-marker-pulse"></div><div class="horus-marker-inner">' + Utils.htmlEscape(callsign) + '</div>';
                var markerIcon = L.divIcon({
                    className: 'horus-map-marker',
                    html: iconHtml,
                    iconSize: [40, 40],
                    iconAnchor: [20, 20]
                });

                if (!Plugins.horus._mapMarker) {
                    Plugins.horus._mapMarker = L.marker(markerPos, { icon: markerIcon }).addTo(rx.map);
                    console.log('[horus] Initialized map marker');
                } else {
                    Plugins.horus._mapMarker.setLatLng(markerPos);
                    Plugins.horus._mapMarker.setIcon(markerIcon);
                    // Update marker inner HTML directly if needed
                    var inner = Plugins.horus._mapMarker.getElement().querySelector('.horus-marker-inner');
                    if (inner) inner.innerHTML = Utils.htmlEscape(callsign);
                }
            }
        };

        window.HorusMessagePanel = HorusMessagePanel;

        // jQuery widget registration — required for the framework's
        // $('#openwebrx-panel-horus-message').horusMessagePanel() call
        $.fn.horusMessagePanel = function() {
            if (!this.data("panel")) {
                this.data("panel", new HorusMessagePanel(this));
            }
            return this.data("panel");
        };

        console.log("[horus] HorusMessagePanel class registered");
    },

    // ── Panel div creation (with retry) ─────────────────────────────

    _createPanelDiv: function() {
        // Use the improved styling from the standalone script
        var divHtml = '<div style="background:#1a1a2e;color:#88ccff;padding:3px 8px;font-weight:bold;' +
            'display:flex;justify-content:space-between;align-items:center;' +
            'border-bottom:1px solid #333;position:sticky;top:0;z-index:1;font-size:12px;">' +
            '<span>▲ Horus Telemetry</span>' +
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


        if (document.getElementById("openwebrx-panel-horus-message")) {
            // Div already exists — just init the widget
            this._initWidget();
            return;
        }

        var container = document.getElementById("openwebrx-panels-container-left");
        if (!container) {
            this._initAttempts++;
            if (this._initAttempts < this._maxInitAttempts) {
                console.warn("[horus] Panels container not found — retrying in 500ms (attempt " +
                    this._initAttempts + "/" + this._maxInitAttempts + ")");
                setTimeout(() => this._createPanelDiv(), 500);
            } else {
                console.error("[horus] Panels container not found after " +
                    this._maxInitAttempts + " attempts — giving up");
            }
            return;
        }

        // Insert before the first message panel
        var firstMsg = container.querySelector(".openwebrx-message-panel");

        var div = document.createElement("div");
        div.id = "openwebrx-panel-horus-message";
        div.className = "openwebrx-panel openwebrx-message-panel";
        // Apply the inline styles from the standalone script for better appearance
        div.style.cssText = 'width:619px;max-height:250px;overflow-y:auto;' +
            'background:rgba(20,20,35,0.95);border:1px solid #444;border-radius:3px;' +
            'font-family:monospace;font-size:12px;color:#e0e0e0;display:block;' +
            'margin-bottom:4px;box-shadow:0 2px 8px rgba(0,0,0,0.6);flex-shrink:0;';
        div.setAttribute("data-panel-name", "horus-message");
        div.innerHTML = divHtml; // Set the innerHTML to the new divHtml

        if (firstMsg) {
            container.insertBefore(div, firstMsg);
        } else {
            container.appendChild(div);
        }
        
        // Add clear button functionality
        div.querySelector('#horus-clear').onclick = function() {
            var tbody = div.querySelector('tbody');
            if (tbody) tbody.innerHTML = '';
            Plugins.horus._mapPathPoints = [];
            if (Plugins.horus._mapPath && typeof rx !== 'undefined' && rx.map) {
                rx.map.removeLayer(Plugins.horus._mapPath);
                Plugins.horus._mapPath = null;
            }
            if (Plugins.horus._mapMarker && typeof rx !== 'undefined' && rx.map) {
                rx.map.removeLayer(Plugins.horus._mapMarker);
                Plugins.horus._mapMarker = null;
            }
        };


        console.log("[horus] Panel div created");
        this._initWidget();
    },

    // ── Widget initialization ──────────────────────────────────────

    _initWidget: function() {
        if (this._panel) return;  // already initialized

        var $el = $("#openwebrx-panel-horus-message");
        if ($el.length) {
            this._panel = $el.horusMessagePanel();
            console.log("[horus] jQuery widget initialized");

            // Flush any pending messages that arrived before the panel was ready
            // Consolidate both plugin's _pendingMessages and window._horusPendingMessages
            var pending = this._pendingMessages.concat(window._horusPendingMessages || []);
            window._horusPendingMessages = []; // Clear the global queue
            this._pendingMessages = [];        // Clear the plugin's queue

            if (pending.length > 0) {
                console.log("[horus] Flushing " + pending.length + " pending messages");
                for (var i = 0; i < pending.length; i++) {
                    if (this._panel.supportsMessage(pending[i])) {
                        this._showPanel();
                        this._panel.pushMessage(pending[i]);
                    }
                }
            }
        } else {
            console.warn("[horus] Panel div not found — widget deferred");
        }
    },

    // ── Panel visibility ────────────────────────────────────────────

    _showPanel: function() {
        var el = document.getElementById("openwebrx-panel-horus-message");
        if (!el) return;

        if (!this._visible) {
            // The styles are now set during creation in _createPanelDiv
            // Just ensure display is block
            el.style.display = "block";
            // No longer need to set other styles here as they are set during creation

            // Hide the empty digimodes placeholder if present
            var digi = document.getElementById("openwebrx-panel-digimodes");
            if (digi) digi.style.display = "none";
            // Also hide ISM message panel
            var ism = document.getElementById("openwebrx-panel-ism-message");
            if (ism) ism.style.display = "none";


            this._visible = true;
            console.log("[horus] Panel shown");

            // Monitor for external display changes (framework may hide on mode switch)
            this._watchDisplay(el);
        }

        // Ensure panel stays visible
        if (el.style.display === "none") {
            el.style.display = "block";
        }
    },

    _watchDisplay: function(el) {
        if (this._displayObserver) return;
        var self = this;
        this._displayObserver = new MutationObserver(function(mutations) {
            mutations.forEach(function(mutation) {
                if (mutation.attributeName === "style" &&
                    el.style.display === "none" &&
                    self._visible) {
                    console.log("[horus] Panel hidden externally — restoring visibility");
                    el.style.display = "block";
                }
            });
        });
        this._displayObserver.observe(el, {
            attributes: true,
            attributeFilter: ["style"]
        });
    },

    // ── Message routing hook (fallback safety net) ──────────────────

    _hookRouting: function() {
        var self = this;

        var attemptHook = function() {
            if (typeof window.secondary_demod_push_data === "function") {
                var origPush = window.secondary_demod_push_data;
                window.secondary_demod_push_data = function(value) {
                    // Primary path: if panel is ready, route Horus messages to it
                    if (self._panel && self._panel.supportsMessage(value)) {
                        self._showPanel();
                        self._panel.pushMessage(value);
                        return;
                    }

                    // Panel not ready yet but message is Horus — queue it
                    if (value && value.mode === "Horus") {
                        self._pendingMessages.push(value);
                        console.log("[horus] Queued message (panel not ready, " +
                            self._pendingMessages.length + " pending)");
                        // Retry panel initialization
                        if (!self._panel) {
                            self._createPanelDiv();
                        }
                        return;
                    }

                    // Not a Horus message — pass through to original handler
                    origPush.apply(this, arguments);
                };
                console.log("[horus] Message routing hooked (fallback)");
            } else {
                console.warn("[horus] secondary_demod_push_data not ready — retrying in 500ms");
                setTimeout(attemptHook, 500);
            }
        };

        attemptHook();
    }
};
