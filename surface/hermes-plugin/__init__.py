"""voice-call - /callme, /call and /calllog: have the voice agent place a real call.

The telephony lives in the LiveKit stack, not in the agent host: the worker joins
any room that appears and answers over the SIP trunk. So a command only has to
start a call, and the worker takes it from there.

    /callme [why]             ring your own phone
    /call 0456789012 <reason> ring that number and follow <reason> as its brief
    /calllog [room]           reprint the transcript of the last call

Both commands work wherever the session runs (CLI and Telegram) because they are
registered as in-session slash commands, and both shell out to the dialer in the
repo, which creates the room with the brief in its metadata and then adds the
phone leg. When the line drops, a watcher posts the transcript back into the same
conversation - the command itself returns while the phone is still ringing, so
nothing can be reported synchronously.

Configuration (environment, read at register time):

    PHOTON_VOICE_DIR     repo checkout            default /opt/photon-livekit-voice
    VOICE_CALL_OWNER     your number for /callme  (in ~/.hermes/.env)
    AGENT_CALL_LOG_DIR   where the worker writes transcripts
                         default <PHOTON_VOICE_DIR>/calls
    VOICE_CALL_DRY_RUN   set to 1 to exercise the plumbing without dialling

Never print a full phone number: every reply masks it.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

VOICE_DIR = Path(os.environ.get("PHOTON_VOICE_DIR") or "/opt/photon-livekit-voice")
DIALER = VOICE_DIR / "cli" / "call_cli.py"
DIALER_PYTHON = VOICE_DIR / "venv" / "bin" / "python"
HERMES_ENV = Path(os.path.expanduser("~/.hermes/.env"))
STACK_ENV = VOICE_DIR / ".env"


def _env_value(path: Path, key: str) -> str:
    """One key out of a .env file. No dotenv dependency, and no side effects."""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def stack_value(key: str) -> str:
    """A stack knob: the process environment first, then the stack's own .env.

    The gateway does not export the stack .env into its own environment, so a
    documented key like DEFAULT_COUNTRY_CODE would otherwise be invisible here
    even though the dialer (which reads the file) honours it.
    """
    from_env = (os.environ.get(key) or "").strip()
    if from_env:
        return from_env
    return _env_value(STACK_ENV, key)

# Owner number, first hit wins. VOICE_CALL_OWNER is the documented key;
# WHATSAPP_ALLOWED_USERS is a convenience fallback for Hermes installs that
# already carry the operator's number there.
OWNER_KEYS = ("VOICE_CALL_OWNER", "WHATSAPP_ALLOWED_USERS")

RING_TIMEOUT_S = int(os.environ.get("VOICE_RING_TIMEOUT_S") or 45)
# The host gives a plugin command a limited window before it stops waiting; stay
# under it and report honestly rather than getting cut off mid-answer.
DIAL_DEADLINE_S = int(os.environ.get("VOICE_DIAL_DEADLINE_S") or 25)

USAGE = (
    "/callme [why]            - the agent rings your phone (optional reason for the call)\n"
    "/call <number> <reason>  - the agent rings <number> and follows <reason> as its brief\n"
    "/calllog [room]          - print the transcript of the last call (or a named room)\n"
    "Numbers: full international form (+61412345678), or local form when\n"
    "DEFAULT_COUNTRY_CODE is set in the stack's .env."
)

# Where the worker writes each call's transcript, one JSON-lines file per room
# (see CallTranscript in agent/agent.py). The briefing is built from that file
# once the line drops.
CALL_LOG_DIR = Path(
    stack_value("AGENT_CALL_LOG_DIR") or (VOICE_DIR / "calls")
)
BRIEF_POLL_S = int(os.environ.get("VOICE_BRIEF_POLL_S") or 5)
# The worker records the hangup as soon as the line drops, but the SDK can flush a
# final utterance after that, so wait a moment past the ended record before reading
# the transcript - otherwise the agent's closing line is missing from the briefing.
BRIEF_SETTLE_S = int(os.environ.get("VOICE_BRIEF_SETTLE_S") or 8)
# Give up on a call that outlives this. The transcript stays on disk, and /calllog
# prints it, so a missed briefing is recoverable rather than lost.
BRIEF_MAX_WAIT_S = int(os.environ.get("VOICE_BRIEF_MAX_WAIT_S") or 900)

# Set in register(); the briefing watcher needs the plugin context after the
# command handler has already returned.
_CTX = None


def owner_number() -> str:
    for key in OWNER_KEYS:
        raw = _env_value(HERMES_ENV, key)
        if raw:
            return raw.split(",")[0].strip()
    return ""


def normalise_number(raw: str) -> str:
    """Loose input -> E.164. Left to the dialer to finish; this is a fast check."""
    given = (raw or "").strip()
    s = re.sub(r"[^\d+]", "", given)
    if not s:
        raise ValueError(
            "no number given" if not given else f"{given!r} does not look like a dialable number"
        )
    if s.startswith("00"):
        s = "+" + s[2:]
    if not s.startswith("+"):
        cc = re.sub(r"\D", "", stack_value("DEFAULT_COUNTRY_CODE"))
        if s.startswith("0"):
            if not cc:
                raise ValueError(
                    f"{given!r} looks like a local number, but DEFAULT_COUNTRY_CODE is "
                    f"not set - set it in the stack .env (e.g. 61) or dial in full "
                    f"international form (+614****5678)"
                )
            s = "+" + cc + s[1:]
        else:
            s = "+" + s
    if not re.match(r"^\+[1-9]\d{7,14}$", s):
        raise ValueError(f"{raw!r} does not look like a dialable number")
    return s


def mask(number: str) -> str:
    """Never print a full phone number into a chat log."""
    d = re.sub(r"\D", "", number or "")
    if not d:
        return "****"
    return "*" * (len(d) - 4) + d[-4:] if len(d) > 4 else "****"


def _session_key() -> str:
    """This session's durable routing key, so the briefing lands in this chat.

    Only the messaging gateway needs it - CLI injection goes to the attached
    session's own input queue - and surfaces that bind no key return "".
    """
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_SESSION_KEY") or ""
    except Exception:  # noqa: BLE001 - no gateway package in this process is fine
        return ""


def _read_transcript(room: str) -> list:
    """Parse the room's transcript, tolerating a half-written final line."""
    recs = []
    try:
        with (CALL_LOG_DIR / f"{room}.jsonl").open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return recs


