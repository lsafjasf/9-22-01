"""RFC 6455 framing layer: opcodes, close codes, frame encode/decode.

Nothing in this module knows anything about the handshake or connection
state machine; it only translates between frames and byte streams.
"""

import struct

# Opcodes (RFC 6455 section 5.2)
OPCODE_CONT = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA

DATA_OPCODES = frozenset((OPCODE_CONT, OPCODE_TEXT, OPCODE_BINARY))
CONTROL_OPCODES = frozenset((OPCODE_CLOSE, OPCODE_PING, OPCODE_PONG))
VALID_OPCODES = DATA_OPCODES | CONTROL_OPCODES

FIN_BIT = 0x80
MASK_BIT = 0x80

# A control frame MUST NOT carry payload longer than 125 bytes (5.5).
CONTROL_PAYLOAD_MAX = 125

# Close codes (RFC 6455 section 7.4)
CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_PROTOCOL_ERROR = 1002
CLOSE_UNSUPPORTED = 1003
CLOSE_NO_STATUS = 1005  # never sent on the wire
CLOSE_ABNORMAL = 1006  # never sent on the wire
CLOSE_INVALID_PAYLOAD = 1007
CLOSE_POLICY_VIOLATION = 1008
CLOSE_MESSAGE_TOO_BIG = 1009
CLOSE_MANDATORY_EXTENSION = 1010
CLOSE_INTERNAL_ERROR = 1011
CLOSE_TLS_FAILURE = 1015  # never sent on the wire

# 1000, 1001..1003, 1007..1011 and 3000..4999 are legal on the wire.
_VALID_SENT_CODES = frozenset(
    [CLOSE_NORMAL, CLOSE_GOING_AWAY, CLOSE_PROTOCOL_ERROR,
     CLOSE_UNSUPPORTED, CLOSE_INVALID_PAYLOAD, CLOSE_POLICY_VIOLATION,
     CLOSE_MESSAGE_TOO_BIG, CLOSE_MANDATORY_EXTENSION, CLOSE_INTERNAL_ERROR]
)


def is_valid_sent_close_code(code):
    if code in _VALID_SENT_CODES:
        return True
    return 3000 <= code <= 4999


def is_legal_received_close_code(code):
    """Code check for a frame received from the peer (7.4.2).

    Absent code (None) means the peer sent an empty close payload.
    """
    if code is None:
        return True
    if code in (CLOSE_NO_STATUS, CLOSE_ABNORMAL, CLOSE_TLS_FAILURE):
        return False
    return is_valid_sent_close_code(code)


def apply_mask(payload, mask_key):
    """XOR ``payload`` with the 4-byte ``mask_key``; returns new bytes."""
    if not mask_key:
        return bytes(payload)
    data = bytearray(payload)
    for i in range(len(data)):
        data[i] ^= mask_key[i & 3]
    return bytes(data)


def encode_frame(opcode, payload=b"", fin=True, mask=False):
    """Serialize one frame. Client frames must use ``mask=True``.

    Raises ValueError for obviously unusable arguments (bad opcode, bad
    payload type, oversized control frame); protocol ordering errors are
    the connection layer's job.
    """
    if opcode not in VALID_OPCODES:
        raise ValueError("reserved opcode 0x%x" % opcode)
    if payload is None:
        payload = b""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("payload must be bytes-like")
    payload = bytes(payload)
    if opcode in CONTROL_OPCODES and len(payload) > CONTROL_PAYLOAD_MAX:
        raise ValueError("control frame payload exceeds 125 bytes")

    first = FIN_BIT if fin else 0
    first |= opcode

    length = len(payload)
    if mask:
        import os
        mask_key = os.urandom(4)
        payload = apply_mask(payload, mask_key)

    out = bytearray()
    if length < 126:
        out += bytes((first, length | (MASK_BIT if mask else 0)))
    elif length <= 0xFFFF:
        out += bytes((first, 126 | (MASK_BIT if mask else 0)))
        out += struct.pack("!H", length)
    else:
        out += bytes((first, 127 | (MASK_BIT if mask else 0)))
        out += struct.pack("!Q", length)
    if mask:
        out += mask_key
    out += payload
    return bytes(out)


