---
name: photon-livekit-voice
description: Use when installing, verifying, repairing or extending this LiveKit + SIP + Gemini Live voice agent - the gates, the silent failure modes, and the order that avoids them.
version: 1.0.0
license: MIT
---

# Working on photon-livekit-voice

This file is for an agent (or a person) operating this repo. It assumes you can
run commands on the host and you have `sudo` for docker and firewall rules. Follow
the order; the order is the whole trick.

## 1. Mental model

Four processes, three interfaces, one config file.

| Piece | Runs as | Owns | Breaks like |
| --- | --- | --- | --- |
| `livekit-server` | docker, host network | rooms, participant auth, SIP control | API unreachable, workers never register |
| `livekit-sip` | docker, host network | SIP signalling + RTP, trunk matching, digest auth | INVITEs vanish, one-way audio, 403 to the caller |
| `livekit-redis` | docker | trunk and room state (**not** a cache) | rooms vanish on restart, stale trunks |
| agent worker | `systemd --user` unit, python venv | joins rooms, talks via Gemini Live, writes transcripts | registers then dies, silent calls, no transcript |
| `.env` | file, mode 600 | every value above reads from it | everything, confusingly |

Three interfaces you will touch:

- **LiveKit API** (`:7880`) - trunks, dispatch rules, rooms. Gates 2, 5 and the ladder use it.
- **The carrier/provider API** - outbound credential, inbound destination. Only gate 3 writes here.
- **SIP over TLS** (`:5061`) - where the carrier connects. Only the carrier and `tests/inbound_sim.py` use it.

Rule of thumb: if a symptom involves *audio*, suspect network/RTP. If it involves
*the caller hearing nothing or everything*, suspect the Gemini key and its rate
limit. If it involves *the phone not ringing*, suspect the provider. If it involves
*a room that never appears*, suspect the dispatch rule.

## 2. Before you change anything

```bash
cd <repo>
git status                     # expect clean; you are on the user's branch, not a scratch copy
test -f .env && stat -c '%a %n' .env    # must be 600; if it is 644, fix that first
docker ps --format '{{.Names}}'         # is the stack already running?
systemctl --user is-active livekit-agent.service
bash install.sh --check                 # read-only: preflight + the whole ladder
```

Read the `--check` output before touching a thing. It tells you which rung is
already broken, which is usually not the rung the user complained about.

**Never** print, echo, `cat` or paste: `.env`, `certs/*.key`, provider API keys,
trunk credentials, or a full phone number. The scripts already print lengths and
masked values; keep it that way in anything you write. If you need to confirm a
secret exists, check its length or compare with its counterpart - do not read it
out.

## 3. Install order (and what each gate is for)

Run gates individually while debugging; `bash install.sh` runs them in order and
stops at the first failure.

### Gate 0 - preflight

```bash
python3 bootstrap/00-preflight.py
```

Checks: `.env` present and 600, `SIP_DOMAIN` non-placeholder, DNS resolves to
`PUBLIC_IP`, the cert covers `SIP_DOMAIN` and is not expired, ports free (or held
by our own stack), python ≥ 3.10, the provider key works and the line is
voice-eligible.

Be careful: two failures are **expected on a test host** - a dummy domain will not
resolve, and a self-signed cert has no SAN. On a real install, zero failures is the
bar.

### Gate 1 - the stack

```bash
bash bootstrap/10-stack.sh
```

Renders `livekit.yaml`, `sip.yaml`, `docker-compose.yaml` from `templates/`, then
starts redis, livekit-server, livekit-sip (host network). Generates
`LIVEKIT_API_KEY`/`SECRET` if empty and writes them back to `.env` (mode 600).

Be careful: never hand-edit those three rendered files. They are generated and
gitignored; change `.env` or the template, then re-run this gate.

### Gate 2 - SIP objects

```bash
bash bootstrap/40-agent.sh --no-unit     # builds the worker venv; this gate imports the SDK
venv/bin/python bootstrap/20-sip-objects.py
```

Creates the inbound trunk and dispatch rule in LiveKit, generates the carrier
credential if `.env` has none, and prints the credential it expects the provider
to use. Ends with a trunk-vs-`.env` comparison: `username match: True`,
`password match: True`.

Be careful: this gate needs `livekit-api`, so on a clean host it cannot run before
the venv exists. It re-execs into `venv/bin/python` itself and otherwise tells you
the command to run - do not "fix" that by installing the SDK globally.

Updating an existing trunk requires care that is already encoded here: the LiveKit
API rejects an **empty** list field on an update (`unsupported list update
operation`), so list fields are only sent when non-empty. If you rewrite this gate,
keep that property.

### Gate 3 - the provider

```bash
venv/bin/python bootstrap/30-photon.py
```

