"""
Art-Net unicast + OSC send/receive helpers.
No optional-dependency imports here — all stdlib.
"""

import socket
import struct

from .constants import ARTNET_PORT, UNIVERSE, OSC_PORT, FRAMES

# ── Art-Net ───────────────────────────────────────────────────────────────────

_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

def _to16(pct: float):
    v = max(0, min(65535, int(float(pct) / 100.0 * 65535)))
    return (v >> 8) & 0xFF, v & 0xFF

def _artnet_packet(dmx: bytes) -> bytes:
    return (b'Art-Net\x00'
            + struct.pack('<H', 0x5000)
            + struct.pack('>H', 14)
            + b'\x00\x00'
            + struct.pack('<H', UNIVERSE)
            + struct.pack('>H', 512)
            + dmx)

def send_universe(targets, positions):
    """Send ONE full universe carrying all windows.

    positions : list of (bottom_pct, top_pct) — one tuple per window, in the
                same order as FRAMES (window i → DMX address FRAMES[i]["dmx"]).
    targets   : list of destination IP strings (deduplicated before sending).
                In broadcast mode all entries are identical → a single packet.
    """
    dmx = bytearray(512)
    for cfg, (bottom_pct, top_pct) in zip(FRAMES, positions):
        off = cfg["dmx"] - 1                      # DMX address 1 → byte index 0
        dmx[off],     dmx[off + 1] = _to16(bottom_pct)
        dmx[off + 2], dmx[off + 3] = _to16(top_pct)
    pkt = _artnet_packet(bytes(dmx))
    for ip in dict.fromkeys(targets):             # dedupe, preserve order
        try:
            _sock.sendto(pkt, (ip, ARTNET_PORT))
        except OSError:
            pass

# ── OSC (calibration triggers + status telemetry) ────────────────────────────
#
# Minimal hand-rolled OSC so no extra dependency is needed.
# We send:    /calibrate, /btm/calibrate, /top/calibrate, /status   (no args)
# We receive: /status  with 8 ints:
#   [cal0, homed0, max0, pos0, cal1, homed1, max1, pos1]
# and map each reply to a window by the packet's source IP.

def _osc_string(s: str) -> bytes:
    b = s.encode("ascii") + b"\x00"
    return b + b"\x00" * ((4 - len(b) % 4) % 4)

def _osc_message(addr: str, *int_args: int) -> bytes:
    msg = _osc_string(addr) + _osc_string("," + "i" * len(int_args))
    for a in int_args:
        msg += struct.pack(">i", int(a))
    return msg

def _osc_read_string(data: bytes, pos: int):
    end = data.index(b"\x00", pos)
    s = data[pos:end].decode("ascii", "replace")
    field = (end - pos) + 1
    field += (4 - field % 4) % 4
    return s, pos + field

def _osc_parse(data: bytes):
    """Return (address, [args]). Supports int and float args."""
    addr, pos = _osc_read_string(data, 0)
    if pos >= len(data):
        return addr, []
    tags, pos = _osc_read_string(data, pos)
    args = []
    for t in tags[1:]:
        if t == "i":
            args.append(struct.unpack_from(">i", data, pos)[0]); pos += 4
        elif t == "f":
            args.append(struct.unpack_from(">f", data, pos)[0]); pos += 4
        elif t == "s":
            _, pos = _osc_read_string(data, pos)
    return addr, args

# Dedicated socket: polls go out from it, ESP replies come back to it (the
# firmware replies to remoteIP:remotePort, i.e. this socket's address).
_osc_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
_osc_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
_osc_sock.settimeout(0.25)

def osc_send(ip: str, addr: str, *int_args: int):
    try:
        _osc_sock.sendto(_osc_message(addr, *int_args), (ip, OSC_PORT))
    except OSError:
        pass
