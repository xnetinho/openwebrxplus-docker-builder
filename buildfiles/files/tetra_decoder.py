#!/usr/bin/env python3
"""TETRA decoder wrapper for OpenWebRX+.
Author: SP8MB (original), adaptado para container Docker.

Reads complex float IQ from stdin (36 kS/s, centered on TETRA carrier).
Writes PCM audio to stdout (8 kHz, 16-bit signed LE, mono).
Writes JSON metadata to stderr (TETMON signaling: network info, calls, etc.).

Pipeline:
  stdin IQ -> GNURadio DQPSK demod -> tetra-rx -> UDP TETMON -> ACELP codec -> stdout PCM
                                                             -> JSON meta -> stderr
"""

import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time

TETRA_DIR = os.environ.get("TETRA_DIR", "/opt/openwebrx-tetra")

ACELP_FRAME_SIZE = 1380
PCM_OUTPUT_BYTES = 960
AUDIO_HEADER_SIZE = 20

AUDIO_PATTERN = re.compile(
    rb"TRA:([0-9a-fA-F]+)\s+RX:([0-9a-fA-F]+)\s+DECR:([0-9a-fA-F]+)"
)


def parse_tetmon_fields(data):
    fields = {}
    for m in re.finditer(rb'([A-Z_]+):([^\s]+)', data):
        fields[m.group(1).decode()] = m.group(2).decode()
    return fields


