/*
 * tetra Receiver Plugin for OpenWebRX+ — v1.4
 *
 * Encryption display split into two distinct fields:
 *   - Cell Sec.  : TEA capability advertised by the BS (NETINFO1 CRYPT).
 *                  Informational only — amber when set, dimmed when none.
 *   - Encryption : actual per-call encryption from Basicinfo enc_mode bits.
 *                  Red 'TEA2 (active)' when encrypted, green 'Clear' during
 *                  a clear call, dimmed 'idle' when no call is active.
 *
 * STATUS / SDS panels are always visible. Empty default '---'. ssi2=0 is
 * rendered as 'broadcast' (TETRA destination 0 means open broadcast).
 *
 * Optional: populate Plugins.tetra.statusNames = {528: 'PTT timeout', ...}
 * to map operator-specific D-STATUS codes to friendly labels.
 *
 * License: MIT
 */

Plugins.tetra = Plugins.tetra || {};
Plugins.tetra._version = 1.4;
Plugins.tetra.networkNames = {};
Plugins.tetra.statusNames  = {};

Plugins.tetra.init = function () {
	if (!Plugins.isLoaded('utils', 0.1)) {
		console.error('[tetra] plugin requires "utils >= 0.1".');
		return false;
	}
	Plugins.utils.on_ready(function () {
		Plugins.tetra._injectPanel();
		Plugins.tetra._registerMetaPanel();
	});
	return true;
};

Plugins.tetra._injectPanel = function () {
	if (document.getElementById('openwebrx-panel-metadata-tetra')) return;

	var slots = '';
	for (var i = 1; i <= 4; i++) {
		slots +=
			'<div class="openwebrx-tetra-slot" id="tetra-slot-' + i + '">' +
				'<div class="tetra-slot-left">' +
					'<span class="tetra-slot-number">TS ' + i + '</span>' +
					'<div class="openwebrx-meta-user-image">' +
						'<img class="directcall" src="static/gfx/openwebrx-directcall.svg">' +
						'<img class="groupcall" src="static/gfx/openwebrx-groupcall.svg">' +
					'</div>' +
				'</div>' +
				'<div class="tetra-slot-body">' +
					'<div class="tetra-slot-row"><span class="tetra-slot-key">ISSI</span><span class="tetra-slot-val tetra-issi" id="tetra-s' + i + '-issi">---</span></div>' +
					'<div class="tetra-slot-row"><span class="tetra-slot-key">GSSI</span><span class="tetra-slot-val tetra-gssi" id="tetra-s' + i + '-gssi">---</span></div>' +
					'<div class="tetra-slot-row"><span class="tetra-slot-key">Type</span><span class="tetra-slot-val tetra-calltype" id="tetra-s' + i + '-type">---</span></div>' +
				'</div>' +
			'</div>';
	}

	var html =
		'<div class="openwebrx-panel openwebrx-meta-panel tetra-panel" id="openwebrx-panel-metadata-tetra" style="display:none" data-panel-name="metadata-tetra">' +
		'<div class="tetra-section tetra-col-network"><div class="tetra-section-title">Network</div><div class="tetra-grid">' +
		'<span class="tetra-key">Name</span><span class="tetra-val tetra-wide" id="tetra-netname">---</span>' +
		'<span class="tetra-key">MCC / MNC</span><span class="tetra-val" id="tetra-mcc-mnc">---</span>' +
		'<span class="tetra-key">LA</span><span class="tetra-val" id="tetra-la">---</span>' +
		'<span class="tetra-key">DL Freq</span><span class="tetra-val" id="tetra-dl">---</span>' +
		'<span class="tetra-key">UL Freq</span><span class="tetra-val" id="tetra-ul">---</span>' +
		'<span class="tetra-key">Color Code</span><span class="tetra-val" id="tetra-cc">---</span>' +
		'<span class="tetra-key">Cell Sec.</span><span class="tetra-val tetra-cellsec cell-none" id="tetra-cellsec">---</span>' +
		'<span class="tetra-key">Encryption</span><span class="tetra-val tetra-enc enc-idle" id="tetra-enc">idle</span>' +
		'</div></div>' +
		'<div class="tetra-section tetra-col-signal"><div class="tetra-section-title">Signal</div>' +
		'<div class="tetra-signal-grid"><span class="tetra-key">AFC</span><span class="tetra-val" id="tetra-afc">---</span>' +
		'<span class="tetra-key">Bursts/s</span><span class="tetra-val" id="tetra-burst-rate">---</span></div>' +
		'<div id="tetra-status-block"><div class="tetra-signal-separator"></div><div class="tetra-signal-subtitle">Status</div><div class="tetra-status-line idle" id="tetra-last-status">---</div></div>' +
		'<div id="tetra-sds-block"><div class="tetra-signal-separator"></div><div class="tetra-signal-subtitle">SDS</div><div class="tetra-sds-line idle" id="tetra-last-sds">---</div></div>' +
		'</div>' +
		'<div class="tetra-section tetra-col-timeslots"><div class="tetra-section-title">Timeslots</div>' + slots + '</div>' +
		'</div>';

	var dmr = document.getElementById('openwebrx-panel-metadata-dmr');
	if (dmr && dmr.parentNode) {
		dmr.insertAdjacentHTML('afterend', html);
	} else {
		var existing = document.querySelector('.openwebrx-meta-panel');
		if (existing && existing.parentNode) {
			existing.parentNode.insertAdjacentHTML('beforeend', html);
		} else {
			document.body.insertAdjacentHTML('beforeend', html);
		}
	}

	var panelEl = document.getElementById('openwebrx-panel-metadata-tetra');
	if (panelEl) {
		panelEl.addEventListener('transitionend', function(ev) {
			if (ev.target !== this) return;
			this.style.transitionDuration = null;
			this.style.transitionProperty = null;
			if (this.movement && this.movement === 'collapse') this.style.display = 'none';
			delete this.movement;
		});
	}

	Plugins.tetra._clearPanel = function() {
		try {
			var panel = $('#openwebrx-panel-metadata-tetra');
			var instance = panel.data('metapanel');
			if (instance && typeof instance.clear === 'function') instance.clear();
		} catch (e) {}
	};

	if (!Plugins.tetra._isUiHooked) {
		Plugins.utils.wrap_func('setFrequency', function() { Plugins.tetra._clearPanel(); return true; }, null, UI);
		Plugins.utils.wrap_func('setOffsetFrequency', function() { Plugins.tetra._clearPanel(); return true; }, null, UI);
		if (typeof Demodulator !== 'undefined' && Demodulator.prototype) {
			Plugins.utils.wrap_func('set_offset_frequency', function() { Plugins.tetra._clearPanel(); return true; }, null, Demodulator.prototype);
		}
		Plugins.tetra._isUiHooked = true;
	}
};

