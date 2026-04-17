#!/usr/bin/env python3
"""
Patches OpenWebRX+ Python files at Docker build time to register TETRA.
Runs once during image build; does not modify anything at runtime.
Exits with code 1 on failure so the Docker build fails loudly.
"""
import re
import sys
import os

TETRA_RX = "/opt/openwebrx-tetra/tetra-rx"

# ── Locate OpenWebRX+ package directory ──────────────────────────────────────

try:
    import owrx as _owrx
    OWRX_DIR = os.path.dirname(_owrx.__file__)
except ImportError:
    sys.exit("ERROR: Cannot import owrx — is OpenWebRX+ installed?")

print(f"Found owrx at: {OWRX_DIR}")

# ── 1. modes.py — register TETRA as an AnalogMode ────────────────────────────

MODES_PATH = os.path.join(OWRX_DIR, "modes.py")

def _patch_modes(path):
    with open(path, "r") as f:
        content = f.read()

    if '"tetra", "TETRA"' in content:
        print("  SKIP modes.py: TETRA already present")
        return True

    # Primary: insert BEFORE nxdn, capturing indentation
    new_content = re.sub(
        r'^([ \t]+)(AnalogMode\("nxdn".*)',
        r'\1AnalogMode("tetra", "TETRA", bandpass=Bandpass(-12500, 12500), requirements=["tetra_decoder"], squelch=False),\n\1\2',
        content,
        count=1,
        flags=re.MULTILINE,
    )
    if new_content != content:
        with open(path, "w") as f:
            f.write(new_content)
        print("  OK   modes.py: inserted TETRA before nxdn entry")
        return True

    # Fallback: append import-time registration at end of file
    fallback = '''

# TETRA mode — added by openwebrx-tetra-plugin (nxdn anchor not found)
try:
    from owrx.modes import AnalogMode, Bandpass, Modes as _Modes
    _tetra = AnalogMode("tetra", "TETRA",
                        bandpass=Bandpass(-12500, 12500),
                        requirements=["tetra_decoder"],
                        squelch=False)
    if not any(getattr(m, "modulation", None) == "tetra" for m in _Modes.getModes()):
        for _attr in ("_modes", "_Modes__modes", "__modes"):
            if hasattr(_Modes, _attr):
                getattr(_Modes, _attr).append(_tetra)
                break
except Exception as _e:
    import logging; logging.getLogger("tetra_patch").warning("modes patch: %s", _e)
'''
    with open(path, "a") as f:
        f.write(fallback)
    print("  WARN modes.py: nxdn anchor not found — appended import-time registration as fallback")
    return True

if os.path.exists(MODES_PATH):
    _patch_modes(MODES_PATH)
else:
    sys.exit(f"ERROR: {MODES_PATH} not found")

# ── 2. feature.py — declare tetra_decoder feature ────────────────────────────

FEATURE_PATH = os.path.join(OWRX_DIR, "feature.py")
TETRA_FEATURE_BLOCK = f'''

# TETRA feature — added by openwebrx-tetra-plugin
try:
    from owrx.feature import FeatureDetector as _FD
    import os as _os
    if "tetra_decoder" not in _FD.features:
        _FD.features["tetra_decoder"] = ["tetra_demod"]
    if not hasattr(_FD, "has_tetra_demod"):
        def _has_tetra_demod(self):
            return _os.path.isfile("{TETRA_RX}") and _os.access("{TETRA_RX}", _os.X_OK)
        _FD.has_tetra_demod = _has_tetra_demod
except Exception as _e:
    import logging; logging.getLogger("tetra_patch").warning("feature patch: %s", _e)
'''

if os.path.exists(FEATURE_PATH):
    with open(FEATURE_PATH, "r") as f:
        feat_content = f.read()
    if "tetra_decoder" not in feat_content:
        with open(FEATURE_PATH, "a") as f:
            f.write(TETRA_FEATURE_BLOCK)
        print("  OK   feature.py: appended tetra_decoder feature")
    else:
        print("  SKIP feature.py: tetra_decoder already present")
else:
    sys.exit(f"ERROR: {FEATURE_PATH} not found")

# ── 3. dsp.py — route "tetra" to Tetra() demodulator chain ──────────────────

DSP_PATH = os.path.join(OWRX_DIR, "dsp.py")

def _patch_dsp(path):
    with open(path, "r") as f:
        content = f.read()

    if 'elif demod == "tetra":' in content:
        print("  SKIP dsp.py: tetra already present")
        return True

    # Primary: insert BEFORE nxdn, capturing indentation so it works regardless
    # of whether the file uses 4 or 8 spaces (or tabs).
    new_content, n = re.subn(
        r'^([ \t]+)(elif demod == "nxdn":)',
        r'\1elif demod == "tetra":\n\1    from csdr.chain.tetra import Tetra\n\1    return Tetra()\n\1\2',
        content,
        count=1,
        flags=re.MULTILINE,
    )
    if n:
        with open(path, "w") as f:
            f.write(new_content)
        print("  OK   dsp.py: inserted TETRA elif before nxdn block")
        return True

    # Fallback: try inserting before dmr, ysf, or m17 if nxdn is absent
    for anchor in (r'"dmr"', r'"ysf"', r'"m17"'):
        new_content, n = re.subn(
            rf'^([ \t]+)(elif demod == {anchor}:)',
            r'\1elif demod == "tetra":\n\1    from csdr.chain.tetra import Tetra\n\1    return Tetra()\n\1\2',
            content,
            count=1,
            flags=re.MULTILINE,
        )
        if n:
            with open(path, "w") as f:
                f.write(new_content)
            print(f"  OK   dsp.py: inserted TETRA elif before {anchor} block (nxdn not found)")
            return True

    # Last resort: module-level monkey-patch appended to end of file.
    # Works even if _getDemodulator is eventually refactored away.
    fallback = '''

# TETRA demodulator routing — added by openwebrx-tetra-plugin
# Fallback: no elif anchor found in _getDemodulator (possible refactor).
try:
    from owrx.dsp import DspManager as _DM
    _orig_get = _DM._getDemodulator
    def _tetra_get(self, demod, *a, **kw):
        if demod == "tetra":
            from csdr.chain.tetra import Tetra
            return Tetra()
        return _orig_get(self, demod, *a, **kw)
    _DM._getDemodulator = _tetra_get
except Exception as _e:
    import logging; logging.getLogger("tetra_patch").warning("dsp patch: %s", _e)
'''
    with open(path, "a") as f:
        f.write(fallback)
    print("  WARN dsp.py: no elif anchor found — used monkey-patch fallback at module level")
    return True

if os.path.exists(DSP_PATH):
    _patch_dsp(DSP_PATH)
else:
    sys.exit(f"ERROR: {DSP_PATH} not found")

print("\nTETRA patch completed successfully.")
