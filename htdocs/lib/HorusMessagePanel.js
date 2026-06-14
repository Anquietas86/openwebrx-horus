/**
 * OpenWebRX Horus Balloon Telemetry Panel
 *
 * Displays decoded Horus Binary/RTTY telemetry in a table alongside the
 * waterfall. Follows the same MessagePanel pattern as WSJT, Packet, etc.
 */

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
    var $b = $(this.el).find('tbody');

    var timeStr = this.formatTime(msg.timestamp);
    var callsign = Utils.htmlEscape(msg.callsign || '???');
    var seq = msg.sequence !== undefined ? msg.sequence : '-';
    var position = this.formatPosition(msg.lat, msg.lon);
    var altitude = this.formatAltitude(msg.altitude);
    var snr = msg.snr !== undefined ? msg.snr.toFixed(1) + ' dB' : '-';
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
    if (!timestamp) return '-';
    try {
        var d = new Date(timestamp);
        return ('0' + d.getUTCHours()).slice(-2) + ':' +
               ('0' + d.getUTCMinutes()).slice(-2) + ':' +
               ('0' + d.getUTCSeconds()).slice(-2);
    } catch(e) {
        return '-';
    }
};

HorusMessagePanel.prototype.formatPosition = function(lat, lon) {
    if (lat === undefined || lon === undefined) return '-';
    var latStr = Math.abs(lat).toFixed(4) + (lat >= 0 ? 'N' : 'S');
    var lonStr = Math.abs(lon).toFixed(4) + (lon >= 0 ? 'E' : 'W');
    var link = '<a href="https://www.google.com/maps/search/?api=1&query=' +
        lat + ',' + lon + '" target="_blank">' +
        latStr + ' ' + lonStr + '</a>';
    return link;
};

HorusMessagePanel.prototype.formatAltitude = function(alt) {
    if (alt === undefined || alt === null) return '-';
    return alt.toLocaleString() + ' m';
};

HorusMessagePanel.prototype.linkCallsign = function(callsign) {
    return '<a href="https://amateur.sondehub.org/#!mt=Mapnik&mz=9&qm=6_hours' +
        '&q=' + encodeURIComponent(callsign) + '" target="_blank">' +
        callsign + '</a>';
};

HorusMessagePanel.prototype.formatSensors = function(msg) {
    var parts = [];

    if (msg.temperature !== undefined) {
        parts.push(msg.temperature.toFixed(1) + '°C');
    }
    if (msg.humidity !== undefined) {
        parts.push(msg.humidity.toFixed(0) + '%RH');
    }
    if (msg.pressure !== undefined) {
        parts.push(msg.pressure.toFixed(1) + 'hPa');
    }
    if (msg.battery !== undefined) {
        parts.push(msg.battery.toFixed(2) + 'V');
    } else if (msg.battery_voltage !== undefined) {
        parts.push(msg.battery_voltage.toFixed(2) + 'V');
    }
    if (msg.sats !== undefined) {
        parts.push(msg.sats + ' sats');
    }
    if (msg.speed !== undefined) {
        parts.push(msg.speed.toFixed(0) + 'km/h');
    }
    if (msg.ascent_rate !== undefined) {
        parts.push(msg.ascent_rate.toFixed(1) + 'm/s');
    }

    // v3 custom fields
    var customNames = msg.custom_field_names || [];
    for (var i = 0; i < customNames.length; i++) {
        var name = customNames[i];
        if (msg[name] !== undefined) {
            var val = msg[name];
            if (typeof val === 'number' && val % 1 !== 0) {
                val = val.toFixed(2);
            }
            parts.push(name + ':' + val);
        }
    }

    return parts.length > 0 ? Utils.htmlEscape(parts.join(' | ')) : '-';
};

// jQuery widget registration
$.fn.horusMessagePanel = function() {
    if (!this.data('panel')) {
        this.data('panel', new HorusMessagePanel(this));
    }
    return this.data('panel');
};
