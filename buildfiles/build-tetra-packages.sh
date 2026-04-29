#!/bin/bash
set -euxo pipefail
source /common.sh

TETRA_INSTALL_DIR="/opt/openwebrx-tetra"
mkdir -p /build_artifacts/tetra "$TETRA_INSTALL_DIR"

pinfo "Installing build dependencies..."
apt_update_with_fallback 120
apt-get install -y --no-install-recommends \
    ca-certificates build-essential pkg-config git wget unzip \
    libosmocore-dev python3-dev

# ── 1. Clone osmo-tetra-sq5bpf ───────────────────────────────────────────────
pinfo "Cloning osmo-tetra-sq5bpf..."
git clone --depth 1 https://github.com/sq5bpf/osmo-tetra-sq5bpf /tmp/osmo-tetra

# ── 2. ETSI ACELP codec — local copy, patch, compile ─────────────────────────
pinfo "Building ETSI ACELP codec..."
cd /tmp/osmo-tetra/etsi_codec-patches

if [ -f /tmp/en_30039502v010301p0.zip ]; then
    pinfo "Using local copy of ETSI ACELP codec."
    cp /tmp/en_30039502v010301p0.zip en_30039502v010301p0.zip
else
    pinfo "Downloading ETSI ACELP codec..."
    wget -qO en_30039502v010301p0.zip \
        --header="User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36" \
        "https://www.etsi.org/deliver/etsi_en/300300_300399/30039502/01.03.01_60/en_30039502v010301p0.zip" || {
        perror "Failed to download ETSI ACELP codec."
        perror "URL: https://www.etsi.org/deliver/etsi_en/300300_300399/30039502/01.03.01_60/en_30039502v010301p0.zip"
        perror "If the URL changed, update build-tetra-packages.sh."
        exit 1
    }
fi

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
