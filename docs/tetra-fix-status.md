# TETRA Frequency Monitoring Branch — Status & Roadmap

Branch: `claude/tetra-frequency-monitoring-3IqM9`
Last updated: 2026-04-30 (session 2)

Purpose: fix the TETRA decoder pipeline so the OpenWebRX+ panel matches
reality and unencrypted voice traffic plays back as audio.

---

## Bugs identified and current status

### Bug 1 — Cell capability vs. active-call encryption (FIXED — `7c740a1`)

CRYPT field in NETINFO1 is the cell-level security-class advertisement
(TEA capability of the BS), NOT the per-call encryption status. Old
code treated it as 'this call is encrypted with TEAn'.

Fixed: NETINFO1 / ENCINFO1 emit `cell_security_class` and `cell_tea`
fields (informational), with `encrypted=false`. Per-call encryption
comes from a separate field (see Bug 5).

### Bug 2 — Reserved enc_mode flashing red (FIXED — `175b1e8`)

First version of `annotate_call_encryption` used `enc > 0`, so any
Basicinfo low-nibble in the reserved range 4..15 produced
`encrypted=true, encryption_type='none'` and the badge briefly flashed
'ENC NONE' in red.

Fixed: `enc in (1, 2, 3)`. Later obsoleted by Bug 5 fix (Basicinfo
is no longer used as the encryption source at all).

### Bug 3 — Audio header offset hardcoded (FIXED — `175b1e8`)

`AUDIO_HEADER_SIZE = 20` was a fixed offset assumption. Real header
width varies (18..22 bytes) with the hex digit count of TRA/RX/DECR.
Misalignment of 1–3 bytes shifted ACELP input and produced silence/noise.

Fixed: `parse_audio_from_udp` uses `match.end()` of the regex to
compute the binary payload offset dynamically.

### Bug 4 — Codec patch keystream handling DOESN'T HELP (REVERTED — `b5e19e8`)

First attempt was to swap the bundled `osmo-tetra-sq5bpf` codec.diff
for the newer `sq5bpf/install-tetra-codec` patch (~5KB larger, with
keystream-handling support). This made things worse:

The new patch interprets every 5th input frame as keystream and XORs
it into the decoded audio. We feed pure ACELP from `tetra-rx` with no
separate keystream stream, so every 5th frame corrupts the codec
state — producing periodic ~300ms intelligible voice + ~60ms
garbled in a loop.

Reverted to the bundled (11-year-old) codec.diff which decodes plain
ACELP without keystream interpretation. Correct behavior when no SCK
is loaded.

### Bug 5 — Basicinfo low-nibble misread as encryption_mode (FIXED — `b47abd1`)

The original code interpreted `Basicinfo:0xXY` as packed
`call_type:encryption_mode`, parsing the low nibble as TEAn. Empirical
data from the user's `tetra-debug.log` capture showed this is wrong;
the authoritative per-call encryption comes from a different field.

Fixed: extract `ENCC:N` from `DSETUPDEC` / `DCONNECTDEC` /
`DTXGRANTDEC` PDUs. Basicinfo is now used only for the call_type label
(high bits), without an appended TEAn suffix.

### Bug 6 — Audio frame regex never matched (FIXED — `b47abd1`)

User's `tetra-debug.log` capture revealed the actual TETMON audio
frame format from this build is `'TRA:HH RX:HH\x00<1380 ACELP bytes>'`
— a 13-byte text header with NUL terminator, no DECR field. My
original regex required `DECR:` and the fallback required `\s+` after
RX (NUL is not `\s`). Result: 0 ACELP frames extracted out of 450
audio packets. Persistent silence even when network was clear.

Fixed: unified `AUDIO_PATTERN` matches both formats:
  - `TRA:HH RX:HH\x00`              (passthrough mode, what -e emitted)
  - `TRA:HH RX:HH DECR:HH `         (standard mode)

### Bug 7 — Codec deadlocks the main loop (FIXED — `52d8833`)

Synchronous `decode()` writes 1380 bytes ACELP and immediately reads
960 bytes PCM. If the codec buffers more than one input frame before
producing output (which the install-tetra-codec patch did, before we
reverted), `read()` blocks forever, the UDP receive loop freezes,
the panel stops updating, the kernel socket buffer overflows, and
the whole decoder appears hung until the user cycles the modulation.

Fixed: refactored CodecPipeline to be asynchronous. `feed()` writes
to cdecoder.stdin and returns immediately; a background reader thread
pulls 960-byte PCM chunks from sdecoder.stdout and emits them on
sys.stdout when ready. Main loop never blocks on the codec.

### Bug 8 — `-e` flag corrupts audio (FIXED — `fec8ff9`)

Definitive fix found by comparing against the reference plugin
`mbbrzoza/OpenWebRX-Tetra-Plugin`. Both projects use the same
`osmo-tetra-sq5bpf` binary, but our `tetra-rx` invocation was
`-r -s -e /dev/stdin` while the reference uses just `-r -s /dev/stdin`.

