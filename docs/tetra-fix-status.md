# TETRA Frequency Monitoring Branch — Status & Roadmap

Branch: `claude/tetra-frequency-monitoring-3IqM9`
Last updated: 2026-05-03 (session 8)

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
compute the binary payload offset dynamically. Superseded by Bug 14
which moves to a fixed 32-byte header.

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
  - `TRA:HH RX:HH\x00`              (default upstream)
  - `TRA:HH RX:HH DECR:HH `         (alt format)

Subsequently extended in Bug 14 to also accept the patched
32-byte format with `TN:N`.

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

The `-e` flag puts `tetra-rx` in encrypted-passthrough mode. With -e
on a clear cell, the codec receives mis-interleaved bytes that
periodically align by chance and produce ~300ms intelligible voice
fragments alternating with ~60ms garbled in a loop. Without -e:
clear cells decode properly; encrypted bursts are dropped by the
upstream binary.

### Bug 9 — `silence_20ms` padding inflated stream rate (FIXED — `365ec98`)

Decoder injected 20ms of zeros to stdout every 20ms whenever no ACELP
frame was available, making the OWRX+ Audio stream indicator oscillate
at 10–15 kbps even with no call. Fixed: removed all silence padding.
PCM is now written only when the codec produces a real frame.

### Bug 10 — Debug output swallowed by csdr_module_tetra (FIXED — `00c45c2`)

`csdr_module_tetra._read_meta()` filters non-JSON stderr to
`logger.debug()`, invisible without DEBUG mode. Fixed: debug_dump
writes to `/tmp/tetra-debug.log` (overridable via `TETRA_DEBUG_FILE`).

### Bug 11 — Codec output mixed clear+scrambled (REFRAMED — see Bug 14)

Even after every fix above, on a TEA2-capable cell (391.525 MHz)
with a clear call active, the user hears intelligible speech
intermixed with scrambled segments.

Diagnostic step (`6ff2e36`): added `TETRA_PCM_DUMP=/path.raw` env var
so every PCM chunk emitted by the codec is also appended to a raw
file. User confirmed (session 4) the offline `.raw` is identical to
what the browser plays — artefact is in our pipeline OR in
osmo-tetra-sq5bpf upstream, not in OWRX+ post-processing.

**Initial hypothesis (sessions 4–6, eventually disproven)**: TEA2
encrypted bursts being decoded as garbled ACELP. Drove Bug 12
(signaling-gate) and Bug 13 (statistical-gate) work, both of which
failed.

