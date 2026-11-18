"""Constant-power stereo placement of mono PCM16 chunks."""

import math

import numpy as np

INT16_MIN, INT16_MAX = -32768, 32767


def pan_gains(pan: float) -> tuple[float, float]:
    """(left, right) gains for pan in [-1, 1]; centre gives both channels ~0.707."""
    angle = (pan + 1.0) * math.pi / 4.0
    return math.cos(angle), math.sin(angle)


def pan_stereo(pcm: bytes, pan: float) -> bytes:
    """Interleaved stereo PCM16 from mono; gains never exceed 1, so no clipping."""
    mono = np.frombuffer(pcm, dtype="<i2")
    gains = np.array(pan_gains(pan))
    stereo = np.rint(mono[:, np.newaxis] * gains[np.newaxis, :])
    return np.clip(stereo, INT16_MIN, INT16_MAX).astype("<i2").tobytes()
