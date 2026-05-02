#!/usr/bin/env python3
"""TETRA decoder wrapper for OpenWebRX+.

Reads complex float IQ from stdin (36 kS/s, centered on TETRA carrier).
Writes PCM audio to stdout (8 kHz, 16-bit signed LE, mono).
Writes JSON metadata to stderr (TETMON signaling: network info, calls).

Per-frame encryption filtering (Bug 13, Option A):
  This build of osmo-tetra-sq5bpf emits voice bursts even for encrypted
  calls — the codec then produces noise with vocal envelope. We use a
  zlib compression-ratio heuristic on each ACELP payload to drop
  bursts that look uniform-random (likely encrypted). Threshold via
  `TETRA_ENC_RATIO_THRESHOLD` (float, default 0 = filter off pending
  calibration). Per-frame ratios are logged when `TETRA_DEBUG=1` so
  the user can pick a threshold from a real capture.

  The earlier signaling-based filter (Bug 12) is preserved but
  defaults to off (`TETRA_DROP_NO_CALL=0`) because short-form FUNC
  emission is too sparse on this build to be a reliable gate.

Debug:
  TETRA_DEBUG=1                  -> dumps to /tmp/tetra-debug.log
                                    (override path with TETRA_DEBUG_FILE).
  TETRA_PCM_DUMP=/path.raw       -> appends each PCM chunk to file.
                                    Listen with `aplay -r 8000 -f S16_LE
                                    -c 1 ...`.
  TETRA_ENC_RATIO_THRESHOLD=0.92 -> drop bursts with zlib ratio above
                                    this value (Option A filter; 0 = off).
  TETRA_DROP_NO_CALL=1           -> legacy clear-call filter (default
                                    off, unusable on this build).

Encryption indicator: short-form FUNCs (D-SETUP, D-CONNECT, D-RELEASE,
D-TX CEASED) carry an `ENCR:N` field where 0=clear and ≠0=encrypted.
The long-form FUNCs (DSETUPDEC, DCONNECTDEC, DRELEASEDEC) don't carry
this field in this build, so we drive the legacy call-state machine
from the short form only.

Voice frame format (this build always emits the 2-field NUL-terminated
header even without `-e`): `TRA:HH RX:HH\\x00` + 1380 bytes (690 × int16
soft-bits, first word `0x6B21` little-endian, ±127 magnitudes after
Viterbi). The TRA byte is unstable upstream; do not use it as a burst
type discriminator.

Codec pipeline is async: feeding ACELP into cdecoder.stdin never blocks
the main loop. A dedicated reader thread pulls PCM from sdecoder.stdout
and writes it to sys.stdout.
"""

import datetime
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import zlib

TETRA_DIR = os.environ.get("TETRA_DIR", "/opt/openwebrx-tetra")
TETRA_DEBUG = os.environ.get("TETRA_DEBUG", "0") == "1"
TETRA_DEBUG_FILE = os.environ.get("TETRA_DEBUG_FILE", "/tmp/tetra-debug.log")
TETRA_PCM_DUMP = os.environ.get("TETRA_PCM_DUMP", "")
TETRA_DROP_NO_CALL = os.environ.get("TETRA_DROP_NO_CALL", "0") == "1"
try:
    TETRA_ENC_RATIO_THRESHOLD = float(os.environ.get("TETRA_ENC_RATIO_THRESHOLD", "0"))
except ValueError:
    TETRA_ENC_RATIO_THRESHOLD = 0.0
TETRA_ENC_RATIO_LOG_EVERY = 25  # log enc_ratio every Nth voice frame

ACELP_FRAME_SIZE = 1380
PCM_OUTPUT_BYTES = 960
CALL_STALE_TIMEOUT = 30.0  # seconds without renewal -> drop SSI

# Unified audio header pattern.
AUDIO_PATTERN = re.compile(
    rb"TRA:[0-9a-fA-F]+ +RX:[0-9a-fA-F]+(?: +DECR:[0-9a-fA-F]+ +|\x00)"
)

TEA_NAMES = {0: "none", 1: "TEA1", 2: "TEA2", 3: "TEA3"}

