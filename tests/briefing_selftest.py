"""Exercise the voice-call plugin's briefing formatter against synthetic transcripts."""
import importlib.util
import json
import os
import sys

from pathlib import Path

PLUGIN = os.environ.get("VOICE_CALL_PLUGIN") or str(
    Path(__file__).resolve().parent.parent / "surface" / "hermes-plugin" / "__init__.py")
spec = importlib.util.spec_from_file_location("voice_call_plugin", PLUGIN)
vc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vc)

ROOM = "selftest-answered"
ROOM2 = "selftest-unanswered"
ROOM3 = "selftest-partial"


def write(room, recs):
    os.makedirs(vc.CALL_LOG_DIR, exist_ok=True)
    with open(os.path.join(vc.CALL_LOG_DIR, f"{room}.jsonl"), "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


write(ROOM, [
    {"at": "2026-10-18T11:15:58+00:00", "kind": "call", "mode": "call",
     "instruction": "tell him the parcel is at the front desk", "callee": "+61412345676"},
    {"at": "2026-10-18T11:16:36+00:00", "kind": "answered", "waited_s": 36.5},
    {"at": "2026-10-18T11:16:37+00:00", "kind": "said", "text": "Hello, I am an AI assistant calling on the owner's behalf."},
    {"at": "2026-10-18T11:16:41+00:00", "kind": "heard", "text": "six one four one", "final": False},
    {"at": "2026-10-18T11:16:43+00:00", "kind": "heard", "text": "six one four one four one six six seven six", "final": True},
    {"at": "2026-10-18T11:16:50+00:00", "kind": "said", "text": "Sorry, I think I reached a machine. Goodbye."},
    {"at": "2026-10-18T11:17:03+00:00", "kind": "ended", "reason": "agent hung up: Reached voicemail, cannot complete call."},
])

write(ROOM2, [
    {"at": "2026-10-18T11:15:58+00:00", "kind": "call", "mode": "callme",
     "instruction": "", "callee": "+61412345678"},
    {"at": "2026-10-18T11:16:45+00:00", "kind": "ended", "reason": "call ended (room closed)"},
])

write(ROOM3, [
    {"at": "2026-10-18T11:15:58+00:00", "kind": "call", "mode": "call",
     "instruction": "ask about the invoice", "callee": "+61412345678"},
    {"at": "2026-10-18T11:16:00+00:00", "kind": "answered", "waited_s": 2.0},
    {"at": "2026-10-18T11:16:02+00:00", "kind": "heard", "text": "who is this", "final": False},
])

print("--- answered (with a final and a partial recognition) ---")
print(vc.briefing(ROOM))
print()
print("--- never answered (callme) ---")
print(vc.briefing(ROOM2, "+61412345678", "callme"))
print()
print("--- only a partial recognition, no ended record ---")
print(vc.briefing(ROOM3, "+61412345678", "call"))
print()
print("--- missing room ---")
print(repr(vc.briefing("no-such-room")))
print()
print("--- number masking in the header ---")
assert "61412345676" not in vc.briefing(ROOM), "full number leaked into the briefing"
assert "614****676" in vc.briefing(ROOM)
print("ok: callee shown as", vc.briefing(ROOM).splitlines()[0])
print()
print("--- callee recovered from the transcript when not passed in ---")
print(vc.briefing(ROOM3).splitlines()[0])

for room in (ROOM, ROOM2, ROOM3):
    os.unlink(os.path.join(vc.CALL_LOG_DIR, f"{room}.jsonl"))
print("\ncleaned up selftest transcripts")
