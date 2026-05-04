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

# ── 1. Clone sq5bpf-2 for tetra-rx (it exposes tms->ssi/tsn we need) ─────────
pinfo "Cloning osmo-tetra-sq5bpf-2 (tetra-rx with per-call SSI access)..."
git clone --depth 1 https://github.com/sq5bpf/osmo-tetra-sq5bpf-2 /tmp/osmo-tetra-2

# ── 1b. Bug 15 patch — emit TN, SSI and TSN in the voice-frame header ────────
#
# Upstream sq5bpf-2 emits voice frames tagged only with TRA/RX/DECR — no
# call identifier, so we cannot tell which SSI a burst belongs to. In a
# busy talkgroup multiple SSIs share the same TS via PTT handoff,
# producing the "scrambled but partially intelligible" symptom (Bug 14
# residual after TN-lock).
#
# This patch:
#   - expands the voice-frame header buffer from 20 to 64 bytes
#   - adds TN (timeslot 1..4), SSI (caller address), and TSN (slot number)
#   - keeps the binary block layout intact (now at offset 64)
#
# Wire format: [64-byte ASCII header NUL-padded] + [1380-byte ACELP block]
#   header text: "TRA:HH RX:HH DECR:N TN:N SSI:N TSN:N\0"
# UDP packet: 1444 bytes (was 1400).
pinfo "Patching tetra_lower_mac.c for per-SSI voice demux (Bug 15)..."
LMAC=/tmp/osmo-tetra-2/src/lower_mac/tetra_lower_mac.c

# Buffer size: 1380+20 -> 1380+64
sed -i 's|unsigned char tmpstr\[1380+20\];|unsigned char tmpstr[1380+64];|' "$LMAC"

# sprintf + memcpy + sendto rewrite (perl handles the embedded \0).
perl -i -pe '
  s|sprintf\(tmpstr,"TRA:%2\.2x RX:%2\.2x DECR:%i\\0",tms->cur_burst\.is_traffic,tetra_hack_rxid,decrypted\);|memset(tmpstr,0,64);snprintf((char *)tmpstr,64,"TRA:%2.2x RX:%2.2x DECR:%i TN:%u SSI:%u TSN:%u",tms->cur_burst.is_traffic,tetra_hack_rxid,decrypted,tcd->time.tn,tms->ssi,tms->tsn);|;
  s|memcpy\(tmpstr\+20,block,sizeof\(block\)\);|memcpy(tmpstr+64,block,sizeof(block));|;
  s|sizeof\(block\)\+20|sizeof(block)+64|g;
' "$LMAC"

# Hard-fail if any of the three rewrites silently missed.
if ! grep -q 'tmpstr\[1380+64\]' "$LMAC"; then
    perror "Bug 15 patch failed: buffer-size rewrite did not apply"
    exit 1
fi
if ! grep -q 'SSI:%u TSN:%u' "$LMAC"; then
    perror "Bug 15 patch failed: SSI/TSN fields not added to sprintf"
    exit 1
fi
if grep -q 'tmpstr+20,block' "$LMAC"; then
    perror "Bug 15 patch failed: legacy memcpy offset still present"
    exit 1
fi
pinfo "Voice demux patch applied; voice header is now 64 bytes with TN/SSI/TSN."

# ── 2. Clone sq5bpf (legacy) just for its codec.diff ─────────────────────────
#
# osmo-tetra-sq5bpf-2 ships a codec.diff that interprets every 5th input
# frame as a TEA keystream block (the "install-tetra-codec" patch). We
# don't load TEA keys, so feeding pure ACELP into that patched codec
# corrupts every 5th frame and produces 300ms intelligible / 60ms
# garbled in a loop (Bug 4). The legacy sq5bpf repo's codec.diff is
# the older one without keystream interpretation — we use that.
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

# ── 4. Build tetra-rx from sq5bpf-2 (with our Bug 15 patch) ──────────────────
pinfo "Building tetra-rx from sq5bpf-2..."
cd /tmp/osmo-tetra-2/src
make ${MAKEFLAGS} tetra-rx
cp tetra-rx "$TETRA_INSTALL_DIR/"

# ── 5. Export artifacts for the runtime stage ────────────────────────────────
pinfo "Exporting build artifacts..."
cp -r "$TETRA_INSTALL_DIR"/* /build_artifacts/tetra/
pinfo "TETRA packages built successfully."
ls -la /build_artifacts/tetra/