class CodecPipeline:
    """Persistent cdecoder|sdecoder subprocess pipeline."""

    def __init__(self):
        self._cdecoder = None
        self._sdecoder = None
        self._lock = threading.Lock()
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
            stdin=subprocess.PIPE, stdout=pipe_w, stderr=subprocess.DEVNULL
        )
        os.close(pipe_w)

        self._sdecoder = subprocess.Popen(
            [sdecoder_path, '/dev/stdin', '/dev/stdout'],
            stdin=pipe_r, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        os.close(pipe_r)

        self._started = True

    def decode(self, acelp_data):
        with self._lock:
            if not self._started:
                try:
                    self.start()
                except Exception:
                    return None
            try:
                if (self._cdecoder.poll() is not None or
                        self._sdecoder.poll() is not None):
                    self.stop()
                    self.start()

                self._cdecoder.stdin.write(acelp_data)
                self._cdecoder.stdin.flush()
                pcm = self._sdecoder.stdout.read(PCM_OUTPUT_BYTES)
                if pcm and len(pcm) == PCM_OUTPUT_BYTES:
                    return pcm
            except (BrokenPipeError, OSError):
                self.stop()
            return None

    def stop(self):
        self._started = False
        for proc in (self._cdecoder, self._sdecoder):
            if proc:
                try:
                    proc.kill()
                    proc.wait(timeout=1)
                except Exception:
                    pass
        self._cdecoder = None
        self._sdecoder = None


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def parse_audio_from_udp(data):
    tra_pos = data.find(b'TRA:')
    if tra_pos < 0:
        return None
    payload = data[tra_pos:]
    if len(payload) < AUDIO_HEADER_SIZE + ACELP_FRAME_SIZE:
        return None
    match = AUDIO_PATTERN.match(payload)
    if not match:
        return None
    return payload[AUDIO_HEADER_SIZE:AUDIO_HEADER_SIZE + ACELP_FRAME_SIZE]


def parse_metadata_from_udp(data):
    begin = data.find(b'TETMON_begin')
    if begin < 0:
        func_pos = data.find(b'FUNC:')
        if func_pos < 0:
            return None
        payload = data[func_pos:]
    else:
        end = data.find(b'TETMON_end', begin)
        if end < 0:
            payload = data[begin + len(b'TETMON_begin'):]
        else:
            payload = data[begin + len(b'TETMON_begin'):end]
    payload = payload.strip()

    fields = parse_tetmon_fields(payload)
    func = fields.get('FUNC', '')
    func_match = re.search(rb'FUNC:(\S+(?:\s+(?!SSI:|IDX:|IDT:|ENCR:|RX:|CID:|NID:|CCODE:|MCC:|MNC:)\S+)*)', payload)
    if func_match:
        func = func_match.group(1).decode()

    if func == 'NETINFO1':
        mcc_raw = fields.get('MCC', '0')
        mnc_raw = fields.get('MNC', '0')
        try:
            mcc = int(mcc_raw, 16)
            mnc = int(mnc_raw, 16)
        except ValueError:
            mcc = int(mcc_raw) if mcc_raw.isdigit() else 0
            mnc = int(mnc_raw) if mnc_raw.isdigit() else 0
        ccode_raw = fields.get('CCODE', '0')
        try:
            color_code = int(ccode_raw, 16)
        except ValueError:
            color_code = int(ccode_raw) if ccode_raw.isdigit() else 0
        crypt = int(fields.get('CRYPT', '0'))
        tea_names = {0: "none", 1: "TEA1", 2: "TEA2", 3: "TEA3"}
        return {
            "protocol": "TETRA",
            "type": "netinfo",
            "mcc": mcc,
            "mnc": mnc,
            "dl_freq": int(fields.get('DLF', '0')),
            "ul_freq": int(fields.get('ULF', '0')),
            "color_code": color_code,
            "encrypted": crypt > 0,
            "encryption_type": tea_names.get(crypt, f"unknown({crypt})"),
            "la": fields.get('LA', ''),
        }

    if func == 'FREQINFO1':
        return {
            "protocol": "TETRA",
            "type": "freqinfo",
            "dl_freq": int(fields.get('DLF', '0')),
            "ul_freq": int(fields.get('ULF', '0')),
        }

    if func == 'DSETUPDEC':
        return {
            "protocol": "TETRA",
            "type": "call_setup",
            "ssi": int(fields.get('SSI', '0')),
            "ssi2": int(fields.get('SSI2', '0')),
            "call_id": int(fields.get('CID', '0')),
            "idx": int(fields.get('IDX', '0')),
        }

    if func in ('DRELEASEDEC', 'D-RELEASE'):
        return {
            "protocol": "TETRA",
            "type": "call_release",
            "ssi": int(fields.get('SSI', '0')),
            "call_id": int(fields.get('CID', '0')),
        }

    if func == 'DCONNECTDEC':
        result = {
            "protocol": "TETRA",
            "type": "call_connect",
            "ssi": int(fields.get('SSI', '0')),
            "call_id": int(fields.get('CID', '0')),
            "idx": int(fields.get('IDX', '0')),
        }
        if 'SSI2' in fields:
            result["ssi2"] = int(fields['SSI2'])
        return result

    if func == 'DTXGRANTDEC':
        result = {
            "protocol": "TETRA",
            "type": "tx_grant",
            "ssi": int(fields.get('SSI', '0')),
            "call_id": int(fields.get('CID', '0')),
            "idx": int(fields.get('IDX', '0')),
        }
        if 'SSI2' in fields:
            result["ssi2"] = int(fields['SSI2'])
        return result

    if func == 'ENCINFO1':
        return {
            "protocol": "TETRA",
            "type": "encinfo",
            "encrypted": int(fields.get('CRYPT', '0')) > 0,
            "enc_mode": fields.get('ENC', '00'),
        }

    if func == 'DSTATUSDEC':
        return {
            "protocol": "TETRA",
            "type": "status",
            "ssi": int(fields.get('SSI', '0')),
            "ssi2": int(fields.get('SSI2', '0')),
            "status": fields.get('STATUS', ''),
        }

    if func == 'BURST':
        return {
            "protocol": "TETRA",
            "type": "burst",
        }

    if func == 'SDSDEC':
        return {
            "protocol": "TETRA",
            "type": "sds",
            "ssi": int(fields.get('SSI', '0')),
            "ssi2": int(fields.get('SSI2', '0')),
        }

    if func.startswith('D-') and 'IDT' in fields:
        ssi = int(fields.get('SSI', '0'))
        ssi2 = int(fields.get('SSI2', '0')) if 'SSI2' in fields else 0
        if ssi > 0 or ssi2 > 0:
            result = {
                "protocol": "TETRA",
                "type": "resource",
                "func": func,
                "ssi": ssi,
                "idt": int(fields.get('IDT', '0')),
            }
            if ssi2 > 0:
                result["ssi2"] = ssi2
            return result

    return None


def emit_meta(meta_dict):
    try:
        line = json.dumps(meta_dict) + '\n'
        sys.stderr.write(line)
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

    demod = subprocess.Popen(
        ['python3', demod_path],
        stdin=0,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env
    )

    tetra_rx_cmd = [tetra_rx_path, '-r', '-s', '-e', '/dev/stdin']
    if os.path.isfile(keyfile):
        tetra_rx_cmd.extend(['-k', keyfile])

    tetra_rx = subprocess.Popen(
        tetra_rx_cmd,
        stdin=demod.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env
    )

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

    re_sync = re.compile(r'TN \d+\((\d+)\)')
    re_access_dl = re.compile(r'DL_USAGE:\s*(\S+)')
    re_access_a1 = re.compile(r'ACCESS1:\s*A/(\d+)')
    re_basicinfo = re.compile(r'Basicinfo:0x([0-9A-Fa-f]{2})')

    def decode_call_type(basicinfo_byte):
        cmt = (basicinfo_byte >> 5) & 0x07
        comm = basicinfo_byte & 0x0F
        types = {0: "individual", 1: "group", 2: "broadcast",
                 3: "acknowledged group"}
        cmt_str = types.get(cmt, "other")
        if comm == 1:
            cmt_str += " TEA1"
        elif comm == 2:
            cmt_str += " TEA2"
        elif comm == 3:
            cmt_str += " TEA3"
        return cmt_str

    def parse_tetra_rx_stdout():
        fd = tetra_rx.stdout.fileno()
        try:
            while True:
                chunk = os.read(fd, 16384)
                if not chunk:
                    break
                text = chunk.decode(errors='replace')
                for m in re_sync.finditer(text):
                    tn = int(m.group(1))
                    if tn == 0:
                        tn = 1
                    current_tn[0] = tn

                for line in text.split('\n'):
                    if 'ACCESS-ASSIGN' in line:
                        tn = current_tn[0]
                        if not (1 <= tn <= 4):
                            continue
                        with state_lock:
                            m = re_access_dl.search(line)
                            if m:
                                usage = m.group(1)
                                ts_usage[tn] = "unallocated" if usage.startswith('U') else "assigned"
                                continue
                            m = re_access_a1.search(line)
                            if m:
                                val = int(m.group(1))
                                ts_usage[tn] = "assigned" if 1 <= val <= 3 else "unallocated"

                    if 'Basicinfo' in line:
                        m = re_basicinfo.search(line)
                        if m:
                            bi = int(m.group(1), 16)
                            with state_lock:
                                call_type_info[0] = decode_call_type(bi)

        except (ValueError, OSError):
            pass

    def read_demod_stderr():
        try:
            for line in demod.stderr:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if 'afc' in data:
                        with state_lock:
                            afc_value[0] = data['afc']
                except (json.JSONDecodeError, Exception):
                    pass
        except (ValueError, OSError):
            pass

    stdout_thread = threading.Thread(target=parse_tetra_rx_stdout, daemon=True)
    stdout_thread.start()
    demod_stderr_thread = threading.Thread(target=read_demod_stderr, daemon=True)
    demod_stderr_thread.start()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('127.0.0.1', udp_port))
    sock.settimeout(0.1)

    silence_20ms = b'\x00' * 320
    last_audio_time = time.monotonic()
    SILENCE_INTERVAL = 0.020

    last_emit_time = {}
    RATE_LIMITS = {
        "burst": 0.5,
        "netinfo": 5.0,
        "freqinfo": 10.0,
        "encinfo": 5.0,
    }

    while running:
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            now = time.monotonic()
            if now - last_audio_time > SILENCE_INTERVAL:
                try:
                    sys.stdout.buffer.write(silence_20ms)
                    sys.stdout.buffer.flush()
                    last_audio_time = now
                except (BrokenPipeError, OSError):
                    running = False
            continue
        except Exception:
            break

        if tetra_rx.poll() is not None:
            break

        meta = parse_metadata_from_udp(data)
        if meta is not None:
            now = time.monotonic()
            msg_type = meta.get("type")
            rate_limit = RATE_LIMITS.get(msg_type, 0)
            last_t = last_emit_time.get(msg_type, 0)

            if now - last_t >= rate_limit:
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

                if msg_type == "call_setup":
                    with state_lock:
                        if call_type_info[0]:
                            meta["call_type"] = call_type_info[0]

                emit_meta(meta)
                last_emit_time[msg_type] = now

        acelp_data = parse_audio_from_udp(data)
        if acelp_data is not None:
            pcm = codec.decode(acelp_data)
            if pcm:
                try:
                    sys.stdout.buffer.write(pcm)
                    sys.stdout.buffer.flush()
                    last_audio_time = time.monotonic()
                except (BrokenPipeError, OSError):
                    running = False
        else:
            now = time.monotonic()
            if now - last_audio_time > SILENCE_INTERVAL:
                try:
                    sys.stdout.buffer.write(silence_20ms)
                    sys.stdout.buffer.flush()
                    last_audio_time = now
                except (BrokenPipeError, OSError):
                    running = False

    sock.close()
    codec.stop()
    for proc in (tetra_rx, demod):
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


if __name__ == '__main__':
    main()
