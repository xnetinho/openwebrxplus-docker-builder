# CLAUDE.md — Development Guide

This file guides AI assistants and future developers working on this repository.

## Repository Purpose

Fork of [0xAF/openwebrxplus-docker-builder](https://github.com/0xAF/openwebrxplus-docker-builder) that adds TETRA digital radio decoding to OpenWebRX+. Produces three Docker images in a build chain:

```
slechev/openwebrxplus  →  slechev/openwebrxplus-softmbe  →  xnetinho/openwebrxplus-tetra
     (upstream)                  (upstream)                        (this repo)
```

## Repository Structure

```
run                              # Main CLI: build, run, dev commands
buildfiles/
  Dockerfile                     # Main image (heavy multi-stage, upstream)
  Dockerfile-softmbe             # SoftMBE image (lightweight, upstream base)
  Dockerfile-tetra               # TETRA image (lightweight, softmbe base)
  common.sh                      # Shared build utilities and cache helpers
  build-softmbe-packages.sh      # Compiles mbelib + codecserver-softmbe
  build-tetra-packages.sh        # Compiles osmo-tetra + ETSI ACELP codec
  install-softmbe-packages.sh    # Installs softmbe into runtime image
  install-tetra-packages.sh      # Installs TETRA into runtime image
  files/
    patch_tetra.py               # Patches OpenWebRX+ at Docker build time
    csdr_chain_tetra.py          # CSDR chain: IQ → PCM (installs to csdr/chain/)
    csdr_module_tetra.py         # Subprocess wrapper for tetra_decoder.py
    tetra_decoder.py             # Full pipeline: GNURadio → tetra-rx → ACELP → PCM
htdocs/
  plugins/receiver/tetra/
    tetra.js                     # Frontend plugin (published to GitHub Pages)
    tetra.css                    # Panel styles
.github/workflows/
  pages.yml                      # Publishes htdocs/ to GitHub Pages automatically
```

## Build Chain

### Full build (heavy — compiles OpenWebRX+ from source)
```bash
./run build
# Chains automatically: main → softmbe → tetra
```

### Lightweight builds (use pre-existing Docker Hub images as base)
```bash
./run build-softmbe   # FROM slechev/openwebrxplus → chains into build-tetra
./run build-tetra     # FROM slechev/openwebrxplus-softmbe (standalone)
```

`build-softmbe` and `build-tetra` do NOT require the APT cache server. They skip
`SOURCES_SCRIPTS_FINGERPRINT`, `OWRX_REPO_COMMIT`, and `FINAL_CACHE_BUSTER` build args.

## TETRA Integration — How It Works

### Backend (Docker image)

1. **`build-tetra-packages.sh`** (builder stage):
   - Installs `libosmocore-dev` from apt (no source compilation needed)
   - Clones [osmo-tetra-sq5bpf](https://github.com/sq5bpf/osmo-tetra-sq5bpf)
   - Downloads ETSI ACELP codec zip from `etsi_codec-patches/` directory
   - Applies `codec.diff` from the repo, compiles `cdecoder` + `sdecoder`
   - Compiles `tetra-rx` via `make` in `src/`

2. **`install-tetra-packages.sh`** (runtime stage):
   - Installs binaries to `/opt/openwebrx-tetra/`
   - Installs `csdr_chain_tetra.py` → `csdr/chain/tetra.py`
   - Installs `csdr_module_tetra.py` → `csdr/modules/tetra.py`
   - Runs `patch_tetra.py` to patch OpenWebRX+ Python files

3. **`patch_tetra.py`** — patches three files at **build time** (not runtime):
   - `modes.py` — inserts `AnalogMode("tetra", ...)` before the `nxdn` entry
   - `feature.py` — registers `tetra_decoder` feature with binary presence check
   - `dsp.py` — inserts `elif demod == "tetra"` before the `nxdn` block
   - Each patch has a fallback strategy; build **fails loudly** if patching is impossible

4. **`tetra_decoder.py`** — runs as subprocess, full pipeline:
   - stdin: complex float32 IQ at 36 kS/s
   - stdout: 16-bit signed LE PCM at 8 kHz
   - stderr: JSON metadata lines (`protocol: "TETRA"`, `type: netinfo|burst|call_setup|...`)

### Frontend (GitHub Pages)

Hosted at: `https://xnetinho.github.io/openwebrxplus-docker-builder/plugins/receiver/tetra/tetra.js`

Follows [0xAF/openwebrxplus-plugins](https://github.com/0xAF/openwebrxplus-plugins) conventions:
- **ES5 JavaScript** — `var`, `function`, prototype chain. No classes, no arrow functions.
- **Tab indentation**
- `Plugins.tetra = Plugins.tetra || {};` namespace
- `init()` returns `true`/`false`
- Depends on `utils >= 0.1`

The plugin:
1. Waits for `Plugins.utils.on_ready()`
2. Injects panel HTML dynamically (no changes to `index.html` needed)
3. Registers `TetraMetaPanel` in `MetaPanel.types['tetra']`
4. Calls `$('#openwebrx-panel-metadata-tetra').metaPanel()` to initialise
5. Displays 4 TDMA timeslots styled like the DMR panel

**MetaPanel dispatch**: OpenWebRX+ calls `update(data)` on all meta panels. `TetraMetaPanel`
accepts data where `data.protocol === 'TETRA'` (set by `csdr_module_tetra.py`).

## Updating OpenWebRX+ Version

When the upstream `slechev/openwebrxplus-softmbe` base image updates:

1. Run `./run build-tetra` — the `--pull` flag fetches the latest base
2. If the build fails at `patch_tetra.py`, the anchors changed. Check:
   - `modes.py`: still has `AnalogMode("nxdn"...)`?
   - `dsp.py`: still has `elif demod == "nxdn":`?
   - `feature.py`: still uses `FeatureDetector.features` dict + `has_*` method pattern?
3. Update anchors in `patch_tetra.py` accordingly
4. If `dsp.py` was refactored (the `# TODO: move this to Modes` was resolved),
   remove the `dsp.py` patch and register via `modes.py` only

## ETSI ACELP Codec URL

The codec is downloaded during build from:
```
http://www.etsi.org/deliver/etsi_en/300300_300399/30039502/01.03.01_60/en_30039502v010301p0.zip
```

If the URL changes, update `ETSI_URL` in `build-tetra-packages.sh`.
The `etsi_codec-patches/codec.diff` inside osmo-tetra-sq5bpf applies the necessary patches
after extraction with `unzip -q -L` (lowercase flag is required on Linux).

## Key File Paths Inside the Container

| Path | Content |
|------|---------|
| `/opt/openwebrx-tetra/tetra-rx` | osmo-tetra protocol decoder |
| `/opt/openwebrx-tetra/cdecoder` | ETSI ACELP audio decoder |
| `/opt/openwebrx-tetra/tetra_decoder.py` | Full pipeline script |
| `/usr/lib/python3/dist-packages/csdr/chain/tetra.py` | CSDR chain |
| `/usr/lib/python3/dist-packages/csdr/modules/tetra.py` | Subprocess module |

## Frontend Plugin Development Rules

- Keep ES5 syntax: `var`, `function () {}`, `.prototype`
- Use tabs, not spaces
- Always declare `Plugins.tetra = Plugins.tetra || {};` at the top
- Update `_version` when changing behaviour
- Test with browser console (F12) — enable `Plugins._enable_debug = true` in `init.js`
- Run `Plugins.utils._DEBUG_ALL_EVENTS = true` to inspect all events
- After changes to `htdocs/`, the GitHub Pages workflow deploys automatically on push to main

## Metadata Message Format (backend → frontend)

All messages include `protocol: "TETRA"` and a `type` field:

| type | Key fields |
|------|-----------|
| `netinfo` | `mcc`, `mnc`, `dl_freq`, `ul_freq`, `color_code`, `encrypted` |
| `burst` | `slot` (0–3), `afc`, `burst_rate` |
| `call_setup` | `issi`, `gssi`, `call_type`, `encrypted`, `slot` |
| `connect` | same as call_setup |
| `tx_grant` | same as call_setup |
| `call_release` | (no extra fields) |
| `sds` | `from`, `to`, `text` |