Plugins.tetra._registerMetaPanel = function () {

	function TetraMetaSlot(el) {
		this.el = $(el);
		this.idx = parseInt(this.el.attr('id').replace('tetra-slot-', ''), 10);
	}

	TetraMetaSlot.prototype.update = function (data) {
		var callType = (data.call_type || 'group').toLowerCase();
		this.el.addClass('active').removeClass('groupcall directcall emergency');
		if (callType.indexOf('individual') >= 0 || callType.indexOf('direct') >= 0) this.el.addClass('directcall');
		else if (callType.indexOf('emergency') >= 0) this.el.addClass('emergency');
		else this.el.addClass('groupcall');
		if (data.encrypted) this.el.addClass('encrypted'); else this.el.removeClass('encrypted');
		this._set('issi', data.issi || '---');
		this._set('gssi', data.gssi || '---');
		this._set('type', callType || '---');
	};

	TetraMetaSlot.prototype.clear = function () {
		this.el.removeClass('active groupcall directcall emergency encrypted');
		this._set('issi', '---'); this._set('gssi', '---'); this._set('type', '---');
	};

	TetraMetaSlot.prototype._set = function (field, value) {
		$('#tetra-s' + this.idx + '-' + field).text(value);
	};

	function TetraMetaPanel($el) {
		MetaPanel.call(this, $el);
		this.modes = ['TETRA'];
		this.slots = this.el.find('.openwebrx-tetra-slot').toArray().map(function (el) { return new TetraMetaSlot(el); });
	}

	TetraMetaPanel.prototype = Object.create(MetaPanel.prototype);
	TetraMetaPanel.prototype.constructor = TetraMetaPanel;

	TetraMetaPanel.prototype.update = function (data) {
		if (!this.isSupported(data)) return;
		var type = data.type;

		if (type === 'netinfo') {
			var mcc = data.mcc || '?', mnc = data.mnc || '?';
			var netName = Plugins.tetra.networkNames[mcc + '-' + mnc] || '---';
			$('#tetra-netname').text(netName);
			$('#tetra-mcc-mnc').text(mcc + ' / ' + mnc);
			$('#tetra-la').text(data.la || '---');
			$('#tetra-dl').text(Plugins.tetra._formatFreq(data.dl_freq));
			$('#tetra-ul').text(Plugins.tetra._formatFreq(data.ul_freq));
			$('#tetra-cc').text(data.color_code !== undefined && data.color_code !== '' ? data.color_code : '---');
			Plugins.tetra._setCellSec(data.cell_tea, data.cell_security_class);

		} else if (type === 'freqinfo') {
			$('#tetra-dl').text(Plugins.tetra._formatFreq(data.dl_freq));
			$('#tetra-ul').text(Plugins.tetra._formatFreq(data.ul_freq));

		} else if (type === 'encinfo') {
			Plugins.tetra._setCellSec(data.cell_tea, data.cell_security_class);

		} else if (type === 'burst') {
			$('#tetra-afc').text(data.afc !== undefined ? data.afc : '---');
			$('#tetra-burst-rate').text(data.burst_rate || '---');
			var si = (data.slot || 0);
			if (si >= 0 && si < this.slots.length) this.slots[si].el.addClass('active');

		} else if (type === 'call_setup' || type === 'call_connect' || type === 'tx_grant') {
			var si2 = (data.slot || 0);
			if (si2 >= 0 && si2 < this.slots.length) this.slots[si2].update(data);
			Plugins.tetra._setEnc(!!data.encrypted, data.encryption_type || 'none');

		} else if (type === 'call_release') {
			for (var i = 0; i < this.slots.length; i++) this.slots[i].clear();
			Plugins.tetra._setEnc(null, null);  // back to idle

		} else if (type === 'status') {
			$('#tetra-last-status').removeClass('idle').text(Plugins.tetra._fmtStatus(data));

		} else if (type === 'sds') {
			$('#tetra-last-sds').removeClass('idle').text(Plugins.tetra._fmtSds(data));
		}
	};

	TetraMetaPanel.prototype.clear = function () {
		MetaPanel.prototype.clear.call(this);
		$('#tetra-netname, #tetra-mcc-mnc, #tetra-la, #tetra-dl, #tetra-ul, #tetra-cc, #tetra-afc, #tetra-burst-rate').text('---');
		$('#tetra-cellsec').text('---').removeClass('cell-tea cell-none').addClass('cell-none');
		Plugins.tetra._setEnc(null, null);
		$('#tetra-last-status').addClass('idle').text('---');
		$('#tetra-last-sds').addClass('idle').text('---');
		for (var i = 0; i < this.slots.length; i++) this.slots[i].clear();
	};

	MetaPanel.types['tetra'] = TetraMetaPanel;
	var $panel = $('#openwebrx-panel-metadata-tetra');
	if ($panel.length && !$panel.data('metapanel')) $panel.metaPanel();
};

