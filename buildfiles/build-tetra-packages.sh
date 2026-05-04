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

# ── 1. Clone sq5bpf-2 for tetra-rx (it exposes tms->ssi/tsn/usage_marker) ────
pinfo "Cloning osmo-tetra-sq5bpf-2 (tetra-rx with per-call SSI/UM access)..."
git clone --depth 1 https://github.com/sq5bpf/osmo-tetra-sq5bpf-2 /tmp/osmo-tetra-2

# ── 1b. Bug 15 patch — emit TN, SSI, TSN, UM in the voice-frame header ────────
#
# Calibration of plain SSI demux revealed 96% of voice frames carry
# SSI=0xFFFFFF (ALL-CALL broadcast destination, per ETSI EN 300 392-2),
# making caller demux impossible from SSI alone. usage_marker is per-call
# at the MAC layer — different concurrent calls on the same channel get
# different UM values, so it can discriminate where SSI cannot.
#
# Wire format: [64-byte ASCII header NUL-padded] + [1380-byte ACELP block]
#   header text: "TRA:HH RX:HH DECR:N TN:N SSI:N TSN:N UM:N\0"
# UDP packet: 1444 bytes (unchanged from previous Bug 15; UM fits in spare
# header room).
pinfo "Patching tetra_lower_mac.c for per-call voice demux (Bug 15 + UM)..."
LMAC=/tmp/osmo-tetra-2/src/lower_mac/tetra_lower_mac.c

# Buffer size: 1380+20 -> 1380+64
sed -i 's|unsigned char tmpstr\[1380+20\];|unsigned char tmpstr[1380+64];|' "$LMAC"

# sprintf + memcpy + sendto rewrite (perl handles the embedded \0).
perl -i -pe '
  s|sprintf\(tmpstr,"TRA:%2\.2x RX:%2\.2x DECR:%i\\0",tms->cur_burst\.is_traffic,tetra_hack_rxid,decrypted\);|memset(tmpstr,0,64);snprintf((char *)tmpstr,64,"TRA:%2.2x RX:%2.2x DECR:%i TN:%u SSI:%u TSN:%u UM:%i",tms->cur_burst.is_traffic,tetra_hack_rxid,decrypted,tcd->time.tn,tms->ssi,tms->tsn,tms->usage_marker);|;
  s|memcpy\(tmpstr\+20,block,sizeof\(block\)\);|memcpy(tmpstr+64,block,sizeof(block));|;
  s|sizeof\(block\)\+20|sizeof(block)+64|g;
' "$LMAC"

# Hard-fail if any of the rewrites silently missed.
if ! grep -q 'tmpstr\[1380+64\]' "$LMAC"; then
    perror "Bug 15 patch failed: buffer-size rewrite did not apply"
    exit 1
fi
if ! grep -q 'UM:%i' "$LMAC"; then
    perror "Bug 15 patch failed: UM (usage_marker) field not added to sprintf"
    exit 1
fi
if ! grep -q 'tms->usage_marker' "$LMAC"; then
    perror "Bug 15 patch failed: usage_marker reference missing"
    exit 1
fi
if grep -q 'tmpstr+20,block' "$LMAC"; then
    perror "Bug 15 patch failed: legacy memcpy offset still present"
    exit 1
fi
pinfo "Voice demux patch applied; voice header is now 64 bytes with TN/SSI/TSN/UM."

# ── 2. Clone sq5bpf (legacy) just for its codec.diff ─────────────────────────
#
# osmo-tetra-sq5bpf-2 ships the install-tetra-codec patch which interprets
# every 5th input frame as a TEA keystream block (Bug 4). Without keys this
# corrupts every 5th frame. Use the legacy sq5bpf codec.diff instead.
pinfo "Cloning osmo-tetra-sq5bpf (legacy) for codec.diff..."
git clone --depth 1 https://github.com/sq5bpf/osmo-tetra-sq5bpf /tmp/osmo-tetra-codec-src

# ── 3. ETSI ACELP codec — local copy or download, then patch with legacy diff
pinfo "Building ETSI ACELP codec..."
cd /tmp/osmo-tetra-codec-src/etsi_codec-patches

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
        exit 1
    }
fi

unzip -q -L en_30039502v010301p0.zip
patch -p1 -N -E < codec.diff
cd c-code && make ${MAKEFLAGS}
cp cdecoder sdecoder "$TETRA_INSTALL_DIR/"

# ── 4. Build tetra-rx from sq5bpf-2 (with our Bug 15+UM patch) ──────────────
pinfo "Building tetra-rx from sq5bpf-2..."
cd /tmp/osmo-tetra-2/src
make ${MAKEFLAGS} tetra-rx
cp tetra-rx "$TETRA_INSTALL_DIR/"

# ── 5. Export artifacts for the runtime stage ────────────────────────────────
pinfo "Exporting build artifacts..."
cp -r "$TETRA_INSTALL_DIR"/* /build_artifacts/tetra/
pinfo "TETRA packages built successfully."
ls -la /build_artifacts/tetra/
