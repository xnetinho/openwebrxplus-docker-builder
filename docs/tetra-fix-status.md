# TETRA Frequency Monitoring Branch — Status & Roadmap

Branch: `claude/tetra-frequency-monitoring-3IqM9`
Last updated: 2026-05-04 (session 9)

Purpose: fix the TETRA decoder pipeline so the OpenWebRX+ panel matches
reality and unencrypted voice traffic plays back as audio.

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

### Bug 11 — Codec output mixed clear+scrambled (REFRAMED — see Bug 14)

Initial hypothesis (encrypted bursts decoded as ACELP garbage) drove
Bugs 12/13 work — both failed. Session 7 user observation reframed
this as **multi-talker interleaving** → Bug 14.

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

### Bug 14 — Multi-TS interleaving (PARTIALLY WORKED — superseded by Bug 15)

User narrative: "scrambled audio is the same clean voice played out
of order, partially intelligible." Confirmed multi-source
interleaving, not encryption.

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
**multiple SSIs share the same TN** via PTT handoff. Need per-SSI
demux (Bug 15).

### Bug 14b — Codec reset on TN unlock (TRIED, DISABLED)

Hypothesis: ACELP codec state leaks between speakers on the same TN.
Implementation: kill+restart cdecoder/sdecoder on every TN unlock.

Session-9 calibration result: **made audio worse** (parts that were
intelligible became scrambled too). 400 codec resets in 3 h
introduced glitches without improving inter-speaker discrimination.
With `TETRA_RESET_CODEC_ON_UNLOCK=0` audio returned to the
"partially intelligible / partially scrambled" baseline.

Decision: kept in code, defaults **off**. Marked dormant.

### Bug 15 — Per-SSI demultiplexing via osmo-tetra-sq5bpf-2 migration (IMPLEMENTED — pending validation)

#### Why migrate

`osmo-tetra-sq5bpf` (legacy, what we used through Bug 14) does NOT
have `tms->ssi` in `struct tetra_mac_state`. Only the `-sq5bpf-2`
fork exposes the per-call SSI/TSN. Per-talker demux is impossible
without those fields.

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

#### Bug 15 patch

In sq5bpf-2's `tp_sap_udata_ind`, case `TPSAP_T_SCH_F`, replace the
voice sprintf:

```c
- unsigned char tmpstr[1380+20];
+ unsigned char tmpstr[1380+64];
...
- sprintf(tmpstr,"TRA:%2.2x RX:%2.2x DECR:%i\0",
-     tms->cur_burst.is_traffic, tetra_hack_rxid, decrypted);
- memcpy(tmpstr+20, block, sizeof(block));
- sendto(..., sizeof(block)+20, ...);
+ memset(tmpstr, 0, 64);
+ snprintf((char *)tmpstr, 64, "TRA:%2.2x RX:%2.2x DECR:%i TN:%u SSI:%u TSN:%u",
+     tms->cur_burst.is_traffic, tetra_hack_rxid, decrypted,
+     tcd->time.tn, tms->ssi, tms->tsn);
+ memcpy(tmpstr+64, block, sizeof(block));
+ sendto(..., sizeof(block)+64, ...);
```

UDP voice packet becomes 1444 bytes (was 1400 in sq5bpf-2 stock,
1412 in our Bug 14 patched sq5bpf, 1393 in legacy sq5bpf).

Three grep assertions hard-fail the build if the rewrites silently
miss.

#### Python wrapper changes

`_parse_voice_header()` extracts `tn`, `ssi`, `tsn`, `decr` from the
ASCII text portion of the 64-byte header.

`parse_audio_from_udp()` tries three formats in order:
  1. 64-byte (Bug 15) — has SSI for per-call demux.
  2. 32-byte (Bug 14) — has TN only.
  3. Variable legacy header.

Returns `(meta_dict, acelp_bytes)`.

Main loop applies layered locks:
  - **SSI-lock (primary)**: first non-zero SSI claims codec; other
    SSIs dropped until silence > `TETRA_SSI_LOCK_TIMEOUT` (default
    2.0 s).
  - **TN-lock (fallback)**: when SSI absent (legacy binary), behaves
    like Bug 14.
  - **Bug 14b codec reset**: now opt-in (default off).

`frame_stats` debug log adds `fed_per_ssi`, `drop_per_ssi`,
`locked_ssi` so the user can validate. Expected pattern with Bug 15
working on busy talkgroup:
```
fed_per_ssi=[ssi14002849=N1] drop_per_ssi=[ssi14003001=N2 ssi14003015=N3]
```
Single SSI fed, others dropped during the lock.

