#!/usr/bin/env python3
"""
TETRA decoder pipeline for OpenWebRX+.
stdin:  complex float32 IQ at 36 kS/s (pi/4-DQPSK, 25 kHz channel)
stdout: 16-bit signed LE PCM at 8 kHz mono
stderr: JSON metadata lines (one object per line)

Pipeline:
  stdin IQ → GNURadio pi/4-DQPSK demod → tetra-rx → cdecoder | sdecoder → stdout PCM
                                               |
                                         TETMON UDP:7379 → JSON → stderr

Author: adapted from mbbrzoza/OpenWebRX-Tetra-Plugin and trollminer/OpenWebRX-Tetra-Plugin
"""

import json
import logging
import os
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

import numpy as np

logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")
logger = logging.getLogger("tetra_decoder")

TETRA_RX  = "/opt/openwebrx-tetra/tetra-rx"
CDECODER  = "/opt/openwebrx-tetra/cdecoder"
SDECODER  = "/opt/openwebrx-tetra/sdecoder"
TETRA_DEC = "/opt/openwebrx-tetra/tetra-dec"

INPUT_RATE  = 36000
OUTPUT_RATE = 8000
FRAME_BYTES = 1380
PCM_BYTES   = 960
SILENCE     = b"\x00" * PCM_BYTES
TETMON_PORT = 7379

# Per-type rate limits — minimum seconds between successive emissions
_RATE_LIMITS = {
    "netinfo":  5.0,
    "freqinfo": 10.0,
    "encinfo":  5.0,
    "burst":    0.25,
    "call":     0.5,
    "release":  0.1,
    "sds":      1.0,
    "status":   1.0,
}
_last_emit: dict = {}
_emit_lock = threading.Lock()


def _rate_ok(key: str) -> bool:
    now = time.monotonic()
    with _emit_lock:
        if now - _last_emit.get(key, 0.0) >= _RATE_LIMITS.get(key, 0.5):
            _last_emit[key] = now
            return True
    return False


def _emit(obj: dict) -> None:
    try:
        sys.stderr.buffer.write((json.dumps(obj) + "\n").encode())
        sys.stderr.buffer.flush()
    except Exception:
        pass


# ── TETMON field helpers ──────────────────────────────────────────────────────

def _field(payload: str, key: str, default=None):
    m = re.search(rf'(?<!\w){re.escape(key)}:(\S+)', payload, re.IGNORECASE)
    return m.group(1) if m else default


def _to_dec(s, default="") -> str:
    """Convert a TETMON hex field to decimal string; leave decimal strings alone."""
    if not s:
        return default
    try:
        if re.match(r'^[0-9A-Fa-f]+$', s) and not s.isdigit():
            return str(int(s, 16))
    except Exception:
        pass
    return s


def _slot(payload: str) -> int:
    v = _field(payload, "IDX", "0") or "0"
    try:
        return int(v)
    except Exception:
        return 0


def _enc_mode_str(val) -> str:
    modes = {"0": "None", "1": "TEA1", "2": "TEA2", "3": "TEA3",
             "4": "TEA4", "5": "TEA5", "6": "TEA6", "7": "TEA7"}
    return modes.get(str(val) if val is not None else "0", str(val) if val else "None")


# ── TETMON parser ─────────────────────────────────────────────────────────────

