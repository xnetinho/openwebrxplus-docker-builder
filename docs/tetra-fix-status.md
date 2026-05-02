# TETRA Frequency Monitoring Branch — Status & Roadmap

Branch: `claude/tetra-frequency-monitoring-3IqM9`
Last updated: 2026-05-02 (session 7)

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

### Bug 11 — Codec output mixed clear+scrambled (CONFIRMED IN PIPELINE; INITIAL DIAGNOSIS WRONG — `6ff2e36`)

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

**Initial hypothesis (sessions 4–6, eventually disproven)**: TEA2
encrypted bursts being decoded as garbled ACELP. Drove Bugs 12 and
13 work.

**Revised hypothesis (session 7, see Bug 14)**: scrambled audio is
not encryption — it's voice from MULTIPLE concurrent calls being
interleaved into the same UDP stream by `tetra-rx`, then fed
sequentially to a single codec instance.

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

100% of voice frames dropped: no short-form FUNCs were emitted
during the entire capture. The state machine never received a
single transition.

Conclusion (then): gate-by-ENCR is unworkable on this build.
Filter remains in code but defaults off (`TETRA_DROP_NO_CALL=0`).

### Bug 13 — Statistical per-frame encryption detection (ABANDONED — sessions 6–7)

Plan: gate codec input by a zlib-style entropy heuristic computed on
each ACELP voice frame. Inspired by TetraEar's fallback layer
(`unique_bytes/total > 0.7 → encrypted`).

#### Implementation iterations

| Commit | Metric | Result |
|---|---|---|
| `6ebdd1a` | zlib over full 1380-byte frame | tight cluster ~0.11, no discrimination |
| `97d9d34` | zlib over 690 sign bytes (high byte of int16) | tight cluster ~0.18, no discrimination |
| `315e15d` | added bit-packed signs (87 bytes) and bit-flip rate | bit-packed ~0.78, flip ~0.33 — still tight clusters |
| `6470d93` | restricted to 432 actual soft-bit positions (skip magic + filler) | bitsD ~1.18, flipD ~0.51 — random-looking, still tight |

#### Why it fundamentally cannot work

The 432 ACELP soft-bits emitted by tetra-rx are **post-Viterbi /
post-FEC**. Modern speech codecs (ACELP) are designed so quantized
parameters approximate uniform random distribution at the bit level.
Combined with FEC, the bit stream is already near-maximum-entropy.
Adding TEA2/TEA3 encryption (XOR with keystream) on top of an
already-random sequence preserves randomness — no measurable change
in any single-frame statistic.

Calibration runs (140 frames session 6, 287 frames session 7) on
captures with mixed clear+scrambled audio always produced a single
tight cluster on every metric tried:

| metric | min | max | mean | spread |
|---|---|---|---|---|
| full | 0.108 | 0.118 | 0.113 | 0.010 |
| sign | 0.171 | 0.190 | 0.181 | 0.019 |
| bits | 0.770 | 0.805 | 0.786 | 0.035 |
| flip | 0.290 | 0.366 | 0.327 | 0.076 |
| signD | 0.241 | 0.269 | 0.256 | 0.028 |
| bitsD | 1.164 | 1.200 | 1.199 | 0.036 |
| flipD | 0.449 | 0.567 | 0.506 | 0.118 |

`bitsD` ≈ 1.2 means zlib output exceeds input — the bit stream is
genuinely random. `flipD` ≈ 0.51 confirms ~50% bit-flip rate.
Speech ACELP after Viterbi is statistically indistinguishable from
random noise. **Option A is dead.** No per-frame statistical
discriminator on the current passthrough format will work.

The metric infrastructure (`_all_metrics`, the 7 candidate
discriminators, `TETRA_ENC_METRIC` env, `TETRA_ENC_RATIO_THRESHOLD`)
is left in the code as a debugging surface. With
`TETRA_ENC_RATIO_THRESHOLD=0` (default), it is a no-op.

### Bug 14 — Multiplexed concurrent-call interleaving (NEW HYPOTHESIS — session 7)

User narrative (session 7, while listening live during a build):

> "o áudio embaralhado aparenta ser o mesmo áudio limpo mas reproduzido
> de forma desordenada, pois em algumas escutas de áudio embaralho,
> mesmo com dificuldade, é possível compreender o teor da conversa,
> é como se os dados chegassem de forma desordenada e fossem
> demodulados assim."

