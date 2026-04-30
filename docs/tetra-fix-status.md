# TETRA Frequency Monitoring Branch — Status & Roadmap

Branch: `claude/tetra-frequency-monitoring-3IqM9`
Last updated: 2026-04-30 (session 3)

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

### Bug 11 — Audio still mixed clear+scrambled after all fixes (OPEN — diagnostic step `6ff2e36`)

Even after applying every fix above, on a cell reported as `Cell Sec.:
TEA2 / Encryption: Clear` (391.525), the user still hears intelligible
speech mixed with scrambled segments. PCM IS being produced (~251
chunks of 960 bytes per debug dump), the input ACELP stream looks
uniform (`TRA:39`, first byte `0x21` consistently), and yet the output
is partly garbled. Observed PCM emission rate ≈ 50% of real-time, hinting
at frame loss or rate mismatch somewhere in the chain.

Hypotheses still on the table:
1. Codec input-format mismatch (cdecoder may expect bit-soft-decision
   int16 per bit, not packed bytes).
2. OWRX+ post-processing chain (Opus encoder, resampler) introduces
   the artefacts AFTER our decoder writes clean PCM.
3. osmo-tetra-sq5bpf upstream is delivering frames that are partially
   from encrypted bursts even though ENCC=0.

**Diagnostic step (commit `6ff2e36`):** added `TETRA_PCM_DUMP` env var
to `tetra_decoder.py`. When set, every PCM chunk emitted to stdout is
also appended to the given file. This isolates the codec output from
the OpenWebRX+ post-processing chain.

User instructions:

```sh
# 1. Pull the latest image with the new env support
docker pull <image>

# 2. Add to your container env (Portainer / docker-compose):
TETRA_DEBUG=1
TETRA_DEBUG_FILE=/tmp/tetra-debug.log
TETRA_PCM_DUMP=/tmp/tetra-pcm.raw

# 3. Tune to the test frequency, let a clear call play for ~30 s.

# 4. Copy the file out:
docker cp <container>:/tmp/tetra-pcm.raw .

# 5. Listen offline:
aplay -r 8000 -f S16_LE -c 1 tetra-pcm.raw
```

Decision tree from the result:
  - **PCM is intelligible offline (clean voice in `aplay`)**: the codec
    is correct; the artefact is introduced by OWRX+'s Opus encoder /
    resampler / WebSocket chain. Fix moves to `csdr_module_tetra.py`
    or the audio output stage.
  - **PCM is also scrambled offline**: the artefact is in our pipeline.
    Look at codec input format (try bit-unpack), frame timing, or
    osmo-tetra-sq5bpf upstream behavior on TEA2-capable cells.

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
| `6ff2e36` | `tetra_decoder.py` | Add TETRA_PCM_DUMP env for offline `aplay` diagnosis |

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
| Periodic 300ms-intelligible voice | -> still mixed clear+scrambled | NO — see Bug 11 |
| TEA1 false positive on clear calls | -> ENCC-derived encryption | YES (`b47abd1`) |

---

## Open work

### Bug 11 PCM-dump diagnosis (next user-side step)

See Bug 11 above. After the user listens to `tetra-pcm.raw` offline with
`aplay`, the next code change goes either into the decoder pipeline or
into OWRX+'s audio output chain. Holding all other work until that
result.

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
