from csdr.chain.demodulator import BaseDemodulatorChain, FixedIfSampleRateChain, FixedAudioRateChain, MetaProvider

try:
    from csdr.module.tetra import TetraDecoderModule
except ImportError:
    from csdr.modules.tetra import TetraDecoderModule


class Tetra(BaseDemodulatorChain, FixedIfSampleRateChain, FixedAudioRateChain, MetaProvider):
    """TETRA demodulator chain for OpenWebRX+.

    Input:  complex float IQ at 36000 S/s (pi/4-DQPSK, 25 kHz channel)
    Output: 16-bit signed LE PCM at 8000 Hz (ACELP decoded)
    Meta:   JSON metadata via stderr (TETMON signaling)

    FixedIfSampleRateChain tells OpenWebRX+ to decimate IQ to 36 kS/s
    before passing to this chain. Without it, IQ arrives at full SDR
    sample rate (e.g. 2.4 MHz) and the demodulator fails silently.
    """

    def __init__(self):
        self._decoder = TetraDecoderModule()
        super().__init__([self._decoder])

    def getFixedIfSampleRate(self):
        return 36000

    def getFixedAudioRate(self):
        return 8000

    def supportsSquelch(self):
        return False

    def setMetaWriter(self, writer):
        self._decoder.setMetaWriter(writer)

    def stop(self):
        if hasattr(self, "_decoder") and self._decoder is not None:
            self._decoder.stop()
        super().stop()
