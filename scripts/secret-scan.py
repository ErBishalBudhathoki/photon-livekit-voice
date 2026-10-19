#!/usr/bin/env python3
"""Refuse to publish secrets: scan what a push would actually send.

The repo is designed so that every live value lives in `.env`, `certs/` and
`calls/`, all gitignored. That is only half of it - the other half is proving it,
every time, including for a value that was committed once and deleted later.

Three passes:

  files    the files git would push (tracked + untracked-but-not-ignored), by name
           and by content
  history  every added line of every commit, so a deleted secret cannot hide
  needles  values from the local `.env` and the local `.secretscan`, matched
           against that text

Rules are two kinds: shapes (`AIza...`, `sk-...`, private key blocks, Bearer
tokens, long hex) and install identifiers - your own number, host, IPs, provider
ids - which belong in `.secretscan` (gitignored) so a public copy of this tool
does not carry them either.

Output is always redacted: rule name, file, line, first four characters.

    python3 scripts/secret-scan.py              # files + history, fails on a hit
    python3 scripts/secret-scan.py --staged     # what `git commit` would add
    python3 scripts/secret-scan.py --no-history # faster, files only
    python3 scripts/secret-scan.py --strict     # also fail on identifiers
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
DENY_FILE = ROOT / ".secretscan"

# A value under one of these keys is a live credential or a personal identifier.
# Anchored on purpose: a loose "IP" matches inside "SIP_IDENTITY", and a loose
# "USER" matches "SIP_USER_AGENT" - both are names, not credentials.
SENSITIVE_KEY = re.compile(
    r"(PASS|SECRET|TOKEN|NUMBER|EMAIL|API_KEY|ACCESS_KEY|PRIVATE_KEY|PROJECT_ID|"
    r"PROFILE_ID|DOMAIN|(^|_)IP$|(^|_)HOST$|_KEY$|(^|_)USER$)", re.I)
BENIGN_VALUE = re.compile(
    r"^(127\.0\.0\.1|localhost|0\.0\.0\.0|ws://127\.0\.0\.1.*|/certs/.*|"
    r"gemini-[a-z0-9.-]+|puck|australia/.*|utc|en_.*|true|false|\d{1,5})$", re.I)

SHAPES = [
    ("google-api-key", re.compile(r"AIza[0-9A-Za-z_-]{30,}")),
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("slack-token", re.compile(r"\bxox[abpsr]-[A-Za-z0-9-]{10,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("bearer-token", re.compile(r"Bearer\s+[A-Za-z0-9._-]{25,}")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("long-hex", re.compile(r"\b[0-9a-fA-F]{40,}\b")),
    ("long-base64", re.compile(r"\b[A-Za-z0-9+/]{48,}={0,2}\b")),
]

FORBIDDEN_NAMES = [
    (re.compile(r"(^|/)\.env$"), ".env"),
    (re.compile(r"(^|/)\.env\.(?!example$)[\w.-]+$"), ".env.*"),
    (re.compile(r"(^|/)certs?/", re.I), "certs/"),
    (re.compile(r"(^|/)calls?/", re.I), "calls/"),
    (re.compile(r"\.(pem|key|crt|p12|pfx|jks|ovpn)$", re.I), "key material"),
    (re.compile(r"(^|/)id_(rsa|ed25519|ecdsa|dsa)"), "private ssh key"),
    (re.compile(r"(^|/)(\.netrc|\.pgpass|\.npmrc|credentials|token)\.?[\w.-]*$"), "credential file"),
    (re.compile(r"\.jsonl$"), "call transcript"),
    (re.compile(r"\.(bak|log|sqlite|db)$"), "runtime artefact"),
]


def run(*args: str) -> str:
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True).stdout


def redact(match: str) -> str:
    return (match[:4] + "***") if len(match) > 6 else "***"


def load_needles(strict: bool) -> dict[str, str]:
    """Live values this install must never publish."""
    needles: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k, v = k.strip(), v.strip().strip("\"'")
            if len(v) >= 8 and SENSITIVE_KEY.search(k) and not BENIGN_VALUE.match(v):
                needles[f"env:{k}"] = v
    if DENY_FILE.exists():
        for line in DENY_FILE.read_text().splitlines():
            s = line.strip()
            if s and not s.startswith("#") and len(s) >= 6:
                needles[f"deny:{s[:3]}…"] = s
    if not strict:
        needles = {k: v for k, v in needles.items() if k.startswith("env:")}
    return needles


def scan(text: str, label: str, needles: dict[str, str]) -> list[str]:
    hits = []
    for key, value in needles.items():
        if value and value in text:
            hits.append(f"{label}: leaks {key} ({redact(value)})")
    for name, rx in SHAPES:
        for m in rx.finditer(text):
            hits.append(f"{label}: looks like {name} ({redact(m.group(0))})")
    return hits


def pushed_files() -> list[str]:
    tracked = run("git", "ls-files").split()
    untracked = run("git", "ls-files", "--others", "--exclude-standard").split()
    return sorted(set(tracked + untracked))


def scan_files(files: list[str], needles: dict[str, str]) -> list[str]:
    hits = []
    for rel in files:
        for pattern, what in FORBIDDEN_NAMES:
            if pattern.search(rel):
                hits.append(f"file:{rel}: {what} must never be pushed")
        path = ROOT / rel
        try:
            text = path.read_text(errors="ignore")
        except (OSError, UnicodeError):
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            hits += scan(line, f"{rel}:{line_no}", needles)
    return hits


def scan_history(needles: dict[str, str]) -> list[str]:
    hits = []
    log = run("git", "log", "-p", "--all", "--no-color", "--format=COMMIT %h")
    commit = "?"
    for line in log.splitlines():
        if line.startswith("COMMIT "):
            commit = line.split(maxsplit=1)[1]
        elif line.startswith("+") and not line.startswith("+++"):
            hits += scan(line, f"history:{commit}", needles)
    return hits


def scan_staged(needles: dict[str, str]) -> list[str]:
    hits = []
    for rel in run("git", "diff", "--cached", "--name-only").split():
        for pattern, what in FORBIDDEN_NAMES:
            if pattern.search(rel):
                hits.append(f"staged:{rel}: {what} must never be committed")
    diff = run("git", "diff", "--cached", "--unified=0", "--no-color")
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            hits += scan(line, "staged", needles)
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--staged", action="store_true", help="only what git would commit")
    ap.add_argument("--no-history", action="store_true", help="skip the commit history")
    ap.add_argument("--strict", action="store_true",
                    help="also match identifiers from .secretscan")
    args = ap.parse_args()

    needles = load_needles(args.strict)
    print(f"secret-scan: {len(needles)} needle(s), "
          f"{len(SHAPES)} shape rule(s), "
          f"mode={'staged' if args.staged else 'files' + ('' if args.no_history else '+history')}")

    hits: list[str] = []
    if args.staged:
        hits += scan_staged(needles)
    else:
        hits += scan_files(pushed_files(), needles)
        if not args.no_history:
            hits += scan_history(needles)

    seen: dict[str, int] = {}
    for h in hits:
        seen[h] = seen.get(h, 0) + 1
    if not seen:
        print("  clean: nothing to publish looks like a secret, a key file or a "
              "transcript")
        return 0
    print(f"\n  {len(seen)} finding(s) - do not push this:\n")
    for h, n in sorted(seen.items()):
        print(f"    x{n:<3} {h}")
    print("\n  Remove the value, and if it was ever committed, rotate it: history "
          "removal does not\n  un-publish a secret that has already been pushed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
