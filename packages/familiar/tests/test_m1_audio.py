"""Audio tests without PortAudio devices."""

import asyncio
import io
import math
import struct
import threading
import wave

import numpy as np
import pytest
from familiar.audio.pan import pan_gains, pan_stereo
from familiar.audio.player import Player
from familiar.audio.recorder import Recorder
from familiar.audio.wav import pcm_to_wav
from familiar.interfaces import AudioChunk, AudioClip, BackendError
from m1_support import run


def test_pan_gains_constant_power():
    left, right = pan_gains(0.0)
    assert left == pytest.approx(math.sqrt(0.5))
    assert right == pytest.approx(left)
    assert pan_gains(-1.0) == pytest.approx((1.0, 0.0))
    assert pan_gains(1.0) == pytest.approx((0.0, 1.0))
    assert pan_gains(-0.6)[0] == pytest.approx(pan_gains(0.6)[1])


def test_pan_stereo_interleaves_and_never_clips():
    pcm = struct.pack("<hhh", 32767, -32768, 1000)
    assert np.frombuffer(pan_stereo(pcm, -1.0), dtype="<i2").tolist() == [
        32767,
        0,
        -32768,
        0,
        1000,
        0,
    ]
    assert np.frombuffer(pan_stereo(pcm, 1.0), dtype="<i2").tolist() == [
        0,
        32767,
        0,
        -32768,
        0,
        1000,
    ]
    centre = np.frombuffer(pan_stereo(pcm, 0.0), dtype="<i2")
    assert centre[0] == centre[1] == round(32767 * math.sqrt(0.5))
    assert abs(centre[2]) <= 32767 and abs(centre[3]) <= 32767
    assert pan_stereo(b"", 0.0) == b""


def test_player_opens_two_channels_only_when_stereo():
    async def scenario():
        made = []

        def factory(**kwargs):
            stream = OutputStream(**kwargs)
            made.append(kwargs)
            return stream

        mono = Player(output_factory=factory)
        await mono.play(chunks(AudioChunk(b"\1\0" * 100, 16000), AudioChunk(b"\1\0", 24000)))
        await mono.wait_idle()
        await mono.aclose()
        stereo = Player(output_factory=factory, stereo=True)
        await stereo.play(chunks(AudioChunk(b"\1\0" * 100, 16000)), pan=-1.0)
        await stereo.wait_idle()
        await stereo.aclose()
        assert [(m["samplerate"], m["channels"]) for m in made] == [
            (16000, 1),
            (24000, 1),
            (16000, 2),
        ]

    run(scenario())


def test_player_pans_each_item_at_its_speakers_seat():
    async def scenario():
        streams = []

        def factory(**kwargs):
            stream = OutputStream(**kwargs)
            streams.append(stream)
            return stream

        player = Player(output_factory=factory, stereo=True)
        sample = b"\xff\x7f" * 50  # full-scale mono
        await player.play(chunks(AudioChunk(sample, 16000)), pan=1.0)
        await player.play(chunks(AudioChunk(sample, 16000)), pan=-1.0)
        await player.wait_idle()
        left, right = (
            np.frombuffer(b"".join(streams[0].writes), dtype="<i2")[channel::2]
            for channel in (0, 1)
        )
        assert left.tolist() == [0] * 50 + [32767] * 50
        assert right.tolist() == [32767] * 50 + [0] * 50
        assert len(streams) == 1  # same rate and channels: no reopen
        await player.aclose()

    run(scenario())


