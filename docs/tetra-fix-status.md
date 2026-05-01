# TETRA Frequency Monitoring Branch — Status & Roadmap

Branch: `claude/tetra-frequency-monitoring-3IqM9`
Last updated: 2026-05-01 (session 5)

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

### Bug 5 — Basicinfo low-nibble misread as encryption_mode (FIXED — `b47abd1`, then SUPERSEDED)

The original code interpreted `Basicinfo:0xXY` as packed
`call_type:encryption_mode`, parsing the low nibble as TEAn.

First fix attempted `ENCC:N` from `DSETUPDEC` / `DCONNECTDEC` /
`DTXGRANTDEC` PDUs. **This was wrong**: log v3 analysis (session 4)
revealed that this build does NOT emit `ENCC:` at all. The actual
per-call encryption indicator is `ENCR:N` and it appears only on the
short-form FUNCs (`D-SETUP`, `D-CONNECT`, `D-RELEASE`, `D-TX`).

Superseded by Bug 12 attempt (commit `f0ed4d2`), which moved the
encryption parse to `ENCR` on short-form FUNCs.

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

### Bug 11 — Codec output itself is mixed clear+scrambled (CONFIRMED IN PIPELINE — `6ff2e36`)

Even after every fix above, on a TEA2-capable cell (391.525 MHz)
with a clear call active, the user hears intelligible speech
intermixed with scrambled segments.

Diagnostic step (`6ff2e36`): added `TETRA_PCM_DUMP=/path.raw` env var
so every PCM chunk emitted by the codec is also appended to a raw
file. User listened with `aplay -r 8000 -f S16_LE -c 1 ...` and in
Audacity (Signed 16-bit PCM, LE, mono, 8 kHz).

**Result confirmed by user (session 4)**: the offline `.raw` is
*identical* to what the browser plays — clear voice mixed with
scrambled segments. This **rules out** the OWRX+ post-processing
chain (Opus encoder, resampler, WebSocket). The artefact is in our
pipeline OR in osmo-tetra-sq5bpf upstream.

