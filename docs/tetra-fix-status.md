# TETRA Frequency Monitoring Branch — Status & Roadmap

Branch: `claude/tetra-frequency-monitoring-3IqM9`
Last updated: 2026-05-05 (session 10 — final)

Purpose: fix the TETRA decoder pipeline so the OpenWebRX+ panel matches
reality and unencrypted voice traffic plays back as audio.

---

## Final state

The audio pipeline is at its **best achievable state given upstream
constraints**: TN-only lock with sq5bpf-2 backbone (kept for the
diagnostic fields it exposes). Per-talker demux is **not possible** on
this network without patching the upper MAC to extract caller ISSI
from MAC-RESOURCE PDU parsing — see Bug 15 finalisation below for
the evidence trail.

What works:
  - All 10 backend bugs from sessions 1–3 fixed.
  - Frontend timeslot indicators (Bug 16, plugins repo) merged to main.
  - TN-lock (Bug 14) drops ~8% of cross-TN frames cleanly.
  - 64-byte voice header (Bug 15) emits TN/SSI/TSN/DECR/UM for
    diagnostics; backwards-compatible parsing for older formats.

What does not work and why:
  - Per-talker demux. SSI on this network is dominated by ALL-CALL
    broadcast (94–96%); usage_marker is always 0 in sq5bpf-2 voice
    bursts. Both fields fail to discriminate concurrent talkers.

User-visible result on this network: the same "partially intelligible /
partially scrambled" baseline as Bug 14 alone. Single-talker periods
decode cleanly; multi-talker broadcast PTT handoffs continue to
overlap because the upstream simply does not expose caller-side info
in the voice path.

---

## Bugs identified and current status

### Bug 1 — Cell capability vs. active-call encryption (FIXED — `7c740a1`)

CRYPT field in NETINFO1 is the cell-level security-class advertisement
(TEA capability of the BS), NOT the per-call encryption status.

Fixed: NETINFO1 / ENCINFO1 emit `cell_security_class` and `cell_tea`
fields (informational), with `encrypted=false`.

### Bug 2 — Reserved enc_mode flashing red (FIXED — `175b1e8`)

`enc > 0` matched reserved nibbles 4..15 producing red `ENC NONE`.
Fixed to `enc in (1,2,3)`. Later obsoleted by Bug 5.

### Bug 3 — Audio header offset hardcoded (FIXED — `175b1e8`, then SUPERSEDED)

`AUDIO_HEADER_SIZE = 20` was wrong. Fixed via dynamic regex offset.
Superseded by Bug 14 (32-byte fixed header) and Bug 15 (64-byte).

### Bug 4 — install-tetra-codec keystream patch corrupts audio (REVERTED — `b5e19e8`)

The newer codec.diff interprets every 5th input frame as keystream
and XORs it into the audio. Without TEA keys this corrupts every
5th frame → ~300 ms intelligible / 60 ms garbled loop. Reverted to
the legacy bundled diff. Decision relevant again in Bug 15: when
migrating to sq5bpf-2 we still pull the codec.diff from sq5bpf
(legacy) to avoid this regression.

### Bug 5 — Basicinfo low-nibble misread as encryption_mode (FIXED — `b47abd1`, then SUPERSEDED)

ENCC field doesn't exist on this build. Real per-call indicator is
`ENCR` on short-form FUNCs. Superseded by Bug 12 fix.

### Bug 6 — Audio frame regex never matched (FIXED — `b47abd1`)

Original regex required DECR field which legacy sq5bpf doesn't emit.
Unified `AUDIO_PATTERN` matches all formats. Bug 14 / Bug 15 added
fixed-header parsing on top.

### Bug 7 — Codec deadlocks the main loop (FIXED — `52d8833`)

Synchronous `decode()` blocked on read. Refactored to async pipeline
with reader thread.

### Bug 8 — `-e` flag corrupts audio (FIXED — `fec8ff9`)

`tetra-rx -e` puts it in encrypted-passthrough mode, emitting
pre-deinterleave bytes. Drop the flag.

### Bug 9 — silence padding inflated stream rate (FIXED — `365ec98`)

Removed `silence_20ms` continuous output. Stream now silent when no
voice — matches DMR/NXDN.

### Bug 10 — Debug output swallowed by csdr_module_tetra (FIXED — `00c45c2`)

`debug_dump` writes to `/tmp/tetra-debug.log` instead of stderr.

### Bug 11 — Codec output mixed clear+scrambled (REFRAMED — see Bug 14/15)