def test_wav_round_trip():
    clip = AudioClip(b"\x01\x02" * 100, 16000)
    with wave.open(io.BytesIO(pcm_to_wav(clip)), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        assert wav.readframes(100) == clip.pcm


class InputStream:
    def __init__(self, **kwargs):
        self.callback = kwargs["callback"]
        self.closed = False

    def start(self):
        self.callback(b"\x01\x00" * 10, 10, None, None)

    def stop(self):
        pass

    def close(self):
        self.closed = True


def test_recorder_injected_stream():
    streams = []

    def factory(**kwargs):
        stream = InputStream(**kwargs)
        streams.append(stream)
        return stream

    recorder = Recorder(stream_factory=factory)
    for _ in range(2):
        recorder.start()
        assert recorder.stop() == AudioClip(b"\x01\x00" * 10, 16000)
    assert all(stream.closed for stream in streams)


class OutputStream:
    def __init__(self, **kwargs):
        self.rate = kwargs["samplerate"]
        self.writes = []
        self.closed = False
        self.aborted = False

    def start(self):
        pass

    def write(self, data):
        self.writes.append(data)

    def stop(self):
        pass

    def abort(self):
        self.aborted = True

    def close(self):
        self.closed = True


async def chunks(*values):
    for value in values:
        yield value


def test_player_reopens_on_rate_change_and_slices():
    async def scenario():
        streams = []

        def factory(**kwargs):
            stream = OutputStream(**kwargs)
            streams.append(stream)
            return stream

        player = Player(output_factory=factory)
        await player.play(chunks(AudioChunk(b"\0" * 6400, 16000), AudioChunk(b"\1\0", 24000)))
        await player.wait_idle()
        assert [stream.rate for stream in streams] == [16000, 24000]
        assert streams[0].closed
        assert b"".join(streams[0].writes) == b"\0" * 6400
        assert all(len(data) <= 1600 for data in streams[0].writes)
        await player.aclose()
        assert streams[1].closed

    run(scenario())


def test_player_stop_drops_queued_and_current_audio():
    async def scenario():
        entered = threading.Event()
        release = threading.Event()
        streams = []

        class BlockingOutput(OutputStream):
            def write(self, data):
                super().write(data)
                entered.set()
                assert release.wait(2)

        def factory(**kwargs):
            stream = BlockingOutput(**kwargs)
            streams.append(stream)
            return stream

        player = Player(output_factory=factory)
        await player.play(chunks(AudioChunk(b"\1\0" * 16000, 16000), AudioChunk(b"\2\0", 16000)))
        assert await asyncio.to_thread(entered.wait, 2)
        player.stop()
        release.set()
        await player.wait_idle()
        assert len(streams[0].writes) == 1
        assert streams[0].aborted
        await player.play(chunks(AudioChunk(b"\3\0", 16000)))
        await player.wait_idle()
        assert streams[-1].writes == [b"\3\0"]
        await player.aclose()

    run(scenario())


def test_player_wait_idle_drains_buffered_output():
    async def scenario():
        calls = []

        class BufferedOutput(OutputStream):
            def stop(self):
                calls.append("drain")

            def abort(self):
                calls.append("abort")

            def close(self):
                calls.append("close")

        player = Player(output_factory=BufferedOutput)
        await player.play(chunks(AudioChunk(b"\1\0", 24000)))
        await player.wait_idle()
        assert calls == ["drain", "close"]
        await player.aclose()

    run(scenario())


def test_player_never_aborts_during_a_write():
    """Aborting PortAudio while another thread writes can hang that write forever."""

    async def scenario():
        entered = threading.Event()
        release = threading.Event()
        aborted = threading.Event()
        state = {"writing": False, "abort_during_write": False}

        class TrackingOutput(OutputStream):
            def write(self, data):
                state["writing"] = True
                self.writes.append(data)
                entered.set()
                assert release.wait(2)
                state["writing"] = False

            def abort(self):
                state["abort_during_write"] |= state["writing"]
                aborted.set()

        player = Player(output_factory=TrackingOutput)
        await player.play(chunks(AudioChunk(b"\1\0" * 24000, 24000)))
        assert await asyncio.to_thread(entered.wait, 2)
        player.stop()
        await asyncio.sleep(0.05)
        assert not aborted.is_set()
        release.set()
        assert await asyncio.to_thread(aborted.wait, 2)
        await player.wait_idle()
        assert not state["abort_during_write"]
        await player.aclose()

    run(scenario())


def test_player_recovers_from_a_stuck_write():
    async def scenario():
        streams = []
        stuck = threading.Event()

        class Output(OutputStream):
            def write(self, data):
                super().write(data)
                if len(streams) == 1:
                    stuck.wait(5)  # first device never returns from write

        def factory(**kwargs):
            stream = Output(**kwargs)
            streams.append(stream)
            return stream

        player = Player(output_factory=factory, write_timeout_s=0.2)
        await player.play(chunks(AudioChunk(b"\1\0", 24000)))
        with pytest.raises(BackendError, match="stopped responding"):
            await player.wait_idle()
        await player.play(chunks(AudioChunk(b"\2\0", 24000)))
        await player.wait_idle()
        assert streams[-1].writes == [b"\2\0"]
        assert len(streams) == 2
        stuck.set()  # the abandoned write finally returns: its stream gets closed
        for _ in range(100):
            if streams[0].closed:
                break
            await asyncio.sleep(0.01)
        assert streams[0].closed
        await player.aclose()

    run(scenario())
