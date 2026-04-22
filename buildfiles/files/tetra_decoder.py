#!/usr/bin/env python3
"""
TETRA decoder pipeline for OpenWebRX+.
stdin:  complex float32 IQ at 36 kS/s (pi/4-DQPSK, 25 kHz channel)
stdout: 16-bit signed LE PCM at 8 kHz mono
stderr: JSON metadata lines (one object per line)

Pipeline:
  stdin IQ
    → _dqpsk_demod_thread (numpy: differential pi/4-DQPSK, decimate 2x)
    → tetra-rx -i (built-in float_to_bits + pseudo-AFC)
        └→ TETMON UDP:7379 → JSON metadata → stderr
        └→ TETMON UDP:7379 → TRA: audio → sdecoder → stdout PCM
"""

import json
import logging
import numpy as np
import os
import queue
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time

logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")
logger = logging.getLogger("tetra_decoder")

TETRA_RX = "/opt/openwebrx-tetra/tetra-rx"
CDECODER = "/opt/openwebrx-tetra/cdecoder"
SDECODER = "/opt/openwebrx-tetra/sdecoder"

FRAME_BYTES = 1380
PCM_BYTES   = 960
SILENCE     = b"\x00" * PCM_BYTES
TETMON_PORT = 7379

# 36 kS/s input, 18 kBaud TETRA = 2 samples per symbol
_SAMP_PER_SYM = 2

# Per-type rate limits
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


# ── TETMON field helpers ───────────────────────────────────────────────

def _field(payload: str, key: str, default=None):
    m = re.search(rf'(?<!\w){re.escape(key)}:(\S+)', payload, re.IGNORECASE)
    return m.group(1) if m else default


def _to_dec(s, default="") -> str:
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


# ── TETMON parser ────────────────────────────────────────────────────

def _parse_tetmon(data: bytes) -> None:
    try:
        text = data.decode("utf-8", errors="replace")
        begin = text.find("TETMON_begin")
        if begin >= 0:
            end = text.find("TETMON_end", begin)
            payload = text[begin + len("TETMON_begin"): end if end >= 0 else len(text)]
        else:
            fp = text.find("FUNC:")
            if fp < 0:
                return
            payload = text[fp:]

        func_m = re.search(
            r'FUNC:((?:\S+)(?:\s+(?!(?:SSI|IDX|MCC|MNC|DL|UL|CC|CRYPT|CALLID|CID|LA|ENCMODE|STATUS|AFC|RATE|MSG)\s*:)\S+)*)',
            payload,
        )
        if not func_m:
            return
        func = func_m.group(1).strip().upper()
        p = payload

        if func == "NETINFO1":
            if not _rate_ok("netinfo"):
                return
            mcc = _field(p, "MCC", "?")
            mnc = _field(p, "MNC", "?")
            try:
                mcc = str(int(mcc, 16)) if not mcc.isdigit() else mcc
            except Exception:
                pass
            try:
                mnc = str(int(mnc, 16)) if not mnc.isdigit() else mnc
            except Exception:
                pass
            _emit({"type": "netinfo", "mcc": mcc, "mnc": mnc,
                   "dl_freq": _field(p, "DL") or _field(p, "DLF", ""),
                   "ul_freq": _field(p, "UL") or _field(p, "ULF", ""),
                   "color_code": _field(p, "CC", ""),
                   "la": _field(p, "LA", ""),
                   "encrypted": (_field(p, "CRYPT", "0") != "0")})

        elif func == "FREQINFO1":
            if not _rate_ok("freqinfo"):
                return
            _emit({"type": "freqinfo",
                   "dl_freq": _field(p, "DL") or _field(p, "DLF", ""),
                   "ul_freq": _field(p, "UL") or _field(p, "ULF", "")})

        elif func == "ENCINFO1":
            if not _rate_ok("encinfo"):
                return
            _emit({"type": "encinfo",
                   "encrypted": (_field(p, "CRYPT", "0") != "0"),
                   "enc_mode": _enc_mode_str(_field(p, "ENCMODE", "0"))})

        elif func in ("DSETUPDEC", "D-SETUP"):
            if not _rate_ok("call"):
                return
            _emit({"type": "call_setup",
                   "issi": _to_dec(_field(p, "SSI", "")),
                   "gssi": _to_dec(_field(p, "SSI2") or _field(p, "GSSI", "")),
                   "call_id": _field(p, "CALLID") or _field(p, "CID", ""),
                   "call_type": "group", "slot": _slot(p),
                   "encrypted": (_field(p, "CRYPT", "0") != "0")})

        elif func in ("DCONNECTDEC", "D-CONNECT", "D-CONNECT ACK"):
            if not _rate_ok("call"):
                return
            _emit({"type": "connect",
                   "issi": _to_dec(_field(p, "SSI", "")),
                   "gssi": _to_dec(_field(p, "SSI2") or _field(p, "GSSI", "")),
                   "call_id": _field(p, "CALLID") or _field(p, "CID", ""),
                   "call_type": "group", "slot": _slot(p),
                   "encrypted": (_field(p, "CRYPT", "0") != "0")})

        elif func in ("DTXGRANTDEC", "D-TX-GRANTED", "D-TX GRANTED"):
            if not _rate_ok("call"):
                return
            _emit({"type": "tx_grant",
                   "issi": _to_dec(_field(p, "SSI", "")),
                   "gssi": _to_dec(_field(p, "SSI2") or _field(p, "GSSI", "")),
                   "call_id": _field(p, "CALLID") or _field(p, "CID", ""),
                   "call_type": "group", "slot": _slot(p),
                   "encrypted": (_field(p, "CRYPT", "0") != "0")})

        elif func in ("DRELEASEDEC", "D-RELEASE"):
            if not _rate_ok("release"):
                return
            _emit({"type": "call_release",
                   "issi": _to_dec(_field(p, "SSI", "")),
                   "call_id": _field(p, "CALLID") or _field(p, "CID", "")})

        elif func == "DSTATUSDEC":
            if not _rate_ok("status"):
                return
            status_raw = _field(p, "STATUS", "")
            try:
                status = str(int(status_raw, 16))
            except Exception:
                status = status_raw
            _emit({"type": "status",
                   "issi": _to_dec(_field(p, "SSI", "")),
                   "to": _to_dec(_field(p, "SSI2", "")),
                   "status": status})

        elif func == "SDSDEC":
            if not _rate_ok("sds"):
                return
            msg_m = re.search(r'MSG:(.+?)(?:TETMON_end|FUNC:|$)', p, re.DOTALL)
            _emit({"type": "sds",
                   "from": _to_dec(_field(p, "SSI", "")),
                   "to": _to_dec(_field(p, "SSI2", "")),
                   "text": msg_m.group(1).strip() if msg_m else ""})

        elif func == "BURST":
            if not _rate_ok("burst"):
                return
            _emit({"type": "burst", "slot": _slot(p),
                   "afc": _field(p, "AFC", "0"),
                   "burst_rate": _field(p, "RATE", "")})

    except Exception as e:
        logger.debug("TETMON parse error: %s", e)


