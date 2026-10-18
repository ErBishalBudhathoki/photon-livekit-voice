"""LiveKit voice agent: a phone call answered by Gemini Live (speech to speech).

One worker process. It joins every room LiveKit dispatches to it, reads the
per-call brief out of the room metadata, waits until the phone leg is actually
ANSWERED, then holds a spoken conversation over the SIP bridge. Voice is
Gemini's Live API end to end - raw audio in, raw audio out over a single
WebSocket - so there is no local STT, TTS or VAD in the path at all and the
Live model owns turn detection and barge-in.

Everything deployment-specific comes from the environment. Nothing in this file
is meant to be edited per install; see .env.example for the contract.

Required:
  LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET   the LiveKit this worker joins
  GEMINI_API_KEY                                     Live API key (s2s)

Optional (defaults shown):
  AGENT_S2S_MODEL=gemini-3.8-live     Live model
  AGENT_S2S_VOICE=Puck                Live voice name
  AGENT_WORKER_PORT=8083              worker health/registration HTTP port
  SIP_IDENTITY=phone-user             identity the dialer gives the phone leg
  AGENT_CALL_LOG_DIR=<repo>/calls     one JSON-lines transcript per room
  AGENT_MIN_TALK_S=12                 an answered outbound call may not be hung
                                      up before this, and not before the agent
                                      has said something
  AGENT_MACHINE_MIN_S=6               earliest a "that is a machine" verdict is
                                      trusted (recorded greetings and people are
                                      indistinguishable before this)
  AGENT_HANGUP_GRACE_S=3.5            keep the line up this long after end_call
                                      so the goodbye is heard
  AGENT_OWNER_LABEL="your owner"      how the persona refers to the operator
  AGENT_PERSONA_FILE=                 optional extra instructions file
  AGENT_INBOUND_GREETING=             optional first line for inbound calls
  AGENT_DELEGATION_ENABLED=false      hand caller requests to a local agent CLI
  AGENT_DELEGATION_ALLOWLIST=         comma-separated caller numbers allowed to
                                      delegate. EMPTY MEANS NOBODY (fail closed)
  AGENT_HERMES_BIN=/usr/local/bin/hermes
  AGENT_HERMES_SESSION=phone
  AGENT_HERMES_TIMEOUT=120
  AGENT_HERMES_HOME=                  HOME for the delegated CLI (default: $HOME)

Run:  venv/bin/python agent.py start
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from env import (  # noqa: E402
    env,
    env_bool,
    env_float,
    env_int,
    load_env_file,
    mask,
    numbers_match,
)

load_env_file()

from livekit import agents  # noqa: E402
from livekit.agents import (  # noqa: E402
    Agent,
    AgentSession,
    WorkerOptions,
    cli,
    function_tool,
)
from livekit.agents.llm import ToolResult  # noqa: E402
from livekit.plugins import google  # noqa: E402

# The Live API (speech-to-speech) path. genai_types supplies the config objects
# the realtime model takes; google.realtime is the plugin's realtime surface.
from google.genai import types as genai_types  # noqa: E402
from livekit.plugins.google.realtime import (  # noqa: E402
    RealtimeModel as GeminiRealtimeModel,
)

# --- configuration -----------------------------------------------------------

S2S_MODEL = env("AGENT_S2S_MODEL", "gemini-3.8-live")
S2S_VOICE = env("AGENT_S2S_VOICE", "Puck")

# Worker health/registration HTTP port. Pick a port nothing else on the host
# holds; if the worker cannot bind it, it never registers and every call gets
# silence.
WORKER_PORT = env_int("AGENT_WORKER_PORT", 8083)

# The identity every dialer in this stack gives the phone leg. Used to wait for
# the human before speaking an opening line.
SIP_IDENTITY = env("SIP_IDENTITY", "phone-user")

OWNER_LABEL = env("AGENT_OWNER_LABEL", "your owner")

# How long to keep the line open after the agent decides to hang up, so its last
# sentence is actually heard before the room is deleted.
HANGUP_GRACE_S = env_float("AGENT_HANGUP_GRACE_S", 3.5)

# How long an answered outbound call must have been up before the agent may hang
# up. A brief that says "...and end the call" reads to the model as permission to
# hang up on its first turn - a real call was dropped 5s after pickup with the
# message never delivered - so the hangup is held back until the agent has spoken
# and the line has been up long enough to have held a short exchange.
MIN_TALK_S = env_float("AGENT_MIN_TALK_S", 12.0)

# A recorded greeting cannot be told apart from a person in the first seconds, so
# the machine verdict is only honoured once the far end has had time to speak.
MACHINE_MIN_S = env_float("AGENT_MACHINE_MIN_S", 6.0)

# Where each call's transcript lands, one JSON-lines file per room. The chat-side
# surface reads this after the line drops to post a post-call briefing back into
# the conversation, and it doubles as the only durable record of what was
# actually said on a call.
CALL_LOG_DIR = Path(
    env("AGENT_CALL_LOG_DIR") or (Path(__file__).resolve().parent.parent / "calls")
)


def live_api_key() -> str:
    """The Gemini Live key: dedicated one if set, else the general Google key."""
    return (
        env("GEMINI_LIVE_API_KEY")
        or env("GEMINI_API_KEY")
        or env("GOOGLE_API_KEY")
        or ""
    )


# --- delegation (OFF by default) ---------------------------------------------
#
# Handing a caller's request to an agent CLI that can run shell commands is a
# remote-code-execution surface pointed at the public phone network. It is
# therefore off unless explicitly switched on AND the caller is on an allowlist.
# An empty allowlist means nobody, not everybody: fail closed.
DELEGATION_ENABLED = env_bool("AGENT_DELEGATION_ENABLED", False)
DELEGATION_ALLOWLIST = [
    n.strip() for n in env("AGENT_DELEGATION_ALLOWLIST").split(",") if n.strip()
]
HERMES_BIN = env("AGENT_HERMES_BIN", "/usr/local/bin/hermes")
HERMES_SESSION = env("AGENT_HERMES_SESSION", "phone")
# A warm delegated turn measures ~11s; the first, cold one took ~34s while the
# session and model loaded. This ceiling is only the backstop for a wedged turn,
# and has to be generous because a real task (updating services, say) takes
# minutes. There is deliberately no --run-budget here: a session retains the
# policy it was created with, so passing one on --continue is refused outright.
HERMES_TIMEOUT_S = env_int("AGENT_HERMES_TIMEOUT", 120)
HERMES_HOME = env("AGENT_HERMES_HOME") or env("HOME") or str(Path.home())
# Ask for the shape the caller will actually hear, so the voice model has nothing
# to reformat under time pressure.
SPOKEN_ANSWER_RULE = (
    "Answer in one or two short spoken sentences suitable for a live phone call. "
    "No headings, no lists, no newlines, no markdown - only the words to say. "
    "Request: "
)
# One delegation at a time: two concurrent calls would otherwise drive the same
# CLI session from two processes and interleave their turns.
_hermes_lock = asyncio.Lock()


def delegation_allowed(caller: str) -> bool:
    """Whether this caller may drive the local agent CLI. Fail closed."""
    if not DELEGATION_ENABLED:
        return False
    if not DELEGATION_ALLOWLIST:
        return False
    return any(numbers_match(caller, allowed) for allowed in DELEGATION_ALLOWLIST)


# --- persona -----------------------------------------------------------------

DELEGATION_PERSONA = f"""
You act on this server through the agent CLI that runs it. It can run commands,
manage services and files, and reboot the machine. For anything the caller wants
done or checked - rebooting, whether the server is up, disk space, logs, what is
running - call ask_hermes with their request in plain words. Never tell the
caller you are unable to do something you can hand over.