def _clock(iso: str) -> str:
    """ISO-8601 timestamp -> HH:MM:SS, the form that reads well in chat."""
    return iso[11:19] if len(iso) >= 19 else iso


def briefing(room: str, number: str = "", mode: str = "call") -> str:
    """The call as it happened: the brief, the exchange, how the line dropped.

    Kept in the order the records were written - that is chronological, and a
    timestamp sort would reorder anything that shares a second.
    """
    recs = _read_transcript(room)
    if not recs:
        return ""
    meta = next((r for r in recs if r.get("kind") == "call"), {})
    mode = mode or str(meta.get("mode") or "call")
    number = number or str(meta.get("callee") or "")
    who = "your phone" if mode == "callme" else (mask(number) if number else "unknown number")

    lines = [f"Call to {who} ({room})"]
    instruction = str(meta.get("instruction") or "").strip()
    if instruction:
        lines.append(f"Brief: {instruction}")

    answered = next((r for r in recs if r.get("kind") == "answered"), None)
    if answered is None:
        lines.append("Never answered - the line was not picked up, so nothing was said.")
        return "\n".join(lines)

    ended = next((r for r in recs if r.get("kind") == "ended"), None)
    on_line = ""
    if ended is not None:
        try:
            secs = (datetime.fromisoformat(ended.get("at", ""))
                    - datetime.fromisoformat(answered.get("at", ""))).total_seconds()
            on_line = f", {secs:.0f}s on the line"
        except ValueError:
            on_line = ""
    waited = answered.get("waited_s")
    waited_txt = f"{float(waited):.0f}s" if isinstance(waited, (int, float)) else f"{waited}"
    lines.append(
        f"Answered {_clock(answered.get('at', ''))} UTC after {waited_txt} of ringing{on_line}."
    )

    # Drop the partial recognitions when finals exist, so a repeated sentence is
    # not printed twice; with only partials, the last one is the best guess.
    heard = [r for r in recs if r.get("kind") == "heard" and (r.get("text") or "").strip()]
    finals = [r for r in heard if r.get("final")]
    keep = {id(r) for r in (finals or heard[-1:])}
    spoken = [r for r in recs
              if (r.get("kind") == "said" and (r.get("text") or "").strip())
              or (r.get("kind") == "heard" and id(r) in keep)]
    for rec in spoken:
        speaker = "agent" if rec.get("kind") == "said" else "caller"
        lines.append(f"{_clock(rec.get('at', ''))} {speaker}: {rec['text']}")
    if not spoken:
        lines.append("Nothing was said in either direction.")
    if ended is not None:
        reason = str(ended.get("reason") or "no reason recorded").rstrip(". ")
        lines.append(f"Ended {_clock(ended.get('at', ''))} UTC - {reason}.")
    return "\n".join(lines)


