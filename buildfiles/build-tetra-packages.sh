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

# ── 1. Clone osmo-tetra-sq5bpf ───────────────────────────────────────────────────
pinfo "Cloning osmo-tetra-sq5bpf..."
git clone --depth 1 https://github.com/sq5bpf/osmo-tetra-sq5bpf /tmp/osmo-tetra

# ── 1b. Bug 14 patch — include TN in voice-frame header for per-TS demux ─────
#
# Upstream emits one global voice stream from all timeslots into the same
# UDP socket with no demux key in the header. The Python wrapper has no
# way to separate concurrent calls, so consecutive bursts from different
# TS get fed sequentially to a single codec instance, producing the
# "scrambled but partially intelligible" symptom Bug 11/14 documented.
#
# This patch:
#   - expands the voice-frame header buffer from 13 to 32 bytes
#   - adds `TN:N` (timeslot 0..3) to the sprintf
#   - keeps the binary block layout intact (now at offset 32 instead of 13)
#
# Resulting wire format:
#   [32-byte ASCII header, NUL-padded] + [1380-byte ACELP soft-bit block]
#   header text: "TRA:HH RX:HH TN:N\0" (rest of 32 bytes is NUL)
# UDP packet size: 1412 bytes (was 1393).
pinfo "Patching tetra_lower_mac.c for per-TS voice demux (Bug 14)..."
LMAC=/tmp/osmo-tetra/src/lower_mac/tetra_lower_mac.c

# Buffer size: 1380+13 -> 1380+32
sed -i 's|unsigned char tmpstr\[1380+13\];|unsigned char tmpstr[1380+32];|' "$LMAC"

# sprintf + memcpy + sendto rewrite (use perl for embedded \0 handling).
perl -i -pe '
  s|sprintf\(tmpstr,"TRA:%2\.2x RX:%2\.2x\\0",tms->cur_burst\.is_traffic,tetra_hack_rxid\);|memset(tmpstr,0,32);snprintf((char *)tmpstr,32,"TRA:%2.2x RX:%2.2x TN:%i",tms->cur_burst.is_traffic,tetra_hack_rxid,tcd->time.tn);|;
  s|memcpy\(tmpstr\+13,block,sizeof\(block\)\);|memcpy(tmpstr+32,block,sizeof(block));|;
  s|sizeof\(block\)\+13|sizeof(block)+32|g;
' "$LMAC"

# Hard-fail if the patch silently missed (e.g. upstream changed those lines).
if ! grep -q 'tmpstr\[1380+32\]' "$LMAC"; then
    perror "Bug 14 patch failed: buffer-size rewrite did not apply"
    exit 1
fi
if ! grep -q 'TN:%i' "$LMAC"; then
    perror "Bug 14 patch failed: TN field not added to sprintf"
    exit 1
fi
if grep -q 'tmpstr+13,block' "$LMAC"; then
    perror "Bug 14 patch failed: legacy memcpy offset still present"
    exit 1
fi
pinfo "Voice demux patch applied; voice header is now 32 bytes with TN field."

# ── 2. ETSI ACELP codec — use the bundled patch from osmo-tetra-sq5bpf ────────
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

# Use the codec.diff bundled with osmo-tetra-sq5bpf (no keystream handling).
#
# NOTE: an earlier revision of this script downloaded the newer codec.diff
# from sq5bpf/install-tetra-codec, which adds keystream-handling support.
# That patch interprets every 5th input frame as a keystream block and XORs
# it into the decoded audio. Since we feed pure ACELP frames from tetra-rx
# (no separate keystream stream), the patched codec corrupts every 5th
# frame and produces intermittently intelligible / scrambled output, like
# 300ms audible + 60ms garbled in a loop. The bundled patch decodes plain
# ACELP without keystream interpretation, which is what we want when no
# SCK is loaded.
#
# If you ever DO have an SCK keyfile and want keystream support, swap in
# the install-tetra-codec patch AND modify tetra_decoder.py to interleave
# a keystream frame every 4 ACELP frames in the codec input stream.

# -L forces lowercase filenames (required for the patch to apply on Linux)
unzip -q -L en_30039502v010301p0.zip
patch -p1 -N -E < codec.diff
cd c-code && make ${MAKEFLAGS}
cp cdecoder sdecoder "$TETRA_INSTALL_DIR/"

# ── 3. Build tetra-rx ──────────────────────────────────────────────────────────────────
pinfo "Building tetra-rx..."
cd /tmp/osmo-tetra/src
make ${MAKEFLAGS} tetra-rx
cp tetra-rx "$TETRA_INSTALL_DIR/"

# ── 4. Export artifacts for the runtime stage ───────────────────────────────────
pinfo "Exporting build artifacts..."
cp -r "$TETRA_INSTALL_DIR"/* /build_artifacts/tetra/
pinfo "TETRA packages built successfully."
ls -la /build_artifacts/tetra/
