"""Connection state machine: message-level API on top of the framing layer.

States (RFC 6455 section 3, "The WebSocket Connection"):
    CONNECTING --handshake done--> OPEN
    OPEN       --close sent------> CLOSING
    CLOSING    --peer close seen-> CLOSED
    OPEN/CLOSING --heartbeat timeout / fatal protocol error--> CLOSED

A connection object is single-use: once CLOSED it never reopens.
"""

import socket
import threading
import time

from .exceptions import WebSocketError, ProtocolError, CloseError
from .framing import (
    OPCODE_CONT, OPCODE_TEXT, OPCODE_BINARY,
    OPCODE_CLOSE, OPCODE_PING, OPCODE_PONG,
    DATA_OPCODES, CONTROL_OPCODES, VALID_OPCODES,
    CLOSE_NORMAL, CLOSE_PROTOCOL_ERROR, CLOSE_INVALID_PAYLOAD,
    CLOSE_POLICY_VIOLATION, CLOSE_MESSAGE_TOO_BIG,
    CLOSE_ABNORMAL, CLOSE_NO_STATUS,
    CONTROL_PAYLOAD_MAX,
    encode_frame, read_frame, EOFError_,
    encode_close_payload, decode_close_payload,
    is_valid_sent_close_code, is_legal_received_close_code,
)

STATE_CONNECTING = "CONNECTING"
STATE_OPEN = "OPEN"
STATE_CLOSING = "CLOSING"
STATE_CLOSED = "CLOSED"

# Defaults for the resource limits. Both are deliberately finite so a
# hostile or broken peer can never make us buffer without bound.
DEFAULT_MAX_MESSAGE_SIZE = 16 * 1024 * 1024
DEFAULT_MAX_FRAGMENTS = 1024

_NO_MESSAGE = object()  # internal sentinel: frame consumed, keep looping


class SocketReader:
    """Minimal buffered reader over a socket.

    Unlike ``sock.makefile("rb")`` this stays consistent when a
    ``socket.timeout`` fires mid-read (the standard library documents that
    a buffered file object may lose data in that case), which the
    heartbeat / recv-timeout logic relies on. Provides the ``read`` /
    ``readline`` subset used by the handshake and framing layers.
    """

    def __init__(self, sock):
        self.sock = sock
        self._buf = bytearray()

    def _fill(self, need):
        chunk = self.sock.recv(max(65536, need))
        if chunk:
            self._buf += chunk
        return chunk

    def read(self, n):
        """Return up to n bytes; short only at EOF or timeout."""
        while len(self._buf) < n:
            if not self._fill(n - len(self._buf)):
                break
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def readline(self):
        while True:
            idx = self._buf.find(b"\n")
            if idx >= 0:
                line = bytes(self._buf[:idx + 1])
                del self._buf[:idx + 1]
                return line
            if not self._fill(1):
                line = bytes(self._buf)
                del self._buf[:]
                return line