Initial hypothesis (encrypted bursts decoded as ACELP garbage) drove
Bugs 12/13 work — both failed. Session 7 user observation reframed
this as **multi-talker interleaving** → Bug 14, then Bug 15.

### Bug 12 — Active-clear-call filter (ATTEMPTED, FAILED — `f0ed4d2`)

Gate codec on per-SSI clear-call state from short-form FUNC ENCR.
Result: total silence (D-SETUP too sparse on this build).
Filter present but defaults off (`TETRA_DROP_NO_CALL=0`).

### Bug 13 — Statistical per-frame encryption detection (ABANDONED)

Tried 7 metrics (zlib full/sign/bits/flip + signD/bitsD/flipD) on
287 mixed-audio frames. All produced single tight clusters because
ACELP soft-bits after Viterbi are statistically random regardless
of clear/encrypted. **Option A is dead.** Metric infrastructure
left as debug surface (`TETRA_ENC_RATIO_THRESHOLD=0` = off).

### Bug 14 — Multi-TS interleaving (PARTIALLY FIXED — TN-lock kept as final design)

User narrative: "scrambled audio is the same clean voice played out
of order, partially intelligible." Confirmed multi-source interleaving,
not encryption.

#### Implementation

  - `a02be9b` — patch sq5bpf's `tetra_lower_mac.c` to emit a 32-byte
    voice header with `TN:N` (timeslot 0..3).
  - `ae524aa` — Python parser + first-TN-wins lock with 2 s silence
    timeout.

#### Session-8 calibration result

| Metric | Value |
|---|---|
| UDP packet size | 1412 bytes (was 1393) — patch active ✓ |
| TN distribution | TN=2 dominant 90%, TN=1 8%, TN=3 2% |
| Cross-TN drops | 505 / 6497 (≈8%) — lock working |
| Audio quality | still mixed (residual scramble) |

Conclusion: TN-lock works as designed but residual scramble means
multiple SSIs share the same TN via PTT handoff. Investigated in
Bug 15 below; ultimately TN-lock remains the best stable design.

### Bug 14b — Codec reset on TN unlock (TRIED, DISABLED)

Hypothesis: ACELP codec state leaks between speakers on the same TN.
Implementation: kill+restart cdecoder/sdecoder on every TN unlock.

Session-9 calibration result: **made audio worse** (parts that were
intelligible became scrambled too). 400 codec resets in 3 h
introduced glitches without improving inter-speaker discrimination.
With `TETRA_RESET_CODEC_ON_UNLOCK=0` audio returned to baseline.

Decision: kept in code, defaults **off**. Marked dormant.

### Bug 15 — Per-call demultiplexing via osmo-tetra-sq5bpf-2 migration (CALIBRATED — TN-lock final)

This is the longest investigation in the branch. Final outcome:
**migrate to sq5bpf-2 for the diagnostic fields it exposes, but lock
on TN only — UM and SSI proved unusable on this network**.

#### Why migrate to sq5bpf-2

`osmo-tetra-sq5bpf` (legacy, what we used through Bug 14) does NOT
have `tms->ssi`, `tms->tsn`, or `tms->usage_marker` in its
`struct tetra_mac_state`. Only the `-sq5bpf-2` fork exposes them.

#### Build strategy

We can't naively switch to sq5bpf-2 because its bundled
`etsi_codec-patches/codec.diff` is the install-tetra-codec one,
which regresses to Bug 4 (keystream-block corruption every 5th
frame when no TEA key is loaded).

Build now clones **two** repos:

| Repo | Purpose |
|---|---|
| `osmo-tetra-sq5bpf-2` (`/tmp/osmo-tetra-2`) | tetra-rx (with our Bug 15 patch) |
| `osmo-tetra-sq5bpf` (`/tmp/osmo-tetra-codec-src`) | codec.diff only (legacy, no keystream) |

cdecoder/sdecoder built using the legacy diff; tetra-rx built from
sq5bpf-2 with our patch.

`README.md` of sq5bpf-2 explicitly warns: *"the new osmo-tetra
versions will coredump with a lot of real world traffic, so this
fork will do it too"* and *"Actually you're lucky if it even
compiles"*. We accept this risk in exchange for the additional
diagnostic fields.

#### Bug 15 patch (round 2 — final, with UM)

In sq5bpf-2's `tp_sap_udata_ind`, case `TPSAP_T_SCH_F`, the voice
sprintf becomes:

```c
unsigned char tmpstr[1380+64];
...
memset(tmpstr, 0, 64);
snprintf((char *)tmpstr, 64, "TRA:%2.2x RX:%2.2x DECR:%i TN:%u SSI:%u TSN:%u UM:%i",
    tms->cur_burst.is_traffic, tetra_hack_rxid, decrypted,
    tcd->time.tn, tms->ssi, tms->tsn, tms->usage_marker);
memcpy(tmpstr+64, block, sizeof(block));
sendto(..., sizeof(block)+64, ...);
```

UDP voice packet becomes 1444 bytes. Three grep assertions hard-fail
the build if the rewrites silently miss.

#### Session-9/10 calibration result

| Field | Observation | Verdict |
|---|---|---|
| `UM` (usage_marker) | 2320/2320 frames have `UM:0` | unusable |
| `SSI` | 96% have `SSI=0xFFFFFF` (TETRA ALL-CALL broadcast); 4% have specific SSIs in 2–5 s bursts | unusable as primary lock |
| `TN` | TN=2 dominant 90% + others — same as session 8 | usable |
| `DECR` | always 0 (no TEA keys) | informational only |

Critically: the unified `UM > SSI > TN` lock (commit `4069fb4`) made
audio **worse** than TN-only. Short SSI sessions (2–5 s) caused
constant lock churn; cross-key drops fragmented ACELP codec
continuity, producing fully scrambled output where TN-only had been
"partially intelligible".

Reason SSI fails: `tms->ssi` exposes the **destination** address from
MAC RESOURCE PDU. For broadcast group calls, destination is
`0xFFFFFF` (ALL-CALL) regardless of caller. Reason UM fails: the
upper MAC of sq5bpf-2 simply doesn't populate `tms->usage_marker`
for these voice bursts on this network.

#### Final design (commit `bd7eccc`)

  - Lock kind: TN only.
  - SSI / UM / TSN / DECR fields **kept in the wire format** for
    diagnostics — `parse_audio_from_udp` extracts them, `frame_stats`
    logs `fed_per_um/ssi/tn` and `drop_per_um/ssi/tn`. They just
    don't drive the lock decision.
  - `TETRA_LOCK_TIMEOUT` (default 2.0) governs the TN-lock; aliases
    `TETRA_TN_LOCK_TIMEOUT` and `TETRA_SSI_LOCK_TIMEOUT` retained
    for backward-compat.
  - Bug 14b codec reset stays opt-in (`TETRA_RESET_CODEC_ON_UNLOCK=0`
    by default).
  - Build infra (sq5bpf-2 + 64-byte header) preserved — if a future
    upstream fix populates UM, the field is already on the wire and
    we can re-enable per-call lock by reverting the one-liner in
    `_frame_lock_key`.

Per-talker demux on this network would now require a deeper patch
to the **upper MAC** in sq5bpf-2 to extract caller ISSI from
MAC-RESOURCE PDU parsing and copy it to a new field on
`struct tetra_mac_state`. The author of sq5bpf-2 explicitly warns
the codebase coredumps on real-world traffic, so further invasive
patching is out of scope without strong evidence the caller field
even survives ALL-CALL broadcast framing.

### Bug 16 — Frontend timeslot lights & call routing (FIXED — plugins repo)

Discovered while comparing with `trollminer/OpenWebRX-Tetra-Plugin`:

  1. `burst` handler read `data.slot` (singular, never emitted by
     backend) which defaulted to 0 → only TS1 ever lit.
  2. `call_setup`/`call_connect`/`tx_grant` had the same bug.
  3. `TetraMetaSlot.update` read `data.issi` / `data.gssi` — backend
     emits `ssi` / `ssi2` (TETMON convention).

Fixed in `xnetinho/openwebrxplus-plugins`:
  - `0a75caf` v1.5 — first attempt (based on stale v1.3 base).
  - `3d215bd` v1.6 — merged with main's v1.4 features.
  - `8b035b0` — tetra.css aligned with main v1.4.
  - `464fafd` — PR #2 merged to main.

Confirmed working session 9. Per-slot ISSI/GSSI population (Bug 17)
deferred — backend would need to tag calls with TN to do that
correctly.

---

## What was done in this branch (commits in chronological order)

### Docker-builder repo