The `-e` flag puts `tetra-rx` in encrypted-passthrough mode:
  - frames from encrypted calls are emitted instead of dropped,
  - the audio header switches from `TRA:HH RX:HH DECR:HH ` (3 fields,
    space-terminated) to `TRA:HH RX:HH\x00` (2 fields, NUL-terminated),
  - the bytes after the header are PRE-deinterleave passthrough, NOT
    fully decoded ACELP.

With -e on a clear cell, the codec receives mis-interleaved bytes that
periodically align by chance and produce ~300ms intelligible voice
fragments alternating with ~60ms garbled in a loop — the exact
symptom the user reported. Without -e:
  - clear cells: proper DECR-decoded ACELP -> intelligible audio.
  - encrypted cells: tetra-rx drops frames, audio stream at 0 kbps.
    Panel still correctly shows `Cell Sec.: TEAn (SC N)`.

The AUDIO_PATTERN regex still tolerates both formats so passing -e
for diagnostics doesn't break parsing.

### Bug 9 — `silence_20ms` padding inflated stream rate (FIXED — `365ec98`)

Decoder injected 20ms of zeros to stdout every 20ms whenever no ACELP
frame was available, making the OWRX+ Audio stream indicator oscillate
at 10–15 kbps even with no call. DMR decoder shows 0 kbps in the same
situation.

Fixed: removed all silence padding. PCM is now written only when the
codec produces a real frame. Matches DMR/NXDN/YSF behavior — stream
is silent (no bytes) when there is no voice activity.

### Bug 10 — Debug output swallowed by csdr_module_tetra (FIXED — `00c45c2`)

`csdr_module_tetra._read_meta()` captures the decoder's stderr and
filters non-JSON lines to `logger.debug()`, which never reaches
docker logs unless the entire OWRX+ logger is in DEBUG mode. The
original debug_dump() wrote to stderr and was therefore invisible.

Fixed: debug_dump now writes to a file (default `/tmp/tetra-debug.log`,
overridable via `TETRA_DEBUG_FILE`). Line-buffered append. Always
writes a startup banner regardless of TETRA_DEBUG, so users can
verify the new code is actually running.

### Bug 11 — PCM_OUTPUT_BYTES=960 misaligns sub-frame boundaries (FIXED — branch `claude/tetra-audio-fix-audio-codec-alignment`)

Root cause of the garbled/clear cycling audio pattern the user recorded.

The codec pipeline produces exactly 640 bytes of PCM per TETRA frame:
  - cdecoder: 2 × (BFI + 137 params) = 276 shorts = 552 bytes
  - sdecoder: 2 × 160 PCM shorts = 640 bytes (2 sub-frames × 320 bytes)

`PCM_OUTPUT_BYTES` was set to 960 (3 × 320), which is NOT a multiple of
the 640-byte frame boundary. Every `read(960)` crossed sub-frame boundaries:
  - Read 1: bytes 0–959 → frame 0 (640) + partial frame 1 (320 of 640)
  - Read 2: bytes 960–1919 → rest of frame 1 (320) + frame 2 (640)
  - This 2-frame alignment slip creates a periodic pattern where every
    3rd read starts at a wrong sub-frame boundary, producing garbled audio
    alternating with ~67% somewhat-clear segments.

Additionally, the AUDIO_PATTERN regex required `DECR:` followed by a
hex value and space (or bare `\x00` as fallback). The v1 osmo-tetra-sq5bpf
build (our build) uses the 2-field format `TRA:HH RX:HH\x00` (13-byte
header) WITHOUT a DECR field at all. The regex matched via the fallback,
but the docstring incorrectly described the header format.

Fixed:
  - `PCM_OUTPUT_BYTES` changed from 960 to 640
  - `AUDIO_PATTERN` regex updated to match both v1 and v2 headers:
    `TRA:HH RX:HH(\x00| DECR:i\x00)` with `match.end()` for dynamic offset
  - Docstrings and CLAUDE.md updated with correct frame format docs

---

## What was done in this branch (commits in chronological order)

| SHA | File | Summary |
|---|---|---|
| `7c740a1` | `tetra_decoder.py` | NETINFO1/ENCINFO1 emit cell capability fields, not encrypted=true |
| `c38183f` | `plugins/receiver/tetra/{tetra.js,tetra.css}` | Mirror v1.3 frontend |
| `ccabe18` | `build-tetra-packages.sh` | Switch codec.diff to install-tetra-codec (later reverted) |
| `175b1e8` | `tetra_decoder.py` | enc in (1,2,3); match.end() for header offset |
| `69276de` | `plugins/receiver/tetra/{tetra.js,tetra.css}` | v1.4: split Cell Sec / Encryption, always-show STATUS |
| `cfd357e` | `docs/tetra-fix-status.md` | Initial status doc |
| `365ec98` | `tetra_decoder.py` | Remove silence_20ms padding; match DMR behavior |
| `00c45c2` | `tetra_decoder.py` | Debug dump to file instead of stderr |
| `b47abd1` | `tetra_decoder.py` | Unified AUDIO_PATTERN; ENCC for per-call encryption |
| `52d8833` | `tetra_decoder.py` | Async codec pipeline (reader thread) |
| `b5e19e8` | `build-tetra-packages.sh` | Revert codec.diff to bundled patch |
| `fec8ff9` | `tetra_decoder.py` | Drop `-e` flag from tetra-rx (passthrough corruption) |

