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
apt-get install -y --no-install-recommends \
    libosmocore python3-gnuradio \
    || apt-get install -y --no-install-recommends libosmocore
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
cp "$TETRA_FILES/csdr_module_tetra.py" "$OWRX_PYTHON/csdr/module/tetra.py"
cp "$TETRA_FILES/csdr_chain_tetra.py"  "$OWRX_PYTHON/csdr/chain/tetra.py"

# ── 3. Patch OpenWebRX+ Python files ─────────────────────────────────────────
pinfo "Patching OpenWebRX+ DSP engine..."
python3 "$TETRA_FILES/patch_tetra.py" || {
    perror "patch_tetra.py failed — Docker build aborted."
    exit 1
}

# ── Cleanup ───────────────────────────────────────────────────────────────────
rm -rf "$TETRA_FILES"
pinfo "TETRA installation complete."
