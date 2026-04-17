/*
 * tetra Receiver Plugin for OpenWebRX+
 * Displays TETRA signaling metadata in a panel styled like DMR.
 *
 * Load via init.js:
 *   await Plugins.load('https://xnetinho.github.io/openwebrxplus-docker-builder/plugins/receiver/tetra/tetra.js');
 *
 * License: MIT
 * Repository: https://github.com/xnetinho/openwebrxplus-docker-builder
 */

Plugins.tetra = Plugins.tetra || {};
Plugins.tetra._version = 1.0;
Plugins.tetra.no_css = false;

Plugins.tetra.init = function () {

	if (!Plugins.isLoaded('utils', 0.1)) {
		console.error('[tetra] plugin requires "utils >= 0.1".');
		return false;
	}

	// Wait for OWRX to be fully initialised before touching the DOM
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
			'<div class="openwebrx-meta-slot openwebrx-tetra-slot" id="tetra-slot-' + i + '">' +
				'<div class="tetra-slot-number">Slot ' + i + '</div>' +
				'<div class="openwebrx-meta-user-image">' +
					'<img class="directcall" src="static/gfx/openwebrx-directcall.svg">' +
					'<img class="groupcall"  src="static/gfx/openwebrx-groupcall.svg">' +
				'</div>' +
				'<div class="tetra-slot-info">' +
					'<span class="tetra-issi" id="tetra-s' + i + '-issi">---</span>' +
					'<span class="tetra-gssi" id="tetra-s' + i + '-gssi">---</span>' +
					'<span class="tetra-calltype" id="tetra-s' + i + '-type"></span>' +
				'</div>' +
			'</div>';
	}

	var html =
		'<div class="openwebrx-panel openwebrx-meta-panel" ' +
			'id="openwebrx-panel-metadata-tetra" ' +
			'style="display:none" ' +
			'data-panel-name="metadata-tetra">' +
			'<div class="tetra-network">' +
				'<span class="tetra-label">MCC/MNC</span>' +
				'<span class="tetra-value" id="tetra-mcc-mnc">---</span>' +
				'<span class="tetra-label">DL</span>' +
				'<span class="tetra-value" id="tetra-dl">---</span>' +
				'<span class="tetra-label">UL</span>' +
				'<span class="tetra-value" id="tetra-ul">---</span>' +
				'<span class="tetra-label">CC</span>' +
				'<span class="tetra-value" id="tetra-cc">---</span>' +
				'<span class="tetra-label">Enc</span>' +
				'<span class="tetra-enc" id="tetra-enc">---</span>' +
			'</div>' +
			'<div class="tetra-signal">' +
				'<span class="tetra-label">AFC</span>' +
				'<span class="tetra-value" id="tetra-afc">---</span>' +
				'<span class="tetra-label">Bursts/s</span>' +
				'<span class="tetra-value" id="tetra-burst-rate">---</span>' +
			'</div>' +
			slots +
		'</div>';

	// Insert after DMR panel to keep meta panels grouped together
	var dmr = document.getElementById('openwebrx-panel-metadata-dmr');
	if (dmr && dmr.parentNode) {
		dmr.insertAdjacentHTML('afterend', html);
	} else {
		// Fallback: append alongside the first existing meta panel
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
		this._data = null;
	}

	TetraMetaSlot.prototype.update = function (data) {
		var callType = (data.call_type || 'group').toLowerCase();

		this.el.addClass('active');
		this.el.removeClass('groupcall directcall emergency');
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

		this._setText('issi', data.issi || '---');
		this._setText('gssi', data.gssi ? '\u2192 ' + data.gssi : '---');
		this._setText('type', callType);
		this._data = data;
	};

	TetraMetaSlot.prototype.clear = function () {
		this.el.removeClass('active groupcall directcall emergency encrypted');
		var slotNum = this.el.attr('id').replace('tetra-slot-', '');
		$('#tetra-s' + slotNum + '-issi').text('---');
		$('#tetra-s' + slotNum + '-gssi').text('---');
		$('#tetra-s' + slotNum + '-type').text('');
		this._data = null;
	};

	TetraMetaSlot.prototype._setText = function (field, value) {
		var slotNum = this.el.attr('id').replace('tetra-slot-', '');
		$('#tetra-s' + slotNum + '-' + field).text(value || '---');
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
			$('#tetra-mcc-mnc').text((data.mcc || '?') + '/' + (data.mnc || '?'));
			$('#tetra-dl').text(Plugins.tetra._formatFreq(data.dl_freq));
			$('#tetra-ul').text(Plugins.tetra._formatFreq(data.ul_freq));
			$('#tetra-cc').text(data.color_code || '---');
			Plugins.tetra._setEnc(data.encrypted);

		} else if (type === 'burst') {
			$('#tetra-afc').text(data.afc || '---');
			$('#tetra-burst-rate').text(data.burst_rate || '---');
			var slotNum = (data.slot || 0);
			if (slotNum >= 0 && slotNum < this.slots.length) {
				$('#tetra-slot-' + (slotNum + 1)).addClass('active');
			}

		} else if (type === 'call_setup' || type === 'connect' || type === 'tx_grant') {
			var slotIdx = (data.slot || 0);
			if (slotIdx >= 0 && slotIdx < this.slots.length) {
				this.slots[slotIdx].update(data);
			}

		} else if (type === 'call_release') {
			for (var i = 0; i < this.slots.length; i++) {
				this.slots[i].clear();
			}
		}
	};

	TetraMetaPanel.prototype.clear = function () {
		MetaPanel.prototype.clear.call(this);
		$('#tetra-mcc-mnc').text('---');
		$('#tetra-dl').text('---');
		$('#tetra-ul').text('---');
		$('#tetra-cc').text('---');
		$('#tetra-enc').text('---').removeClass('enc-yes enc-no');
		$('#tetra-afc').text('---');
		$('#tetra-burst-rate').text('---');
		for (var i = 0; i < this.slots.length; i++) {
			this.slots[i].clear();
		}
	};

	// ── Register and initialise ────────────────────────────────────────────────

	MetaPanel.types['tetra'] = TetraMetaPanel;

	var $panel = $('#openwebrx-panel-metadata-tetra');
	if ($panel.length && !$panel.data('metapanel')) {
		$panel.metaPanel();
	}
};

// ── Helpers ────────────────────────────────────────────────────────────────────

Plugins.tetra._formatFreq = function (hz) {
	if (!hz) return '---';
	return (parseFloat(hz) / 1e6).toFixed(4) + ' MHz';
};

Plugins.tetra._setEnc = function (encrypted) {
	var $el = $('#tetra-enc');
	if (encrypted) {
		$el.text('YES').removeClass('enc-no').addClass('enc-yes');
	} else {
		$el.text('NO').removeClass('enc-yes').addClass('enc-no');
	}
};