The agent takes several seconds, but ask_hermes returns at once and the result
arrives later on its own. So after you call it, say one short line acknowledging
that you are on it - "Okay, doing that now, give me a moment." - and then stop.
Do not guess the answer and do not repeat yourself; you are prompted with the
result when it comes back, and you report it in one short sentence then. If the
caller asks for a reboot, warn them the line will drop before you call it.
"""

NO_DELEGATION_PERSONA = """
You cannot perform actions on this server - you have no way to run commands,
change anything, or look anything up on it. If the caller asks for something
like that, say plainly that you cannot do it, offer to take a message, and do
not pretend otherwise.
"""

INSTRUCTIONS = f"""
You are a friendly, concise voice assistant answering a phone call.
Keep every reply to one or two short sentences - this is a live phone call, not
an essay. Never use markdown, bullet points, emoji or stage directions; write
only the words to be spoken. If you did not catch something, ask the caller to
repeat it. If asked something you do not know, say so plainly.
""".strip()

_PERSONA_FILE = env("AGENT_PERSONA_FILE")
if _PERSONA_FILE:
    try:
        EXTRA_PERSONA = Path(_PERSONA_FILE).expanduser().read_text(encoding="utf-8").strip()
    except OSError as exc:
        print(f"[agent] persona file unreadable ({exc!r}); ignoring", flush=True)
        EXTRA_PERSONA = ""
else:
    EXTRA_PERSONA = ""


def base_instructions(allow_delegation: bool) -> str:
    parts = [INSTRUCTIONS, DELEGATION_PERSONA if allow_delegation else NO_DELEGATION_PERSONA]
    if EXTRA_PERSONA:
        parts.append(EXTRA_PERSONA)
    return "\n\n".join(p.strip() for p in parts if p.strip())


# --- call brief --------------------------------------------------------------


def read_brief(ctx) -> dict:
    """The per-call brief the dialer attached: room metadata, else job metadata.

    The dialer creates the room with this JSON in its metadata before adding the
    SIP participant, so an outbound call arrives carrying its own purpose. An
    inbound call has neither and gets the standing persona unchanged.
    """
    for raw in (getattr(ctx.room, "metadata", ""), getattr(ctx.job, "metadata", "")):
        raw = (raw or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            # A bare string is the whole brief.
            return {"mode": "call", "instruction": raw}
        if isinstance(data, dict):
            return data
    return {}


# --- SIP leg introspection ---------------------------------------------------


def sip_attrs(participant) -> dict:
    return {
        k: v
        for k, v in (getattr(participant, "attributes", None) or {}).items()
        if k.startswith("sip.")
    }


def sip_call_status(participant) -> str:
    """The SIP leg's own call state, as published by the LiveKit SIP service.

    dialing -> ringing -> active. Absent on non-SIP participants.
    """
    return str(sip_attrs(participant).get("sip.callStatus") or "")


def sip_participants(ctx) -> list:
    """Remote participants that are a phone leg (they carry sip.* attributes)."""
    out = []
    for p in list(ctx.room.remote_participants.values()):
        if sip_attrs(p):
            out.append(p)
    return out


def caller_number(ctx) -> str:
    """The far-end phone number, best effort.

    LiveKit SIP publishes the remote party as `sip.phoneNumber` on inbound
    calls; other fields appear depending on trunk and provider. Read the first
    that looks like a number, and log everything found so an operator can see
    what their own carrier actually delivers before trusting an allowlist.
    """
    for p in sip_participants(ctx):
        attrs = sip_attrs(p)
        for key in ("sip.phoneNumber", "sip.from.number", "sip.fromNumber", "sip.trunkPhoneNumber"):
            val = attrs.get(key)
            if val and any(c.isdigit() for c in str(val)):
                return str(val)
    return ""


async def wait_for_phone_leg(ctx, timeout_s: float = 15.0) -> list:
    """Wait briefly for the SIP participant to appear, then return the legs.

    Both the caller-ID gate and the answered gate key off the phone leg, and it
    is not always in the room at the instant the agent joins. This is a short
    wait on purpose: the leg is added when the INVITE goes out, which is
    immediate, and nothing is spoken until the answered gate passes anyway.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        legs = sip_participants(ctx)
        if legs or time.monotonic() >= deadline:
            return legs
        await asyncio.sleep(0.2)