def _parse_tetmon(data: bytes) -> None:
    """Parse a TETMON UDP datagram and emit typed JSON to stderr."""
    try:
        text = data.decode("utf-8", errors="replace")

        # Prefer TETMON_begin/TETMON_end delimited payload
        begin = text.find("TETMON_begin")
        if begin >= 0:
            end = text.find("TETMON_end", begin)
            payload = text[begin + len("TETMON_begin"): end if end >= 0 else len(text)]
        else:
            fp = text.find("FUNC:")
            if fp < 0:
                return
            payload = text[fp:]

        # FUNC may be multi-word (e.g. "D-TX GRANTED", "D-CONNECT ACK")
        func_m = re.search(
            r'FUNC:((?:\S+)(?:\s+(?!(?:SSI|IDX|MCC|MNC|DL|UL|CC|CRYPT|CALLID|CID|LA|ENCMODE|STATUS|AFC|RATE|MSG)\s*:)\S+)*)',
            payload,
        )
        if not func_m:
            return
        func = func_m.group(1).strip().upper()
        p = payload

        # ── NETINFO1 ──────────────────────────────────────────────────────────
        if func == "NETINFO1":
            if not _rate_ok("netinfo"):
                return
            mcc = _field(p, "MCC", "?")
            mnc = _field(p, "MNC", "?")
            # Some firmware sends MCC/MNC as hex
            try:
                mcc = str(int(mcc, 16)) if not mcc.isdigit() else mcc
            except Exception:
                pass
            try:
                mnc = str(int(mnc, 16)) if not mnc.isdigit() else mnc
            except Exception:
                pass
            _emit({
                "type": "netinfo",
                "mcc": mcc,
                "mnc": mnc,
                "dl_freq": _field(p, "DL") or _field(p, "DLF", ""),
                "ul_freq": _field(p, "UL") or _field(p, "ULF", ""),
                "color_code": _field(p, "CC", ""),
                "la": _field(p, "LA", ""),
                "encrypted": (_field(p, "CRYPT", "0") != "0"),
            })

        # ── FREQINFO1 ─────────────────────────────────────────────────────────
        elif func == "FREQINFO1":
            if not _rate_ok("freqinfo"):
                return
            _emit({
                "type": "freqinfo",
                "dl_freq": _field(p, "DL") or _field(p, "DLF", ""),
                "ul_freq": _field(p, "UL") or _field(p, "ULF", ""),
            })

        # ── ENCINFO1 ──────────────────────────────────────────────────────────
        elif func == "ENCINFO1":
            if not _rate_ok("encinfo"):
                return
            _emit({
                "type": "encinfo",
                "encrypted": (_field(p, "CRYPT", "0") != "0"),
                "enc_mode": _enc_mode_str(_field(p, "ENCMODE", "0")),
            })

        # ── D-SETUP ───────────────────────────────────────────────────────────
        elif func in ("DSETUPDEC", "D-SETUP"):
            if not _rate_ok("call"):
                return
            _emit({
                "type": "call_setup",
                "issi": _to_dec(_field(p, "SSI", "")),
                "gssi": _to_dec(_field(p, "SSI2") or _field(p, "GSSI", "")),
                "call_id": _field(p, "CALLID") or _field(p, "CID", ""),
                "call_type": "group",
                "slot": _slot(p),
                "encrypted": (_field(p, "CRYPT", "0") != "0"),
            })

        # ── D-CONNECT ─────────────────────────────────────────────────────────
        elif func in ("DCONNECTDEC", "D-CONNECT", "D-CONNECT ACK"):
            if not _rate_ok("call"):
                return
            _emit({
                "type": "connect",
                "issi": _to_dec(_field(p, "SSI", "")),
                "gssi": _to_dec(_field(p, "SSI2") or _field(p, "GSSI", "")),
                "call_id": _field(p, "CALLID") or _field(p, "CID", ""),
                "call_type": "group",
                "slot": _slot(p),
                "encrypted": (_field(p, "CRYPT", "0") != "0"),
            })

        # ── D-TX GRANTED ──────────────────────────────────────────────────────
        elif func in ("DTXGRANTDEC", "D-TX-GRANTED", "D-TX GRANTED"):
            if not _rate_ok("call"):
                return
            _emit({
                "type": "tx_grant",
                "issi": _to_dec(_field(p, "SSI", "")),
                "gssi": _to_dec(_field(p, "SSI2") or _field(p, "GSSI", "")),
                "call_id": _field(p, "CALLID") or _field(p, "CID", ""),
                "call_type": "group",
                "slot": _slot(p),
                "encrypted": (_field(p, "CRYPT", "0") != "0"),
            })

        # ── D-RELEASE ─────────────────────────────────────────────────────────
        elif func in ("DRELEASEDEC", "D-RELEASE"):
            if not _rate_ok("release"):
                return
            _emit({
                "type": "call_release",
                "issi": _to_dec(_field(p, "SSI", "")),
                "call_id": _field(p, "CALLID") or _field(p, "CID", ""),
            })

        # ── D-STATUS ──────────────────────────────────────────────────────────
        elif func == "DSTATUSDEC":
            if not _rate_ok("status"):
                return
            status_raw = _field(p, "STATUS", "")
            try:
                status = str(int(status_raw, 16))
            except Exception:
                status = status_raw
            _emit({
                "type": "status",
                "issi": _to_dec(_field(p, "SSI", "")),
                "to": _to_dec(_field(p, "SSI2", "")),
                "status": status,
            })

        # ── SDS (Short Data Service) ──────────────────────────────────────────
        elif func == "SDSDEC":
            if not _rate_ok("sds"):
                return
            msg_m = re.search(r'MSG:(.+?)(?:TETMON_end|FUNC:|$)', p, re.DOTALL)
            _emit({
                "type": "sds",
                "from": _to_dec(_field(p, "SSI", "")),
                "to": _to_dec(_field(p, "SSI2", "")),
                "text": msg_m.group(1).strip() if msg_m else "",
            })

        # ── BURST ─────────────────────────────────────────────────────────────
        elif func == "BURST":
            if not _rate_ok("burst"):
                return
            _emit({
                "type": "burst",
                "slot": _slot(p),
                "afc": _field(p, "AFC", "0"),
                "burst_rate": _field(p, "RATE", ""),
            })

    except Exception as e:
        logger.debug("TETMON parse error: %s", e)