def _watch_call(room: str, number: str, mode: str, ctx, session_key: str) -> None:
    """Wait for the call to end, then post its transcript into this conversation.

    Its own thread because the call outlives the command by design: the dialer
    returns as soon as the phone rings. Nothing may raise out of here.
    """
    try:
        deadline = time.monotonic() + BRIEF_MAX_WAIT_S
        while time.monotonic() < deadline:
            time.sleep(BRIEF_POLL_S)
            recs = _read_transcript(room)
            ended = next((r for r in recs if r.get("kind") == "ended"), None)
            if ended is None:
                continue
            # Settle on the hangup's own timestamp, so a call that ended while we
            # were not looking does not wait an extra window here.
            try:
                age = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(ended.get("at", ""))).total_seconds()
            except ValueError:
                age = BRIEF_SETTLE_S
            if age >= BRIEF_SETTLE_S:
                break
        text = briefing(room, number, mode)
        if not text:
            print(f"[voice-call] no transcript for {room}; nothing to post", file=sys.stderr)
            return
        body = ("[voice-call] The call has ended. Transcript below - give me the short "
                f"version of what was said.\n\n{text}")
        if not ctx.inject_message(body, role="user", session_key=session_key or None):
            print(f"[voice-call] briefing for {room} could not be posted into this "
                  f"conversation; it is on disk - /calllog prints it", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - a watcher must never break anything else
        print(f"[voice-call] briefing watcher failed for {room}: {exc!r}", file=sys.stderr)


def _spawn_briefing(result: dict, number: str, mode: str) -> None:
    """Start the post-call briefing watcher for a call that actually went out."""
    room = str(result.get("room") or "")
    if not result.get("ok") or result.get("dry_run") or not room:
        return
    threading.Thread(
        target=_watch_call, args=(room, number, mode, _CTX, _session_key()),
        name=f"voice-call-brief-{room}", daemon=True,
    ).start()


async def _dial(number: str, instruction: str, mode: str) -> dict:
    """Run the dialer and return its JSON verdict."""
    # Opt-in escape hatch so the command plumbing can be exercised (and demoed)
    # without ringing a real phone. Never set this for normal use.
    if (os.environ.get("VOICE_CALL_DRY_RUN") or "").strip().lower() in {"1", "true", "yes"}:
        return {"ok": True, "dry_run": True, "room": f"dry-run-{int(time.time())}",
                "callee": number, "mode": mode, "participant_id": "dry-run",
                "sip_call_id": "dry-run"}

    if not DIALER.is_file() or not DIALER_PYTHON.is_file():
        return {"ok": False,
                "error": f"the dialer is not installed at {DIALER} (python {DIALER_PYTHON}). "
                         f"Set PHOTON_VOICE_DIR to the repo checkout."}

    argv = [
        str(DIALER_PYTHON), str(DIALER),
        "--to", number,
        "--mode", mode,
        "--json",
        "--ring-timeout", str(RING_TIMEOUT_S),
    ]
    if instruction:
        argv += ["--instruction", instruction]

    env = dict(os.environ)
    # Gateway sessions can run with a different HOME; the dialer reads its config
    # from the repo, but keep HOME sane for anything that resolves ~.
    env["HOME"] = os.path.expanduser("~")
    # The host runs under its own python with PYTHONPATH aimed at its venv. The
    # dialer is a different interpreter in the stack's venv, and inheriting that
    # path makes it import foreign-built wheels and die on an ABI mismatch
    # (cryptography: "undefined symbol: PyType_GetName"). It reads its own config
    # from the repo, so it needs nothing from ours.
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONEXECUTABLE",
                "VIRTUAL_ENV", "LD_LIBRARY_PATH", "LD_PRELOAD"):
        env.pop(key, None)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        )
    except OSError as exc:
        return {"ok": False, "error": f"could not start the dialer: {exc}"}

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=DIAL_DEADLINE_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"ok": False, "error": f"the dialer did not report back within {DIAL_DEADLINE_S}s"}

    text = out.decode(errors="replace").strip()
    if text:
        try:
            return json.loads(text.splitlines()[-1])
        except ValueError:
            pass
    tail = [ln for ln in err.decode(errors="replace").strip().splitlines() if ln.strip()]
    return {"ok": False,
            "error": tail[-1] if tail else f"the dialer exited {proc.returncode} with no result"}


