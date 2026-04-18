# Locate the correct base chain class across OpenWebRX+ versions.
# Known locations:
#   csdr.chain               (older builds)
#   csdr.chain.demodulator   (newer builds)
_BaseDemodulatorChain = None
for _mod_path, _cls_name in [
    ("csdr.chain",            "BaseDemodulatorChain"),
    ("csdr.chain.demodulator","BaseDemodulatorChain"),
    ("csdr.chain",            "Chain"),
    ("csdr.chain.analog",     "AnalogDemodulator"),
]:
    try:
        import importlib as _il
        _m = _il.import_module(_mod_path)
        _BaseDemodulatorChain = getattr(_m, _cls_name, None)
        if _BaseDemodulatorChain is not None:
            break
    except ImportError:
        continue

if _BaseDemodulatorChain is None:
    raise ImportError(
        "Cannot find a suitable base chain class in csdr.chain — "
        "check the csdr Python package version."
    )

# Locate TetraDecoderModule (csdr.module vs csdr.modules across versions)
_TetraDecoderModule = None
for _mod_path in ("csdr.modules.tetra", "csdr.module.tetra"):
    try:
        import importlib as _il
        _m = _il.import_module(_mod_path)
        _TetraDecoderModule = getattr(_m, "TetraDecoderModule", None)
        if _TetraDecoderModule is not None:
            break
    except ImportError:
        continue

if _TetraDecoderModule is None:
    raise ImportError("Cannot import TetraDecoderModule from csdr.modules.tetra or csdr.module.tetra")


class Tetra(_BaseDemodulatorChain):
    """
    TETRA demodulator chain.
    Input:  complex float IQ at 36000 S/s (pi/4-DQPSK, 25 kHz channel)
    Output: 16-bit signed LE PCM at 8000 Hz (ACELP decoded)
    """

    def __init__(self):
        self._decoder = _TetraDecoderModule()
        super().__init__([self._decoder])

    def supportsSquelch(self):
        return False

    def getInputSampleRate(self):
        return 36000

    def getOutputSampleRate(self):
        return 8000

    def stop(self):
        if hasattr(self, "_decoder") and self._decoder is not None:
            self._decoder.stop()
        super().stop()
