"""Encode Kokoro's float audio samples to 16-bit PCM WAV bytes.

Pure stdlib (wave + array) so it needs neither torch nor numpy and can be
unit-tested in any environment. Kokoro emits float audio at 24 kHz.
"""

import array
import io
import wave
from typing import Iterable

SAMPLE_RATE: int = 24000


def float_to_wav_bytes(samples: Iterable[float], sample_rate: int = SAMPLE_RATE) -> bytes:
    """Convert an iterable of floats in [-1.0, 1.0] to mono 16-bit PCM WAV bytes.

    Values outside [-1.0, 1.0] are clamped. Symmetric scaling (×32767) keeps
    +1.0 and -1.0 at equal magnitude.
    """
    pcm = array.array("h")
    for s in samples:
        if s > 1.0:
            s = 1.0
        elif s < -1.0:
            s = -1.0
        pcm.append(int(s * 32767))

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()
