from csdr.chain import BaseDemodulatorChain
from csdr.modules.tetra import TetraDecoderModule


class Tetra(BaseDemodulatorChain):
    """
    TETRA demodulator chain.
    Input:  complex float IQ at 36000 S/s (pi/4-DQPSK, 25 kHz channel)
    Output: 16-bit signed LE PCM at 8000 Hz (ACELP decoded)
    """

    def __init__(self):
        self._decoder = TetraDecoderModule()
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