Reads the provider's endpoints, issues or reuses an outbound credential, creates the
LiveKit outbound trunk, and **writes the provider's inbound destination** to
`sip:<SIP_DOMAIN>:<SIP_TLS_PORT>;transport=tls`.

Be careful: this is the only gate that changes state outside this host. On a test
or rehearsal box it will point the live number at the test box and inbound calls
will stop arriving at the real one. Run it only on the host that should receive
calls. To rehearse elsewhere, build the outbound trunk directly from `.env` values
instead - the credential is already there, and nothing upstream needs to change.

### Gate 4 - the worker

```bash
bash bootstrap/40-agent.sh
```

Builds the venv (uv, CPython 3.10 pinned), renders and enables
`livekit-agent.service`, restarts it, and waits for `registered worker` in the log.

Be careful: restarting the unit **drops any call in progress**. Do it between
calls, not during one.

### Gate 5 - the dialer

```bash
bash bootstrap/50-dialer.sh
```

Confirms the dialer can authenticate to the API and sees the outbound trunk with
the right address. No call is placed.

## 4. Verify before you claim it works

In this order. Every rung is cheap and each isolates one layer.

```bash
# 1. the ladder: 20 checks over stack, credentials and worker
python3 bootstrap/90-check.py

# 2. the agent speaks, with no telephony at all (measures audio energy)
venv/bin/python tests/room_test.py

# 3. inbound: a fake carrier INVITE over TLS, digest auth, dispatch, greeting
venv/bin/python tests/inbound_sim.py

# 4. a real call, only after 1-3 are green
venv/bin/python cli/call_cli.py --to <number> --instruction '<brief>'
```

`tests/room_test.py` passes only if audio above a noise floor comes back from the
agent - "the agent said something" is not asserted from a log line.
`tests/inbound_sim.py` sends mu-law silence as RTP on purpose: a SIP leg that
carries no RTP is torn down in seconds and the failure looks like "the agent did
not greet".

Say what you verified and what you did not. Inbound telephony can only be proven on
a host with a public IP and a carrier pointing at it.

## 5. Failure playbook

| Symptom | First thing to run | Likely cause |
| --- | --- | --- |
| Install stops at gate 0, "does not resolve" | `dig +short $SIP_DOMAIN` | grey-cloud record missing, or `PUBLIC_IP` stale |
| Gate 0 "certificate does not cover" | `openssl x509 -in certs/sip.crt -noout -text \| grep -A1 SAN` | cert issued for a different name; re-run `bootstrap/cert-acme.sh` |
| Gate 2 `ModuleNotFoundError: livekit` | `ls venv/bin/python` | the venv does not exist yet; run `bash bootstrap/40-agent.sh --no-unit` |
| Gate 2 HTTP 400 "unsupported list update operation" | `python3 bootstrap/90-check.py` | an empty list field was sent on update; send only non-empty lists |
| Gate 3 HTTP 403 | - | 403 from this provider means the request had no User-Agent; check for a proxy |
| Gate 3 HTTP 401 | - | the provider API key is wrong or expired |
| Gate 3 HTTP 404 / 422 | - | wrong project id (404) or malformed id (422) |
| Gate 4 never says "registered worker" | `journalctl --user -u livekit-agent -n 50` | wrong `LIVEKIT_URL`/keys, or port 8083 taken |
| Outbound fails instantly, 401 | `python3 bootstrap/90-check.py` rung 2 | trunk credential ≠ provider; re-run gate 3 |
| Outbound times out (408) | - | nobody answered in the ring window; `--ring-timeout` and retry |
| Call connects, callee hears nothing | `ss -unp \| grep 1000` on the host | RTP range blocked in one of the two firewall layers |
| Agent silent mid-call | `journalctl ... \| grep -i 1011` | Gemini tokens-per-minute ceiling; serialise calls |
| Agent hangs up after one word | - | machine verdict path; check `AGENT_MACHINE_MIN_S` and the log line |
| Agent will not hang up | - | `AGENT_MIN_TALK_S` guard: it must have spoken and the line must be that old |
| Inbound rings, caller gets 403 | `python3 bootstrap/90-check.py` rung 2 | provider destination or credential; gate 3 |
| Briefing never reaches chat | plugin logs; `ls calls/` | `AGENT_CALL_LOG_DIR` mismatch or `allow_gateway_injection` off |

Working with logs: `journalctl --user -u livekit-agent -f` for the worker,
`docker logs -f livekit-sip` for signalling, `docker logs -f livekit-server` for
room control. The worker logs JSON per event and plain `[agent] ...` lines for its
own decisions (answered gate, hangup reason, delegation).

## 6. Hard rules

1. **Gate 3 only on the host that should receive calls.** It repoints the provider's
   inbound destination.
2. **Never print a secret.** No `cat .env`, no echoing a key, no pasting a full
   phone number into a report. Masked values only.
