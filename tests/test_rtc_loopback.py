"""End-to-End: Hub/Sink -> PeerAudioTrack/PeerVideoTrack -> WebRTC -> Empfaenger.

Nutzt die echten Track-Klassen des Bots, nur als lokale Schlaufe
(zwei RTCPeerConnections im Prozess, nur UDP-Host-Candidates).
"""
import asyncio
import time

import numpy as np
from aiortc import RTCPeerConnection

from musikbot.hub import Hub
from musikbot.player import PeerAudioTrack, PeerVideoTrack

SR = 48000
FRAME = 960


def _block(t: float) -> np.ndarray:
    phase = 2 * np.pi * 440 * t
    val = int(9000 * np.sin(phase))
    arr = np.full((1, FRAME * 2), val, dtype=np.int16)
    return arr


def _video_frames():
    import av
    for i in range(10_000):
        arr = np.zeros((240, 424, 3), dtype=np.uint8)
        arr[:, :, 1] = (i * 7) % 255
        f = av.VideoFrame.from_ndarray(arr, format="rgb24")
        f.pts = i
        from fractions import Fraction
        f.time_base = Fraction(1, 25)
        yield f


def test_rtc_loopback():
    async def run():
        hub = Hub()
        sink = hub.new_sink()
        pc1 = RTCPeerConnection()
        pc2 = RTCPeerConnection()
        got = {"audio": 0, "video": 0}
        done = asyncio.Event()

        at = PeerAudioTrack(sink)
        vt = PeerVideoTrack(sink, 25.0)
        pc1.addTrack(at)
        pc1.addTrack(vt)

        @pc2.on("track")
        def on_track(track):
            async def pump():
                while got.get(track.kind, 0) < 5:
                    fr = await asyncio.wait_for(track.recv(), 20)
                    assert fr is not None
                    got[track.kind] = got.get(track.kind, 0) + 1
                if got["audio"] >= 5 and got["video"] >= 5:
                    done.set()
            asyncio.ensure_future(pump())

        await pc1.setLocalDescription(await pc1.createOffer())
        await pc2.setRemoteDescription(pc1.localDescription)
        await pc2.setLocalDescription(await pc2.createAnswer())
        await pc1.setRemoteDescription(pc2.localDescription)

        frames = _video_frames()
        t0 = time.monotonic()
        feed = asyncio.ensure_future(_feed(hub, frames, t0, done))
        await asyncio.wait_for(done.wait(), 30)
        feed.cancel()
        await pc1.close()
        await pc2.close()
        assert got["audio"] >= 5, got
        assert got["video"] >= 5, got

    asyncio.run(run())


async def _feed(hub, frames, t0, done):
    n = 0
    try:
        while not done.is_set():
            target = t0 + (n + 1) * FRAME / SR
            hub.push_audio(_block(n * FRAME / SR))
            if n % 2 == 0:
                hub.push_video(next(frames))
            n += 1
            delay = target - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
    except StopIteration:
        pass