| SHA | File | Summary |
|---|---|---|
| `7c740a1` | `tetra_decoder.py` | NETINFO1/ENCINFO1 emit cell capability fields |
| `c38183f` | plugins (mirror) | Mirror v1.3 frontend |
| `175b1e8` | `tetra_decoder.py` | enc in (1,2,3); dynamic header offset |
| `69276de` | plugins (mirror) | v1.4 split Cell Sec / Encryption |
| `cfd357e` | docs | Initial status doc |
| `365ec98` | `tetra_decoder.py` | Remove silence_20ms padding |
| `00c45c2` | `tetra_decoder.py` | Debug dump to file |
| `b47abd1` | `tetra_decoder.py` | Unified AUDIO_PATTERN; ENCC parsing (later wrong) |
| `52d8833` | `tetra_decoder.py` | Async codec pipeline |
| `b5e19e8` | `build-tetra-packages.sh` | Revert codec.diff to bundled |
| `fec8ff9` | `tetra_decoder.py` | Drop `-e` flag |
| `6ff2e36` | `tetra_decoder.py` | TETRA_PCM_DUMP env |
| `f0ed4d2` | `tetra_decoder.py` | Bug 12 active-clear-call filter (failed) |
| `7b464a5` | docs | Register Bug 11 outcome + DECR findings |
| `08e96c3` | docs | Register TetraEar findings + Option A approval |
| `6ebdd1a` | `tetra_decoder.py` | Bug 13 attempt 1: zlib full frame |
| `97d9d34` | `tetra_decoder.py` | Bug 13 attempt 2: zlib sign bytes |
| `315e15d` | `tetra_decoder.py` | Bug 13 attempt 3: 4 metrics |
| `6470d93` | `tetra_decoder.py` | Bug 13 attempt 4: data-window — Option A dead |
| `ce38fc8` | docs | Register Bug 13 abandonment + Bug 14 hypothesis |
| `a02be9b` | `build-tetra-packages.sh` | **Bug 14**: TN field in 32-byte voice header |
| `ae524aa` | `tetra_decoder.py` | **Bug 14 wrapper**: TN demux |
| `28e9e66` | docs | Register Bug 14 implementation |
| `64bbb33` | `tetra_decoder.py` | **Bug 14b**: codec reset on unlock (later disabled) |
| `49ded77` | `build-tetra-packages.sh` | **Bug 15**: migrate to sq5bpf-2 + 64-byte voice header |
| `dbfdf29` | `tetra_decoder.py` | **Bug 15 wrapper**: SSI-lock primary (made audio worse) |
| `7e9fe93` | docs | Register Bug 15 round 1 |
| `3b3f7f6` | `build-tetra-packages.sh` | Bug 15 round 2: add UM (usage_marker) to voice header |
| `4069fb4` | `tetra_decoder.py` | Bug 15 round 2: unified UM>SSI>TN lock (also made audio worse) |
| `bd7eccc` | `tetra_decoder.py` | **Bug 15 final**: revert lock to TN-only; UM/SSI become diagnostic-only |

### Plugins repo

| SHA | File | Summary |
|---|---|---|
| `0a75caf` | `tetra.js` | v1.5 Bug 16: data.timeslots iteration + slot routing |
| `3d215bd` | `tetra.js` | v1.6: merge main v1.4 + v1.5 fixes |
| `8b035b0` | `tetra.css` | v1.4: align with main |
| `464fafd` | merge | PR #2 merged to main |

---

## Voice frame wire format (current, after Bug 15 final)

```
[64-byte ASCII header, NUL-padded] + [1380-byte ACELP soft-bit block]
header text: "TRA:HH RX:HH DECR:N TN:N SSI:N TSN:N UM:N\0"
UDP packet size: 1444 bytes
```

Backward-compat: 32-byte (Bug 14) and variable-length legacy headers
still parse, with `ssi=-1` / `tsn=-1` / `decr=-1` / `um=-1` for
missing fields.

Active lock decision uses **TN only**. SSI/UM/TSN/DECR are parsed
into `voice_meta` and surfaced in `frame_stats` debug output for
operators to observe network behaviour.

---

## Validation status

| Symptom | After fix | Confirmed |
|---|---|---|
| Audio stream at 10–15 kbps when idle | -> 0 kbps when idle | YES (`365ec98`) |
| `[tetra-debug]` lines invisible in docker logs | -> /tmp/tetra-debug.log | YES (`00c45c2`) |
| 0 ACELP frames extracted | -> ACELP extraction working | YES |
| Panel freezes on first ACELP | -> panel keeps updating | YES (`52d8833`) |
| TS1 only lights on the panel | -> all 4 TS reflect data.timeslots | YES (`3d215bd`) session-9 |
| TEA1 false positive on clear calls | -> ENCR-derived encryption | superseded |
| Mixed clear+scrambled audio | -> reframed as multi-talker interleaving | confirmed |
| `TETRA_DROP_NO_CALL=1` filters encrypted | -> total silence (Bug 12) | filter unusable |
| Bug 13 statistical filter discriminates | -> all 7 metrics single cluster | Option A dead |
| Bug 14 TN demux eliminates cross-TN cross-talk | -> ~8% cross-TN drops | confirmed session 8 |
| Bug 14b codec reset improves audio | -> introduces glitches, no improvement | disabled session 9 |
| Bug 15 round 1 SSI-lock improves audio | -> 96% SSI=ALL-CALL, dominant lock churn | reverted session 9 |
| Bug 15 round 2 UM-lock improves audio | -> UM=0 always, audio worse than Bug 14 | reverted session 10 |
| **Bug 15 final TN-only lock + sq5bpf-2** | -> back to Bug 14 baseline + better diagnostics | confirmed session 10 |