# ── TETMON UDP listener ───────────────────────────────────────────────

def _tetmon_listener(
    stop_event: threading.Event,
    audio: "_AudioPipeline",
    pcm_queue: "queue.Queue[bytes]",
) -> None:
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
            tra_pos = data.find(b"TRA:")
            if tra_pos >= 0:
                frame = data[tra_pos + 4 : tra_pos + 4 + FRAME_BYTES]
                if len(frame) == FRAME_BYTES:
                    pcm = audio.feed(frame)
                    try:
                        pcm_queue.put_nowait(pcm)
                    except queue.Full:
                        pass
            else:
                _parse_tetmon(data)
        except socket.timeout:
            continue
        except Exception as e:
            logger.debug("TETMON error: %s", e)
    sock.close()


# ── pi/4-DQPSK demodulator ───────────────────────────────────────────────

def _dqpsk_demod_thread(
    tetra_rx_proc: subprocess.Popen,
    stop_event: threading.Event,
) -> None:
    """
    Reads complex float32 IQ at 36 kS/s from stdin.
    Demodulates pi/4-DQPSK via differential phase detection (decimate 2x).
    Writes float32 phase symbols at 18 kS/s to tetra-rx stdin.

    tetra-rx -i accepts pre-demodulated real-float phase-shift symbols,
    NOT raw complex IQ.  The -i flag only performs float_to_bits internally.
    """
    # 16 bytes = 2 complex64 samples = 1 symbol period
    ALIGN = _SAMP_PER_SYM * 8
    prev_sym = np.complex64(1.0 + 0j)
    leftover = b""

    while not stop_event.is_set():
        try:
            chunk = sys.stdin.buffer.read(4096)
        except Exception:
            break
        if not chunk:
            break

        buf = leftover + chunk
        # Round down to complete symbol periods (2 IQ samples = 16 bytes each)
        n_bytes = (len(buf) // ALIGN) * ALIGN
        if n_bytes == 0:
            leftover = buf
            continue
        leftover = buf[n_bytes:]

        iq = np.frombuffer(buf[:n_bytes], dtype=np.complex64)

        # Decimate by 2: one sample per symbol (symbol timing)
        z = iq[::_SAMP_PER_SYM]

        # Differential phase detection
        z_prev = np.empty(len(z) + 1, dtype=np.complex64)
        z_prev[0] = prev_sym
        z_prev[1:] = z[:-1]
        diff = z * np.conj(z_prev)
        # Normalise to units of pi/4 — tetra-rx process_sym_fl() quantises
        # these into the 4 DQPSK symbols {-3, -1, +1, +3}
        phase = (np.angle(diff) / (np.pi / 4)).astype(np.float32)

        prev_sym = z[-1]

        try:
            tetra_rx_proc.stdin.write(phase.tobytes())
            tetra_rx_proc.stdin.flush()
        except (BrokenPipeError, OSError):
            break


# ── Audio pipeline ─────────────────────────────────────────────────────────

class _AudioPipeline:
    """
    TETMON audio: TRA: frames (already channel-decoded by tetra-rx) go
    directly to sdecoder via /dev/stdin /dev/stdout.
    """

    def __init__(self):
        self._decoder = None
        self._lock = threading.Lock()

    def start(self) -> None:
        dec = SDECODER if os.path.isfile(SDECODER) else CDECODER
        if not os.path.isfile(dec):
            logger.warning("No ACELP decoder found (%s / %s)", SDECODER, CDECODER)
            return
        try:
            self._decoder = subprocess.Popen(
                [dec, "/dev/stdin", "/dev/stdout"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            logger.warning("Decoder start failed: %s", e)

    def feed(self, frame: bytes) -> bytes:
        with self._lock:
            if not self._decoder or self._decoder.poll() is not None:
                return SILENCE
            try:
                self._decoder.stdin.write(frame)
                self._decoder.stdin.flush()
                pcm = b""
                deadline = time.monotonic() + 0.15
                while time.monotonic() < deadline:
                    ready, _, _ = select.select([self._decoder.stdout], [], [], 0.05)
                    if not ready:
                        break
                    chunk = os.read(self._decoder.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    pcm += chunk
                return pcm if pcm else SILENCE
            except Exception:
                return SILENCE

    def stop(self) -> None:
        with self._lock:
            if self._decoder:
                try:
                    self._decoder.stdin.close()
                except Exception:
                    pass
                try:
                    self._decoder.terminate()
                    self._decoder.wait(timeout=2)
                except Exception:
                    pass
                self._decoder = None


# ── Main ────────────────────────────────────────────────────────────────────────

def main() -> None:
    stop_event = threading.Event()

    def _on_signal(*_):
        stop_event.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    tetra_env = dict(os.environ)
    tetra_env["TETRA_HACK_PORT"] = str(TETMON_PORT)
    tetra_env["TETRA_HACK_IP"]   = "127.0.0.1"
    tetra_env["TETRA_HACK_RXID"] = "1"

    # tetra-rx -i expects pre-demodulated float32 phase symbols (NOT raw IQ).
    # _dqpsk_demod_thread reads IQ from fd 0, demodulates, and writes here.
    tetra_rx = subprocess.Popen(
        [TETRA_RX, "-i", "-a", "-r", "-s", "-e", "/dev/stdin"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=tetra_env,
    )

    def _log_tetra_stderr():
        for line in tetra_rx.stderr:
            logger.debug("tetra-rx: %s", line.decode("utf-8", errors="replace").rstrip())
    threading.Thread(target=_log_tetra_stderr, daemon=True, name="tetra-rx-log").start()

    # Demodulation thread: reads complex IQ from stdin, writes float symbols
    threading.Thread(
        target=_dqpsk_demod_thread, args=(tetra_rx, stop_event),
        daemon=True, name="dqpsk-demod",
    ).start()

    audio = _AudioPipeline()
    audio.start()

    pcm_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=16)

    threading.Thread(
        target=_tetmon_listener, args=(stop_event, audio, pcm_queue),
        daemon=True, name="tetmon",
    ).start()

    last_audio = time.monotonic()
    try:
        while not stop_event.is_set():
            if tetra_rx.poll() is not None:
                break
            try:
                pcm = pcm_queue.get(timeout=0.02)
                sys.stdout.buffer.write(pcm)
                sys.stdout.buffer.flush()
                last_audio = time.monotonic()
            except queue.Empty:
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
