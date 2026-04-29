/*
 * tetra Receiver Plugin for OpenWebRX+ — v1.3
 *
 * Encryption semantics (v1.3):
 *   - Cell security class (TEA capability from NETINFO1) shown as amber
 *     informational badge when no call is active.
 *   - Active call encryption (from Basicinfo enc_mode bits) shown in red.
 *   - Clear call on TEA-capable cell shows green 'Clear (SC TEAn)'.
 *
 * License: MIT
 */

Plugins.tetra = Plugins.tetra || {};
Plugins.tetra._version = 1.3;
Plugins.tetra.networkNames = {};

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
		'<span class="tetra-key">Encryption</span><span class="tetra-val tetra-enc" id="tetra-enc">---</span>' +
		'</div></div>' +
		'<div class="tetra-section tetra-col-signal"><div class="tetra-section-title">Signal</div>' +
		'<div class="tetra-signal-grid"><span class="tetra-key">AFC</span><span class="tetra-val" id="tetra-afc">---</span>' +
		'<span class="tetra-key">Bursts/s</span><span class="tetra-val" id="tetra-burst-rate">---</span></div>' +
		'<div id="tetra-status-block" style="display:none"><div class="tetra-signal-separator"></div><div class="tetra-signal-subtitle">Status</div><div class="tetra-status-line" id="tetra-last-status">---</div></div>' +
		'<div id="tetra-sds-block" style="display:none"><div class="tetra-signal-separator"></div><div class="tetra-signal-subtitle">SDS</div><div class="tetra-sds-line" id="tetra-last-sds">---</div></div>' +
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
		this._cellTea = 'none';
		this._cellSc  = 0;
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
			this._cellSc  = data.cell_security_class || 0;
			this._cellTea = data.cell_tea || 'none';
			Plugins.tetra._setEnc(false, 'none', this._cellTea);

		} else if (type === 'freqinfo') {
			$('#tetra-dl').text(Plugins.tetra._formatFreq(data.dl_freq));
			$('#tetra-ul').text(Plugins.tetra._formatFreq(data.ul_freq));

		} else if (type === 'encinfo') {
			this._cellSc  = data.cell_security_class || this._cellSc;
			this._cellTea = data.cell_tea || this._cellTea;
			Plugins.tetra._setEnc(false, 'none', this._cellTea);

		} else if (type === 'burst') {
			$('#tetra-afc').text(data.afc !== undefined ? data.afc : '---');
			$('#tetra-burst-rate').text(data.burst_rate || '---');
			var si = (data.slot || 0);
			if (si >= 0 && si < this.slots.length) this.slots[si].el.addClass('active');

		} else if (type === 'call_setup' || type === 'call_connect' || type === 'tx_grant') {
			var si2 = (data.slot || 0);
			if (si2 >= 0 && si2 < this.slots.length) this.slots[si2].update(data);
			Plugins.tetra._setEnc(!!data.encrypted, data.encryption_type || 'none', this._cellTea);

		} else if (type === 'call_release') {
			for (var i = 0; i < this.slots.length; i++) this.slots[i].clear();
			Plugins.tetra._setEnc(false, 'none', this._cellTea);

		} else if (type === 'status') {
			$('#tetra-last-status').text((data.ssi || '?') + ' → ' + (data.ssi2 || '?') + '  Status ' + (data.status || '?'));
			$('#tetra-status-block').show();

		} else if (type === 'sds') {
			$('#tetra-last-sds').text((data.ssi || '?') + ' → ' + (data.ssi2 || '?') + ': ' + (data.text || ''));
			$('#tetra-sds-block').show();
		}
	};

	TetraMetaPanel.prototype.clear = function () {
		MetaPanel.prototype.clear.call(this);
		$('#tetra-netname, #tetra-mcc-mnc, #tetra-la, #tetra-dl, #tetra-ul, #tetra-cc, #tetra-afc, #tetra-burst-rate, #tetra-last-status, #tetra-last-sds').text('---');
		$('#tetra-enc').text('---').removeClass('enc-yes enc-no enc-tea enc-cell');
		$('#tetra-status-block, #tetra-sds-block').hide();
		this._cellTea = 'none'; this._cellSc = 0;
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
 * Update the encryption badge (3 states):
 *  encrypted=true               -> red  'TEA2 (active)' or 'ENC YES'
 *  encrypted=false, cellTea set -> amber 'Clear (SC TEA2)'
 *  encrypted=false, no cellTea  -> green 'Clear'
 */
Plugins.tetra._setEnc = function (encrypted, encType, cellTea) {
	var $el = $('#tetra-enc');
	$el.removeClass('enc-yes enc-no enc-tea enc-cell');

	if (encrypted) {
		var label = (encType || '').toUpperCase();
		if (label.indexOf('TEA') === 0) $el.text(label + ' (active)').addClass('enc-tea');
		else $el.text('ENC ' + (label || 'YES')).addClass('enc-yes');
		return;
	}

	var ct = (cellTea || 'none').toUpperCase();
	if (ct !== 'NONE' && ct !== '') $el.text('Clear (SC ' + ct + ')').addClass('enc-cell');
	else $el.text('Clear').addClass('enc-no');
};
