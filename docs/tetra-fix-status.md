# TETRA Frequency Monitoring Branch — Status & Roadmap

Branch: `claude/tetra-frequency-monitoring-3IqM9`
Started: 2026-04-29

Purpose: fix the TETRA decoder pipeline so the OpenWebRX+ panel matches
reality and unencrypted voice traffic plays back as audio.

---

## Original problem (user report)

On frequency 390.050 MHz, SDRSharp + TETRA plugin clearly demodulates
voice (audible, intelligible) — proving the call is unencrypted. Our
OpenWebRX+ build with the TETRA plugin shows:

- Encryption badge: "TEA2" (red, alarming)
- No audio output at all (silence)

## Root-cause analysis

Three distinct bugs were identified and worked on:

### Bug 1 — Cell capability vs. active-call encryption (FIXED)

The `CRYPT:` field in TETMON `NETINFO1` carries the cell-level security
class advertisement (TEA capability of the BS), NOT the per-call encryption
status. The previous code interpreted CRYPT as "this call is encrypted
with TEAn", causing the badge to show TEA2 on every cell that merely
advertises the capability.

Fixed in commit `7c740a1`:
- NETINFO1/ENCINFO1 now emit `cell_security_class` + `cell_tea` fields
  (informational), and `encrypted=false` for the network-level event.
- Per-call `encrypted`/`encryption_type` derived from Basicinfo nibble
  (later refined — see Bug 4).

### Bug 2 — Reserved encryption_mode values flashing red (FIXED)

`Basicinfo` low nibble can be 0..15. Only 1/2/3 are TEA1/TEA2/TEA3 per
ETSI EN 300 392-2; 4..15 are reserved/proprietary. The first version
of `annotate_call_encryption` used `enc > 0`, so a Basicinfo with low
nibble = 5 (e.g.) produced `encrypted=true, encryption_type="none"` and
the panel briefly flashed `ENC NONE` in red.

Fixed in commit `175b1e8`:
- `meta["encrypted"] = enc in (1, 2, 3)`.

### Bug 3 — Audio header offset hardcoded (FIXED)

`AUDIO_HEADER_SIZE = 20` was a fixed offset into the TETMON UDP payload
before the binary ACELP frame. The text header `TRA:%x RX:%x DECR:%x `
varies from 18 to 22 bytes depending on hex digit count. The fixed
offset shifted the codec input by 1–3 bytes, producing silence/noise
even when the codec was healthy.

Fixed in commit `175b1e8`:
- `parse_audio_from_udp` now uses `match.end()` from the regex.
- Trailing whitespace included in the regex so end position is correct.

### Bug 4 — Codec patch missing keystream handling (FIXED)

The `codec.diff` bundled with `osmo-tetra-sq5bpf/etsi_codec-patches/` is
11 years old (~14.8 KB). The standalone `sq5bpf/install-tetra-codec`
repository ships a newer patch (~20.1 KB, last touched 2026-01-04) which
adds keystream handling: `Read_Tetra_File()` is modified to consume a
keystream array from the unused fifth frame slot and apply XOR after
decode.

Without this, `cdecoder`/`sdecoder` produce silence/noise on cells that
advertise a security class, even when the call is in clear.

Fixed in commit `ccabe18`:
- `build-tetra-packages.sh` now downloads the newer codec.diff from
  `install-tetra-codec` and applies it instead of the bundled one.

### Bug 5 — Basicinfo low-nibble != encryption_mode (SUSPECTED, NOT FIXED)

User observation 2026-04-29: panel shows `Encryption: TEA1 (active)` on
a call where SDRSharp clearly hears voice (i.e. the call is in clear).
The `Basicinfo` byte from `tetra-rx` log is being interpreted as:
  - bits 7..5 = call_type (individual / group / broadcast / acknowledged)
  - bits 3..0 = encryption_mode

This layout was inherited from the original SP8MB code without source
citation. Reviewing `osmo-tetra-sq5bpf/src/lower_mac/upper_mac.c` shows
that `Basicinfo` is printed for several MLE/MM PDUs and is NOT a
uniform "call_type:enc_mode" packed byte. Treating its low nibble as
`encryption_mode` produces false positives like the observed TEA1.

The authoritative source for per-call encryption is the `ENCR:` field
in TETMON `DSETUPDEC` / `DCONNECTDEC` / `DTXGRANTDEC` PDUs, which our
`parse_metadata_from_udp` currently ignores.

### Bug 6 — Audio still silent after rebuild (OPEN)

User confirmed Docker image was rebuilt with `ccabe18` (keystream patch)
and `175b1e8` (header offset fix). Audio stream indicator shows
10–14 kbps — too high for pure silence (which compresses to ~1–3 kbps
with Opus), too low for clear voice (~15–25 kbps). The codec is
producing PCM, but it's noise.

