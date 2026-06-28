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
            $(this.el).append($(
                '<table>' +
                    '<thead><tr>' +
                        '<th class="time">UTC</th>' +
                        '<th class="callsign">Callsign</th>' +
                        '<th class="sequence">Seq</th>' +
                        '<th class="position">Position</th>' +
                        '<th class="altitude">Alt (m)</th>' +
                        '<th class="snr">SNR</th>' +
                        '<th class="sensors">Sensors</th>' +
                    '</tr></thead>' +
                    '<tbody></tbody>' +
                '</table>'
            ));
        };

        HorusMessagePanel.prototype.pushMessage = function(msg) {
            var $b = $(this.el).find("tbody");

            var timeStr = this.formatTime(msg.timestamp);
            var callsign = Utils.htmlEscape(msg.callsign || "???");
            var seq = msg.sequence !== undefined ? Utils.htmlEscape(String(msg.sequence)) : "-";
            var position = this.formatPosition(msg.lat, msg.lon);
            var altitude = this.formatAltitude(msg.altitude);
            var snr = msg.snr !== undefined ? Utils.htmlEscape(msg.snr.toFixed(1) + " dB") : "-";
            var sensors = this.formatSensors(msg);

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
        div.style.display = "none";
        div.style.width = "619px";
        div.setAttribute("data-panel-name", "horus-message");

        if (firstMsg) {
            container.insertBefore(div, firstMsg);
        } else {
            container.appendChild(div);
        }

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
            var pending = this._pendingMessages.concat(window._horusPendingMessages || []);
            window._horusPendingMessages = [];
            this._pendingMessages = [];
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
            el.style.display = "block";
            el.style.maxHeight = "300px";
            el.style.overflowY = "auto";
            el.style.flexShrink = "0";
            el.style.marginTop = "4px";
            el.style.background = "rgba(0,0,0,0.85)";

            // Hide the empty digimodes placeholder if present
            var digi = document.getElementById("openwebrx-panel-digimodes");
            if (digi) digi.style.display = "none";

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
