/**
 * OpenWebRX+ Plugin: Horus Balloon Telemetry
 *
 * Uses the standard MessagePanel framework (same as Packet, WSJT, etc.)
 * so the panel gets proper sizing, scrolling, and clear button from the
 * framework CSS. The plugin creates the panel div, defines the
 * HorusMessagePanel class, and hooks message routing.
 */

Plugins.horus = {
    _version: "2.0.0",
    _panel: null,

    init: function() {
        this._definePanel();
        this._createPanelDiv();
        this._initWidget();
        this._hookRouting();

        console.log("[horus] Plugin initialized v" + this._version);
        return true;
    },

    _definePanel: function() {
        if (typeof window.HorusMessagePanel !== "undefined") return;

        function HorusMessagePanel(el) {
            MessagePanel.call(this, el);
            this.initClearButton();
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
            var seq = msg.sequence !== undefined ? msg.sequence : "-";
            var position = this.formatPosition(msg.lat, msg.lon);
            var altitude = this.formatAltitude(msg.altitude);
            var snr = msg.snr !== undefined ? msg.snr.toFixed(1) + " dB" : "-";
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
            return '<a href="https://www.google.com/maps/search/?api=1&query=' +
                lat + ',' + lon + '" target="_blank">' +
                latStr + ' ' + lonStr + '</a>';
        };

        HorusMessagePanel.prototype.formatAltitude = function(alt) {
            if (alt === undefined || alt === null) return "-";
            return alt.toLocaleString() + " m";
        };

        HorusMessagePanel.prototype.linkCallsign = function(callsign) {
            return '<a href="https://amateur.sondehub.org/#!mt=Mapnik&mz=9&qm=6_hours' +
                '&q=' + encodeURIComponent(callsign) + '" target="_blank">' +
                callsign + '</a>';
        };

        HorusMessagePanel.prototype.formatSensors = function(msg) {
            var parts = [];
            if (msg.temperature !== undefined)
                parts.push(msg.temperature.toFixed(1) + "°C");
            if (msg.humidity !== undefined)
                parts.push(msg.humidity.toFixed(0) + "%RH");
            if (msg.pressure !== undefined)
                parts.push(msg.pressure.toFixed(1) + "hPa");
            if (msg.battery !== undefined)
                parts.push(msg.battery.toFixed(2) + "V");
            else if (msg.battery_voltage !== undefined)
                parts.push(msg.battery_voltage.toFixed(2) + "V");
            if (msg.sats !== undefined)
                parts.push(msg.sats + " sats");
            if (msg.speed !== undefined)
                parts.push(msg.speed.toFixed(0) + "km/h");
            if (msg.ascent_rate !== undefined)
                parts.push(msg.ascent_rate.toFixed(1) + "m/s");

            var customNames = msg.custom_field_names || [];
            for (var i = 0; i < customNames.length; i++) {
                var name = customNames[i];
                if (msg[name] !== undefined) {
                    var val = msg[name];
                    if (typeof val === "number" && val % 1 !== 0) val = val.toFixed(2);
                    parts.push(name + ":" + val);
                }
            }
            return parts.length > 0 ? Utils.htmlEscape(parts.join(" | ")) : "-";
        };

        window.HorusMessagePanel = HorusMessagePanel;

        $.fn.horusMessagePanel = function() {
            if (!this.data("panel")) {
                this.data("panel", new HorusMessagePanel(this));
            }
            return this.data("panel");
        };

        console.log("[horus] HorusMessagePanel class registered");
    },

    _createPanelDiv: function() {
        if (document.getElementById("openwebrx-panel-horus-message")) return;

        var container = document.getElementById("openwebrx-panels-container-left");
        if (!container) return;

        // Insert before the first message panel (same area as other digital mode panels)
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

        console.log("[horus] Panel div created (standard framework pattern)");
    },

    _initWidget: function() {
        var $el = $("#openwebrx-panel-horus-message");
        if ($el.length) {
            this._panel = $el.horusMessagePanel();
            console.log("[horus] jQuery widget initialized");
        }
    },

    _showPanel: function() {
        var el = document.getElementById("openwebrx-panel-horus-message");
        if (el && el.style.display === "none") {
            // Strip framework panel classes that collapse height to 0,
            // then apply our own layout styles
            el.className = "";
            el.style.cssText = "display:block; max-height:300px; overflow-y:auto; flex-shrink:0; width:619px; margin-top:4px; background:rgba(0,0,0,0.85);";
            // Also hide the empty digimodes grey box
            var digi = document.getElementById("openwebrx-panel-digimodes");
            if (digi) digi.style.display = "none";
        }
    },

    _hookRouting: function() {
        var self = this;

        // Hook the fallback — framework calls this when no built-in panel claims the message
        var origPush = window.secondary_demod_push_data;
        window.secondary_demod_push_data = function(value) {
            if (self._panel && self._panel.supportsMessage(value)) {
                self._showPanel();
                self._panel.pushMessage(value);
                return;
            }
            if (typeof origPush === "function") {
                origPush.apply(this, arguments);
            }
        };

        console.log("[horus] Message routing hooked");
    }
};