User narrative reinforces the diagnosis: hears one operator clearly,
then the base-station response on the same TS comes back scrambled,
and subsequent QSOs (the operator's replies) remain scrambled. Strong
match with TEA2-encrypted calls being decoded as garbled ACELP — i.e.
osmo-tetra-sq5bpf is emitting voice bursts even for encrypted calls
on this build, and we feed them to the codec indiscriminately.

### Bug 12 — Active-clear-call filter (ATTEMPTED, FAILED — `f0ed4d2`)

Hypothesis: gate codec input on a per-SSI clear-call state machine
driven by short-form FUNC ENCR field. Track:

  - `D-SETUP` / `D-CONNECT` with `ENCR:0`  → mark SSI clear.
  - `D-SETUP` / `D-CONNECT` with `ENCR:1/2/3` → drop SSI from clear set.
  - `D-RELEASE` or `D-TX CEASED` → drop SSI from clear set.
  - 30 s stale timeout per SSI.

Drop ACELP frames if no SSI is currently in the clear set
(`TETRA_DROP_NO_CALL=1`, default; set to `0` to disable filter).

**Result: total silence.** Log v4 (403 MB capture, session 5):

```
grep -E 'call_open_clear|call_release|call_drop_enc' tetra-debug.log
   | wc -l                                        →  0
grep 'frame_stats' tetra-debug.log | tail -1
   →  fed=0 dropped=20278 active_clear=0
grep -oE 'ENCR:[0-9]+' tetra-debug.log | wc -l   →  0
```

100% of the 20 278 voice frames were dropped because zero short-form
FUNCs were emitted during the entire 3+ minute capture (zero `ENCR:`
occurrences). The state machine never received a single transition.

For comparison, log v3 (session 4) had 79 `ENCR:0` events, all from
short-form FUNCs, so the parsing path itself works — but those events
are emitted only sporadically by `tetra-rx` while voice bursts flow
continuously, and on log v4 they never appeared.

Conclusion: **the gate-by-ENCR strategy is unworkable** on this
build. Filter remains in code (gated by `TETRA_DROP_NO_CALL`, default
ON) but is effectively unusable until replaced — user must set
`TETRA_DROP_NO_CALL=0` to get audio at all.

### Bug 13 — Need a frame-level encryption indicator (OPEN — analysis complete)

To fix Bug 11 we need to drop encrypted ACELP bursts on a per-frame
basis (signaling cannot be the source of truth — see Bug 12).

**Upstream investigation (session 5)**: read
`sq5bpf/osmo-tetra-sq5bpf-2/src/lower_mac/tetra_lower_mac.c` via
WebFetch. The voice-frame passthrough is:

```c
sprintf(tmpstr,"TRA:%2.2x RX:%2.2x DECR:%i\0",
        tms->cur_burst.is_traffic, tetra_hack_rxid, decrypted);
```

  - `TRA:` = `is_traffic` (boolean upstream; the 0x19/0x1d/0x21 etc.
    values we observed in the log are likely artefacts of adjacent
    `block` memory bleeding into the format buffer, not a counter).
  - `RX:`  = fixed `tetra_hack_rxid`.
  - `DECR:` = `decrypted` flag — set to `1` only if
    `get_voice_keystream()` succeeded; `0` otherwise.

Crucially, `decrypted=0` is emitted both for **clear** bursts (no
keystream needed) and for **encrypted bursts we couldn't decrypt**
(no key loaded). They are indistinguishable in the passthrough header.

A grep of the upstream source confirmed: **no other variable**
(`encrypted_burst`, `encr_burst`, `kss_failed`, `encryption_used`, …)
exists that flags "this burst was encrypted regardless of decryption
outcome". The header carries `DECR` only.

Implication: for a TEA2/TEA3 cell **without keys**, no header-based
filter can separate clear from encrypted. Three remaining paths:

| Option | Approach | Risk / cost |
|---|---|---|
| A | Statistical detection on the 1380-byte ACELP payload (entropy / χ² — encrypted is near-uniform, clear has structure). Gate codec.feed() per frame. | Medium. ~80 LOC Python; needs calibration with the existing PCM dump. Risk of false positives on silence/whisper bursts. |
| B | Patch upstream `tetra_lower_mac.c` to read the MAC PDU encryption-mode bit and emit a real `ENC:N` field in the passthrough header. | High. Modify C, rebuild osmo-tetra in the Docker image. Authoritative result. |
| C | Accept mixed audio. Set `TETRA_DROP_NO_CALL=0` and document the limitation. | Zero cost. No real fix. |

Decision pending — user to choose A, B, or C.

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
| `b47abd1` | `tetra_decoder.py` | Unified AUDIO_PATTERN; ENCC for per-call encryption (later wrong) |
| `52d8833` | `tetra_decoder.py` | Async codec pipeline (reader thread) |
| `b5e19e8` | `build-tetra-packages.sh` | Revert codec.diff to bundled patch |
| `fec8ff9` | `tetra_decoder.py` | Drop `-e` flag from tetra-rx (passthrough corruption) |
| `6ff2e36` | `tetra_decoder.py` | Add TETRA_PCM_DUMP env for offline `aplay` diagnosis |
| `f0ed4d2` | `tetra_decoder.py` | Active-clear-call filter via short-form FUNC ENCR (Bug 12 — failed) |

---

## Field schema (current)

Events emitted to stderr by `tetra_decoder.py`, one JSON per line:

```
netinfo      mcc, mnc, dl_freq, ul_freq, color_code,
             cell_security_class (int), cell_tea (str),
             encrypted=false, encryption_type="none", la
freqinfo     dl_freq, ul_freq
encinfo      cell_security_class, cell_tea, enc_mode, encrypted=false
call_setup        ssi, encr, encrypted, encryption_type, call_type    (D-SETUP)
call_connect      ssi, encr, encrypted, encryption_type, call_type    (D-CONNECT)
call_setup_detail   ssi, ssi2, call_id, idx                           (DSETUPDEC)
call_connect_detail ssi, ssi2?, call_id, idx                          (DCONNECTDEC)
call_release ssi, call_id?
tx           ssi, func                                                (D-TX*)
tx_grant     ssi, ssi2?, call_id, idx                                 (DTXGRANTDEC)
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
| TEA1 false positive on clear calls | -> ENCC-derived encryption | superseded — ENCC doesn't exist on this build |
| Mixed clear+scrambled audio in browser | -> still mixed offline (`.raw` confirms) | confirmed in pipeline (Bug 11) |
| `TETRA_DROP_NO_CALL=1` filters encrypted | -> total silence (Bug 12) | filter unusable on this build |

---

## Open work

### Bug 13 — Pick A / B / C (next decision point)

Source-of-truth investigation is done. The header passthrough has no
"this burst was encrypted" flag. User to pick:

- **A** statistical: probably-fast, may have false positives.
- **B** upstream patch: probably-correct, more invasive.
- **C** accept mixed audio: no work, no fix.

Workaround in the meantime: `TETRA_DROP_NO_CALL=0` to disable the
useless-on-this-build filter and revert to mixed audio.

### Frontend: SCK-locked badge state

When `cell_security_class > 0` AND no keyfile is loaded, the panel
currently shows green 'Clear' (correct per ENCR=0 semantics, but
misleading because the call is wrapped in air-interface encryption
the user can't decode). Proposed visual:

| Cell SC | ENCR | Badge | Color |
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
| Encryption source | Basicinfo low nibble | ENCR (short-form FUNC) |
| Cell-cap vs call-enc | merged | separated (`Cell Sec.` row) |
| Mixed clear/encrypted audio on TEA2 cell | same problem (untested) | same problem — see Bug 11/13 |

The reference does not split cell-capability from active-call encryption,
nor does it use ENCR-driven gating. Our improvements there go beyond
what the reference does. The audio pipeline differences after our
fixes are functionally equivalent on clear-only cells; the open
issue (Bug 11/13) likely affects the reference plugin too on
TEA2-capable cells.

---

## Session log (high-level)

- **Session 1**: Bugs 1, 2, 3 identified and fixed; codec.diff swap (later reverted as Bug 4).
- **Session 2**: Bugs 5, 6, 7, 9, 10 fixed; reference plugin comparison; async codec pipeline; ENCC parsing introduced (later proven wrong).
- **Session 3**: Bug 8 (`-e` flag) fixed; PCM-dump diagnostic step added (Bug 11 isolation).
- **Session 4**: User confirmed offline `.raw` is identical to browser audio → artefact is in pipeline (not OWRX+ chain). Log v3 analysis: ENCC absent, ENCR present (79 events all `ENCR:0`); voice frames precede first short-form FUNC by 125 frames. Active-clear-call filter implemented (Bug 12, commit `f0ed4d2`).
- **Session 5**: Log v4 analysis: 0 ENCR fields in 403 MB capture, 100% frame drop, total silence — Bug 12 unusable. WebFetch on upstream `osmo-tetra-sq5bpf-2` confirms no encryption flag exists in passthrough header beyond `DECR` (which is decryption-success, not encryption-detected). Bug 13 opened with three options (A statistical / B upstream patch / C accept). Pending user decision.