try:
    _DEBUG_FH = open(TETRA_DEBUG_FILE, 'a', buffering=1)
    _DEBUG_FH.write(
        '\n=== tetra_decoder.py startup at {} | TETRA_DEBUG={} | PCM_DUMP={} | '
        'DROP_NO_CALL={} | ENC_RATIO_THRESHOLD={} ===\n'.format(
            datetime.datetime.now().isoformat(timespec='seconds'),
            'on' if TETRA_DEBUG else 'off',
            TETRA_PCM_DUMP or 'off',
            'on' if TETRA_DROP_NO_CALL else 'off',
            TETRA_ENC_RATIO_THRESHOLD if TETRA_ENC_RATIO_THRESHOLD > 0 else 'off'))
except Exception:
    _DEBUG_FH = None

_PCM_DUMP_FH = None
if TETRA_PCM_DUMP:
    try:
        _PCM_DUMP_FH = open(TETRA_PCM_DUMP, 'ab', buffering=0)
    except Exception:
        _PCM_DUMP_FH = None


def debug_dump(label, data, max_bytes=128):
    if not TETRA_DEBUG or _DEBUG_FH is None:
        return
    body = data[:max_bytes]
    hex_str = ' '.join('{:02x}'.format(b) for b in body)
    ascii_str = ''.join(chr(b) if 32 <= b < 127 else '.' for b in body)
    try:
        _DEBUG_FH.write('[{}] {} len={} hex={} ascii={}\n'.format(
            time.strftime('%H:%M:%S'), label, len(data), hex_str, ascii_str))
    except Exception:
        pass


# ---------- Encryption-ratio heuristic (Bug 13, Option A) ----------

def _enc_ratio(acelp_data):
    """zlib compression ratio of an ACELP voice frame.

    Clear bursts: speech-correlated sign patterns, ratio low (~0.5–0.85).
    Encrypted bursts: near-uniform random signs, ratio high (~0.9–1.0).

    Returns 1.0 (== "treat as encrypted") for empty/short input.
    """
    if not acelp_data or len(acelp_data) < 32:
        return 1.0
    try:
        return len(zlib.compress(acelp_data, 6)) / len(acelp_data)
    except Exception:
        return 1.0


# ---------- Active clear-call tracking (legacy Bug 12, default off) ----------

_active_clear_calls = set()
_last_seen_call = {}
_calls_lock = threading.Lock()


def _mark_clear(ssi):
    if ssi <= 0:
        return
    now = time.monotonic()
    with _calls_lock:
        added = ssi not in _active_clear_calls
        _active_clear_calls.add(ssi)
        _last_seen_call[ssi] = now
    if added and TETRA_DEBUG:
        debug_dump('call_open_clear', f'ssi={ssi}'.encode(), max_bytes=32)


def _mark_encrypted(ssi):
    if ssi <= 0:
        return
    with _calls_lock:
        removed = ssi in _active_clear_calls
        _active_clear_calls.discard(ssi)
        _last_seen_call.pop(ssi, None)
    if removed and TETRA_DEBUG:
        debug_dump('call_drop_enc', f'ssi={ssi}'.encode(), max_bytes=32)


def _mark_release(ssi):
    if ssi <= 0:
        return
    with _calls_lock:
        removed = ssi in _active_clear_calls
        _active_clear_calls.discard(ssi)
        _last_seen_call.pop(ssi, None)
    if removed and TETRA_DEBUG:
        debug_dump('call_release', f'ssi={ssi}'.encode(), max_bytes=32)


def _has_active_clear_call():
    now = time.monotonic()
    with _calls_lock:
        stale = [s for s, t in _last_seen_call.items()
                 if now - t > CALL_STALE_TIMEOUT]
        for s in stale:
            _active_clear_calls.discard(s)
            _last_seen_call.pop(s, None)
        return bool(_active_clear_calls)


def parse_tetmon_fields(data):
    fields = {}
    for m in re.finditer(rb'([A-Z_]+):([^\s]+)', data):
        fields[m.group(1).decode()] = m.group(2).decode()
    return fields


