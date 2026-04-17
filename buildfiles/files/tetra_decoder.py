#!/usr/bin/env python3
"""
TETRA decoder pipeline for OpenWebRX+.
Reads complex float32 IQ from stdin (36 kS/s, pi/4-DQPSK, 25 kHz channel).
Writes 16-bit signed LE PCM audio to stdout (8 kHz mono).
Writes JSON signaling metadata to stderr (one object per line).

Pipeline:
  stdin IQ -> GNURadio pi/4-DQPSK demod -> tetra-rx -> tetra-dec/cdecoder -> stdout PCM
                                              |
                                         TETMON UDP -> metadata -> stderr JSON

Author: adapted from mbbrzoza/OpenWebRX-Tetra-Plugin (SP8MB) for xnetinho/openwebrxplus-docker-builder
"""

import json
import logging
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time

logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")
logger = logging.getLogger("tetra_decoder")

TETRA_RX    = "/opt/openwebrx-tetra/tetra-rx"
TETRA_DEC   = "/opt/openwebrx-tetra/tetra-dec"
CDECODER    = "/opt/openwebrx-tetra/cdecoder"
SDECODER    = "/opt/openwebrx-tetra/sdecoder"

INPUT_RATE   = 36000   # S/s complex float32
OUTPUT_RATE  = 8000    # Hz PCM s16le
FRAME_BYTES  = 1380    # ACELP frame size
PCM_BYTES    = 960     # output PCM bytes per frame (480 samples * 2 bytes)
SILENCE      = b"\x00" * PCM_BYTES
TETMON_PORT  = 7379    # UDP port tetra-rx uses for TETMON metadata

# ── Rate limiter ─────────────────────────────────────────────────────────────

class _RateLimiter:
    def __init__(self, interval=0.5):
        self._last = {}
        self._interval = interval

    def allow(self, key):
        now = time.monotonic()
        if now - self._last.get(key, 0) >= self._interval:
            self._last[key] = now
            return True
        return False

_rate = _RateLimiter(0.5)


def _emit(obj):
    """Write a metadata object as JSON to stderr."""
    try:
        sys.stderr.buffer.write((json.dumps(obj) + "\n").encode())
        sys.stderr.buffer.flush()
    except Exception:
        pass


# ── TETMON UDP listener ───────────────────────────────────────────────────────

def _tetmon_listener(stop_event):
    """Parses TETMON UDP packets from tetra-rx and emits JSON metadata."""
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


def _parse_tetmon(data):
    """Parse a raw TETMON packet and emit the relevant metadata event."""
    try:
        text = data.decode("utf-8", errors="replace").strip()
        if not text:
            return

        # TETMON format (space-separated tokens)
        tokens = text.split()
        if not tokens:
            return

        msg_type = tokens[0].lower()

        if msg_type == "netinfo" and len(tokens) >= 5:
            if _rate.allow("netinfo"):
                _emit({
                    "type": "netinfo",
                    "mcc": tokens[1],
                    "mnc": tokens[2],
                    "dl_freq": tokens[3],
                    "ul_freq": tokens[4] if len(tokens) > 4 else "",
                    "color_code": tokens[5] if len(tokens) > 5 else "",
                    "encrypted": "enc" in text.lower(),
                })

        elif msg_type in ("call_setup", "connect", "tx_grant") and len(tokens) >= 3:
            if _rate.allow(f"call_{tokens[1]}"):
                _emit({
                    "type": msg_type,
                    "issi": tokens[1],
                    "gssi": tokens[2] if len(tokens) > 2 else "",
                    "call_type": tokens[3] if len(tokens) > 3 else "group",
                    "encrypted": "enc" in text.lower(),
                    "slot": int(tokens[4]) if len(tokens) > 4 and tokens[4].isdigit() else 0,
                })

        elif msg_type == "burst" and len(tokens) >= 3:
            if _rate.allow("burst"):
                _emit({
                    "type": "burst",
                    "slot": int(tokens[1]) if tokens[1].isdigit() else 0,
                    "afc": tokens[2] if len(tokens) > 2 else "0",
                    "burst_rate": tokens[3] if len(tokens) > 3 else "",
                })

        elif msg_type in ("call_release", "disconnect"):
            if _rate.allow("release"):
                _emit({"type": "call_release"})

        elif msg_type == "sds" and len(tokens) >= 3:
            if _rate.allow("sds"):
                _emit({
                    "type": "sds",
                    "from": tokens[1],
                    "to": tokens[2],
                    "text": " ".join(tokens[3:]) if len(tokens) > 3 else "",
                })

    except Exception as e:
        logger.debug("TETMON parse error: %s", e)


