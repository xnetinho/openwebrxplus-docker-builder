/*
 * tetra Receiver Plugin for OpenWebRX+
 *
 * Displays TETRA (Terrestrial Trunked Radio) signaling metadata:
 * network info, encryption mode, 4 TDMA timeslots, status and SDS messages.
 *
 * Requires the xnetinho/openwebrxplus-tetra Docker image (backend decoder).
 *
 * Load via init.js:
 *   await Plugins.load('https://xnetinho.github.io/openwebrxplus-docker-builder/plugins/receiver/tetra/tetra.js');
 *
 * License: MIT
 * Copyright (c) 2026 xnetinho
 *
 * Changes:
 * 1.1:
 *  - robust TETMON parser: NETINFO1, FREQINFO1, ENCINFO1, DSETUPDEC,
 *    DCONNECTDEC, DTXGRANTDEC, DRELEASEDEC, DSTATUSDEC, SDSDEC, BURST
 *  - granular per-type rate limiting in backend
 *  - two-stage codec (cdecoder | sdecoder) in backend
 *  - GNURadio pi/4-DQPSK demodulation stage connected to tetra-rx
 *  - new panel fields: Location Area (LA), encryption mode (TEA1/2/3),
 *    network name map, status messages, SDS messages
 *  - panel layout expanded proportionally to data; no overflow
 * 1.0:
 *  - initial release
 */

Plugins.tetra = Plugins.tetra || {};
Plugins.tetra._version = 1.1;

/*
 * Optional network name map: add entries as 'MCC-MNC': 'Name'.
 * Example: Plugins.tetra.networkNames['724-05'] = 'TIM Brasil';
 */
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

// ── Panel HTML injection ───────────────────────────────────────────────────────

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
						'<img class="groupcall"  src="static/gfx/openwebrx-groupcall.svg">' +
					'</div>' +
				'</div>' +
				'<div class="tetra-slot-body">' +
					'<div class="tetra-slot-row">' +
						'<span class="tetra-slot-key">ISSI</span>' +
						'<span class="tetra-slot-val tetra-issi" id="tetra-s' + i + '-issi">---</span>' +
					'</div>' +
					'<div class="tetra-slot-row">' +
						'<span class="tetra-slot-key">GSSI</span>' +
						'<span class="tetra-slot-val tetra-gssi" id="tetra-s' + i + '-gssi">---</span>' +
					'</div>' +
					'<div class="tetra-slot-row">' +
						'<span class="tetra-slot-key">Type</span>' +
						'<span class="tetra-slot-val tetra-calltype" id="tetra-s' + i + '-type">---</span>' +
					'</div>' +
				'</div>' +
			'</div>';
	}

	var html =
		'<div class="openwebrx-panel openwebrx-meta-panel tetra-panel" ' +
			'id="openwebrx-panel-metadata-tetra" ' +
			'style="display:none" ' +
			'data-panel-name="metadata-tetra">' +

			'<div class="tetra-section">' +
				'<div class="tetra-section-title">Network</div>' +
				'<div class="tetra-grid">' +
					'<span class="tetra-key">Name</span>' +
					'<span class="tetra-val tetra-wide" id="tetra-netname">---</span>' +
					'<span class="tetra-key">MCC / MNC</span>' +
					'<span class="tetra-val" id="tetra-mcc-mnc">---</span>' +
					'<span class="tetra-key">LA</span>' +
					'<span class="tetra-val" id="tetra-la">---</span>' +
					'<span class="tetra-key">DL Freq</span>' +
					'<span class="tetra-val" id="tetra-dl">---</span>' +
					'<span class="tetra-key">UL Freq</span>' +
					'<span class="tetra-val" id="tetra-ul">---</span>' +
					'<span class="tetra-key">Color Code</span>' +
					'<span class="tetra-val" id="tetra-cc">---</span>' +
					'<span class="tetra-key">Encryption</span>' +
					'<span class="tetra-val tetra-enc" id="tetra-enc">---</span>' +
				'</div>' +
			'</div>' +

			'<div class="tetra-section">' +
				'<div class="tetra-section-title">Signal</div>' +
				'<div class="tetra-grid">' +
					'<span class="tetra-key">AFC</span>' +
					'<span class="tetra-val" id="tetra-afc">---</span>' +
					'<span class="tetra-key">Bursts/s</span>' +
					'<span class="tetra-val" id="tetra-burst-rate">---</span>' +
				'</div>' +
			'</div>' +

			'<div class="tetra-section">' +
				'<div class="tetra-section-title">Timeslots</div>' +
				slots +
			'</div>' +

			'<div class="tetra-section tetra-messages" id="tetra-status-section" style="display:none">' +
				'<div class="tetra-section-title">Status</div>' +
				'<div class="tetra-message-row" id="tetra-last-status">---</div>' +
			'</div>' +

			'<div class="tetra-section tetra-messages" id="tetra-sds-section" style="display:none">' +
				'<div class="tetra-section-title">SDS</div>' +
				'<div class="tetra-message-row" id="tetra-last-sds">---</div>' +
			'</div>' +

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
};

// ── MetaPanel subclass ─────────────────────────────────────────────────────────