def _report(result: dict, number: str, mode: str, instruction: str) -> str:
    who = "your phone" if mode == "callme" else mask(number)
    if result.get("dry_run"):
        brief = instruction or ("greet you and ask what you need." if mode == "callme" else "-")
        return "\n".join([
            f"[dry run] would call {who} now.",
            f"Brief: {brief}",
            "Nothing was dialled: VOICE_CALL_DRY_RUN is set in this session.",
        ])
    if not result.get("ok"):
        err = str(result.get("error") or "unknown error")
        lines = [f"Call failed: {err}"]
        low = err.lower()
        if "timed out" in low or "timeout" in low or "408" in low:
            lines.append(
                "The number never answered within the ring window. Nothing was said, and "
                "the line is now clear - try again, or give them more time to pick up."
            )
        elif "404" in low or "not found" in low:
            lines.append("That number is not reachable - check the digits.")
        elif "deadline" in low or "unavailable" in low:
            lines.append(
                "The dialer or the SIP legs could not complete the setup - check that the "
                "cloud security list AND the host firewall allow the RTP UDP range inbound."
            )
        lines.append("Nothing was said on the call.")
        return "\n".join(lines)

    state = result.get("state") or ("answered" if result.get("answered") else "ringing")
    if state == "answered":
        lines = [f"{who} answered - the voice agent is speaking now."]
    else:
        lines = [f"Ringing {who} now - the agent speaks the moment they pick up."]
    if mode == "callme":
        lines.append(
            f"Brief: {instruction}" if instruction
            else "Brief: greet you and ask what you need."
        )
    else:
        lines.append(f"Brief: {instruction}")
    lines.append(f"room {result.get('room')}")
    lines.append("I'll post what was said here once the line drops.")
    return "\n".join(lines)


async def _cmd_callme(raw_args: str) -> str:
    instruction = (raw_args or "").strip()
    number = owner_number()
    if not number:
        return (
            "No owner number configured. Put VOICE_CALL_OWNER=<your number> in "
            "~/.hermes/.env (or WHATSAPP_ALLOWED_USERS) and try again."
        )
    try:
        number = normalise_number(number)
    except ValueError as exc:
        return f"The configured owner number is not dialable: {exc}"
    result = await _dial(number, instruction, "callme")
    _spawn_briefing(result, number, "callme")
    return _report(result, number, "callme", instruction)


async def _cmd_call(raw_args: str) -> str:
    text = (raw_args or "").strip()
    if not text:
        return "Give me a number and a reason for the call.\n\n" + USAGE

    parts = text.split(None, 1)
    try:
        number = normalise_number(parts[0])
    except ValueError as exc:
        return f"{exc}\n\n{USAGE}"
    if len(parts) < 2 or not parts[1].strip():
        return (
            "Tell me why the agent is calling, so it knows what to say:\n"
            "  /call 0456789012 ask whether the 3pm appointment is still on\n\n" + USAGE
        )
    instruction = parts[1].strip()
    result = await _dial(number, instruction, "call")
    _spawn_briefing(result, number, "call")
    return _report(result, number, "call", instruction)


async def _cmd_calllog(raw_args: str) -> str:
    """Print the transcript of the last call, or of a named room."""
    room = (raw_args or "").strip()
    if not room:
        try:
            logs = [f for f in os.listdir(CALL_LOG_DIR) if f.endswith(".jsonl")]
        except OSError:
            return "No calls have been logged yet."
        if not logs:
            return "No calls have been logged yet."
        room = max(logs, key=lambda f: os.path.getmtime(CALL_LOG_DIR / f))[:-6]
    text = briefing(room)
    return text or f"No transcript recorded for room {room}."


def register(ctx) -> None:
    # Held for the briefing watcher: it runs on its own thread after this handler
    # returns, and the injection API hangs off the context, not the handler.
    global _CTX
    _CTX = ctx
    ctx.register_command(
        "callme",
        handler=_cmd_callme,
        description="Have the voice agent ring your own phone and follow an optional reason.",
        args_hint="[why]",
    )
    ctx.register_command(
        "call",
        handler=_cmd_call,
        description="Have the voice agent call a number and follow the reason you give it.",
        args_hint="<number> <reason>",
    )
    ctx.register_command(
        "calllog",
        handler=_cmd_calllog,
        description="Print the transcript of the most recent call (or a named room).",
        args_hint="[room]",
    )
    # The build stamp makes it obvious in the logs which revision a running process
    # actually loaded - a plugin edited after the host loaded it stays stale until
    # the host reloads.
    try:
        stamp = time.strftime("%H:%M:%S", time.localtime(os.path.getmtime(__file__)))
    except OSError:
        stamp = "unknown"
    print(f"[voice-call] registered /callme, /call and /calllog (build {stamp}); "
          f"repo={VOICE_DIR} calls={CALL_LOG_DIR}", file=sys.stderr)