class Frame:
    """One parsed WebSocket frame."""

    __slots__ = ("fin", "opcode", "payload", "rsv", "masked")

    def __init__(self, fin, opcode, payload, rsv=0, masked=False):
        self.fin = fin
        self.opcode = opcode
        self.payload = payload
        self.rsv = rsv
        self.masked = masked

    def is_control(self):
        return self.opcode in CONTROL_OPCODES

    def __repr__(self):
        return "Frame(fin=%r, opcode=0x%x, %d payload bytes)" % (
            self.fin, self.opcode, len(self.payload))


class EOFError_(Exception):
    """TCP stream ended cleanly in the middle of the connection."""


def _read_exact(reader, n):
    """Read exactly n bytes; raise EOFError_ if the stream ends early."""
    if n == 0:
        return b""
    chunks = []
    remaining = n
    while remaining:
        chunk = reader.read(remaining)
        if not chunk:
            raise EOFError_("stream ended mid-frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(reader, max_payload=None):
    """Read and unmask one frame from a binary buffered ``reader``.

    ``max_payload`` optionally bounds the declared payload length; a frame
    announcing more is rejected with a 1009 protocol error before any
    payload bytes are consumed, so a peer cannot force unbounded reads.

    Raises:
        EOFError_: clean EOF at a frame boundary (connection closed),
                   or a truncated frame.
        ProtocolError: malformed length/encoding on the wire.
    """
    from .exceptions import ProtocolError

    head = reader.read(2)
    if head == b"":
        return None
    if len(head) < 2:
        raise EOFError_("stream ended mid-frame header")

    b0, b1 = head[0], head[1]
    fin = bool(b0 & FIN_BIT)
    rsv = (b0 >> 4) & 0x7
    opcode = b0 & 0x0F
    masked = bool(b1 & MASK_BIT)
    length = b1 & 0x7F

    if length == 126:
        length = struct.unpack("!H", _read_exact(reader, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _read_exact(reader, 8))[0]
        # RFC 6455 5.2: the most significant bit MUST be 0.
        if length >= 1 << 63:
            raise ProtocolError("64-bit length with high bit set")

    if max_payload is not None and length > max_payload:
        raise ProtocolError(
            "frame payload of %d bytes exceeds limit of %d"
            % (length, max_payload), CLOSE_MESSAGE_TOO_BIG)

    mask_key = b""
    if masked:
        mask_key = _read_exact(reader, 4)

    payload = _read_exact(reader, length)
    if masked:
        payload = apply_mask(payload, mask_key)
    return Frame(fin=fin, opcode=opcode, payload=payload, rsv=rsv,
                 masked=masked)


def encode_close_payload(code, reason=""):
    """Build a close frame payload from code + textual reason."""
    if code is None:
        return b""
    reason_bytes = reason.encode("utf-8") if isinstance(reason, str) else bytes(reason)
    return struct.pack("!H", code) + reason_bytes


def decode_close_payload(payload):
    """Parse a close payload into ``(code_or_None, reason_bytes)``.

    Structural validation only: a one-byte payload or a non-UTF-8 reason is
    a 1002/1007 protocol error; whether the *code value* is permitted is
    checked separately by the connection layer.
    """
    from .exceptions import ProtocolError

    if len(payload) < 2:
        if len(payload) == 0:
            return None, b""
        raise ProtocolError("close frame payload of one byte")
    code = struct.unpack("!H", payload[:2])[0]
    reason_bytes = bytes(payload[2:])
    try:
        reason_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise ProtocolError("close reason is not valid UTF-8",
                            CLOSE_INVALID_PAYLOAD)
    return code, reason_bytes