async def wait_for_answered(ctx, timeout_s: float = 60.0) -> bool:
    """Block until the phone leg is picked up - ringing is not enough.

    The SIP service adds the participant to the room as soon as the INVITE goes
    out, so a participant existing proves nothing: in a traced call the callee
    joined the room at 0.05s and only answered at 15.4s, so a greeting gated on
    the join was spoken into a ringing line and dropped. sip.callStatus flips to
    'active' at the moment the far end answers, which is when to speak.

    Returns True when someone is listening; False when the leg died first.
    """
    room = ctx.room
    deadline = time.monotonic() + timeout_s
    seen = False
    last_status = None
    while True:
        participants = list(room.remote_participants.values())
        if participants:
            seen = True
            statuses = []
            for p in participants:
                status = sip_call_status(p)
                if status == "active" or not status:
                    # 'active' is the answer; no status at all means this is not a
                    # SIP leg (a browser or dashboard join) and is already live.
                    print(f"[agent] answered: sip.callStatus={status or 'n/a'}", flush=True)
                    return True
                statuses.append(status)
            current = ",".join(statuses)
            if current != last_status:
                print(f"[agent] waiting for answer: sip.callStatus={current}", flush=True)
                last_status = current
        elif seen:
            print("[agent] phone leg left before answering", flush=True)
            return False
        if time.monotonic() >= deadline:
            # Safety valve: the leg is still up but no answer was confirmed (still
            # ringing, or a build that never publishes the attribute). Speaking is
            # better than dead air - the callee hears it if they pick up next.
            if participants:
                print("[agent] answer unconfirmed; speaking anyway", flush=True)
                return True
            return False
        await asyncio.sleep(0.25)