Plugins.tetra._registerMetaPanel = function () {

	// ── TetraMetaSlot ──────────────────────────────────────────────────────────

	function TetraMetaSlot(el) {
		this.el = $(el);
		this.idx = parseInt(this.el.attr('id').replace('tetra-slot-', ''), 10);
	}

	TetraMetaSlot.prototype.update = function (data) {
		var callType = (data.call_type || 'group').toLowerCase();

		this.el.addClass('active').removeClass('groupcall directcall emergency');
		if (callType.indexOf('individual') >= 0 || callType.indexOf('direct') >= 0) {
			this.el.addClass('directcall');
		} else if (callType.indexOf('emergency') >= 0) {
			this.el.addClass('emergency');
		} else {
			this.el.addClass('groupcall');
		}

		if (data.encrypted) {
			this.el.addClass('encrypted');
		} else {
			this.el.removeClass('encrypted');
		}

		this._set('issi', data.issi || '---');
		this._set('gssi', data.gssi || '---');
		this._set('type', callType || '---');
	};

	TetraMetaSlot.prototype.clear = function () {
		this.el.removeClass('active groupcall directcall emergency encrypted');
		this._set('issi', '---');
		this._set('gssi', '---');
		this._set('type', '---');
	};

	TetraMetaSlot.prototype._set = function (field, value) {
		$('#tetra-s' + this.idx + '-' + field).text(value);
	};

	// ── TetraMetaPanel ─────────────────────────────────────────────────────────

	function TetraMetaPanel($el) {
		MetaPanel.call(this, $el);
		this.modes = ['TETRA'];
		this.slots = this.el.find('.openwebrx-tetra-slot').toArray().map(function (el) {
			return new TetraMetaSlot(el);
		});
	}

	TetraMetaPanel.prototype = Object.create(MetaPanel.prototype);
	TetraMetaPanel.prototype.constructor = TetraMetaPanel;

	TetraMetaPanel.prototype.update = function (data) {
		if (!this.isSupported(data)) return;
		var type = data.type;

		if (type === 'netinfo') {
			var mcc = data.mcc || '?';
			var mnc = data.mnc || '?';
			var netKey = mcc + '-' + mnc;
			var netName = Plugins.tetra.networkNames[netKey] || '---';
			$('#tetra-netname').text(netName);
			$('#tetra-mcc-mnc').text(mcc + ' / ' + mnc);
			$('#tetra-la').text(data.la || '---');
			$('#tetra-dl').text(Plugins.tetra._formatFreq(data.dl_freq));
			$('#tetra-ul').text(Plugins.tetra._formatFreq(data.ul_freq));
			$('#tetra-cc').text(data.color_code !== undefined && data.color_code !== '' ? data.color_code : '---');
			Plugins.tetra._setEnc(data.encrypted, null);

		} else if (type === 'freqinfo') {
			$('#tetra-dl').text(Plugins.tetra._formatFreq(data.dl_freq));
			$('#tetra-ul').text(Plugins.tetra._formatFreq(data.ul_freq));

		} else if (type === 'encinfo') {
			Plugins.tetra._setEnc(data.encrypted, data.enc_mode);

		} else if (type === 'burst') {
			$('#tetra-afc').text(data.afc !== undefined ? data.afc : '---');
			$('#tetra-burst-rate').text(data.burst_rate || '---');
			var si = (data.slot || 0);
			if (si >= 0 && si < this.slots.length) {
				this.slots[si].el.addClass('active');
			}

		} else if (type === 'call_setup' || type === 'connect' || type === 'tx_grant') {
			var si2 = (data.slot || 0);
			if (si2 >= 0 && si2 < this.slots.length) {
				this.slots[si2].update(data);
			}

		} else if (type === 'call_release') {
			for (var i = 0; i < this.slots.length; i++) {
				this.slots[i].clear();
			}

		} else if (type === 'status') {
			var stxt = (data.issi || '?') + ' \u2192 ' + (data.to || '?') + ': Status ' + (data.status || '?');
			$('#tetra-last-status').text(stxt);
			$('#tetra-status-section').show();

		} else if (type === 'sds') {
			var sdstxt = (data.from || '?') + ' \u2192 ' + (data.to || '?') + ': ' + (data.text || '');
			$('#tetra-last-sds').text(sdstxt);
			$('#tetra-sds-section').show();
		}
	};

	TetraMetaPanel.prototype.clear = function () {
		MetaPanel.prototype.clear.call(this);
		$('#tetra-netname').text('---');
		$('#tetra-mcc-mnc').text('---');
		$('#tetra-la').text('---');
		$('#tetra-dl').text('---');
		$('#tetra-ul').text('---');
		$('#tetra-cc').text('---');
		$('#tetra-enc').text('---').removeClass('enc-yes enc-no enc-tea');
		$('#tetra-afc').text('---');
		$('#tetra-burst-rate').text('---');
		$('#tetra-status-section').hide();
		$('#tetra-sds-section').hide();
		for (var i = 0; i < this.slots.length; i++) {
			this.slots[i].clear();
		}
	};

	// ── Register ───────────────────────────────────────────────────────────────

	MetaPanel.types['tetra'] = TetraMetaPanel;

	var $panel = $('#openwebrx-panel-metadata-tetra');
	if ($panel.length && !$panel.data('metapanel')) {
		$panel.metaPanel();
	}
};

// ── Helpers ────────────────────────────────────────────────────────────────────

Plugins.tetra._formatFreq = function (hz) {
	if (!hz) return '---';
	var n = parseFloat(hz);
	if (isNaN(n) || n === 0) return '---';
	return (n / 1e6).toFixed(4) + ' MHz';
};

Plugins.tetra._setEnc = function (encrypted, encMode) {
	var $el = $('#tetra-enc');
	$el.removeClass('enc-yes enc-no enc-tea');
	if (encrypted) {
		var label = (encMode && encMode !== 'None') ? encMode : 'YES';
		$el.text(label).addClass(encMode && encMode !== 'None' ? 'enc-tea' : 'enc-yes');
	} else {
		$el.text('No').addClass('enc-no');
	}
};