Plugins.tetra._formatFreq = function (hz) {
	if (!hz) return '---';
	var n = parseFloat(hz);
	if (isNaN(n) || n === 0) return '---';
	return (n / 1e6).toFixed(4) + ' MHz';
};

/**
 * Cell-level security capability advertised by the BS (NETINFO1/ENCINFO1).
 * Informational only — does NOT mean the current call is encrypted.
 */
Plugins.tetra._setCellSec = function (cellTea, cellSc) {
	var $el = $('#tetra-cellsec');
	$el.removeClass('cell-tea cell-none');
	var ct = (cellTea || 'none').toLowerCase();
	if (ct !== 'none' && ct !== '') {
		var label = cellTea.toUpperCase();
		if (cellSc !== undefined && cellSc !== null && cellSc !== '') label += ' (SC ' + cellSc + ')';
		$el.text(label).addClass('cell-tea');
	} else {
		$el.text('None').addClass('cell-none');
	}
};

/**
 * Per-call active encryption from Basicinfo enc_mode bits (1/2/3 = TEA1/2/3).
 *  encrypted=true            -> red 'TEA2 (active)' / 'ENC YES'
 *  encrypted=false (in call) -> green 'Clear'
 *  encrypted=null            -> dim 'idle' (no call active)
 */
Plugins.tetra._setEnc = function (encrypted, encType) {
	var $el = $('#tetra-enc');
	$el.removeClass('enc-yes enc-no enc-tea enc-idle');

	if (encrypted === null || encrypted === undefined) {
		$el.text('idle').addClass('enc-idle');
		return;
	}
	if (encrypted) {
		var label = (encType || '').toUpperCase();
		if (label.indexOf('TEA') === 0) $el.text(label + ' (active)').addClass('enc-tea');
		else $el.text('ENC ' + (label || 'YES')).addClass('enc-yes');
		return;
	}
	$el.text('Clear').addClass('enc-no');
};

Plugins.tetra._fmtStatus = function (data) {
	var from = data.ssi || data.from || '?';
	var to = (data.ssi2 && data.ssi2 !== '0' && data.ssi2 !== 0) ? data.ssi2 : 'broadcast';
	var code = data.status || '?';
	var name = Plugins.tetra.statusNames[code];
	return from + ' → ' + to + '  code ' + code + (name ? ' (' + name + ')' : '');
};

Plugins.tetra._fmtSds = function (data) {
	var from = data.ssi || data.from || '?';
	var to = (data.ssi2 && data.ssi2 !== '0' && data.ssi2 !== 0) ? data.ssi2 : 'broadcast';
	return from + ' → ' + to + ': ' + (data.text || '');
};
