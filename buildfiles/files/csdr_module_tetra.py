import json
import logging
import pickle
import subprocess
import threading
from csdr.module import PopenModule
from pycsdr.types import Format

logger = logging.getLogger(__name__)

TETRA_DECODER_SCRIPT = "/opt/openwebrx-tetra/tetra_decoder.py"


class TetraDecoderModule(PopenModule):
    """
    Wraps tetra_decoder.py as a subprocess.
    stdin:  complex float32 IQ at 36000 S/s
    stdout: 16-bit signed LE PCM at 8000 Hz
    stderr: JSON metadata lines (TETMON signaling)
    """

    def __init__(self):
        self._meta_thread = None
        self._meta_writer = None
        super().__init__()

    def getInputFormat(self):
        return Format.COMPLEX_FLOAT

    def getOutputFormat(self):
        return Format.SHORT

    def getCommand(self):
        return ["python3", TETRA_DECODER_SCRIPT]

    def _getProcess(self):
        # Override to capture stderr so the metadata thread can read it.
        # PopenModule default only sets stdin=PIPE, stdout=PIPE.
        return subprocess.Popen(
            self.getCommand(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def start(self):
        super().start()
        self._meta_thread = threading.Thread(
            target=self._read_meta,
            daemon=True,
            name="tetra-meta-reader",
        )
        self._meta_thread.start()

    def setMetaWriter(self, writer):
        self._meta_writer = writer

    def _read_meta(self):
        try:
            for raw in self.process.stderr:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    msg["protocol"] = "TETRA"
                    msg["mode"] = "tetra"
                    if self._meta_writer:
                        self._meta_writer.write(pickle.dumps(msg))
                except json.JSONDecodeError:
                    logger.debug("tetra meta (non-JSON): %s", line)
        except Exception as e:
            logger.debug("tetra meta reader exited: %s", e)

    def stop(self):
        if self._meta_thread and self._meta_thread.is_alive():
            self._meta_thread.join(timeout=2)
        super().stop()
