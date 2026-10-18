"""Drive the post-call briefing watcher end to end with a fake plugin context.

Mirrors the ordering a real call produced: the hangup record lands, then the SDK
flushes the agent's closing line AFTER it, so the watcher must settle on the ended
timestamp rather than requiring `ended` to be the last record.
"""
import importlib.util
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from pathlib import Path

PLUGIN = os.environ.get("VOICE_CALL_PLUGIN") or str(
    Path(__file__).resolve().parent.parent / "surface" / "hermes-plugin" / "__init__.py")
spec = importlib.util.spec_from_file_location("voice_call_plugin", PLUGIN)
vc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vc)

vc.BRIEF_POLL_S = 1  # don't sit for 5s per poll in a test
vc.BRIEF_SETTLE_S = 4

ROOM = "selftest-watch"
NUMBER = "+61412345676"
PATH = os.path.join(vc.CALL_LOG_DIR, f"{ROOM}.jsonl")


class FakeCtx:
    def __init__(self):
        self.calls = []

    def inject_message(self, content, role="user", session_key=None):
        self.calls.append({"content": content, "role": role, "session_key": session_key})
        return True


def stamp(offset_s=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).isoformat(
        timespec="seconds").replace("+00:00", "+00:00")


def append(rec):
    with open(PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


os.makedirs(vc.CALL_LOG_DIR, exist_ok=True)
if os.path.exists(PATH):
    os.unlink(PATH)
append({"at": stamp(-90), "kind": "call", "mode": "call",
        "instruction": "ask whether the 3pm appointment is still on", "callee": NUMBER})
append({"at": stamp(-60), "kind": "answered", "waited_s": 29.8})
append({"at": stamp(-58), "kind": "said",
        "text": "Hi, this is the owner's assistant calling about the 3pm appointment."})

ctx = FakeCtx()
vc._CTX = ctx
vc._spawn_briefing({"ok": True, "room": ROOM, "state": "answered"}, NUMBER, "call")

# A call that is still up must not produce a briefing.
time.sleep(3)
assert not ctx.calls, f"briefing posted while the call was still live: {ctx.calls}"
print("ok: nothing posted while the line was still up")

# The hangup lands, and only afterwards does the closing line get flushed.
append({"at": stamp(-5), "kind": "heard", "text": "yes it is still on", "final": True})
append({"at": stamp(-4), "kind": "ended",
        "reason": "agent hung up: the message was delivered"})
append({"at": stamp(-3), "kind": "said", "text": "Goodbye then."})

deadline = time.monotonic() + 25
while not ctx.calls and time.monotonic() < deadline:
    time.sleep(0.5)
assert ctx.calls, "the watcher never posted the briefing after the call ended"

posted = ctx.calls[0]
print("ok: briefing injected after the call ended")
print("    role=%r session_key=%r" % (posted["role"], posted["session_key"]))
print("    threads still alive:", [t.name for t in threading.enumerate()
                                    if t.name.startswith("voice-call")])
print()
print(posted["content"])
assert NUMBER not in posted["content"], "full number leaked into the injected text"
assert "Goodbye then." in posted["content"], "closing line flushed after the hangup is missing"
assert posted["content"].rstrip().endswith("the message was delivered."), \
    "the Ended line should be the last line of the briefing"
print("\nok: closing line kept, Ended line last, number masked")

before = len(ctx.calls)
time.sleep(2)
assert len(ctx.calls) == before, "the watcher posted more than once"
print("ok: one briefing per call")
os.unlink(PATH)
print("cleaned up", PATH)