class CodecPipeline:
    def __init__(self):
        self._cdecoder = None
        self._sdecoder = None
        self._lock = threading.Lock()
        self._reader = None
        self._started = False

    def start(self):
        cdecoder_path = os.path.join(TETRA_DIR, 'cdecoder')
        sdecoder_path = os.path.join(TETRA_DIR, 'sdecoder')
        if not os.path.isfile(cdecoder_path) or not os.path.isfile(sdecoder_path):
            for p in ['/tetra/bin', '/usr/local/bin']:
                if os.path.isfile(os.path.join(p, 'cdecoder')):
                    cdecoder_path = os.path.join(p, 'cdecoder')
                    sdecoder_path = os.path.join(p, 'sdecoder')
                    break
        pipe_r, pipe_w = os.pipe()
        self._cdecoder = subprocess.Popen(
            [cdecoder_path, '/dev/stdin', '/dev/stdout'],
            stdin=subprocess.PIPE, stdout=pipe_w, stderr=subprocess.DEVNULL)
        os.close(pipe_w)
        self._sdecoder = subprocess.Popen(
            [sdecoder_path, '/dev/stdin', '/dev/stdout'],
            stdin=pipe_r, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        os.close(pipe_r)
        self._started = True
        self._reader = threading.Thread(
            target=self._reader_loop, name='codec-reader', daemon=True)
        self._reader.start()

    def _reader_loop(self):
        sd = self._sdecoder
        if sd is None or sd.stdout is None:
            return
        try:
            while self._started:
                pcm = sd.stdout.read(PCM_OUTPUT_BYTES)
                if not pcm:
                    break
                if len(pcm) != PCM_OUTPUT_BYTES:
                    continue
                debug_dump('pcm', pcm, max_bytes=16)
                if _PCM_DUMP_FH is not None:
                    try:
                        _PCM_DUMP_FH.write(pcm)
                    except Exception:
                        pass
                try:
                    sys.stdout.buffer.write(pcm)
                    sys.stdout.buffer.flush()
                except (BrokenPipeError, OSError):
                    break
        except Exception:
            pass

    def feed(self, acelp_data):
        with self._lock:
            if not self._started:
                try: self.start()
                except Exception: return
            try:
                if (self._cdecoder.poll() is not None or
                        self._sdecoder.poll() is not None):
                    self._stop_locked()
                    self.start()
                self._cdecoder.stdin.write(acelp_data)
                self._cdecoder.stdin.flush()
            except (BrokenPipeError, OSError):
                self._stop_locked()

    def _stop_locked(self):
        self._started = False
        for proc in (self._cdecoder, self._sdecoder):
            if proc:
                try: proc.kill(); proc.wait(timeout=1)
                except Exception: pass
        self._cdecoder = None
        self._sdecoder = None

    def stop(self):
        with self._lock:
            self._stop_locked()


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def parse_audio_from_udp(data):
    tra_pos = data.find(b'TRA:')
    if tra_pos < 0:
        return None
    payload = data[tra_pos:]
    match = AUDIO_PATTERN.match(payload)
    if match is None:
        return None
    offset = match.end()
    if len(payload) < offset + ACELP_FRAME_SIZE:
        return None
    return payload[offset:offset + ACELP_FRAME_SIZE]


def _enc_pair_from_encr(fields):
    """ENCR:N is the encryption indicator on short-form FUNCs."""
    try:
        encr = int(fields.get('ENCR', '0'))
    except ValueError:
        encr = 0
    return (encr in (1, 2, 3), TEA_NAMES.get(encr, "none"), encr)


def parse_metadata_from_udp(data):
    begin = data.find(b'TETMON_begin')
    if begin < 0:
        func_pos = data.find(b'FUNC:')
        if func_pos < 0:
            return None
        payload = data[func_pos:]
    else:
        end = data.find(b'TETMON_end', begin)
        payload = data[begin + len(b'TETMON_begin'):end] if end >= 0 else data[begin + len(b'TETMON_begin'):]
    payload = payload.strip()

    fields = parse_tetmon_fields(payload)
    func = fields.get('FUNC', '')
    func_match = re.search(
        rb'FUNC:(\S+(?:\s+(?!SSI:|SSI2:|IDX:|IDT:|ENCR:|ENCC:|RX:|CID:|NID:|CCODE:|MCC:|MNC:|TXGRANT:|TXPERM:|CALLOWN:|STATUS:|DLF:|ULF:|LA:|CRYPT:|ENC:|TIME:)\S+)*)',
        payload)
    if func_match:
        func = func_match.group(1).decode()

    try: ssi = int(fields.get('SSI', '0'))
    except ValueError: ssi = 0

    # ----- Short-form FUNCs (drive legacy call-state machine via ENCR) -----
    if func == 'D-SETUP':
        enc, enc_type, encr = _enc_pair_from_encr(fields)
        if encr == 0:
            _mark_clear(ssi)
        else:
            _mark_encrypted(ssi)
        return {"protocol": "TETRA", "type": "call_setup",
                "ssi": ssi, "encr": encr, "encrypted": enc,
                "encryption_type": enc_type}

    if func == 'D-CONNECT':
        enc, enc_type, encr = _enc_pair_from_encr(fields)
        if encr == 0:
            _mark_clear(ssi)
        else:
            _mark_encrypted(ssi)
        return {"protocol": "TETRA", "type": "call_connect",
                "ssi": ssi, "encr": encr, "encrypted": enc,
                "encryption_type": enc_type}

    if func == 'D-RELEASE':
        _mark_release(ssi)
        return {"protocol": "TETRA", "type": "call_release", "ssi": ssi}

    if func.startswith('D-TX'):
        # 'D-TX CEASED' marks end of TX for that SSI -> drop from active set.
        if 'CEASED' in func:
            _mark_release(ssi)
        return {"protocol": "TETRA", "type": "tx",
                "ssi": ssi, "func": func}

    # ----- Long-form FUNCs (additional fields, no ENCR) -----
    if func == 'NETINFO1':
        try: mcc = int(fields.get('MCC', '0'), 16)
        except ValueError: mcc = 0
        try: mnc = int(fields.get('MNC', '0'), 16)
        except ValueError: mnc = 0
        try: color_code = int(fields.get('CCODE', '0'), 16)
        except ValueError: color_code = 0
        crypt = int(fields.get('CRYPT', '0'))
        return {
            "protocol": "TETRA", "type": "netinfo",
            "mcc": mcc, "mnc": mnc,
            "dl_freq": int(fields.get('DLF', '0')),
            "ul_freq": int(fields.get('ULF', '0')),
            "color_code": color_code,
            "cell_security_class": crypt,
            "cell_tea": TEA_NAMES.get(crypt, f"unknown({crypt})"),
            "encrypted": False, "encryption_type": "none",
            "la": fields.get('LA', ''),
        }

    if func == 'FREQINFO1':
        return {"protocol": "TETRA", "type": "freqinfo",
                "dl_freq": int(fields.get('DLF', '0')),
                "ul_freq": int(fields.get('ULF', '0'))}

    if func == 'DSETUPDEC':
        # No ENCR field here; the short D-SETUP decides clear/enc.
        return {"protocol": "TETRA", "type": "call_setup_detail",
                "ssi": ssi,
                "ssi2": int(fields.get('SSI2', '0')),
                "call_id": int(fields.get('CID', '0')),
                "idx": int(fields.get('IDX', '0'))}

    if func == 'DRELEASEDEC':
        _mark_release(ssi)
        return {"protocol": "TETRA", "type": "call_release",
                "ssi": ssi,
                "call_id": int(fields.get('CID', '0'))}

    if func == 'DCONNECTDEC':
        result = {"protocol": "TETRA", "type": "call_connect_detail",
                  "ssi": ssi,
                  "call_id": int(fields.get('CID', '0')),
                  "idx": int(fields.get('IDX', '0'))}
        if 'SSI2' in fields:
            result["ssi2"] = int(fields['SSI2'])
        return result

    if func == 'DTXGRANTDEC':
        result = {"protocol": "TETRA", "type": "tx_grant",
                  "ssi": ssi,
                  "call_id": int(fields.get('CID', '0')),
                  "idx": int(fields.get('IDX', '0'))}
        if 'SSI2' in fields:
            result["ssi2"] = int(fields['SSI2'])
        return result

    if func == 'ENCINFO1':
        crypt = int(fields.get('CRYPT', '0'))
        return {"protocol": "TETRA", "type": "encinfo",
                "cell_security_class": crypt,
                "cell_tea": TEA_NAMES.get(crypt, f"unknown({crypt})"),
                "enc_mode": fields.get('ENC', '00'),
                "encrypted": False}

    if func == 'DSTATUSDEC':
        return {"protocol": "TETRA", "type": "status",
                "ssi": ssi,
                "ssi2": int(fields.get('SSI2', '0')),
                "status": fields.get('STATUS', '')}

    if func == 'BURST':
        return {"protocol": "TETRA", "type": "burst"}

    if func == 'SDSDEC':
        return {"protocol": "TETRA", "type": "sds",
                "ssi": ssi,
                "ssi2": int(fields.get('SSI2', '0'))}

    if func.startswith('D-') and 'IDT' in fields:
        ssi2 = int(fields.get('SSI2', '0')) if 'SSI2' in fields else 0
        if ssi > 0 or ssi2 > 0:
            result = {"protocol": "TETRA", "type": "resource",
                      "func": func, "ssi": ssi,
                      "idt": int(fields.get('IDT', '0'))}
            if ssi2 > 0:
                result["ssi2"] = ssi2
            return result

    return None


def emit_meta(meta_dict):
    try:
        sys.stderr.write(json.dumps(meta_dict) + '\n')
        sys.stderr.flush()
    except (BrokenPipeError, OSError):
        pass


def main():
    running = True
    def shutdown(signum, frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    udp_port = find_free_port()
    env = os.environ.copy()
    env['TETRA_HACK_PORT'] = str(udp_port)
    env['TETRA_HACK_IP'] = '127.0.0.1'
    env['TETRA_HACK_RXID'] = '1'

    keyfile = os.path.join(TETRA_DIR, 'keyfile')
    tetra_rx_path = os.path.join(TETRA_DIR, 'tetra-rx')
    demod_path = os.path.join(TETRA_DIR, 'tetra_demod.py')

    demod = subprocess.Popen(['python3', demod_path], stdin=0,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    tetra_rx_cmd = [tetra_rx_path, '-r', '-s', '/dev/stdin']
    if os.path.isfile(keyfile):
        tetra_rx_cmd.extend(['-k', keyfile])
    tetra_rx = subprocess.Popen(tetra_rx_cmd, stdin=demod.stdout,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    demod.stdout.close()

    codec = CodecPipeline()
    state_lock = threading.Lock()
    ts_usage = {1: "unknown", 2: "unknown", 3: "unknown", 4: "unknown"}
    current_tn = [0]
    afc_value = [0.0]
    burst_count = [0]
    burst_rate = [0.0]
    burst_window_start = [time.monotonic()]
    call_type_info = [""]
    drop_count = [0]
    drop_no_call_count = [0]
    drop_enc_count = [0]
    fed_count = [0]
    enc_frame_index = [0]
    last_drop_log = [time.monotonic()]

    re_sync = re.compile(r'TN \d+\((\d+)\)')
    re_access_dl = re.compile(r'DL_USAGE:\s*(\S+)')
    re_access_a1 = re.compile(r'ACCESS1:\s*A/(\d+)')
    re_basicinfo = re.compile(r'Basicinfo:0x([0-9A-Fa-f]{2})')

    def decode_call_type(basicinfo_byte):
        cmt = (basicinfo_byte >> 5) & 0x07
        types = {0: "individual", 1: "group", 2: "broadcast",
                 3: "acknowledged group"}
        return types.get(cmt, "other")

    def parse_tetra_rx_stdout():
        fd = tetra_rx.stdout.fileno()
        try:
            while True:
                chunk = os.read(fd, 16384)
                if not chunk: break
                text = chunk.decode(errors='replace')
                for m in re_sync.finditer(text):
                    tn = int(m.group(1)) or 1
                    current_tn[0] = tn
                for line in text.split('\n'):
                    if 'ACCESS-ASSIGN' in line:
                        tn = current_tn[0]
                        if not (1 <= tn <= 4): continue
                        with state_lock:
                            m = re_access_dl.search(line)
                            if m:
                                ts_usage[tn] = "unallocated" if m.group(1).startswith('U') else "assigned"
                                continue
                            m = re_access_a1.search(line)
                            if m:
                                v = int(m.group(1))
                                ts_usage[tn] = "assigned" if 1 <= v <= 3 else "unallocated"
                    if 'Basicinfo' in line:
                        m = re_basicinfo.search(line)
                        if m:
                            ct = decode_call_type(int(m.group(1), 16))
                            with state_lock:
                                call_type_info[0] = ct
        except (ValueError, OSError):
            pass

    def read_demod_stderr():
        try:
            for line in demod.stderr:
                line = line.strip()
                if not line: continue
                try:
                    data = json.loads(line)
                    if 'afc' in data:
                        with state_lock:
                            afc_value[0] = data['afc']
                except (json.JSONDecodeError, Exception):
                    pass
        except (ValueError, OSError):
            pass

    threading.Thread(target=parse_tetra_rx_stdout, daemon=True).start()
    threading.Thread(target=read_demod_stderr, daemon=True).start()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('127.0.0.1', udp_port))
    sock.settimeout(0.1)

    last_emit_time = {}
    RATE_LIMITS = {"burst": 0.5, "netinfo": 5.0, "freqinfo": 10.0, "encinfo": 5.0}

    while running:
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except Exception:
            break
        if tetra_rx.poll() is not None:
            break

        debug_dump('udp', data)

        meta = parse_metadata_from_udp(data)
        if meta is not None:
            now = time.monotonic()
            msg_type = meta.get("type")
            rate_limit = RATE_LIMITS.get(msg_type, 0)
            if now - last_emit_time.get(msg_type, 0) >= rate_limit:
                if msg_type == "burst":
                    burst_count[0] += 1
                    elapsed = now - burst_window_start[0]
                    if elapsed >= 2.0:
                        burst_rate[0] = burst_count[0] / elapsed
                        burst_count[0] = 0
                        burst_window_start[0] = now
                    with state_lock:
                        meta["timeslots"] = {str(k): v for k, v in ts_usage.items()}
                        meta["afc"] = afc_value[0]
                        meta["burst_rate"] = round(burst_rate[0], 1)
                        if call_type_info[0]:
                            meta["call_type"] = call_type_info[0]

                if msg_type in ("call_setup", "call_connect", "call_setup_detail",
                                "call_connect_detail", "tx_grant", "tx"):
                    with state_lock:
                        if call_type_info[0]:
                            meta["call_type"] = call_type_info[0]

                if msg_type == "call_release":
                    with state_lock:
                        call_type_info[0] = ""

                emit_meta(meta)
                last_emit_time[msg_type] = now

        acelp_data = parse_audio_from_udp(data)
        if acelp_data is not None:
            debug_dump('acelp', acelp_data, max_bytes=32)
            enc_frame_index[0] += 1

            ratio = _enc_ratio(acelp_data)
            ratio_says_drop = (
                TETRA_ENC_RATIO_THRESHOLD > 0 and ratio > TETRA_ENC_RATIO_THRESHOLD)
            no_call_says_drop = (
                TETRA_DROP_NO_CALL and not _has_active_clear_call())

            if TETRA_DEBUG and (enc_frame_index[0] % TETRA_ENC_RATIO_LOG_EVERY) == 0:
                debug_dump(
                    'enc_ratio',
                    ('frame={} ratio={:.3f} threshold={:.3f} drop_ratio={} '
                     'drop_no_call={}').format(
                        enc_frame_index[0], ratio, TETRA_ENC_RATIO_THRESHOLD,
                        int(ratio_says_drop), int(no_call_says_drop)
                    ).encode(),
                    max_bytes=128)

            if ratio_says_drop:
                drop_enc_count[0] += 1
                drop_count[0] += 1
            elif no_call_says_drop:
                drop_no_call_count[0] += 1
                drop_count[0] += 1
            else:
                fed_count[0] += 1
                codec.feed(acelp_data)

            now = time.monotonic()
            if TETRA_DEBUG and now - last_drop_log[0] >= 5.0:
                debug_dump(
                    'frame_stats',
                    ('fed={} dropped={} drop_enc={} drop_no_call={} '
                     'active_clear={}').format(
                        fed_count[0], drop_count[0], drop_enc_count[0],
                        drop_no_call_count[0], len(_active_clear_calls)
                    ).encode(),
                    max_bytes=96)
                last_drop_log[0] = now

    sock.close()
    codec.stop()
    for proc in (tetra_rx, demod):
        try: proc.terminate(); proc.wait(timeout=2)
        except Exception:
            try: proc.kill()
            except Exception: pass


if __name__ == '__main__':
    main()