---

## Open work / deferred

### Bug 17 — Per-slot ISSI/GSSI population (deferred)

With Bug 15 the backend now has SSI per voice frame. The frontend
could correlate calls to slots via signaling FUNCs and populate
per-slot ISSI/GSSI. Cosmetic. Defer until per-call demux becomes
viable (would require upper-MAC patch).

### Two decoder instances (config side, not our bug)

User confirmed `ps auxf` shows two complete `tetra_decoder.py` chains
running concurrently, even after a clean container restart. Structural
to the OWRX+ config (likely two TETRA profiles or two SDRs). Each has
its own UDP port and stdout pipe; they don't corrupt each other's
audio — only the shared debug log file gets intercalated. Not a fix
needed in our code.

### Frontend SCK-locked badge state, STATUS code dictionary, TS slot stale, frequency offset

Cosmetic / non-blocking. Carry over from earlier sessions.

### Caller-ISSI extraction (long shot)

Patching the upper MAC in sq5bpf-2 to parse MAC-RESOURCE PDU caller
side and copy it to `struct tetra_mac_state` would enable per-talker
demux. Out of scope absent strong evidence the broadcast traffic on
this network actually carries a per-burst caller field. Coredump
warnings on the upstream make this risky.

---

## Reference comparisons

### mbbrzoza/OpenWebRX-Tetra-Plugin

Same `osmo-tetra-sq5bpf` (legacy) upstream + ETSI codec. Has Bug 7
(deadlock), Bug 8 (-e flag), Bug 9 (silence padding), Bug 16
(data.slot bug). Same audio interleaving problem we've now isolated.
Doesn't address it.

### aruznieto/TetraEar

Pure-Python TETRA stack. Encryption detection inspired Bug 13
(failed against osmo-tetra's post-Viterbi bit stream). Confirms
cdecoder input format we send is correct.

### trollminer/OpenWebRX-Tetra-Plugin (session 8)

Uses sq5bpf legacy + same 4-TS-light bug we fixed in Bug 16. No
audio (their codec deadlocks like our pre-Bug-7).

---

## Session log (high-level)

- **Session 1–3**: Bugs 1–10. Async pipeline, debug-to-file, -e flag
  fix, codec.diff revert, ENCR/ENCC convergence.
- **Session 4–5**: Bug 11 isolation via PCM dump. Bug 12 attempted
  (signaling-gate, fails — short-form FUNCs too sparse). Bug 13 opened.
- **Session 6–7**: Bug 13 — 7 statistical metrics tried, all fail.
  Option A formally abandoned. User narrative reframes Bug 11 as
  multi-talker interleaving. Bug 14 opened.
- **Session 8**: Bug 14 implemented (sq5bpf + TN-lock). 8% cross-TN
  drops confirmed in calibration but residual audio scramble
  remains, indicating intra-TN multi-SSI is the dominant cause.
- **Session 9**: Bug 14b codec-reset experiment (failed, disabled).
  Frontend Bug 16 fixed in plugins repo. Bug 15 round 1 implemented:
  migrate tetra-rx to sq5bpf-2, 64-byte voice header with
  TN/SSI/TSN/DECR, SSI-lock primary. Calibration: 96% of frames
  have SSI=0xFFFFFF (TETRA ALL-CALL) → SSI-lock churns and audio
  worsens.
- **Session 10**: Bug 15 round 2 added UM (usage_marker) — calibration
  shows UM=0 always on this network/build. Unified UM>SSI>TN lock
  also made audio worse (lock churn fragments codec). Reverted to
  TN-only lock as final design. UM/SSI/TSN/DECR remain on the wire
  for diagnostics. Build infra (sq5bpf-2 + 64-byte header) preserved.
  Net result: same audio quality as Bug 14 alone ("partially
  intelligible / partially scrambled"); per-talker demux is not
  achievable on this network without an upper-MAC patch out of
  scope of this branch.