(Translation: "the scrambled audio sounds like the same clean audio
but reproduced in a disordered way; in some listens to scrambled
audio, even with difficulty, it's possible to understand the content
of the conversation; it's like the data is arriving out of order and
being demodulated that way.")

This is **not** encryption — it's **voice-stream multiplexing without
demultiplexing**. Hypothesis:

`tetra-rx` from `osmo-tetra-sq5bpf` decodes voice from **all four
TETRA timeslots** that carry traffic, plus possibly multiple SSIs
sharing the same TS over time, and emits all bursts to the same
single UDP voice stream tagged only with `TRA:HH RX:HH DECR:%i\0`.
The header has **no SSI, no TS, no call-id field** that would let
us demultiplex. We feed everything into one cdecoder instance;
the codec interprets sequential bursts as if they were one stream.

When only one call is active → audio is intelligible (single source).
When two or more calls are active concurrently → consecutive bursts
belong to different speakers, codec state thrashes, audio sounds
"scrambled" — but is actually two intelligible conversations
super-imposed.

Consistent with every observation:

| Observation | Bug 11 (encryption) | Bug 14 (interleaving) |
|---|---|---|
| `.raw` mixed clear/scrambled identical to browser | ✓ | ✓ |
| Mixed segments are speech-shaped | ✓ | ✓ |
| Bug 12 ENCR-gate dropped 100% of voice | ✗ unexpected | ✓ — D-SETUP rare regardless of clear/enc |
| Bug 13 metrics show all frames look random | ✗ contradicts | ✓ — speech ACELP IS statistically random |
| User can partially understand "scrambled" audio | ✗ should be pure noise if encrypted | ✓ — multiple intelligible streams overlapped |
| Scrambled increases when base responds | ✗ unrelated | ✓ — second talker activates second SSI/TS |
| Panel only shows TS1, but voice flows continuously | ✗ unexplained | ✓ — tetra-rx emits voice from all TS regardless of panel state |

#### Why it's promising

If true, the fix path is:
  1. Patch upstream to include TS index and SSI in the voice header
     (`TRA:HH RX:HH DECR:%i TS:%d SSI:%d\0`), OR equivalently emit
     the voice via separate UDP ports per TS.
  2. In `tetra_decoder.py`, demultiplex by TS/SSI: maintain one
     `CodecPipeline` instance per active SSI, feed each its own
     bursts.
  3. Pick which decoded stream to forward to OWRX+ (e.g., the
     longest-running, the one in the user-selected talkgroup, or
     mix all PCM with averaging).

Step 1 is essentially Option B from Bug 13 with a different field
emitted — same upstream patch infrastructure.

#### Why we couldn't see it before

- Bug 11 hypothesis "encrypted bursts decoded as ACELP garbage"
  fits the symptom equally well to a casual observer.
- TetraEar focuses on encryption (it's the project's main feature)
  so we mistakenly imitated their approach.
- mbbrzoza reference plugin has the same problem but didn't document
  the cause.
- The TETRA cell at 391.525 happens to host a high-traffic talkgroup
  (active QSO + base response), making interleaving frequent.

#### Next diagnostic step

Need a tcpdump-style capture to confirm whether tetra-rx multiplexes
voice from multiple TS or SSIs into the same UDP stream. Concretely:
add per-frame logging of `is_traffic` byte, plus byte-distribution
fingerprint, plus inter-arrival time. If we see consecutive frames
arriving with periodic patterns matching multiple interleaved
sources (e.g., two distinct fingerprint clusters alternating every
60 ms), Bug 14 is confirmed.

Alternatively (faster path): patch upstream `tetra_lower_mac.c` to
include TS in the voice header. Build, run, observe whether multiple
TS values appear in voice frames during a "scrambled" listen.

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
| `7b464a5` | `docs/tetra-fix-status.md` | Register Bug 11 outcome, failed Bug 12, upstream DECR findings |
| `08e96c3` | `docs/tetra-fix-status.md` | Register TetraEar findings + Option A approval |
| `6ebdd1a` | `tetra_decoder.py` | Bug 13 attempt 1: zlib over full frame (no discrim) |
| `97d9d34` | `tetra_decoder.py` | Bug 13 attempt 2: zlib over sign bytes (no discrim) |
| `315e15d` | `tetra_decoder.py` | Bug 13 attempt 3: 4 metrics in parallel (full/sign/bits/flip) |
| `6470d93` | `tetra_decoder.py` | Bug 13 attempt 4: data-window metrics signD/bitsD/flipD (no discrim — Option A dead) |

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
| Periodic 300ms-intelligible voice | -> still mixed clear+scrambled | NO — see Bug 14 |
| TEA1 false positive on clear calls | -> ENCR-derived encryption | superseded — ENCC doesn't exist on this build |
| Mixed clear+scrambled audio in browser | -> still mixed offline (`.raw` confirms) | confirmed in pipeline |
| `TETRA_DROP_NO_CALL=1` filters encrypted | -> total silence (Bug 12) | filter unusable on this build |
| Bug 13 statistical filter discriminates | -> all 7 metrics show single tight cluster | Option A dead |

---

## Open work

### Bug 14 — Demultiplex voice by TS/SSI (next decision point)

Two sub-options, equivalent to Bug 13 A/B but for a different field:

- **A (Python-only)**: read each frame's TRA/RX bytes plus a
  byte-distribution fingerprint of the soft-bit window, cluster
  consecutive frames by similarity, route to per-cluster codec
  instances. Brittle and may need tuning. Zero rebuild cost.

- **B (upstream patch)**: modify
  `osmo-tetra-sq5bpf-2/src/lower_mac/tetra_lower_mac.c` so the voice
  header includes TS and SSI:
  ```c
  sprintf(tmpstr,"TRA:%2.2x RX:%2.2x DECR:%i TS:%d SSI:%d\0",
          tms->cur_burst.is_traffic, tetra_hack_rxid, decrypted,
          tms->cur_ts, tms->cur_call_ssi);
  ```
  Recompile, update Python parser to read TS/SSI, demux to per-SSI
  codec instances. Single source of truth. Confirmed needed if
  Option A from this bug doesn't work. Same rebuild cost as Bug 13's
  Option B (which was never triggered).

### Frontend: SCK-locked badge state (defer)

When `cell_security_class > 0` AND no keyfile is loaded, the panel
shows green 'Clear' (correct per ENCR=0 semantics, but misleading
since the call is wrapped in air-interface encryption the user can't
decode). Visual table:

| Cell SC | ENCR | Badge | Color |
|---|---|---|---|
| 0 | 0 | Clear | green |
| 0 | 1/2/3 | TEAn (active) | red |
| >0 | 0 | Air-encrypted (SCK) | orange |
| >0 | 1/2/3 | TEAn + SCK | red |

Holding until audio path works.

### STATUS code dictionary (defer)

Observed codes on MNC 4321 (Brazil): 528, 4096, 17624, 55711.
No public dictionary; needs operator contact or empirical mapping.
`Plugins.tetra.statusNames` already accepts runtime user config.

### TS slot stale state (cosmetic)

Soft 30s timeout to clear `call_setup` slots when no matching
`call_release` arrives.

### Frequency offset (separate bug, defer)

User reports actually listening on 391.025 MHz while panel shows
DL Freq 391.525 MHz — 500 kHz offset = 20 channels in 25 kHz raster.
Likely a centre-frequency / IF offset in the demod chain. Not
related to Bug 14; track separately.

---

## Reference comparisons

### mbbrzoza/OpenWebRX-Tetra-Plugin (session 2)

Same `osmo-tetra-sq5bpf` upstream + ETSI codec. Diffs after our fixes:

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
| Mixed audio on busy cell | same problem (untested) | same problem — see Bug 14 |

### aruznieto/TetraEar (session 6)

Pure-Python TETRA decoder (does NOT use osmo-tetra). Relevant bits:

| Aspect | TetraEar | Ours |
|---|---|---|
| Upstream | Reimplements lower MAC + protocol in Python | Uses osmo-tetra-sq5bpf binary |
| Encryption detection | MAC header 2-bit field + entropy fallback + call-meta override + SDS heuristic | Tried both — neither works on osmo-tetra passthrough format |
| Codec format | builds 690 × int16 soft-bits, header `0x6B21` | tetra-rx already emits this format (verified via log v3) |
| Auto-decrypt | yes (loads keys from `keys.txt`) | no key loaded; encrypted bursts pass through as garbage |
| Per-TS / per-SSI demux | full TETRA stack — implicit | currently single codec instance for all bursts (Bug 14) |

TetraEar's MAC-layer parsing was useful to confirm encryption_mode is
exposable from a fork of osmo-tetra (Bug 13 / Bug 14 Option B). Its
entropy fallback validated the *idea* of statistical detection but
operates on raw MAC PDU bytes; we operate on post-Viterbi soft-bits
where the metric signal collapses (Bug 13 lesson).

---

## Session log (high-level)

- **Session 1**: Bugs 1, 2, 3 identified and fixed; codec.diff swap (later reverted as Bug 4).
- **Session 2**: Bugs 5, 6, 7, 9, 10 fixed; reference plugin comparison; async codec pipeline; ENCC parsing introduced (later proven wrong).
- **Session 3**: Bug 8 (`-e` flag) fixed; PCM-dump diagnostic step added (Bug 11 isolation).
- **Session 4**: User confirmed offline `.raw` is identical to browser audio → artefact is in pipeline (not OWRX+ chain). Log v3 analysis: ENCC absent, ENCR present (79 events all `ENCR:0`). Bug 12 implemented (`f0ed4d2`).
- **Session 5**: Log v4 analysis: 0 ENCR fields in 403 MB capture, 100% frame drop, total silence — Bug 12 unusable. Upstream confirms no encryption flag in passthrough header beyond `DECR`. Bug 13 opened with 3 options.
- **Session 6**: TetraEar inspection. User approved Option A. Implemented zlib(full) and zlib(signs); both showed single-cluster ratios (~0.11, ~0.18) on 140 mixed-audio frames. Added bit-packed signs and flip rate; still single clusters.
- **Session 7**: Restricted metrics to the 432-position soft-bit window (skip magic + filler). bitsD ≈ 1.2 (no compression possible) and flipD ≈ 0.51 (random) prove ACELP soft-bits are statistically random regardless of clear/encrypted. **Option A formally abandoned.** User reported live observation: scrambled audio is intelligible but disordered, suggesting **interleaved concurrent calls** (Bug 14) rather than encrypted content. Hypothesis fits all observations (Bug 11 narrative, Bug 12 silence, Bug 13 random metrics). Pending decision Bug 14 A/B.
