"""Serialize PCM16 mono clips as WAV."""

import io
import wave

from familiar.interfaces import AudioClip


def pcm_to_wav(clip: AudioClip) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(clip.sample_rate)
        handle.writeframes(clip.pcm)
    return output.getvalue()
