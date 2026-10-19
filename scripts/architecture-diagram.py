#!/usr/bin/env python3
"""Generate docs/architecture.svg - the diagram at the top of the README.

Diagram as code: edit the boxes and arrows here, run this, commit both the script
and the SVG. Flat palette on purpose (slate strokes, white boxes, one accent per
layer, small status LEDs), no gradients, 1240x700 so GitHub renders it legibly.

    python3 scripts/architecture-diagram.py
"""
from pathlib import Path
from xml.sax.saxutils import escape

W, H = 1240, 700
CANVAS = "#eceef1"
INK = "#20252b"
MUTED = "#4b5560"
BOX = "#ffffff"
HOST = "#e2e6ea"
HOST_EDGE = "#98a1ac"

ACCENT = {
    "carrier": "#a35a00",   # amber - the phone network
    "media": "#0f6f66",     # teal - audio path
    "control": "#3b4a5a",   # slate - control plane
    "state": "#6b6f76",     # grey - state / artefacts
    "model": "#7a4a00",     # dark amber - the model
    "surface": "#0f5f8a",   # steel blue - chat surface
}

FONT = ("ui-monospace, SFMono-Regular, Menlo, Consolas, 'DejaVu Sans Mono', "
        "monospace")

parts: list[str] = []


def box(x, y, w, h, title, lines=(), accent="control", led=None):
    """A flat component box with a 4px accent rail on the left."""
    parts.append(
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="2" fill="{BOX}" '
        f'stroke="{INK}" stroke-width="1.4"/>')
    parts.append(
        f'<rect x="{x}" y="{y}" width="4" height="{h}" fill="{ACCENT[accent]}"/>')
    tx = x + 14
    ty = y + (22 if lines else h / 2 + 4)
    parts.append(f'<text x="{tx}" y="{ty}" font-family="{FONT}" font-size="13" '
                 f'font-weight="600" fill="{INK}">{escape(title)}</text>')
    for i, line in enumerate(lines):
        parts.append(f'<text x="{tx}" y="{ty + 18 + i * 15}" font-family="{FONT}" '
                     f'font-size="10.5" fill="{MUTED}">{escape(line)}</text>')
    if led:
        cx, cy = x + w - 14, y + 13
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="3.2" fill="{led}"/>')


def text(x, y, s, size=11, fill=None, weight="400", anchor="start", opacity=1):
    parts.append(
        f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" '
        f'font-weight="{weight}" fill="{fill or MUTED}" text-anchor="{anchor}" '
        f'opacity="{opacity}">{escape(s)}</text>')


def arrow(x1, y1, x2, y2, label=None, dashed=False, both=False, color=None,
          label_dy=-7):
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    head = f'marker-end="url(#{"both" if both else "end"})"'
    start = 'marker-start="url(#both)" ' if both else ""
    parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                 f'stroke="{color or INK}" stroke-width="1.5"{dash} '
                 f'{start}{head}/>')
    if label:
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2 + label_dy
        wpx = len(label) * 6.0 + 8
        if abs(x2 - x1) < 2:          # vertical: put the label to the right
            mx, my = x1 + 8, (y1 + y2) / 2 + 3
            parts.append(f'<rect x="{mx - 2}" y="{my - 9}" width="{wpx}" '
                         f'height="13" fill="{CANVAS}" opacity="0.92"/>')
            text(mx + 2, my, label, 10, MUTED)
        else:
            parts.append(f'<rect x="{mx - wpx / 2}" y="{my - 9}" '
                         f'width="{wpx}" height="13" fill="{CANVAS}" '
                         f'opacity="0.92"/>')
            text(mx, my, label, 10, MUTED, anchor="middle")


# ---------------------------------------------------------------- canvas
parts.append(f'<rect width="{W}" height="{H}" fill="{CANVAS}"/>')
parts.append(
    '<defs>'
    f'<marker id="end" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" '
    f'markerHeight="7" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="{INK}"/>'
    '</marker>'
    f'<marker id="both" viewBox="0 0 8 8" refX="4" refY="4" markerWidth="7" '
    f'markerHeight="7" orient="auto"><path d="M0,4 L4,0 L8,4 L4,8 z" fill="{INK}"/>'
    '</marker>'
    '</defs>')

text(40, 46, "photon-livekit-voice", 20, INK, "700")
text(40, 68, "one host, one number, speech-to-speech. Solid = audio or control in "
             "the call path; dashed = optional", 11.5, MUTED)

# ------------------------------------------------------------ host boundary
parts.append(f'<rect x="300" y="112" width="680" height="424" rx="3" '
             f'fill="{HOST}" stroke="{HOST_EDGE}" stroke-width="1.4" '
             f'stroke-dasharray="7 5"/>')
text(316, 134, "YOUR HOST  ·  public IPv4  ·  systemd --user + docker", 10.5,
     "#5d6672", "600")

# ------------------------------------------------------------------ outside
text(44, 132, "OUTSIDE", 9.5, "#77808c", "700")
box(40, 148, 210, 62, "PSTN", ["callee / caller"], "carrier")
box(40, 258, 210, 62, "Carrier SBC", ["SIP trunk provider"], "carrier")
arrow(145, 210, 145, 258, "call", label_dy=-4)