#### Validation plan

Rebuild image, listen for ~5 minutes covering multi-talker activity:

```sh
# UDP packet size: should be 1444 (proves Bug 15 C patch active)
grep -oE 'len=14[34][0-9]' /opt/owrx-docker/debug/tetra-debug.log \
  | sort | uniq -c

# Per-SSI distribution
grep frame_stats /opt/owrx-docker/debug/tetra-debug.log | tail -10

# SSI lock churn
grep -E 'ssi_lock|ssi_unlock' /opt/owrx-docker/debug/tetra-debug.log
```

Decision tree:
  - **Audio fully intelligible**: Bug 15 confirmed, close.
  - **Audio improved but still glitchy on speaker handoff**: SSI
    detection works but codec needs warm-up between speakers.
    Selectively re-enable Bug 14b reset OR add a small pre-roll.
  - **Audio still mixed at same level as Bug 14**: SSI field is
    inconsistent or 0 in this build → investigate sq5bpf-2 SSI
    population timing.

### Bug 16 — Frontend timeslot lights & call routing (FIXED on plugins repo)

Discovered while comparing with `trollminer/OpenWebRX-Tetra-Plugin`:

  1. `burst` handler read `data.slot` (singular, never emitted by
     backend) which defaulted to 0 → only TS1 ever lit.
  2. `call_setup`/`call_connect`/`tx_grant` had the same bug.
  3. `TetraMetaSlot.update` read `data.issi` / `data.gssi` — backend
     emits `ssi` / `ssi2` (TETMON convention).

Fixed in `xnetinho/openwebrxplus-plugins` branch:
  - `0a75caf` v1.5 — first attempt (based on stale v1.3 base).
  - `3d215bd` v1.6 — merged with main's v1.4 features (Cell Sec
    separate row, idle state, statusNames, _fmtStatus, _fmtSds).
  - `8b035b0` — tetra.css aligned with main v1.4.
  - PR merged to main as `464fafd`.

User confirmation (session 9): TS1-4 now light correctly. ISSI/GSSI
per slot still empty because the backend doesn't tag calls with TN
(deferred — needs Bug 17 if desired).

---

## What was done in this branch (commits in chronological order)

| SHA | File | Summary |
|---|---|---|
| `7c740a1` | `tetra_decoder.py` | NETINFO1/ENCINFO1 emit cell capability fields |
| `c38183f` | plugins | Mirror v1.3 frontend |
| `175b1e8` | `tetra_decoder.py` | enc in (1,2,3); dynamic header offset |
| `69276de` | plugins | v1.4 split Cell Sec / Encryption |
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
| `6470d93` | `tetra_decoder.py` | Bug 13 attempt 4: data-window metrics — Option A dead |
| `ce38fc8` | docs | Register Bug 13 abandonment + Bug 14 hypothesis |
| `a02be9b` | `build-tetra-packages.sh` | **Bug 14**: TN field in 32-byte voice header |
| `ae524aa` | `tetra_decoder.py` | **Bug 14 wrapper**: TN demux |
| `28e9e66` | docs | Register Bug 14 implementation |
| `64bbb33` | `tetra_decoder.py` | **Bug 14b**: codec reset on unlock (later disabled) |
| `49ded77` | `build-tetra-packages.sh` | **Bug 15**: migrate to sq5bpf-2 + 64-byte voice header (TN/SSI/TSN/DECR) |
| `dbfdf29` | `tetra_decoder.py` | **Bug 15 wrapper**: SSI-lock demux + Bug 14b default off |

Plugins repo (separate):
| SHA | File | Summary |
|---|---|---|
| `0a75caf` | `tetra.js` | v1.5 Bug 16: data.timeslots iteration + slot routing |
| `3d215bd` | `tetra.js` | v1.6: merge main v1.4 + v1.5 fixes |
| `8b035b0` | `tetra.css` | v1.4: align with main |
| `464fafd` | merge | PR #2 merged to main |

---

## Voice frame wire format (current, after Bug 15)

```
[64-byte ASCII header, NUL-padded] + [1380-byte ACELP soft-bit block]
header text: "TRA:HH RX:HH DECR:N TN:N SSI:N TSN:N\0"
UDP packet size: 1444 bytes
```

