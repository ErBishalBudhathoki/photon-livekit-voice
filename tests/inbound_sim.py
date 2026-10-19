#!/usr/bin/env python3
"""Prove the INBOUND path without a phone line and without a provider.

The other tests cover everything except "a call arrives and the trunk accepts it".
This one stands in for the provider's SBC: it opens a TLS connection to the SIP
listener, sends an INVITE for the number the trunk owns, answers the digest
challenge with the trunk's own credential, and then checks what the stack did
with it:

  1. the SIP listener accepts the INVITE and challenges (407/401)
  2. our digest is accepted            -> 180 Ringing, then 200 OK
  3. the dispatch rule creates a room  -> name starts with ROOM_PREFIX
  4. the worker auto-joins that room   -> an AGENT participant appears
  5. the agent greets                  -> transcript gets call/answered/said

Pass means: trunk number match, digest auth, dispatch rule and worker dispatch
all work together. What it cannot prove is the outer edge - a public DNS name,
a certificate a carrier will trust, published ports, and the provider's own
destination setting. Those are host/provider items, listed in the README.

    venv/bin/python tests/inbound_sim.py              # 15s hold
    venv/bin/python tests/inbound_sim.py --hold 30
    venv/bin/python tests/inbound_sim.py --host 127.0.0.1 --port 5061
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import secrets
import socket
import ssl
import struct
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agent"))

from env import env, load_env_file  # noqa: E402

load_env_file()

from livekit.api import LiveKitAPI  # noqa: E402
from livekit.protocol.room import ListParticipantsRequest, ListRoomsRequest  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def record(state: str, what: str, detail: str = "") -> None:
    results.append((state, what, detail))
    print(f"  [{state}] {what}" + (f" - {detail}" if detail else ""))


def mask(num: str) -> str:
    return num if len(num) <= 4 else f"{num[:2]}****{num[-4:]}"


def parse_challenge(header: str) -> dict[str, str]:
    header = re.sub(r"^Digest\s+", "", header, flags=re.I)
    out: dict[str, str] = {}
    for m in re.finditer(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', header):
        out[m.group(1).lower()] = m.group(2) if m.group(2) is not None else m.group(3)
    return out


def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def digest_auth(ch: dict[str, str], method: str, uri: str, user: str, pw: str) -> str:
    realm, nonce = ch["realm"], ch["nonce"]
    qop = "auth" if "auth" in ch.get("qop", "") else ""
    ha1 = md5(f"{user}:{realm}:{pw}")
    ha2 = md5(f"{method}:{uri}")
    nc, cnonce = "00000001", secrets.token_hex(8)
    resp = (md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}") if qop
            else md5(f"{ha1}:{nonce}:{ha2}"))
    parts = [f'username="{user}"', f'realm="{realm}"', f'nonce="{nonce}"',
             f'uri="{uri}"', f'response="{resp}"',
             f"algorithm={ch.get('algorithm', 'MD5')}"]
    if qop:
        parts += [f"qop={qop}", f"nc={nc}", f'cnonce="{cnonce}"']
    if ch.get("opaque"):
        parts.append(f'opaque="{ch["opaque"]}"')
    return "Digest " + ", ".join(parts)


class RtpSource(threading.Thread):
    """Send silence to the address the stack offered, 160 bytes every 20 ms.

    A SIP leg that carries no RTP is dropped after a few seconds, and then the test
    measures LiveKit's patience instead of the agent's first turn. A carrier always
    sends something; so does this.
    """

    PAYLOAD = b"\xff" * 160  # G.711 mu-law silence

    def __init__(self, host: str, port: int, local_port: int) -> None:
        super().__init__(daemon=True)
        self.dest = (host, port)
        self.local_port = local_port
        self.seq = 0
        self.ts = 0
        self.ssrc = secrets.randbits(32)
        self._stop = threading.Event()

    def run(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind(("0.0.0.0", self.local_port))
        except OSError:
            s.bind(("0.0.0.0", 0))
        s.settimeout(1.0)
        next_at = time.time()
        while not self._stop.is_set():
            head = struct.pack("!BBHII", 0x80, 0, self.seq & 0xFFFF, self.ts,
                               self.ssrc)
            try:
                s.sendto(head + self.PAYLOAD, self.dest)
            except OSError:
                break
            self.seq += 1
            self.ts += 160
            next_at += 0.02
            time.sleep(max(0.0, next_at - time.time()))
        s.close()

    def stop(self) -> None:
        self._stop.set()


class Dialog:
    """A minimal SIP UAC over TLS: enough of INVITE/ACK/BYE to be a provider."""

    def __init__(self, host: str, port: int, domain: str, number: str,
                 user: str, password: str) -> None:
        self.host, self.port, self.domain = host, port, domain
        self.number, self.user, self.password = number, user, password
        self.uri = f"sip:{number}@{domain}"
        self.call_id = secrets.token_hex(12) + "@inbound-sim"
        self.from_tag = secrets.token_hex(6)
        self.branch = "z9hG4bK" + secrets.token_hex(8)
        self.rtp_port = 40000 + secrets.randbelow(1000)
        self.sock: ssl.SSLSocket | None = None
        self.to_tag = ""
        self.rtp: RtpSource | None = None

    def _local_uri(self) -> str:
        return f"sip:sim@{self.host}:{self.port};transport=tls"

    def _msg(self, method: str, cseq: str, auth: str = "",
             auth_header: str = "Authorization", with_sdp: bool = False) -> bytes:
        lines = [
            f"{method} {self.uri} SIP/2.0",
            f"Via: SIP/2.0/TLS {self.host}:{self.port};branch={self.branch};rport",
            "Max-Forwards: 70",
            f"From: <sip:inbound-sim@local.test>;tag={self.from_tag}",
            f"To: <{self.uri}>" + (f";tag={self.to_tag}" if self.to_tag else ""),
            f"Call-ID: {self.call_id}",
            f"CSeq: {cseq} {method}",
            f"Contact: <{self._local_uri()}>",
            "User-Agent: photon-livekit-voice/inbound-sim",
        ]
        if auth:
            lines.append(f"{auth_header}: {auth}")
        if not with_sdp:
            lines += ["Content-Length: 0", "", ""]
            return "\r\n".join(lines).encode()
        body = "\r\n".join([
            "v=0",
            f"o=- 1 1 IN IP4 {self.host}",
            "s=inbound-sim",
            f"c=IN IP4 {self.host}",
            "t=0 0",
            f"m=audio {self.rtp_port} RTP/AVP 0 101",
            "a=rtpmap:0 PCMU/8000",
            "a=rtpmap:101 telephone-event/8000",
            "a=sendrecv",
        ]) + "\r\n"
        lines += ["Content-Type: application/sdp", f"Content-Length: {len(body)}",
                  "", body]
        return "\r\n".join(lines).encode()

    def connect(self) -> None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_alpn_protocols(["sip"])
        try:
            raw = socket.create_connection((self.host, self.port), timeout=10)
            self.sock = ctx.wrap_socket(raw, server_hostname=self.domain)
        except ssl.SSLError:
            # Some carriers' SBCs reject ALPN "sip"; LiveKit accepts both.
            ctx2 = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx2.check_hostname = False
            ctx2.verify_mode = ssl.CERT_NONE
            raw = socket.create_connection((self.host, self.port), timeout=10)
            self.sock = ctx2.wrap_socket(raw, server_hostname=self.domain)
        assert self.sock is not None

    def _read(self, buf: bytes, timeout: float = 8.0) -> tuple[str, bytes]:
        assert self.sock is not None
        self.sock.settimeout(timeout)
        while True:
            if b"\r\n\r\n" in buf:
                head, rest = buf.split(b"\r\n\r\n", 1)
                m = re.search(rb"Content-Length:\s*(\d+)", head, re.I)
                need = int(m.group(1)) if m else 0
                if len(rest) >= need:
                    return head.decode(errors="replace"), rest[need:]
            chunk = self.sock.recv(65535)
            if not chunk:
                raise ConnectionError("the SIP listener closed the connection")
            buf += chunk

    def _final(self, buf: bytes, timeout: float = 8.0) -> tuple[str, bytes]:
        for _ in range(10):
            head, buf = self._read(buf, timeout)
            line = head.splitlines()[0]
            code = line.split()[1]
            print(f"    <- {line}")
            if not code.startswith("1"):
                return head, buf
        raise RuntimeError("no final SIP response")

    def invite(self) -> bool:
        assert self.sock is not None
        self.sock.sendall(self._msg("INVITE", "1", with_sdp=True))
        head, buf = self._final(b"")
        code = head.split()[1]
        if code in ("401", "407"):
            want = "proxy-authenticate" if code == "407" else "www-authenticate"
            send_as = "Proxy-Authorization" if code == "407" else "Authorization"
            challenge = ""
            for line in head.splitlines():
                if line.lower().startswith(want + ":"):
                    challenge = line.split(":", 1)[1].strip()
            if not challenge:
                record(FAIL, "the trunk challenged with a digest we can read",
                       f"{code} carried no {want} header")
                return False
            ch = parse_challenge(challenge)
            record(PASS, "the trunk challenged for credentials",
                   f"{code} realm={ch.get('realm')!r} algorithm="
                   f"{ch.get('algorithm', 'MD5')}")
            auth = digest_auth(ch, "INVITE", self.uri, self.user, self.password)
            self.sock.sendall(self._msg("INVITE", "2", auth=auth,
                                        auth_header=send_as, with_sdp=True))
            head, buf = self._final(buf)
            code = head.split()[1]
        if code != "200":
            reason = head.splitlines()[0]
            record(FAIL, "the trunk accepts the credential", reason)
            record(FAIL, "the call is answered with 200 OK", "the stack refused the INVITE")
            print("    " + head.replace("\r\n", "\n    ")[:700])
            return False
        record(PASS, "the trunk accepts the credential", "digest accepted")
        record(PASS, "the call is answered with 200 OK", self.call_id.split("@")[0])
        m = re.search(r"To:.*;tag=([^;\r\n]+)", head, re.I)
        self.to_tag = m.group(1) if m else ""
        self.sock.sendall(self._msg("ACK", "2"))
        self._start_rtp(head)
        return True

    def _start_rtp(self, head: str) -> None:
        """Where the stack wants audio, straight out of its answer SDP."""
        if "\r\n\r\n" not in head:
            return
        sdp = head.split("\r\n\r\n", 1)[1]
        addr = re.search(r"c=IN IP4 (\S+)", sdp)
        port = re.search(r"m=audio (\d+)", sdp)
        if not (addr and port):
            print("    (no usable SDP in the answer - not sending RTP)")
            return
        self.rtp = RtpSource(addr.group(1), int(port.group(1)), self.rtp_port)
        self.rtp.start()
        print(f"    -> sending mu-law silence to {addr.group(1)}:{port.group(1)} "
              f"from {self.rtp_port}")

    def bye(self) -> None:
        if not self.sock:
            return
        if self.rtp:
            self.rtp.stop()
        try:
            self.sock.sendall(self._msg("BYE", "3"))
            head, _ = self._final(b"", timeout=6)
            print(f"    <- {head.splitlines()[0]}")
        except Exception as exc:  # noqa: BLE001
            print(f"    (no BYE reply: {type(exc).__name__} - the room may already be gone)")
        finally:
            self.sock.close()
            self.sock = None


async def trunk_number() -> str | None:
    """The number the inbound trunk owns - the stack knows it even when .env does not."""
    from livekit.protocol.sip import ListSIPInboundTrunkRequest

    api = LiveKitAPI(env("LIVEKIT_URL"), env("LIVEKIT_API_KEY"), env("LIVEKIT_API_SECRET"))
    name = env("SIP_INBOUND_TRUNK") or "photon-inbound"
    try:
        trunks = (await api.sip.list_inbound_trunk(ListSIPInboundTrunkRequest())).items
        trunk = next((t for t in trunks if t.name == name), None)
        return (list(trunk.numbers)[0] if trunk and trunk.numbers else None)
    finally:
        await api.aclose()


async def check_room(prefix: str, before: set[str], hold: int) -> str | None:
    api = LiveKitAPI(env("LIVEKIT_URL"), env("LIVEKIT_API_KEY"), env("LIVEKIT_API_SECRET"))
    found: str | None = None
    try:
        deadline = time.time() + hold
        while time.time() < deadline:
            rooms = (await api.room.list_rooms(ListRoomsRequest())).rooms
            new = [r for r in rooms if r.name.startswith(prefix) and r.name not in before]
            if new:
                found = new[0].name
                parts = (await api.room.list_participants(
                    ListParticipantsRequest(room=found))).participants
                agents = [p for p in parts if p.kind == 4]
                if agents:
                    record(PASS, "the dispatch rule created a room", found)
                    record(PASS, "the worker auto-joined it",
                           f"{agents[0].identity} ({len(parts)} participant(s))")
                    # The first turn needs the answered gate plus a Gemini round trip;
                    # measure it by waiting for the transcript rather than by sleeping
                    # a number that happened to be long enough on one host.
                    await wait_for_greeting(found)
                    return found
            await asyncio.sleep(1)
        return found
    finally:
        await api.aclose()


async def wait_for_greeting(room: str, timeout: float = 20.0) -> bool:
    path = REPO / "calls" / f"{room}.jsonl"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and '"kind": "said"' in path.read_text():
            return True
        await asyncio.sleep(1)
    return False


def report(room: str | None) -> int:
    if room:
        path = REPO / "calls" / f"{room}.jsonl"
        if path.exists():
            lines = path.read_text().splitlines()
            kinds = [__import__("json").loads(x).get("kind") for x in lines if x.strip()]
            record(PASS if "said" in kinds else FAIL, "the agent greeted",
                   f"{path.name}: {', '.join(kinds)}")
        else:
            record(FAIL, "the agent greeted", f"no transcript at {path}")
    print("\n=== summary ===")
    failed = [r for r in results if r[0] == FAIL]
    for state, what, _ in results:
        print(f"  {state:4s} {what}")
    if failed:
        print(f"\n{len(failed)} check(s) failed. Inbound is not wired end to end.")
        return 1
    print("\nInbound works inside the stack: TLS, number match, digest auth, dispatch, "
          "agent join, greeting.\nWhat this cannot prove: public DNS, a certificate the "
          "carrier trusts, published ports,\nand the provider's inbound destination.")
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(env("SIP_TLS_PORT") or 5061))
    ap.add_argument("--hold", type=int, default=15)
    args = ap.parse_args()

    user, password = env("SIP_INBOUND_USER"), env("SIP_INBOUND_PASS")
    if not (user and password):
        print("missing from .env: SIP_INBOUND_USER / SIP_INBOUND_PASS - "
              "run bootstrap/20-sip-objects.py")
        return 2
    number = env("SIP_INBOUND_NUMBER") or await trunk_number()
    if not number:
        print("no inbound number: .env has none and the trunk owns none - "
              "run bootstrap/20-sip-objects.py")
        return 2

    prefix = env("ROOM_PREFIX") or "call-"
    print(f"=== simulating a provider INVITE for {mask(number)} to {args.host}:{args.port} ===")

    api = LiveKitAPI(env("LIVEKIT_URL"), env("LIVEKIT_API_KEY"), env("LIVEKIT_API_SECRET"))
    try:
        before = {r.name for r in (await api.room.list_rooms(ListRoomsRequest())).rooms}
    finally:
        await api.aclose()

    d = Dialog(args.host, args.port, env("SIP_DOMAIN") or "localhost",
               number, user, password)
    try:
        d.connect()
        tls_version = d.sock.version() if d.sock else "?"
        record(PASS, "TLS to the SIP listener", tls_version)
    except Exception as exc:  # noqa: BLE001
        record(FAIL, "TLS to the SIP listener", f"{type(exc).__name__}: {exc}")
        return report(None)

    answered = d.invite()
    room = await check_room(prefix, before, args.hold) if answered else None
    if answered and not room:
        record(FAIL, "the dispatch rule created a room", f"nothing with prefix {prefix!r}")
    d.bye()
    return report(room)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