class WSConnection:
    """One WebSocket connection (client or server side).

    Parameters:
        sock:             connected socket (blocking mode).
        reader:           optional buffered reader; defaults to a
                          SocketReader over ``sock``.
        is_client:        clients mask outgoing frames and must receive
                          unmasked frames; servers are the reverse.
        max_message_size: reassembled message byte limit; a peer
                          exceeding it is closed with 1009.
        max_fragments:    fragment-count limit per message; a peer
                          exceeding it is closed with 1008.
        ping_interval:    seconds between automatic pings while idle in
                          recv_message; None disables the heartbeat.
        ping_timeout:     seconds to wait for any frame after a ping
                          before the connection is declared dead.
        close_timeout:    seconds close() waits for the peer's close
                          frame before giving up and closing the socket.
    """

    def __init__(self, sock, reader=None, is_client=False,
                 max_message_size=DEFAULT_MAX_MESSAGE_SIZE,
                 max_fragments=DEFAULT_MAX_FRAGMENTS,
                 ping_interval=None, ping_timeout=10.0,
                 close_timeout=5.0, state=STATE_CONNECTING,
                 subprotocol=None):
        self.sock = sock
        self.reader = reader if reader is not None else SocketReader(sock)
        self.is_client = is_client
        self.max_message_size = max_message_size
        self.max_fragments = max_fragments
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.close_timeout = close_timeout
        self.state = state
        self.subprotocol = subprotocol

        # Outcome of the close handshake, visible once state is CLOSED.
        self.close_code = None    # peer's code, or None if it sent none
        self.close_reason = ""

        self._send_lock = threading.Lock()

        # Fragmented-message reassembly state.
        self._frag_opcode = None
        self._frag_parts = []
        self._frag_size = 0

        # Heartbeat bookkeeping (monotonic clock).
        self._next_ping = None
        self._pong_deadline = None
        if ping_interval is not None:
            self._next_ping = time.monotonic() + ping_interval

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def send_message(self, data, fragment_size=None):
        """Send one message. ``str`` is sent as text, bytes-like as binary.

        ``fragment_size`` splits the payload into frames of at most that
        many bytes (first frame carries the message opcode, the rest are
        continuations). Only valid while the connection is OPEN.
        """
        if self.state != STATE_OPEN:
            raise WebSocketError(
                "cannot send data while connection is %s" % self.state)
        if isinstance(data, str):
            opcode = OPCODE_TEXT
            payload = data.encode("utf-8")
        elif isinstance(data, (bytes, bytearray, memoryview)):
            opcode = OPCODE_BINARY
            payload = bytes(data)
        else:
            raise TypeError("message must be str or bytes-like")

        if not fragment_size or len(payload) <= fragment_size:
            self._send_frame(opcode, payload)
            return
        if fragment_size < 1:
            raise ValueError("fragment_size must be >= 1")
        chunks = [payload[i:i + fragment_size]
                  for i in range(0, len(payload), fragment_size)]
        with self._send_lock:
            for index, chunk in enumerate(chunks):
                self._send_frame_locked(
                    opcode if index == 0 else OPCODE_CONT,
                    chunk, fin=index == len(chunks) - 1)

    def recv_message(self, timeout=None):
        """Return the next complete message (``str`` for text, ``bytes``
        for binary). Returns ``None`` when the peer closes the connection
        cleanly; ``close_code``/``close_reason`` then describe the close.

        Ping/pong and fragmentation are handled internally. Raises
        ``TimeoutError`` if no complete message arrives within ``timeout``
        seconds, ``ProtocolError`` on a fatal frame violation, and
        ``CloseError`` on abnormal loss (including heartbeat timeout).
        """
        deadline = (time.monotonic() + timeout
                    if timeout is not None else None)
        while True:
            if self.state == STATE_CLOSED:
                raise CloseError(
                    "connection is closed",
                    self.close_code
                    if self.close_code is not None else CLOSE_NO_STATUS,
                    self.close_reason)
            try:
                frame = self._read_frame(deadline)
            except TimeoutError:
                raise  # application-level recv timeout, not a transport error
            except ProtocolError as exc:
                # Frame-layer violation (bad length, oversized payload):
                # shut down per protocol with the carried close code.
                self._fail_protocol(str(exc), exc.close_code)
            except (EOFError_, OSError) as exc:
                self._on_transport_lost(exc)
            if frame is None:  # clean TCP EOF
                self._on_transport_lost(EOFError_("stream ended"))
            if self._pong_deadline is not None:
                # Any inbound frame proves liveness.
                self._pong_deadline = None
                self._next_ping = (time.monotonic() + self.ping_interval
                                   if self.ping_interval is not None
                                   else None)
            try:
                result = self._handle_frame(frame)
            except ProtocolError as exc:
                if self.state != STATE_CLOSED:
                    self._fail_protocol(str(exc), exc.close_code)
                raise
            if result is not _NO_MESSAGE:
                return result

    def ping(self, payload=b""):
        """Send a ping; the peer's pong is consumed by recv_message."""
        if self.state not in (STATE_OPEN, STATE_CLOSING):
            raise WebSocketError(
                "cannot ping while connection is %s" % self.state)
        self._send_frame(OPCODE_PING, payload)

    def close(self, code=CLOSE_NORMAL, reason=""):
        """Perform the closing handshake and shut down the transport.

        Sends a close frame with ``code``/``reason``, waits up to
        ``close_timeout`` for the peer's close frame, then closes the
        socket. Idempotent: safe to call when already closing/closed.
        """
        if self.state == STATE_CLOSED:
            return
        if not is_valid_sent_close_code(code):
            raise ValueError("invalid close code: %r" % code)
        payload = encode_close_payload(code, reason)
        if len(payload) > CONTROL_PAYLOAD_MAX:
            raise ValueError("close reason too long (payload > 125 bytes)")
        if self.state == STATE_OPEN:
            # Transition first: the peer may observe (and answer) the
            # close frame before sendall() returns.
            self.state = STATE_CLOSING
            try:
                self._send_frame(OPCODE_CLOSE, payload)
            except OSError:
                pass
        # Wait for the peer's close frame (or transport EOF/timeout).
        deadline = time.monotonic() + self.close_timeout
        try:
            while self.state == STATE_CLOSING:
                if time.monotonic() >= deadline:
                    break
                try:
                    frame = self._read_frame(deadline)
                except (EOFError_, OSError, socket.timeout):
                    break
                if frame is None:
                    break
                try:
                    self._handle_frame(frame)
                except ProtocolError:
                    break
        finally:
            self._terminate()

    # ------------------------------------------------------------------
    # frame I/O
    # ------------------------------------------------------------------
    def _send_frame(self, opcode, payload, fin=True):
        with self._send_lock:
            self._send_frame_locked(opcode, payload, fin)

    def _send_frame_locked(self, opcode, payload, fin=True):
        if self.state == STATE_CLOSED:
            raise WebSocketError("connection is closed")
        data = encode_frame(opcode, payload, fin=fin, mask=self.is_client)
        self.sock.sendall(data)

    def _read_frame(self, deadline):
        """Read one frame, driving the heartbeat and ``deadline``.

        Returns a Frame, or None on clean TCP EOF at a frame boundary.
        """
        while True:
            timeout = self._compute_timeout(deadline)
            self.sock.settimeout(timeout)
            try:
                return read_frame(self.reader,
                                  max_payload=self.max_message_size)
            except socket.timeout:
                self._on_timeout_tick(deadline)

    def _compute_timeout(self, deadline):
        now = time.monotonic()
        waits = []
        if deadline is not None:
            waits.append(deadline - now)
        if self._pong_deadline is not None:
            waits.append(self._pong_deadline - now)
        elif self._next_ping is not None:
            waits.append(self._next_ping - now)
        if not waits:
            return None  # block indefinitely
        return max(0.01, min(waits))

    def _on_timeout_tick(self, deadline):
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            raise TimeoutError("no complete message within timeout")
        if self._pong_deadline is not None and now >= self._pong_deadline:
            self._terminate()
            raise CloseError(
                "heartbeat timeout: peer silent for %.1fs after ping"
                % self.ping_timeout, CLOSE_ABNORMAL)
        if (self._pong_deadline is None and self._next_ping is not None
                and now >= self._next_ping):
            try:
                self._send_frame(OPCODE_PING, b"")
            except OSError:
                self._on_transport_lost(OSError("send failed"))
            self._pong_deadline = now + self.ping_timeout

    # ------------------------------------------------------------------
    # inbound frame dispatch
    # ------------------------------------------------------------------
    def _handle_frame(self, frame):
        """Validate and dispatch one frame.

        Returns a complete message, None for a clean close, or
        _NO_MESSAGE when the frame was consumed internally.
        """
        self._check_frame_shape(frame)
        opcode = frame.opcode

        if opcode in CONTROL_OPCODES:
            return self._handle_control(frame)

        # Data frames: fragmentation state machine (RFC 6455 5.4).
        if opcode == OPCODE_CONT:
            if self._frag_opcode is None:
                self._fail_protocol(
                    "continuation frame without a fragmented message")
        else:
            if self._frag_opcode is not None:
                self._fail_protocol(
                    "new data frame while a fragmented message is open")
            if frame.fin:
                return self._complete_message(opcode, frame.payload)
            self._frag_opcode = opcode
            self._frag_parts = [frame.payload]
            self._frag_size = len(frame.payload)
            self._check_message_limits(1)
            return _NO_MESSAGE

        # Continuation of an open fragmented message.
        self._frag_parts.append(frame.payload)
        self._frag_size += len(frame.payload)
        self._check_message_limits(len(self._frag_parts))
        if not frame.fin:
            return _NO_MESSAGE
        payload = b"".join(self._frag_parts)
        opcode = self._frag_opcode
        self._frag_opcode = None
        self._frag_parts = []
        self._frag_size = 0
        return self._complete_message(opcode, payload)

    def _check_frame_shape(self, frame):
        if frame.rsv:
            self._fail_protocol("RSV bits set without a negotiated "
                                "extension")
        if frame.opcode not in VALID_OPCODES:
            self._fail_protocol("reserved opcode 0x%x" % frame.opcode)
        # RFC 6455 5.2: client frames MUST be masked, server frames
        # MUST NOT be.
        if self.is_client and frame.masked:
            self._fail_protocol("server sent a masked frame")
        if not self.is_client and not frame.masked:
            self._fail_protocol("client sent an unmasked frame")
        if frame.opcode in CONTROL_OPCODES:
            if not frame.fin:
                self._fail_protocol("fragmented control frame")
            if len(frame.payload) > CONTROL_PAYLOAD_MAX:
                self._fail_protocol("control frame payload exceeds "
                                    "125 bytes")

    def _handle_control(self, frame):
        if frame.opcode == OPCODE_PING:
            if self.state in (STATE_OPEN, STATE_CLOSING):
                try:
                    self._send_frame(OPCODE_PONG, frame.payload)
                except OSError:
                    pass
            return _NO_MESSAGE
        if frame.opcode == OPCODE_PONG:
            return _NO_MESSAGE
        # Close frame.
        code, reason_bytes = decode_close_payload(frame.payload)
        if not is_legal_received_close_code(code):
            self._fail_protocol("illegal close code %r" % code)
        self.close_code = code
        self.close_reason = reason_bytes.decode("utf-8")
        if self.state == STATE_OPEN:
            # Peer initiated: echo a close frame to finish the handshake.
            try:
                self._send_frame(OPCODE_CLOSE, frame.payload)
            except OSError:
                pass
        self._terminate()
        return None

    def _complete_message(self, opcode, payload):
        if opcode == OPCODE_TEXT:
            try:
                return payload.decode("utf-8")
            except UnicodeDecodeError:
                self._fail_protocol("text message is not valid UTF-8",
                                    CLOSE_INVALID_PAYLOAD)
        return payload

    def _check_message_limits(self, fragment_count):
        if self._frag_size > self.max_message_size:
            self._fail_protocol(
                "message exceeds %d byte limit" % self.max_message_size,
                CLOSE_MESSAGE_TOO_BIG)
        if fragment_count > self.max_fragments:
            self._fail_protocol(
                "message exceeds %d fragment limit" % self.max_fragments,
                CLOSE_POLICY_VIOLATION)

    # ------------------------------------------------------------------
    # termination paths
    # ------------------------------------------------------------------
    def _fail_protocol(self, message, close_code=CLOSE_PROTOCOL_ERROR):
        """Fatal frame violation: close per protocol, then raise."""
        try:
            if self.state in (STATE_OPEN, STATE_CLOSING):
                reason = str(message)[:120]
                self._send_frame(OPCODE_CLOSE,
                                 encode_close_payload(close_code, reason))
        except OSError:
            pass
        self._terminate()
        raise ProtocolError(message, close_code)

    def _on_transport_lost(self, exc):
        """TCP EOF or reset. Clean only if a close frame was exchanged."""
        was_closing = self.state == STATE_CLOSING
        self._terminate()
        if was_closing:
            raise CloseError("connection closed during close handshake",
                             self.close_code
                             if self.close_code is not None
                             else CLOSE_ABNORMAL,
                             self.close_reason)
        raise CloseError("connection lost without close handshake: %s"
                         % exc, CLOSE_ABNORMAL)

    def _terminate(self):
        self.state = STATE_CLOSED
        self._frag_opcode = None
        self._frag_parts = []
        try:
            self.sock.close()
        except OSError:
            pass
