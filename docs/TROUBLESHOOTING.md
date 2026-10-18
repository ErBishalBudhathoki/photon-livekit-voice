# Troubleshooting

Every entry here is a real failure from building this, in the order the layers
stack. Find your symptom, work out which layer it is in, fix that layer.

The rule that saves the most time: **the symptom is almost never in the layer you
think it is.** "The agent said nothing" was a greeting firing while the phone was
still ringing. "The number is broken" was a trunk credential that did not match.
"The call failed" was a dialer waiting for an answer that took 36 seconds.

Run `python3 bootstrap/90-check.py` first - it checks the layers in dependency
order and usually names the broken one outright.

## By symptom

| Symptom | Layer | Cause and fix |
|---|---|---|
| `/call` returns `ServerError(code=canceled, ... status=408)`, `sip request timed out` | dialer | The dialer was blocking on an answer inside a ring window shorter than the real ring (voicemail took 36s here). The dialer must not block: send the INVITE, return, and let the worker's answered-gate decide. See `cli/call_cli.py`. |
| Call connects, the other side hears nothing, only they talk | worker | The opening line was generated while the line was still ringing and the audio went nowhere. Gate on `sip.callStatus == active` before speaking (`wait_for_answered`). |
| Same, but intermittently, with `1011 Resource has been exhausted` in the log | Gemini Live | Tokens-per-minute ceiling (65K on the free tier), tripped by overlapping Live sessions. One call at a time on the free tier, or a paid key. Retrying `generate_reply` 3x hides the blip, not the cause. |
| The agent hangs up after a second | worker | The model decided it was done. `MIN_TALK_S` (12s) refuses an `end_call` until the agent has spoken *and* the line has been up long enough. |
| The agent talks at an answering machine | worker | `end_call(reason='voicemail')` bypasses the talk guard past `MACHINE_MIN_S` (6s). The outbound persona is instructed to hang up on machines. Inbound never hangs up this way. |
| Every outbound call 401s, or the carrier never accepts anything | trunk credential | The credential in the trunk is not the credential the provider was given. `bootstrap/90-check.py` compares them (lengths only) and fails loudly. Re-run `bootstrap/30-photon.py`. |
| Inbound calls never arrive; outbound works | provider/DNS/firewall | In order of likelihood: the provider's `destinationUri` does not match this host; the A record is **proxied** by Cloudflare (grey cloud only - SIP cannot cross the proxy); the cert name does not match `SIP_DOMAIN`; the cloud security list does not allow the SIP TLS port (the host firewall is the *other* layer, and both matter). |
| `docker compose ps` is all Up but no call works | stack config | Two classics: Redis without persistence (a cache-only Redis loses the phone line across a restart, and the trunk silently goes away), and `use_external_ip: false` in `livekit.yaml` (media has no routable address). |
| The worker restarts forever, never says `registered worker` | worker | `LIVEKIT_URL` / key mismatch, or a Python ABI mismatch in the venv. The ABI one reads as `cryptography: undefined symbol: PyType_GetName` and happens when a foreign `PYTHONPATH` (another venv's, or Hermes') leaks into this venv's interpreter. Run the worker and dialer with a clean environment. |
| The worker is registered but a room never gets a participant | dispatch | No dispatch rule, or a rule whose `room_prefix` does not match the room the dialer creates. |
| `docker exec livekit-sip strings` -> `strings: not found` | tooling | The image has no `strings`. `docker cp` the binary out and run `strings` on the host. |
| Port already in use during bring-up | host | `ss -lntp 'sport = :7880'`. Move the other service, or point `LIVEKIT_API_PORT` elsewhere. Preflight checks all five ports. |
| The transcript is written but the briefing never appears in chat | chat surface | Two causes: the gateway injection grant is missing (`plugins.entries.voice-call.allow_gateway_injection: true`), or the watcher is waiting for a record ordering that does not happen. The worker writes `ended` at hangup and the SDK flushes a farewell *after* it - search for `ended` anywhere in the file, and settle 8s past its own timestamp, rather than requiring it to be last. |
| A briefing arrived but the text is cut off mid-sentence | chat surface | Settled too early. `BRIEF_SETTLE_S` is 8s past the `ended` timestamp for a reason. |
| `/calllog` shows nothing for a room that clearly ran | chat surface | The transcript directory the plugin reads is not the one the worker writes. Both read `AGENT_CALL_LOG_DIR`; check the unit's `EnvironmentFile`. |
| `lineCount: 0` on the voice profile | provider account | No voice-eligible line on the project. No script can create one - get a number in the provider dashboard. |
| Calls connect but the AI is unintelligible / robotic | backend | Check you are on s2s and not a local TTS fallback; `AGENT_VOICE_BACKEND=s2s`, `AGENT_S2S_MODEL=gemini-3.8-live`. |

## The ones that cost the most

**The 408.** The first working `/call` failed with `status=408 sip request timed
out` after exactly 20 seconds. The dialer was holding the INVITE open waiting for
an answer, and LiveKit cancels an INVITE when its ring window closes. Real ring
times to voicemail here were 36.5s and 36.8s - longer than the window. The fix was
structural, not a bigger number: create the room, put the brief in its metadata,
add the SIP participant, return. The worker decides when there is a human, using
the `sip.callStatus` attribute. Ring window: 45s.

**The silence.** A call where the user heard nothing and the agent heard
everything. The opening line had been generated at `session.start`, while the line
was still ringing, and it played into a call nobody had answered yet. The answered
gate now sits *between* session start and the opening line: poll
`ctx.room.remote_participants` for a participant whose `sip.callStatus` is
`active`, and if it never arrives, say nothing at all and leave.

**The early hangup.** On one call the agent said `"Hello,"` and called `end_call`
one second later. A model with a "wrap up the call" instruction and no floor will
wrap up immediately. `end_call` now refuses until the agent has actually spoken and
the line has been up for `MIN_TALK_S`. The refusal is `reply_required=True`, which
tells the model to keep going rather than retry the tool.

**Machines.** Voicemail greetings are exactly the kind of audio a conversational
model will happily answer. The outbound persona is told not to leave a message and
not to wait for a tone; when it reports `reason='voicemail'`, the hangup bypasses
the talk guard - but only past 6s, because a 3s "verdict" is just the model being
impatient.

**The credentials.** Everything looked right and every call 401'd: the trunk held
a different password than the one handed to the provider. Nothing in either system
will tell you this. `bootstrap/90-check.py` compares the trunk against `.env` and
fails as a hard error, which is why it exists.

**The proxy.** An A record with Cloudflare's orange cloud resolves fine, answers
ping, and silently eats SIP. There is no error anywhere - the INVITEs simply never
arrive. Preflight resolves the record and checks it against Cloudflare's published
ranges, because "DNS is fine" is not the same as "DNS is usable for SIP".

## If a call fails and nothing here matches

Collect these four things before changing anything - they identify the layer in
one pass:

```bash
docker compose logs --tail 50 sip                      # did the INVITE arrive?
journalctl --user -u livekit-agent -n 80 --no-pager    # did the worker get a job?
cat calls/<room>.jsonl                                 # what was said, and when
python3 bootstrap/90-check.py                          # the ladder
```
