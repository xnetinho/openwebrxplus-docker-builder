#!/bin/bash
set -euxo pipefail
source /common.sh

TETRA_INSTALL_DIR="/opt/openwebrx-tetra"
mkdir -p /build_artifacts/tetra "$TETRA_INSTALL_DIR"

pinfo "Installing build dependencies..."
apt_update_with_fallback 120
apt-get install -y --no-install-recommends \
    ca-certificates build-essential pkg-config git wget unzip \
    gnuradio libosmocore-dev python3-dev

# ── 1. Clone osmo-tetra-sq5bpf ───────────────────────────────────────────────
pinfo "Cloning osmo-tetra-sq5bpf..."
git clone --depth 1 https://github.com/sq5bpf/osmo-tetra-sq5bpf /tmp/osmo-tetra

# ── 2. ETSI ACELP codec — download, patch, compile ───────────────────────────
pinfo "Building ETSI ACELP codec..."
cd /tmp/osmo-tetra/etsi_codec-patches

wget -qO en_30039502v010301p0.zip \
    "http://www.etsi.org/deliver/etsi_en/300300_300399/30039502/01.03.01_60/en_30039502v010301p0.zip" || {
    perror "Failed to download ETSI ACELP codec."
    perror "URL: http://www.etsi.org/deliver/etsi_en/300300_300399/30039502/01.03.01_60/en_30039502v010301p0.zip"
    perror "If the URL changed, update build-tetra-packages.sh."
    exit 1
}

# -L forces lowercase filenames (required for the patch to apply on Linux)
unzip -q -L en_30039502v010301p0.zip
patch -p1 -N -E < codec.diff
cd c-code && make ${MAKEFLAGS}
cp cdecoder sdecoder "$TETRA_INSTALL_DIR/"

# ── 3. Build tetra-rx ─────────────────────────────────────────────────────────
pinfo "Building tetra-rx..."
cd /tmp/osmo-tetra/src
make ${MAKEFLAGS} tetra-rx
cp tetra-rx "$TETRA_INSTALL_DIR/"

# ── 4. Export artifacts for the runtime stage ─────────────────────────────────
pinfo "Exporting build artifacts..."
cp -r "$TETRA_INSTALL_DIR"/* /build_artifacts/tetra/
pinfo "TETRA packages built successfully."
ls -la /build_artifacts/tetra/
