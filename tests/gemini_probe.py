#!/usr/bin/env python3
"""Probe the Gemini Live API directly: does s2s work, and where is the ceiling?

Worth running before blaming the stack for silence. It takes the voice backend out
of the picture entirely - no LiveKit, no SIP, no worker - and asks the API one
question.

Phase 1 opens a single Live session and measures time-to-first-audio.
Phase 2 opens sessions back-to-back until one fails, which is how the real
tokens-per-minute ceiling shows up. The number it prints is what the quota page
does not tell you: the binding column is tokens/min, not requests/min.

The key is read in-process from the repo .env (or GEMINI_API_KEY in the
environment) and never printed - only its length and a masked form.

    venv/bin/python tests/gemini_probe.py [MODEL] [BURST]
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agent"))

from env import env, load_env_file  # noqa: E402

load_env_file()


def key() -> str:
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_LIVE_API_KEY"):
        value = env(name)
        if value:
            print(f"key {name}: {value[:6]}...{value[-4:]} (len={len(value)})")
            return value
    print("no Gemini key found: set GEMINI_API_KEY in .env")
    return ""


async def one_session(client, types, model: str, prompt: str):
    """One Live session, one text turn -> (ok, time-to-first-audio, error, bytes)."""
    cfg = types.LiveConnectConfig(response_modalities=["AUDIO"])
    started = time.time()
    ttfa = None
    audio_bytes = 0
    try:
        async with client.aio.live.connect(model=model, config=cfg) as session:
            await session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=prompt)]),
                turn_complete=True)
            async for msg in session.receive():
                content = getattr(msg, "server_content", None)
                if content is None:
                    continue
                turn = getattr(content, "model_turn", None)
                if turn is not None and getattr(turn, "parts", None):
                    for part in turn.parts:
                        blob = getattr(part, "inline_data", None)
                        if blob is not None and getattr(blob, "data", None):
                            if ttfa is None:
                                ttfa = time.time() - started
                            audio_bytes += len(blob.data)
                if getattr(content, "turn_complete", False):
                    break
        return True, ttfa, None, audio_bytes
    except Exception as exc:  # noqa: BLE001
        return False, ttfa, f"{type(exc).__name__}: {str(exc)[:300]}", audio_bytes


async def main() -> int:
    api_key = key()
    if not api_key:
        return 2
    model = sys.argv[1] if len(sys.argv) > 1 else env("AGENT_S2S_MODEL", "gemini-3.8-live")
    burst = int(sys.argv[2]) if len(sys.argv) > 2 else 12

    import warnings
    warnings.filterwarnings("ignore")
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    print(f"\n--- phase 1: a single Live session ({model}) ---")
    ok, ttfa, err, nbytes = await one_session(
        client, types, model, "Reply with exactly the words: testing one two three.")
    if ok:
        print(f"  session OK | time to first audio: "
              f"{f'{ttfa:.2f}s' if ttfa is not None else 'no audio'} | {nbytes} bytes")
    else:
        print(f"  session FAILED -> {err}")
        if "1011" in (err or "") or "exhausted" in (err or "").lower():
            print("  the key works but is rate-limited right now - wait a minute and retry")
        return 1

    print(f"\n--- phase 2: {burst} sessions back-to-back ---")
    succeeded = 0
    first_fail = None
    for i in range(1, burst + 1):
        ok, _, err, _ = await one_session(client, types, model, "hi")
        if ok:
            succeeded += 1
            print(f"  {i:>2}: ok")
        else:
            print(f"  {i:>2}: FAIL -> {err}")
            first_fail = first_fail or (i, err)
    print(f"\n  succeeded {succeeded}/{burst}")
    if first_fail:
        i, err = first_fail
        err = err or ""
        print(f"  first failure at session #{i}: {err}")
        if any(token in err for token in ("429", "RESOURCE_EXHAUSTED", "1011")) \
                or "quota" in err.lower():
            print(f"  => throttled after ~{i - 1} rapid sessions; the binding limit is "
                  f"tokens/min, not sessions/min")
            print("     concurrent calls are what trip this: serialise them, or use a paid key")
        elif "403" in err or "PERMISSION" in err:
            print("  => not a rate limit: this key or project has no Live API access")
    else:
        print(f"  => no throttling in {burst} back-to-back sessions")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