Backward-compat: 32-byte (Bug 14) and variable-length legacy headers
still parse, with `ssi=-1` / `tsn=-1` / `decr=-1` for missing fields.

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
| Mixed clear+scrambled audio | -> reframed as multi-talker interleaving (Bug 14/15) | confirmed |
| `TETRA_DROP_NO_CALL=1` filters encrypted | -> total silence (Bug 12) | filter unusable |
| Bug 13 statistical filter discriminates | -> all 7 metrics single cluster | Option A dead |
| Bug 14 TN demux eliminates cross-talk | -> works for cross-TN (~8% drop) | confirmed session 8 |
| Bug 14b codec reset improves audio | -> introduces glitches, no improvement | disabled session 9 |
| Bug 15 SSI demux eliminates intra-TN cross-talk | pending user validation | OPEN |

---

## Open work

### Bug 15 validation (next)

User rebuilds image with commits `49ded77` + `dbfdf29`, listens
~5 minutes covering multi-talker activity, reports:
  - Voice UDP packet size = 1444 (proving Bug 15 C patch active)
  - `frame_stats` `fed_per_ssi` shows multiple SSIs being seen
  - `drop_per_ssi` non-zero during multi-talker bursts
  - Audio intelligibility end-to-end with default
    `TETRA_SSI_LOCK_TIMEOUT=2.0`

### Two decoder instances (config side, not our bug)

User confirmed `ps auxf` shows two complete `tetra_decoder.py` +
`tetra_demod.py` + `tetra-rx` chains running concurrently, even
after a clean container restart. This is structural to the user's
OWRX+ config (likely two TETRA profiles or two SDRs). Each has its
own UDP port and its own stdout pipe to OWRX+, so they don't
corrupt each other's audio — only the shared debug log file gets
intercalated. Not a fix needed in our code; user can verify config.

### Frontend per-slot ISSI/GSSI population (Bug 17 — deferred)

With Bug 15 the backend now has SSI per voice frame. We could
correlate calls to slots via signaling FUNCs and populate per-slot
ISSI/GSSI. Cosmetic. Defer until Bug 15 audio is confirmed.

### Frontend SCK-locked badge state, STATUS code dictionary, TS slot stale, frequency offset

Cosmetic / non-blocking. Carry over from earlier sessions.

---

## Reference comparisons

### mbbrzoza/OpenWebRX-Tetra-Plugin

Same `osmo-tetra-sq5bpf` (legacy) upstream + ETSI codec. Has Bug 7
(deadlock), Bug 8 (-e flag inconsistency before our `fec8ff9`), Bug
9 (silence padding), Bug 16 (data.slot bug). Same audio interleaving
problem we've now isolated. Doesn't address it.

### aruznieto/TetraEar

Pure-Python TETRA stack. Encryption detection inspired Bug 13
(failed against osmo-tetra's post-Viterbi bit stream). Confirms
cdecoder input format we send is correct.

### trollminer/OpenWebRX-Tetra-Plugin (session 8)

Uses sq5bpf legacy + same 4-TS-light bug we fixed in Bug 16. No
audio (their codec deadlocks like our pre-Bug-7). Useful comparison
for confirming the `data.timeslots` payload shape.

---

## Session log (high-level)

- **Session 1–3**: Bugs 1–10. Async pipeline, debug-to-file,
  -e flag fix, codec.diff revert, ENCR/ENCC convergence.
- **Session 4–5**: Bug 11 isolation via PCM dump. Bug 12 attempted
  (signaling-gate, fails — short-form FUNCs too sparse). Bug 13
  opened.
- **Session 6–7**: Bug 13 — 7 statistical metrics tried, all fail.
  Option A formally abandoned. User narrative reframes Bug 11 as
  multi-talker interleaving. Bug 14 opened.
- **Session 8**: Bug 14 implemented (sq5bpf + TN-lock). 8% cross-TN
  drops confirmed in calibration but residual audio scramble
  remains, indicating intra-TN multi-SSI is the dominant cause.
- **Session 9**: Bug 14b codec-reset experiment (failed, disabled).
  Frontend Bug 16 found via trollminer comparison and fixed in
  plugins repo (PR merged to main as `464fafd`). Bug 15 implemented:
  migrate tetra-rx to sq5bpf-2 (legacy codec.diff retained from
  sq5bpf), 64-byte voice header with TN/SSI/TSN/DECR, SSI-lock
  primary gate with TN-lock fallback. Validation pending.
