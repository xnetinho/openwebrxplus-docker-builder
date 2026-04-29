/*
 * tetra Receiver Plugin for OpenWebRX+ — v1.2
 *
 * Displays TETRA (Terrestrial Trunked Radio) signaling metadata:
 * network info, encryption mode (TEA1/2/3), 4 TDMA timeslots,
 * status and SDS messages.
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
 * 1.2:
 *  - receives encryption_type from backend (TEA1/TEA2/TEA3/none)
 *  - STATUS and SDS moved into SIGNAL column
 *  - fixed-width columns to prevent layout dancing
 *  - improved _setEnc to use encryption_type field
 * 1.1:
 *  - robust TETMON parser, GNURadio demod, codec pipeline
 * 1.0:
 *  - initial release
 */

Plugins.tetra = Plugins.tetra || {};
Plugins.tetra._version = 1.2;

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

			/* ── Coluna 1: NETWORK ─────────────────────────────── */
			'<div class="tetra-section tetra-col-network">' +
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

			/* ── Coluna 2: SIGNAL + STATUS + SDS ───────────────── */
			'<div class="tetra-section tetra-col-signal">' +
				'<div class="tetra-section-title">Signal</div>' +
				'<div class="tetra-signal-grid">' +
					'<span class="tetra-key">AFC</span>' +
					'<span class="tetra-val" id="tetra-afc">---</span>' +
					'<span class="tetra-key">Bursts/s</span>' +
					'<span class="tetra-val" id="tetra-burst-rate">---</span>' +
				'</div>' +

				/* STATUS dentro de SIGNAL */
				'<div id="tetra-status-block" style="display:none">' +
					'<div class="tetra-signal-separator"></div>' +
					'<div class="tetra-signal-subtitle">Status</div>' +
					'<div class="tetra-status-line" id="tetra-last-status">---</div>' +
				'</div>' +

				/* SDS dentro de SIGNAL */
				'<div id="tetra-sds-block" style="display:none">' +
					'<div class="tetra-signal-separator"></div>' +
					'<div class="tetra-signal-subtitle">SDS</div>' +
					'<div class="tetra-sds-line" id="tetra-last-sds">---</div>' +
				'</div>' +

			'</div>' +

			/* ── Coluna 3: TIMESLOTS ───────────────────────────── */
			'<div class="tetra-section tetra-col-timeslots">' +
				'<div class="tetra-section-title">Timeslots</div>' +
				slots +
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
			Plugins.tetra._setEnc(data.encrypted, data.encryption_type);

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

		} else if (type === 'call_setup' || type === 'call_connect' || type === 'tx_grant') {
			var si2 = (data.slot || 0);
			if (si2 >= 0 && si2 < this.slots.length) {
				this.slots[si2].update(data);
			}

		} else if (type === 'call_release') {
			for (var i = 0; i < this.slots.length; i++) {
				this.slots[i].clear();
			}

		} else if (type === 'status') {
			var ssiFrom = data.ssi || '?';
			var ssiTo   = data.ssi2 || '?';
			var stxt = ssiFrom + ' \u2192 ' + ssiTo + '  Status ' + (data.status || '?');
			$('#tetra-last-status').text(stxt);
			$('#tetra-status-block').show();

		} else if (type === 'sds') {
			var from = data.ssi || data.from || '?';
			var to   = data.ssi2 || data.to || '?';
			var sdstxt = from + ' \u2192 ' + to + ': ' + (data.text || '');
			$('#tetra-last-sds').text(sdstxt);
			$('#tetra-sds-block').show();
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
		$('#tetra-last-status').text('---');
		$('#tetra-status-block').hide();
		$('#tetra-last-sds').text('---');
		$('#tetra-sds-block').hide();
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

/**
 * Atualiza o badge de criptografia.
 *
 * @param {boolean} encrypted  - se a rede usa criptografia
 * @param {string}  encType    - valor do campo encryption_type do backend:
 *                               "TEA1", "TEA2", "TEA3", "none",
 *                               ou enc_mode hex do ENCINFO1 (ex: "05")
 */
Plugins.tetra._setEnc = function (encrypted, encType) {
	var $el = $('#tetra-enc');
	$el.removeClass('enc-yes enc-no enc-tea');

	if (!encrypted) {
		$el.text('No').addClass('enc-no');
		return;
	}

	/* Se o backend envia encryption_type como "TEA1", "TEA2", "TEA3" */
	if (encType && encType !== 'none' && encType !== 'None' && encType !== '00') {
		/* Normaliza: garante formato "TEAn" */
		var label = encType.toUpperCase();
		if (label.indexOf('TEA') === 0) {
			$el.text(label).addClass('enc-tea');
		} else {
			/* enc_mode hex do ENCINFO1 (ex: "05") — mostra raw */
			$el.text('ENC 0x' + label).addClass('enc-tea');
		}
	} else {
		$el.text('YES').addClass('enc-yes');
	}
};
