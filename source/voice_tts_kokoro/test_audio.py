import io
import wave

from audio import SAMPLE_RATE, float_to_wav_bytes


def test_wav_header_is_mono_16bit_at_sample_rate():
    data = float_to_wav_bytes([0.0, 0.5, -0.5, 1.0, -1.0], sample_rate=SAMPLE_RATE)
    with wave.open(io.BytesIO(data), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2          # 16-bit
        assert w.getframerate() == SAMPLE_RATE
        assert w.getnframes() == 5


def test_samples_are_scaled_and_clamped_to_int16():
    data = float_to_wav_bytes([0.0, 1.0, -1.0, 2.0, -2.0], sample_rate=SAMPLE_RATE)
    with wave.open(io.BytesIO(data), "rb") as w:
        frames = w.readframes(w.getnframes())
    import array
    pcm = array.array("h")
    pcm.frombytes(frames)
    # 0.0 -> 0, 1.0 -> 32767, -1.0 -> -32767; values > 1.0 clamp to the same.
    assert list(pcm) == [0, 32767, -32767, 32767, -32767]


def test_empty_input_produces_valid_empty_wav():
    data = float_to_wav_bytes([], sample_rate=SAMPLE_RATE)
    with wave.open(io.BytesIO(data), "rb") as w:
        assert w.getnframes() == 0
