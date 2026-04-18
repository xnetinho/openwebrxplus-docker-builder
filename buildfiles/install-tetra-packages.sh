#!/bin/bash
set -euxo pipefail

OWRX_PYTHON="/usr/lib/python3/dist-packages"
TETRA_OPT="/opt/openwebrx-tetra"
TETRA_FILES="/tmp/tetra_files"

pinfo() { printf "\e[38;5;15;48;5;12m[+] %-85s\e[0m\n" "$*"; }
perror() { printf "\e[38;5;15;48;5;1m[+] %-85s\e[0m\n" "$*"; }

# ── Runtime dependencies ──────────────────────────────────────────────────────
pinfo "Installing TETRA runtime dependencies..."
apt-get update -qq

# libosmocore is required; fail hard if absent
apt-get install -y --no-install-recommends libosmocore

# python3-gnuradio: install if available in apt; the base image may already
# have GNURadio installed via a custom build — don't treat absence as fatal
if apt-cache show python3-gnuradio >/dev/null 2>&1; then
    apt-get install -y --no-install-recommends python3-gnuradio \
        && pinfo "python3-gnuradio installed via apt" \
        || pinfo "WARN python3-gnuradio apt install failed (GNURadio may already be present)"
else
    pinfo "python3-gnuradio not in apt repos — checking if GNURadio already importable..."
    python3 -c "from gnuradio import gr; print('GNURadio', gr.version())" 2>/dev/null \
        && pinfo "GNURadio already available in base image" \
        || pinfo "WARN GNURadio not available — TETRA will run without pi/4-DQPSK demodulation"
fi

apt-get clean
rm -rf /var/lib/apt/lists/*

# ── 1. Binaries and scripts ───────────────────────────────────────────────────
pinfo "Installing TETRA binaries..."
mkdir -p "$TETRA_OPT"
cp "$TETRA_FILES/tetra-rx"  "$TETRA_OPT/" 2>/dev/null || true
cp "$TETRA_FILES/cdecoder"  "$TETRA_OPT/" 2>/dev/null || true
cp "$TETRA_FILES/sdecoder"  "$TETRA_OPT/" 2>/dev/null || true
cp "$TETRA_FILES/"*.py      "$TETRA_OPT/" 2>/dev/null || true
chmod +x "$TETRA_OPT/"*    2>/dev/null || true

# ── 2. CSDR Python modules ────────────────────────────────────────────────────
pinfo "Installing TETRA CSDR modules..."

# Locate csdr module directory — OpenWebRX+ versions differ (module vs modules)
CSDR_MODULE_DIR=""
for candidate in \
    "$OWRX_PYTHON/csdr/module" \
    "$OWRX_PYTHON/csdr/modules" \
    "$(python3 -c 'import csdr.module, os; print(os.path.dirname(csdr.module.__file__))' 2>/dev/null || true)"
do
    if [ -d "$candidate" ]; then
        CSDR_MODULE_DIR="$candidate"
        break
    fi
done

if [ -z "$CSDR_MODULE_DIR" ]; then
    perror "Cannot locate csdr module directory under $OWRX_PYTHON"
    python3 -c "import csdr; import os; print(os.path.dirname(csdr.__file__))" || true
    exit 1
fi

CSDR_CHAIN_DIR="$OWRX_PYTHON/csdr/chain"
mkdir -p "$CSDR_MODULE_DIR" "$CSDR_CHAIN_DIR"

pinfo "Using csdr module dir: $CSDR_MODULE_DIR"
cp "$TETRA_FILES/csdr_module_tetra.py" "$CSDR_MODULE_DIR/tetra.py"
cp "$TETRA_FILES/csdr_chain_tetra.py"  "$CSDR_CHAIN_DIR/tetra.py"

# ── 3. Patch OpenWebRX+ Python files ─────────────────────────────────────────
pinfo "Patching OpenWebRX+ DSP engine..."
python3 "$TETRA_FILES/patch_tetra.py" || {
    perror "patch_tetra.py failed — Docker build aborted."
    exit 1
}

# ── Cleanup ───────────────────────────────────────────────────────────────────
rm -rf "$TETRA_FILES"
pinfo "TETRA installation complete."
