#!/usr/bin/env python3
"""Drive the REAL call_cli.place_call() with only the PSTN leg stubbed out.

Proves the production path end to end without ringing anyone: call_cli creates
the room and writes the brief, a stand-in callee joins as phone-user, and the
agent (restarted with the new code) must read the brief, wait for the callee and
then speak its opening line, which we read back from the journal.
"""
import array
import asyncio
import os
import sys
from pathlib import Path
import time
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent"))
from env import load_env_file  # noqa: E402

load_env_file()

import call_cli  # noqa: E402
from livekit import api, rtc  # noqa: E402
from livekit.api import LiveKitAPI  # noqa: E402
from livekit.api import sip_service  # noqa: E402
from livekit.protocol import room as proom  # noqa: E402

ROOM = f"test-callcli-{int(time.time())}"
NUMBER = "+61400000000"
INSTRUCTION = "ask whether the 3pm appointment is still on"
stats = {"frames": 0, "peak": 0}

# --- stub only the PSTN leg -------------------------------------------------
async def _fake_create_sip_participant(self, create):
    print(f"[stub] would INVITE {create.sip_call_to} into {create.room_name}", flush=True)
    print(f"[stub] participant_metadata={create.participant_metadata}", flush=True)
    print(f"[stub] ringing_timeout={create.ringing_timeout} wait={create.wait_until_answered}", flush=True)
    return SimpleNamespace(room_name=create.room_name, participant_id="PA_test", sip_call_id="SC_test")


sip_service.SipService.create_sip_participant = _fake_create_sip_participant


async def consume(track):
    stream = rtc.AudioStream(track)
    async for ev in stream:
        samples = array.array("h", bytes(ev.frame.data))
        if samples:
            stats["frames"] += 1
            stats["peak"] = max(stats["peak"], max(abs(s) for s in samples))


async def stand_in_callee(url, room_name, hold_s):
    await asyncio.sleep(2)  # let call_cli create the room first
    token = (api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
             .with_identity("phone-user").with_name("Phone User")
             .with_grants(api.VideoGrants(room_join=True, room=room_name)))
    room = rtc.Room()

    @room.on("track_subscribed")
    def _on_track(track, publication, participant):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            asyncio.ensure_future(consume(track))

    await room.connect(url, token.to_jwt())
    print(f"[callee] joined {room_name} as {room.local_participant.identity}", flush=True)
    source = rtc.AudioSource(48000, 1)
    track = rtc.LocalAudioTrack.create_audio_track("mic", source)
    await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))

    async def pump():
        frame = rtc.AudioFrame.create(48000, 1, 480)
        while True:
            await source.capture_frame(frame)
            await asyncio.sleep(0.01)

    task = asyncio.create_task(pump())
    await asyncio.sleep(hold_s)
    task.cancel()
    await room.disconnect()


async def main():
    url = os.environ["LIVEKIT_URL"]
    api_client = LiveKitAPI(url, os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
    callee = asyncio.create_task(stand_in_callee(url, ROOM, 25))
    result = await call_cli.place_call(api_client, NUMBER, INSTRUCTION, "call", ROOM, 18, True)
    print(f"[call_cli] result={result}", flush=True)
    await callee
    print(f"[callee] agent audio frames={stats['frames']} peak={stats['peak']}", flush=True)
    await api_client.room.delete_room(proom.DeleteRoomRequest(room=ROOM))
    await api_client.aclose()


asyncio.run(main())
