#!/usr/bin/env python3
"""The room test: prove the agent talks, with no phone call and nothing billed.

This is the rung between "the stack is up" and "a real call works", and it is the
one that isolates the agent from telephony. It stands in for the callee:

  * creates the room exactly the way cli/call_cli.py does - brief in the metadata
  * joins as the SIP identity the worker waits for ("phone-user")
  * publishes a silent mic track, so there is a participant to talk to
  * measures the audio the agent sends back

The measurement is the point. "The agent said something" is not a thing you can
assert from a log line, but audio energy is: if the agent never speaks, no frames
carry a loud sample. A pass here means the brief was read, the answered gate
opened, and the model produced audio - leaving only the carrier as a suspect.

    venv/bin/python tests/room_test.py                  # 40s, default instruction
    venv/bin/python tests/room_test.py 60               # hold longer
    venv/bin/python tests/room_test.py 40 --keep-room   # do not delete the room
"""
from __future__ import annotations

import array
import asyncio
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agent"))

from env import load_env_file  # noqa: E402

load_env_file()

from livekit import api, rtc  # noqa: E402
from livekit.protocol import room as proom  # noqa: E402

HOLD_S = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 40
KEEP_ROOM = "--keep-room" in sys.argv
INSTRUCTION = os.environ.get(
    "ROOM_TEST_INSTRUCTION", "ask whether the 3pm appointment is still on")
# Peak sample above which a frame counts as speech rather than line noise.
LOUD = 500

stats = {"frames": 0, "peak": 0, "loud_frames": 0}


async def consume(track: rtc.Track) -> None:
    stream = rtc.AudioStream(track)
    async for ev in stream:
        samples = array.array("h", bytes(ev.frame.data))
        if not samples:
            continue
        peak = max(abs(s) for s in samples)
        stats["frames"] += 1
        stats["peak"] = max(stats["peak"], peak)
        if peak > LOUD:
            stats["loud_frames"] += 1


async def main() -> int:
    missing = [k for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
               if not os.environ.get(k)]
    if missing:
        print(f"missing from the environment: {', '.join(missing)}")
        print("run from the repo root, or with the repo's .env present")
        return 2

    url = os.environ["LIVEKIT_URL"]
    lkapi = api.LiveKitAPI(url, os.environ["LIVEKIT_API_KEY"],
                           os.environ["LIVEKIT_API_SECRET"])
    room_name = f"test-brief-{int(time.time())}"
    brief = {
        "mode": "call",
        "instruction": INSTRUCTION,
        "callee": "+61400000000",
        "placed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "placed_by": "room-test",
    }
    await lkapi.room.create_room(proom.CreateRoomRequest(
        name=room_name, metadata=json.dumps(brief), empty_timeout=60, departure_timeout=20))
    print(f"room created: {room_name}", flush=True)
    print(f"brief: {json.dumps(brief)}", flush=True)

    token = (api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
             .with_identity(os.environ.get("AGENT_SIP_IDENTITY", "phone-user"))
             .with_name("Phone User")
             .with_grants(api.VideoGrants(room_join=True, room=room_name)))
    room = rtc.Room()

    @room.on("track_subscribed")
    def _on_track_subscribed(track, publication, participant):
        print(f"subscribed to {participant.identity}'s {track.kind}", flush=True)
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            asyncio.ensure_future(consume(track))

    @room.on("participant_connected")
    def _on_join(participant):
        print(f"participant joined: {participant.identity}", flush=True)

    await room.connect(url, token.to_jwt())
    print(f"joined as {room.local_participant.identity}", flush=True)

    # A published microphone is what the worker's answered gate is looking for.
    source = rtc.AudioSource(48000, 1)
    track = rtc.LocalAudioTrack.create_audio_track("mic", source)
    await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
    print("published a silent mic track", flush=True)

    async def pump() -> None:
        frame = rtc.AudioFrame.create(48000, 1, 480)
        while True:
            await source.capture_frame(frame)
            await asyncio.sleep(0.01)

    task = asyncio.create_task(pump())
    print(f"listening for {HOLD_S}s ...", flush=True)
    await asyncio.sleep(HOLD_S)
    task.cancel()

    print(f"\nagent audio: frames={stats['frames']} peak={stats['peak']} "
          f"loud_frames={stats['loud_frames']}")
    transcript = REPO / "calls" / f"{room_name}.jsonl"
    if transcript.is_file():
        print(f"transcript: {transcript} ({transcript.stat().st_size} bytes)")

    if not KEEP_ROOM:
        await room.disconnect()
        await lkapi.room.delete_room(proom.DeleteRoomRequest(room=room_name))
    else:
        print(f"keeping the room: {room_name}")
        await room.disconnect()
    await lkapi.aclose()

    # The verdict. No audio at all means the worker never joined, never opened the
    # answered gate, or produced nothing - all three need looking at before a real
    # call, because on a real call you cannot tell which one it was.
    if stats["loud_frames"] == 0:
        print("\nFAIL: the agent produced no audio. Check, in this order:")
        print("  1. was the worker dispatched at all?  journalctl --user -u livekit-agent -n 50")
        print("  2. did it read the brief?            grep brief calls/*.jsonl")
        print("  3. did the answered gate open?       look for 'answered' in the transcript")
        print("  4. is GEMINI_API_KEY valid and under its tokens/min ceiling?")
        return 1
    print(f"\nPASS: the agent spoke ({stats['loud_frames']} frames above the noise floor).")
    print("Telephony is now the only untested layer: try one real call.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