**Revised hypothesis (session 7, see Bug 14)**: voice from MULTIPLE
concurrent calls / timeslots being interleaved into the same UDP
stream by `tetra-rx`, then fed sequentially to a single codec
instance. User narrative ("scrambled audio is the same clean voice
played out of order, partially intelligible") is incompatible with
encryption (which would be pure noise) and consistent with
multi-source interleaving.

### Bug 12 — Active-clear-call filter (ATTEMPTED, FAILED — `f0ed4d2`)

Hypothesis: gate codec input on a per-SSI clear-call state machine
driven by short-form FUNC ENCR field.

**Result: total silence.** Log v4 (403 MB capture, session 5):
20 278/20 278 voice frames dropped, zero `ENCR:` in 403 MB log.
Short-form FUNCs are too sparse on this build. Filter remains in
code but defaults off (`TETRA_DROP_NO_CALL=0`).

### Bug 13 — Statistical per-frame encryption detection (ABANDONED — sessions 6–7)

Plan: gate codec input by zlib-style entropy on each ACELP frame.

#### Implementation iterations

| Commit | Metric | Result |
|---|---|---|
| `6ebdd1a` | zlib over full 1380-byte frame | tight cluster ~0.11, no discrimination |
| `97d9d34` | zlib over 690 sign bytes | tight cluster ~0.18, no discrimination |
| `315e15d` | added bit-packed signs and bit-flip rate | bit-packed ~0.78, flip ~0.33 — still tight |
| `6470d93` | restricted to 432 actual soft-bit positions (skip magic + filler) | bitsD ~1.18, flipD ~0.51 — random-looking, still tight |

#### Why it fundamentally cannot work

The 432 ACELP soft-bits emitted by tetra-rx are post-Viterbi /
post-FEC. Modern speech codecs (ACELP) are designed so quantized
parameters approximate uniform random distribution at the bit level.
Combined with FEC, the bit stream is already near-maximum-entropy.
TEA2 encryption (XOR with keystream) on top preserves randomness —
no measurable change in any single-frame statistic.

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

`bitsD` ≈ 1.2 means zlib output exceeds input — bit stream is
genuinely random. `flipD` ≈ 0.51 confirms ~50% bit-flip rate.
**Option A is dead.** Metric infrastructure left in code as a
debug surface, gated by `TETRA_ENC_RATIO_THRESHOLD=0` (off).

### Bug 14 — Multiplexed concurrent-call interleaving (IMPLEMENTED — pending validation)

User narrative (session 7) ruled out the encryption hypothesis:

> "o áudio embaralhado aparenta ser o mesmo áudio limpo mas reproduzido
> de forma desordenada, pois em algumas escutas de áudio embaralho,
> mesmo com dificuldade, é possível compreender o teor da conversa"

Evidence matrix:

| Observation | Encrypted hypothesis | Interleaving hypothesis |
|---|---|---|
| `.raw` mixed clear/scrambled identical to browser | ✓ | ✓ |
| Mixed segments are speech-shaped | ✓ | ✓ |
| Bug 12 ENCR-gate dropped 100% of voice | ✗ unexpected | ✓ — D-SETUP rare |
| Bug 13 metrics show all frames look random | ✗ contradicts | ✓ — speech ACELP IS random |
| User can partially understand "scrambled" audio | ✗ should be pure noise | ✓ — overlapped speech |
| Scrambled increases when base responds | ✗ unrelated | ✓ — second SSI activates |
| Panel only shows TS1, but voice flows continuously | ✗ unexplained | ✓ — tetra-rx emits all TS regardless |

#### Decision: Option B (upstream patch, session 7)

User approved Option B: patch upstream `tetra_lower_mac.c` to add
the timeslot number (TN) to the voice-frame header so the Python
wrapper can demultiplex.

#### Upstream investigation (session 8)

Read `osmo-tetra-sq5bpf/src/lower_mac/tetra_lower_mac.c` (the older
repo our build clones — NOT `-sq5bpf-2`) via WebFetch. Confirmed:

  - The voice sprintf is in `tp_sap_udata_ind`, case `TPSAP_T_SCH_F`,
    inside `if (tms->cur_burst.is_traffic)`.
  - Original format: `sprintf(tmpstr,"TRA:%2.2x RX:%2.2x\0",...)` —
    13-byte header (matches what user observed in v3 log).
  - `tcd->time.tn` is in scope and contains the current TN (0..3).
  - `tms->ssi` and `tms->tsn` do **not** exist in this repo's
    `struct tetra_mac_state` (they exist in `-sq5bpf-2` only).
    So we can demultiplex by TN, but not by SSI, without a deeper
    refactor.

#### Implementation

Build-time patch (`a02be9b`): in `build-tetra-packages.sh`, after
`git clone` we use `sed`+`perl` to rewrite three lines of
`tetra_lower_mac.c`:

```c
- unsigned char tmpstr[1380+13];
+ unsigned char tmpstr[1380+32];
...
- sprintf(tmpstr,"TRA:%2.2x RX:%2.2x\0",
-     tms->cur_burst.is_traffic, tetra_hack_rxid);
- memcpy(tmpstr+13, block, sizeof(block));
- sendto(..., sizeof(block)+13, ...);
+ memset(tmpstr, 0, 32);
+ snprintf((char *)tmpstr, 32, "TRA:%2.2x RX:%2.2x TN:%i",
+     tms->cur_burst.is_traffic, tetra_hack_rxid, tcd->time.tn);
+ memcpy(tmpstr+32, block, sizeof(block));
+ sendto(..., sizeof(block)+32, ...);
```

Three post-patch `grep` assertions hard-fail the build if any of the
three edits silently missed (e.g. upstream changed those lines).
UDP voice packet size becomes 1412 bytes (was 1393).

Python wrapper (`ae524aa`): `parse_audio_from_udp` returns
`(tn, acelp_bytes)`. Reads the first 32 bytes as text (NUL-padded),
extracts `TN:N`. Falls back to the legacy 13-byte regex when no
TN found, returning `tn=-1` (so unpatched binaries still work).

Main loop applies a "first-TN-wins" lock:

  - When no TN is currently locked, the first voice frame's TN
    claims the codec.
  - Subsequent frames from other TNs are dropped (`drop_tn` counter).
  - The lock auto-releases after `TETRA_TN_LOCK_TIMEOUT` seconds
    (default 2.0) without renewal from the locked TN.
  - Set `TETRA_TN_LOCK_TIMEOUT=0` to disable demux.

Per-TN counters in `frame_stats` debug log let the user verify the
hypothesis. If multiple TS are carrying voice we'll see e.g.
`fed_per_tn=[tn1=120] drop_per_tn=[tn0=87 tn2=43]` — proving that
without the lock those TS-1 / TS-3 bursts would have been
interleaved into the codec.

#### Validation plan

User rebuilds image, runs with:

```sh
TETRA_DEBUG=1
TETRA_DEBUG_FILE=/opt/owrx-docker/debug/tetra-debug.log
TETRA_PCM_DUMP=/opt/owrx-docker/debug/tetra-pcm.raw
```

Listens for ~3 minutes covering both single-talker and
multi-talker activity, then runs:

```sh
# UDP packet size: should now be 1412 (was 1393) on voice frames
grep -oE 'len=14[01][0-9]' /opt/owrx-docker/debug/tetra-debug.log \
  | sort | uniq -c

# Per-TN frame distribution
grep frame_stats /opt/owrx-docker/debug/tetra-debug.log | tail -10

# TN lock acquisitions / releases
grep -E 'tn_lock|tn_unlock' /opt/owrx-docker/debug/tetra-debug.log
```

Decision tree:
  - Audio is now intelligible end-to-end: Bug 14 confirmed and fixed.
  - Audio still scrambled, but `drop_per_tn` shows multiple TNs:
    interleaving exists at SSI-within-TN level — need Bug 15
    (per-SSI demux, requires `-sq5bpf-2` migration or deeper
    upstream patch).
  - Audio still scrambled, `fed_per_tn` only shows one TN: the root
    cause is something else; revisit hypothesis.

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
| `ce38fc8` | `docs/tetra-fix-status.md` | Register Bug 13 abandonment + Bug 14 hypothesis |
| `a02be9b` | `build-tetra-packages.sh` | **Bug 14 patch**: upstream voice header gains TN field (32-byte fixed) |
| `ae524aa` | `tetra_decoder.py` | **Bug 14 wrapper**: parse 32-byte header + first-TN-wins demux |

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

Voice frame format on the wire (between patched tetra-rx and Python
decoder, UDP loopback):

```
[32-byte ASCII header, NUL-padded] + [1380-byte ACELP soft-bit block]
header text: "TRA:HH RX:HH TN:N\0"
UDP packet size: 1412 bytes
```

Legacy 13-byte format still parsed for backward compatibility
(returns `tn=-1`, no demux applied).

---

## Validation status

| Symptom | After fix | Confirmed |
|---|---|---|
| Audio stream at 10–15 kbps when idle | -> 0 kbps when idle | YES (`365ec98`) |
| `[tetra-debug]` lines invisible in docker logs | -> /tmp/tetra-debug.log | YES (`00c45c2`) |
| 0 ACELP frames extracted | -> ACELP extraction working | YES (in dump after `b47abd1`) |
| Panel freezes on first ACELP | -> panel keeps updating | YES (`52d8833`) |
| Periodic 300ms-intelligible voice | -> still mixed clear+scrambled | NO — see Bug 14 |
| TEA1 false positive on clear calls | -> ENCR-derived encryption | superseded |
| Mixed clear+scrambled audio in browser | -> still mixed offline (`.raw` confirms) | confirmed in pipeline |
| `TETRA_DROP_NO_CALL=1` filters encrypted | -> total silence (Bug 12) | filter unusable |
| Bug 13 statistical filter discriminates | -> all 7 metrics show single tight cluster | Option A dead |
| Bug 14 TN demux eliminates cross-talk | pending user validation | OPEN |

---

## Open work

### Bug 14 validation (next)

User to rebuild image with commits `a02be9b` + `ae524aa`, capture
~3 minutes of mixed-talker activity, and report:

  - Voice UDP packet size = 1412 (proving the C patch took effect).
  - `frame_stats` per-TN counters: are multiple TNs being seen?
  - Audio intelligibility end-to-end with default
    `TETRA_TN_LOCK_TIMEOUT=2.0`.

Outcomes:
  - **Fixed**: close Bug 14, move on to frontend / cosmetic items.
  - **Better but residual scramble**: open Bug 15 (per-SSI demux,
    requires migrating to `osmo-tetra-sq5bpf-2` which exposes
    `tms->ssi` in `struct tetra_mac_state`).
  - **Unchanged**: revisit hypothesis with new data.

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

Soft 30 s timeout to clear `call_setup` slots when no matching
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
| Codec call style | synchronous | async via reader thread |
| Silence padding | yes (legacy) | removed (`365ec98`) |
| Debug dumps | none | TETRA_DEBUG=1 -> file |
| Encryption source | Basicinfo low nibble | ENCR (short-form FUNC) |
| Cell-cap vs call-enc | merged | separated (`Cell Sec.` row) |
| Voice-stream demux | none | first-TN-wins (Bug 14) |
| Mixed audio on busy cell | same problem | should be fixed pending validation |

### aruznieto/TetraEar (session 6)

Pure-Python TETRA decoder (does NOT use osmo-tetra). Relevant bits:

| Aspect | TetraEar | Ours |
|---|---|---|
| Upstream | Reimplements lower MAC + protocol in Python | Uses osmo-tetra-sq5bpf binary |
| Encryption detection | MAC header 2-bit field + entropy fallback + call-meta override + SDS heuristic | Tried both — neither works on osmo-tetra passthrough format |
| Codec format | builds 690 × int16 soft-bits, header `0x6B21` | tetra-rx already emits this format (verified via log v3) |
| Auto-decrypt | yes (loads keys from `keys.txt`) | no key loaded; encrypted bursts pass through as garbage |
| Per-TS / per-SSI demux | full TETRA stack — implicit | per-TN (Bug 14); per-SSI deferred to Bug 15 if needed |

---

## Session log (high-level)

- **Session 1**: Bugs 1, 2, 3 identified and fixed; codec.diff swap (later reverted as Bug 4).
- **Session 2**: Bugs 5, 6, 7, 9, 10 fixed; reference plugin comparison; async codec pipeline; ENCC parsing introduced (later proven wrong).
- **Session 3**: Bug 8 (`-e` flag) fixed; PCM-dump diagnostic step added (Bug 11 isolation).
- **Session 4**: User confirmed offline `.raw` is identical to browser audio → artefact is in pipeline (not OWRX+ chain). Log v3 analysis: ENCC absent, ENCR present (79 events all `ENCR:0`). Bug 12 implemented (`f0ed4d2`).
- **Session 5**: Log v4 analysis: 0 ENCR fields in 403 MB capture, 100% frame drop, total silence — Bug 12 unusable. Upstream confirms no encryption flag in passthrough header beyond `DECR`. Bug 13 opened with 3 options.
- **Session 6**: TetraEar inspection. User approved Option A. Implemented zlib(full) and zlib(signs); both showed single-cluster ratios on 140 mixed-audio frames. Added bit-packed signs and flip rate; still single clusters.
- **Session 7**: Restricted metrics to the 432-position soft-bit window (skip magic + filler). bitsD ≈ 1.2 (no compression possible) and flipD ≈ 0.51 (random) prove ACELP soft-bits are statistically random regardless of clear/encrypted. **Option A formally abandoned.** User reported live observation: scrambled audio is intelligible but disordered, suggesting **interleaved concurrent calls** (Bug 14) rather than encrypted content. User approved Option B (upstream patch).
- **Session 8**: WebFetch on `osmo-tetra-sq5bpf` (note: NOT `-sq5bpf-2`, the older repo our build uses) confirmed the C patch design — `tcd->time.tn` is accessible at the voice sprintf, but `tms->ssi` is not (only in `-sq5bpf-2`). Implemented the upstream patch via build-script `sed`+`perl` (`a02be9b`) and the Python parser + first-TN-wins demux (`ae524aa`). Validation pending.
