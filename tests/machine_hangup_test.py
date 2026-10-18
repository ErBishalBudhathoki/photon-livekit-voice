"""In-process check of the voicemail/machine hangup path in agent.py.

Drives Assistant.end_call without a live session: the hangup itself is stubbed,
so nothing touches LiveKit. Exercises the verdict helper, the early-verdict
refusal and the fact that a machine verdict bypasses the 12s talk guard while a
normal reason still hits it.
"""
import asyncio
import sys
from pathlib import Path
import time

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agent"))
import agent  # noqa: E402

FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {got!r}")
    if not ok:
        FAILS.append(f"{label}: got {got!r}, want {want!r}")


def raw_tool(tool):
    """The plain coroutine behind a @function_tool wrapper."""
    for attr in ("_func", "func", "__wrapped__", "original", "_original"):
        fn = getattr(tool, attr, None)
        if callable(fn):
            return fn
    raise AssertionError(f"no raw function on {tool!r}")


def make(answered_ago=None, said=0):
    a = agent.Assistant.__new__(agent.Assistant)
    a._answered_at = None if answered_ago is None else time.monotonic() - answered_ago
    a._said_turns = said
    a._transcript = None
    a._delegations = set()
    hung = []

    async def fake_hangup():
        hung.append(True)

    a._hangup = fake_hangup
    return a, hung


end_call = raw_tool(agent.Assistant.end_call)


async def main():
    # 1. the verdict helper
    check("voicemail reason", agent._machine_verdict("voicemail"), True)
    check("prose verdict",
          agent._machine_verdict("Reached voicemail, cannot complete call."), True)
    check("automated menu", agent._machine_verdict("auto attendant menu"), True)
    check("digit readout", agent._machine_verdict("answering machine read the number"), True)
    check("goodbye is not a machine", agent._machine_verdict("said goodbye"), False)
    check("declined is not a machine", agent._machine_verdict("they declined"), False)

    # 2. machine verdict too early -> refused, line stays up
    a, hung = make(answered_ago=3.0, said=0)
    r = await end_call(a, reason="voicemail")
    check("machine at 3s refused", r.reply_required, True)
    check("machine at 3s did not hang up", hung, [])

    # 3. machine verdict after the floor -> allowed even though nothing was said
    a, hung = make(answered_ago=8.0, said=0)
    r = await end_call(a, reason="voicemail")
    await asyncio.sleep(0)
    check("machine at 8s not refused", r.reply_required, False)
    check("machine at 8s hung up", hung, [True])

    # 4. the old guard still holds a normal early hangup back
    a, hung = make(answered_ago=5.0, said=0)
    r = await end_call(a, reason="done")
    check("normal at 5s with nothing said refused", r.reply_required, True)
    check("normal at 5s did not hang up", hung, [])

    # 5. ...and lets a real exchange end normally
    a, hung = make(answered_ago=13.0, said=2)
    r = await end_call(a, reason="said goodbye")
    await asyncio.sleep(0)
    check("normal at 13s after speaking not refused", r.reply_required, False)
    check("normal at 13s hung up", hung, [True])

    # 6. inbound (no answer timestamp) is never held
    a, hung = make(answered_ago=None, said=0)
    r = await end_call(a, reason="caller asked me to hang up")
    await asyncio.sleep(0)
    check("inbound not refused", r.reply_required, False)
    check("inbound hung up", hung, [True])

    print("\nFAILURES:", FAILS or "none")
    return 1 if FAILS else 0


sys.exit(asyncio.run(main()))