---

## Field schema (current)

Events emitted to stderr by `tetra_decoder.py`, one JSON per line:

```
netinfo      mcc, mnc, dl_freq, ul_freq, color_code,
             cell_security_class (int), cell_tea (str),
             encrypted=false, encryption_type="none", la
freqinfo     dl_freq, ul_freq
encinfo      cell_security_class, cell_tea, enc_mode, encrypted=false
call_setup   ssi, ssi2, call_id, idx, encc, encrypted, encryption_type, call_type
call_connect ssi, ssi2, call_id, idx, encc, encrypted, encryption_type, call_type
tx_grant     ssi, ssi2, call_id, idx, encc, encrypted, encryption_type, call_type
call_release ssi, call_id
status       ssi, ssi2, status
sds          ssi, ssi2
burst        timeslots{}, afc, burst_rate, call_type
resource     func, ssi, idt, ssi2?
```

---

## Validation status

| Symptom | After fix | Confirmed |
|---|---|---|
| Audio stream at 10–15 kbps when idle | -> 0 kbps when idle | YES (`365ec98`) |
| `[tetra-debug]` lines invisible in docker logs | -> /tmp/tetra-debug.log | YES (`00c45c2`) |
| 0 ACELP frames extracted | -> ACELP extraction working | YES (in dump after `b47abd1`) |
| Panel freezes on first ACELP | -> panel keeps updating | YES (`52d8833`) |
| Periodic garbled/clear cycling audio | -> continuous clear voice | **PENDING REBUILD (Bug 11 fix)** |
| TEA1 false positive on clear calls | -> ENCC-derived encryption | YES (`b47abd1`) |

---

## Open work

### Test on a clear cell

Commit `fec8ff9` (drop `-e`) is expected to be the final audio fix.
Needs field validation on a cell with `cell_security_class = 0`. The
user's primary test cell (391.525 / SC=2) will still produce no audio
because we have no SCK; that's expected.

Good test target: 390.050 (where the user originally heard voice via
SDRSharp, indicating SC=0).

### Frontend: SCK-locked badge state

When `cell_security_class > 0` AND no keyfile is loaded, the panel
currently shows green 'Clear' (correct per ENCC=0 semantics, but
misleading because the call is wrapped in air-interface encryption
the user can't decode). Proposed visual:

| Cell SC | ENCC | Badge | Color |
|---|---|---|---|
| 0 | 0 | Clear | green |
| 0 | 1/2/3 | TEAn (active) | red |
| >0 | 0 | Air-encrypted (SCK) | orange |
| >0 | 1/2/3 | TEAn + SCK | red |

Not yet implemented. Holding until the audio path is confirmed working.

### STATUS code dictionary

Observed codes on MNC 4321 (Brazil, custom operator):
  - 528, 4096, 17624, 55711

No public dictionary; would need operator contact or empirical mapping
(observe codes alongside known events). Plugins.tetra.statusNames is
ready to be populated at runtime from a user config.

### TS slot stale state

If a `call_setup` arrives but no matching `call_release`, the TS slot
stays 'active' indefinitely (icon visible, ISSI/GSSI dashes). Worth
adding a soft timeout (~30s of no `call_*` events for that slot ->
clear it). Cosmetic.

---

## Reference comparison: mbbrzoza/OpenWebRX-Tetra-Plugin

During session 2 we compared our pipeline against
[mbbrzoza/OpenWebRX-Tetra-Plugin](https://github.com/mbbrzoza/OpenWebRX-Tetra-Plugin),
which uses the same `osmo-tetra-sq5bpf` upstream and ETSI codec. Key
diffs found:

| Aspect | Reference | Ours (post-fixes) |
|---|---|---|
| `tetra-rx` flags | `-r -s` | `-r -s` (after `fec8ff9`) |
| Codec patch | bundled osmo-tetra-sq5bpf | bundled (after `b5e19e8`) |
| Builds float_to_bits | yes (unused at runtime) | no (not needed) |
| Codec call style | synchronous | async via reader thread |
| Silence padding | yes (legacy) | removed (`365ec98`) |
| Debug dumps | none | TETRA_DEBUG=1 -> file |
| Encryption source | Basicinfo low nibble | ENCC field |
| Cell-cap vs call-enc | merged | separated (`Cell Sec.` row) |

The reference does not split cell-capability from active-call encryption,
nor does it use the ENCC field. Our improvements there go beyond what
the reference does. The audio pipeline differences after our fixes are
functionally equivalent on clear cells; ours is more resilient on
encrypted cells (no deadlock, lower kbps when idle).
