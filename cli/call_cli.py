#!/usr/bin/env python3
"""Place an outbound call on the SIP trunk, with a spoken brief for the agent.

The point of a separate dialer: dialling a number into a room is easy, but the
agent that answers has no idea why the call was made. This one carries the
*reason* with the call.

How the brief travels: the room is created FIRST with a JSON brief in its
metadata, and only then is the SIP participant added. The agent worker auto-joins
the room and reads that metadata at entrypoint, so an outbound call arrives with
its own purpose attached:

    --mode callme    the callee is the operator; optional instruction is why
    --mode call      the callee is a third party; instruction is the reason
                     for the call and what to accomplish
    --mode room      plain dial, no brief

The dialer does NOT wait for the answer (LiveKit's ringing timeout would fail a
call the person simply took a while to pick up); it returns once the INVITE is
out, after a short watch on the leg so the caller hears "ringing" or "the call
did not go through" instead of a guess. The agent side is what waits for the
answer: agent.py greets only on sip.callStatus=active, so the opening line lands
on the person who picked up, however long that takes.

Usage:
    venv/bin/python cli/call_cli.py --to 0456789012 --instruction "confirm the booking"
    venv/bin/python cli/call_cli.py --to +15551234567 --mode callme --json
    venv/bin/python cli/call_cli.py --list
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "agent"))

from env import (  # noqa: E402
    digits,
    env,
    load_env_file,
    mask,
)

load_env_file()

from livekit.api import LiveKitAPI  # noqa: E402
from livekit.protocol import room as proom  # noqa: E402
from livekit.protocol import sip as psip  # noqa: E402

# How long to watch the leg after the INVITE, so a bad number or a busy line is
# reported as a failure rather than as a successful ring.
CONFIRM_WINDOW_S = 12.0

# The trunk the bootstrap created. Rename both sides together, or override here.
TRUNK_NAME = env("SIP_OUTBOUND_TRUNK", "photon-outbound")
SIP_IDENTITY = env("SIP_IDENTITY", "phone-user")
SIP_NAME = env("SIP_PARTICIPANT_NAME", "Phone User")
# Country code for numbers typed in local form (0xxxxxxxxx). Leave empty to
# require full international form: guessing a country here dials the wrong
# country, which costs money and is embarrassing.
DEFAULT_COUNTRY_CODE = digits(env("DEFAULT_COUNTRY_CODE", ""))

E164 = re.compile(r"^\+[1-9]\d{7,14}$")


def normalise_number(raw: str) -> str:
    """Loose input -> E.164, using DEFAULT_COUNTRY_CODE for local numbers.

    0456 789 012 -> +61456789012 (cc=61);  0061... -> +61...
    """
    given = (raw or "").strip()
    s = re.sub(r"[^\d+]", "", given)
    if not s:
        raise ValueError(
            "no number given" if not given else f"{given!r} does not look like a dialable number"
        )
    if s.startswith("00"):
        s = "+" + s[2:]
    if not s.startswith("+"):
        if s.startswith("0"):
            if not DEFAULT_COUNTRY_CODE:
                raise ValueError(
                    f"{raw!r} is in local form but DEFAULT_COUNTRY_CODE is not set - "
                    "give the number in international form (+<country><number>) or set "
                    "DEFAULT_COUNTRY_CODE in .env"
                )
            s = "+" + DEFAULT_COUNTRY_CODE + s[1:]
        else:
            s = "+" + s
    if not E164.match(s):
        raise ValueError(f"not a dialable number: {raw!r}")
    return s


def build(cls, **kwargs):
    """Pass only the fields this livekit-api version actually supports."""
    known = {f.name for f in cls.DESCRIPTOR.fields}
    used = {k: v for k, v in kwargs.items() if k in known}
    return cls(**used)


async def list_trunks(api: LiveKitAPI) -> int:
    trunks = (await api.sip.list_outbound_trunk(psip.ListSIPOutboundTrunkRequest())).items
    print("=== outbound trunks ===")
    for t in trunks:
        print(f"  {t.sip_trunk_id}  {t.name!r}  {t.address}  "
              f"{psip.SIPTransport.Name(t.transport)}  numbers={list(t.numbers)}")
    if not trunks:
        print("  (none - run bootstrap/20-sip-objects.py)")
    rooms = (await api.room.list_rooms(proom.ListRoomsRequest())).rooms
    print("=== active rooms ===")
    for r in rooms:
        print(f"  {r.name!r}  participants={r.num_participants}  metadata={r.metadata[:80]!r}")
    if not rooms:
        print("  (none)")
    return 0


async def _leg_state(api: LiveKitAPI, room: str) -> tuple[bool, str, str]:
    """The phone leg's presence and SIP state, as the server sees it.

    sip.callStatus runs dialing -> ringing -> active (then hangup); on a failure
    the participant disappears and sip.status carries the SIP code.
    """
    resp = await api.room.list_participants(proom.ListParticipantsRequest(room=room))
    for participant in resp.participants:
        if participant.identity == SIP_IDENTITY:
            attrs = dict(participant.attributes or {})
            return True, attrs.get("sip.callStatus", ""), attrs.get("sip.status", "")
    return False, "", ""


async def _confirm_leg(api: LiveKitAPI, room: str,
                       window_s: float = CONFIRM_WINDOW_S) -> tuple[str, str, str]:
    """Watch the leg briefly and report what it is actually doing.

    Accepting the INVITE is not the same as reaching a person, so 'ok' on its own
    says nothing: a leg that is removed from the room failed (bad number, busy,
    carrier rejection), one sitting at dialing/ringing is on its way, and
    sip.callStatus=active means someone picked up.
    """
    start = time.monotonic()
    status = ""
    sip_status = ""
    while True:
        present, status, sip_status = await _leg_state(api, room)
        if not present:
            # A short grace period: the participant can lag the RPC by a moment.
            if time.monotonic() - start < 3.0:
                await asyncio.sleep(0.5)
                continue
            return "failed", status, sip_status
        if status == "active":
            return "answered", status, sip_status
        if time.monotonic() - start >= window_s:
            return "ringing", status, sip_status
        await asyncio.sleep(0.5)


async def place_call(api: LiveKitAPI, number: str, instruction: str, mode: str,
                     room_name: str | None, ring_timeout: int,
                     wait_until_answered: bool) -> dict:
    trunks = (await api.sip.list_outbound_trunk(psip.ListSIPOutboundTrunkRequest())).items
    trunk = next((t for t in trunks if t.name == TRUNK_NAME), None) or (trunks[0] if trunks else None)
    if trunk is None:
        return {"ok": False,
                "error": "no outbound trunk configured - run bootstrap/20-sip-objects.py"}

    brief = {
        "mode": mode,
        "instruction": (instruction or "").strip(),
        "callee": number,
        "placed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "placed_by": env("VOICE_PLACED_BY", "cli"),
    }
    room = room_name or f"outbound-{int(time.time())}"

    # Room first, brief in its metadata, so the agent has the reason before it
    # joins. departure_timeout keeps the room from lingering long after a hangup.
    await api.room.create_room(build(
        proom.CreateRoomRequest,
        name=room,
        metadata=json.dumps(brief),
        empty_timeout=120,
        departure_timeout=30,
    ))

    req = build(
        psip.CreateSIPParticipantRequest,
        sip_trunk_id=trunk.sip_trunk_id,
        sip_call_to=number,
        room_name=room,
        participant_identity=SIP_IDENTITY,
        participant_name=SIP_NAME,
        participant_metadata=json.dumps(brief),
        participant_attributes={"call.mode": mode, "call.callee": number},
        # ringing_timeout is a protobuf Duration, so it wants a timedelta.
        ringing_timeout=timedelta(seconds=ring_timeout),
        wait_until_answered=wait_until_answered,
    )

    try:
        info = await api.sip.create_sip_participant(req)
    except Exception as exc:  # noqa: BLE001
        # Leave no empty room (and no agent job) behind on a failed dial.
        try:
            await api.room.delete_room(proom.DeleteRoomRequest(room=room))
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "room": room}

    if wait_until_answered:
        state, status, sip_status = "answered", "active", ""
    else:
        state, status, sip_status = await _confirm_leg(api, room)

    return {
        "ok": True,
        "state": state,
        "call_status": status,
        "sip_status": sip_status,
        "answered": state == "answered",
        "room": info.room_name or room,
        "participant_id": info.participant_id,
        "sip_call_id": info.sip_call_id,
        "callee": number,
        "mode": mode,
    }


async def main() -> int:
    ap = argparse.ArgumentParser(description="Outbound call on the SIP line, with a brief.")
    ap.add_argument("--to", help="number to dial (E.164, or local form with DEFAULT_COUNTRY_CODE)")
    ap.add_argument("--instruction", default="", help="why the call is being made / what to accomplish")
    ap.add_argument("--mode", default="call", choices=["call", "callme", "room"])
    ap.add_argument("--room", default=None)
    ap.add_argument("--ring-timeout", type=int, default=45,
                    help="max seconds to let it ring before giving up (default 45)")
    ap.add_argument("--wait", action="store_true",
                    help="block until the call is answered (fails on a slow pickup)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    missing = [k for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
               if not env(k)]
    if missing:
        msg = f"missing from the environment: {', '.join(missing)}"
        print(json.dumps({"ok": False, "error": msg}) if args.json else msg)
        return 2

    api = LiveKitAPI(env("LIVEKIT_URL"), env("LIVEKIT_API_KEY"), env("LIVEKIT_API_SECRET"))
    try:
        if args.list:
            return await list_trunks(api)
        if not args.to:
            print(json.dumps({"ok": False, "error": "no --to number given"}) if args.json
                  else "usage: call_cli.py --to <number> [--instruction '...'] [--mode call|callme]")
            return 2
        try:
            number = normalise_number(args.to)
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}) if args.json
                  else f"bad number: {exc}")
            return 2

        result = await place_call(api, number, args.instruction, args.mode, args.room,
                                  args.ring_timeout, args.wait)
        if args.json:
            print(json.dumps({**result, "callee_masked": mask(number)}))
        elif result["ok"]:
            print(f"dialing {mask(number)}")
            print(f"  room   : {result['room']}")
            print(f"  mode   : {result['mode']}  instruction={args.instruction!r}")
            print(f"  sip id : {result['sip_call_id']}")
            print(f"  state  : {result['state']}"
                  f" (sip.callStatus={result.get('call_status') or 'n/a'}"
                  f"{', sip.status=' + result['sip_status'] if result.get('sip_status') else ''})")
            if result["state"] == "ringing":
                print("  ringing - the agent speaks the moment they pick up.")
            elif result["state"] == "answered":
                print("  answered - the agent is on the line.")
            print("  the agent worker auto-joins this room and follows the brief.")
        else:
            print(f"CALL FAILED: {result['error']}")
            print("  if this is a timeout/503, the SIP legs could not exchange media:")
            print("  check the cloud security list AND the host firewall both allow the")
            print("  RTP UDP range from .env (RTP_PORT_RANGE) inbound.")
        return 0 if result["ok"] else 1
    finally:
        await api.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