# --------------------------------------------------------------- host boxes
box(324, 236, 186, 96, "livekit-sip", ["5061/tcp  SIP over TLS",
                                      "10000-10100/udp  RTP"], "media", "#1f7a1f")
box(560, 236, 186, 96, "livekit-server", ["7880  API / WS", "7881/tcp  7882/udp"],
    "control", "#1f7a1f")
box(796, 236, 160, 96, "agent worker", ["unit: livekit-agent", ":8083 health"],
    "media", "#1f7a1f")
box(560, 396, 186, 58, "redis", ["6380 · trunks + rooms"], "state")
box(796, 396, 160, 58, "calls/", ["<room>.jsonl"], "state")

arrow(250, 284, 324, 284)
arrow(324, 316, 250, 316, dashed=False, color=ACCENT["media"])
# right-aligned just outside the host boundary, so no label sits on the dashed line
text(296, 276, "SIP/TLS", 9.5, MUTED, anchor="end")
text(296, 308, "RTP", 9.5, MUTED, anchor="end")
arrow(510, 284, 560, 284, "room")
arrow(746, 284, 796, 284, "job")
arrow(625, 332, 625, 396, "state", label_dy=0)
arrow(876, 332, 876, 396, "append", label_dy=0)

# ---------------------------------------------------------------- the model
text(1030, 132, "CLOUD", 9.5, "#77808c", "700")
box(1010, 236, 190, 96, "Gemini Live", ["speech to speech", "Live API · WSS"],
    "model")
arrow(956, 289, 1010, 289, dashed=False)

# --------------------------------------------------------------- chat layer
box(1010, 396, 190, 58, "chat surface", ["/callme · /call",
                                          "/calllog · briefing"], "surface")
arrow(956, 425, 1010, 425, dashed=True)

# ------------------------------------------------------ the two call flows
text(40, 452, "OUTBOUND", 9.5, "#77808c", "700")
text(40, 470, "cli/call_cli.py or /call", 10.5, INK, "600")
text(40, 486, "room first, brief in", 9.5, MUTED)
text(40, 499, "metadata, then dial", 9.5, MUTED)

text(40, 530, "INBOUND", 9.5, "#77808c", "700")
text(40, 548, "INVITE → dispatch rule", 10.5, INK, "600")
text(40, 564, "room call-<n>, worker joins,", 10, MUTED)
text(40, 578, "waits for callStatus=active", 10, MUTED)

# -------------------------------------------------------------- behaviour
parts.append(f'<rect x="300" y="562" width="680" height="104" rx="3" '
             f'fill="#f6f7f9" stroke="{HOST_EDGE}" stroke-width="1"/>')
text(316, 584, "WHAT THE WORKER GUARDS", 10, "#5d6672", "700")
text(316, 604, "waits for sip.callStatus=active before the first word  ·  "
               "refuses end_call before AGENT_MIN_TALK_S", 10.5, MUTED)
text(316, 622, "drops voicemail instead of leaving a message  ·  writes one "
               "JSON-lines transcript per call", 10.5, MUTED)
text(316, 640, "delegation off by default; when on it needs a caller-ID allowlist",
     10.5, MUTED)
text(316, 658, "every value it reads comes from .env (mode 600, gitignored)",
     10.5, MUTED)

# ------------------------------------------------------------------ legend
lx, ly = 1010, 480
parts.append(f'<line x1="{lx}" y1="{ly}" x2="{lx + 34}" y2="{ly}" stroke="{INK}" '
             f'stroke-width="1.5" marker-end="url(#end)"/>')
text(lx + 42, ly + 4, "in the call path", 10, MUTED)
parts.append(f'<line x1="{lx}" y1="{ly + 22}" x2="{lx + 34}" y2="{ly + 22}" '
             f'stroke="{INK}" stroke-width="1.5" stroke-dasharray="5 4" '
             f'marker-end="url(#end)"/>')
text(lx + 42, ly + 26, "optional", 10, MUTED)
text(lx, ly + 52, "ports are defaults", 10, MUTED)
text(lx, ly + 68, "see .env.example", 10, MUTED)
text(1010, 584, "carrier bills per minute", 10, MUTED)
text(1010, 600, "model bills per token", 10, MUTED)
text(1010, 616, "(bounded by tokens/min)", 10, MUTED)

svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
       f'viewBox="0 0 {W} {H}" role="img" '
       f'aria-label="Architecture: PSTN and a carrier SBC reach livekit-sip over '
       f'SIP/TLS, which hands the call to livekit-server, which dispatches a job '
       f'to the agent worker; the worker talks to Gemini Live and writes '
       f'transcripts the chat surface can read.">\n  '
       + "\n  ".join(parts) + "\n</svg>\n")

out = Path(__file__).resolve().parent.parent / "docs" / "architecture.svg"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(svg)
print(f"wrote {out} ({len(svg)} bytes)")