# ── Audio pipeline (tetra-rx → cdecoder → PCM) ───────────────────────────────

class _AudioPipeline:
    def __init__(self):
        self._cdecoder = None
        self._lock = threading.Lock()

    def start(self):
        decoder_bin = CDECODER if os.path.isfile(CDECODER) else TETRA_DEC
        self._cdecoder = subprocess.Popen(
            [decoder_bin],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def feed(self, frame_bytes):
        """Send an ACELP frame and return PCM bytes (or silence on error)."""
        with self._lock:
            if not self._cdecoder or self._cdecoder.poll() is not None:
                return SILENCE
            try:
                self._cdecoder.stdin.write(frame_bytes)
                self._cdecoder.stdin.flush()
                pcm = b""
                deadline = time.monotonic() + 0.1
                while len(pcm) < PCM_BYTES and time.monotonic() < deadline:
                    chunk = self._cdecoder.stdout.read(PCM_BYTES - len(pcm))
                    if not chunk:
                        break
                    pcm += chunk
                return pcm if len(pcm) == PCM_BYTES else SILENCE
            except Exception:
                return SILENCE

    def stop(self):
        with self._lock:
            if self._cdecoder:
                try:
                    self._cdecoder.stdin.close()
                    self._cdecoder.terminate()
                    self._cdecoder.wait(timeout=2)
                except Exception:
                    pass


# ── Main pipeline ─────────────────────────────────────────────────────────────

def main():
    stop_event = threading.Event()

    def _sigterm(*_):
        stop_event.set()

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT,  _sigterm)

    # GNU Radio pi/4-DQPSK demodulator → tetra-rx
    gnuradio_cmd = [
        "python3", "-c",
        f"""
import sys, subprocess
from gnuradio import gr, blocks, digital, filter as grfilter
from gnuradio.filter import firdes

class FlowGraph(gr.top_block):
    def __init__(self):
        super().__init__()
        src  = blocks.file_descriptor_source(gr.sizeof_gr_complex, 0, False)
        resamp = grfilter.rational_resampler_ccc(1, 1)
        dmod = digital.dqpsk_demod(samples_per_symbol=2, gray_coded=True, verbose=False, log=False)
        pack = blocks.pack_k_bits_bb(8)
        sink = blocks.file_descriptor_sink(gr.sizeof_char, {TETMON_PORT})
        self.connect(src, dmod, pack, sink)

FlowGraph().run()
"""
    ]

    tetra_rx_proc = subprocess.Popen(
        [TETRA_RX, "-t", str(TETMON_PORT)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    audio = _AudioPipeline()
    audio.start()

    meta_thread = threading.Thread(
        target=_tetmon_listener, args=(stop_event,), daemon=True
    )
    meta_thread.start()

    # Pump: stdin IQ → tetra-rx stdin
    def _pump_input():
        try:
            while not stop_event.is_set():
                chunk = sys.stdin.buffer.read(4096)
                if not chunk:
                    break
                tetra_rx_proc.stdin.write(chunk)
                tetra_rx_proc.stdin.flush()
        except Exception:
            pass
        finally:
            try:
                tetra_rx_proc.stdin.close()
            except Exception:
                pass

    pump = threading.Thread(target=_pump_input, daemon=True)
    pump.start()

    # Pump: tetra-rx stdout → cdecoder → stdout PCM
    last_audio = time.monotonic()
    buf = b""
    try:
        while not stop_event.is_set():
            if tetra_rx_proc.poll() is not None:
                break
            ready, _, _ = select.select([tetra_rx_proc.stdout], [], [], 0.02)
            if ready:
                chunk = tetra_rx_proc.stdout.read(FRAME_BYTES)
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
                # Emit silence to keep the audio stream alive
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
            tetra_rx_proc.terminate()
            tetra_rx_proc.wait(timeout=3)
        except Exception:
            pass


if __name__ == "__main__":
    main()
