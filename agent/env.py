"""Tiny .env loader and shared helpers.

Deliberately dependency-free: this runs in the same venv as the agent and the
dialer, and a missing python-dotenv should never be the reason a phone line is
down. Handles the shapes a .env actually contains: comments, blank lines,
`export ` prefixes, single/double quoted values, and values containing `=`.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


def find_env_file(start: Path | None = None) -> Path | None:
    """The nearest .env at or above `start` (default: this file's directory)."""
    here = (start or Path(__file__).resolve().parent).resolve()
    for d in (here, *here.parents):
        cand = d / ".env"
        if cand.is_file():
            return cand
    return None


def load_env_file(path: Path | str | None = None, override: bool = False) -> Path | None:
    """Load KEY=VALUE pairs into os.environ. Existing vars win by default.

    Precedence that matters: systemd's EnvironmentFile beats a stale .env on
    disk, so a value set in the unit is never silently replaced here.
    """
    path = Path(path) if path else find_env_file()
    if not path or not path.is_file():
        return None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if not key or (not override and key in os.environ):
            continue
        os.environ[key] = val
    return path


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def env_float(name: str, default: float) -> float:
    try:
        return float(env(name) or default)
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(float(env(name) or default))
    except ValueError:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    raw = env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


_DIGITS = re.compile(r"\D+")


def digits(number: str) -> str:
    return _DIGITS.sub("", number or "")


def numbers_match(a: str, b: str, tail: int = 9) -> bool:
    """Compare two phone numbers tolerating country-code and trunk-prefix noise.

    +61412345678, 61412345678 and 0412345678 all end in the same 9 digits, and
    the caller-ID a carrier delivers rarely matches the format an operator typed
    into .env. Comparing the last 9 digits is what makes an allowlist usable;
    a shorter-than-9-digit input falls back to whole-string comparison.
    """
    da, db = digits(a), digits(b)
    if not da or not db:
        return False
    if len(da) < tail or len(db) < tail:
        return da == db
    return da[-tail:] == db[-tail:]


def mask(number: str) -> str:
    """Never print a full phone number: keep the last 4 digits only."""
    d = digits(number)
    if not d:
        return "(unknown)"
    if len(d) <= 4:
        return "*" * len(d)
    return "*" * (len(d) - 4) + d[-4:]


def normalise_number(given: str) -> str:
    """Turn what a human typed into an E.164-ish dial string, or raise ValueError.

    Kept deliberately dumb: a leading + is honoured, otherwise the number is
    passed through with separators removed. Guessing a country code here would
    silently dial the wrong country.
    """
    raw = (given or "").strip()
    if not raw:
        raise ValueError("no number given")
    plus = raw.startswith("+")
    d = digits(raw)
    if len(d) < 6:
        raise ValueError(f"{given!r} does not look like a dialable number")
    return ("+" if plus else "") + d