def compose_instructions(brief: dict, allow_delegation: bool) -> str:
    """The persona for this call: the standing instructions plus the call's brief."""
    head = base_instructions(allow_delegation)
    instruction = str(brief.get("instruction") or "").strip()
    mode = str(brief.get("mode") or "").strip()
    if not instruction and mode in ("", "room"):
        return head

    body: list[str] = []
    if mode == "callme":
        body.append(
            f"This call was placed by {OWNER_LABEL} through the agent, to their own"
            " phone, because they asked for it. The person who answers is"
            f" {OWNER_LABEL} - treat them as such."
        )
        body.append(
            f"They asked for this call for this reason: {instruction}"
            if instruction
            else "They asked you to ring them; greet them and ask what they need."
        )
    else:
        body.append(
            f"You are placing an outbound call for {OWNER_LABEL}. The person who"
            " answers is NOT them and has never spoken to you before."
        )
        body.append(
            "Introduce yourself honestly in one short sentence as an AI assistant"
            f" calling on {OWNER_LABEL}'s behalf. Never claim to be human and never"
            " invent a name or an organisation you do not have."
        )
        body.append(
            f"The reason for this call, which you must carry out: {instruction}"
            if instruction
            else "State plainly why you are calling and ask for what you need."
        )
        body.append(
            "Pursue that reason until it is settled or the person declines, then"
            " thank them, say goodbye, and call end_call to hang up. If they ask you"
            " to stop, to hang up, or not to call again, agree at once, apologise for"
            " the interruption, say goodbye and call end_call."
        )
    body.append(
        "If a machine answers - a recorded greeting, an automated voice reading a"
        " number or an address back, a menu of options, or a voicemail beep - do"
        " not leave a message, do not say goodbye to it and do not wait for a"
        ' tone: call end_call at once with reason="voicemail".'
    )
    return head + "\n\n" + "\n".join(body)


def opening_line(brief: dict) -> str:
    """The first thing the agent says, which differs for an outbound call."""
    mode = str(brief.get("mode") or "").strip()
    if mode == "callme":
        reason = str(brief.get("instruction") or "").strip()
        if reason:
            return (
                f"Greet {OWNER_LABEL} in one short sentence, say you are calling about"
                f" this: {reason}. Then ask what they need."
            )
        return f"Greet {OWNER_LABEL} in one short sentence and ask what they need."
    if mode == "call":
        return (
            "Open the call now: in one short sentence say who you are, then say why"
            " you are calling. This is an outbound call, so do not ask how you can help."
        )
    configured = env("AGENT_INBOUND_GREETING")
    if configured:
        return configured
    return "Greet the caller in one short sentence and ask how you can help."


# --- transcript --------------------------------------------------------------


class CallTranscript:
    """Append-only record of one call: the brief, what was said, what was heard.

    JSON lines so a reader can tail the file while the call is still up. Every
    write is guarded: a logging failure must never be able to take down a live
    phone call.
    """

    def __init__(self, room: str, brief: dict | None = None):
        self.room = room
        self.path = CALL_LOG_DIR / f"{room}.jsonl"
        self.started = time.monotonic()
        self._closed = False
        try:
            CALL_LOG_DIR.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[agent] transcript dir unavailable: {exc!r}", flush=True)
        self.write(
            "call",
            mode=(brief or {}).get("mode") or "inbound",
            instruction=str((brief or {}).get("instruction") or ""),
            # Who was dialled, so the transcript is readable on its own (the chat
            # side masks it before printing).
            callee=str((brief or {}).get("callee") or ""),
        )

    def write(self, kind: str, **fields) -> None:
        rec = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "room": self.room, "kind": kind}
        rec.update(fields)
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001
            print(f"[agent] transcript write failed ({kind}): {exc!r}", flush=True)

    def said(self, text: str) -> None:
        self.write("said", text=text)

    def heard(self, text: str, final: bool) -> None:
        self.write("heard", text=text, final=bool(final))

    def answered(self) -> None:
        self.write("answered", waited_s=round(time.monotonic() - self.started, 1))

    def ended(self, reason: str = "") -> None:
        """Close the transcript. First caller wins - several shutdown paths race."""
        if self._closed:
            return
        self._closed = True
        self.write("ended", reason=reason,
                   duration_s=round(time.monotonic() - self.started, 1))