3. **Never hand-edit rendered configs** (`livekit.yaml`, `sip.yaml`,
   `docker-compose.yaml`). Change `.env` or `templates/`, re-run gate 1.
4. **Never bump the SDK casually.** `livekit-agents`, `livekit-api`,
   `livekit-plugins-google`, `livekit-protocol` are pinned because the answered gate
   reads a protocol-level attribute. Bump one, re-run the ladder and both
   telephony-free tests.
5. **Never enable delegation without an allowlist.** `AGENT_DELEGATION_ENABLED=true`
   with an empty `AGENT_DELEGATION_ALLOWLIST` lets any caller reach a local agent
   session.
6. **Never delete `calls/`** to "clean up": it is the only record of what was said.
   Add it to `.gitignore` (already done) rather than removing transcripts.
7. **Never proxy the SIP DNS record.** Grey cloud only; an orange cloud breaks SIP
   with no error anywhere.
8. **Do not restart the worker mid-call.**
9. **Do not install the SDK globally** to make a gate import work. Use the venv.
10. **Do not commit `.env`.** `scripts/secret-scan.py` will refuse, and CI repeats it.

## 7. Rehearsing on a clean host

Useful before publishing a change to the installer, and the only way to find bugs
that only exist on a fresh machine (interpreter assumptions, missing venv, gate
ordering).

1. Fresh Linux box with docker, `systemd --user` + linger, egress, a few GB free.
2. Copy the repo **minus** `venv/`, `.git/`, `certs/`, `calls/`, `.env` and the three
   rendered configs.
3. Give it its own `.env`: non-resolving `SIP_DOMAIN`, its own `PUBLIC_IP`, blank
   `LIVEKIT_*` so gate 1 generates a pair, and only the values that must come from
   the origin host (provider key/ids, model key) - never the origin's LiveKit keys,
   TLS paths or ports.
4. Move files with the provider's SSH config + `scp`. Some sandbox CLIs advertise
   `exec --stdin` and silently drop stdin; if you see `not in gzip format`, that is
   why.
5. Run gates 0, 1, 2, 4, 5 - and run **each twice**. The second run is the update
   path, which is where idempotence bugs live.
6. Skip gate 3 (see rule 1). Skip telephony: a sandbox publishes HTTPS URLs, not
   raw ports, so SIP/RTP never arrive. `tests/room_test.py` and
   `tests/inbound_sim.py` are what a sandbox *can* prove.
7. Destroy the sandbox and land the fixes before anyone else runs the installer.

## 8. Rotating a credential

Order matters; each step is safe to stop at.

1. Provider API key -> dashboard, then `.env`.
2. Carrier credential -> delete `SIP_INBOUND_USER`/`SIP_INBOUND_PASS`, run gate 2, then gate 3, then the ladder.
3. Model API key -> `.env`, `systemctl --user restart livekit-agent`.
4. LiveKit key/secret -> `.env`, re-run gate 1 (re-renders configs), restart the unit.
5. TLS cert -> `bash bootstrap/cert-acme.sh`, restart `livekit-sip`.

If a credential was ever pushed to a git remote, rotate first and rewrite history
second. A pushed secret is compromised regardless of how briefly it was public.

## 9. Definition of done

Before reporting success:

- [ ] `bash install.sh --check` exits 0 (20/20 on the ladder)
- [ ] `venv/bin/python tests/room_test.py` passes (audio measured, not assumed)
- [ ] `venv/bin/python tests/inbound_sim.py` passes 7/7 on the host that receives calls
- [ ] one real outbound call completed, transcript written, briefing posted if the plugin is installed
- [ ] `python3 scripts/secret-scan.py --strict` is clean
- [ ] `git status` clean

Then say which rungs you proved, on which host, and which ones remain unproven -
telephony on a host with no public IP, or inbound before gate 3 has ever run.

## 10. Useful facts

- Worker health: `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8083/` → 200
- Registration evidence: `journalctl --user -u livekit-agent | grep 'registered worker'`
- Trunk objects: the ladder (`bootstrap/90-check.py`) lists them masked - there is no
  separate CLI for it.
- Transcripts: `calls/<room>.jsonl`, one JSON object per line, `kind` is one of
  `call`, `answered`, `said`, `heard`, `ended`.
- Inbound rooms are prefixed by `ROOM_PREFIX` (default `call-`); outbound by
  `outbound-`.
- SIP digest: the trunk challenges `407` with `Proxy-Authenticate`, realm `LiveKit`,
  and expects `Proxy-Authorization`.
- `AGENT_MIN_TALK_S=12`, `AGENT_MACHINE_MIN_S=6`, `AGENT_HANGUP_GRACE_S=3.5` are the
  three knobs that decide when a call may end.
