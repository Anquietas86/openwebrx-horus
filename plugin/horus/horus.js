/**
 * OpenWebRX+ Plugin: Horus Balloon Telemetry
 *
 * Displays decoded Horus Binary/RTTY telemetry in a dockable panel.
 * Listens for secondary_demod messages with mode === "Horus" and renders
 * them in a live scrolling table.
 *
 * Requires the utils plugin for server message event hooks.
 */

Plugins.horus = {
    _version: "1.0.0",
    _panel: null,
    _tbody: null,
    _maxRows: 200,

    init: function() {
        if (!Plugins.isLoaded("utils", "0.5")) {
            console.warn("[horus] utils plugin >= 0.5 required");
            return false;
        }

        this._createPanel();
        this._bindEvents();

        console.log("[horus] Plugin initialized v" + this._version);
        return true;
    },

    _createPanel: function() {
        var container = document.getElementById("openwebrx-panels-container-left");
        if (!container) {
            console.warn("[horus] Panel container not found, using fallback");
            container = document.body;
        }

        var panel = document.createElement("div");
        panel.id = "openwebrx-panel-horus-message";
        panel.className = "openwebrx-panel openwebrx-message-panel";
        panel.style.display = "none";
        panel.style.width = "619px";
        panel.setAttribute("data-panel-name", "horus-message");

        panel.innerHTML =
            '<div class="horus-panel-header">' +
                '<span class="horus-title">Horus Telemetry</span>' +
                '<button class="horus-clear-btn" title="Clear">&#x2715;</button>' +
            '</div>' +
            '<div class="horus-table-wrap">' +
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
                '</table>' +
            '</div>';

        container.appendChild(panel);

        this._panel = panel;
        this._tbody = panel.querySelector("tbody");

        var self = this;
        panel.querySelector(".horus-clear-btn").addEventListener("click", function() {
            self._tbody.innerHTML = "";
        });
    },

    _bindEvents: function() {
        var self = this;

        document.addEventListener("server:secondary_demod:before", function(e) {
            var msg = e.detail;
            if (msg && msg.value && msg.value.mode === "Horus") {
                self._pushMessage(msg.value);
            }
        });
    },

    _pushMessage: function(msg) {
        this._panel.style.display = "";

        var row = document.createElement("tr");
        row.innerHTML =
            '<td class="time">' + this._formatTime(msg.timestamp) + '</td>' +
            '<td class="callsign">' + this._linkCallsign(msg.callsign || "???") + '</td>' +
            '<td class="sequence">' + (msg.sequence !== undefined ? msg.sequence : "-") + '</td>' +
            '<td class="position">' + this._formatPosition(msg.lat, msg.lon) + '</td>' +
            '<td class="altitude">' + this._formatAltitude(msg.altitude) + '</td>' +
            '<td class="snr">' + (msg.snr !== undefined ? msg.snr.toFixed(1) + " dB" : "-") + '</td>' +
            '<td class="sensors">' + this._formatSensors(msg) + '</td>';

        this._tbody.appendChild(row);
        this._scrollToBottom();
        this._pruneRows();
    },

    _formatTime: function(timestamp) {
        if (!timestamp) return "-";
        try {
            var d = new Date(timestamp);
            return ("0" + d.getUTCHours()).slice(-2) + ":" +
                   ("0" + d.getUTCMinutes()).slice(-2) + ":" +
                   ("0" + d.getUTCSeconds()).slice(-2);
        } catch(e) {
            return "-";
        }
    },

    _formatPosition: function(lat, lon) {
        if (lat === undefined || lon === undefined) return "-";
        var latStr = Math.abs(lat).toFixed(4) + (lat >= 0 ? "N" : "S");
        var lonStr = Math.abs(lon).toFixed(4) + (lon >= 0 ? "E" : "W");
        return '<a href="https://www.google.com/maps/search/?api=1&query=' +
            lat + ',' + lon + '" target="_blank">' +
            latStr + ' ' + lonStr + '</a>';
    },

    _formatAltitude: function(alt) {
        if (alt === undefined || alt === null) return "-";
        return alt.toLocaleString() + " m";
    },

    _linkCallsign: function(callsign) {
        var escaped = callsign.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
        return '<a href="https://amateur.sondehub.org/#!mt=Mapnik&mz=9&qm=6_hours' +
            '&q=' + encodeURIComponent(callsign) + '" target="_blank">' +
            escaped + '</a>';
    },

    _formatSensors: function(msg) {
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

        if (parts.length === 0) return "-";
        return parts.join(" | ").replace(/&/g, "&amp;").replace(/</g, "&lt;");
    },

    _scrollToBottom: function() {
        var wrap = this._panel.querySelector(".horus-table-wrap");
        if (wrap) wrap.scrollTop = wrap.scrollHeight;
    },

    _pruneRows: function() {
        while (this._tbody.children.length > this._maxRows) {
            this._tbody.removeChild(this._tbody.firstChild);
        }
    }
};