# --- the agent ---------------------------------------------------------------

# Words in the model's own reason that mean it decided the far end is a machine.
_MACHINE_WORDS = (
    "voicemail", "voice mail", "voice-mail", "answering machine", "machine",
    "recorded", "recording", "automated", "auto attendant", "ivr", "beep",
    "not a person", "no human", "robot",
)


def _machine_verdict(reason: str) -> bool:
    """True when the agent reports it reached voicemail or an answering machine."""
    low = reason.lower()
    return any(w in low for w in _MACHINE_WORDS)


class Assistant(Agent):
    def __init__(self, instructions: str, ctx=None, transcript=None,
                 allow_delegation: bool = False):
        # instructions carry the standing persona plus, for an outbound call, the
        # brief that says who is being called and why (see compose_instructions).
        super().__init__(instructions=instructions)
        # The job context, so the agent can end the call itself (delete the room,
        # which drops the SIP leg) rather than leaving the callee on a dead line.
        self._ctx = ctx
        # The call's transcript, so the hangup records why the line dropped.
        self._transcript = transcript
        # Delegation is gated twice: the persona does not offer it unless allowed,
        # and the tool refuses at runtime if it is called anyway. A stranger must
        # not be able to reach a shell through the phone line.
        self._allow_delegation = allow_delegation
        # When the far end picked up (set by entrypoint once the answered gate
        # passes) and how much the agent has said since. Both exist to stop a
        # hangup that arrives before the call's purpose has been carried out.
        self._answered_at: float | None = None
        self._said_turns = 0
        # In-flight delegations. A bare create_task can be garbage collected before
        # it finishes, so hold a reference until it is done.
        self._delegations: set[asyncio.Task] = set()

    def mark_answered(self) -> None:
        """Record the moment the far end picked up, for the hangup guard."""
        self._answered_at = time.monotonic()

    def note_spoken(self, text: str) -> None:
        """Record that the agent said something out loud, for the hangup guard."""
        if text.strip():
            self._said_turns += 1

    def _hangup_hold(self) -> str:
        """Why the agent must not hang up yet, or "" when it may.

        The brief says what to achieve, not when to stop: "call him and tell him X
        and end the call" invites the model to hang up before X is said, which it
        did on a real call. Hold the hangup until the agent has spoken and the
        line has been up long enough for a short exchange.
        """
        if self._answered_at is None:
            # Nothing of ours to protect: inbound, or not picked up yet.
            return ""
        if self._said_turns < 1:
            return (
                "You have not said anything to the person yet. Deliver what you "
                "called to say, in full, and let them respond."
            )
        waited = time.monotonic() - self._answered_at
        if waited < MIN_TALK_S:
            return (
                f"The line has only been up {waited:.0f} seconds and you have not "
                "delivered your message yet. Say what you called to say, then wait "
                "for their answer before ending the call."
            )
        return ""

    @function_tool
    async def end_call(self, reason: str = "") -> ToolResult:
        """Hang up the phone line, ending the call.

        Use this once the call's purpose is settled and you have said your final
        sentence, when the person says goodbye or asks you to hang up, or when
        they tell you not to call again. Speak your goodbye FIRST - the line
        closes a few seconds after you call this.

        If a MACHINE answered - a recorded greeting, a synthetic voice reading a
        number or an address back, an auto attendant menu, or a voicemail beep -
        do NOT leave a message and do not talk to it. Call this at once with
        reason="voicemail" and say nothing more; the line closes by itself.

        Args:
            reason: why the call is ending. Use "voicemail" for a machine;
                anything else is treated as a normal end of call (for the log).
        """
        print(f"[agent] end_call: {reason or 'no reason given'}", flush=True)
        machine = _machine_verdict(reason)
        if machine and self._answered_at is not None:
            # Dropping a machine is the wanted outcome, so the "deliver your
            # message first" hold does not apply - but an early verdict is not
            # trustworthy, and a wrong one drops a real person mid-sentence.
            up = time.monotonic() - self._answered_at
            if up < MACHINE_MIN_S:
                print("[agent] end_call refused: machine verdict too early", flush=True)
                return ToolResult(
                    "You cannot tell yet - the line has only been up "
                    f"{up:.0f} seconds. Listen to what the far end says before "
                    "deciding it is a machine.",
                    reply_required=True,
                )
            print(f"[agent] end_call: machine verdict after {up:.0f}s, hanging up",
                  flush=True)
        hold = "" if machine else self._hangup_hold()
        if hold:
            # Refuse, and ask for a reply, so the model gets another turn to
            # actually deliver the message instead of dropping the line.
            print(f"[agent] end_call refused: {hold}", flush=True)
            return ToolResult(hold, reply_required=True)
        if self._transcript is not None:
            self._transcript.ended(f"agent hung up: {reason or 'no reason given'}")
        task = asyncio.create_task(self._hangup())
        # Hold a reference: a bare task can be garbage collected mid-flight.
        self._delegations.add(task)
        task.add_done_callback(self._delegations.discard)
        return ToolResult(
            "The line will close in a few seconds. Say nothing further.",
            reply_required=False,
        )

    async def _hangup(self) -> None:
        """Drop the call after a grace period, so the goodbye is not cut off."""
        await asyncio.sleep(HANGUP_GRACE_S)
        if self._ctx is None:
            return
        try:
            await self._ctx.delete_room()
            print("[agent] call ended by the agent", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[agent] hangup failed: {exc!r}", flush=True)

    @function_tool
    async def ask_hermes(self, instruction: str) -> ToolResult:
        """Hand a request to the agent CLI that runs this server.

        It can run shell commands, read and write files, manage services and cron
        jobs, search the web and reboot the machine. Use it for anything the
        caller wants done or checked - rebooting, service status, disk space,
        logs. Pass the caller's request in plain words.
        """
        if not self._allow_delegation:
            # Reachable only if the model calls the tool uninvited: the persona
            # never offers it to a caller who is not on the allowlist.
            print("[agent] ask_hermes REFUSED (delegation not allowed for this call)",
                  flush=True)
            return ToolResult(
                "You cannot do that on this call. Tell the caller you are not able "
                "to perform actions on this server and offer to take a message.",
                reply_required=True,
            )

        print("[agent] ask_hermes ENTERED (LLM decided to delegate)", flush=True)

        task = asyncio.create_task(self._run_delegation(instruction))
        self._delegations.add(task)
        task.add_done_callback(self._delegations.discard)

        # say() is unavailable on this backend: the Google realtime plugin leaves
        # RealtimeCapabilities.supports_say at its default of False, so there is no
        # TTS to synthesize into and session.say() raises. The realtime model IS
        # the voice here, so let it speak the acknowledgement itself - that is one
        # generation on a model that already reaches first audio in ~1.6s, not an
        # extra round trip to a separate service.
        return ToolResult(
            "The request has been handed over and is running in the background. The "
            "result is NOT ready yet. Reply with one short sentence - six words at "
            "most - saying you are on it. Do not answer the question and do not "
            "guess at the result.",
            reply_required=True,
        )

    async def _run_delegation(self, instruction: str) -> None:
        answer = await self._ask_hermes(instruction)
        try:
            await self.session.generate_reply(
                instructions=(
                    "You have just finished the task the caller asked for. Report "
                    f"the result in one short spoken sentence: {answer}"
                ),
                # Never let the report trigger another delegation.
                tool_choice="none",
            )
        except Exception as exc:
            # By the time a slow task lands the caller has often hung up, and there
            # is no session left to speak into.
            print(f"[agent] could not report result: {exc!r}", flush=True)

    async def _ask_hermes(self, instruction: str) -> str:
        async with _hermes_lock:
            proc = await asyncio.create_subprocess_exec(
                HERMES_BIN,
                "chat",
                "-q",
                SPOKEN_ANSWER_RULE + instruction,
                "--oneshot",
                "--continue",
                HERMES_SESSION,
                "--create-if-missing",
                "--yolo",
                "-Q",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Absolute HOME: the worker's unit may not set one, and the CLI
                # keeps its config and session state under it.
                env={**os.environ, "HOME": HERMES_HOME},
            )
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(), timeout=HERMES_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return (
                    "That is still working. Tell the caller it is taking longer than "
                    "expected and to ask again in a moment."
                )

        # The answer is the whole of stdout; the session banner and the session id
        # both go to stderr, so there is nothing to filter out here.
        answer = out.decode(errors="replace").strip()
        if not answer:
            # Empty stdout means the CLI failed outright rather than doing the
            # work. Say so plainly: left vague, the voice model reports a failure
            # as a successful check that found nothing.
            tail = err.decode(errors="replace").strip().splitlines()[-3:]
            print(f"[agent] ask_hermes FAILED rc={proc.returncode}: {tail}", flush=True)
            return (
                "That did not work - the agent returned no answer at all. Tell the "
                "caller something went wrong and that nothing was done."
            )
        print(f"[agent] ask_hermes -> {answer[:200]!r}", flush=True)
        return answer


# --- worker ------------------------------------------------------------------


def agent_load() -> float:
    """Availability load metric for this worker, in 0..1.

    livekit-agents' default reads /sys/fs/cgroup/cpu.stat, which on a shared host
    is the ROOT cgroup: the whole machine, not this worker. A box running a dozen
    other services crosses the 0.7 threshold on its own and the worker marks
    itself unavailable - at which point LiveKit skips it and the agent never joins
    the call. Measure this worker's own process tree instead.

    Concurrency here is bounded by the phone line, not by CPU, so a low reading is
    the honest one.
    """
    try:
        me = psutil.Process()
        procs = [me, *me.children(recursive=True)]
        total = 0.0
        for p in procs:
            try:
                total += p.cpu_percent(interval=None)  # % of a single core
            except psutil.Error:
                continue
        cpus = psutil.cpu_count() or 1
        return min(total / 100.0 / cpus, 1.0)
    except Exception:
        return 0.0


def _s2s_session(instructions: str) -> AgentSession:
    """AgentSession backed by the Gemini Live API (speech to speech).

    There is deliberately no stt/tts/vad here. The Live model takes raw audio in
    and returns audio out over a single WebSocket, and owns turn detection and
    barge-in itself. That removes the entire local voice path - no whisper, no
    Kokoro, no silero - and with it the per-turn CPU cost, plus the
    STT -> LLM -> TTS serialisation that batch recognition forces (a local
    recognizer only starts at end-of-speech, so nothing can overlap).
    """
    return AgentSession(
        llm=GeminiRealtimeModel(
            model=S2S_MODEL,
            voice=S2S_VOICE,
            api_key=live_api_key(),
            # AgentSession.instructions never reaches a realtime model: it owns its
            # own prompting, so the persona has to be handed over directly.
            instructions=instructions,
            # Without these the transcript events stay empty and a bad turn leaves
            # nothing to read in the journal.
            input_audio_transcription=genai_types.AudioTranscriptionConfig(),
            output_audio_transcription=genai_types.AudioTranscriptionConfig(),
            # Live API function calling is sequential only - NON_BLOCKING is not
            # supported - so the model waits on a tool result before it speaks
            # again. That is fine here: ask_hermes returns immediately, the
            # delegation runs in the background, and the answer is spoken when it
            # lands. Being explicit rather than relying on the default.
            tool_behavior=genai_types.Behavior.BLOCKING,
        ),
    )


async def entrypoint(ctx: agents.JobContext):
    print(f"[agent] job received room={ctx.room.name!r}", flush=True)
    await ctx.connect()

    # An outbound call carries its brief in the room metadata (the dialer); an
    # inbound call carries nothing and keeps the standing persona.
    brief = read_brief(ctx)
    if brief:
        print(
            f"[agent] call brief mode={brief.get('mode')!r} "
            f"instruction={str(brief.get('instruction') or '')[:160]!r}",
            flush=True,
        )

    transcript = CallTranscript(ctx.room.name, brief)

    async def _on_shutdown(reason: str = "") -> None:
        # Fires whether the agent hung up, the callee did, or the room was torn
        # down. Without it a caller-side hangup leaves the transcript open and the
        # chat-side watcher waiting for an "ended" line that never comes.
        transcript.ended(f"call ended ({reason or 'room closed'})")

    ctx.add_shutdown_callback(_on_shutdown)

    @ctx.room.on("track_subscribed")
    def _on_track_subscribed(track, publication, participant):
        print(f"[agent] subscribed to {participant.identity} track={track.kind}",
              flush=True)

    @ctx.room.on("participant_connected")
    def _on_participant_connected(p):
        attrs = sip_attrs(p)
        print(f"[agent] participant joined: {p.identity} sip_attrs={attrs}", flush=True)

    # Both the caller-ID gate and the answered gate key off the phone leg. The
    # leg is added when the INVITE goes out (the dialer creates the room first so
    # the brief is in place), and an inbound call arrives with one already there.
    phone_legs = await wait_for_phone_leg(ctx)
    if phone_legs:
        print(f"[agent] phone leg present: {[p.identity for p in phone_legs]}", flush=True)

    caller = caller_number(ctx)
    allow_delegation = delegation_allowed(caller)
    print(
        f"[agent] caller={mask(caller) if caller else '(none)'} "
        f"delegation={'on' if allow_delegation else 'off'}"
        f"{'' if DELEGATION_ENABLED else ' (disabled globally)'}",
        flush=True,
    )

    instructions = compose_instructions(brief, allow_delegation)
    session = _s2s_session(instructions)
    print(f"[agent] backend=s2s model={S2S_MODEL} voice={S2S_VOICE}", flush=True)

    assistant = Assistant(instructions, ctx, transcript, allow_delegation=allow_delegation)
    await session.start(room=ctx.room, agent=assistant)
    print("[agent] session started", flush=True)

    @session.on("user_input_transcribed")
    def _on_user_transcript(ev):
        print(f"[agent] heard (final={ev.is_final}): {ev.transcript!r}", flush=True)
        transcript.heard(ev.transcript, ev.is_final)

    @session.on("conversation_item_added")
    def _on_conversation_item(ev):
        # What the agent actually said. The only way to check after the fact that
        # an outbound call followed its brief, or that the opening line landed.
        item = getattr(ev, "item", None)
        if getattr(item, "role", None) != "assistant":
            return
        text = (getattr(item, "text_content", None) or "").strip()
        if text:
            assistant.note_spoken(text)
            transcript.said(text)
            print(f"[agent] said: {text!r}", flush=True)

    @session.on("user_state_changed")
    def _on_user_state(ev):
        print(f"[agent] user_state {ev.old_state} -> {ev.new_state}", flush=True)

    # agent_state is the turn clock: listening -> thinking is the moment the turn
    # was committed and the LLM call started; thinking -> speaking is the moment
    # audio started going out. Journal timestamps on these two lines split a slow
    # turn into "before the model" and "after the model" without guessing.
    @session.on("agent_state_changed")
    def _on_agent_state(ev):
        print(f"[agent] agent_state {ev.old_state} -> {ev.new_state}", flush=True)

    @session.on("metrics_collected")
    def _on_metrics(ev):
        # Print the numbers, not just the class name: per-stage duration/ttft is
        # the only way to see where a slow turn is going when reading the journal.
        m = ev.metrics
        bits = [type(m).__name__]
        for attr in ("duration", "ttft", "audio_duration", "input_tokens", "output_tokens"):
            val = getattr(m, attr, None)
            if isinstance(val, (int, float)):
                bits.append(f"{attr}={val:.3f}")
        print("[agent] metrics " + " ".join(bits), flush=True)

    if phone_legs:
        # The room for an outbound call exists before anyone picks up, so the
        # opening line must wait for the phone to be ANSWERED, not merely for the
        # leg to exist: a greeting spoken into a ringing line is dropped. Inbound
        # calls arrive with a live caller, but their leg can still be ringing, so
        # the same gate applies.
        if not await wait_for_answered(ctx):
            print("[agent] call was never answered; nothing to say", flush=True)
            return
        # Start the hangup clock at the answer, not at the dial.
        assistant.mark_answered()
        transcript.answered()

    line = opening_line(brief)
    for attempt in (1, 2, 3):
        try:
            await session.generate_reply(instructions=line)
            break
        except Exception as exc:  # noqa: BLE001
            # generate_reply waits a few seconds for the realtime model to open a
            # generation; a cold Live socket can miss that window and leave the
            # caller in silence. Retry instead of going quiet.
            print(f"[agent] opening line attempt {attempt} failed: {exc!r}", flush=True)
            if attempt == 3:
                break
            await asyncio.sleep(1.0)


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            port=WORKER_PORT,
            load_fnc=agent_load,
        )
    )