Hypotheses (not yet validated):
  a) `match.end()` still does not align to the start of binary ACELP —
     the regex matches text, but TETMON might insert extra delimiters,
     padding, or a length byte before the binary payload.
  b) `tetra-rx -e` is emitting frames where DECR contains the post-
     decryption-attempt bytes from the WRONG SCK. Without keystream,
     ACELP gets garbage. The new codec patch handles keystream, but only
     if `tetra-rx` actually emits keystream bytes — which requires the
     `-K` flag (or similar) we may not be setting.
  c) The frame size constant `ACELP_FRAME_SIZE = 1380` may be wrong for
     the patched codec; the new keystream-aware codec might expect a
     different per-frame layout.

---

## What was done in this branch (commits in order)

| SHA | File | Summary |
|---|---|---|
| `7c740a1` | `tetra_decoder.py` | NETINFO1/ENCINFO1 emit cell capability fields; Basicinfo nibble propagated to call events |
| `c38183f` | `plugins/receiver/tetra/{tetra.js,tetra.css}` | Mirror v1.3 frontend |
| `ccabe18` | `build-tetra-packages.sh` | Switch codec.diff source to install-tetra-codec |
| `175b1e8` | `tetra_decoder.py` | encrypted = enc in (1,2,3); match.end() for header offset |
| `69276de` | `plugins/receiver/tetra/{tetra.js,tetra.css}` | Frontend v1.4: split Cell Sec / Encryption, always-show STATUS, broadcast label |

(Plus equivalent commits on the `openwebrxplus-plugins` repo before we
consolidated all work in this repo only.)

---

## What remains (planned for next session)

### Priority 1 — Diagnose the audio noise (Bug 6)

1. Add a `TETRA_DEBUG=1` env-var path to `tetra_decoder.py` that, when
   enabled, dumps raw TETMON UDP packets (hex + ascii prefix) to stderr.
   Capture 5–10 packets from a known-clear cell.
2. Compare the actual layout against the regex assumption. Look for:
   - Length byte after the text header before binary ACELP.
   - Extra fields like `SCK:` or `KSG:` between RX and DECR.
   - The actual byte count of the binary payload (is it really 1380?).
3. If `tetra-rx` requires a keystream-emit flag, add it.
4. If `ACELP_FRAME_SIZE` is wrong, fix it.

### Priority 2 — Replace Basicinfo encryption interpretation (Bug 5)

1. Add `ENCR:` extraction to `parse_metadata_from_udp` for `DSETUPDEC`,
   `DCONNECTDEC`, `DTXGRANTDEC`.
2. Refactor `annotate_call_encryption` to prefer `ENCR:` from the PDU
   itself over the cached Basicinfo nibble.
3. If neither source is present, set `encryption_type="unknown"` and
   render the badge as gray "Unknown" — never invent a TEA value.
4. Validate the Basicinfo bit layout against osmo-tetra source. If
   confirmed wrong, remove the `_TEA1/2/3` suffix from `call_type`.

### Priority 3 — Audio gating decision

Once encryption detection is reliable, decide whether the panel should:
  - Always pipe codec output (current behavior), trusting the user to
    mute when they see TEA-active.
  - Actively gate codec output to silence when `encrypted=true` and no
    `keyfile` is loaded (saves CPU on cdecoder/sdecoder).

User preference TBD.

### Priority 4 — STATUS code dictionary

Once the user identifies the operator on 391.525 MHz / 381.525 MHz
(MCC 724 / MNC 4321), populate `Plugins.tetra.statusNames` with known
codes. Examples seen in field:
  - 528: ?
  - 4096: ?
  - 55711: ?

All observed in MMC=4321 cell. No public dictionary; needs operator
contact or empirical observation.

### Priority 5 — UI polish

- TS slot icon stays visible after first burst even if no real call
  metadata follows. Consider clearing the slot back to inactive after
  N seconds of no `call_*` events.
- Truncated `TypeACKNOWLEDGED GROUP T...` in slot should fit — tighten
  font or grid columns.

---

## Reference: backend metadata schema (current)

Events emitted to stderr by `tetra_decoder.py`, one JSON per line:

```
netinfo      mcc, mnc, dl_freq, ul_freq, color_code,
             cell_security_class, cell_tea,
             encrypted=false, encryption_type="none", la
freqinfo     dl_freq, ul_freq
encinfo      cell_security_class, cell_tea, enc_mode, encrypted=false
call_setup   ssi, ssi2, call_id, idx,
             encrypted, encryption_type, call_type        (annotated)
call_connect ssi, ssi2, call_id, idx, encrypted, encryption_type, call_type
tx_grant     ssi, ssi2, call_id, idx, encrypted, encryption_type, call_type
call_release ssi, call_id
status       ssi, ssi2, status
sds          ssi, ssi2
burst        timeslots{}, afc, burst_rate, call_type
resource     func, ssi, idt, ssi2?
```

Note: per Bug 5, the `encrypted/encryption_type` annotation on call
events is currently derived from suspect Basicinfo decoding and should
be replaced with `ENCR:` extraction from the PDU itself.