# ── TETMON UDP listener ───────────────────────────────────────────────────────

def _tetmon_listener(stop_event: threading.Event) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", TETMON_PORT))
        sock.settimeout(1.0)
    except OSError as e:
        logger.warning("TETMON UDP bind failed: %s", e)
        return
    while not stop_event.is_set():
        try:
            data, _ = sock.recvfrom(4096)
            _parse_tetmon(data)
        except socket.timeout:
            continue
        except Exception as e:
            logger.debug("TETMON error: %s", e)
    sock.close()


# ── Audio pipeline ────────────────────────────────────────────────────────────

class _AudioPipeline:
    """
    Two-stage ACELP codec: cdecoder stdout → pipe → sdecoder stdin.
    Falls back to a single cdecoder (or tetra-dec) if sdecoder is absent.
    """

    def __init__(self):
        self._cdecoder = None
        self._sdecoder = None
        self._two_stage = False
        self._lock = threading.Lock()

    def start(self) -> None:
        if os.path.isfile(CDECODER) and os.path.isfile(SDECODER):
            try:
                r_fd, w_fd = os.pipe()
                self._cdecoder = subprocess.Popen(
                    [CDECODER],
                    stdin=subprocess.PIPE,
                    stdout=w_fd,
                    stderr=subprocess.DEVNULL,
                )
                os.close(w_fd)
                self._sdecoder = subprocess.Popen(
                    [SDECODER],
                    stdin=r_fd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                os.close(r_fd)
                self._two_stage = True
                logger.debug("Audio: cdecoder | sdecoder (two-stage)")
                return
            except Exception as e:
                logger.warning("Two-stage codec failed (%s), falling back", e)
                self._teardown()

        dec = CDECODER if os.path.isfile(CDECODER) else TETRA_DEC
        self._cdecoder = subprocess.Popen(
            [dec],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._two_stage = False
        logger.debug("Audio: single-stage %s", dec)

    def _pcm_src(self):
        return self._sdecoder if self._two_stage else self._cdecoder

    def feed(self, frame: bytes) -> bytes:
        with self._lock:
            try:
                if not self._cdecoder or self._cdecoder.poll() is not None:
                    return SILENCE
                self._cdecoder.stdin.write(frame)
                self._cdecoder.stdin.flush()
                out = self._pcm_src()
                if not out or out.poll() is not None:
                    return SILENCE
                pcm = b""
                deadline = time.monotonic() + 0.1
                while len(pcm) < PCM_BYTES and time.monotonic() < deadline:
                    chunk = out.stdout.read(PCM_BYTES - len(pcm))
                    if not chunk:
                        break
                    pcm += chunk
                return pcm if len(pcm) == PCM_BYTES else SILENCE
            except Exception:
                return SILENCE

    def _teardown(self) -> None:
        for p in (self._sdecoder, self._cdecoder):
            if p:
                try:
                    p.stdin.close()
                except Exception:
                    pass
                try:
                    p.terminate()
                    p.wait(timeout=2)
                except Exception:
                    pass
        self._cdecoder = None
        self._sdecoder = None

    def stop(self) -> None:
        with self._lock:
            self._teardown()


# ── Pi/4-DQPSK demodulator (numpy) ───────────────────────────────────────────
#
# TETRA: pi/4-DQPSK, 18 kBaud, 2 samples/symbol at 36 kS/s, alpha=0.35 RRC.
# Output: one bit per byte (value 0 or 1) → tetra-rx stdin.

_RRC_CACHE: np.ndarray = None  # type: ignore


def _rrc_taps(sps: int = 2, alpha: float = 0.35, ntaps: int = 11) -> np.ndarray:
    """Root-raised-cosine filter taps (TETRA spec: alpha=0.35)."""
    half = ntaps // 2
    n = np.arange(-half, half + 1, dtype=float)
    h = np.empty(len(n))
    for i, nn in enumerate(n):
        t = nn / sps
        if t == 0.0:
            h[i] = 1.0 - alpha + 4.0 * alpha / np.pi
        elif abs(abs(t) - 1.0 / (4.0 * alpha)) < 1e-9:
            h[i] = (alpha / np.sqrt(2.0)) * (
                (1.0 + 2.0 / np.pi) * np.sin(np.pi / (4.0 * alpha))
                + (1.0 - 2.0 / np.pi) * np.cos(np.pi / (4.0 * alpha))
            )
        else:
            h[i] = (
                np.sin(np.pi * t * (1.0 - alpha))
                + 4.0 * alpha * t * np.cos(np.pi * t * (1.0 + alpha))
            ) / (np.pi * t * (1.0 - (4.0 * alpha * t) ** 2))
    h /= np.sqrt(np.dot(h, h))
    return h.astype(np.float32)


def _demod_pi4dqpsk(iq: np.ndarray, prev_sym: complex) -> tuple:
    """
    Pi/4-DQPSK demodulation.
    iq: complex64, 2 samples/symbol
    Returns: (bytes with one bit/byte, updated prev_sym)

    ETSI EN 300 392-2 Table 8.2 Gray mapping:
      Δφ = +π/4  → 00    (region  0..π/2)
      Δφ = +3π/4 → 01    (region  π/2..π)
      Δφ = -3π/4 → 11    (region -π..-π/2)
      Δφ = -π/4  → 10    (region -π/2..0)
    """
    global _RRC_CACHE
    if _RRC_CACHE is None:
        _RRC_CACHE = _rrc_taps()

    SPS = 2
    if len(iq) < SPS:
        return b'', prev_sym

    filtered = np.convolve(iq, _RRC_CACHE, mode='same')

    n_syms = len(filtered) // SPS
    if n_syms == 0:
        return b'', prev_sym

    # For each symbol period pick the sample with highest amplitude
    mat = filtered[:n_syms * SPS].reshape(n_syms, SPS)
    pick = np.argmax(np.abs(mat), axis=1)
    symbols = mat[np.arange(n_syms), pick]

    # Differential phase detection
    chain = np.empty(n_syms + 1, dtype=np.complex64)
    chain[0] = prev_sym
    chain[1:] = symbols
    phase_diff = np.angle(chain[1:] * np.conj(chain[:-1]))

    # Vectorised decision
    bits = np.empty(n_syms * 2, dtype=np.uint8)
    b0 = (phase_diff < 0).astype(np.uint8)        # MSB: 1 for negative Δφ
    b1 = (np.abs(phase_diff) > np.pi / 2).astype(np.uint8)  # LSB: 1 for ±3π/4
    bits[0::2] = b0
    bits[1::2] = b1

    return bits.tobytes(), complex(symbols[-1])


def _run_demod(src_fd: int, sink_fd: int, stop_event: threading.Event) -> None:
    """
    Read complex float32 IQ from src_fd, demodulate pi/4-DQPSK with numpy,
    write one-bit-per-byte stream to sink_fd (tetra-rx stdin).
    src_fd is a dup of stdin so Python's BufferedReader is never touched.
    """
    BYTES_PER_SAMPLE = 8        # complex64 = 2 × float32
    CHUNK_SAMPLES    = 4096
    CHUNK_BYTES      = CHUNK_SAMPLES * BYTES_PER_SAMPLE

    prev_sym = complex(1.0, 0.0)
    buf = b''

    try:
        while not stop_event.is_set():
            try:
                ready, _, _ = select.select([src_fd], [], [], 0.5)
                if not ready:
                    continue
                data = os.read(src_fd, CHUNK_BYTES)
                if not data:
                    break
            except OSError:
                break

            buf += data
            n_complete = (len(buf) // BYTES_PER_SAMPLE) * BYTES_PER_SAMPLE
            if n_complete == 0:
                continue

            iq = np.frombuffer(buf[:n_complete], dtype=np.complex64).copy()
            buf = buf[n_complete:]

            bits, prev_sym = _demod_pi4dqpsk(iq, prev_sym)
            if bits:
                try:
                    os.write(sink_fd, bits)
                except OSError:
                    break

    except Exception as e:
        logger.debug("demod thread error: %s", e)
    finally:
        try:
            os.close(src_fd)
        except Exception:
            pass
        try:
            os.close(sink_fd)
        except Exception:
            pass


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    stop_event = threading.Event()

    def _on_signal(*_):
        stop_event.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    # Duplicate stdin fd so daemon threads never touch Python's BufferedReader.
    # This prevents the _enter_buffered_busy crash at interpreter shutdown.
    stdin_dup = os.dup(0)

    # Pipe: demodulated bits (one bit/byte) → tetra-rx stdin
    gn_r, gn_w = os.pipe()

    tetra_rx = subprocess.Popen(
        [TETRA_RX, "-t", str(TETMON_PORT)],
        stdin=gn_r,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    os.close(gn_r)

    audio = _AudioPipeline()
    audio.start()

    threading.Thread(
        target=_tetmon_listener, args=(stop_event,), daemon=True, name="tetmon"
    ).start()

    # Demodulator: pi/4-DQPSK via numpy (no GNURadio required)
    threading.Thread(
        target=_run_demod, args=(stdin_dup, gn_w, stop_event), daemon=True, name="demod"
    ).start()

    # Audio pump: tetra-rx stdout → codec → stdout PCM
    last_audio = time.monotonic()
    buf = b""
    try:
        while not stop_event.is_set():
            if tetra_rx.poll() is not None:
                break
            ready, _, _ = select.select([tetra_rx.stdout], [], [], 0.02)
            if ready:
                chunk = tetra_rx.stdout.read(FRAME_BYTES)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= FRAME_BYTES:
                    frame, buf = buf[:FRAME_BYTES], buf[FRAME_BYTES:]
                    pcm = audio.feed(frame)
                    sys.stdout.buffer.write(pcm)
                    sys.stdout.buffer.flush()
                    last_audio = time.monotonic()
            else:
                if time.monotonic() - last_audio > 0.02:
                    sys.stdout.buffer.write(SILENCE)
                    sys.stdout.buffer.flush()
                    last_audio = time.monotonic()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        audio.stop()
        try:
            tetra_rx.terminate()
            tetra_rx.wait(timeout=3)
        except Exception:
            pass


if __name__ == "__main__":
    main()
