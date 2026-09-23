"""State machine, heartbeat, and resource-limit behavior."""

import socket
import struct
import threading
import time
import unittest

from wsmini.exceptions import (
    WebSocketError, ProtocolError, CloseError,
)
from wsmini.framing import (
    OPCODE_PING, OPCODE_PONG, OPCODE_CLOSE, OPCODE_TEXT, OPCODE_CONT,
    encode_frame, read_frame,
)
from wsmini.connection import (
    WSConnection, STATE_CONNECTING, STATE_OPEN, STATE_CLOSING,
    STATE_CLOSED,
)

from util import make_pair, raw_pair
from test_framing import SocketReaderWrap


class StateMachineTest(unittest.TestCase):
    def test_initial_state_is_connecting(self):
        a, b = socket.socketpair()
        conn = WSConnection(a)
        self.assertEqual(conn.state, STATE_CONNECTING)
        a.close()
        b.close()

    def test_full_lifecycle_states(self):
        client, server = make_pair()
        self.assertEqual(client.state, STATE_OPEN)
        self.assertEqual(server.state, STATE_OPEN)

        def run_server():
            server.recv_message()

        thread = threading.Thread(target=run_server)
        thread.start()
        client.close()
        thread.join(timeout=2.0)
        self.assertEqual(client.state, STATE_CLOSED)
        self.assertEqual(server.state, STATE_CLOSED)

    def test_recv_after_close_raises(self):
        client, server = make_pair(close_timeout=0.2)
        client.close()
        with self.assertRaises(CloseError):
            client.recv_message()
        server.close()

    def test_abnormal_transport_loss(self):
        client, server = make_pair()
        client.sock.close()  # vanish without a close frame
        with self.assertRaises(CloseError) as ctx:
            server.recv_message()
        self.assertEqual(ctx.exception.close_code, 1006)
        self.assertEqual(server.state, STATE_CLOSED)


class HeartbeatTest(unittest.TestCase):
    def test_heartbeat_timeout_terminates(self):
        # Peer never pongs: the connection must die abnormally.
        raw, server = raw_pair(
            server_ping_interval=0.05, server_ping_timeout=0.15)
        raw.settimeout(2.0)
        start = time.monotonic()
        with self.assertRaises(CloseError) as ctx:
            server.recv_message()
        elapsed = time.monotonic() - start
        self.assertEqual(ctx.exception.close_code, 1006)
        self.assertEqual(server.state, STATE_CLOSED)
        self.assertLess(elapsed, 1.5)
        # A ping was sent before the timeout killed the connection.
        ping = read_frame(SocketReaderWrap(raw))
        self.assertEqual(ping.opcode, OPCODE_PING)
        raw.close()

    def test_pong_keeps_connection_alive(self):
        # A peer that pongs (but sends no messages) survives; the recv
        # side observes only its own application-level timeout.
        client, server = make_pair(
            server_ping_interval=0.05, server_ping_timeout=0.5,
            client_close_timeout=0.2, server_close_timeout=0.2)

        def client_loop():
            # Auto-responds to pings while waiting for messages.
            try:
                client.recv_message(timeout=0.6)
            except (TimeoutError, CloseError):
                pass

        thread = threading.Thread(target=client_loop)
        thread.start()
        with self.assertRaises(TimeoutError):
            server.recv_message(timeout=0.4)
        # Still healthy after several ping rounds.
        self.assertEqual(server.state, STATE_OPEN)
        server.send_message("still here")
        thread.join(timeout=2.0)
        client.close()
        server.close()

    def test_ping_gets_pong_with_same_payload(self):
        raw, server = raw_pair()
        raw.sendall(encode_frame(OPCODE_PING, b"token", mask=True))
        with self.assertRaises(TimeoutError):
            server.recv_message(timeout=0.2)
        reply = read_frame(SocketReaderWrap(raw))
        self.assertEqual(reply.opcode, OPCODE_PONG)
        self.assertEqual(reply.payload, b"token")
        raw.close()


class LimitTest(unittest.TestCase):
    def _close_code_seen_by(self, raw):
        frame = read_frame(SocketReaderWrap(raw))
        self.assertEqual(frame.opcode, OPCODE_CLOSE)
        return struct.unpack("!H", frame.payload[:2])[0]

    def test_single_frame_over_limit_closed_1009(self):
        raw, server = raw_pair(server_max_message_size=100)
        raw.sendall(encode_frame(OPCODE_TEXT, b"x" * 200, mask=True))
        with self.assertRaises(ProtocolError) as ctx:
            server.recv_message()
        self.assertEqual(ctx.exception.close_code, 1009)
        self.assertEqual(self._close_code_seen_by(raw), 1009)
        self.assertEqual(server.state, STATE_CLOSED)
        raw.close()

    def test_fragmented_message_over_limit_closed_1009(self):
        raw, server = raw_pair(server_max_message_size=100)
        raw.sendall(encode_frame(OPCODE_TEXT, b"x" * 60, fin=False,
                                 mask=True))
        raw.sendall(encode_frame(OPCODE_CONT, b"y" * 60, fin=True,
                                 mask=True))
        with self.assertRaises(ProtocolError) as ctx:
            server.recv_message()
        self.assertEqual(ctx.exception.close_code, 1009)
        self.assertEqual(self._close_code_seen_by(raw), 1009)
        raw.close()

    def test_fragment_count_over_limit_closed_1008(self):
        raw, server = raw_pair(server_max_fragments=3)
        raw.sendall(encode_frame(OPCODE_TEXT, b"a", fin=False, mask=True))
        for _ in range(3):
            raw.sendall(encode_frame(OPCODE_CONT, b"b", fin=False,
                                     mask=True))
        with self.assertRaises(ProtocolError) as ctx:
            server.recv_message()
        self.assertEqual(ctx.exception.close_code, 1008)
        self.assertEqual(self._close_code_seen_by(raw), 1008)
        raw.close()

    def test_message_at_exact_limit_accepted(self):
        client, server = make_pair(server_max_message_size=100,
                                   close_timeout=0.2)
        client.send_message(b"x" * 100)
        self.assertEqual(server.recv_message(), b"x" * 100)
        client.close()
        server.close()


class MessageValidationTest(unittest.TestCase):
    def test_invalid_utf8_text_closed_1007(self):
        raw, server = raw_pair()
        raw.sendall(encode_frame(OPCODE_TEXT, b"\xff\xfe", mask=True))
        with self.assertRaises(ProtocolError) as ctx:
            server.recv_message()
        self.assertEqual(ctx.exception.close_code, 1007)
        raw.close()

    def test_invalid_utf8_across_fragments_closed_1007(self):
        raw, server = raw_pair()
        raw.sendall(encode_frame(OPCODE_TEXT, b"\xc3", fin=False,
                                 mask=True))
        raw.sendall(encode_frame(OPCODE_CONT, b"\xff", fin=True,
                                 mask=True))
        with self.assertRaises(ProtocolError) as ctx:
            server.recv_message()
        self.assertEqual(ctx.exception.close_code, 1007)
        raw.close()


if __name__ == "__main__":
    unittest.main()
